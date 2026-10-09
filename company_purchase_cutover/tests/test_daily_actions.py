import hashlib
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, new_test_user, tagged
from .test_purchase_history import PurchaseMigrationCase


@tagged('post_install', '-at_install')
class TestDailyPurchaseActions(PurchaseMigrationCase):
    def test_purchase_manager_can_prepare_without_accounting_or_settings(self):
        original = self._order(confirmed=True)
        batch = self._batch()
        batch.move_scope = 'purchase'
        self._run(batch)
        original.button_cancel()
        manager = new_test_user(self.env(context={**self.env.context, 'no_reset_password': True}),
            login='daily_purchase_manager', groups='purchase.group_purchase_manager',
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        self.assertFalse(manager.has_group('base.group_system'))
        self.assertFalse(manager.has_group('account.group_account_manager'))
        history = batch.purchase_history_ids.with_user(manager).with_context(allowed_company_ids=[self.source.id, self.target.id])
        archive_bytes = batch.archive_attachment_id.raw
        report_bytes = batch.report_attachment_id.raw
        with patch.object(type(batch), '_lock', side_effect=AssertionError('Daily action acquired cutover lock')):
            action = history.action_choose_vendor()
            with Form(history.env[action['res_model']].with_context(action['context'])) as chooser:
                chooser.vendor_id = self.vendor.with_env(history.env)
            chooser.record.action_confirm()
            history.action_prepare_draft()
            self.assertEqual(history.target_order_id.state, 'draft')
            history.action_prepare_draft()
        self.assertEqual(batch.archive_attachment_id.raw, archive_bytes)
        self.assertEqual(batch.report_attachment_id.raw, report_bytes)
        update = self.env['ir.config_parameter'].sudo().search([('key', '=like', 'company_financial_cutover.purchase_update.' + batch.archive_key + '.%')])
        self.assertEqual(len(update), 1)
        manifest = json.loads(update.value)
        raw = self.env['ir.attachment'].sudo().browse(manifest['attachment_id']).raw
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['sha256'])
        self.assertEqual(json.loads(raw)['target_order_id'], history.target_order_id.id)
        self.assertLess(len(raw), 1024)
        self.assertEqual(self.env['purchase.order'].search_count([('company_id', '=', self.target.id)]), 1)

    def test_purchase_reader_cannot_mutate_followups(self):
        self._order()
        batch = self._batch()
        batch.move_scope = 'purchase'
        self._run(batch)
        buyer = new_test_user(self.env(context={**self.env.context, 'no_reset_password': True}),
            login='daily_purchase_reader', groups='purchase.group_purchase_user',
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        history = batch.purchase_history_ids.with_user(buyer).with_context(allowed_company_ids=[self.source.id, self.target.id])
        for action in (lambda: history._choose_vendor(self.vendor.with_env(history.env)), history.action_prepare_draft):
            with self.assertRaises(AccessError):
                action()

    def test_cancelled_replacement_reference_allows_other_eligible_history(self):
        originals = self._order() | self._order()
        batch = self._batch()
        batch.move_scope = 'purchase'
        self._run(batch)
        originals.button_cancel()
        first, second = batch.purchase_history_ids.sorted('id')
        first.action_prepare_draft()
        first.target_order_id.button_cancel()
        second.action_prepare_draft()
        self.assertEqual(first.target_order_id.state, 'cancel')
        self.assertEqual(second.target_order_id.state, 'draft')
        self.assertNotEqual(first.target_order_id, second.target_order_id)

    def test_invalid_reference_marker_blocks_without_creating_an_order(self):
        original = self._order()
        batch = self._batch()
        batch.move_scope = 'purchase'
        self._run(batch)
        original.button_cancel()
        history = batch.purchase_history_ids
        claim, _issues = history._claim_destination_identity(self.vendor)
        self.env['ir.config_parameter'].sudo().set_param(claim[0], '[]')
        with self.assertRaisesRegex(UserError, 'replacement-order marker needs recovery'):
            history.action_prepare_draft()
        self.assertFalse(history.target_order_id)
        self.assertEqual(self.env['purchase.order'].search_count([('company_id', '=', self.target.id)]), 0)
