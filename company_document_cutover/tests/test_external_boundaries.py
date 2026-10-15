"""Native optional-model fixtures prove unsafe external hooks are never reached."""
from contextlib import ExitStack
from unittest.mock import patch
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from .test_documents import TestDocumentMigration


@tagged('post_install', '-at_install')
class TestExternalBoundaries(FinancialCutoverCase):
    _job = TestDocumentMigration._job
    _match = TestDocumentMigration._match
    _document = TestDocumentMigration._document
    _pay = TestDocumentMigration._pay

    def setUp(self):
        super().setUp()
        self.journals = {company: {name: journal for name, journal in journals.items() if name != 'bank'}
            for company, journals in self.journals.items()}

    def _provider(self, company):
        if 'payment.provider' not in self.env:
            self.skipTest('Optional native payment provider models are absent')
        return self.env['payment.provider'].with_company(company).create({
            'name': 'Disabled synthetic provider', 'code': 'none', 'state': 'disabled', 'company_id': company.id})

    def _transaction(self, company):
        return self.env['payment.transaction'].create({'provider_id': self._provider(company).id,
            'payment_method_id': self.env.ref('payment.payment_method_card').id, 'reference': 'SYNTHETIC-EXTERNAL-%s' % company.id,
            'provider_reference': 'SYNTHETIC-NO-REMOTE-TRANSACTION', 'amount': 100, 'currency_id': company.currency_id.id,
            'partner_id': self.customer.id, 'operation': 'offline', 'state': 'done', 'is_post_processed': True})

    def _no_external_calls(self):
        stack = ExitStack()
        for model, names in [('payment.transaction', ('_charge_with_token', '_post_process')),
                             ('account.payment', ('_create_payment_transaction',)),
                             ('account.edi.document', ('_process_documents_web_services', '_process_documents_no_web_services'))]:
            if model in self.env:
                for name in names:
                    if hasattr(type(self.env[model]), name):
                        stack.enter_context(patch.object(type(self.env[model]), name,
                            side_effect=AssertionError('External payment/EDI hook must never run')))
        return stack

    def _banks(self):
        for company in (self.source, self.target):
            journal = self.env['account.journal'].with_company(company).create({'name': 'Synthetic manual bank',
                'code': 'BNK', 'type': 'bank', 'company_id': company.id, 'default_account_id': self.accounts[company.id]['bank'].id})
            (journal.inbound_payment_method_line_ids | journal.outbound_payment_method_line_ids).payment_account_id = self.accounts[company.id]['bank']
            self.journals[company.id]['bank'] = journal

    def test_source_invoice_with_recorded_provider_transaction_blocks_without_charge(self):
        invoice = self._document('out_invoice', 100)
        tx = self._transaction(self.source)
        invoice.transaction_ids = [Command.link(tx.id)]
        job = self._job()
        with self._no_external_calls():
            job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('provider', job.report)
            job.action_execute(); self.assertEqual(job.state, 'failed')
        self.assertFalse(self.env['account.move'].search([('company_id', '=', self.target.id)]))
        self.assertEqual(self.env['payment.transaction'].search_count([]), 1)

    def test_manual_coded_target_payment_with_token_cannot_be_adopted(self):
        self._banks()
        invoice = self._document('out_invoice', 100); payment = self._pay(invoice, 100)
        job = self._job()
        target_invoice, _, _ = job._copy_move(invoice)
        target_move, _, _ = job._copy_move(payment.move_id)
        token = self.env['payment.token'].create({'provider_id': self._provider(self.target).id,
            'payment_method_id': self.env.ref('payment.payment_method_card').id, 'partner_id': self.customer.id,
            'provider_ref': 'SYNTHETIC-NOT-A-CREDENTIAL', 'payment_details': 'Synthetic only'})
        target_move.origin_payment_id.payment_token_id = token
        self._match(job, invoice, target_invoice); self._match(job, payment.move_id, target_move)
        before = target_move.origin_payment_id.read(['invoice_ids', 'write_date'])[0]
        with self._no_external_calls():
            job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('specialized read-only', job.report)
            job.action_execute(); self.assertEqual(job.state, 'failed')
        self.assertEqual(before, target_move.origin_payment_id.read(['invoice_ids', 'write_date'])[0])
        self.assertFalse(self.env['payment.transaction'].search([]))

    def test_saved_target_provider_link_cannot_bypass_unsupported_gate(self):
        invoice = self._document('out_invoice', 100); job = self._job()
        job.action_review(); job.action_execute(); self.assertEqual(job.state, 'done', job.report)
        target = self.env['account.move'].browse(job._stored_link(invoice)['target'])
        target.transaction_ids = [Command.link(self._transaction(self.target).id)]
        with self._no_external_calls(), self.assertRaisesRegex(UserError, 'specialized read-only'):
            job.action_execute()

    def test_target_edi_journal_blocks_copy_before_posting_or_queueing(self):
        if 'account.edi.format' not in self.env:
            self.skipTest('Optional EDI app absent; run the documented EDI boundary check')
        invoice = self._document('out_invoice', 100)
        fmt = self.env['account.edi.format'].create({'name': 'Synthetic no-service format', 'code': 'synthetic_history_gate'})
        self.journals[self.source.id]['sale'].edi_format_ids = [Command.clear()]
        self.journals[self.target.id]['sale'].edi_format_ids = [Command.set(fmt.ids)]
        job = self._job(); before = self.env['account.edi.document'].search([]).ids
        with self._no_external_calls():
            job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('would start electronic', job.report)
            job.action_execute(); self.assertEqual(job.state, 'failed')
        self.assertEqual(before, self.env['account.edi.document'].search([]).ids)
        self.assertFalse(self.env['account.move'].search([('company_id', '=', self.target.id)]))

    def test_existing_target_edi_document_cannot_be_adopted_silently(self):
        if 'account.edi.format' not in self.env:
            self.skipTest('Optional EDI app absent; run the documented EDI boundary check')
        invoice = self._document('out_invoice', 100); job = self._job()
        target, _, _ = job._copy_move(invoice)
        fmt = self.env['account.edi.format'].create({'name': 'Synthetic EDI evidence', 'code': 'synthetic_existing_gate'})
        self.env['account.edi.document'].create({'move_id': target.id, 'edi_format_id': fmt.id, 'state': 'sent'})
        self._match(job, invoice, target)
        before = target.read(['state', 'write_date'])[0]
        with self._no_external_calls():
            job.action_review(); self.assertEqual(job.state, 'blocked'); self.assertIn('specialized read-only', job.report)
        self.assertEqual(before, target.read(['state', 'write_date'])[0])
