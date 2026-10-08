{
    "name": "Company Financial Cutover",
    "version": "19.0.1.0.0",
    "summary": "Open a replacement company's ledger and unpaid customer/vendor items",
    "category": "Accounting/Accounting",
    "author": "FiFO-move contributors",
    "license": "LGPL-3",
    "depends": ["account"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/cutover_views.xml",
    ],
    "installable": True,
    "application": False,
}
