{
    "name": "Company Stock Cutover",
    "version": "19.0.2.7.4",
    "summary": "Move warehouse stock between companies, preserving native costing and valuation",
    "category": "Inventory/Inventory",
    "author": "Sawo Coffee",
    "license": "LGPL-3",
    "depends": ["stock_accountant", "product_expiry", "company_kit_bom_migration"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/sequence.xml",
        "views/migration_views.xml",
        "views/warehouse_cutover_views.xml",
    ],
    "installable": True,
    "application": False,
}
