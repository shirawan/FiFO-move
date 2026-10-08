def post_init_hook(env):
    env["company.financial.cutover"]._restore_archives()


def uninstall_hook(env):
    History = env["company.financial.purchase.history"].sudo()
    for history in History.search([("target_order_id.state", "not in", [False, "cancel"])]):
        source = env["purchase.order"].sudo().browse(history.source_order_res_id).exists()
        if source and source.state != "cancel":
            from odoo.exceptions import UserError
            raise UserError("Cancel the original purchase order %s before uninstalling. Its replacement already exists, and both orders must not remain active when the migration guards are removed." % source.name)
    env["company.financial.cutover"]._archive_completed()
