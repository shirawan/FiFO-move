from unittest.mock import patch

from odoo.tests import Form, new_test_user, tagged

from .test_cutover import FinancialCutoverCase


@tagged("post_install", "-at_install")
class TestReviewWorkflow(FinancialCutoverCase):
    def test_review_builds_one_plan_and_posts_only_on_confirmation(self):
        self._invoice()
        batch = self._batch()
        original = type(batch)._plan
        with patch.object(type(batch), "_plan", autospec=True, side_effect=original) as plan:
            batch.action_review()
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(batch.state, "preview")
        self.assertTrue(batch.snapshot_hash)
        self.assertFalse(batch.move_id)
        self.assertFalse(self.env["account.move"].search([("company_id", "=", self.target.id)]))
        batch.action_apply()
        self.assertEqual(batch.move_id.state, "posted")

    def test_review_shows_account_and_contact_decisions_together(self):
        old_contact = self.env["res.partner"].create({"name": "Workflow Alex", "company_id": self.source.id})
        new_contact = self.env["res.partner"].create({"name": "Workflow Alex", "company_id": self.target.id})
        self._entry(self.source, [("receivable", 100, old_contact), ("revenue", -100)])
        self.accounts[self.target.id]["revenue"].code = "4999"
        batch = self._batch()
        batch.action_review()
        self.assertEqual(batch.state, "draft")
        self.assertEqual(batch.check_status, "blocked")
        self.assertIn("contact", batch.decisions_text)
        self.assertIn("account", batch.decisions_text)
        self.assertEqual(batch.unresolved_mapping_ids.source_account_id, self.accounts[self.source.id]["revenue"])
        self.assertEqual(batch.unresolved_partner_mapping_ids.source_partner_id, old_contact)
        with Form(batch) as form:
            with form.unresolved_mapping_ids.edit(0) as choice:
                choice.target_account_id = self.accounts[self.target.id]["revenue"]
            with form.unresolved_partner_mapping_ids.edit(0) as choice:
                choice.target_partner_id = new_contact
        batch.action_review()
        self.assertEqual(batch.state, "preview")
        self.assertFalse(batch.decisions_text)
        self.assertFalse(batch.unresolved_mapping_ids)
        self.assertFalse(batch.unresolved_partner_mapping_ids)

    def test_existing_destination_is_detected_but_not_auto_approved(self):
        existing = self._entry(self.target, [("bank", 15), ("equity", -15)])
        self._invoice()
        batch = self._batch()
        batch.action_review()
        self.assertEqual(batch.destination_mode, "existing")
        self.assertFalse(batch.existing_data_reviewed)
        self.assertEqual(batch.check_status, "blocked")
        self.assertIn("earlier openings", batch.decisions_text)
        batch.existing_data_reviewed = True
        batch.action_review()
        self.assertEqual(batch.state, "preview")
        self.assertIn("Existing data", batch.summary)
        self.assertTrue(batch.destination_balance_preview)
        batch.action_apply()
        self.assertEqual(existing.state, "posted")
        self.assertFalse(existing.reversal_move_ids)

    def test_readonly_accountant_can_view_entry_without_posting_permission(self):
        self._invoice()
        batch = self._run()
        accountant = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}),
            login="workflow_readonly_accountant", groups="account.group_account_manager",
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        record = batch.with_user(accountant)
        self.assertFalse(record.can_run_move)
        self.assertTrue(record.can_view_entry)
        self.assertFalse(record.can_download_report)
        self.assertEqual(record.action_open_entry()["res_id"], batch.move_id.id)

    def test_settled_item_no_longer_asks_for_an_unneeded_contact_choice(self):
        contact = self.env["res.partner"].create({"name": "Workflow paid contact", "company_id": self.source.id})
        self.env["res.partner"].create({"name": contact.name, "company_id": self.target.id})
        old = self._entry(self.source, [("receivable", 100, contact), ("revenue", -100)])
        batch = self._batch()
        batch.action_review()
        self.assertEqual(batch.check_status, "blocked")
        self.assertEqual(batch.unresolved_partner_mapping_ids.source_partner_id, contact)
        payment = self._entry(self.source, [("bank", 100), ("receivable", -100, contact)])
        (old | payment).line_ids.filtered(lambda line: line.account_id.account_type == "asset_receivable").reconcile()
        batch.action_review()
        self.assertEqual(batch.state, "preview")
        self.assertFalse(batch.unresolved_partner_mapping_ids)
        self.assertIn("Contacts: 0 existing; 0 proposed new; 0 need a choice.", batch.check_report)
