# -*- coding: utf-8 -*-
"""Audit log for every sync attempt (pending / done / failed)."""
import json

from odoo import _, api, fields, models


class DbSyncLog(models.Model):
    _name = "db.sync.log"
    _description = "Database Sync Log"
    _order = "create_date desc, id desc"

    config_id = fields.Many2one(
        "db.sync.config", string="Target", ondelete="set null", index=True
    )
    model_name = fields.Char(string="Model", required=True, index=True)
    res_id = fields.Integer(string="Record ID", required=True, index=True)
    operation = fields.Selection(
        [("create", "Create"), ("write", "Write"), ("unlink", "Unlink")],
        required=True,
        default="create",
        index=True,
    )
    payload = fields.Text(
        string="Payload (JSON)",
        help="JSON snapshot of the values that were (or will be) sent.",
    )
    state = fields.Selection(
        [("pending", "Pending"), ("done", "Done"), ("failed", "Failed")],
        required=True,
        default="pending",
        index=True,
    )
    error_message = fields.Text(string="Error")
    retry_count = fields.Integer(string="Retries", default=0)
    last_retry_date = fields.Datetime(readonly=True)

    def _set_payload(self, values):
        """Serialize *values* into the JSON payload column."""
        self.ensure_one()
        self.payload = json.dumps(values, default=str, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Manual retry
    # ------------------------------------------------------------------
    def action_retry(self):
        """Retry the selected log entries right now (synchronously)."""
        logs = self.filtered(lambda l: l.state != "done")
        if not logs:
            return False
        self.env["db.sync.engine"].retry_logs(logs)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "view_mode": "tree,form",
            "domain": [("id", "in", logs.ids)],
            "target": "current",
        }

    @api.model
    def _cron_retry(self):
        """Entry point for the 5-minute retry cron."""
        self.env["db.sync.engine"].cron_retry_logs()
