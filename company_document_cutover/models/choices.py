"""Offer unambiguous setup matches without merging contacts or invoices by name."""
from odoo import models
from odoo.exceptions import UserError


class DocumentChoices(models.Model):
    _inherit = 'company.document.cutover'

    def action_find_setup_matches(self):
        self.ensure_one(); self._operator()
        if self.state == 'done':
            raise UserError('Completed move choices are read-only.')
        records = {}
        def add(record):
            for item in record:
                records[(item._name,item.id)] = item
        for move in self._moves():
            add(move.journal_id); add(move.partner_id); add(move.invoice_payment_term_id)
            add(move.fiscal_position_id); add(move.invoice_cash_rounding_id)
            for line in move.line_ids:
                add(line.account_id); add(line.partner_id); add(line.product_id); add(line.tax_ids | line.tax_line_id)
                for key in (line.analytic_distribution or {}):
                    add(self.env['account.analytic.account'].browse([int(n) for n in key.split(',')]).exists())
        if getattr(self,'include_purchase',False):
            orders=self.env['purchase.order'].search([('company_id','in',self.source_company_ids.ids),
                ('date_order','<=',str(self.cutover_date)+' 23:59:59')],limit=self.record_limit+1)
            if len(orders)>self.record_limit:
                raise UserError('Purchase history exceeds the configured review limit.')
            add(orders.partner_id);add(orders.payment_term_id);add(orders.order_line.product_id);add(orders.order_line.tax_ids)
        known={(r.model_name,r.source_id) for r in self.mapping_ids}
        count=0
        for key,old in records.items():
            if key in known:
                continue
            old.check_access('read')
            Model=self.env[old._name].with_company(self.target_company_id)
            domain=[('company_ids','in',self.target_company_id.ids)] if 'company_ids' in old._fields else (
                [('company_id','in',[False,self.target_company_id.id])] if 'company_id' in old._fields else [])
            candidates=Model
            if old._name=='res.partner':
                if old.vat:
                    identity=''.join(old.vat.upper().split())
                    candidates=Model.search(domain+[('vat','!=',False)]).filtered(lambda p:''.join(p.vat.upper().split())==identity)
                elif old.ref:
                    candidates=Model.search(domain+[('ref','=',old.ref)],limit=2)
                elif not old.company_id:
                    candidates=old
            elif old._name=='account.account':
                candidates=Model.search(domain+[('code','=',old.code)],limit=2)
            elif old._name=='account.journal':
                candidates=Model.search(domain+[('code','=',old.code),('type','=',old.type)],limit=2)
            elif old._name=='product.product':
                if old.barcode:
                    candidates=Model.search(domain+[('barcode','=',old.barcode)],limit=2)
                elif old.default_code:
                    candidates=Model.search(domain+[('default_code','=',old.default_code)],limit=2)
                elif not old.company_id:
                    candidates=old
            else:
                candidates=Model.search(domain+[('name','=',old.name)],limit=2)
            if len(candidates)!=1:
                continue
            self.env['company.document.mapping'].create({'job_id':self.id,
                'source_record':'%s,%s'%(old._name,old.id),'target_record':'%s,%s'%(old._name,candidates.id)})
            known.add(key);count+=1
        self._system_write({'state':'draft','preview_hash':False,
            'report':'Found %s unambiguous setup matches. Review the proposed matches, then click Review my documents. Existing invoices always need an explicit document choice; contacts are never matched by name alone.'%count})
        return True
