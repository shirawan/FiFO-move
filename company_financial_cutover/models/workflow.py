"""A single review action; presentation never replaces accounting validation."""
from odoo import api, fields, models
from odoo.exceptions import AccessError


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    can_run_move = fields.Boolean(compute="_compute_can_run_move")
    can_view_entry = fields.Boolean(compute="_compute_result_access")
    can_download_report = fields.Boolean(compute="_compute_result_access")
    unresolved_mapping_ids = fields.One2many("company.financial.account.mapping", "cutover_id",
        domain=[("target_account_id", "=", False), ("handled_by_stock", "=", False)])
    unresolved_partner_mapping_ids = fields.One2many("company.financial.partner.mapping", "cutover_id",
        domain=[("target_partner_id", "=", False), ("create_contact", "=", False),
            ("required_for_financial", "=", True)])
    decisions_text = fields.Text(compute="_compute_review_messages")
    information_text = fields.Text(compute="_compute_review_messages")

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends("move_id", "report_attachment_id", "can_run_move")
    def _compute_result_access(self):
        for batch in self:
            batch.can_view_entry = False
            batch.can_download_report = False
            if batch.move_id:
                try:
                    batch.move_id.check_access("read")
                    batch.can_view_entry = True
                except AccessError:
                    pass
            if batch.can_run_move:
                try:
                    batch.report_attachment_id.check_access("read")
                    batch.can_download_report = True
                except AccessError:
                    pass

    @api.depends_context("uid", "allowed_company_ids")
    @api.depends("source_company_id", "target_company_id", "include_source_branches",
        "include_financial", "include_purchase_history")
    def _compute_can_run_move(self):
        for batch in self:
            batch.can_run_move = bool(batch._has_move_role() and batch.source_company_id
                and batch.target_company_id
                and (batch._source_companies() | batch.target_company_id) <= self.env.companies)

    @api.depends("check_report", "check_status")
    def _compute_review_messages(self):
        for batch in self:
            requirements, _, notices = (batch.check_report or "").partition("For review — these notices do not block copying:")
            batch.decisions_text = requirements.strip() if batch.check_status == "blocked" else False
            batch.information_text = (notices.strip() if batch.check_status == "blocked"
                else "\n\n".join(part.strip() for part in (requirements, notices) if part.strip()))

    def _destination_has_accounting(self):
        self.ensure_one()
        if not self.include_financial or not self.target_company_id:
            return False
        return bool(self.env["account.move"].search_count([
            ("company_id", "=", self.target_company_id.id), ("state", "in", ["draft", "posted"])], limit=1)
            or self.env["account.payment"].search_count([
                ("company_id", "=", self.target_company_id.id),
                ("state", "not in", ["canceled", "rejected"])], limit=1))

    @api.onchange("source_company_id")
    def _onchange_detect_branches(self):
        for batch in self:
            if batch.state != "done":
                batch.include_source_branches = bool(batch.source_company_id.sudo().all_child_ids)

    @api.onchange("target_company_id", "include_financial")
    def _onchange_detect_destination(self):
        for batch in self:
            if batch.state != "done":
                batch.destination_mode = "existing" if batch._destination_has_accounting() else "fresh"
                batch.existing_data_reviewed = False

    def action_review(self):
        """Detect scope, collect decisions and store the same plan that passed checks."""
        self.ensure_one()
        self._operator()
        # Re-detect on the server too: old drafts and API callers do not run onchanges.
        # Scope changes invalidate prior review and existing-transfer approval.
        values = {}
        branches = bool(self.source_company_id.sudo().all_child_ids)
        if self.include_source_branches != branches:
            values["include_source_branches"] = branches
        if self.include_financial and self._destination_has_accounting() and self.destination_mode != "existing":
            values["destination_mode"] = "existing"
        if values:
            self.write(values)
        return self._review_selected_data(build_preview=True)


class PartnerMapping(models.Model):
    _inherit = "company.financial.partner.mapping"

    # Presentation only. _plan() independently validates every required contact.
    required_for_financial = fields.Boolean(default=False)
