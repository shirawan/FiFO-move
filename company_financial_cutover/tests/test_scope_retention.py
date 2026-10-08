import hashlib
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged, new_test_user

from .test_purchase_history import PurchaseMigrationCase


@tagged("post_install", "-at_install")
class TestScopeAndRetention(PurchaseMigrationCase):
    def _purchase_batch(self):
        batch = self._batch()
        batch.write({"include_financial": False, "journal_id": False, "retained_earnings_account_id": False})
        return batch

    def test_purchase_only_preserves_an_existing_destination_ledger(self):
        existing = self._entry(self.target, [("bank", 25), ("equity", -25)])
        order = self._order()
        batch = self._run(self._purchase_batch())
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.line_ids)
        self.assertEqual(batch.purchase_history_ids.source_order_res_id, order.id)
        self.assertEqual(self.env["account.move"].search([("company_id", "=", self.target.id)]), existing)
        self.assertFalse(self.env["ir.config_parameter"].sudo().search([("key", "=", batch._completion_key())]))
        self.assertTrue(batch.archive_attachment_id)

    def test_financial_only_ignores_purchase_duplicates_and_purchase_can_follow(self):
        self._invoice()
        self._order()
        duplicate = self._order()
        batch = self._batch()
        batch.include_purchase_history = False
        self._run(batch)
        self.assertTrue(batch.move_id)
        self.assertFalse(batch.purchase_history_ids)
        purchases = self._purchase_batch()
        purchases.action_match()
        self.assertEqual(purchases.check_status, "blocked")
        self.assertIn("duplicate unprocessed purchase orders", purchases.check_report)
        duplicate.button_cancel()
        self._run(purchases)
        self.assertEqual(len(purchases.purchase_history_ids), 2)
        self.assertFalse(purchases.move_id)

    def test_empty_choice_blocks_without_creating_business_records(self):
        batch = self._purchase_batch()
        batch.include_purchase_history = False
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        with self.assertRaisesRegex(UserError, "Choose financial balances"):
            batch.action_preview()
        self.assertFalse(batch.archive_attachment_id)

    def test_repeating_purchase_only_skips_existing_copies(self):
        self._order()
        first = self._run(self._purchase_batch())
        repeat = self._purchase_batch()
        repeat.action_match()
        self.assertEqual(repeat.check_status, "blocked")
        with self.assertRaisesRegex(UserError, "no new purchase orders"):
            repeat.action_preview()
        self.assertEqual(self.env["company.financial.purchase.history"].search_count([
            ("company_id", "=", self.target.id)]), 1)
        self.assertTrue(first.report_attachment_id)

    def test_new_duplicate_of_an_already_copied_active_order_blocks(self):
        self._order()
        self._run(self._purchase_batch())
        duplicate = self._order()
        repeat = self._purchase_batch()
        with self.assertRaisesRegex(UserError, "duplicate unprocessed purchase orders"):
            repeat.action_preview()
        self.assertFalse(repeat.purchase_history_ids)
        duplicate.button_cancel()
        self._run(repeat)
        self.assertEqual(repeat.purchase_history_ids.source_order_res_id, duplicate.id)

    def test_native_archive_contains_history_and_cannot_be_changed_by_rpc_flags(self):
        self._order()
        batch = self._run(self._purchase_batch())
        archive = batch.archive_attachment_id
        payload = json.loads(archive.raw)
        parameter = self.env["ir.config_parameter"].sudo().search([("key", "=", "company_financial_cutover.archive." + batch.archive_key)])
        manifest = json.loads(parameter.value)
        self.assertEqual(hashlib.sha256(archive.raw).hexdigest(), manifest["sha256"])
        self.assertFalse(archive.res_model)
        self.assertFalse(archive.public)
        self.assertEqual(payload["purchases"][0]["snapshot"]["lines"][0]["qty"], 5)
        self.assertFalse(self.env["ir.model.data"].search([("model", "=", "ir.attachment"), ("res_id", "=", archive.id)]))
        for call in (lambda: archive.with_context(_fifo_archive_token=True).write({"raw": b"changed"}), lambda: archive.unlink()):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                call()

    def test_archive_failure_rolls_back_the_financial_move_and_purchase_history(self):
        self._invoice()
        self._order()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        with patch.object(type(batch), "_save_durable_archive", side_effect=UserError("Archive failed")):
            with self.assertRaisesRegex(UserError, "Archive failed"):
                batch.action_apply()
        self.assertEqual(batch.state, "preview")
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.purchase_history_ids)
        self.assertFalse(self.env["account.move"].search([("company_id", "=", self.target.id)]))

    def test_purchase_user_cannot_read_private_financial_recovery_archives(self):
        self._invoice()
        self._order()
        batch = self._run()
        buyer = new_test_user(self.env, login="archive_buyer", groups="purchase.group_purchase_user",
            company_id=self.target.id, company_ids=[self.target.id])
        buyer_batch = batch.with_user(buyer).with_context(allowed_company_ids=[self.target.id])
        self.assertEqual(batch.purchase_history_ids.with_env(buyer_batch.env).read(["name"])[0]["name"], batch.purchase_history_ids.name)
        with self.assertRaises(AccessError):
            batch.archive_attachment_id.with_env(buyer_batch.env).read(["raw"])

    def test_uninstall_rejects_a_reopened_original_with_an_active_replacement(self):
        from ..hooks import uninstall_hook
        original = self._order(confirmed=True)
        batch = self._run(self._purchase_batch())
        original.button_cancel()
        batch.purchase_history_ids.action_prepare_draft()
        original.button_draft()
        with self.assertRaisesRegex(UserError, "Cancel the original purchase order"):
            uninstall_hook(self.env)
        original.button_cancel()
        uninstall_hook(self.env)
        self.assertTrue(batch.archive_attachment_id)

    def test_missing_custom_history_with_a_durable_marker_blocks_recopy(self):
        self._order()
        batch = self._run(self._purchase_batch())
        self.env.cr.execute("DELETE FROM company_financial_purchase_history WHERE id = %s", [batch.purchase_history_ids.id])
        self.env.invalidate_all()
        repeat = self._purchase_batch()
        with self.assertRaisesRegex(UserError, "history screen is missing"):
            repeat.action_preview()
        self.assertTrue(batch.archive_attachment_id)

    def test_a_bad_archive_hash_blocks_restoration(self):
        self._order()
        batch = self._run(self._purchase_batch())
        parameter = self.env["ir.config_parameter"].sudo().search([("key", "=", "company_financial_cutover.archive." + batch.archive_key)])
        manifest = json.loads(parameter.value)
        manifest["sha256"] = "0" * 64
        parameter.value = json.dumps(manifest)
        with self.assertRaisesRegex(UserError, "missing or changed"):
            batch._load_durable_archive(parameter)

    def test_purchase_markers_block_copying_to_another_company(self):
        self._order()
        self._run(self._purchase_batch())
        other = self.env["res.company"].create({"name": "Other replacement", "currency_id": self.target.currency_id.id})
        Cutover = self.env["company.financial.cutover"].with_context(allowed_company_ids=(self.source | self.target | other).ids)
        batch = Cutover.create({"source_company_id": self.source.id, "target_company_id": other.id,
            "include_financial": False, "include_purchase_history": True})
        with self.assertRaisesRegex(UserError, "already moved to another company"):
            batch.action_preview()

    def test_duplicate_vendor_bills_block_until_the_duplicate_is_reversed(self):
        original = self._invoice("in_invoice", 100)
        duplicate = self._invoice("in_invoice", 100)
        (original | duplicate).write({"ref": "SUPPLIER-2024-1"})
        batch = self._batch()
        with self.assertRaisesRegex(UserError, "duplicate vendor bills"):
            batch.action_preview()
        duplicate._reverse_moves([{"date": self.cutoff, "invoice_date": self.cutoff}], cancel=True)
        self._run(batch)
        self.assertEqual(batch.line_ids.filtered(lambda line: line.kind == "open_item").balance, -100)
