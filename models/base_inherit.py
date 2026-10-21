from odoo import api, models


class Base(models.AbstractModel):
    _inherit = 'base'

    def _db_sync_enabled(self):
        return (not self.env.context.get('skip_db_sync')
                and self._name in self.env['db.sync.config']._get_synced_models())

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if records and records._db_sync_enabled():
            self.env['db.sync.config'].sync_records(records, vals_list, 'create')
        return records

    def write(self, vals):
        res = super().write(vals)
        if vals and self._db_sync_enabled():
            self.env['db.sync.config'].sync_records(
                self, [vals] * len(self), 'write')
        return res
