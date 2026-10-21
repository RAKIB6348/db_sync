import logging
import xmlrpc.client
from datetime import date, datetime

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Odoo er technical field, eigulo sync hobe na
MAGIC_FIELDS = {'id', 'create_uid', 'create_date', 'write_uid',
                'write_date', '__last_update', 'display_name'}


class DbSyncConfig(models.Model):
    _name = 'db.sync.config'
    _description = 'Database Sync Configuration'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    url = fields.Char(string='Target URL', required=True,
                      help='e.g. http://localhost:8069')
    db_name = fields.Char(string='Target Database', required=True)
    username = fields.Char(required=True)
    password = fields.Char(required=True, help='Password ba API key')
    model_ids = fields.Many2many('ir.model', string='Models to Sync',
                                 ondelete='cascade')
    assume_same_id = fields.Boolean(
        string='Same ID dhore nao (Duplicated DB)',
        help='Target DB jodi main DB er duplicate hoy, tahole ager record '
             'gulor ID duita DB te same. Eta tick korle module update er '
             'somoy target e same ID er record khuje oitai update kore. '
             'Na korle ager record update korle target e notun record '
             'toiri hobe.')
    mapping_ids = fields.One2many('db.sync.mapping', 'config_id')

    # ---------------- cache ----------------
    @api.model
    @tools.ormcache()
    def _get_synced_models(self):
        configs = self.sudo().search([('active', '=', True)])
        return frozenset(configs.mapped('model_ids.model'))

    @api.model_create_multi
    def create(self, vals_list):
        self.env.registry.clear_cache()
        return super().create(vals_list)

    def write(self, vals):
        self.env.registry.clear_cache()
        return super().write(vals)

    def unlink(self):
        self.env.registry.clear_cache()
        return super().unlink()

    # ---------------- connection ----------------
    def _connect(self):
        self.ensure_one()
        common = xmlrpc.client.ServerProxy(
            f'{self.url}/xmlrpc/2/common', allow_none=True)
        uid = common.authenticate(self.db_name, self.username, self.password, {})
        if not uid:
            raise UserError(_('Target DB te login hoy nai. User/password check koro.'))
        return {
            'uid': uid,
            'obj': xmlrpc.client.ServerProxy(
                f'{self.url}/xmlrpc/2/object', allow_none=True),
            'fields': {},   # target model er field list cache
            'cache': {},    # resolved ID cache
        }

    def _rpc(self, conn, model, method, args, kwargs=None):
        kwargs = dict(kwargs or {})
        # skip_db_sync: loop thamanor jonno
        kwargs['context'] = {'skip_db_sync': True, 'active_test': False}
        return conn['obj'].execute_kw(
            self.db_name, conn['uid'], self.password, model, method, args, kwargs)

    def action_test_connection(self):
        self.ensure_one()
        try:
            self._connect()
        except Exception as e:
            raise UserError(_('Connection failed: %s') % e)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('Success'),
                       'message': _('Target database connected.'),
                       'type': 'success', 'sticky': False},
        }

    # ---------------- helpers ----------------
    def _remote_fields(self, conn, model):
        if model not in conn['fields']:
            conn['fields'][model] = self._rpc(
                conn, model, 'fields_get', [], {'attributes': ['type']})
        return conn['fields'][model]

    @staticmethod
    def _to_rpc(value):
        if isinstance(value, datetime):
            return fields.Datetime.to_string(value)
        if isinstance(value, date):
            return fields.Date.to_string(value)
        if isinstance(value, bytes):
            return value.decode()
        return value

    def _resolve_remote_id(self, conn, comodel, source_id):
        """Source DB er ID theke Target DB er ID ber kora.
        1) mapping table  2) xml id (country, currency ityadi)
        3) same ID (jodi option tick kora thake)"""
        key = (comodel, source_id)
        if key in conn['cache']:
            return conn['cache'][key]
        target_id = False
        mapping = self.env['db.sync.mapping'].sudo().search([
            ('config_id', '=', self.id), ('model', '=', comodel),
            ('source_id', '=', source_id)], limit=1)
        if mapping:
            target_id = mapping.target_id
        else:
            xml = self.env['ir.model.data'].sudo().search([
                ('model', '=', comodel), ('res_id', '=', source_id)], limit=1)
            if xml:
                res = self._rpc(conn, 'ir.model.data', 'search_read', [[
                    ('module', '=', xml.module), ('name', '=', xml.name),
                    ('model', '=', comodel)]], {'fields': ['res_id'], 'limit': 1})
                if res:
                    target_id = res[0]['res_id']
            if not target_id and self.assume_same_id:
                if self._rpc(conn, comodel, 'search',
                             [[('id', '=', source_id)]], {'limit': 1}):
                    target_id = source_id
        conn['cache'][key] = target_id
        return target_id

    def _prepare_vals(self, conn, record, names):
        remote_fields = self._remote_fields(conn, record._name)
        vals = {}
        for name in names:
            field = record._fields.get(name)
            if (not field or name in MAGIC_FIELDS or not field.store
                    or name not in remote_fields
                    or (field.compute and not field.inverse)
                    or field.type in ('one2many', 'many2one_reference', 'reference')):
                continue
            value = record[name]
            if field.type == 'many2one':
                if not value:
                    vals[name] = False
                else:
                    tid = self._resolve_remote_id(conn, field.comodel_name, value.id)
                    if tid:
                        vals[name] = tid
            elif field.type == 'many2many':
                ids = [t for t in (
                    self._resolve_remote_id(conn, field.comodel_name, r.id)
                    for r in value) if t]
                vals[name] = [(6, 0, ids)]
            else:
                vals[name] = self._to_rpc(value)
        return vals

    def _sync_one(self, conn, record, vals, operation):
        Mapping = self.env['db.sync.mapping'].sudo()
        domain = [('config_id', '=', self.id), ('model', '=', record._name),
                  ('source_id', '=', record.id)]
        mapping = Mapping.search(domain, limit=1)

        # Duplicated DB: ager record target e same ID te ache kina dekho
        if not mapping and operation == 'write' and self.assume_same_id:
            if self._rpc(conn, record._name, 'search',
                         [[('id', '=', record.id)]], {'limit': 1}):
                mapping = Mapping.create({
                    'config_id': self.id, 'model': record._name,
                    'source_id': record.id, 'target_id': record.id})

        if mapping:
            # create er somoy sob field, update er somoy shudhu change howa field
            if operation == 'create':
                names = [n for n, f in record._fields.items() if f.store]
            else:
                names = list(vals)
            rv = self._prepare_vals(conn, record, names)
            if rv:
                self._rpc(conn, record._name, 'write', [[mapping.target_id], rv])
        else:
            names = [n for n, f in record._fields.items() if f.store]
            rv = self._prepare_vals(conn, record, names)
            new_id = self._rpc(conn, record._name, 'create', [rv])
            Mapping.create({
                'config_id': self.id, 'model': record._name,
                'source_id': record.id, 'target_id': new_id})

    @api.model
    def sync_records(self, records, vals_list, operation):
        configs = self.sudo().search([
            ('active', '=', True),
            ('model_ids.model', '=', records._name)])
        for config in configs:
            try:
                # savepoint: sync e error hole main DB er transaction nosto hobe na
                with self.env.cr.savepoint():
                    conn = config._connect()
                    for record, vals in zip(records.sudo(), vals_list):
                        config._sync_one(conn, record, vals, operation)
            except Exception:
                _logger.exception(
                    'DB Sync failed: model=%s op=%s target=%s',
                    records._name, operation, config.name)
