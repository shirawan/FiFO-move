{
    "name": "Company Financial Cutover",
    "version": "19.0.1.2.0",
    "summary": "Move opening balances, unpaid items, and purchase history to a replacement company",
    "category": "Accounting/Accounting",
    "author": "FiFO-move contributors",
    "license": "LGPL-3",
    "depends": ["account", "purchase"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/cutover_views.xml",
        "views/purchase_history_views.xml",
    ],
    "installable": True,
    "application": False,
}
