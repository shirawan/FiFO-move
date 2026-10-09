{
    "name": "Company Purchase Cutover",
    "version": "19.0.3.0.0",
    "summary": "Optional purchase history and replacement drafts for company moves",
    "category": "Inventory/Purchase", "author": "FiFO-move contributors", "license": "LGPL-3",
    "depends": ["company_financial_cutover", "purchase"],
    "auto_install": False,
    "data": ["security/security.xml", "security/ir.model.access.csv", "views/purchase_history_views.xml", "views/cutover_views.xml"],
    "pre_init_hook": "pre_init_hook", "post_init_hook": "post_init_hook", "uninstall_hook": "uninstall_hook",
    "installable": True, "application": False,
}
