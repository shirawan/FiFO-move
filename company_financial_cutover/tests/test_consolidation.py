from datetime import date
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_cutover import FinancialCutoverCase


@tagged('post_install', '-at_install')
class TestConsolidation(FinancialCutoverCase):
    def _branches(self):
        branches = self.env['res.company'].create([
            {'name': name, 'parent_id': self.source.id} for name in ('Branch B', 'Branch C')])
        self.env = self.env(context={**self.env.context,
            'allowed_company_ids': (self.source | branches | self.target).ids})
        for branch in branches:
            self.accounts = {**self.accounts, branch.id: self.accounts[self.source.id]}
            self.journals = {**self.journals, branch.id: {'general': self.env['account.journal'].with_company(branch).create({
                'name': branch.name + ' opening', 'code': 'MISC', 'type': 'general', 'company_id': branch.id})}}
        return branches.with_env(self.env)

    def _existing(self, **values):
        batch = self._batch()
        batch.write({'destination_mode': 'existing'})
        batch.write({'existing_data_reviewed': True, **values})
        return batch

    def test_parent_and_two_branches_combine_balances_and_preserve_items(self):
        branches = self._branches()
        originals = self._entry(self.source, [('receivable', 100, self.customer), ('revenue', -100)])
        for branch, amount in zip(branches, (200, 300)):
            originals |= self._entry(branch, [('receivable', amount, self.customer), ('revenue', -amount)])
        batch = self._batch()
        batch.write({'include_source_branches': True})
        self._run(batch)
        items = batch.line_ids.filtered(lambda line: line.kind == 'open_item')
        self.assertEqual(sorted(items.mapped('balance')), [100, 200, 300])
        self.assertEqual(sum(batch.move_id.line_ids.filtered(lambda line: line.balance > 0).mapped('balance')), 600)
        self.assertEqual(set(items.source_line_id.company_id.ids), set((self.source | branches).ids))
        self.assertTrue(all(company.name in ' '.join(items.mapped('label')) for company in branches))
        self.assertEqual(set(batch.completed_source_ids), set((self.source | branches).ids))
        self.assertEqual(originals.mapped('state'), ['posted'] * 3)
        for branch in branches:
            key = 'company_financial_cutover.completed.source.%s' % branch.id
            self.assertTrue(self.env['ir.config_parameter'].sudo().get_param(key))
        self.assertEqual(batch._archive_payload()['audit']['completed_source_ids'], batch.completed_source_ids)

    def test_archived_branch_is_included_not_silently_omitted(self):
        branches = self._branches()
        for company, amount in zip(self.source | branches, (10, 20, 30)):
            self._entry(company, [('bank', amount), ('equity', -amount)])
        branches[1].write({'active': False})
        batch = self._batch()
        batch.write({'include_source_branches': True})
        self._run(batch)
        self.assertEqual(batch.move_id.line_ids.filtered(lambda line: line.balance > 0).balance, 60)

    def test_unselected_branch_requires_company_switcher(self):
        branches = self._branches()
        batch = self._batch()
        batch.write({'include_source_branches': True})
        restricted = batch.with_context(allowed_company_ids=(self.source | self.target).ids)
        with self.assertRaisesRegex(AccessError, 'every included branch'):
            restricted.action_match()

    def test_branch_added_after_preview_rejects_changed_scope(self):
        self._branches()
        self._invoice()
        batch = self._batch()
        batch.write({'include_source_branches': True})
        batch.action_match()
        batch.action_preview()
        extra = self.env['res.company'].create({'name': 'New branch D', 'parent_id': self.source.id})
        batch = batch.with_context(allowed_company_ids=self.env.companies.ids + extra.ids)
        with self.assertRaisesRegex(UserError, 'changed.*fresh Preview'):
            batch.action_apply()
        self.assertFalse(batch.move_id)

    def test_previously_moved_branch_blocks_parent_consolidation(self):
        branches = self._branches()
        self._invoice()
        self.env['ir.config_parameter'].sudo().set_param(
            'company_financial_cutover.completed.source.%s' % branches[0].id, 'previous move')
        batch = self._batch()
        batch.write({'include_source_branches': True})
        batch.action_match()
        with self.assertRaisesRegex(UserError, 'already has a completed'):
            batch.action_preview()

    def test_used_destination_preserves_new_activity_and_drafts(self):
        self._invoice()
        new = self._entry(self.target, [('bank', 25), ('revenue', -25)])
        before = [(line.id, line.balance, line.partner_id.id) for line in new.line_ids]
        draft = self.env['account.move'].with_company(self.target).create({
            'company_id': self.target.id, 'journal_id': self.journals[self.target.id]['general'].id})
        batch = self._existing()
        self._run(batch)
        self.assertEqual(before, [(line.id, line.balance, line.partner_id.id) for line in new.line_ids])
        self.assertEqual(draft.state, 'draft')
        self.assertFalse(batch.correction_move_ids)
        self.assertEqual(self.env['account.move'].search_count([('company_id', '=', self.target.id)]), 3)

    def test_used_destination_requires_explicit_overlap_review(self):
        self._invoice()
        self._entry(self.target, [('bank', 25), ('revenue', -25)])
        batch = self._batch()
        batch.write({'destination_mode': 'existing'})
        batch.action_match()
        self.assertEqual(batch.check_status, 'blocked')
        self.assertIn('identify earlier openings and copied invoices', batch.check_report)
        self.assertFalse(batch.move_id)

    def test_replace_earlier_opening_preserves_payment_and_new_invoice(self):
        self._invoice(amount=100)
        old = self._entry(self.target, [('receivable', 100, self.customer), ('revenue', -100)])
        payment = self._entry(self.target, [('bank', 40), ('receivable', -40, self.customer)])
        ar = old.line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable')
        (ar | payment.line_ids.filtered(lambda line: line.account_id == ar.account_id)).reconcile()
        trading = self._entry(self.target, [('receivable', 20, self.customer), ('revenue', -20)])
        batch = self._existing(prior_opening_move_ids=[Command.set(old.ids)])
        self._run(batch)
        self.assertEqual(old.state, 'posted')
        self.assertEqual(payment.state, 'posted')
        self.assertEqual(ar.amount_residual, 0)
        account_preview = next(row for row in batch.destination_balance_preview if row['account'].endswith('receivable'))
        self.assertEqual((account_preview['before'], account_preview['added'], account_preview['removed'], account_preview['after']), (80, 100, -100, 80))
        carried = batch.move_id.line_ids.filtered(lambda line: line.account_id == ar.account_id)
        self.assertEqual(carried.amount_residual, 60)
        self.assertEqual(trading.line_ids.filtered(lambda line: line.account_id == ar.account_id).amount_residual, 20)
        self.assertEqual(batch.correction_move_ids.state, 'posted')
        self.assertEqual(batch.correction_move_ids.financial_cutover_id, batch)
        payload = batch._archive_payload()
        batch._validate_archived_opening(payload)
        self.assertEqual(payload['corrections'][0]['id'], batch.correction_move_ids.id)
        with self.assertRaisesRegex(UserError, 'cannot be changed'):
            batch.correction_move_ids.button_draft()
        with self.assertRaises(UserError):
            batch.action_apply()

    def test_changed_prior_opening_rejects_stale_preview(self):
        self._invoice()
        old = self._entry(self.target, [('receivable', 100, self.customer), ('revenue', -100)])
        batch = self._existing(prior_opening_move_ids=[Command.set(old.ids)])
        batch.action_match()
        batch.action_preview()
        old.write({'ref': 'Changed after review'})
        with self.assertRaisesRegex(UserError, 'changed.*fresh Preview'):
            batch.action_apply()
        self.assertFalse(batch.move_id)
        self.assertFalse(old.reversal_move_ids)

    def test_failure_rolls_back_opening_and_reversal_together(self):
        self._invoice()
        old = self._entry(self.target, [('receivable', 100, self.customer), ('revenue', -100)])
        batch = self._existing(prior_opening_move_ids=[Command.set(old.ids)])
        batch.action_match()
        batch.action_preview()
        before = self.env['account.move'].search_count([])
        with patch.object(type(batch), '_reconcile_opening_groups', side_effect=UserError('simulation')):
            with self.assertRaisesRegex(UserError, 'simulation'):
                batch.action_apply()
        self.assertEqual(self.env['account.move'].search_count([]), before)
        self.assertFalse(old.reversal_move_ids)
        self.assertEqual(batch.state, 'preview')

    def _copied_invoice(self, source):
        return self.env['account.move'].with_company(self.target).create({
            'company_id': self.target.id, 'move_type': source.move_type, 'partner_id': self.customer.id,
            'journal_id': self.journals[self.target.id]['sale'].id,
            'date': source.date, 'invoice_date': source.invoice_date, 'ref': source.name,
            'invoice_line_ids': [Command.create({'name': 'Already copied', 'quantity': 1,
                'price_unit': source.amount_total, 'account_id': self.accounts[self.target.id]['revenue'].id,
                'tax_ids': [Command.clear()]})]})

    def test_explicit_copied_invoice_is_not_carried_twice(self):
        source = self._invoice(amount=100)
        self._entry(self.source, [('bank', 30), ('equity', -30)])
        copied = self._copied_invoice(source)
        copied.action_post()
        batch = self._existing()
        batch.action_match()
        self.env['company.financial.copied.invoice'].create({
            'cutover_id': batch.id, 'source_move_id': source.id, 'target_move_id': copied.id})
        self._run(batch)
        self.assertFalse(batch.line_ids.filtered(lambda line: line.kind == 'open_item'))
        self.assertEqual(copied.amount_residual, 100)
        self.assertEqual(batch.move_id.line_ids.filtered(lambda line: line.balance > 0).balance, 30)
        self.assertEqual(batch._archive_payload()['copied_invoices'][0]['target_move_id'], copied.id)

    def test_partly_paid_source_cannot_be_skipped_without_its_payment(self):
        source = self._invoice(amount=100)
        payment = self._entry(self.source, [('bank', 40), ('receivable', -40, self.customer)])
        ar = source.line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable')
        (ar | payment.line_ids.filtered(lambda line: line.account_id == ar.account_id)).reconcile()
        copied = self._copied_invoice(source)
        copied.action_post()
        batch = self._existing()
        batch.action_match()
        self.env['company.financial.copied.invoice'].create({
            'cutover_id': batch.id, 'source_move_id': source.id, 'target_move_id': copied.id})
        batch.action_match()
        self.assertIn('copied payments need a separate', batch.check_report)
        with self.assertRaisesRegex(UserError, 'copied payments need a separate'):
            batch.action_preview()

    def test_branch_purchase_history_keeps_actual_company_and_skips_repeats(self):
        branches = self._branches()
        product = self.env['product.product'].create({'name': 'Branch service', 'type': 'service',
            'supplier_taxes_id': [Command.clear()]})
        orders = self.env['purchase.order']
        for company in self.source | branches:
            orders |= self.env['purchase.order'].with_company(company).create({
                'company_id': company.id, 'partner_id': self.vendor.id, 'partner_ref': 'SAME-REF',
                'date_order': '2024-06-30 10:00:00',
                'order_line': [Command.create({'product_id': product.id, 'name': 'Branch order',
                    'product_qty': 5, 'price_unit': 10, 'product_uom_id': product.uom_id.id,
                    'date_planned': '2024-07-01 10:00:00', 'tax_ids': [Command.clear()]})]})
        batch = self._batch()
        batch.write({'include_source_branches': True, 'include_financial': False})
        self._run(batch)
        histories = batch.purchase_history_ids
        self.assertEqual(len(histories), 3)
        self.assertEqual(set(histories.source_company_id.ids), set((self.source | branches).ids))
        for history in histories:
            self.assertEqual(history.snapshot['company_id'], history.source_company_id.id)
            self.assertEqual(history.source_company_name, history.source_company_id.name)
            marker = self.env['ir.config_parameter'].sudo().get_param(
                'company_financial_cutover.purchase.%s.%s' % (history.source_company_id.id, history.source_order_res_id))
            self.assertTrue(marker)
        repeat = self.env['company.financial.cutover'].create({
            'source_company_id': branches[0].id, 'target_company_id': self.target.id,
            'include_financial': False, 'include_purchase_history': True})
        self.assertFalse(repeat._purchase_plan())
        # Each branch has a legitimate separate order with the same vendor ref.
        # It must not be mistaken for another active original in a different branch.
        history = histories.filtered(lambda row: row.source_company_id == branches[0])
        original = orders.filtered(lambda order: order.company_id == branches[0])
        original.button_cancel()
        history.action_prepare_draft()
        self.assertEqual(history.target_order_id.origin,
            'Migrated purchase %s/%s' % (branches[0].id, original.id))

    def test_completed_branch_scope_does_not_expand_when_new_branch_added(self):
        branches = self._branches()
        self._invoice()
        batch = self._batch()
        batch.write({'include_source_branches': True})
        self._run(batch)
        extra = self.env['res.company'].create({'name': 'Later branch', 'parent_id': self.source.id})
        self.assertNotIn(extra, batch._source_companies())
        self.assertEqual(set(batch._source_companies().ids), set((self.source | branches).ids))

    def test_recovery_relinks_corrections_and_keeps_payment_residual(self):
        self._invoice()
        old = self._entry(self.target, [('receivable', 100, self.customer), ('revenue', -100)])
        payment = self._entry(self.target, [('bank', 40), ('receivable', -40, self.customer)])
        (old | payment).line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable').reconcile()
        batch = self._existing(prior_opening_move_ids=[Command.set(old.ids)])
        self._run(batch)
        opening, corrections, key = batch.move_id, batch.correction_move_ids, batch.archive_key
        self.env.flush_all()
        self.env.cr.execute('UPDATE account_move SET financial_cutover_id = NULL WHERE financial_cutover_id = %s', [batch.id])
        self.env.cr.execute('DELETE FROM company_financial_cutover WHERE id = %s', [batch.id])
        self.env.invalidate_all()
        self.env['company.financial.cutover']._restore_archives()
        recovered = self.env['company.financial.cutover'].search([('archive_key', '=', key)])
        self.assertEqual(recovered.move_id, opening)
        self.assertEqual(recovered.correction_move_ids, corrections)
        self.assertEqual(corrections.financial_cutover_id, recovered)
        self.assertEqual(opening.line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable').amount_residual, 60)

    def test_changed_correction_rejects_archive_recovery(self):
        self._invoice()
        old = self._entry(self.target, [('receivable', 100, self.customer), ('revenue', -100)])
        batch = self._existing(prior_opening_move_ids=[Command.set(old.ids)])
        self._run(batch)
        payload = batch._archive_payload()
        payload['corrections'][0]['lines'][0]['balance'] += 10
        with self.assertRaisesRegex(UserError, 'archived financial opening has changed'):
            batch._validate_archived_opening(payload)

    def test_branch_outstanding_account_from_parent_is_not_lumped(self):
        branches = self._branches()
        outstanding = self.env['account.account'].with_company(self.source).create({
            'name': 'Parent outstanding receipts', 'code': '1090', 'account_type': 'asset_current',
            'reconcile': True, 'company_ids': [Command.set(self.source.ids)]})
        self.accounts[self.source.id]['outstanding'] = outstanding
        self.env['account.journal'].with_company(self.source).create({
            'name': 'Parent bank', 'code': 'BNK', 'type': 'bank', 'company_id': self.source.id,
            'default_account_id': self.accounts[self.source.id]['bank'].id,
            'suspense_account_id': outstanding.id})
        self._entry(branches[0], [('outstanding', 100), ('receivable', -100, self.customer)])
        batch = self._batch()
        batch.write({'include_source_branches': True})
        batch.action_match()
        self.assertIn('Finish bank reconciliation', batch.check_report)
        with self.assertRaisesRegex(UserError, 'Finish bank reconciliation'):
            batch.action_preview()

    def test_new_destination_activity_after_preview_requires_updated_totals(self):
        self._invoice()
        batch = self._existing()
        batch.action_match()
        batch.action_preview()
        self._entry(self.target, [('bank', 25), ('revenue', -25)])
        with self.assertRaisesRegex(UserError, 'changed.*fresh Preview'):
            batch.action_apply()
        self.assertFalse(batch.move_id)

    def test_copied_invoice_tax_recognition_must_match(self):
        source = self._invoice()
        copied = self._copied_invoice(source)
        copied.action_post()
        self.accounts[self.source.id]['inventory'].reconcile = True
        tax = self.env['account.tax'].with_company(self.source).create({
            'name': 'Zero cash basis', 'amount': 0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': self.source.id, 'tax_exigibility': 'on_payment',
            'cash_basis_transition_account_id': self.accounts[self.source.id]['inventory'].id,
            'tax_group_id': self.env['account.tax.group'].with_company(self.source).create({
                'name': 'Recognition test', 'company_id': self.source.id}).id})
        # Native repost with zero-rate cash-basis tax keeps the same amounts,
        # but future tax recognition differs from the copied invoice.
        source.button_draft()
        source.invoice_line_ids.tax_ids = tax
        source.action_post()
        batch = self._existing()
        batch.action_match()
        self.env['company.financial.copied.invoice'].create({
            'cutover_id': batch.id, 'source_move_id': source.id, 'target_move_id': copied.id})
        with self.assertRaisesRegex(UserError, 'different rates or payment recognition'):
            batch.action_preview()
