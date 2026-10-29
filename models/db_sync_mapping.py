# -*- coding: utf-8 -*-
"""Main-DB id -> remote-DB id mapping, one row per (config, model, main id)."""
from odoo import fields, models


class DbSyncMapping(models.Model):
    _name = "db.sync.mapping"
    _description = "Database Sync ID Mapping"
    _order = "id desc"

    config_id = fields.Many2one(
        "db.sync.config", required=True, ondelete="cascade", index=True
    )
    model_name = fields.Char(required=True, index=True)
    main_id = fields.Integer(required=True, index=True)
    remote_id = fields.Integer(required=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "config_model_main_uniq",
            "unique(config_id, model_name, main_id)",
            "A mapping already exists for this configuration, model and main ID.",
        ),
    ]
