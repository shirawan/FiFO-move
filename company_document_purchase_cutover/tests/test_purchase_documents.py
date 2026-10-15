from datetime import datetime
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from odoo.addons.company_document_cutover.tests.test_documents import TestDocumentMigration


@tagged('post_install','-at_install')
class TestPurchaseDocuments(FinancialCutoverCase):
    _job=TestDocumentMigration._job
    _match=TestDocumentMigration._match
    _run_job=TestDocumentMigration._run_job

    def _products(self):
        return tuple(self.env['product.product'].with_company(c).create({
            'name':'Synthetic received goods','company_id':c.id,'type':'consu','is_storable':True,
            'purchase_method':'receive','standard_price':10,'taxes_id':[Command.clear()],
            'supplier_taxes_id':[Command.clear()]}) for c in (self.source,self.target))

    def _po(self,product,qty=10,confirm=False,reference=False):
        order=self.env['purchase.order'].with_company(self.source).create({
            'company_id':self.source.id,'partner_id':self.vendor.id,'date_order':datetime(2024,6,30),
            'partner_ref':reference,'order_line':[Command.create({'product_id':product.id,
                'product_qty':qty,'product_uom_id':product.uom_id.id,'price_unit':20,'discount':5,
                'date_planned':datetime(2024,6,30),'tax_ids':[Command.clear()]})]})
        if confirm:
            order.button_confirm();order.write({'date_order':datetime(2024,6,30),'date_approve':datetime(2024,6,30)})
        return order

    def _purchase_job(self,old,new,**options):
        options.setdefault('purchase_history_mode','native')
        job=self._job(include_accounting=False,include_purchase=True,old_operations_frozen=True,**options)
        self._match(job,old,new)
        return job

    def _receive(self,po,qty,lot=False,owner=False,package=False):
        move=po.picking_ids.move_ids.filtered(lambda m:m.state not in ('done','cancel'))[:1]
        move.quantity=qty
        if lot:
            move.move_line_ids.lot_id=lot
        if owner:
            move.move_line_ids.owner_id=owner
        if package:
            move.move_line_ids.result_package_id=package
        move.picked=True
        move._action_done(cancel_backorder=False)
        return po.order_line.move_ids.filtered(lambda m:m.state=='done')

    def _bill(self,po,qty):
        action=po.with_company(self.source).action_create_invoice()
        move=self.env['account.move'].browse(action.get('res_id')) if action.get('res_id') else po.invoice_ids.filtered(lambda m:m.state=='draft')[-1:]
        move.invoice_line_ids.quantity=qty
        move.invoice_line_ids.account_id=self.accounts[self.source.id]['expense']
        move.invoice_date=self.cutoff;move.date=self.cutoff;move.action_post()
        return move

    def test_rfqs_confirmed_cancelled_preserve_native_commitments_once(self):
        old,new=self._products()
        draft=self._po(old,3);confirmed=self._po(old,10,True);cancelled=self._po(old,2);cancelled.button_cancel()
        sent=self._po(old,4);sent.write({'state':'sent'})
        before={o.id:o.read(['state','date_order','date_approve','write_date','order_line'])[0] for o in draft|confirmed|cancelled|sent}
        suppliers=self.env['product.supplierinfo'].search([('product_tmpl_id','=',old.product_tmpl_id.id)]).read()
        job=self._purchase_job(old,new);self._run_job(job)
        orders=self.env['purchase.order'].search([('company_id','=',self.target.id)])
        self.assertEqual(len(orders),4)
        for source in draft|confirmed|cancelled|sent:
            dest=self.env['purchase.order'].browse(job._stored_link(source)['target'])
            self.assertEqual(dest.state,source.state);self.assertEqual(dest.order_line.product_qty,source.order_line.product_qty)
            self.assertEqual(dest.order_line.price_unit,20);self.assertEqual(dest.order_line.discount,5)
            self.assertEqual(dest.date_order,source.date_order);self.assertEqual(dest.date_approve,source.date_approve)
            self.assertEqual(dest.order_line.qty_received,0);self.assertEqual(dest.order_line.qty_invoiced,0)
        active=orders.filtered(lambda o:o.state=='purchase')
        self.assertEqual(sum(active.picking_ids.move_ids.mapped('product_uom_qty')),10)
        self.assertFalse(active.picking_ids.filtered(lambda p:p.state=='done'))
        job.action_execute();again=self._purchase_job(old,new);self._run_job(again)
        self.assertEqual(self.env['purchase.order'].search_count([('company_id','=',self.target.id)]),4)
        self.assertEqual(before,{o.id:o.read(['state','date_order','date_approve','write_date','order_line'])[0] for o in draft|confirmed|cancelled|sent})
        self.assertEqual(suppliers,self.env['product.supplierinfo'].search([('product_tmpl_id','=',old.product_tmpl_id.id)]).read())

    def test_partial_receipt_bill_preview_blocks_without_fake_quantities(self):
        old,new=self._products();po=self._po(old,10,True);self._receive(po,4);self._bill(po,2)
        self.assertEqual(po.order_line.qty_received,4);self.assertEqual(po.order_line.qty_invoiced,2)
        job=self._purchase_job(old,new);job.action_review()
        self.assertEqual(job.state,'blocked',job.report);self.assertIn('stock/bill-history mappings',job.report)
        self.assertIn('ordered less net received 6.0',job.report);self.assertIn('ordered less billed 8.0',job.report)
        self.assertFalse(self.env['purchase.order'].search([('company_id','=',self.target.id)]))

    def test_multiple_bills_return_completed_history_explicitly_blocked(self):
        old,new=self._products();po=self._po(old,10,True);done=self._receive(po,10)
        self._bill(po,4);self._bill(po,6)
        self.assertEqual(len(po.invoice_ids),2);self.assertEqual(po.order_line.qty_invoiced,10)
        returned=done[:1].copy({'origin_returned_move_id':done[0].id,'product_uom_qty':1,
            'quantity':1,'location_id':done[0].location_dest_id.id,'location_dest_id':done[0].location_id.id,
            'picking_id':False,'state':'draft','purchase_line_id':po.order_line.id,'to_refund':True})
        returned._action_confirm();returned.picked=True;returned._action_done()
        self.assertEqual(po.order_line.qty_received,9)
        job=self._purchase_job(old,new);job.action_review();self.assertEqual(job.state,'blocked',job.report)
        self.assertIn('receipts/returns or bill links',job.report)

    def test_shared_product_confirmation_blocked_to_protect_source(self):
        product=self.env['product.product'].create({'name':'Shared product','type':'consu','purchase_method':'receive'})
        self._po(product,2,True);job=self._purchase_job(product,product);job.action_review()
        self.assertEqual(job.state,'blocked');self.assertIn('destination-specific products',job.report)

    def test_old_and_new_purchase_workflows_cannot_duplicate_commitment(self):
        old,new=self._products();po=self._po(old,10,True)
        job=self._purchase_job(old,new);self._run_job(job)
        # Source cancellation is a synthetic user operation, never performed by mover.
        po.button_cancel()
        batch=self._batch();batch.write({'include_financial':False,'include_purchase_history':True})
        batch.action_review();self.assertEqual(batch.state,'preview',batch.check_report);batch.action_apply()
        history=batch.purchase_history_ids
        self.assertTrue(history)
        self.assertIn('document mover',' '.join(history._replacement_destination_issues(self.vendor)))
        with self.assertRaisesRegex(UserError,'document mover'):history.action_prepare_draft()
        self.assertEqual(self.env['purchase.order'].search_count([('company_id','=',self.target.id)]),1)

    def test_bill_po_links_blocked_even_in_accounting_only_job(self):
        old,new=self._products();po=self._po(old,10,True);self._receive(po,4);self._bill(po,2)
        job=self._job();self._match(job,old,new);job.action_review()
        self.assertEqual(job.state,'blocked');self.assertIn('Bill/PO line links',job.report)

    def _history_fixture(self, returned=False, tracked=False, owned=False, packaged=False):
        old,new=self._products()
        lots=[]
        if tracked:
            (old|new).tracking='serial' if tracked=='serial' else 'lot'
            lots=[self.env['stock.lot'].create({'name':'LOT-ORIGINAL-123','product_id':product.id,'company_id':product.company_id.id}) for product in (old,new)]
        packages=[self.env['stock.package'].create({'name':'PACK-OLD'}),self.env['stock.package'].create({'name':'PACK-NEW'})] if packaged else []
        source=self._po(old,10,True)
        target=self.env['purchase.order'].with_company(self.target).create({
            'company_id':self.target.id,'partner_id':self.vendor.id,'date_order':source.date_order,
            'order_line':[Command.create({'product_id':new.id,'name':source.order_line.name,
                'product_qty':10,'product_uom_id':new.uom_id.id,'price_unit':20,'discount':5,
                'date_planned':source.order_line.date_planned,'tax_ids':[Command.clear()]})]})
        target.button_confirm();target.write({'date_order':source.date_order,'date_approve':source.date_approve})
        a=self._receive(source,1 if tracked=='serial' else 4,lots[0] if lots else False,self.vendor if owned else False,packages[0] if packages else False);b=self._receive(target,1 if tracked=='serial' else 4,lots[1] if lots else False,self.vendor if owned else False,packages[1] if packages else False)
        a.date=datetime(2024,6,29);b.date=a.date
        pairs=[(a,b)]
        if returned:
            results=[]
            for move in (a,b):
                ret=move.copy({'origin_returned_move_id':move.id,'product_uom_qty':1,'quantity':1,
                    'location_id':move.location_dest_id.id,'location_dest_id':move.location_id.id,
                    'picking_id':False,'state':'draft','purchase_line_id':move.purchase_line_id.id,'to_refund':True})
                ret._action_confirm();ret.picked=True;ret._action_done();ret.date=datetime(2024,6,30)
                results.append(ret)
            pairs.append(tuple(results))
        job=self._purchase_job(old,new)
        self._match(job,source,target);self._match(job,source.order_line,target.order_line)
        for a,b in pairs:self._match(job,a,b)
        if tracked:
            self._match(job,*lots)
        if packaged:
            self._match(job,*packages)
        if tracked or owned or packaged:
            for old_location,new_location in [(a.location_id,b.location_id),(a.location_dest_id,b.location_dest_id)]:
                self._match(job,old_location,new_location)
        return source,target,job

    def test_existing_receipts_returns_reused_without_stock_or_po_creation(self):
        source,target,job=self._history_fixture(returned=True)
        before=self.env['stock.move'].search([]).read(['state','quantity','product_uom_qty','write_date'])
        job.action_review()
        self.assertIn('ordered less net received 7.0',job.report)
        self.assertIn('Open receipt commitments: 6.0',job.report)
        self.assertIn('Quantity available to bill now: 3.0',job.report)
        self._run_job(job)
        self.assertEqual(target.order_line.qty_received,3)
        self.assertEqual(target.order_line.qty_invoiced,0)
        self.assertEqual(before,self.env['stock.move'].search([]).read(['state','quantity','product_uom_qty','write_date']))
        self.assertEqual(self.env['purchase.order'].search_count([('company_id','=',self.target.id)]),1)
        job.action_execute()
        self.assertEqual(job._stored_link(source)['target'],target.id)

    def test_historical_po_copied_bills_link_natively_and_retry_once(self):
        source,target,job=self._history_fixture()
        bill=self._bill(source,2)
        job.include_accounting=True
        self._run_job(job)
        link=job._stored_link(bill)
        dest=self.env['account.move'].browse(link['target'])
        self.assertEqual(dest.invoice_line_ids.purchase_line_id,target.order_line)
        self.assertEqual(target.order_line.qty_invoiced,2)
        self.assertEqual(target.order_line.qty_received,4)
        self.assertEqual(target.order_line.qty_to_invoice,2)
        job.action_execute()
        self.assertEqual(len(target.invoice_ids),1)
        self.assertEqual(source.order_line.qty_invoiced,2)

    def test_history_mismatch_and_unmarked_bill_duplicate_block_together(self):
        source,target,job=self._history_fixture()
        target.order_line.price_unit=999
        target.order_line.move_ids.filtered(lambda m:m.state=='done').date=datetime(2024,6,28)
        job.action_review()
        self.assertEqual(job.state,'blocked')
        self.assertIn('terms or native received quantity',job.report)
        self.assertIn('Receipt/return evidence differs',job.report)

    def test_history_receipt_edit_invalidates_review(self):
        source,target,job=self._history_fixture()
        job.action_review();self.assertEqual(job.state,'ready',job.report)
        target.order_line.move_ids.filtered(lambda m:m.state=='done').date=datetime(2024,6,28)
        job.action_execute();self.assertEqual(job.state,'failed')
        self.assertFalse(self.env['ir.config_parameter'].sudo().get_param(job._key(source)))

    def test_history_multiple_bills_return_and_credit_native_quantities(self):
        source,target,job=self._history_fixture(returned=True)
        bills=self._bill(source,1)|self._bill(source,2)
        credit=self.env['account.move'].with_company(self.source).create({
            'company_id':self.source.id,'journal_id':bills[0].journal_id.id,'move_type':'in_refund',
            'partner_id':self.vendor.id,'invoice_date':self.cutoff,'date':self.cutoff,
            'invoice_line_ids':[Command.create({'name':source.order_line.name,'product_id':source.order_line.product_id.id,
                'purchase_line_id':source.order_line.id,'quantity':1,'price_unit':20,'discount':5,
                'account_id':self.accounts[self.source.id]['expense'].id,'tax_ids':[Command.clear()]})]})
        credit.action_post();job.include_accounting=True
        self._run_job(job)
        self.assertEqual(len(target.invoice_ids),3)
        self.assertEqual(target.order_line.qty_received,3)
        self.assertEqual(target.order_line.qty_invoiced,2)
        self.assertEqual(target.order_line.qty_to_invoice,1)
        for bill in bills|credit:
            dest=self.env['account.move'].browse(job._stored_link(bill)['target'])
            self.assertEqual(dest.invoice_line_ids.purchase_line_id,target.order_line)
        evidence=job._stored_link(source)['receipt_matches']
        self.assertEqual(len(evidence),2)
        self.assertEqual({x['target'] for x in evidence},set(target.order_line.move_ids.filtered(lambda m:m.state=='done').ids))

    def test_history_existing_unmarked_bill_blocks_copy(self):
        source,target,job=self._history_fixture()
        self._bill(source,2);job.include_accounting=True
        target.with_company(self.target).action_create_invoice()
        job.action_review()
        self.assertEqual(job.state,'blocked')
        self.assertIn('without verified migration evidence',job.report)
        self.assertEqual(len(target.invoice_ids),1)

    def test_history_fault_after_bills_rolls_back_links_and_quantities(self):
        from unittest.mock import patch
        source,target,job=self._history_fixture();self._bill(source,2);job.include_accounting=True
        job.action_review();self.assertEqual(job.state,'ready',job.report)
        original=type(job)._history_matches
        def fail(record,order,final=False):
            if final:raise UserError('Synthetic failure after bill posting')
            return original(record,order,final=final)
        with patch.object(type(job),'_history_matches',fail):job.action_execute()
        self.assertEqual(job.state,'failed');self.assertEqual(target.order_line.qty_invoiced,0)
        self.assertFalse(target.invoice_ids)
        self._run_job(job);self.assertEqual(target.order_line.qty_invoiced,2)

    def test_history_after_cutover_and_tracked_goods_block(self):
        source,target,job=self._history_fixture()
        source.order_line.move_ids.filtered(lambda m:m.state=='done').date=datetime(2024,7,1)
        target.order_line.product_id.tracking='lot'
        job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('after the cutover',job.report)
        source.order_line.move_ids.filtered(lambda m:m.state=='done').date=datetime(2024,6,29)
        job.action_review();self.assertIn('lots/serials',job.report)

    def test_history_completed_receipt_edit_detected_by_durable_evidence(self):
        source,target,job=self._history_fixture();self._run_job(job)
        target.order_line.move_ids.filtered(lambda m:m.state=='done').date=datetime(2024,6,28)
        with self.assertRaisesRegex(UserError,'evidence changed'):job.action_execute()

    def test_history_value_mismatch_without_valuation_journal_blocks(self):
        source,target,job=self._history_fixture()
        old=source.order_line.move_ids.filtered(lambda m:m.state=='done')
        new=target.order_line.move_ids.filtered(lambda m:m.state=='done')
        self.assertFalse(old.account_move_id|new.account_move_id)
        new.value=old.value+5
        job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('Receipt/return evidence differs',job.report)

    def test_history_native_bill_value_change_rolls_back(self):
        from unittest.mock import patch
        source,target,job=self._history_fixture();self._bill(source,2);job.include_accounting=True
        receipt=target.order_line.move_ids.filtered(lambda m:m.state=='done');before=receipt.value
        job.action_review();self.assertEqual(job.state,'ready',job.report)
        original=type(self.env['account.move']).action_post
        def change_after_native(records):
            result=original(records)
            if records.company_id==self.target:receipt.value+=5
            return result
        with patch.object(type(self.env['account.move']),'action_post',change_after_native):job.action_execute()
        self.assertEqual(job.state,'failed');self.assertIn('stock values',job.report)
        self.assertEqual(receipt.value,before);self.assertFalse(target.invoice_ids)

    def test_pending_return_is_not_silently_discarded(self):
        source,target,job=self._history_fixture()
        receipt=source.order_line.move_ids.filtered(lambda m:m.state=='done')
        pending=receipt.copy({'origin_returned_move_id':receipt.id,'state':'draft','picking_id':False,
            'location_id':receipt.location_dest_id.id,'location_dest_id':receipt.location_id.id,'product_uom_qty':1})
        pending._action_confirm()
        job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('pending supplier returns',job.report)

    def test_foreign_po_valuation_uses_company_currency_rounding(self):
        source,target,job=self._history_fixture()
        currency=self.env['res.currency'].create({'name':'HST','symbol':'H','rounding':1.0,'active':True})
        (source|target).currency_id=currency
        old=source.order_line.move_ids.filtered(lambda m:m.state=='done')
        target.order_line.move_ids.filtered(lambda m:m.state=='done').value=old.value+0.05
        self.assertTrue(currency.is_zero(0.05))
        self.assertFalse(self.target.currency_id.is_zero(0.05))
        job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('Receipt/return evidence differs',job.report)

    def test_one_destination_history_cannot_represent_two_source_orders(self):
        source,target,job=self._history_fixture()
        other=self._po(source.order_line.product_id,10,True)
        self._receive(other,4)
        self._match(job,other,target);self._match(job,other.order_line,target.order_line)
        self._match(job,other.order_line.move_ids.filtered(lambda m:m.state=='done'),target.order_line.move_ids.filtered(lambda m:m.state=='done'))
        job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('cannot represent multiple',job.report)

    def test_fully_received_billed_locked_order_preserved(self):
        source,target,job=self._history_fixture()
        old_done=source.order_line.move_ids.filtered(lambda m:m.state=='done')
        new_done=target.order_line.move_ids.filtered(lambda m:m.state=='done')
        old_more=self._receive(source,6)-old_done;new_more=self._receive(target,6)-new_done
        (old_more|new_more).date=datetime(2024,6,30)
        self._match(job,old_more,new_more)
        self._bill(source,10);job.include_accounting=True
        source.button_lock();job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('state differ',job.report)
        target.button_lock();self._run_job(job)
        self.assertTrue(target.locked)
        self.assertEqual((target.order_line.qty_received,target.order_line.qty_invoiced,target.order_line.qty_to_invoice),(10,10,0))
        self.assertFalse(target.order_line.move_ids.filtered(lambda m:m.state not in ('done','cancel')))

    def test_untouched_locked_order_native_lock_and_durable_check(self):
        old,new=self._products();source=self._po(old,10,True);source.button_lock()
        job=self._purchase_job(old,new);self._run_job(job)
        target=self.env['purchase.order'].browse(job._stored_link(source)['target'])
        self.assertTrue(target.locked)
        target.button_unlock()
        with self.assertRaisesRegex(UserError,'lock state changed'):job.action_execute()

    def test_source_lock_change_requires_fresh_review(self):
        old,new=self._products();source=self._po(old,10,True)
        job=self._purchase_job(old,new);job.action_review();self.assertEqual(job.state,'ready')
        source.button_lock();job.action_execute()
        self.assertEqual(job.state,'failed');self.assertIn('changed',job.report)
        self.assertFalse(self.env['purchase.order'].search([('company_id','=',self.target.id)]))

    def test_billing_policy_mismatch_blocked_before_copying_bill(self):
        source,target,job=self._history_fixture();self._bill(source,2);job.include_accounting=True
        target.order_line.product_id.purchase_method='purchase'
        job.action_review();self.assertEqual(job.state,'blocked');self.assertIn('billing policy differs',job.report)
        job.action_execute();self.assertEqual(job.state,'failed');self.assertFalse(target.invoice_ids)

    def test_wrong_product_pending_receipt_blocked(self):
        source,target,job=self._history_fixture()
        wrong=self.env['product.product'].with_company(self.target).create({'name':'Wrong goods','company_id':self.target.id,'type':'consu','is_storable':True})
        target.order_line.move_ids.filtered(lambda m:m.state not in ('done','cancel')).product_id=wrong
        job.action_review();self.assertEqual(job.state,'blocked');self.assertIn('pending receipt product',job.report)
        job.action_execute();self.assertEqual(job.state,'failed')
        self.assertFalse(self.env['ir.config_parameter'].sudo().get_param(job._key(source)))

    def test_billing_policy_change_after_review_is_detected(self):
        old,new=self._products();self._po(old,10,True);job=self._purchase_job(old,new)
        job.action_review();self.assertEqual(job.state,'ready')
        (old|new).purchase_method='purchase'
        job.action_execute();self.assertEqual(job.state,'failed');self.assertIn('changed',job.report)
        self.assertFalse(self.env['purchase.order'].search([('company_id','=',self.target.id)]))

    def test_legacy_po_billing_policy_change_cannot_bypass_done_check(self):
        import json
        source,target,job=self._history_fixture();self._bill(source,2);job.include_accounting=True
        self._run_job(job)
        parameter=self.env['ir.config_parameter'].sudo()
        saved=json.loads(parameter.get_param(job._key(source)))
        saved.pop('source_details');saved.pop('target_details')
        parameter.set_param(job._key(source),json.dumps(saved))
        target.order_line.product_id.purchase_method='purchase'
        with self.assertRaisesRegex(UserError,'billing policy'):job.action_execute()
        self.assertEqual(len(target.invoice_ids),1)

    def test_completed_untouched_order_rechecks_pending_product(self):
        old,new=self._products();source=self._po(old,10,True);job=self._purchase_job(old,new)
        self._run_job(job);target=self.env['purchase.order'].browse(job._stored_link(source)['target'])
        wrong=self.env['product.product'].with_company(self.target).create({'name':'Wrong pending goods','company_id':self.target.id,'type':'consu'})
        target.order_line.move_ids.product_id=wrong
        with self.assertRaisesRegex(UserError,'pending receipt'):job.action_execute()

    def test_purchase_steps_resume_without_duplicate_commitments(self):
        old,new=self._products();sources=self._po(old,3)|self._po(old,4)|self._po(old,5)
        job=self._purchase_job(old,new,batch_size=1)
        for expected in (1,2,3):
            job.action_review();self.assertEqual(job.state,'ready',job.report)
            job.action_execute();self.assertEqual(job.state,'done' if expected==3 else 'partial',job.report)
            self.assertEqual(self.env['purchase.order'].search_count([('company_id','=',self.target.id)]),expected)
        for order in sources:self.assertTrue(job._stored_link(order))

    def test_existing_bill_adopted_into_matched_history_without_copy(self):
        source,target,job=self._history_fixture();bill=self._bill(source,2);job.include_accounting=True
        dest,_,_=job._copy_move(bill);self._match(job,bill,dest)
        self._run_job(job)
        self.assertEqual(target.order_line.qty_invoiced,2)
        self.assertEqual(len(target.invoice_ids),1);self.assertEqual(job._stored_link(bill)['target'],dest.id)

    def test_existing_lot_receipts_reused_with_exact_native_details(self):
        source,target,job=self._history_fixture(tracked=True)
        before=self.env['stock.quant'].search([('company_id','=',self.target.id)]).read(['quantity','lot_id','location_id','write_date'])
        self._run_job(job)
        self.assertEqual(target.order_line.qty_received,4)
        self.assertEqual(before,self.env['stock.quant'].search([('company_id','=',self.target.id)]).read(['quantity','lot_id','location_id','write_date']))
        job.action_execute()

    def test_lot_identity_mismatch_blocks_reuse(self):
        source,target,job=self._history_fixture(tracked=True)
        target.order_line.move_ids.move_line_ids.lot_id.name='DIFFERENT-LOT'
        job.action_review();self.assertEqual(job.state,'blocked');self.assertIn('lot/serial number',job.report)

    def test_more_than_200_purchase_orders_moved_in_steps(self):
        old,new=self._products()
        for _ in range(201):self._po(old,1)
        job=self._purchase_job(old,new,batch_size=1000);self._run_job(job)
        self.assertEqual(self.env['purchase.order'].search_count([('company_id','=',self.target.id)]),201)

    def test_serial_receipt_reused_without_moving_inventory(self):
        source,target,job=self._history_fixture(tracked='serial')
        self._run_job(job);self.assertEqual(target.order_line.qty_received,1)
        self.assertEqual(target.order_line.move_ids.move_line_ids.lot_id.name,'LOT-ORIGINAL-123')

    def test_owner_and_package_receipt_history_reused(self):
        source,target,job=self._history_fixture(owned=True,packaged=True)
        before=self.env['stock.quant'].search([('company_id','=',self.target.id)]).read(['quantity','owner_id','package_id','write_date'])
        self._run_job(job)
        self.assertEqual(before,self.env['stock.quant'].search([('company_id','=',self.target.id)]).read(['quantity','owner_id','package_id','write_date']))
        self.assertEqual(target.order_line.move_ids.move_line_ids.owner_id,self.vendor)

    def test_accounting_only_incremental_verifies_existing_linked_bill(self):
        source,target,job=self._history_fixture();self._bill(source,2);job.include_accounting=True;self._run_job(job)
        TestDocumentMigration._document(self,'out_invoice',50)
        again=self._job(allow_incremental=True);self._match(again,source.order_line.product_id,target.order_line.product_id)
        self._run_job(again)
        self.assertEqual(len(target.invoice_ids),1);self.assertEqual(target.order_line.qty_invoiced,2)
