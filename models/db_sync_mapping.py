from odoo import fields, models


class DbSyncMapping(models.Model):
    """Main DB er record ID  <->  Target DB er record ID."""
    _name = 'db.sync.mapping'
    _description = 'Database Sync ID Mapping'

    config_id = fields.Many2one('db.sync.config', required=True,
                                ondelete='cascade', index=True)
    model = fields.Char(required=True, index=True)
    source_id = fields.Integer(required=True, index=True)
    target_id = fields.Integer(required=True)

    _sql_constraints = [
        ('uniq_mapping', 'unique(config_id, model, source_id)',
         'Ei record er mapping already ache.'),
    ]
