from unittest.mock import patch

from lxml import html

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged, new_test_user

from .test_cutover import FinancialCutoverCase


class PurchaseMigrationCase(FinancialCutoverCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({
            "name": "Purchase service", "type": "service", "purchase_ok": True,
            "supplier_taxes_id": [Command.clear()]})
        for company in (cls.source, cls.target):
            cls.product.with_company(company).property_account_expense_id = cls.accounts[company.id]["expense"]

    def _order(self, confirmed=False, received=0):
        order = self.env["purchase.order"].with_company(self.source).create({
            "partner_id": self.vendor.id, "company_id": self.source.id,
            "date_order": "2024-06-30 10:00:00", "partner_ref": "Supplier reference",
            "order_line": [Command.create({"product_id": self.product.id,
                "name": "Historical purchase", "product_qty": 5, "price_unit": 10,
                "date_planned": "2024-07-01 10:00:00", "product_uom_id": self.product.uom_id.id,
                "tax_ids": [Command.clear()]})]})
        if confirmed:
            order.button_confirm()
        if received:
            order.order_line.qty_received_manual = received
        return order


@tagged("post_install", "-at_install")
class TestPurchaseMigration(PurchaseMigrationCase):
    def test_all_order_states_copy_without_creating_orders_or_bills(self):
        self._invoice()
        draft = self._order()
        historical = self._order(confirmed=True, received=5)
        historical.button_lock()
        cancelled = self._order()
        cancelled.button_cancel()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self.assertEqual(len(batch.purchase_preview_data), 3)
        self.assertFalse(batch.purchase_history_ids)
        before = [(order.id, order.state, order.order_line.qty_received) for order in draft | historical | cancelled]
        batch.action_apply()
        self.assertEqual(len(batch.purchase_history_ids), 3)
        self.assertEqual(set(batch.purchase_history_ids.mapped("original_state")), {"draft", "purchase", "cancel"})
        self.assertEqual(before, [(order.id, order.state, order.order_line.qty_received) for order in draft | historical | cancelled])
        self.assertFalse(self.env["purchase.order"].search([("company_id", "=", self.target.id)]))
        self.assertFalse(self.env["account.move"].search([("company_id", "=", self.target.id), ("move_type", "=", "in_invoice")]))

    def test_changed_or_added_source_order_rejects_preview(self):
        self._invoice()
        order = self._order()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        order.order_line.price_unit = 25
        with self.assertRaisesRegex(UserError, "changed.*fresh Preview"):
            batch.action_apply()
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.purchase_history_ids)
        batch.action_preview()
        self._order()
        with self.assertRaisesRegex(UserError, "changed.*fresh Preview"):
            batch.action_apply()

    def test_history_failure_rolls_back_financial_entry_and_completion_marker(self):
        self._invoice()
        self._order()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        with patch.object(type(self.env["company.financial.purchase.history"]), "_system_create", side_effect=UserError("History blocked")):
            with self.assertRaisesRegex(UserError, "History blocked"):
                batch.action_apply()
        self.assertEqual(batch.state, "preview")
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.purchase_history_ids)
        self.assertFalse(self.env["ir.config_parameter"].sudo().search([("key", "=", batch._completion_key())]))

    def test_purchase_history_is_immutable_and_cannot_be_forged(self):
        self._invoice()
        self._order()
        batch = self._run()
        history = batch.purchase_history_ids
        for action in (lambda: history.write({"amount_total": 1}), lambda: history.unlink(),
            lambda: history.copy(), lambda: batch.write({"purchase_preview_data": []})):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                action()

    def test_preparing_draft_is_repeatable_and_never_confirms_original_or_replacement(self):
        self._invoice()
        original = self._order(confirmed=True)
        history = self._run().purchase_history_ids
        self.assertTrue(history.draft_eligible)
        self.assertEqual(history.original_status_label, "Confirmed order")
        with self.assertRaisesRegex(UserError, "Cancel the original order"):
            history.action_prepare_draft()
        self.assertFalse(history.target_order_id)
        original.button_cancel()
        history.action_prepare_draft()
        replacement = history.target_order_id
        self.assertFalse(history.draft_eligible)
        self.assertIn("already exists", history.draft_guidance)
        self.assertEqual(replacement.state, "draft")
        self.assertEqual(replacement.company_id, self.target)
        self.assertEqual(replacement.order_line.product_qty, 5)
        self.assertEqual(original.state, "cancel")
        history.action_prepare_draft()
        self.assertEqual(history.target_order_id, replacement)
        self.assertEqual(self.env["purchase.order"].search_count([("company_id", "=", self.target.id)]), 1)
        original.button_draft()
        with self.assertRaisesRegex(UserError, "Cancel the original order"):
            replacement.button_confirm()
        with self.assertRaisesRegex(UserError, "Do not duplicate"):
            replacement.copy()
        original.button_cancel()
        replacement.button_confirm()
        self.assertEqual(replacement.state, "purchase")
        with self.assertRaisesRegex(UserError, "replacement draft already exists"):
            original.button_draft()
            original.button_confirm()

    def test_partially_received_order_preserves_remaining_work_without_new_draft(self):
        self._invoice()
        self._order(confirmed=True, received=2)
        history = self._run().purchase_history_ids
        self.assertFalse(history.draft_eligible)
        self.assertIn("already received or billed", history.draft_guidance)
        self.assertEqual(history.snapshot["lines"][0]["received"], 2)
        self.assertIn("Left to receive", history.details_html)
        with self.assertRaisesRegex(UserError, "partially received/billed"):
            history.action_prepare_draft()
        self.assertFalse(history.target_order_id)

    def test_existing_destination_reference_blocks_duplicate_draft(self):
        self._invoice()
        original = self._order()
        history = self._run().purchase_history_ids
        original.button_cancel()
        self.env["purchase.order"].with_company(self.target).create({
            "company_id": self.target.id, "partner_id": self.vendor.id, "partner_ref": "Supplier reference"})
        with self.assertRaisesRegex(UserError, "existing destination purchase order"):
            history.action_prepare_draft()

    def test_purchase_only_user_can_read_target_history_and_other_company_cannot(self):
        self._invoice()
        self._order()
        history = self._run().purchase_history_ids
        buyer = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}), login="replacement-purchase-reader",
            groups="purchase.group_purchase_user", company_id=self.target.id, company_ids=[Command.set(self.target.ids)])
        visible = history.with_user(buyer).with_context(allowed_company_ids=self.target.ids)
        self.assertEqual(visible.read(["name", "vendor_name", "details_html", "original_status_label", "draft_guidance", "draft_eligible"])[0]["name"], history.name)
        with self.assertRaises(AccessError):
            visible.action_prepare_draft()
        outsider = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}), login="other-purchase-reader",
            groups="purchase.group_purchase_user", company_id=self.source.id, company_ids=[Command.set(self.source.ids)])
        with self.assertRaises(AccessError):
            history.with_user(outsider).with_context(allowed_company_ids=self.source.ids).read(["name"])

    def test_purchase_history_can_be_excluded_explicitly(self):
        self._invoice()
        self._order()
        batch = self._batch()
        batch.include_purchase_history = False
        self._run(batch)
        self.assertFalse(batch.purchase_history_ids)

    def test_fully_billed_history_does_not_recreate_a_vendor_bill(self):
        order = self._order(confirmed=True, received=5)
        order.action_create_invoice()
        bill = order.invoice_ids
        bill.write({"invoice_date": self.cutoff, "date": self.cutoff})
        bill.action_post()
        self.assertEqual(order.order_line.qty_invoiced, 5)
        batch = self._run()
        history = batch.purchase_history_ids
        self.assertEqual(history.snapshot["lines"][0]["billed"], 5)
        self.assertEqual(history.snapshot["bills"][0]["name"], bill.name)
        unpaid = batch.line_ids.filtered(lambda l: l.kind == "open_item")
        self.assertEqual(unpaid.balance, -50)
        self.assertFalse(self.env["account.move"].search([("company_id", "=", self.target.id), ("move_type", "=", "in_invoice")]))
        with self.assertRaisesRegex(UserError, "partially received/billed"):
            history.action_prepare_draft()

    def test_history_preview_fields_cannot_be_supplied_by_rpc(self):
        with self.assertRaises(AccessError):
            self.env["company.financial.cutover"].create({
                "source_company_id": self.source.id, "target_company_id": self.target.id,
                "cutover_date": self.cutoff, "purchase_preview_data": [{"id": 999}]})

    def test_completed_financial_move_can_add_purchase_history_without_reposting(self):
        self._invoice()
        self._order()
        batch = self._batch()
        batch.include_purchase_history = False
        self._run(batch)
        original_move = batch.move_id
        batch.action_preview_purchases()
        self.assertEqual(len(batch.purchase_preview_data), 1)
        self.assertFalse(batch.purchase_history_ids)
        batch.action_import_purchases()
        self.assertEqual(len(batch.purchase_history_ids), 1)
        self.assertEqual(batch.move_id, original_move)
        self.assertEqual(self.env["account.move"].search_count([("company_id", "=", self.target.id)]), 1)
        batch.action_preview_purchases()
        batch.action_import_purchases()
        self.assertEqual(len(batch.purchase_history_ids), 1)

    def test_later_purchase_history_import_rejects_a_changed_preview(self):
        self._invoice()
        order = self._order()
        batch = self._batch()
        batch.include_purchase_history = False
        self._run(batch)
        batch.action_preview_purchases()
        order.order_line.product_qty = 20
        with self.assertRaisesRegex(UserError, "Purchase orders changed"):
            batch.action_import_purchases()
        self.assertFalse(batch.purchase_history_ids)

    def test_purchase_preview_distinguishes_currencies_and_escapes_vendor_names(self):
        self._invoice()
        euro = self.env.ref("base.EUR")
        euro.active = True
        self.vendor.name = '<img src=x onerror="alert(1)">'
        first = self._order(confirmed=True)
        second = self._order()
        second.currency_id = euro
        second.button_cancel()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        rendered = html.fromstring(batch.purchase_preview_html)
        self.assertFalse(rendered.xpath("//script|//img"))
        rows = rendered.xpath("//tbody/tr")
        by_order = {row.xpath("./td/text()")[0]: row.xpath("./td/text()") for row in rows}
        self.assertEqual(by_order[first.name][2], "Confirmed order")
        self.assertEqual(by_order[second.name][2], "Cancelled")
        self.assertTrue(by_order[first.name][-1].endswith(self.source.currency_id.name))
        self.assertTrue(by_order[second.name][-1].endswith("EUR"))

    def test_cancelled_history_explains_why_no_replacement_is_available(self):
        self._invoice()
        order = self._order()
        order.button_cancel()
        history = self._run().purchase_history_ids
        self.assertFalse(history.draft_eligible)
        self.assertEqual(history.original_status_label, "Cancelled")
        self.assertIn("cancelled", history.draft_guidance)
        with self.assertRaisesRegex(UserError, "cancelled"):
            history.action_prepare_draft()
