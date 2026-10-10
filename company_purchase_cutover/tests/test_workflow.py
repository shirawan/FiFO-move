from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from .test_purchase_history import PurchaseMigrationCase


@tagged("post_install", "-at_install")
class TestPurchaseWorkflow(PurchaseMigrationCase):
    def _manager(self):
        return new_test_user(self.env(context={**self.env.context, "no_reset_password": True}),
            login="workflow_purchase_manager", groups="purchase.group_purchase_manager",
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])

    def test_purchase_manager_reviews_and_copies_without_accounting_access(self):
        order = self._order()
        manager = self._manager()
        Model = self.env["company.financial.cutover"].with_user(manager)
        batch = Model.create({"source_company_id": self.source.id, "target_company_id": self.target.id,
            "include_financial": False, "include_purchase_history": True})
        self.assertFalse(manager.has_group("account.group_account_manager"))
        self.assertTrue(batch.can_run_move)
        batch.action_review()
        self.assertEqual(batch.state, "preview")
        with self.assertRaises(AccessError), self.cr.savepoint():
            batch.write({"include_financial": True})
        self.assertFalse(batch.include_financial)
        self.assertEqual(batch.state, "preview")
        batch.action_apply()
        self.assertEqual(batch.state, "done")
        self.assertEqual(batch.purchase_history_ids.source_order_res_id, order.id)
        self.assertTrue(batch.archive_attachment_id)
        self.assertTrue(batch.report_attachment_id)
        self.assertTrue(batch.can_download_report)
        self.assertFalse(batch.move_id)

    def test_purchase_manager_cannot_read_or_create_financial_moves(self):
        self._invoice()
        financial = self._batch()
        manager = self._manager()
        with self.assertRaises(AccessError), self.cr.savepoint():
            financial.with_user(manager).check_access("read")
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.env["company.financial.cutover"].with_user(manager).create({
                "source_company_id": self.source.id, "target_company_id": self.target.id})

    def test_repeated_review_has_no_new_archive_or_duplicate_history(self):
        self._order()
        first = self._batch()
        first.move_scope = "purchase"
        first.action_review()
        self.assertEqual(first.state, "preview")
        first.action_apply()
        before = self.env["ir.attachment"].sudo().search_count([])
        repeat = self._batch()
        repeat.move_scope = "purchase"
        repeat.action_review()
        self.assertEqual(repeat.check_status, "up_to_date")
        self.assertEqual(repeat.state, "draft")
        self.assertFalse(repeat.snapshot_hash)
        self.assertFalse(repeat.archive_attachment_id)
        self.assertEqual(self.env["ir.attachment"].sudo().search_count([]), before)
        self.assertEqual(self.env["company.financial.purchase.history"].search_count([
            ("company_id", "=", self.target.id)]), 1)

    def test_cancelled_history_stays_out_of_followup_queue(self):
        self._order().button_cancel()
        batch = self._batch()
        batch.move_scope = "purchase"
        batch.action_review()
        batch.action_apply()
        history = batch.purchase_history_ids
        self.assertEqual(history.replacement_step, "history")
        self.assertFalse(history.followup_required)
        self.assertIn("no replacement", history.replacement_note)
        action = batch.action_open_purchase_work()
        self.assertEqual(action["context"]["search_default_needs_work"], 1)
        self.assertFalse(self.env["company.financial.purchase.history"].search(
            action["domain"] + [("followup_required", "=", True)]))

    def test_button_authorization_matches_backend_role(self):
        self._order()
        batch = self._batch()
        batch.move_scope = "purchase"
        self._run(batch)
        administrator = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}),
            login="workflow_settings_only", groups="base.group_system,purchase.group_purchase_user",
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        history = batch.purchase_history_ids.with_user(administrator)
        self.assertFalse(history.can_manage_replacement)
        with self.assertRaises(AccessError):
            history.action_prepare_draft()
        self.assertTrue(batch.purchase_history_ids.with_user(self._manager()).can_manage_replacement)

    def test_negative_quantity_order_is_not_silently_hidden_as_completed_history(self):
        order = self._order()
        order.order_line.product_qty = -5
        batch = self._batch()
        batch.move_scope = "purchase"
        batch.action_review()
        batch.action_apply()
        history = batch.purchase_history_ids
        self.assertTrue(history.followup_required)
        self.assertNotEqual(history.replacement_step, "history")
        self.assertFalse(history.target_order_id)

    def test_combined_review_button_requires_purchase_access(self):
        self._order()
        batch = self._batch()
        operator = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}),
            login="workflow_financial_only_operator", groups="base.group_system,account.group_account_manager",
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        self.assertFalse(operator.has_group("purchase.group_purchase_user"))
        restricted = batch.with_user(operator)
        self.assertFalse(restricted.can_run_move)
        with self.assertRaisesRegex(AccessError, "Purchase user access"):
            restricted.action_review()
