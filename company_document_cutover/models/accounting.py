"""Preserve native analytic, fiscal-position and rounding configurations."""
from odoo import fields, models
from odoo.exceptions import UserError
from .migration import packed


class AccountingHistory(models.Model):
    _inherit = 'company.document.cutover'

    def _distribution(self, line, source=False):
        result = {}
        for key, percentage in (line.analytic_distribution or {}).items():
            try:
                ids = [int(part) for part in key.split(',')]
            except (ValueError, AttributeError) as exc:
                raise UserError('Analytic distribution has an invalid account reference: %s.' % line.move_id.display_name) from exc
            accounts = self.env['account.analytic.account'].browse(ids).exists()
            if len(accounts) != len(ids):
                raise UserError('An analytic account used by %s was deleted.' % line.move_id.display_name)
            mapped = [self._target(a).id if source else a.id for a in accounts]
            if len(mapped) != len(set(mapped)):
                raise UserError('Different analytic accounts cannot collapse into one account in a distribution.')
            canonical = ','.join(map(str, sorted(mapped)))
            result[canonical] = result.get(canonical, 0) + percentage
        return result

    def _target(self, record):
        target = super()._target(record)
        if not record:
            return target
        if record._name == 'account.analytic.account':
            if record.plan_id.root_id != target.plan_id.root_id and self._explicit(record.plan_id.root_id) != target.plan_id.root_id:
                raise UserError('Analytic account must belong to the matching destination plan.')
            if record.currency_id != target.currency_id or not target.active:
                raise UserError('Analytic account currency or active setting differs.')
        if record._name == 'account.fiscal.position':
            if (record.foreign_vat or '') != (target.foreign_vat or ''):
                raise UserError('Fiscal position foreign tax registration differs.')
            def rules(position, original):
                get = self._target if original else lambda r:r
                return (sorted((get(r.account_src_id).id, get(r.account_dest_id).id) for r in position.account_ids),
                        sorted((get(t).id, tuple(sorted(get(o).id for o in t.original_tax_ids))) for t in position.tax_ids))
            if rules(record, True) != rules(target, False):
                raise UserError('Fiscal position account or tax mapping differs.')
        if record._name == 'account.cash.rounding' and any(record[n] != target[n] for n in ('rounding', 'rounding_method', 'strategy')):
            raise UserError('Cash rounding precision, method or strategy differs.')
        return target

    def _rounding_accounts(self, rounding, company, source=False):
        if not rounding:
            return []
        configured = rounding.with_company(company)
        get = self._target if source else lambda r:r
        return [get(configured[n]).id for n in ('profit_account_id', 'loss_account_id')]

    def _move_checks(self, move, all_moves, represented=None):
        self._target(move.fiscal_position_id)
        target_rounding = self._target(move.invoice_cash_rounding_id)
        if self._rounding_accounts(move.invoice_cash_rounding_id, move.company_id, True) != self._rounding_accounts(target_rounding, self.target_company_id):
            raise UserError('Cash rounding gain/loss accounts do not match.')
        for line in move.line_ids:
            self._distribution(line, True)
        return super()._move_checks(move, all_moves, represented)

    def _line_key(self, line, source=False):
        return packed([super()._line_key(line, source), self._distribution(line, source)])

    def _analytic_evidence(self, line, source=False):
        get = self._target if source else lambda r:r
        fnames = self.env['account.analytic.line']._get_plan_fnames()
        return sorted(packed([round(a.amount,8), str(a.date), a.unit_amount,
            sorted(get(a[n]).id for n in fnames if a[n]),
            get(a.general_account_id).id, get(a.product_id).id, get(a.partner_id).id]) for a in line.analytic_line_ids)

    def _line_map(self, source, target):
        result = super()._line_map(source, target)
        for line in source.line_ids:
            new = self.env['account.move.line'].browse(result[str(line.id)])
            if self._analytic_evidence(line, True) != self._analytic_evidence(new):
                raise UserError('Native analytic amounts or account allocations differ for %s.' % source.display_name)
        return result

    def _copy_move(self, source):
        result = super()._copy_move(source)
        target = result[0]
        if target.fiscal_position_id != self._target(source.fiscal_position_id) or target.invoice_cash_rounding_id != self._target(source.invoice_cash_rounding_id):
            raise UserError('Native posting changed fiscal position or cash rounding; rolled back.')
        return result

    def _invoice_values(self, move):
        values = super()._invoice_values(move)
        values.update({'fiscal_position_id': self._target(move.fiscal_position_id).id,
                       'invoice_cash_rounding_id': self._target(move.invoice_cash_rounding_id).id})
        for line, command in zip(move.invoice_line_ids, values['invoice_line_ids']):
            if line.display_type not in ('line_section', 'line_note'):
                command[2]['analytic_distribution'] = self._distribution(line, True)
        return values

    def _entry_lines(self, move):
        values = super()._entry_lines(move)
        for line, command in zip(move.line_ids, values):
            command[2]['analytic_distribution'] = self._distribution(line, True)
        return values

    def _record_details(self, record):
        values = super()._record_details(record)
        if record._name == 'account.move':
            if record.fiscal_position_id or record.invoice_cash_rounding_id or any(l.analytic_distribution for l in record.line_ids):
                values['native_configuration'] = {
                    'position':record.fiscal_position_id.id, 'rounding':record.invoice_cash_rounding_id.id,
                    'analytics':[self._analytic_evidence(l) for l in record.line_ids]}
        return values

    def _mapping_config(self, record):
        if record._name == 'account.fiscal.position':
            return [record.read(['foreign_vat', 'tax_ids', 'write_date'])[0],
                    record.account_ids.read(['account_src_id', 'account_dest_id']),
                    record.tax_ids.read(['original_tax_ids'])]
        if record._name == 'account.cash.rounding':
            return [record.with_company(c).read(['rounding','rounding_method','strategy','profit_account_id','loss_account_id'])[0]
                    for c in (self.source_company_ids | self.target_company_id).sorted('id')]
        if record._name == 'account.analytic.account':
            return record.read(['active','company_id','currency_id','plan_id','root_plan_id','write_date'])[0]
        if record._name == 'account.analytic.plan':
            return record.read(['parent_id','default_applicability','write_date'])[0]
        return super()._mapping_config(record)

    def _lock_tables(self):
        return (*super()._lock_tables(), 'account_analytic_account', 'account_analytic_plan', 'account_analytic_line',
                'account_fiscal_position', 'account_fiscal_position_account', 'account_fiscal_position_account_tax_rel', 'account_cash_rounding')


class AccountingHistoryMapping(models.Model):
    _inherit = 'company.document.mapping'
    _accounting_choices = [('account.analytic.account','Analytic account'), ('account.analytic.plan','Analytic plan'),
                           ('account.fiscal.position','Fiscal position'), ('account.cash.rounding','Cash rounding')]
    source_record = fields.Reference(selection_add=_accounting_choices, ondelete={k:'cascade' for k,v in _accounting_choices})
    target_record = fields.Reference(selection_add=_accounting_choices, ondelete={k:'cascade' for k,v in _accounting_choices})
    model_name = fields.Selection(selection_add=_accounting_choices, ondelete={k:'cascade' for k,v in _accounting_choices})
