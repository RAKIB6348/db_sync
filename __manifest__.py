{
    'name': 'DB Sync (Create + Update Replication)',
    'version': '17.0.2.0.0',
    'category': 'Tools',
    'summary': 'Main database e record create/update hole target database e o hoy',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'views/db_sync_config_views.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
}
