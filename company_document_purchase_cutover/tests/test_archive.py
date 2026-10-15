"""Synthetic native history: preserving stock must not fabricate native receipts."""
import json
from datetime import datetime
from unittest.mock import patch
from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged, new_test_user
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from .test_purchase_documents import TestPurchaseDocuments


@tagged('post_install', '-at_install')
class TestPurchaseArchive(FinancialCutoverCase):
    _job = TestPurchaseDocuments._job
    _match = TestPurchaseDocuments._match
    _run_job = TestPurchaseDocuments._run_job
    _products = TestPurchaseDocuments._products
    _po = TestPurchaseDocuments._po
    _receive = TestPurchaseDocuments._receive
    _bill = TestPurchaseDocuments._bill

    def _fixture(self, billing=True):
        old, new = self._products()
        order = self._po(old, 10, True)
        receipt = self._receive(order, 4)
        receipt.date = datetime(2024, 6, 29)
        returned = receipt.copy({'origin_returned_move_id': receipt.id, 'product_uom_qty': 1,
            'quantity': 1, 'location_id': receipt.location_dest_id.id, 'location_dest_id': receipt.location_id.id,
            'picking_id': False, 'state': 'draft', 'purchase_line_id': order.order_line.id, 'to_refund': True})
        returned._action_confirm(); returned.picked = True; returned._action_done()
        returned.date = datetime(2024, 6, 30)
        bill = self._bill(order, 2) if billing else self.env['account.move']
        job = self._job(include_accounting=billing, include_purchase=True)
        self._match(job, old, new)
        return order, bill, job

    def _economics(self):
        evidence = {}
        for model, names in [
            ('stock.move', ['company_id', 'state', 'quantity', 'product_uom_qty', 'value', 'date', 'write_date']),
            ('stock.move.line', ['company_id', 'quantity', 'lot_id', 'owner_id', 'package_id', 'write_date']),
            ('stock.quant', ['company_id', 'quantity', 'reserved_quantity', 'write_date']),
            ('product.value', ['company_id', 'value', 'date', 'write_date']),
            ('purchase.order', ['company_id', 'state', 'write_date']),
        ]:
            evidence[model] = self.env[model].search([], order='id').read(names)
        return evidence

    def test_missing_receipts_archive_with_bill_refs_and_zero_stock_writes(self):
        order, bill, job = self._fixture()
        before = self._economics()
        # Existing unrelated new-company stock and trading must remain intact.
        new_product = job._target(order.order_line.product_id)
        self.env['stock.quant']._update_available_quantity(new_product,
            self.env['stock.warehouse'].search([('company_id', '=', self.target.id)], limit=1).lot_stock_id, 17)
        before = self._economics()
        with patch.object(type(self.env['stock.move']), '_action_done', side_effect=AssertionError('No stock execution')):
            self._run_job(job)
        self.assertEqual(before, self._economics())
        marker = job._stored_link(order)
        self.assertTrue(marker['audit_only']); self.assertEqual(marker['target_model'], 'ir.attachment')
        self.assertFalse(self.env['purchase.order'].search([('company_id', '=', self.target.id)]))
        self.assertEqual(len(job.purchase_archive_ids), 1)
        payload = json.loads(job.purchase_archive_ids.attachment_id.raw)
        line = payload['original']['lines'][0]
        self.assertEqual((line['qty_received'], line['qty_invoiced'], line['qty_to_invoice'], line['pending_supplier_delivery']), (3, 2, 1, 6))
        self.assertEqual(len(payload['original']['stock_history']), 3)
        moved = self.env['account.move'].browse(job._stored_link(bill)['target'])
        self.assertFalse(moved.invoice_line_ids.purchase_line_id)
        self.assertEqual(moved.amount_total, bill.amount_total)
        self.assertIn('saved accounting document #%s' % moved.id, job.purchase_archive_ids.accounting_matches)
        self.assertEqual(job._stored_link(bill)['purchase_history_references'][0]['target_bill_line'], moved.invoice_line_ids.id)
        job.action_execute()
        again = self._job(include_purchase=True)
        self._match(again, order.order_line.product_id, new_product)
        self._run_job(again)
        self.assertEqual(len(self.env['company.document.purchase.archive'].search([])), 1)
        self.assertEqual(self.env['account.move'].search_count([('company_id', '=', self.target.id)]), 1)

    def test_purchase_only_archive_needs_no_irrelevant_setup_or_freeze(self):
        order, bill, job = self._fixture(billing=False)
        job.mapping_ids.unlink()
        self._run_job(job)
        self.assertFalse(job.old_operations_frozen)
        self.assertIn('HISTORY ONLY', job.purchase_archive_ids.report)
        self.assertIn('Pending supplier delivery 6', job.purchase_archive_ids.report)
        self.assertFalse(self.env['purchase.order'].search([('company_id', '=', self.target.id)]))

    def test_pending_return_and_route_graph_retained_without_running_it(self):
        order, bill, job = self._fixture(billing=False)
        receipt = order.order_line.move_ids.filtered(lambda m:m.state == 'done' and not m.origin_returned_move_id)
        pending = receipt.copy({'origin_returned_move_id': receipt.id, 'state': 'draft', 'picking_id': False,
            'product_uom_qty': 2, 'quantity': 0, 'location_id': receipt.location_dest_id.id, 'location_dest_id': receipt.location_id.id})
        pending._action_confirm()
        incoming = order.order_line.move_ids.filtered(lambda m:m.state not in ('done', 'cancel') and m.location_id.usage == 'supplier')
        downstream = incoming.copy({'state': 'draft', 'picking_id': False, 'purchase_line_id': False,
            'location_id': incoming.location_dest_id.id, 'location_dest_id': incoming.location_dest_id.id})
        downstream.move_orig_ids = [Command.link(incoming.id)]
        before = self._economics(); self._run_job(job); self.assertEqual(before, self._economics())
        payload = json.loads(job.purchase_archive_ids.attachment_id.raw)
        self.assertEqual(payload['original']['lines'][0]['pending_supplier_return'], 2)
        graph = {r['move']['id']: r for r in payload['original']['stock_history']}
        self.assertEqual(graph[incoming.id]['graph']['next_moves'], downstream.ids)
        self.assertIn(pending.id, graph[receipt.id]['graph']['returns'])

    def test_archive_tamper_cannot_duplicate_and_is_reported_together(self):
        order, bill, job = self._fixture(billing=False); self._run_job(job)
        job.purchase_archive_ids.attachment_id.raw = b'{}'
        self.env.invalidate_all()
        self.assertIn('needs accountant review', job.purchase_archive_ids.with_context(bin_size=True).accounting_matches)
        with self.assertRaisesRegex(UserError, 'historical purchase evidence'):
            job.action_execute()
        again = self._job(include_accounting=False, include_purchase=True)
        again.action_review(); self.assertEqual(again.state, 'blocked')
        self.assertIn('historical purchase evidence', again.report)
        self.assertEqual(self.env['company.document.purchase.archive'].search_count([]), 1)

    def test_financial_failure_rolls_back_bill_and_archive_and_retry_once(self):
        order, bill, job = self._fixture()
        job.action_review(); self.assertEqual(job.state, 'ready', job.report)
        with patch.object(type(job), '_archive_purchase', side_effect=UserError('Synthetic archive failure')):
            job.action_execute()
        self.assertEqual(job.state, 'failed')
        self.assertFalse(self.env['account.move'].search([('company_id', '=', self.target.id)]))
        self.assertFalse(self.env['company.document.purchase.archive'].search([]))
        self._run_job(job)
        self.assertEqual(len(job.purchase_archive_ids), 1)

    def test_archive_rows_readonly_and_company_scoped_with_private_download(self):
        order, bill, job = self._fixture(billing=False); self._run_job(job)
        history = job.purchase_archive_ids
        manager = new_test_user(self.env, login='audit_purchase_manager', groups='purchase.group_purchase_manager',
            company_id=self.target.id, company_ids=[Command.set((self.source | self.target).ids)])
        readable = history.with_user(manager).with_context(allowed_company_ids=(self.source | self.target).ids)
        self.assertTrue(readable.saved_documents)
        with self.assertRaises(AccessError): readable.write({'report': 'fake received quantity'})
        with self.assertRaises(AccessError): readable.unlink()
        hidden = self.env['company.document.purchase.archive'].with_user(manager).with_context(allowed_company_ids=self.target.ids)
        self.assertFalse(hidden.search([]))
        self.assertFalse(history.attachment_id.public)
        self.assertFalse(history.attachment_id.res_model)

    def test_recovery_rebuilds_archive_screen_from_surviving_private_data(self):
        order, bill, job = self._fixture(billing=False); self._run_job(job)
        saved = job.purchase_archive_ids.attachment_id
        payload = json.loads(job.audit_attachment_id.raw)
        # Simulate addon tables disappearing, retaining native blobs and markers.
        self.env.cr.execute('DELETE FROM company_document_purchase_archive WHERE job_id = %s', [job.id])
        job._restore_extra_history(payload)
        history = self.env['company.document.purchase.archive'].search([('job_id', '=', job.id)])
        self.assertEqual(len(history), 1); self.assertEqual(history.attachment_id, saved)
        job._restore_extra_history(payload)
        self.assertEqual(self.env['company.document.purchase.archive'].search_count([]), 1)

    def test_unrelated_existing_po_duplicate_requires_explicit_choice(self):
        order, bill, job = self._fixture(billing=False)
        self.env['purchase.order'].with_company(self.target).create({'partner_id': self.vendor.id,
            'company_id': self.target.id, 'partner_ref': order.name})
        job.action_review(); self.assertEqual(job.state, 'blocked')
        self.assertIn('Possible existing purchase duplicate', job.report)

    def test_first_financial_step_keeps_history_for_resume(self):
        order, bill, job = self._fixture()
        second = self._bill(order, 1)
        job.batch_size = 1
        job.action_review(); self.assertEqual(job.state, 'ready', job.report)
        job.action_execute(); self.assertEqual(job.state, 'partial', job.report)
        self.assertEqual(len(job.purchase_archive_ids), 1)
        receipt = json.loads(job.audit_attachment_id.raw)
        self.assertTrue(any(link.get('audit_only') for link in receipt['links']))
        self.assertEqual(self.env['account.move'].search_count([('company_id', '=', self.target.id)]), 1)
        job.action_review(); self.assertEqual(job.state, 'ready', job.report)
        job.action_execute(); self.assertEqual(job.state, 'done', job.report)
        self.assertEqual(self.env['account.move'].search_count([('company_id', '=', self.target.id)]), 2)
        self.assertTrue(job._stored_link(second))
        self.assertEqual(len(job.purchase_archive_ids), 1)
        self.assertFalse(self.env['purchase.order'].search([('company_id', '=', self.target.id)]))

    def test_unselected_company_bill_link_cannot_leak_into_archive(self):
        order, bill, job = self._fixture(billing=False)
        outsider = self.env['res.company'].create({'name': 'Unselected synthetic company',
            'currency_id': self.source.currency_id.id, 'account_fiscal_country_id': self.source.account_fiscal_country_id.id})
        # Invalid cross-company linkage represents legacy imports/settings errors.
        # Raw SQL is fixture-only, never used by the mover.
        original_bill = self._bill(order, 1)
        self.env.flush_all()
        self.env.cr.execute('UPDATE account_move SET company_id=%s WHERE id=%s', [outsider.id, original_bill.id])
        self.env.cr.execute('UPDATE account_move_line SET company_id=%s WHERE move_id=%s', [outsider.id, original_bill.id])
        self.env.invalidate_all()
        job.action_review(); self.assertEqual(job.state, 'blocked', job.report)
        self.assertIn('unselected company', job.report)
        self.assertFalse(self.env['company.document.purchase.archive'].search([]))

    def test_stock_graph_edit_after_review_invalidates_archive_confirmation(self):
        order, bill, job = self._fixture(billing=False)
        job.action_review(); self.assertEqual(job.state, 'ready', job.report)
        receipt = order.order_line.move_ids.filtered(lambda m:m.state == 'done' and not m.origin_returned_move_id)
        receipt.value += 5
        job.action_execute(); self.assertEqual(job.state, 'failed'); self.assertIn('changed', job.report)
        self.assertFalse(self.env['company.document.purchase.archive'].search([]))

    def test_accountant_reads_history_without_stock_write_access_or_company_leaks(self):
        order, bill, job = self._fixture(billing=False)
        receipt = order.order_line.move_ids.filtered(lambda m:m.state=='done' and not m.origin_returned_move_id)
        value = self.env['product.value'].create({'move_id':receipt.id,'value':45,'date':datetime(2024,6,29)})
        outsider = self.env['res.company'].create({'name':'Private package company'})
        package_type = self.env['stock.package.type'].create({'name':'Private outside-company type','company_id':outsider.id})
        shared_type = self.env['stock.package.type'].create({'name':'Shared synthetic package type','company_id':False})
        self.env = self.env(context={**self.env.context,'no_reset_password':True,'mail_notify_force_send':False})
        manager = new_test_user(self.env, login='document_accountant_no_stock',
            groups='base.group_system,account.group_account_manager,purchase.group_purchase_manager',
            company_id=self.source.id, company_ids=[Command.set((self.source|self.target).ids)])
        self.assertFalse(manager.has_group('stock.group_stock_manager'))
        userenv = self.env(user=manager,context={**self.env.context,'allowed_company_ids':(self.source|self.target).ids})
        self.assertEqual(userenv['product.value'].browse(value.id).value,45)
        with self.assertRaises(AccessError): userenv['product.value'].browse(value.id).write({'value':99})
        self.assertFalse(userenv['stock.package.type'].search([('id','=',package_type.id)]))
        self.assertTrue(userenv['stock.package.type'].search([('id','=',shared_type.id)]))
        userjob = userenv['company.document.cutover'].browse(job.id)
        self._run_job(userjob)
        self.assertTrue(userjob.purchase_archive_ids.saved_documents)

    def test_web_binary_size_context_preserves_json_and_download_content(self):
        import base64
        order, bill, job = self._fixture(); self._run_job(job)
        history = job.purchase_archive_ids
        self.env.invalidate_all()
        web_values = history.with_context(bin_size=True).read(['accounting_matches','saved_documents'])[0]
        self.assertIn('saved accounting document #', web_values['accounting_matches'])
        self.assertTrue(web_values['saved_documents'])
        raw = history.attachment_id.with_context(bin_size=False).raw
        self.assertEqual(base64.b64decode(history.with_context(bin_size=False).saved_documents), raw)
        job.with_context(bin_size=True).action_execute()

    def test_damaged_accounting_match_does_not_crash_history_screen(self):
        order, bill, job = self._fixture(); self._run_job(job)
        self.env['ir.config_parameter'].sudo().set_param(job._key(bill), '{"line_map": []}')
        self.env.invalidate_all()
        self.assertIn('needs accountant review', job.purchase_archive_ids.with_context(bin_size=True).accounting_matches)

    def test_optional_reinstall_restores_existing_job_without_new_native_records(self):
        order, bill, job = self._fixture(); self._run_job(job)
        job_id = job.id; receipt = job.audit_attachment_id; saved = job.purchase_archive_ids.attachment_id
        before = self._economics()
        self.env.cr.execute('DELETE FROM company_document_purchase_archive WHERE job_id=%s',[job.id])
        job._system_write({'include_purchase':False,'purchase_history_mode':'native'})
        self.env.invalidate_all()
        self.env['company.document.cutover'].action_restore_saved_moves()
        self.assertEqual(job.id,job_id); self.assertEqual(job.state,'done')
        self.assertTrue(job.include_purchase); self.assertEqual(job.purchase_history_mode,'preserve_stock')
        self.assertEqual(len(job.purchase_archive_ids),1); self.assertEqual(job.purchase_archive_ids.attachment_id,saved)
        self.assertEqual(job.audit_attachment_id,receipt); self.assertEqual(before,self._economics())
        self.env['company.document.cutover'].action_restore_saved_moves()
        self.assertEqual(len(job.purchase_archive_ids),1)
