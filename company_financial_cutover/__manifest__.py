{
    "name": "Company Financial Cutover",
    "version": "19.0.3.0.0",
    "summary": "Move opening balances and unpaid items to a replacement company",
    "category": "Accounting/Accounting",
    "author": "FiFO-move contributors",
    "license": "LGPL-3",
    "depends": ["account"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/cutover_views.xml",
        "views/recovery_views.xml",
    ],
    "installable": True,
    "application": False,
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
}
