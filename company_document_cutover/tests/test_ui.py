from unittest.mock import patch
from odoo import Command
from odoo.tests import HttpCase, tagged, new_test_user
from odoo.tests.common import ChromeBrowser
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from .test_documents import TestDocumentMigration


@tagged('post_install','-at_install','document_browser')
class TestDocumentBrowser(FinancialCutoverCase, HttpCase):
    _job=TestDocumentMigration._job
    _match=TestDocumentMigration._match
    _document=TestDocumentMigration._document

    def _exercise_form(self, mobile=False):
        invoice=self._document('out_invoice',100)
        if not mobile:self._document('out_invoice',200)
        job=self._job(batch_size=1)
        self.env=self.env(context={**self.env.context,'no_reset_password':True,'mail_notify_force_send':False})
        new_test_user(self.env,login='document_browser',password='document_browser',
            groups='base.group_system,account.group_account_manager',company_id=self.source.id,
            company_ids=[Command.set((self.source|self.target).ids)])
        self.browser_size='390x844' if mobile else '1440x1000'
        action=self.env.ref('company_document_cutover.document_action').id
        code='''
(async () => {
    const wait = async (check) => {
        for (let n=0; n<250; n++) {
            const found=check(); if(found) return found;
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        throw new Error('Document form did not reach the expected step');
    };
    const visible = selector => [...document.querySelectorAll(selector)].find(e=>e.getClientRects().length);
    if(!document.body.innerText.includes('Old company and branches') || !document.body.innerText.includes('New company'))
        throw new Error('Plain-language company choices are missing');
    for(let step=0; step<__steps__; step++) {
        const review=await wait(()=>visible('button[name="action_review"]'));
        review.click();
        const confirm=await wait(()=>visible('button[name="action_execute"]'));
        if(!document.body.innerText.includes('New documents: '+(__steps__-step))) throw new Error('Review count is missing');
        confirm.click();
        const dialog=await wait(()=>visible('.modal-footer .btn-primary'));
        dialog.click();
        if(step+1<__steps__) await wait(()=>document.body.innerText.includes('Step saved.'));
        else await wait(()=>document.body.innerText.includes('Completed. Original source records stayed unchanged.'));
    }
    if(visible('button[name="action_execute"]')) throw new Error('Completed move still offers confirmation');
    console.log('test successful');
})();
'''
        code=code.replace('__steps__','1' if mobile else '2')
        original=ChromeBrowser._wait_code_ok
        def capture(browser,*args,**kwargs):
            result=original(browser,*args,**kwargs)
            future=browser.take_screenshot('document_mobile_' if mobile else 'document_desktop_')
            if future:future.result(timeout=10)
            return result
        with patch.object(ChromeBrowser,'_wait_code_ok',capture):
            self.browser_js('/odoo/action-%s/%s'%(action,job.id),code,
                ready="!!document.querySelector('button[name=action_review]')",login='document_browser',timeout=60,
                cookies={'cids':'%s-%s'%(self.source.id,self.target.id)})
        self.env.invalidate_all();self.assertEqual(job.state,'done')
        self.assertEqual(self.env['account.move'].search_count([('company_id','=',self.target.id)]),1 if mobile else 2)
        self.assertTrue(job._stored_link(invoice))

    def test_desktop_review_and_confirm(self):
        self._exercise_form()

    def test_mobile_review_and_confirm(self):
        self._exercise_form(mobile=True)
