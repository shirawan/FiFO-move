"""Synthetic native Odoo scenario shared by browser and retention checks."""
from datetime import date

from odoo import Command


def build_consolidation(environment, prefix):
    root, target = environment['res.company'].create([
        {'name': prefix + name, 'currency_id': environment.company.currency_id.id,
            'account_fiscal_country_id': environment.ref('base.us').id}
        for name in ('Old A', 'New A (standalone)')])
    branches = environment['res.company'].create([
        {'name': prefix + name, 'parent_id': root.id} for name in ('Branch B', 'Branch C')])
    scenario = environment(context={**environment.context, 'allowed_company_ids': (root | branches | target).ids})
    accounts, journals = {}, {}
    for company in root | target:
        accounts[company.id] = {}
        for key, code, kind in (('receivable', '1000', 'asset_receivable'), ('bank', '1010', 'asset_cash'),
                ('payable', '2000', 'liability_payable'), ('revenue', '4000', 'income'), ('expense', '5000', 'expense')):
            accounts[company.id][key] = scenario['account.account'].with_company(company).create({
                'name': key.title(), 'code': code, 'account_type': kind,
                'reconcile': kind in {'asset_receivable', 'liability_payable'}, 'company_ids': [Command.set(company.ids)]})
    for company in root | branches | target:
        if company in branches:
            accounts[company.id] = accounts[root.id]
        journals[company.id] = {}
        for key, code, kind in (('general', 'MISC', 'general'), ('sale', 'INV', 'sale')):
            journals[company.id][key] = scenario['account.journal'].with_company(company).create({
                'name': prefix + key.title(), 'code': code, 'type': kind, 'company_id': company.id,
                'default_account_id': accounts[company.id]['revenue'].id})
    contact = scenario['res.partner'].create({'name': prefix + 'Shared customer/vendor', 'ref': prefix + 'CONTACT'})
    for company in root | branches | target:
        contact.with_company(company).write({'property_account_receivable_id': accounts[company.id]['receivable'].id,
            'property_account_payable_id': accounts[company.id]['payable'].id})

    def entry(company, values):
        move = scenario['account.move'].with_company(company).create({
            'company_id': company.id, 'journal_id': journals[company.id]['general'].id, 'date': date.today(),
            'line_ids': [Command.create({'name': key, 'account_id': accounts[company.id][key].id,
                'partner_id': contact.id if key == 'receivable' else False,
                'debit': max(amount, 0), 'credit': max(-amount, 0), 'currency_id': company.currency_id.id,
                'amount_currency': amount, 'tax_ids': [Command.clear()]}) for key, amount in values]})
        move.action_post()
        return move

    def invoice(company, amount, reference):
        move = scenario['account.move'].with_company(company).create({
            'move_type': 'out_invoice', 'company_id': company.id, 'journal_id': journals[company.id]['sale'].id,
            'partner_id': contact.id, 'invoice_date': date.today(), 'date': date.today(), 'ref': reference,
            'invoice_line_ids': [Command.create({'name': reference, 'quantity': 1, 'price_unit': amount,
                'account_id': accounts[company.id]['revenue'].id, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    originals = scenario['account.move']
    for company, amount in zip(root | branches, (100, 200, 300)):
        originals |= invoice(company, amount, prefix + company.name)
    copied_source = invoice(branches[0], 50, prefix + 'ALREADY-COPIED')
    originals |= copied_source
    earlier = entry(target, [('receivable', 100), ('revenue', -100)])
    payment = entry(target, [('bank', 40), ('receivable', -40)])
    (earlier | payment).line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable').reconcile()
    trading = invoice(target, 20, prefix + 'NEW-ACTIVITY')
    copied = invoice(target, 50, prefix + 'ALREADY-COPIED')
    product = scenario['product.product'].create({'name': prefix + 'Shared service', 'type': 'service',
        'supplier_taxes_id': [Command.clear()]})
    orders = scenario['purchase.order']
    for company in root | branches:
        order = scenario['purchase.order'].with_company(company).create({'company_id': company.id,
            'partner_id': contact.id, 'partner_ref': prefix + 'PO-' + str(company.id),
            'order_line': [Command.create({'product_id': product.id, 'name': 'Branch history',
                'product_qty': 5, 'price_unit': 10, 'product_uom_id': product.uom_id.id,
                'date_planned': str(date.today()) + ' 10:00:00', 'tax_ids': [Command.clear()]})]})
        order.button_confirm()
        if company == branches[1]:
            order.button_cancel()
        orders |= order
    batch = scenario['company.financial.cutover'].create({'source_company_id': root.id,
        'target_company_id': target.id, 'include_source_branches': True, 'destination_mode': 'existing',
        'prior_opening_move_ids': [Command.set(earlier.ids)], 'cutover_date': date.today(),
        'journal_id': journals[target.id]['general'].id})
    batch.action_match()
    scenario['company.financial.copied.invoice'].create({'cutover_id': batch.id,
        'source_move_id': copied_source.id, 'target_move_id': copied.id})
    return {'companies': (root | branches | target).ids, 'source': root.id, 'target': target.id,
        'source_companies': (root | branches).ids, 'branches': branches.ids, 'batch': batch.id,
        'earlier': earlier.id, 'payment': payment.id, 'trading': trading.id, 'copied': copied.id,
        'originals': originals.ids, 'orders': orders.ids, 'branch_order': orders.filtered(lambda order: order.company_id == branches[0]).id,
        'contact': contact.id, 'currency': target.currency_id.name,
        'baseline': {'accounting': [(move.id, move.state, move.amount_residual, str(move.write_date)) for move in originals],
            'destination': [(move.id, move.state, [(line.id, line.balance) for line in move.line_ids]) for move in earlier | payment | trading | copied]}}


def verify_consolidation(environment, data, restored):
    scenario = environment(context={**environment.context, 'allowed_company_ids': data['companies']})
    opening = scenario['account.move'].browse(data['opening'])
    corrections = scenario['account.move'].browse(data['corrections'])
    assert opening.state == 'posted' and corrections.mapped('state') == ['posted']
    assert sorted(opening.line_ids.filtered(lambda line: line.account_id.account_type == 'asset_receivable').mapped('amount_residual')) == [60, 200, 300]
    trading = scenario['account.move'].browse(data['trading'])
    copied = scenario['account.move'].browse(data['copied'])
    assert trading.amount_residual == 20 and copied.amount_residual == 50
    target_lines = scenario['account.move.line'].search([('company_id', '=', data['target']), ('parent_state', '=', 'posted')])
    assert sum(target_lines.filtered(lambda line: line.account_id.account_type == 'asset_receivable').mapped('balance')) == 630
    assert scenario['account.move'].search_count([('company_id', '=', data['target'])]) == 6
    if restored:
        batch = scenario['company.financial.cutover'].search([('archive_key', '=', data['archive_key'])])
        assert batch.move_id == opening and batch.correction_move_ids == corrections
        assert opening.financial_cutover_id == batch and corrections.financial_cutover_id == batch
        assert set(batch.completed_source_ids) == set(data['source_companies'])
        assert sorted((history.snapshot for history in batch.purchase_history_ids), key=lambda row: row['id']) == sorted(data['histories'], key=lambda row: row['id'])
        assert set(batch.purchase_history_ids.source_company_id.ids) == set(data['source_companies'])
        history = batch.purchase_history_ids.filtered(lambda row: row.source_order_res_id == data['branch_order'])
        assert history.target_order_id.id == data['replacement'] and history.target_order_id.state == 'draft'
    else:
        assert 'financial_cutover_id' not in opening._fields
    print('SURVIVAL: A+B/C, earlier-opening adjustment, existing payment, copied invoice and new activity preserved; no reposts.')
