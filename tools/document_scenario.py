"""Synthetic-only fixture creation and independent native ledger reconciliation."""
from datetime import date,datetime
from odoo import Command


def build(env, prefix='Document simulation '):
    assert env.cr.dbname in ('fifo_doc_simulation', 'fifo_doc_history'), 'Only the dedicated disposable database is allowed'
    env=env(context={**env.context,'no_reset_password':True,'tracking_disable':True,
        'mail_notify_force_send':False,'mail_create_nosubscribe':True})
    assert not env['ir.mail_server'].search_count([])
    assert not env['payment.provider'].search_count([('state','!=','disabled')]) if 'payment.provider' in env else True
    assert 'company.stock.fifo.migration' not in env, 'Stock mover must not be installed for this simulation'
    env['ir.cron'].sudo().search([]).write({'active':False})
    root,target=env['res.company'].create([{'name':prefix+n,'currency_id':env.company.currency_id.id,
        'account_fiscal_country_id':env.ref('base.us').id,'iap_enrich_auto_done':True} for n in ('Old A','New A')])
    branches=env['res.company'].create([{'name':prefix+n,'parent_id':root.id,'iap_enrich_auto_done':True} for n in ('B','C')])
    source=root|branches
    for company in source|target:
        if not env['stock.warehouse'].search_count([('company_id','=',company.id)]):
            env['stock.warehouse'].with_company(company).create({'name':prefix+company.name,'code':'D%s'%company.id,'company_id':company.id})
    env=env(context={**env.context,'allowed_company_ids':(source|target).ids})
    accounts={};journals={}
    specs=[('receivable','1000','asset_receivable'),('bank','1010','asset_cash'),('payable','2000','liability_payable'),
        ('equity','3000','equity'),('revenue','4000','income'),('expense','5000','expense')]
    for c in root|target:
        accounts[c.id]={}
        for n,code,kind in specs:
            accounts[c.id][n]=env['account.account'].with_company(c).create({'name':n,'code':code,'account_type':kind,
                'reconcile':kind in ('asset_receivable','liability_payable','asset_cash'),'company_ids':[Command.set(c.ids)]})
    for c in branches:accounts[c.id]=accounts[root.id]
    for c in source|target:
        journals[c.id]={}
        for n,kind in [('general','general'),('sale','sale'),('purchase','purchase'),('bank','bank')]:
            j=env['account.journal'].with_company(c).create({'name':prefix+n,'code':n[:3].upper(),'type':kind,'company_id':c.id,
                'default_account_id':accounts[c.id]['bank' if kind=='bank' else 'expense' if kind=='purchase' else 'revenue'].id})
            journals[c.id][n]=j
            if kind=='bank':(j.inbound_payment_method_line_ids|j.outbound_payment_method_line_ids).write({'payment_account_id':accounts[c.id]['bank'].id})
    partner=env['res.partner'].create({'name':prefix+'Shared customer and vendor','ref':prefix+'CONTACT'})
    for c in source|target:partner.with_company(c).write({'property_account_receivable_id':accounts[c.id]['receivable'].id,
        'property_account_payable_id':accounts[c.id]['payable'].id})
    cutoff=date(2024,6,30)
    def invoice(c,kind,amount,posted=True,reference=''):
        sale=kind.startswith('out_')
        m=env['account.move'].with_company(c).create({'company_id':c.id,'move_type':kind,
            'journal_id':journals[c.id]['sale' if sale else 'purchase'].id,'partner_id':partner.id,
            'date':cutoff,'invoice_date':cutoff,'invoice_date_due':date(2024,7,31),'ref':reference,
            'invoice_line_ids':[Command.create({'name':prefix+kind,'quantity':1,'price_unit':amount,
                'account_id':accounts[c.id]['revenue' if sale else 'expense'].id,'tax_ids':[Command.clear()]})]})
        if posted:m.action_post()
        return m
    def pay(m,amount):
        c=m.company_id;kind='inbound' if m.move_type=='out_invoice' else 'outbound'
        j=journals[c.id]['bank'];method=(j.inbound_payment_method_line_ids if kind=='inbound' else j.outbound_payment_method_line_ids).filtered(lambda l:l.code=='manual')
        p=env['account.payment'].with_company(c).create({'company_id':c.id,'journal_id':j.id,'date':cutoff,
            'amount':amount,'currency_id':c.currency_id.id,'payment_type':kind,'partner_type':'customer' if kind=='inbound' else 'supplier',
            'partner_id':partner.id,'payment_method_line_id':method.id,'memo':'Synthetic ledger-only payment',
            'destination_account_id':accounts[c.id]['receivable' if kind=='inbound' else 'payable'].id})
        p.write({'state':'paid'})
        (m.line_ids|p.move_id.line_ids).filtered(lambda l:l.account_id.account_type in ('asset_receivable','liability_payable')).reconcile()
        p.invoice_ids=[Command.set(m.ids)]
        return p
    invoices=[invoice(c,'out_invoice',a) for c,a in zip(source,(100,200,300))]
    bills=[invoice(c,'in_invoice',a) for c,a in zip(source,(80,120,160))]
    for m,a in ((invoices[1],50),(invoices[2],300),(bills[1],40),(bills[2],160)):pay(m,a)
    credit=invoice(root,'out_refund',30);vendor_credit=invoice(root,'in_refund',20)
    (credit.line_ids|invoices[0].line_ids).filtered(lambda l:l.account_id.account_type=='asset_receivable').reconcile()
    (vendor_credit.line_ids|bills[0].line_ids).filtered(lambda l:l.account_id.account_type=='liability_payable').reconcile()
    invoice(branches[0],'out_invoice',40,False);invoice(branches[1],'in_invoice',50,False)
    # Destination is already trading: this is unrelated new-company evidence.
    trade=invoice(target,'out_invoice',7,True,'UNRELATED-NEW-ACTIVITY')
    products=[]
    for c in (root,target):products.append(env['product.product'].with_company(c).create({'name':prefix+'Goods',
        'company_id':c.id,'type':'consu','is_storable':True,'purchase_method':'receive',
        'taxes_id':[Command.clear()],'supplier_taxes_id':[Command.clear()]}))
    old,new=products
    orders=env['purchase.order']
    for c,qty,state in zip(source,(3,10,2),('draft','purchase','cancel')):
        o=env['purchase.order'].with_company(c).create({'company_id':c.id,'partner_id':partner.id,
            'date_order':datetime(2024,6,30),'order_line':[Command.create({'product_id':old.id,'product_qty':qty,
                'product_uom_id':old.uom_id.id,'price_unit':20,'discount':5,'tax_ids':[Command.clear()],
                'date_planned':datetime(2024,6,30)})]})
        if state=='purchase':o.button_confirm();o.write({'date_order':datetime(2024,6,30),'date_approve':datetime(2024,6,30)})
        if state=='cancel':o.button_cancel()
        orders|=o
    job=env['company.document.cutover'].create({'source_company_ids':[Command.set(source.ids)],'target_company_id':target.id,
        'cutover_date':cutoff,'same_legal_entity':True,'existing_data_reviewed':True,
        'include_purchase':True,'old_operations_frozen':True})
    pairs=[(a,accounts[target.id][n]) for n,a in accounts[root.id].items()]
    pairs += [(j,journals[target.id][n]) for c in source for n,j in journals[c.id].items()]
    pairs += [(partner,partner),(old,new)]
    for a,b in pairs:env['company.document.mapping'].create({'job_id':job.id,
        'source_record':'%s,%s'%(a._name,a.id),'target_record':'%s,%s'%(b._name,b.id)})
    job.action_review();assert job.state=='ready',job.report
    originals=env['account.move'].search([('company_id','in',source.ids)])
    return {'companies':(source|target).ids,'sources':source.ids,'target':target.id,'job':job.id,'invoices':[m.id for m in invoices],'bills':[m.id for m in bills],
        'orders':orders.ids,'trading':trade.id,'partner':partner.id,'cutover':str(cutoff),
        'accounts':{n:a.id for n,a in accounts[target.id].items()},
        'source_fingerprints':{str(m.id):job._fingerprint(m) for m in originals},
        'source_po_fingerprints':{str(o.id):job._fingerprint(o) for o in orders},
        'trading_fingerprint':job._fingerprint(trade)}


