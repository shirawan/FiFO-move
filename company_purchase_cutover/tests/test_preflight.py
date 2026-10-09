from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from .test_purchase_history import PurchaseMigrationCase

@tagged("post_install", "-at_install")
class TestPurchasePreflight(PurchaseMigrationCase):
    def _purchase_batch(self):
        batch = self._batch()
        batch.move_scope = "purchase"
        return batch

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
