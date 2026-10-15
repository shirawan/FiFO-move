{
 'name': 'Company Purchase Document Migration — Candidate',
 'version': '19.0.0.5.0', 'license': 'LGPL-3',
 'depends': ['company_document_cutover', 'purchase_stock'],
 'data': ['security/ir.model.access.csv', 'security/archive_rules.xml', 'views/migration_views.xml', 'views/archive_views.xml'], 'post_init_hook': 'post_init_hook', 'installable': True,
}
