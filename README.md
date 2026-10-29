# db_sync — Create replication to a duplicate Odoo database

Replicate newly created records from a **main** Odoo database to a
**duplicate** Odoo instance over XML-RPC. Create is synced now; update and
delete can be added later on the same skeleton.

## Contents

1. [Architecture](#architecture)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Test Connection](#test-connection)
5. [How it works (create sync)](#how-it-works-create-sync)
6. [ID mapping & dedup](#id-mapping--dedup)
7. [Failure handling & retry cron](#failure-handling--retry-cron)
8. [Loop prevention](#loop-prevention)
9. [Field filtering / payload rules](#field-filtering--payload-rules)
10. [Testing with res.partner](#testing-with-respartner)
11. [Unit tests](#unit-tests)
12. [Known limitations](#known-limitations)
13. [Extending later](#extending-later)

## Architecture

| Model | Purpose |
|-------|---------|
| `db.sync.config` | Remote target: URL, database, username, API key, active flag, `model_ids` opt-in, max retries. |
| `db.sync.mapping` | Main-db id → remote-db id mapping, one row per (config, model, main_id). |
| `db.sync.log` | One row per sync attempt: pending / done / failed, JSON payload, error message, retry count. |
| `db.sync.engine` | Abstract engine: postcommit queueing, XML-RPC session, payload building, retry cron. |
| `db.sync.base` | Overrides `create()` on `base` (all models) — the hot-path hook. |

The hot path on the main DB:

1. User clicks Save → `create()` runs on the opted-in model.
2. `queue_create()` runs in-process: it checks `no_db_sync`, registry readiness,
   the skip list (`db.sync.*`, `ir.*`, `mail.*`, …), and the cached set of opted-in
   model names. If nothing applies, it costs almost nothing.
3. The ids are queued in `cr.postcommit.data`, **inside the user's transaction**.
4. The main transaction commits → the postcommit callback opens a **fresh cursor**
   (SUPERUSER), authenticates once per remote target, and pushes each record.
5. Remote failures are caught per-record and written to `db.sync.log` — they
   **never** roll back or slow down the user's save in any visible way.

Because the postcommit callback runs *after* commit, a rolled-back main
transaction never creates a remote record.

## Installation

Place the `db_sync` directory in your addons path (e.g.
`custom_addons/chs_addons/db_sync` on the running Odoo 17 instance), then:

1. Restart Odoo (or use **Apps → Update Apps List**).
2. Activate **Developer Mode**.
3. Go to **Apps**, search "Database Sync", and click **Install**.

No new tables on the remote side are required — the module only creates
records there through normal XML-RPC. Install the same modules on both
databases so the remote models exist with compatible fields.

## Configuration

1. **Settings → Technical → Database Sync → Sync Targets** → **Create**.
2. Fill in:
   * **Server URL** — e.g. `http://duplicate:8069` (http:// or https://)
   * **Database** — the duplicate database name
   * **Username** — a remote Odoo user with create access on the synced models
   * **API Key** — that user's API key (used as the XML-RPC password).  
     *Alternative:* you can use that user's regular login password instead.
   * **Active** — unchecked disables this target
   * **Models to Sync** — choose the models to replicate (e.g. `res.partner`)
   * **Max Retries** — automatic retry limit for failed syncs (default 5)
3. Click **Test Connection** (see below). Save.

> **API key:** on the remote instance, open the user's form → **Actions →
> Reset API Key** (or check Preferences). The remote user needs at least
> create/write access to every model listed in **Models to Sync**.

Multiple targets can be active at once; each opted-in create is pushed to
every active target that lists its model.

## Test Connection

On the config form, click **Test Connection**. It authenticates via
`/xmlrpc/2/common`, then checks `check_access_rights('create')` on
`res.partner`. A success notification is shown; otherwise an error popup
displays the reason (wrong URL/db/user/key, or insufficient access rights).
The result is stored in **Last Test Result**.

## How it works (create sync)

Only models opted in through an active config are synced. The `create()`
override on `base` enqueues `(config_id, model_name, res_id)` into
`cr.postcommit.data` and registers a single postcommit callback per
transaction. After commit, `process_pending()`:

1. Opens one XML-RPC session per target (authenticate once).
2. For each record:
   * Checks dedup (see below); if already remote, skips.
   * Builds the payload (see field rules below).
   * Converts m2o/m2m through the mapping, syncing missing related records
     first (cycle-guarded).
   * Calls remote `create`.
   * Upserts `db.sync.mapping` and marks the log `done`.
3. Any exception → log row `failed` with the error message; the batch
   continues with the next record. Transport errors abort the remaining
   batch for that target only.

## ID mapping & dedup

Before creating remotely, the engine checks `db.sync.mapping` for
`(config, model, main_id)`. If a mapping exists and the remote record still
exists, the record is considered already synced (and refreshed with `write`).

As a fallback, if the remote model has an `x_main_id` Integer field, the
engine searches `x_main_id = main_id` and reuses that remote id, writing the
main id back into `x_main_id` on every create.

Related records (m2o / m2m) are converted through the same mapping: if a
related record is missing remotely, the engine syncs it first (as long as its
model is not in the skip list). Cycles such as `partner.company ↔
company.partner` are guarded by a per-run `visiting` set.

## Failure handling & retry cron

Every queued create writes a `pending` log row inside the user's transaction.
After the postcommit push:

* success → `done`, payload stored, error cleared
* failure → `failed`, error message stored, **never raised**

A cron (**ir.cron**) runs every **5 minutes** and retries every `pending` /
`failed` log whose `retry_count < max_retries` (default 5). Each retry
increments `retry_count`; exhausted logs stay `failed` and can be retried
manually after raising `max_retries` on the config.

**Retry button**: on the Sync Logs list/form, click **Retry** on any
non-done log to re-push it immediately.

## Loop prevention

Every remote call uses context `no_db_sync=True` (plus
`tracking_disable`, `mail_notrack`, `mail_create_nolog`, `no_reset_password`).
The local `create()` hook checks the same flag first, so records created by
the sync itself (on either side) are never queued back.

## Field filtering / payload rules

Included:
* stored, non-related, non-computed scalar fields (char, text, integer,
  float, boolean, date, datetime, selection, monetary, …)
* many2one → mapped remote id (or `False`)
* many2many → `[(6, 0, [remote ids])]`
* reference → resolved remote id

Excluded (never copied):
* `id`, `create_uid`, `create_date`, `write_uid`, `write_date`,
  `__last_update`
* computed non-stored fields, related fields, one2many (children sync via
  their own create hook)
* binary fields (images / attachments — not copied; enable later if needed)

If a related record cannot be resolved, its value is set to `False` and a
warning note is appended to the payload instead of blocking the save.

## Testing with res.partner

On the **main** database:

1. Install `db_sync`.
2. **Settings → Technical → Database Sync → Sync Targets → Create**.
3. URL = duplicate Odoo instance (e.g. `http://duplicate:8069`), database,
   username, API key. **Models to Sync** = `res.partner`. Save.
4. Click **Test Connection** → expect "Connected to …".
5. **Contacts → Create** → fill Name, email, phone → Save.
6. The save must succeed instantly. Open **Settings → Technical →
   Database Sync → Sync Logs** → you should see a `done` log for
   `res.partner`.
7. On the **duplicate** database, open Contacts → the new partner appears
   with the same name/email/phone (a different internal id, tracked in
   `db.sync.mapping`).

Negative path: point the URL at a dead host, create another contact → the
local save still succeeds, and Sync Logs shows `failed` with the connection
error; the cron retries every 5 minutes.

## Unit tests

Run without a remote server (XML-RPC is mocked):

```
./odoo-bin -d <test_db> -u db_sync --test-enable --test-tags /db_sync --stop-after-init
```

Tests cover: opt-in guards, skip-list, `no_db_sync` context, rolled-back
transactions leaving no log, payload field filtering, m2o mapping
conversion, full push with mapping + done log, remote failure → failed log,
dedup skipping a second create, remote context flag, cron retry, manual
retry, and Test Connection success/auth failure.

## Known limitations

* **Create only** — updates and deletes are not yet synced. The log model
  already has an `operation` field ready for `write`/`unlink`; add
  `write()`/`unlink()` overrides on `base` and implement
  `queue_write()`/`queue_unlink()` in the engine following the same pattern.
* **Binary fields** (images, attachments) are not copied.
* Remote instance must have the same models/modules installed; otherwise
  field lookups fail per-record and are logged.
* Mapping table is local to the main DB; if it is lost, dedup falls back to
  `x_main_id` on the remote model (if present).
* Postcommit callbacks run synchronously in the Odoo worker after commit;
  they block that worker briefly. For very high write volume, switch to
  OCA `queue_job` (see below).

## Extending later

**Add update/delete sync**: override `write()`/`unlink()` on `db_sync_base`
and call `db.sync.engine.queue_write(self, vals)` / `queue_unlink(self)`;
implement those in the engine using the same postcommit + log pattern.

**Switch from postcommit to OCA `queue_job`**: replace the
`cr.postcommit.add(...)` call in `queue_create()` with
`self.env['queue.job'].sudo().with_delay(...).process_pending(pending)` and
move `process_pending()` into a job method. Everything else (session,
payload, mapping, log, retry) stays unchanged.
