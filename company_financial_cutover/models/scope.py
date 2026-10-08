"""Independent financial and purchase choices over the guided move."""
import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import UserError


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    include_financial = fields.Boolean(string="Financial balances and unpaid items", default=True,
        help="Create the financial opening in a fresh company. Turn this off to copy purchase history without changing accounting.")
    move_scope = fields.Selection([
        ("financial", "Financial balances and unpaid items"),
        ("purchase", "Purchase orders (read-only history)"),
        ("both", "Financial balances and purchase orders"),
        ("stock", "Stock (separate guided move)"),
    ], string="Move", compute="_compute_move_scope", inverse="_inverse_move_scope", required=True,
        help="Choose one option. The screen shows only the setup needed for your selection.")
    move_scope_description = fields.Char(compute="_compute_scope_description")
    stock_mover_available = fields.Boolean(compute="_compute_stock_mover_available")

    @api.depends("include_financial", "include_purchase_history")
    def _compute_move_scope(self):
        for batch in self:
            batch.move_scope = {
                (True, True): "both", (True, False): "financial",
                (False, True): "purchase", (False, False): "stock",
            }[(bool(batch.include_financial), bool(batch.include_purchase_history))]

    def _inverse_move_scope(self):
        for batch in self:
            if batch.move_scope not in {"financial", "purchase", "both", "stock"}:
                raise UserError("Choose what you want to move.")
            batch.write({"include_financial": batch.move_scope in {"financial", "both"},
                "include_purchase_history": batch.move_scope in {"purchase", "both"}})

    @api.onchange("move_scope")
    def _onchange_move_scope(self):
        # Only update the virtual form here. The inverse invalidates the saved
        # review on save; changing a dropdown must not delete persisted rows.
        for batch in self:
            scope = batch.move_scope
            batch.include_financial = scope in {"financial", "both"}
            batch.include_purchase_history = scope in {"purchase", "both"}
            if batch.state != "done":
                batch.state = "draft"
                batch.check_status = "unchecked"
                batch.check_report = False
                batch.summary = False
                batch.snapshot_hash = False
                batch.purchase_preview_data = False
                batch.purchase_history_preview_ready = False
                batch.line_ids = False

    @api.depends("move_scope")
    def _compute_scope_description(self):
        descriptions = {
            "financial": "Move opening balances and individual unpaid customer/vendor items. Purchase orders stay in the old company.",
            "purchase": "Copy purchase orders as read-only history. Accounting balances stay unchanged. Eligible orders can be prepared as replacement drafts later.",
            "both": "Move opening balances and unpaid items, and copy purchase orders as read-only history. Stock can be moved afterwards.",
            "stock": "Open the stock mover to choose warehouses and review quantities and values. This screen will not move financial balances or purchase history.",
        }
        for batch in self:
            batch.move_scope_description = descriptions.get(batch.move_scope, "Choose what you want to move.")

    def _compute_stock_mover_available(self):
        for batch in self:
            batch.stock_mover_available = "company.stock.warehouse.cutover" in self.env

    def _validate_purchase_only(self):
        self.ensure_one()
        self._operator()
        issues = []
        if self.state == "done":
            issues.append("This move is already completed. Use Review missing purchase orders to add only missing history.")
        if self.source_company_id == self.target_company_id:
            issues.append("Choose different old and new companies.")
        if not self.include_purchase_history:
            issues.append("Choose financial balances, purchase orders, or both. For stock only, use Open stock mover below.")
        if any(c.sudo().parent_id or c.sudo().all_child_ids for c in (self.source_company_id, self.target_company_id)):
            issues.append("Companies with branches need a separately reviewed move.")

        self._raise_issues("Review these purchase-copy requirements together:", issues)

    def _plan(self):
        if self.include_financial:
            rows, digest, count = super()._plan()
            return rows, hashlib.sha256(json.dumps([digest, True]).encode()).hexdigest(), count
        self._validate_purchase_only()
        purchases = self._purchase_plan()
        if not purchases:
            raise UserError("There are no new purchase orders to copy. Existing copies are skipped, so no duplicate history will be created.")
        evidence = [False, self.include_purchase_history, self.source_company_id.id,
            self.target_company_id.id, purchases]
        return [], hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest(), 0

    def _check_purchase_only(self):
        report = ["Purchase orders only: financial balances, invoices, bills and stock will not be changed."]
        try:
            self._validate_purchase_only()
            self._match_contacts(self._purchase_contacts())
            self._plan()
        except UserError as exc:
            status = "blocked"
            report.append("Needs attention: %s" % exc)
        else:
            status = "ready"
            report.append("Purchase checks passed. Already copied orders are skipped.")
            missing = self.partner_mapping_ids.filtered(lambda row: not row.target_partner_id)
            if missing:
                report.append("Vendors needing a choice: %s. Choose them under Review matches before cancelling original orders. "
                    "You can copy the history now and choose vendors from the saved history later." % len(missing))
        notices = self._review_notices(self.env["account.move.line"])
        if notices:
            report.append("For review — these notices do not block copying:\n" + "\n".join("• " + note for note in notices))
        self._system_write({"check_status": status, "check_report": "\n\n".join(report)})
        return True

    def action_preview(self):
        if self.include_financial:
            return super().action_preview()
        self.ensure_one()
        _rows, digest, _count = self._plan()
        purchases = self._purchase_plan()
        self.line_ids._system_unlink()
        self._system_write({"state": "preview", "snapshot_hash": digest,
            "purchase_preview_data": purchases,
            "summary": "%s new purchase orders will be copied as read-only history. Financial balances, receipts and bills will not be created. Already copied orders are skipped." % len(purchases)})
        return True

    def action_apply(self):
        if self.include_financial:
            return super().action_apply()
        self.ensure_one()
        self._operator()
        with self.env.cr.savepoint():
            self._lock()
            if self.state != "preview":
                raise UserError("Build and review a fresh Preview before confirming.")
            _rows, digest, _count = self._plan()
            if digest != self.snapshot_hash:
                raise UserError("The selected data changed. Build a fresh Preview before moving.")
            self._create_purchase_history(self.purchase_preview_data or [])
            self._system_write({"state": "done", "completed_at": fields.Datetime.now(), "completed_by": self.env.user.id})
        return {"type": "ir.actions.act_window", "name": "Completed company move",
            "res_model": self._name, "res_id": self.id, "view_mode": "form"}

    def action_open_stock_mover(self):
        self.ensure_one()
        self._operator()
        if self.source_company_id == self.target_company_id:
            raise UserError("Choose different old and new companies.")
        if "company.stock.warehouse.cutover" not in self.env:
            raise UserError("Stock moves need Company Stock Cutover and its dependencies. Ask your Odoo administrator to install them first.")
        if self.include_financial and self.state != "done":
            raise UserError("Complete the selected financial opening first, then open the stock mover. To move only stock, select Stock in the Move choice.")
        action = self.env.ref("company_stock_fifo_migration.whole_warehouse_cutover_action").read()[0]
        action.update({"views": [(False, "form")], "context": {
            **self.env.context, "default_source_company_id": self.source_company_id.id,
            "default_target_company_id": self.target_company_id.id}})
        return action
