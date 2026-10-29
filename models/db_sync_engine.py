# -*- coding: utf-8 -*-
"""Synchronization engine.

Responsibilities:

* decide whether a model is opted-in (ormcache-backed),
* queue created ids into ``cr.postcommit`` so the remote call runs only
  after the main transaction has committed,
* build a portable payload (field filtering + relation conversion through
  the ID mapping, syncing missing related records first),
* call the remote XML-RPC API, upsert the mapping, write the log,
* retry failed/pending logs (cron + manual button).

The remote call NEVER runs inside the user's transaction: the postcommit
callback opens a fresh cursor, and every exception is caught and written
to ``db.sync.log`` so a remote outage can never break a local save.
"""

import json
import logging
import socket
from urllib.parse import urlsplit, urlunsplit
from xmlrpc.client import ProtocolError, SafeTransport, ServerProxy, Transport

from odoo import SUPERUSER_ID, _, api, fields, models, tools

_logger = logging.getLogger(__name__)

# Context flag that disables syncing (loop prevention). It is set on every
# remote call, so if this module is also installed on the duplicate, the
# records we create there are never echoed back.
NO_SYNC_CTX_KEY = "no_db_sync"

# Technical models that must never be synced, whatever the config says.
SKIP_MODEL_PREFIXES = (
    "db.sync.",
    "ir.",
    "mail.",
    "base.",
    "web.",
    "bus.",
    "portal.",
    "http.",
    "link_tracker.",
)

