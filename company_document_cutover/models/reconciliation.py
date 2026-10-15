"""Let native reconciliation regenerate exchange and cash-basis entries once."""
from odoo import models
from odoo.exceptions import UserError


class ReconciledHistory(models.Model):
    _inherit = 'company.document.cutover'

    def _generated_moves(self, moves):
        partials = moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids
        return (partials.exchange_move_id |
                moves.filtered(lambda m:m.tax_cash_basis_rec_id)) & moves

    def _dependency_edges(self, moves):
        result = super()._dependency_edges(moves)
        for generated in moves.filtered(lambda m:m.tax_cash_basis_rec_id):
            result.append((generated.id, generated.tax_cash_basis_rec_id.debit_move_id.move_id.id))
            result.append((generated.id, generated.tax_cash_basis_rec_id.credit_move_id.move_id.id))
        return result

    def _move_checks(self, move, all_moves, represented=None):
        company = move.company_id
        partials = move.line_ids.matched_debit_ids | move.line_ids.matched_credit_ids
        names = []
        if partials.exchange_move_id:
            names += ['currency_exchange_journal_id','income_currency_exchange_account_id','expense_currency_exchange_account_id']
        if any(t.tax_exigibility == 'on_payment' for t in move.line_ids.tax_ids | move.line_ids.tax_line_id) or move.tax_cash_basis_rec_id:
            names += ['tax_cash_basis_journal_id','account_cash_basis_base_account_id']
            if company.tax_exigibility != self.target_company_id.tax_exigibility:
                raise UserError('Enable matching native cash-basis accounting settings in both companies.')
        for name in names:
            if self._target(company[name]) != self.target_company_id[name]:
                raise UserError('Native exchange/cash-basis setup differs: %s.' % company._fields[name].string)
        return super()._move_checks(move, all_moves, represented)

    def _capture_generated(self, moves, targets, line_map, before):
        generated = self.env['account.move'].search([('company_id','=',self.target_company_id.id)]) - before
        candidates = self._generated_moves(moves).filtered(lambda m:m.id not in targets)
        for target in generated:
            matches = []
            for source in candidates:
                if (source.date != target.date or source.state != target.state or source.move_type != target.move_type or
                        self._target(source.journal_id) != target.journal_id or source.currency_id != target.currency_id or
                        bool(source.tax_cash_basis_rec_id) != bool(target.tax_cash_basis_rec_id)):
                    continue
                try:
                    lines = self._line_map(source,target)
                except UserError:
                    continue
                if source.tax_cash_basis_origin_move_id and targets.get(source.tax_cash_basis_origin_move_id.id) != target.tax_cash_basis_origin_move_id:
                    continue
                matches.append((source,lines))
            if len(matches) != 1:
                raise UserError('Native reconciliation generated an unexpected or ambiguous exchange/cash-basis entry. No step records were retained.')
            source, lines = matches[0]
            targets[source.id] = target; line_map.update(lines); candidates -= source

    def _reconcile_moves(self, moves, targets, line_map):
        pending = moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids
        while pending:
            progress = False
            for partial in pending.sorted('id'):
                if str(partial.debit_move_id.id) not in line_map or str(partial.credit_move_id.id) not in line_map:
                    continue
                debit = self.env['account.move.line'].browse(line_map[str(partial.debit_move_id.id)])
                credit = self.env['account.move.line'].browse(line_map[str(partial.credit_move_id.id)])
                existing = debit.matched_credit_ids.filtered(lambda p:p.credit_move_id==credit)
                if not existing:
                    if not self.target_company_id.currency_id.is_zero(min(debit.amount_residual,-credit.amount_residual)-partial.amount):
                        raise UserError('This partial reconciliation cannot be reproduced by native reconcile() exactly.')
                    before = self.env['account.move'].search([('company_id','=',self.target_company_id.id)])
                    (debit | credit).reconcile()
                    self._capture_generated(moves,targets,line_map,before)
                    existing = debit.matched_credit_ids.filtered(lambda p:p.credit_move_id==credit)
                if (len(existing) != 1 or not self.target_company_id.currency_id.is_zero(existing.amount-partial.amount) or
                        not debit.currency_id.is_zero(existing.debit_amount_currency-partial.debit_amount_currency) or
                        not credit.currency_id.is_zero(existing.credit_amount_currency-partial.credit_amount_currency)):
                    raise UserError('Native reconciliation produced different accounting/foreign amounts; rolled back.')
                if partial.exchange_move_id and existing.exchange_move_id != targets.get(partial.exchange_move_id.id):
                    raise UserError('Native exchange-difference ownership differs from the original reconciliation.')
                pending -= partial; progress=True
            if not progress:
                raise UserError('Native exchange/cash-basis history could not be recreated from the selected reconciliation graph.')
        if moves.filtered(lambda m:m.id not in targets):
            raise UserError('Not all generated accounting entries were recreated; rolled back.')

    def _record_details(self, record):
        result = super()._record_details(record)
        if record._name == 'account.move':
            partials = record.line_ids.matched_debit_ids | record.line_ids.matched_credit_ids
            if partials:
                result['reconciliations'] = [[p.id,p.debit_move_id.id,p.credit_move_id.id,p.amount,
                    p.debit_amount_currency,p.credit_amount_currency,p.exchange_move_id.id,p.full_reconcile_id.id,str(p.max_date)]
                    for p in partials.sorted('id')]
        if record._name == 'account.move' and record.tax_cash_basis_rec_id:
            result['cash_basis'] = [record.tax_cash_basis_rec_id.id, record.tax_cash_basis_origin_move_id.id]
        return result
