# -*- coding: utf-8 -*-
"""Global ``create()`` hook.

Every model implicitly inherits ``base``, so overriding it here intercepts
record creation on all models with near-zero cost for non-opted-in models
(the guards below run before any DB query or RPC).
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class DbSyncBase(models.AbstractModel):
    _inherit = "base"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Never let a sync problem break the user's save.
        try:
            self.env["db.sync.engine"].queue_create(records)
        except Exception:  # noqa: BLE001
            _logger.exception("db_sync: could not queue records for synchronization")
        return records

    # Future: add write()/unlink() here using the same pattern, e.g.
    #
    #   def write(self, vals):
    #       result = super().write(vals)
    #       try:
    #           self.env["db.sync.engine"].queue_write(self, vals)
    #       except Exception:
    #           _logger.exception("db_sync: could not queue write")
    #       return result
    #
    # and implement queue_write/queue_unlink in db_sync_engine.py. The log
    # model already has an ``operation`` field ready for "write"/"unlink".