# Never copied into the payload: magic / audit columns.
AUDIT_FIELDS = frozenset(
    {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update"}
)

# Remote-side optional dedup field. If the remote model has this integer
# column we store the main id there and use it to prevent duplicates even
# when the local mapping table was lost.
REMOTE_MAIN_ID_FIELD = "x_main_id"

# Transport-level errors: abort the whole batch instead of timing out on
# every remaining record.  (A remote ORM error arrives as xmlrpc Fault and
# is handled per-record.)
CONNECTION_ERRORS = (
    socket.timeout,
    TimeoutError,
    ConnectionError,
    OSError,
    ProtocolError,
)


# ---------------------------------------------------------------------------
# XML-RPC transport with timeouts
# ---------------------------------------------------------------------------
class _TimeoutTransport(Transport):
    def __init__(self, timeout, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(SafeTransport):
    def __init__(self, timeout, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _rpc_url(url, timeout):
    """Normalize the configured URL and return (base_url, transport)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError(_("Server URL is empty"))
    parsed = urlsplit(url if "://" in url else "http://" + url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(_("Server URL must be an http(s) URL"))
    netloc = parsed.hostname
    if ":" in parsed.hostname and not parsed.hostname.startswith("["):
        netloc = "[%s]" % parsed.hostname
    if parsed.port:
        netloc = "%s:%s" % (netloc, parsed.port)
    base = urlunsplit((parsed.scheme, netloc, "", "", ""))
    transport = (
        _TimeoutSafeTransport(timeout) if base.startswith("https://")
        else _TimeoutTransport(timeout)
    )
    return base, transport


# ---------------------------------------------------------------------------
# Post-commit entry point (module level: no ORM env captured in the closure)
# ---------------------------------------------------------------------------
def _run_postcommit(registry, pending):
    """Runs after the main transaction commits, with a fresh cursor.

    ``pending`` is a set of (config_id, model_name, res_id).  Never raises.
    """
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {NO_SYNC_CTX_KEY: True})
            env["db.sync.engine"].process_pending(pending)
    except Exception:  # noqa: BLE001 - background job must never propagate
        _logger.exception("db_sync: post-commit sync failed")


class DbSyncSession:
    """Per-run connection state and caches shared across records."""

    __slots__ = (
        "env", "config", "uid", "obj", "context",
        "mapping_cache", "exists_cache", "remote_fields_cache",
        "resolved_cache", "synced", "visiting",
    )

    def __init__(self, env, config, uid, obj, context):
        self.env = env
        self.config = config
        self.uid = uid
        self.obj = obj
        self.context = context
        self.mapping_cache = {}        # (model, main_id) -> remote_id
        self.exists_cache = {}         # (model, remote_id) -> bool
        self.remote_fields_cache = {}  # model -> set of field names
        self.resolved_cache = {}       # (model, main_id) -> remote_id or None
        self.synced = set()            # (model, main_id) already pushed this run
        self.visiting = set()          # cycle guard while syncing relations

    def execute(self, model, method, args, kwargs=None):
        kw = {"context": self.context}
        if kwargs:
            kw.update(kwargs)
        return self.obj.execute_kw(
            self.config.db, self.uid, self.config.api_key,
            model, method, args, kw,
        )

    def remote_exists(self, model, remote_id):
        if not remote_id:
            return False
        key = (model, remote_id)
        if key not in self.exists_cache:
            found = self.execute(model, "search", [[["id", "=", remote_id]], 0, 1])
            self.exists_cache[key] = bool(found)
        return self.exists_cache[key]

    def remote_field_names(self, model):
        """Set of field names on the remote model (cached per run)."""
        if model not in self.remote_fields_cache:
            try:
                info = self.execute(model, "fields_get", [None, ["type"]])
            except Exception:  # noqa: BLE001 - model may not exist remotely
                info = {}
            self.remote_fields_cache[model] = set(info)
        return self.remote_fields_cache[model]


class DbSyncEngine(models.AbstractModel):
    _name = "db.sync.engine"
    _description = "Database Sync Engine"

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    @api.model
    @tools.ormcache()
    def _synced_model_names(self):
        """Frozenset of model technical names opted-in by any active config.

        Cached in the registry ormcache; invalidated by db.sync.config
        create/write/unlink via ``registry.clear_cache()``.
        """
        configs = self.env["db.sync.config"].sudo().search([("active", "=", True)])
        names = set()
        for config in configs:
            names.update(config.model_ids.mapped("model"))
        return frozenset(names)

    @api.model
    def _configs_for_model(self, model_name):
        """Active configs that opted-in *model_name*."""
        return self.env["db.sync.config"].sudo().search(
            [("active", "=", True), ("model_ids.model", "=", model_name)]
        )

    @api.model
    def _should_skip_model(self, model_name):
        """True for technical models that must never be synced."""
        if not model_name or model_name == "base":
            return True
        return model_name.startswith(SKIP_MODEL_PREFIXES)

    # ------------------------------------------------------------------
    # Queueing (called from the base create() hook)
    # ------------------------------------------------------------------
    @api.model
    def queue_create(self, records):
        """Queue *records* for a post-commit remote create.

        Cheap guards first so the common case (model not opted-in) costs
        almost nothing on the hot create path.  A ``pending`` log row is
        written inside the same transaction, so a rolled-back save leaves
        no log, while a committed save whose postcommit callback never ran
        (process crash) is still retried by the cron.
        """
        if not records or not self.env.registry.ready:
            return
        ctx = self.env.context
        if ctx.get(NO_SYNC_CTX_KEY) or ctx.get("install_mode"):
            return
        model_name = records._name
        if self._should_skip_model(model_name):
            return
        if model_name not in self._synced_model_names():
            return

        configs = self._configs_for_model(model_name)
        if not configs:
            return

        Log = self.env["db.sync.log"].sudo()
        cr = self.env.cr
        pending = cr.postcommit.data.setdefault("db_sync.pending", set())
        for config in configs:
            for record in records:
                pending.add((config.id, model_name, record.id))
                # Pending log in the user's transaction (rolls back with it).
                existing = Log.search(
                    [
                        ("config_id", "=", config.id),
                        ("model_name", "=", model_name),
                        ("res_id", "=", record.id),
                        ("operation", "=", "create"),
                        ("state", "=", "pending"),
                    ],
                    limit=1,
                )
                if not existing:
                    Log.create(
                        {
                            "config_id": config.id,
                            "model_name": model_name,
                            "res_id": record.id,
                            "operation": "create",
                            "state": "pending",
                        }
                    )

        if not cr.postcommit.data.get("db_sync.callback"):
            cr.postcommit.data["db_sync.callback"] = True
            registry = self.env.registry
            cr.postcommit.add(lambda: _run_postcommit(registry, pending))

    # ------------------------------------------------------------------
    # Post-commit processing
    # ------------------------------------------------------------------
    @api.model
    def process_pending(self, pending):
        """Push every queued (config_id, model, id) to its remote target."""
        if not pending:
            return
        # Group by config so each target gets a single authenticated session.
        by_config = {}
        for config_id, model_name, res_id in pending:
            by_config.setdefault(config_id, set()).add((model_name, res_id))

        for config_id, entries in by_config.items():
            config = self.env["db.sync.config"].sudo().browse(config_id).exists()
            if not config or not config.active:
                continue
            try:
                session = self._open_session(config)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "db_sync: cannot connect to %s (%s): %s",
                    config.name, config.url, exc,
                )
                self._log_batch_failure(config, entries, exc)
                continue
            self._push_entries(config, session, entries)

    def _push_entries(self, config, session, entries):
        """Sync each entry; transport errors abort the rest of the batch."""
        for model_name, res_id in sorted(entries):
            key = (model_name, res_id)
            if key in session.synced:
                continue
            try:
                self._sync_one(session, model_name, res_id, operation="create")
            except CONNECTION_ERRORS:
                _logger.warning(
                    "db_sync: connection to %s lost, aborting batch", config.name
                )
                self._log_failure(
                    config, model_name, res_id, "create",
                    _("Connection to the remote server failed"),
                )
                break
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                _logger.exception(
                    "db_sync: could not sync %s id %s", model_name, res_id
                )
                self._log_failure(config, model_name, res_id, "create", exc)

    # ------------------------------------------------------------------
    # Single-record sync
    # ------------------------------------------------------------------
    def _sync_one(self, session, model_name, res_id, operation="create"):
        """Create (or re-create) one record remotely and save the mapping.

        Returns the remote id, or None when there is nothing to do
        (record deleted before the callback ran / already pushed this run).
        """
        model = session.env.get(model_name)
        if model is None:
            raise ValueError(_("Model %s is not available", model_name))
        record = model.sudo().browse(res_id).exists()
        if not record:
            session.synced.add((model_name, res_id))
            return None
        if (model_name, res_id) in session.synced:
            return self._mapping_lookup(session, model_name, res_id)
        if (model_name, res_id) in session.visiting:
            return None

        session.visiting.add((model_name, res_id))
        try:
            # 1. Dedup: mapping first, then remote x_main_id.
            remote_id = self._resolve_self(session, model_name, res_id)
            values, notes = self._build_payload(session, record)

            if remote_id:
                # Already present remotely (retry path) - refresh it.
                session.execute(model_name, "write", [[remote_id], values])
            else:
                if REMOTE_MAIN_ID_FIELD in session.remote_field_names(model_name):
                    values[REMOTE_MAIN_ID_FIELD] = res_id
                remote_id = session.execute(model_name, "create", [values])

            self._save_mapping(session, model_name, res_id, remote_id)
            self._save_log_done(session, model_name, res_id, operation, values, notes)
            session.synced.add((model_name, res_id))
            return remote_id
        finally:
            session.visiting.discard((model_name, res_id))

    # ------------------------------------------------------------------
    # Self resolution / dedup
    # ------------------------------------------------------------------
    def _resolve_self(self, session, model_name, res_id):
        """Return the remote id for a main record, or None if it must be created."""
        # 1. Local mapping table.
        remote_id = self._mapping_lookup(session, model_name, res_id)
        if remote_id and session.remote_exists(model_name, remote_id):
            return remote_id
        # 2. Remote-side x_main_id fallback (mapping lost, record still there).
        if REMOTE_MAIN_ID_FIELD in session.remote_field_names(model_name):
            found = session.execute(
                model_name, "search",
                [[[REMOTE_MAIN_ID_FIELD, "=", res_id]], 0, 1],
            )
            if found:
                session.mapping_cache[(model_name, res_id)] = found[0]
                return found[0]
        return None

    def _mapping_lookup(self, session, model_name, main_id):
        key = (model_name, main_id)
        if key in session.mapping_cache:
            return session.mapping_cache[key]
        row = session.env["db.sync.mapping"].sudo().search(
            [
                ("config_id", "=", session.config.id),
                ("model_name", "=", model_name),
                ("main_id", "=", main_id),
            ],
            limit=1,
        )
        remote_id = row.remote_id if row else None
        session.mapping_cache[key] = remote_id
        return remote_id

    def _save_mapping(self, session, model_name, main_id, remote_id):
        Mapping = session.env["db.sync.mapping"].sudo()
        row = Mapping.search(
            [
                ("config_id", "=", session.config.id),
                ("model_name", "=", model_name),
                ("main_id", "=", main_id),
            ],
            limit=1,
        )
        if row:
            if row.remote_id != remote_id:
                row.remote_id = remote_id
        else:
            Mapping.create(
                {
                    "config_id": session.config.id,
                    "model_name": model_name,
                    "main_id": main_id,
                    "remote_id": remote_id,
                }
            )
        session.mapping_cache[(model_name, main_id)] = remote_id

    # ------------------------------------------------------------------
    # Relation resolution (m2o / m2m -> remote ids)
    # ------------------------------------------------------------------
    def _resolve_related(self, session, comodel, main_id):
        """Remote id for a related main record; sync it first if missing.

        Returns None when the related record cannot be synchronized (technical
        model, cycle, remote failure) - the caller drops the value and logs
        a warning instead of blocking the main save.
        """
        if not main_id:
            return None
        key = (comodel, main_id)
        if key in session.resolved_cache:
            return session.resolved_cache[key]
        if key in session.visiting:
            # Cycle (e.g. partner.company -> company.partner): leave unresolved.
            session.resolved_cache[key] = None
            return None
        if self._should_skip_model(comodel):
            session.resolved_cache[key] = None
            return None

        remote_id = self._resolve_self(session, comodel, main_id)
        if not remote_id:
            try:
                remote_id = self._sync_one(session, comodel, main_id, operation="create")
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "db_sync: could not sync related %s id %s: %s",
                    comodel, main_id, exc,
                )
                remote_id = None
        session.resolved_cache[key] = remote_id
        return remote_id

    # ------------------------------------------------------------------
    # Payload building
    # ------------------------------------------------------------------
    def _build_payload(self, session, record):
        """Return (values, notes) for a create/write payload.

        Only stored, non-computed, non-related fields are included.
        One2many children are synced through their own create hook (their
        inverse m2o is resolved via the mapping).  Binary fields
        (images / attachments) are never copied.
        """
        field_names = []
        for name, field in record._fields.items():
            if name in AUDIT_FIELDS or name.startswith("_"):
                continue
            if not field.store or field.related or field.compute:
                continue
            if field.type in ("one2many", "binary"):
                continue
            field_names.append(name)

        values = {}
        notes = []
        if not field_names:
            return values, notes

        data = record.sudo().read(field_names, load="_classic_write")[0]
        for name in field_names:
            field = record._fields[name]
            value = data.get(name)
            if field.type == "many2one":
                # load='_classic_write' returns the bare id (or False).
                local_id = value[0] if isinstance(value, (list, tuple)) else value
                if not local_id:
                    values[name] = False
                    continue
                remote_id = self._resolve_related(session, field.comodel_name, local_id)
                if remote_id:
                    values[name] = remote_id
                else:
                    values[name] = False
                    notes.append(
                        "unresolved m2o %s -> %s#%s" % (name, field.comodel_name, local_id)
                    )
            elif field.type == "many2many":
                remote_ids = []
                for local_id in value or []:
                    remote_id = self._resolve_related(session, field.comodel_name, local_id)
                    if remote_id:
                        remote_ids.append(remote_id)
                    else:
                        notes.append(
                            "unresolved m2m %s -> %s#%s"
                            % (name, field.comodel_name, local_id)
                        )
                values[name] = [(6, 0, remote_ids)]
            elif field.type == "reference":
                # value is "model,id" (classic) or False.
                if isinstance(value, str) and "," in value:
                    ref_model, _, ref_id = value.partition(",")
                    remote_id = self._resolve_related(
                        session, ref_model, int(ref_id) if ref_id else 0
                    )
                    values[name] = (
                        "%s,%s" % (ref_model, remote_id) if remote_id else False
                    )
                else:
                    values[name] = value or False
            else:
                values[name] = value if value is not None else False
        return values, notes

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log_batch_failure(self, config, entries, exc):
        for model_name, res_id in entries:
            self._log_failure(config, model_name, res_id, "create", exc)

    def _log_failure(self, config, model_name, res_id, operation, exc):
        if isinstance(exc, str):
            message = exc
        else:
            message = "%s: %s" % (type(exc).__name__, exc)
        try:
            Log = self.env["db.sync.log"].sudo()
            existing = Log.search(
                [
                    ("config_id", "=", config.id),
                    ("model_name", "=", model_name),
                    ("res_id", "=", res_id),
                    ("operation", "=", operation),
                    ("state", "=", "pending"),
                ],
                limit=1,
            )
            vals = {"state": "failed", "error_message": message}
            if existing:
                existing.write(vals)
            else:
                Log.create(
                    dict(
                        vals,
                        config_id=config.id,
                        model_name=model_name,
                        res_id=res_id,
                        operation=operation,
                    )
                )
        except Exception:  # noqa: BLE001
            _logger.exception("db_sync: could not write failure log")

    def _save_log_done(self, session, model_name, res_id, operation, values, notes):
        payload = dict(values)
        if notes:
            payload["_notes"] = list(notes)
        try:
            Log = self.env["db.sync.log"].sudo()
            existing = Log.search(
                [
                    ("config_id", "=", session.config.id),
                    ("model_name", "=", model_name),
                    ("res_id", "=", res_id),
                    ("operation", "=", operation),
                    ("state", "!=", "done"),
                ],
                limit=1,
            )
            vals = {
                "payload": json.dumps(payload, default=str, ensure_ascii=False),
                "state": "done",
                "error_message": False,
            }
            if existing:
                existing.write(vals)
            else:
                Log.create(
                    dict(
                        vals,
                        config_id=session.config.id,
                        model_name=model_name,
                        res_id=res_id,
                        operation=operation,
                    )
                )
        except Exception:  # noqa: BLE001
            _logger.exception("db_sync: could not write success log")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    @api.model
    def _open_session(self, config):
        """Authenticate against the remote server and return a session."""
        base, transport = _rpc_url(config.url, config.timeout or 15)
        common = ServerProxy(
            base + "/xmlrpc/2/common", transport=transport, allow_none=True
        )
        uid = common.authenticate(config.db, config.username, config.api_key, {})
        if not uid:
            raise PermissionError(
                _("Authentication failed for %(user)s@%(db)s",
                  user=config.username, db=config.db)
            )
        obj = ServerProxy(
            base + "/xmlrpc/2/object", transport=transport, allow_none=True
        )
        context = {
            NO_SYNC_CTX_KEY: True,       # loop prevention on the remote
            "tracking_disable": True,
            "mail_notrack": True,
            "mail_create_nolog": True,
            "no_reset_password": True,
        }
        return DbSyncSession(self.env, config, uid, obj, context)

    @api.model
    def test_connection(self, config):
        """Return a human-readable success message or raise on failure."""
        base, transport = _rpc_url(config.url, config.timeout or 15)
        common = ServerProxy(
            base + "/xmlrpc/2/common", transport=transport, allow_none=True
        )
        version = common.version()
        uid = common.authenticate(config.db, config.username, config.api_key, {})
        if not uid:
            raise PermissionError(
                _("Authentication failed for %(user)s@%(db)s",
                  user=config.username, db=config.db)
            )
        obj = ServerProxy(
            base + "/xmlrpc/2/object", transport=transport, allow_none=True
        )
        # Make sure the remote user can actually create records.
        can_create = obj.execute_kw(
            config.db, uid, config.api_key,
            "res.partner", "check_access_rights", ["create", False],
            {"context": {}},
        )
        if not can_create:
            raise PermissionError(
                _("User %(user)s cannot create records on the remote database",
                  user=config.username)
            )
        return _(
            "Connected to %(db)s as uid %(uid)s (server %(version)s).",
            db=config.db, uid=uid,
            version=version.get("server_version", "?"),
        )

    # ------------------------------------------------------------------
    # Retry (cron + manual button)
    # ------------------------------------------------------------------
    @api.model
    def cron_retry_logs(self):
        """Retry every failed/pending log below its config's max_retries."""
        logs = self.env["db.sync.log"].sudo().search(
            [("state", "in", ("pending", "failed"))]
        )
        by_config = {}
        for log in logs:
            max_retries = log.config_id.max_retries or 5
            if log.retry_count >= max_retries:
                continue
            by_config.setdefault(log.config_id.id, self.env["db.sync.log"].sudo())
            by_config[log.config_id.id] |= log
        for config_id, config_logs in by_config.items():
            config = self.env["db.sync.config"].sudo().browse(config_id)
            self._retry_for_config(config, config_logs)

    def retry_logs(self, logs):
        """Manually retry the given log records (Retry button)."""
        by_config = {}
        for log in logs.sudo():
            by_config.setdefault(log.config_id.id, self.env["db.sync.log"].sudo())
            by_config[log.config_id.id] |= log
        for config_id, config_logs in by_config.items():
            config = self.env["db.sync.config"].sudo().browse(config_id)
            self._retry_for_config(config, config_logs)

    def _retry_for_config(self, config, logs):
        if not config or not config.exists():
            logs.write(
                {"state": "failed", "error_message": _("Configuration deleted")}
            )
            return
        try:
            session = self._open_session(config)
        except Exception as exc:  # noqa: BLE001
            self._bump_retries(logs, exc)
            return
        for log in logs:
            try:
                model = self.env.get(log.model_name)
                if model is None or not model.sudo().browse(log.res_id).exists():
                    log.write(
                        {
                            "state": "failed",
                            "error_message": _("Record no longer exists"),
                            "retry_count": log.retry_count + 1,
                            "last_retry_date": fields.Datetime.now(),
                        }
                    )
                    continue
                self._sync_one(
                    session, log.model_name, log.res_id, operation=log.operation
                )
                log.write(
                    {
                        "state": "done",
                        "error_message": False,
                        "retry_count": log.retry_count + 1,
                        "last_retry_date": fields.Datetime.now(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "db_sync: retry failed for %s id %s", log.model_name, log.res_id
                )
                log.write(
                    {
                        "state": "failed",
                        "error_message": "%s: %s" % (type(exc).__name__, exc),
                        "retry_count": log.retry_count + 1,
                        "last_retry_date": fields.Datetime.now(),
                    }
                )

    def _bump_retries(self, logs, exc):
        message = "%s: %s" % (type(exc).__name__, exc)
        for log in logs:
            log.write(
                {
                    "state": "failed",
                    "error_message": message,
                    "retry_count": log.retry_count + 1,
                    "last_retry_date": fields.Datetime.now(),
                }
            )
