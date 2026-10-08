from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


_CUTOVER_CREATE_TOKEN = object()
_CUTOVER_RESTORE_TOKEN = object()


class AccountMove(models.Model):
    _inherit = "account.move"

    financial_cutover_id = fields.Many2one("company.financial.cutover", readonly=True, copy=False, ondelete="restrict")

    @api.model_create_multi
    def create(self, vals_list):
        capability = self.env.context.get("_financial_cutover_create")
        for vals in vals_list:
            if vals.get("financial_cutover_id") and not (
                isinstance(capability, tuple) and len(capability) == 2
                and capability[0] is _CUTOVER_CREATE_TOKEN
                and capability[1] == vals["financial_cutover_id"]
            ):
                raise AccessError("Financial cutover links are system-managed.")
        return super().create(vals_list)

    def _create_financial_cutover(self, values):
        # Enter at the full model's create(), preserving later addons' hooks.
        # Object identity cannot be forged by a JSON/RPC context flag.
        move = self.with_context(_financial_cutover_create=(
            _CUTOVER_CREATE_TOKEN, values["financial_cutover_id"])).create(values)
        return move.with_env(self.env)

    def write(self, vals):
        capability = self.env.context.get("_financial_cutover_restore")
        if "financial_cutover_id" in vals and not (
            isinstance(capability, tuple) and len(capability) == 2
            and capability[0] is _CUTOVER_RESTORE_TOKEN and capability[1] == vals["financial_cutover_id"]):
            raise AccessError("Financial cutover links are system-managed.")
        if {"date", "name", "ref", "company_id", "currency_id", "journal_id", "line_ids", "state"}.intersection(vals):
            if self.filtered(lambda m: m.financial_cutover_id.state == "done"):
                raise UserError("Completed financial opening entries cannot be changed or reset. Post accountant-reviewed corrections separately.")
        return super().write(vals)

    def _restore_financial_cutover_link(self, cutover_id):
        return self.with_context(_financial_cutover_restore=(_CUTOVER_RESTORE_TOKEN, cutover_id)).write({"financial_cutover_id": cutover_id})

    def unlink(self):
        if self.financial_cutover_id.filtered(lambda b: b.state == "done"):
            raise UserError("Completed financial opening entries must be retained.")
        return super().unlink()


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        moves = self.env["account.move"].browse([vals.get("move_id") for vals in vals_list if vals.get("move_id")])
        if moves.financial_cutover_id.filtered(lambda b: b.state == "done"):
            raise UserError("Journal items cannot be added to completed financial openings.")
        return super().create(vals_list)

    def write(self, vals):
        protected = {"move_id", "account_id", "partner_id", "date", "date_maturity", "name", "debit", "credit", "balance",
            "amount_currency", "currency_id", "company_id", "company_currency_id", "journal_id", "parent_state",
            "display_type", "sequence", "tax_ids", "tax_tag_ids", "tax_line_id", "tax_repartition_line_id", "analytic_distribution"}
        if protected.intersection(vals) and self.move_id.financial_cutover_id.filtered(lambda b: b.state == "done"):
            raise UserError("Amounts and identities of completed financial opening items are immutable; payment reconciliation remains available.")
        if vals.get("move_id") and self.env["account.move"].browse(vals["move_id"]).financial_cutover_id.state == "done":
            raise UserError("Journal items cannot be moved into completed financial openings.")
        return super().write(vals)

    def unlink(self):
        if self.move_id.financial_cutover_id.filtered(lambda b: b.state == "done"):
            raise UserError("Completed financial opening items must be retained.")
        return super().unlink()
