from .migration import transfer_purchase_ownership
from odoo.exceptions import UserError


def pre_init_hook(env):
    transfer_purchase_ownership(env.cr)


def post_init_hook(env):
    env["company.financial.cutover"]._restore_archives(strict=False)


def uninstall_hook(env):
    History = env["company.financial.purchase.history"].sudo()
    for history in History.search([("target_order_id.state", "not in", [False, "cancel"])]):
        source = env["purchase.order"].sudo().browse(history.source_order_res_id).exists()
        if source and source.state != "cancel":
            raise UserError("Cancel the original purchase order %s before uninstalling. Its replacement already exists, and both orders must not remain active when the migration guards are removed." % source.name)
    env["company.financial.cutover"]._archive_completed()
