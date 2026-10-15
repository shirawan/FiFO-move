{
    'name': 'Company Accounting Document Migration — Candidate',
    'version': '19.0.0.5.0',
    'license': 'LGPL-3',
    'depends': ['company_financial_cutover'],
    'data': ['security/security.xml', 'security/ir.model.access.csv', 'views/migration_views.xml'],
    'post_init_hook': 'post_init_hook', 'installable': True,
}
