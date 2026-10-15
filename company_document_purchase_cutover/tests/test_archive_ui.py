from unittest.mock import patch
from odoo import Command
from odoo.tests import HttpCase, tagged, new_test_user
from odoo.tests.common import ChromeBrowser
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from .test_archive import TestPurchaseArchive


@tagged('post_install', '-at_install', 'document_browser')
class TestPurchaseArchiveBrowser(FinancialCutoverCase, HttpCase):
    _job = TestPurchaseArchive._job
    _match = TestPurchaseArchive._match
    _run_job = TestPurchaseArchive._run_job
    _products = TestPurchaseArchive._products
    _po = TestPurchaseArchive._po
    _receive = TestPurchaseArchive._receive
    _bill = TestPurchaseArchive._bill
    _fixture = TestPurchaseArchive._fixture

    def _exercise_archive(self, mobile=False):
        order, bill, job = self._fixture()
        self.env = self.env(context={**self.env.context, 'no_reset_password': True, 'mail_notify_force_send': False})
        new_test_user(self.env, login='archive_browser', password='archive_browser',
            groups='base.group_system,account.group_account_manager,purchase.group_purchase_manager',
            company_id=self.source.id, company_ids=[Command.set((self.source | self.target).ids)])
        self.browser_size = '390x844' if mobile else '1440x1000'
        action = self.env.ref('company_document_cutover.document_action').id
        code = '''
(async () => {
    const wait = async check => {
        for(let n=0;n<250;n++) { const x=check(); if(x)return x; await new Promise(r=>setTimeout(r,100)); }
        throw new Error('Historical purchase screen did not reach the expected step');
    };
    const visible = selector => [...document.querySelectorAll(selector)].find(e=>e.getClientRects().length);
    if(!document.body.innerText.includes('Historical receipts and returns'))throw new Error('History choice missing');
    (await wait(()=>visible('button[name="action_review"]'))).click();
    const confirm=await wait(()=>visible('button[name="action_execute"]'));
    if(!document.body.innerText.includes('History only: retain original documents'))throw new Error('Audit-only review missing');
    if(!document.body.innerText.includes('In the old company only'))throw new Error('Old commitments not distinguished');
    confirm.click(); (await wait(()=>visible('.modal-footer .btn-primary'))).click();
    await wait(()=>document.body.innerText.includes('Completed. Original source records stayed unchanged.'));
    await wait(()=>document.querySelector('[name="purchase_archive_ids"]'));
    if(!document.body.innerText.toLowerCase().includes('historical references'))throw new Error('Historical references missing from result');
    console.log('test successful');
})();
'''
        self.browser_js('/odoo/action-%s/%s' % (action, job.id), code,
            ready="!!document.querySelector('button[name=action_review]')", login='archive_browser', timeout=60,
            cookies={'cids': '%s-%s' % (self.source.id, self.target.id)})
        self.env.invalidate_all(); self.assertEqual(job.state, 'done', job.report)
        self.assertFalse(self.env['purchase.order'].search([('company_id', '=', self.target.id)]))
        history = job.purchase_archive_ids
        action = self.env.ref('company_document_purchase_cutover.purchase_archive_action').id
        original = ChromeBrowser._wait_code_ok
        def capture(browser, *args, **kwargs):
            result = original(browser, *args, **kwargs)
            future = browser.take_screenshot('archive_mobile_' if mobile else 'archive_desktop_')
            if future: future.result(timeout=10)
            return result
        with patch.object(ChromeBrowser, '_wait_code_ok', capture):
            self.browser_js('/odoo/action-%s/%s' % (action, history.id), '''
if(!document.body.innerText.includes('HISTORY ONLY'))throw new Error('History-only label missing');
if(!document.body.innerText.includes('Pending supplier delivery 6'))throw new Error('Original quantities missing');
if(!document.body.innerText.includes('saved accounting document #'))throw new Error('Accounting reference missing');
if(document.querySelector('.o_form_button_edit'))throw new Error('Historical reference offers editing');
console.log('test successful');
''', ready="!!document.querySelector('[name=accounting_matches]')", login='archive_browser', timeout=60,
                cookies={'cids': '%s-%s' % (self.source.id, self.target.id)})

    def test_desktop_history_choice_review_and_reference(self):
        self._exercise_archive()

    def test_phone_history_choice_review_and_reference(self):
        self._exercise_archive(mobile=True)