def verify(env,data):
    env=env(context={**env.context,'allowed_company_ids':data['companies']})
    job=env['company.document.cutover'].browse(data['job']);assert job.state=='done',job.report
    for i,h in data['source_fingerprints'].items():assert job._fingerprint(env['account.move'].browse(int(i)))==h
    for i,h in data['source_po_fingerprints'].items():assert job._fingerprint(env['purchase.order'].browse(int(i)))==h
    assert job._fingerprint(env['account.move'].browse(data['trading']))==data['trading_fingerprint']
    # Independent SQL sums, including untouched new-company activity; no planner output is trusted.
    env.cr.execute('SELECT account_id,SUM(balance) FROM account_move_line WHERE company_id=%s AND parent_state=%s GROUP BY account_id',
        [data['target'],'posted'])
    actual={a:float(v) for a,v in env.cr.fetchall()}
    expected={'receivable':227,'bank':150,'payable':-140,'revenue':-577,'expense':340,'equity':0}
    for name,value in expected.items():assert abs(actual.get(data['accounts'][name],0)-value)<.0001,(name,actual,value)
    assert abs(sum(actual.values()))<.0001
    ar=[];ap=[]
    for ids,result in ((data['invoices'],ar),(data['bills'],ap)):
        for i in ids:
            original=env['account.move'].browse(i);native=env['account.move'].browse(job._stored_link(original)['target'])
            result.append(native.amount_residual)
    assert ar==[70,150,0] and ap==[60,80,0],(ar,ap)
    native_orders=env['purchase.order'].search([('company_id','=',data['target'])]);assert len(native_orders)==3
    active=native_orders.filtered(lambda o:o.state=='purchase');assert active.order_line.product_qty==10
    assert not active.picking_ids.filtered(lambda p:p.state=='done')
    assert sum(active.picking_ids.move_ids.mapped('product_uom_qty'))==10
    moves=env['account.move'].search([('company_id','=',data['target'])]);payments=env['account.payment'].search([('company_id','=',data['target'])])
    assert len(payments)==4 and all(p.payment_method_id.code=='manual' for p in payments)
    assert not moves.financial_cutover_id
    return {'status':'PASS','method':'Independent SQL trial balance plus native residual/PO/source evidence',
        'account_balances':expected,'P_and_L':237,'balance_sheet_net_profit':237,
        'customer_residuals':ar,'vendor_residuals':ap,'native_destination_documents':len(moves),
        'native_manual_accounting_payments':len(payments),'destination_purchase_orders':3,
        'remaining_delivery':10,'remaining_billing':10,'validated_destination_receipts':0,
        'source_unchanged':True,'existing_destination_activity_unchanged':True,'additional_opening_entries':0}
