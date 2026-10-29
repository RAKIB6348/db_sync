# -*- coding: utf-8 -*-
{
    "name": "Database Sync",
    "summary": "Create-sync selected models to a duplicate Odoo database over XML-RPC",
    "description": """
Whenever a record is created on the main database, the same record is
replicated to a configured duplicate Odoo instance over XML-RPC.

* Configuration model with per-model opt-in (Settings > Technical)
* Post-commit replication: a rolled-back transaction never reaches the remote
* ID mapping (db.sync.mapping) + optional remote ``x_main_id`` dedup
* Failure logs with a 5-minute retry cron and a manual Retry button
* Loop prevention via the ``no_db_sync`` context flag
""",
    "author": "CHS",
    "category": "Tools",
    "version": "17.0.1.0.0",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "views/db_sync_config_views.xml",
        "views/db_sync_log_views.xml",
        "data/db_sync_cron_data.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
