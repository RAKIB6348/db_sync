# -*- coding: utf-8 -*-
"""Tests for the db_sync module.

The XML-RPC layer is mocked: no remote server is required.  Post-commit
callbacks are not executed by TestCursor, so tests drive
``process_pending()`` directly after asserting that the ids were queued.
"""

from unittest.mock import ANY, MagicMock, patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.db_sync.models.db_sync_engine import DbSyncSession, NO_SYNC_CTX_KEY


@tagged("post_install", "-at_install")
class TestDbSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner_model = cls.env["ir.model"]._get("res.partner")
        cls.config = cls.env["db.sync.config"].create(
            {
                "name": "Duplicate",
                "url": "http://duplicate:8069",
                "db": "duplicate_db",
                "username": "admin",
                "api_key": "secret",
                "model_ids": [(6, 0, cls.partner_model.ids)],
            }
        )
        # Invalidate the ormcache filled before the config existed.
        cls.env.registry.clear_cache()

    def _mock_remote(self, create_return=42, fields_get=None, existing_ids=None):
        """Patch ServerProxy so authenticate/create/search behave like a remote.

        :param existing_ids: remote ids that a bare ``search`` reports as
            found (used by the exists-check after a mapping lookup).
        """
        common = MagicMock()
        common.authenticate.return_value = 1
        common.version.return_value = {"server_version": "17.0"}
        obj = MagicMock()
        existing_ids = existing_ids or []

        def execute_kw(db, uid, pwd, model, method, args, kwargs=None):
            if method == "fields_get":
                return fields_get or {}
            if method == "create":
                return create_return
            if method == "search":
                # exists-check for known remote ids; x_main_id lookup finds none
                domain = args[0][0] if args and args[0] else None
                # domain is [field, operator, value], e.g. ["id", "=", 555]
                if (
                    domain
                    and len(domain) == 3
                    and domain[0] == "id"
                    and domain[2] in existing_ids
                ):
                    return [domain[2]]
                return []
            if method == "write":
                return True
            if method == "check_access_rights":
                return True
            return []

        obj.execute_kw.side_effect = execute_kw

        def factory(url, **kwargs):
            return common if url.endswith("/common") else obj

        return patch(
            "odoo.addons.db_sync.models.db_sync_engine.ServerProxy",
            side_effect=factory,
        ), common, obj

    # ------------------------------------------------------------------
    # Queueing guards
    # ------------------------------------------------------------------
    def test_queue_skips_non_opted_in_model(self):
        """A model absent from model_ids must not be queued."""
        self.env["res.company"].create({"name": "Not Synced Co"})
        pending = self.env.cr.postcommit.data.get("db_sync.pending") or set()
        self.assertFalse(
            any(entry[1] == "res.company" for entry in pending),
            "res.company must not be queued",
        )

    def test_queue_skips_technical_models(self):
        """db.sync.* records are never queued (skip list wins over opt-in)."""
        # Creating a config itself must not queue a db.sync.config sync.
        self.env["db.sync.config"].create(
            {
                "name": "Second",
                "url": "http://x:8069",
                "db": "x",
                "username": "u",
                "api_key": "k",
            }
        )
        pending = self.env.cr.postcommit.data.get("db_sync.pending") or set()
        self.assertFalse(any(entry[1].startswith("db.sync.") for entry in pending))
        self.assertFalse(any(entry[1].startswith("ir.") for entry in pending))
        self.assertFalse(any(entry[1].startswith("mail.") for entry in pending))

    def test_queue_skips_no_db_sync_context(self):
        """Context flag no_db_sync=True must suppress queueing (loop guard)."""
        self.env["res.partner"].with_context(**{NO_SYNC_CTX_KEY: True}).create(
            {"name": "Loop Guard"}
        )
        pending = self.env.cr.postcommit.data.get("db_sync.pending") or set()
        self.assertFalse(
            any(entry[1] == "res.partner" for entry in pending),
            "no_db_sync context must prevent queueing",
        )

    def test_queue_registers_partner_create(self):
        """An opted-in res.partner create is queued with a pending log."""
        partner = self.env["res.partner"].create({"name": "Queued Partner"})
        pending = self.env.cr.postcommit.data.get("db_sync.pending") or set()
        self.assertIn((self.config.id, "res.partner", partner.id), pending)
        log = self.env["db.sync.log"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("res_id", "=", partner.id),
                ("state", "=", "pending"),
            ]
        )
        self.assertEqual(len(log), 1, "a pending log row must exist after create")

    def test_rolled_back_save_leaves_no_log(self):
        """A rolled-back transaction must not leave a pending log behind."""
        partner_id = None
        try:
            with self.env.cr.savepoint():
                partner = self.env["res.partner"].create({"name": "Doomed"})
                partner_id = partner.id
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        log = self.env["db.sync.log"].search(
            [("model_name", "=", "res.partner"), ("res_id", "=", partner_id)]
        )
        self.assertFalse(log, "rolled-back create must not leave a sync log")

    # ------------------------------------------------------------------
    # Payload building
    # ------------------------------------------------------------------
    def test_payload_filters_audit_and_computed_fields(self):
        """Payload excludes id, audit columns and non-stored computed fields."""
        partner = self.env["res.partner"].create({"name": "Payload Test"})
        engine = self.env["db.sync.engine"]
        config = self.config
        session = DbSyncSession(self.env, config, 1, MagicMock(), {})
        values, _notes = engine._build_payload(session, partner)
        self.assertNotIn("id", values)
        self.assertNotIn("create_uid", values)
        self.assertNotIn("create_date", values)
        self.assertNotIn("write_uid", values)
        self.assertNotIn("write_date", values)
        # display_name is computed & non-stored
        self.assertNotIn("display_name", values)
        # image_1920 is binary
        self.assertNotIn("image_1920", values)
        # a plain stored field is present
        self.assertIn("name", values)
        self.assertEqual(values["name"], "Payload Test")

    def test_payload_converts_many2one_via_mapping(self):
        """m2o values are replaced by the mapped remote id."""
        parent = self.env["res.partner"].create({"name": "Parent"})
        child = self.env["res.partner"].create(
            {"name": "Child", "parent_id": parent.id}
        )
        # Pretend the parent was already synced as remote id 777.
        self.env["db.sync.mapping"].create(
            {
                "config_id": self.config.id,
                "model_name": "res.partner",
                "main_id": parent.id,
                "remote_id": 777,
            }
        )
        engine = self.env["db.sync.engine"]
        # Mock remote object: fields_get/search return a known remote id.
        obj = MagicMock()

        def fake_execute_kw(db, uid, pwd, model, method, args, kwargs=None):
            if method in ("fields_get", "search"):
                return [777] if method == "search" else {}
            return []

        obj.execute_kw.side_effect = fake_execute_kw
        session = DbSyncSession(self.env, self.config, 1, obj, {})
        # remote_exists must confirm 777 exists.
        values, notes = engine._build_payload(session, child)
        self.assertEqual(values.get("parent_id"), 777)
        self.assertEqual(notes, [])

    # ------------------------------------------------------------------
    # Full push with a mocked remote
    # ------------------------------------------------------------------
    def test_process_pending_creates_remote_and_mapping(self):
        """A queued partner is created remotely and a mapping is stored."""
        partner = self.env["res.partner"].create({"name": "Push Me"})
        pending = set(self.env.cr.postcommit.data.get("db_sync.pending") or set())
        self.assertIn((self.config.id, "res.partner", partner.id), pending)

        engine = self.env["db.sync.engine"]
        patcher, _common, obj = self._mock_remote(create_return=999)
        with patcher:
            engine.process_pending(pending)

        obj.execute_kw.assert_any_call(
            self.config.db, 1, self.config.api_key,
            "res.partner", "create", [ANY], ANY,
        )
        mapping = self.env["db.sync.mapping"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("main_id", "=", partner.id),
            ]
        )
        self.assertEqual(mapping.remote_id, 999)
        log = self.env["db.sync.log"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("res_id", "=", partner.id),
            ]
        )
        self.assertEqual(log.state, "done")
        self.assertTrue(log.payload)

    def test_process_pending_failure_writes_failed_log(self):
        """A remote error marks the log failed and never raises."""
        partner = self.env["res.partner"].create({"name": "Will Fail"})
        pending = set(self.env.cr.postcommit.data.get("db_sync.pending") or set())
        engine = self.env["db.sync.engine"]

        common = MagicMock()
        common.authenticate.return_value = 1
        obj = MagicMock()
        obj.execute_kw.side_effect = Exception("remote exploded")
        with patch(
            "odoo.addons.db_sync.models.db_sync_engine.ServerProxy",
            side_effect=lambda url, **kw: common if url.endswith("/common") else obj,
        ):
            engine.process_pending(pending)  # must not raise

        log = self.env["db.sync.log"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("res_id", "=", partner.id),
            ]
        )
        self.assertTrue(log, "a log row must exist after a failed push")
        self.assertEqual(log.state, "failed")
        self.assertIn("remote exploded", log.error_message)

    def test_dedup_skips_second_create(self):
        """An existing mapping prevents a second remote create."""
        partner = self.env["res.partner"].create({"name": "Dedup"})
        self.env["db.sync.mapping"].create(
            {
                "config_id": self.config.id,
                "model_name": "res.partner",
                "main_id": partner.id,
                "remote_id": 555,
            }
        )
        engine = self.env["db.sync.engine"]
        # The mapping's remote id 555 must exist on the remote.
        patcher, _common, obj = self._mock_remote(existing_ids=[555])
        with patcher:
            engine.process_pending({(self.config.id, "res.partner", partner.id)})

        # Only search/write calls - no create for this record.
        create_calls = [
            c for c in obj.execute_kw.call_args_list
            if c.args[3] == "res.partner" and c.args[4] == "create"
        ]
        self.assertEqual(create_calls, [], "existing mapping must skip create")

    def test_loop_prevention_context_on_remote_call(self):
        """Remote execute_kw receives no_db_sync=True in its context."""
        partner = self.env["res.partner"].create({"name": "Ctx"})
        engine = self.env["db.sync.engine"]
        patcher, _common, obj = self._mock_remote()
        with patcher:
            engine.process_pending({(self.config.id, "res.partner", partner.id)})

        create_call = next(
            c for c in obj.execute_kw.call_args_list
            if c.args[4] == "create"
        )
        kwargs = create_call.args[6]
        self.assertTrue(kwargs["context"].get(NO_SYNC_CTX_KEY))

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------
    def test_cron_retries_failed_log(self):
        """The cron picks up a failed log and re-pushes it successfully."""
        partner = self.env["res.partner"].create({"name": "Retry Me"})
        log = self.env["db.sync.log"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("res_id", "=", partner.id),
            ],
            limit=1,
        )
        log.write({"state": "failed", "error_message": "boom", "retry_count": 0})

        engine = self.env["db.sync.engine"]
        patcher, _common, _obj = self._mock_remote(create_return=321)
        with patcher:
            engine.cron_retry_logs()

        log.invalidate_recordset()
        self.assertEqual(log.state, "done")
        self.assertEqual(log.retry_count, 1)
        mapping = self.env["db.sync.mapping"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("main_id", "=", partner.id),
            ]
        )
        self.assertEqual(mapping.remote_id, 321)

    def test_retry_respects_max_retries(self):
        """Logs at max_retries are left untouched by the cron."""
        partner = self.env["res.partner"].create({"name": "Exhausted"})
        log = self.env["db.sync.log"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("res_id", "=", partner.id),
            ],
            limit=1,
        )
        log.write({"state": "failed", "error_message": "boom", "retry_count": 5})

        engine = self.env["db.sync.engine"]
        patcher, _common, obj = self._mock_remote()
        with patcher:
            engine.cron_retry_logs()

        log.invalidate_recordset()
        self.assertEqual(log.state, "failed")
        self.assertEqual(log.retry_count, 5)

    def test_manual_retry_button(self):
        """action_retry on a failed log re-runs the sync."""
        partner = self.env["res.partner"].create({"name": "Manual"})
        log = self.env["db.sync.log"].search(
            [
                ("config_id", "=", self.config.id),
                ("model_name", "=", "res.partner"),
                ("res_id", "=", partner.id),
            ],
            limit=1,
        )
        log.write({"state": "failed", "error_message": "x", "retry_count": 0})
        patcher, _common, _obj = self._mock_remote(create_return=77)
        with patcher:
            log.action_retry()
        log.invalidate_recordset()
        self.assertEqual(log.state, "done")

    # ------------------------------------------------------------------
    # Test Connection
    # ------------------------------------------------------------------
    def test_connection_success(self):
        patcher, _common, _obj = self._mock_remote()
        with patcher:
            message = self.env["db.sync.engine"].test_connection(self.config)
        self.assertIn("duplicate_db", message)

    def test_connection_auth_failure_raises(self):
        common = MagicMock()
        common.authenticate.return_value = False
        common.version.return_value = {"server_version": "17.0"}
        with patch(
            "odoo.addons.db_sync.models.db_sync_engine.ServerProxy",
            return_value=common,
        ):
            with self.assertRaises(PermissionError):
                self.env["db.sync.engine"].test_connection(self.config)
