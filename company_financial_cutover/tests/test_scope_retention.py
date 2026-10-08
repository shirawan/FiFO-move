import hashlib
import json
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged, new_test_user

from .test_purchase_history import PurchaseMigrationCase


@tagged("post_install", "-at_install")
class TestScopeAndRetention(PurchaseMigrationCase):
    def test_vendor_choices_exclude_already_copied_and_cancelled_history(self):
        first_vendor = self.env["res.partner"].create({"name": "First vendor", "company_id": self.source.id})
        next_vendor = self.env["res.partner"].create({"name": "Next vendor", "company_id": self.source.id})
        copied = self._order()
        copied.partner_id = first_vendor
        self._run(self._purchase_batch())
        cancelled = self._order()
        cancelled.partner_id = first_vendor
        cancelled.button_cancel()
        pending = self._order()
        pending.partner_id = next_vendor
        pending.partner_ref = "NEXT-ORDER"
        batch = self._purchase_batch()
        batch.action_match()
        self.assertEqual(batch.partner_mapping_ids.source_partner_id, next_vendor)
        self.assertIn("Vendors needing a choice: 1", batch.check_report)
        batch.action_preview()
        self.assertEqual({row["id"] for row in batch.purchase_preview_data}, {cancelled.id, pending.id})

    def test_replacement_ui_guides_an_authorized_user_through_vendor_cancel_and_draft(self):
        old = self.env["res.partner"].create({"name": "Local supplier", "company_id": self.source.id})
        target = self.env["res.partner"].create({"name": "Local supplier", "company_id": self.target.id,
            "ref": "NEW-SUPPLIER", "email": "supplier@example.test"})
        original = self._order(confirmed=True)
        original.partner_id = old
        batch = self._run(self._purchase_batch())
        operator = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}),
            login="replacement-ui-operator", groups="base.group_system,account.group_account_manager,purchase.group_purchase_user",
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        history = batch.purchase_history_ids.with_user(operator).with_context(allowed_company_ids=[self.source.id, self.target.id])
        self.assertEqual(history.replacement_step, "vendor")
        self.assertEqual(history.current_original_status, "Confirmed order")
        action = history.action_choose_vendor()
        with Form(history.env[action["res_model"]].with_context(action["context"])) as chooser:
            chooser.vendor_id = target
            self.assertEqual(chooser.vendor_reference, "NEW-SUPPLIER")
            self.assertEqual(chooser.vendor_email, "supplier@example.test")
        chooser.record.action_confirm()
        self.assertEqual(history.replacement_step, "cancel")
        self.assertEqual(history.destination_vendor_id, target)
        original_action = history.action_open_original()
        self.assertEqual((original_action["res_model"], original_action["res_id"], original_action["target"]),
            ("purchase.order", original.id, "new"))
        self.assertEqual(original.state, "purchase")
        original.button_cancel()
        history.invalidate_recordset()
        self.assertEqual(history.replacement_step, "prepare")
        self.assertEqual(history.current_original_status, "Cancelled")
        history.action_prepare_draft()
        self.assertEqual(history.replacement_step, "created")
        replacement = history.target_order_id
        self.assertEqual((replacement.state, replacement.partner_id), ("draft", target))
        history.action_prepare_draft()
        self.assertEqual(history.target_order_id, replacement)

    def test_replacement_ui_sends_changed_originals_to_manager_review(self):
        original = self._order(confirmed=True)
        history = self._run(self._purchase_batch()).purchase_history_ids
        self.assertEqual(history.replacement_step, "cancel")
        original.order_line.qty_received_manual = 2
        history.invalidate_recordset()
        self.assertEqual(history.replacement_step, "manual")
        self.assertIn("original order changed", history.replacement_note)
        self.assertEqual(history.snapshot["lines"][0]["received"], 0)
        self.assertFalse(history.target_order_id)

    def test_purchase_only_name_match_can_be_chosen_before_original_cancellation(self):
        old = self.env["res.partner"].create({"name": "Local supplier", "company_id": self.source.id})
        target = self.env["res.partner"].create({"name": "Local supplier", "company_id": self.target.id})
        original = self._order(confirmed=True)
        original.partner_id = old
        batch = self._purchase_batch()
        batch.action_match()
        self.assertEqual(batch.partner_mapping_ids.source_partner_id, old)
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        self.assertIn("Vendors needing a choice: 1", batch.check_report)
        with Form(batch) as form:
            with form.partner_mapping_ids.edit(0) as choice:
                choice.target_partner_id = target
        self._run(batch)
        original.button_cancel()
        batch.purchase_history_ids.action_prepare_draft()
        self.assertEqual(batch.purchase_history_ids.target_order_id.partner_id, target)
        self.assertFalse(batch.move_id)

    def test_completed_history_vendor_choice_handles_missing_legacy_mappings_and_is_archived(self):
        old = self.env["res.partner"].create({"name": "Local supplier", "company_id": self.source.id})
        target = self.env["res.partner"].create({"name": "Local supplier", "company_id": self.target.id})
        original = self._order(confirmed=True)
        original.partner_id = old
        batch = self._run(self._purchase_batch())
        history = batch.purchase_history_ids
        self.env.cr.execute("DELETE FROM company_financial_partner_mapping WHERE cutover_id = %s", [batch.id])
        self.env.invalidate_all()
        operator = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}),
            login="vendor-choice-operator", groups="base.group_system,account.group_account_manager,purchase.group_purchase_user",
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        history = history.with_user(operator).with_context(allowed_company_ids=[self.source.id, self.target.id])
        self.assertFalse(history.env.su)
        action = history.action_choose_vendor()
        with Form(history.env[action["res_model"]].with_context(action["context"])) as form:
            form.vendor_id = target
        form.record.action_confirm()
        self.assertEqual(original.state, "purchase")
        self.assertEqual(history.replacement_vendor_id, target)
        payload = json.loads(batch.archive_attachment_id.raw)
        self.assertEqual(payload["purchases"][0]["replacement_vendor_id"], target.id)
        with self.assertRaisesRegex(UserError, "active existing vendor"):
            history._choose_vendor(old)
        target.active = False
        with self.assertRaisesRegex(UserError, "active existing vendor"):
            history._choose_vendor(target)
        target.active = True
        original.button_cancel()
        history.action_prepare_draft()
        self.assertEqual(history.target_order_id.partner_id, target)
        with self.assertRaisesRegex(UserError, "replacement already exists"):
            history._choose_vendor(self.vendor)
        with self.assertRaises(AccessError):
            history.write({"replacement_vendor_id": self.vendor.id})

    def _remove_financial_history_for_recovery(self, batch):
        move = batch.move_id
        self.env.flush_all()
        self.env.cr.execute("UPDATE account_move SET financial_cutover_id = NULL WHERE id = %s", [move.id])
        self.env.cr.execute("DELETE FROM company_financial_purchase_history WHERE cutover_id = %s", [batch.id])
        self.env.cr.execute("DELETE FROM company_financial_cutover WHERE id = %s", [batch.id])
        self.env.invalidate_all()
        return move

    def test_recovery_rejects_reposted_opening_with_changed_amounts(self):
        self._invoice()
        batch = self._run()
        archive_key = batch.archive_key
        move = self._remove_financial_history_for_recovery(batch)
        move.button_draft()
        move.write({"line_ids": [Command.update(line.id, {
            "debit": 120 if line.balance > 0 else 0, "credit": 120 if line.balance < 0 else 0,
            "amount_currency": 120 if line.balance > 0 else -120}) for line in move.line_ids]})
        move.action_post()
        with self.assertRaisesRegex(UserError, "archived financial opening has changed"):
            self.env["company.financial.cutover"]._restore_archives()
        self.assertFalse(move.financial_cutover_id)
        self.assertFalse(self.env["company.financial.cutover"].search([("archive_key", "=", archive_key)]))

    def test_recovery_allows_normal_payment_reconciliation(self):
        self._invoice()
        batch = self._run()
        move = batch.move_id
        receipt = self._entry(self.target, [("bank", 100), ("receivable", -100, self.customer)])
        (move | receipt).line_ids.filtered(lambda line: line.account_id.account_type == "asset_receivable").reconcile()
        self._remove_financial_history_for_recovery(batch)
        self.env["company.financial.cutover"]._restore_archives()
        self.assertEqual(move.financial_cutover_id.state, "done")
        self.assertEqual(move.line_ids.filtered(lambda line: line.account_id.account_type == "asset_receivable").amount_residual, 0)

    def test_old_archive_without_native_signature_still_checks_amounts(self):
        self._invoice()
        batch = self._run()
        payload = json.loads(batch.archive_attachment_id.raw)
        payload.pop("opening")
        batch._validate_archived_opening(payload)
        payload["lines"][0]["balance"] += 20
        with self.assertRaisesRegex(UserError, "archived financial opening has changed"):
            batch._validate_archived_opening(payload)

    def test_old_archive_with_excluded_stock_restores_included_line_sequences(self):
        self._entry(self.source, [("inventory", 100), ("equity", -100)])
        batch = self._batch()
        batch.offset_account_id = self.accounts[self.target.id]["clearing"]
        batch.action_match()
        batch.mapping_ids.filtered(lambda row: row.source_account_id == self.accounts[self.source.id]["inventory"]).handled_by_stock = True
        self._run(batch)
        payload = json.loads(batch.archive_attachment_id.raw)
        payload.pop("opening")
        batch._validate_archived_opening(payload)

    def test_selection_creates_the_chosen_scope_and_preserves_legacy_choices(self):
        for selection, financial, purchases in (("financial", True, False),
                ("purchase", False, True), ("both", True, True), ("stock", False, False)):
            batch = self.env["company.financial.cutover"].create({
                "source_company_id": self.source.id, "target_company_id": self.target.id,
                "move_scope": selection})
            self.assertEqual((batch.include_financial, batch.include_purchase_history), (financial, purchases))
            self.assertEqual(batch.move_scope, selection)
        legacy = self._batch()
        self.assertEqual(legacy.move_scope, "both")
        legacy.include_purchase_history = False
        self.assertEqual(legacy.move_scope, "financial")

    def test_selection_change_invalidates_review_and_cannot_change_a_completed_move(self):
        self._invoice()
        self._order()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self.assertTrue(batch.line_ids)
        with Form(batch) as form:
            form.move_scope = "purchase"
            self.assertFalse(form.include_financial)
            self.assertTrue(form.include_purchase_history)
            self.assertEqual(form.state, "draft")
            self.assertEqual(batch.state, "preview")
            self.assertTrue(batch.line_ids)
        self.assertEqual(batch.state, "draft")
        self.assertFalse(batch.line_ids)
        self.assertFalse(batch.snapshot_hash)
        self.assertFalse(batch.include_financial)
        self._run(batch)
        self.assertFalse(batch.move_id)
        self.assertTrue(batch.purchase_history_ids)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            batch.move_scope = "financial"

    def test_stock_selection_requires_its_addon_without_creating_financial_or_purchase_data(self):
        batch = self._batch()
        batch.move_scope = "stock"
        self.assertFalse(batch.include_financial)
        self.assertFalse(batch.include_purchase_history)
        self.assertFalse(batch.stock_mover_available)
        with self.assertRaisesRegex(UserError, "Stock moves need Company Stock Cutover"):
            batch.action_open_stock_mover()
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.purchase_history_ids)

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
        self.assertEqual(purchases.check_status, "ready")
        self.assertIn("Possible duplicate purchase orders", purchases.check_report)
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

    def test_new_duplicate_history_is_copied_without_repeating_the_first_copy(self):
        self._order()
        self._run(self._purchase_batch())
        duplicate = self._order()
        repeat = self._purchase_batch()
        repeat.action_match()
        self.assertEqual(repeat.check_status, "ready")
        self.assertIn("Possible duplicate purchase orders", repeat.check_report)
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

    def test_duplicate_vendor_bills_remain_separate_without_forcing_source_cleanup(self):
        original = self._invoice("in_invoice", 100)
        duplicate = self._invoice("in_invoice", 100)
        (original | duplicate).write({"ref": "SUPPLIER-2024-1"})
        before = [(move.id, move.state, move.amount_residual, move.write_date) for move in original | duplicate]
        batch = self._batch()
        batch.action_match()
        self.assertEqual(batch.check_status, "ready")
        self.assertIn("Possible duplicate vendor bills", batch.check_report)
        self._run(batch)
        opening = batch.line_ids.filtered(lambda line: line.kind == "open_item")
        self.assertEqual(sorted(opening.mapped("balance")), [-100, -100])
        self.assertEqual(len(opening.posted_line_id), 2)
        self.assertEqual(before, [(move.id, move.state, move.amount_residual, move.write_date) for move in original | duplicate])
