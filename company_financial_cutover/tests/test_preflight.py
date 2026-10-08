from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .test_purchase_history import PurchaseMigrationCase


@tagged("post_install", "-at_install")
class TestPracticalPreflight(PurchaseMigrationCase):
    def _purchase_batch(self):
        batch = self._batch()
        batch.move_scope = "purchase"
        return batch

    def _ambiguous_earnings(self):
        self.env["account.account"].with_company(self.target).create({
            "name": "Other earnings", "code": "3011", "account_type": "equity_unaffected",
            "company_ids": [Command.set(self.target.ids)]})

    def test_all_independent_financial_issues_appear_in_one_check(self):
        old_contacts = self.env["res.partner"].create([
            {"name": name, "company_id": self.source.id} for name in ("Alex customer", "Sam customer")])
        self.env["res.partner"].create([
            {"name": partner.name, "company_id": self.target.id} for partner in old_contacts])
        for partner in old_contacts:
            self._entry(self.source, [("receivable", 100, partner), ("revenue", -100)])
        self._entry(self.source, [("bank", 50), ("revenue", -50)], date(2023, 12, 31))
        self._ambiguous_earnings()
        batch = self._batch()
        batch.action_match()
        batch.mapping_ids.filtered(lambda row: row.source_account_id in
            (self.accounts[self.source.id]["bank"] | self.accounts[self.source.id]["revenue"])).target_account_id = False
        batch.journal_id = self.env["account.journal"].with_company(self.target).create({
            "name": "Cash basis", "code": "CABA", "type": "general", "company_id": self.target.id})
        batch.retained_earnings_account_id = False
        batch.action_match()
        expected = ("Miscellaneous journal", "Previous years' earnings", "account for bank", "account for revenue", "Alex customer", "Sam customer")
        self.assertEqual(batch.check_status, "blocked")
        for phrase in expected:
            self.assertIn(phrase, batch.check_report)
        with self.assertRaises(UserError) as caught:
            batch.action_preview()
        for phrase in expected:
            self.assertIn(phrase, str(caught.exception))
        self.assertNotIn("generated opening does not balance", batch.check_report)
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.line_ids)

    def test_unused_earnings_and_clearing_settings_do_not_force_cleanup(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.retained_earnings_account_id = False
        batch.offset_account_id = self.accounts[self.target.id]["expense"]
        batch.action_preview()
        action = batch.action_apply()
        self.assertEqual((action["res_model"], action["res_id"]), (batch._name, batch.id))
        self.assertEqual(batch.move_id.state, "posted")
        self.assertFalse(batch.line_ids.filtered(lambda row: row.kind in {"retained", "stock_clearing"}))

    def test_used_earnings_account_is_still_required(self):
        self._entry(self.source, [("bank", 100), ("revenue", -100)], date(2023, 12, 31))
        batch = self._batch()
        batch.action_match()
        batch.retained_earnings_account_id = False
        with self.assertRaisesRegex(UserError, "Previous years' earnings"):
            batch.action_preview()
        self.assertFalse(batch.move_id)

    def test_zero_balance_account_needs_no_destination_choice(self):
        self._entry(self.source, [("bank", 100), ("equity", -100), ("expense", 20), ("expense", -20)])
        batch = self._batch()
        batch.action_match()
        batch.mapping_ids.filtered(lambda row: row.source_account_id == self.accounts[self.source.id]["expense"]).target_account_id = False
        self._run(batch)
        self.assertEqual(batch.move_id.state, "posted")
        self.assertFalse(batch.line_ids.filtered(lambda row: row.source_account_id == self.accounts[self.source.id]["expense"]))

    def test_settled_contacts_need_no_manual_match(self):
        partner = self.env["res.partner"].create({"name": "Settled Alex", "company_id": self.source.id})
        self.env["res.partner"].create({"name": partner.name, "company_id": self.target.id})
        sale = self._entry(self.source, [("receivable", 100, partner), ("revenue", -100)])
        payment = self._entry(self.source, [("bank", 100), ("receivable", -100, partner)])
        (sale | payment).line_ids.filtered(lambda row: row.account_id.account_type == "asset_receivable").reconcile()
        batch = self._run()
        self.assertFalse(batch.partner_mapping_ids)
        self.assertEqual(batch.move_id.state, "posted")

    def test_duplicate_history_copies_and_replacements_still_prevent_double_orders(self):
        original = self._order()
        duplicate = self._order()
        batch = self._run(self._purchase_batch())
        self.assertEqual(len(batch.purchase_history_ids), 2)
        self.assertIn("Possible duplicate purchase orders", batch.check_report)
        history = batch.purchase_history_ids.filtered(lambda row: row.source_order_res_id == original.id)
        self.assertEqual(history.replacement_step, "manual")
        self.assertIn(duplicate.name, history.replacement_note)
        original.button_cancel()
        with self.assertRaisesRegex(UserError, "Other active original orders"):
            history.action_prepare_draft()
        self.assertFalse(history.target_order_id)
        duplicate.button_cancel()
        history.action_prepare_draft()
        other = batch.purchase_history_ids - history
        with self.assertRaisesRegex(UserError, "existing destination purchase order"):
            other.action_prepare_draft()
        self.assertEqual(self.env["purchase.order"].search_count([("company_id", "=", self.target.id)]), 1)

    def test_replacement_reports_vendor_cancel_and_all_product_problems_together(self):
        partner = self.env["res.partner"].create({"name": "Ambiguous supplier", "company_id": self.source.id})
        self.env["res.partner"].create({"name": partner.name, "company_id": self.target.id})
        self.product.company_id = self.source
        other = self.env["product.product"].create({"name": "Unmapped second item", "type": "service",
            "company_id": self.source.id, "purchase_ok": True, "supplier_taxes_id": [Command.clear()]})
        order = self._order()
        order.partner_id = partner
        order.order_line = [Command.create({"product_id": other.id, "name": "Second purchase line",
            "product_qty": 1, "price_unit": 30, "date_planned": "2024-07-01 10:00:00",
            "product_uom_id": other.uom_id.id, "tax_ids": [Command.clear()]})]
        history = self._run(self._purchase_batch()).purchase_history_ids
        self.assertEqual(history.replacement_step, "manual")
        self.assertIn("Historical purchase", history.replacement_note)
        self.assertIn("Second purchase line", history.replacement_note)
        with self.assertRaises(UserError) as caught:
            history.action_prepare_draft()
        for phrase in ("Cancel the original", "Choose destination vendor", "Historical purchase", "Second purchase line"):
            self.assertIn(phrase, str(caught.exception))
        self.assertFalse(history.target_order_id)
        self.assertNotEqual(order.state, "cancel")

    def test_cancelled_destination_order_does_not_force_cleanup(self):
        original = self._order()
        history = self._run(self._purchase_batch()).purchase_history_ids
        obsolete = self.env["purchase.order"].with_company(self.target).create({
            "partner_id": self.vendor.id, "company_id": self.target.id, "partner_ref": original.partner_ref})
        obsolete.button_cancel()
        original.button_cancel()
        history.action_prepare_draft()
        self.assertEqual(history.target_order_id.state, "draft")
        self.assertEqual(obsolete.state, "cancel")

    def test_missing_partner_and_accounts_are_reported_together(self):
        self._entry(self.source, [("receivable", 100), ("revenue", -100)])
        batch = self._batch()
        batch.action_match()
        batch.mapping_ids.target_account_id = False
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        for phrase in ("needs a partner", "account for receivable", "account for revenue"):
            self.assertIn(phrase, batch.check_report)
        self.assertFalse(batch.move_id)
