{
 'name': 'Company Purchase Document Migration — Candidate',
 'version': '19.0.0.4.0', 'license': 'LGPL-3',
 'depends': ['company_document_cutover', 'purchase_stock'],
 'data': ['views/migration_views.xml'], 'post_init_hook': 'post_init_hook', 'installable': True,
}
