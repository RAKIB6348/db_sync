# -*- coding: utf-8 -*-
"""Configuration record: one row per remote Odoo target."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DbSyncConfig(models.Model):
    _name = "db.sync.config"
    _description = "Database Sync Configuration"
    _order = "name"

    name = fields.Char(required=True)
    url = fields.Char(
        string="Server URL",
        required=True,
        help="Base URL of the remote Odoo instance, e.g. http://duplicate:8069",
    )
    db = fields.Char(string="Database", required=True)
    username = fields.Char(required=True)
    api_key = fields.Char(
        string="API Key",
        required=True,
        help="Odoo API key of the remote user (used as the XML-RPC password).",
    )
    active = fields.Boolean(default=True)
    model_ids = fields.Many2many(
        "ir.model",
        string="Models to Sync",
        domain=[("transient", "=", False)],
        help="Only records of these models are replicated to this target.",
    )
    max_retries = fields.Integer(
        string="Max Retries",
        default=5,
        help="Maximum number of automatic retries for a failed sync log entry.",
    )
    timeout = fields.Integer(
        string="Timeout (s)",
        default=15,
        help="Connect/read timeout for XML-RPC calls.",
    )
    last_test_result = fields.Char(
        string="Last Test Result", readonly=True, copy=False
    )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_test_connection(self):
        """Authenticate against the remote server and report success/failure."""
        self.ensure_one()
        engine = self.env["db.sync.engine"]
        try:
            message = engine.test_connection(self)
        except Exception as exc:  # noqa: BLE001 - surface any RPC failure to the user
            raise UserError(
                _("Connection to %(url)s failed: %(error)s",
                  url=self.url, error=exc)
            ) from exc
        self.sudo().write({"last_test_result": message})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection successful"),
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    # ------------------------------------------------------------------
    # Cache invalidation: the engine caches the set of synced model names
    # in an ormcache; any config change must invalidate it.
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env.registry.clear_cache()
        return records

    def write(self, vals):
        res = super().write(vals)
        self.env.registry.clear_cache()
        return res

    def unlink(self):
        res = super().unlink()
        self.env.registry.clear_cache()
        return res
