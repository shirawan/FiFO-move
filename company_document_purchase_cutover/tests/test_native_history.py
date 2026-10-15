from datetime import datetime
from unittest.mock import patch
from odoo import Command
from odoo.tests import tagged
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from .test_purchase_documents import TestPurchaseDocuments


@tagged('post_install', '-at_install')
class TestExistingNativeHistory(FinancialCutoverCase):
    _job = TestPurchaseDocuments._job
    _match = TestPurchaseDocuments._match
    _run_job = TestPurchaseDocuments._run_job
    _products = TestPurchaseDocuments._products
    _po = TestPurchaseDocuments._po
    _receive = TestPurchaseDocuments._receive
    _purchase_job = TestPurchaseDocuments._purchase_job
    _history_fixture = TestPurchaseDocuments._history_fixture

    def _native_snapshot(self):
        return {m:self.env[m].search([], order='id').read(['write_date']) for m in
            ('stock.move', 'stock.move.line', 'stock.quant', 'product.value', 'account.move', 'account.move.line', 'purchase.order')}

    def test_existing_manual_valuation_reused_read_only(self):
        source, target, job = self._history_fixture()
        receipts = [o.order_line.move_ids.filtered(lambda m:m.state == 'done') for o in (source, target)]
        values = [self.env['product.value'].create({'move_id': move.id, 'value': 71,
            'date': datetime(2024, 6, 29)}) for move in receipts]
        self._match(job, *values)
        before = self._native_snapshot()
        with patch.object(type(values[0]), 'create', side_effect=AssertionError('No new manual valuation')):
            self._run_job(job)
        self.assertEqual(before, self._native_snapshot())
        job.action_execute()
        values[1].value = 72
        with self.assertRaisesRegex(Exception, 'evidence changed'): job.action_execute()

    def test_manual_valuation_missing_or_different_match_blocks(self):
        source, target, job = self._history_fixture()
        a = self.env['product.value'].create({'move_id': source.order_line.move_ids.filtered(lambda m:m.state == 'done').id,
            'value': 71, 'date': datetime(2024, 6, 29)})
        b = self.env['product.value'].create({'move_id': target.order_line.move_ids.filtered(lambda m:m.state == 'done').id,
            'value': 71, 'date': datetime(2024, 6, 28)})
        job.action_review(); self.assertEqual(job.state, 'blocked')
        self._match(job, a, b)
        job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('valuation adjustment differs', job.report)

    def test_existing_valuation_journal_adopted_without_second_asset(self):
        source, target, job = self._history_fixture()
        original = self._entry(self.source, [('expense', 40), ('equity', -40)])
        existing = self._entry(self.target, [('expense', 40), ('equity', -40)])
        source.order_line.move_ids.filtered(lambda m:m.state == 'done').account_move_id = original
        target.order_line.move_ids.filtered(lambda m:m.state == 'done').account_move_id = existing
        self._match(job, original, existing)
        for name in ('location_id', 'location_dest_id'):
            self._match(job, source.order_line.move_ids.filtered(lambda m:m.state == 'done')[name],
                target.order_line.move_ids.filtered(lambda m:m.state == 'done')[name])
        job.include_accounting = True
        before = self._native_snapshot()
        with patch.object(type(self.env['account.move']), 'action_post', side_effect=AssertionError('No valuation posting')):
            self._run_job(job)
        self.assertEqual(before, self._native_snapshot())
        self.assertEqual(job._stored_link(original)['target'], existing.id)
        self.assertEqual(self.env['account.move'].search_count([('company_id', '=', self.target.id)]), 1)

    def test_missing_existing_valuation_blocks_audit_ledger_double_count(self):
        old, new = self._products(); order = self._po(old, 10, True)
        receipt = self._receive(order, 4); receipt.date = datetime(2024, 6, 29)
        original = self._entry(self.source, [('expense', 40), ('equity', -40)])
        receipt.account_move_id = original
        # Existing inventory-related financial opening cannot justify replaying source valuation.
        self._entry(self.target, [('expense', 40), ('equity', -40)])
        job = self._job(include_purchase=True); self._match(job, old, new)
        job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('already be represented', job.report)
        job.action_execute(); self.assertEqual(job.state, 'failed')
        self.assertEqual(self.env['account.move'].search_count([('company_id', '=', self.target.id)]), 1)

    def _complex_fixture(self, returns=False):
        source, target, job = self._history_fixture()
        for old, new in [(source.order_line.move_ids[0].location_id, target.order_line.move_ids[0].location_id),
                         (source.order_line.move_ids[0].location_dest_id, target.order_line.move_ids[0].location_dest_id)]:
            self._match(job, old, new)
        pending = [o.order_line.move_ids.filtered(lambda m:m.state not in ('done', 'cancel')) for o in (source, target)]
        pending[0].date = datetime(2024, 6, 30); pending[1].date = pending[0].date
        self._match(job, *pending)
        downstream = []
        for movement in pending:
            dest = movement.copy({'state': 'draft', 'picking_id': False, 'purchase_line_id': False,
                'date': movement.date, 'location_id': movement.location_dest_id.id,
                'location_dest_id': movement.location_dest_id.id})
            dest.move_orig_ids = [Command.link(movement.id)]
            downstream.append(dest)
        self._match(job, *downstream)
        if returns:
            results = []
            for order in (source, target):
                receipt = order.order_line.move_ids.filtered(lambda m:m.state == 'done')
                ret = receipt.copy({'origin_returned_move_id': receipt.id, 'state': 'draft', 'picking_id': False,
                    'location_id': receipt.location_dest_id.id, 'location_dest_id': receipt.location_id.id,
                    'product_uom_qty': 1, 'quantity': 0, 'date': datetime(2024, 6, 30)})
                ret._action_confirm(); ret.date = datetime(2024, 6, 30)
                results.append(ret)
            self._match(job, *results)
        return source, target, job, downstream

    def test_existing_pending_return_and_route_reused_without_execution(self):
        source, target, job, downstream = self._complex_fixture(returns=True)
        before = self._native_snapshot()
        with patch.object(type(self.env['stock.move']), '_action_done', side_effect=AssertionError('No receipt/return execution')):
            self._run_job(job)
        self.assertEqual(before, self._native_snapshot())
        self.assertEqual(job._pending_demand(target.order_line), 6)
        self.assertEqual(target.order_line.qty_received, 4)

    def test_complex_route_extra_target_edge_blocks(self):
        source, target, job, downstream = self._complex_fixture()
        unrelated = downstream[1].copy({'state': 'draft', 'picking_id': False, 'purchase_line_id': False})
        unrelated.move_orig_ids = [Command.link(downstream[1].id)]
        job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('route links differ', job.report)

    def test_downstream_value_mismatch_blocks_before_native_adoption(self):
        source, target, job, downstream = self._complex_fixture()
        downstream[1].value += 5
        job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('different state, quantity', job.report)

    def test_completed_history_detects_descendant_edit(self):
        from odoo.exceptions import UserError
        source, target, job, downstream = self._complex_fixture()
        self._run_job(job)
        downstream[1].value += 5
        with self.assertRaisesRegex(UserError, 'settings changed'): job.action_execute()

    def test_optional_native_choices_remove_and_recover_without_changing_done_job(self):
        from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG
        from odoo.addons.company_document_cutover.models.migration import remove_optional_document_matches
        source, target, job = self._history_fixture(); self._run_job(job)
        choices = job.mapping_ids.filtered(lambda r:r.model_name in ('purchase.order','purchase.order.line','stock.move'))
        expected = {(r.model_name,r.source_id,r.target_id) for r in choices}
        before = self._native_snapshot()
        remove_optional_document_matches(choices.with_context(**{MODULE_UNINSTALL_FLAG:True}))
        self.assertEqual(job.state,'done')
        self.env['company.document.cutover'].action_restore_saved_moves()
        current = {(r.model_name,r.source_id,r.target_id) for r in job.mapping_ids}
        self.assertTrue(expected <= current); self.assertEqual(job.state,'done')
        self.assertEqual(before,self._native_snapshot()); job.action_execute()
