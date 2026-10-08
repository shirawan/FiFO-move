"""Independent financial and purchase choices over the guided move."""
import hashlib
import json

from odoo import fields, models
from odoo.exceptions import UserError


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    include_financial = fields.Boolean(string="Financial balances and unpaid items", default=True,
        help="Create the financial opening in a fresh company. Turn this off to copy purchase history without changing accounting.")
    stock_mover_available = fields.Boolean(compute="_compute_stock_mover_available")

    def _compute_stock_mover_available(self):
        for batch in self:
            batch.stock_mover_available = "company.stock.warehouse.cutover" in self.env

    def _validate_purchase_only(self):
        self.ensure_one()
        self._operator()
        if self.state == "done":
            raise UserError("This move is already completed. Use Review missing purchase orders to add only missing history.")
        if self.source_company_id == self.target_company_id:
            raise UserError("Choose different old and new companies.")
        if not self.include_purchase_history:
            raise UserError("Choose financial balances, purchase orders, or both. For stock only, use Open stock mover below.")
        if any(c.sudo().parent_id or c.sudo().all_child_ids for c in (self.source_company_id, self.target_company_id)):
            raise UserError("Companies with branches need a separately reviewed move.")

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
            self._plan()
        except UserError as exc:
            status = "blocked"
            report.append("Needs attention: %s" % exc)
        else:
            status = "ready"
            report.append("Purchase checks passed. Click 2. Review selected data. Already copied orders are skipped.")
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
        if "company.stock.warehouse.cutover" not in self.env:
            raise UserError("Stock moves need Company Stock Cutover and its dependencies. Ask your Odoo administrator to install them first.")
        if self.include_financial and self.state != "done":
            raise UserError("Complete the selected financial opening first, then open the stock mover. For stock only, untick financial balances.")
        action = self.env.ref("company_stock_fifo_migration.whole_warehouse_cutover_action").read()[0]
        action.update({"views": [(False, "form")], "context": {
            **self.env.context, "default_target_company_id": self.target_company_id.id}})
        return action
