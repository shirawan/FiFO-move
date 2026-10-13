from datetime import date
from psycopg2.errors import UniqueViolation
from unittest.mock import patch
from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged, new_test_user
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from odoo.addons.company_document_cutover.models.migration import SOURCE


@tagged('post_install', '-at_install')
class TestDocumentMigration(FinancialCutoverCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env=cls.env(context={**cls.env.context,'no_reset_password':True,'mail_notify_force_send':False})
        for company in (cls.source, cls.target):
            cls.journals[company.id]['bank'] = cls.env['account.journal'].with_company(company).create({
                'name': 'Manual bank evidence', 'code': 'BNK', 'type': 'bank', 'company_id': company.id,
                'default_account_id': cls.accounts[company.id]['bank'].id})
            journal = cls.journals[company.id]['bank']
            (journal.inbound_payment_method_line_ids | journal.outbound_payment_method_line_ids).write({
                'payment_account_id': cls.accounts[company.id]['bank'].id})

    def _job(self, **options):
        job = self.env['company.document.cutover'].create({
            'source_company_ids': [Command.set(self.source.ids)], 'target_company_id': self.target.id,
            'cutover_date': self.cutoff, 'same_legal_entity': True, 'existing_data_reviewed': True, **options})
        pairs = [(a, self.accounts[self.target.id][n]) for n,a in self.accounts[self.source.id].items()]
        pairs += [(j, self.journals[self.target.id][n]) for n,j in self.journals[self.source.id].items()]
        pairs += [(self.customer, self.customer), (self.vendor,self.vendor)]
        for old, new in pairs:
            self._match(job, old, new)
        return job

    def _match(self, job, old, new):
        return self.env['company.document.mapping'].create({'job_id': job.id,
            'source_record': '%s,%s' % (old._name,old.id), 'target_record': '%s,%s' % (new._name,new.id)})

    def _pay(self, invoice, amount):
        kind = 'inbound' if invoice.move_type == 'out_invoice' else 'outbound'
        journal = self.journals[self.source.id]['bank']
        method = (journal.inbound_payment_method_line_ids if kind=='inbound' else journal.outbound_payment_method_line_ids).filtered(lambda m: m.code == 'manual')
        payment = self.env['account.payment'].with_company(self.source).create({
            'company_id': self.source.id, 'journal_id': journal.id, 'date': self.cutoff,
            'amount': amount, 'currency_id': invoice.currency_id.id, 'partner_id': invoice.partner_id.id,
            'payment_type': kind, 'partner_type': 'customer' if kind=='inbound' else 'supplier',
            'payment_method_line_id': method.id, 'destination_account_id': self.accounts[self.source.id]['receivable' if kind=='inbound' else 'payable'].id,
            'memo': 'Synthetic recorded payment only'})
        payment.write({'state': 'paid'})
        (invoice.line_ids | payment.move_id.line_ids).filtered(lambda l: l.account_id.account_type in ('asset_receivable','liability_payable')).reconcile()
        payment.invoice_ids = [Command.set(invoice.ids)]
        return payment

    def _document(self, kind, amount, posted=True):
        sale=kind.startswith('out_')
        move=self.env['account.move'].with_company(self.source).create({
            'move_type':kind,'company_id':self.source.id,'journal_id':self.journals[self.source.id]['sale' if sale else 'purchase'].id,
            'partner_id':(self.customer if sale else self.vendor).id,'invoice_date':self.cutoff,'date':self.cutoff,
            'invoice_date_due':date(2024,7,31),'invoice_line_ids':[Command.create({'name':'Synthetic document',
                'quantity':1,'price_unit':amount,'account_id':self.accounts[self.source.id]['revenue' if sale else 'expense'].id,
                'tax_ids':[Command.clear()]})]})
        move.action_post()
        if not posted:
            move.button_draft()
        return move

    def _run_job(self, job):
        job.action_review()
        self.assertEqual(job.state,'ready',job.report)
        job.action_execute()
        self.assertEqual(job.state,'done',job.report)
        return job

    def _target_move(self, job, source):
        return self.env['account.move'].browse(job._stored_link(source)['target'])

    def test_original_documents_payments_credits_trial_balance_no_opening(self):
        invoices = [self._document('out_invoice', a) for a in (100,200,300)]
        bills = [self._document('in_invoice', a) for a in (80,120,160)]
        payments = self.env['account.payment']
        for doc, amount in ((invoices[1],50),(invoices[2],300),(bills[1],40),(bills[2],160)):
            payments |= self._pay(doc,amount)
        credit = self._document('out_refund',30)
        vendor_credit = self._document('in_refund',20)
        (credit.line_ids | invoices[0].line_ids).filtered(lambda l:l.account_id.account_type=='asset_receivable').reconcile()
        (vendor_credit.line_ids | bills[0].line_ids).filtered(lambda l:l.account_id.account_type=='liability_payable').reconcile()
        drafts = [self._document(k, a,False) for k,a in [('out_invoice',40),('in_invoice',50),('out_refund',5),('in_refund',6)]]
        self._entry(self.source,[('expense',20),('equity',-20)],when=date(2023,12,31))
        original = self.env['account.move'].search([('company_id','=',self.source.id)])
        before = {m.id: self._job_fingerprint(m) for m in original}
        job = self._job()
        self._run_job(job)
        for source in original:
            dest = self._target_move(job,source)
            self.assertEqual(dest.move_type,source.move_type)
            self.assertEqual(dest.state,source.state)
            self.assertEqual(dest.amount_total,source.amount_total)
            self.assertEqual(dest.amount_residual,source.amount_residual)
            self.assertEqual(dest.invoice_date,source.invoice_date)
            self.assertEqual(dest.invoice_date_due,source.invoice_date_due)
            self.assertEqual(dest.currency_id,source.currency_id)
        self.assertEqual([self._target_move(job,m).amount_residual for m in invoices],[70,150,0])
        self.assertEqual([self._target_move(job,m).amount_residual for m in bills],[60,80,0])
        target_pay = self.env['account.payment'].search([('company_id','=',self.target.id)])
        self.assertEqual(len(target_pay),4)
        self.assertTrue(all(p.payment_method_id.code=='manual' for p in target_pay))
        self.assertTrue(all(p.move_id.state=='posted' for p in target_pay))
        for n,a in self.accounts[self.source.id].items():
            old=sum(original.filtered(lambda m:m.state=='posted').line_ids.filtered(lambda l:l.account_id==a).mapped('balance'))
            new=sum(self.env['account.move.line'].search([('company_id','=',self.target.id),('parent_state','=','posted'),('account_id','=',self.accounts[self.target.id][n].id)]).mapped('balance'))
            self.assertAlmostEqual(old,new,msg=n)
        self.assertEqual(before,{m.id:self._job_fingerprint(m) for m in original})
        count = self.env['account.move'].search_count([('company_id','=',self.target.id)])
        job.action_execute()
        again=self._job();self._run_job(again)
        self.assertEqual(self.env['account.move'].search_count([('company_id','=',self.target.id)]),count)
        self.assertFalse(self.env['account.move'].search([('company_id','=',self.target.id),('financial_cutover_id','!=',False)]))
        balance_job=self._batch();balance_job.action_match()
        self.assertEqual(balance_job.check_status,'blocked')

    def _job_fingerprint(self, move):
        return self.env['company.document.cutover']._fingerprint(move)

    def test_multiple_taxes_discounts_quantities_native(self):
        job=self._job()
        taxes={}
        for company in (self.source,self.target):
            group=self.env['account.tax.group'].create({'name':'Synthetic tax group','company_id':company.id})
            taxes[company.id]=self.env['account.tax'].with_company(company).create([
                {'name':'Synthetic 10','amount':10,'amount_type':'percent','type_tax_use':'sale','company_id':company.id,'tax_group_id':group.id},
                {'name':'Synthetic 5','amount':5,'amount_type':'percent','type_tax_use':'sale','company_id':company.id,'tax_group_id':group.id}])
        for a,b in zip(taxes[self.source.id],taxes[self.target.id]):self._match(job,a,b)
        move=self.env['account.move'].with_company(self.source).create({
            'company_id':self.source.id,'journal_id':self.journals[self.source.id]['sale'].id,'move_type':'out_invoice',
            'partner_id':self.customer.id,'invoice_date':self.cutoff,'date':self.cutoff,
            'invoice_line_ids':[Command.create({'name':'Taxed goods','quantity':2,'price_unit':100,'discount':10,
                'account_id':self.accounts[self.source.id]['revenue'].id,'tax_ids':[Command.set(taxes[self.source.id].ids)]})]})
        move.action_post();self._run_job(job)
        dest=self._target_move(job,move)
        self.assertEqual(dest.amount_untaxed,180);self.assertEqual(dest.amount_tax,27);self.assertEqual(dest.amount_total,207)
        self.assertEqual(dest.invoice_line_ids.quantity,2);self.assertEqual(dest.invoice_line_ids.discount,10)

    def test_foreign_currency_reproduces_native_rates(self):
        currency=self.env.ref('base.EUR')
        if currency == self.source.currency_id:currency=self.env.ref('base.USD')
        currency.active=True
        for company in (self.source,self.target):
            self.env['res.currency.rate'].create({'company_id':company.id,'currency_id':currency.id,'name':self.cutoff,'rate':2})
        move=self._invoice(currency=currency,amount=120)
        job=self._job();self._run_job(job)
        dest=self._target_move(job,move)
        self.assertEqual(dest.currency_id,currency);self.assertEqual(dest.amount_residual,120)
        self.assertEqual(sorted(move.line_ids.mapped('balance')),sorted(dest.line_ids.mapped('balance')))

    def test_foreign_rate_mismatch_rolls_back_without_forcing(self):
        currency=self.env.ref('base.EUR')
        if currency==self.source.currency_id:currency=self.env.ref('base.USD')
        currency.active=True
        for company, rate in ((self.source,2),(self.target,3)):
            self.env['res.currency.rate'].create({'company_id':company.id,'currency_id':currency.id,'name':self.cutoff,'rate':rate})
        self._invoice(currency=currency,amount=120)
        job=self._job();job.action_review();self.assertEqual(job.state,'ready',job.report)
        job.action_execute();self.assertEqual(job.state,'failed')
        self.assertFalse(self.env['account.move'].search([('company_id','=',self.target.id)]))

    def test_missing_mappings_and_legal_confirmation_report_together(self):
        self._invoice()
        job=self._job(same_legal_entity=False)
        job.mapping_ids.filtered(lambda r:r.model_name=='account.journal').unlink()
        job.action_review();self.assertEqual(job.state,'blocked')
        self.assertIn('legal entity',job.report);self.assertIn('explicit match',job.report)

    def test_locked_period_blocked_before_post(self):
        self._invoice();job=self._job()
        self.target.hard_lock_date=self.cutoff
        job.action_review();self.assertEqual(job.state,'blocked',job.report);self.assertIn('locked',job.report)

    def test_destination_trading_preserved_and_change_invalidates_review(self):
        self._invoice();trade=self._entry(self.target,[('bank',7),('equity',-7)])
        job=self._job();job.action_review();self.assertEqual(job.state,'ready',job.report)
        self._entry(self.target,[('bank',9),('equity',-9)])
        job.action_execute();self.assertEqual(job.state,'failed');self.assertIn('changed',job.report)
        self._run_job(job)
        self.assertEqual(trade.state,'posted');self.assertEqual(sum(trade.line_ids.filtered(lambda l:l.account_id==self.accounts[self.target.id]['bank']).mapped('balance')),7)

    def test_source_change_invalidates_review(self):
        self._invoice();job=self._job();job.action_review()
        self._invoice(amount=25);job.action_execute()
        self.assertEqual(job.state,'failed');self.assertFalse(self.env['account.move'].search([('company_id','=',self.target.id)]))

    def test_controlled_interruption_rolls_back_native_records(self):
        self._invoice();self._invoice(amount=25);job=self._job();job.action_review()
        original=type(job)._copy_move;counter=[]
        def interrupted(record,source):
            result=original(record,source);counter.append(source.id)
            if len(counter)==2:raise UserError('Synthetic interruption after native creation')
            return result
        with patch.object(type(job),'_copy_move',interrupted):job.action_execute()
        self.assertEqual(job.state,'failed');self.assertIn('Synthetic interruption',job.report)
        self.assertFalse(self.env['account.move'].search([('company_id','=',self.target.id)]))
        self._run_job(job);self.assertEqual(self.env['account.move'].search_count([('company_id','=',self.target.id)]),2)

    def test_source_branches_must_be_explicit_and_authorized(self):
        branch=self.env['res.company'].create({'name':'Synthetic branch','parent_id':self.source.id})
        user=new_test_user(self.env,login='document_scope',groups='base.group_system,account.group_account_manager',company_id=self.source.id,company_ids=[Command.set((self.source|self.target).ids)])
        with self.assertRaises(AccessError):
            self.env['company.document.cutover'].with_user(user).with_context(allowed_company_ids=(self.source|self.target).ids).create({
                'source_company_ids':[Command.set((self.source|branch).ids)],'target_company_id':self.target.id,'cutover_date':self.cutoff})

    def test_forged_audit_state_denied(self):
        with self.assertRaises(AccessError):self._job(state='done')
        job=self._job()
        with self.assertRaises(AccessError):job.write({'state':'done'})

    def test_edited_native_target_blocks_repeat_and_new_job(self):
        move=self._invoice();job=self._job();self._run_job(job)
        self._target_move(job,move).ref='Edited after migration'
        with self.assertRaisesRegex(UserError,'evidence changed'):job.action_execute()
        again=self._job()
        with self.assertRaisesRegex(UserError,'evidence changed'):again.action_review()

    def test_postcutover_reconciliation_is_explicit_dependency(self):
        invoice=self._invoice()
        payment=self._entry(self.source,[('bank',20),('receivable',-20,self.customer)],when=date(2024,7,1))
        (invoice.line_ids|payment.line_ids).filtered(lambda l:l.account_id.account_type=='asset_receivable').reconcile()
        job=self._job();job.action_review();self.assertEqual(job.state,'blocked');self.assertIn('outside cutover',job.report)

    def test_hidden_branch_job_and_matches_cannot_be_read(self):
        branch=self.env['res.company'].create({'name':'Hidden branch','parent_id':self.source.id})
        job=self._job()
        job.with_context(allowed_company_ids=(self.source|branch|self.target).ids).write({'source_company_ids':[Command.set((self.source|branch).ids)]})
        user=new_test_user(self.env,login='document_hidden_read',groups='account.group_account_manager',company_id=self.source.id,company_ids=[Command.set((self.source|self.target).ids)])
        limited=job.with_user(user).with_context(allowed_company_ids=(self.source|self.target).ids)
        with self.assertRaises(AccessError):limited.read(['report'])
        with self.assertRaises(AccessError):job.mapping_ids.with_user(user).with_context(allowed_company_ids=(self.source|self.target).ids).read(['source_record'])

    def test_employee_cannot_read_durable_financial_attachment(self):
        self._invoice();job=self._job();self._run_job(job)
        user=new_test_user(self.env,login='ordinary_employee',groups='base.group_user',company_id=self.target.id,company_ids=[Command.set(self.target.ids)])
        with self.assertRaises(AccessError):job.audit_attachment_id.with_user(user).with_context(allowed_company_ids=self.target.ids).read(['raw'])

    def test_native_tax_evidence_changes_are_detected(self):
        self.test_multiple_taxes_discounts_quantities_native()
        job=self.env['company.document.cutover'].search([('target_company_id','=',self.target.id)],limit=1)
        tax_line=self.env['account.move.line'].search([('company_id','=',self.target.id),('tax_line_id','!=',False)],limit=1)
        tag=self.env['account.account.tag'].create({'name':'Synthetic tampered tax tag','applicability':'taxes'})
        tax_line.tax_tag_ids=[Command.set(tag.ids)]
        with self.assertRaisesRegex(UserError,'evidence changed'):job.action_execute()

    def test_changed_coverage_after_completion_does_not_incrementally_copy(self):
        self._invoice();job=self._job();self._run_job(job)
        self._invoice(amount=22)
        again=self._job();again.action_review()
        self.assertEqual(again.state,'blocked');self.assertIn('coverage changed',again.report)
        self.assertEqual(self.env['account.move'].search_count([('company_id','=',self.target.id)]),1)

    def test_mapping_derived_identity_cannot_be_forged(self):
        job=self._job()
        with self.assertRaises(AccessError):job.mapping_ids[:1].write({'source_id':999999})

    def test_mapping_native_uniqueness_is_enforced(self):
        job=self._job()
        job.mapping_ids.flush_recordset()
        row=job.mapping_ids.filtered(lambda r:r.source_record==self.customer)
        with self.assertRaises(UniqueViolation):
            with self.env.cr.savepoint():
                self.env.cr.execute('INSERT INTO company_document_mapping (job_id,model_name,source_id,target_id,source_record,target_record) VALUES (%s,%s,%s,%s,%s,%s)',
                    [job.id,row.model_name,row.source_id,row.target_id,str(row.source_record),str(row.target_record)])

    def test_number_overlap_without_document_identity_is_allowed(self):
        source=self._invoice(amount=100)
        unrelated=self.env['account.move'].with_company(self.target).create({'move_type':'out_invoice','company_id':self.target.id,
            'journal_id':self.journals[self.target.id]['sale'].id,'partner_id':self.customer.id,'invoice_date':self.cutoff,'date':self.cutoff,
            'invoice_line_ids':[Command.create({'name':'New company sale','quantity':1,'price_unit':7,
                'account_id':self.accounts[self.target.id]['revenue'].id,'tax_ids':[Command.clear()]})]})
        unrelated.action_post();self.assertEqual(source.name,unrelated.name)
        job=self._job();self._run_job(job);self.assertEqual(unrelated.amount_total,7)

    def test_identical_unmarked_destination_document_needs_review(self):
        source=self._invoice(amount=100)
        native=self.env['account.move'].with_company(self.target).create({'move_type':'out_invoice','company_id':self.target.id,
            'journal_id':self.journals[self.target.id]['sale'].id,'partner_id':self.customer.id,'invoice_date':self.cutoff,'date':self.cutoff,
            'invoice_line_ids':[Command.create({'name':'Potential earlier copy','quantity':1,'price_unit':100,
                'account_id':self.accounts[self.target.id]['revenue'].id,'tax_ids':[Command.clear()]})]})
        native.action_post();job=self._job();job.action_review();self.assertEqual(job.state,'blocked');self.assertIn('duplicate',job.report)

    def test_unsorted_branch_scope_survives_cache_refresh(self):
        branch=self.env['res.company'].create({'name':'Alphabetically first branch','parent_id':self.source.id})
        self.env=self.env(context={**self.env.context,'allowed_company_ids':(self.source|branch|self.target).ids})
        self._invoice()
        job=self._job(source_company_ids=[Command.set((self.source|branch).ids)])
        self._run_job(job)
        self.assertEqual(self.env['account.move'].search_count([('company_id','=',self.target.id)]),1)

    def test_renumbered_existing_invoice_requires_pair_decision(self):
        source=self._document('out_invoice',100);job=self._job()
        existing,_,_=job._copy_move(source);existing.name='IMPORTED/2024/00999'
        job.action_review();self.assertEqual(job.state,'blocked',job.report)
        self.assertIn('Possible existing document duplicate',job.report)
        job.action_execute();self.assertEqual(job.state,'failed')
        invoices=self.env['account.move'].search([('company_id','=',self.target.id),('move_type','=','out_invoice')])
        self.assertEqual(invoices,existing)
        self.assertEqual(sum(invoices.mapped('amount_total')),100)
        self.env['company.document.overlap'].create({'job_id':job.id,'source_move_id':source.id,
            'target_move_id':existing.id,'separate':True,'reason':'Synthetic distinct sale with identical accounting details; verified by accountant.'})
        self._run_job(job)
        self.assertIn('Synthetic distinct sale',job.audit_attachment_id.raw.decode())
        with self.assertRaisesRegex(UserError,'read-only'):job.overlap_ids.write({'reason':'changed'})

    def test_overlap_decision_cannot_hide_another_pair_or_wrong_company(self):
        source=self._document('out_invoice',100);job=self._job()
        first,_,_=job._copy_move(source);first.name='IMPORTED/2024/00990'
        second,_,_=job._copy_move(source);second.name='IMPORTED/2024/00991'
        self.env['company.document.overlap'].create({'job_id':job.id,'source_move_id':source.id,
            'target_move_id':first.id,'separate':True,'reason':'First pair only'})
        job.action_review();self.assertEqual(job.state,'blocked');self.assertIn(second.name,job.report)
        with self.assertRaises(AccessError),self.env.cr.savepoint():
            self.env['company.document.overlap'].create({'job_id':job.id,'source_move_id':source.id,
                'target_move_id':source.id,'separate':True,'reason':'Wrong company'})

    def test_original_invoice_payment_reference_preserved_and_guarded(self):
        source=self._document('out_invoice',100);source.payment_reference='PAYMENT-REFERENCE-KEEP-123'
        job=self._job();self._run_job(job)
        target=self._target_move(job,source)
        self.assertEqual(target.payment_reference,source.payment_reference)
        self.assertIn(source.payment_reference,job.audit_attachment_id.raw.decode())
        target.payment_reference='CHANGED'
        with self.assertRaisesRegex(UserError,'evidence|reference'):job.action_execute()

    def test_reference_change_invalidates_preview(self):
        source=self._document('out_invoice',100);job=self._job();job.action_review()
        source.payment_reference='CHANGED-AFTER-REVIEW'
        job.action_execute();self.assertEqual(job.state,'failed');self.assertIn('changed',job.report)
        self.assertFalse(self.env['account.move'].search([('company_id','=',self.target.id)]))

    def test_existing_invoice_later_booked_date_requires_overlap_review(self):
        source=self._document('out_invoice',100);job=self._job()
        existing,_,_=job._copy_move(source)
        existing.button_draft();existing.write({'date':date(2024,7,1),'name':'/'})
        existing.action_post();existing.name='IMPORTED/2024/00999'
        self.assertNotEqual(source.date,existing.date)
        self.assertEqual(source.invoice_date,existing.invoice_date)
        job.action_review();self.assertEqual(job.state,'blocked');self.assertIn('Possible existing document duplicate',job.report)
        job.action_execute();self.assertEqual(job.state,'failed')
        self.assertEqual(self.env['account.move'].search_count([('company_id','=',self.target.id)]),1)

    def test_blank_original_payment_reference_preserved(self):
        source=self._document('out_invoice',100)
        source.write({'payment_reference':False})
        self.assertFalse(source.payment_reference)
        job=self._job();self._run_job(job)
        self.assertFalse(self._target_move(job,source).payment_reference)
