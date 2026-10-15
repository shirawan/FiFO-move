"""Bounded native full-history replay. Unsupported evidence always blocks."""
import hashlib
import json
from collections import Counter
from psycopg2.errors import LockNotAvailable
from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG
from odoo.addons.company_financial_cutover.models.cutover import FinancialCutover

_TOKEN = object()
LINK = 'company_document_cutover.link.'
SOURCE = 'company_document_cutover.source.'


def packed(value):
    return json.dumps(value, sort_keys=True, default=str, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def remove_optional_document_matches(records):
    """Native uninstall removes only addon-owned choices, retaining job receipts."""
    if not records.env.is_system() or not records.env.context.get(MODULE_UNINSTALL_FLAG):
        raise AccessError('Optional choices may be removed only during native addon uninstall.')
    return records.with_context(_document_capability=_TOKEN).unlink()


class DocumentJob(models.Model):
    _name = 'company.document.cutover'
    _description = 'Accounting document migration candidate'
    _rec_name = 'name'

    name = fields.Char(default='Document move', required=True)
    source_company_ids = fields.Many2many('res.company', string='Old company and branches', required=True)
    target_company_id = fields.Many2one('res.company', string='New company', required=True)
    cutover_date = fields.Date(required=True, default=fields.Date.context_today)
    same_legal_entity = fields.Boolean(string='Accountant verified the same legal entity and tax registration')
    existing_data_reviewed = fields.Boolean(string='Accountant reviewed existing new-company data for earlier transfers')
    old_operations_frozen = fields.Boolean(string='Old-company fulfilment will be stopped before new commitments are used')
    include_accounting = fields.Boolean(string='Original invoices, bills, credits and accounting history', default=True)
    mapping_ids = fields.One2many('company.document.mapping', 'job_id', string='Explicit matches', copy=False)
    batch_size = fields.Integer(string='Records per step', default=100, required=True)
    record_limit = fields.Integer(string='Maximum records to review', default=10000, required=True)
    allow_incremental = fields.Boolean(string='Include additional history after an earlier move')
    overlap_ids = fields.One2many('company.document.overlap', 'job_id', string='Separate transactions', copy=False)
    state = fields.Selection([('draft', 'Choose'), ('blocked', 'Choices needed'), ('ready', 'Review'),
                              ('failed', 'Failed safely'), ('partial', 'Next step'), ('done', 'Result')], default='draft', readonly=True, copy=False)
    report = fields.Text(readonly=True, copy=False)
    preview_hash = fields.Char(readonly=True, copy=False)
    audit_attachment_id = fields.Many2one('ir.attachment', readonly=True, copy=False, ondelete='set null')

    def _operator(self):
        self.check_access('write')
        if not (self.env.user.has_group('base.group_system') and self.env.user.has_group('account.group_account_manager')):
            raise AccessError('A Settings and Accounting administrator must run document migration.')
        for job in self:
            if not (job.source_company_ids | job.target_company_id) <= self.env.companies:
                raise AccessError('Enable all selected old companies and the new company.')

    @api.model_create_multi
    def create(self, values):
        if any({'state', 'report', 'preview_hash', 'audit_attachment_id'} & set(v) for v in values):
            raise AccessError('Migration results are system managed.')
        jobs = super().create(values)
        jobs._operator()
        return jobs

    def _system_write(self, values):
        return self.with_context(_document_capability=_TOKEN).write(values)

    def write(self, values):
        self._operator()
        if self.env.context.get('_document_capability') is not _TOKEN:
            if {'state', 'report', 'preview_hash', 'audit_attachment_id'} & set(values):
                raise AccessError('Migration results are system managed.')
            if self.filtered(lambda j: j.state == 'done'):
                raise UserError('Completed jobs are read-only. Native documents and saved evidence remain available.')
            values = {**values, 'state': 'draft', 'preview_hash': False, 'report': False}
        return super().write(values)

    def unlink(self):
        self._operator()
        if self.filtered(lambda j: j.state == 'done'):
            raise UserError('Retain completed migration receipts.')
        return super().unlink()

    def _key(self, record):
        return LINK + '%s.%s.%s' % (record._name, record.id, self.target_company_id.id)

    def _stored_link(self, record):
        raw = self.env['ir.config_parameter'].sudo().get_param(self._key(record))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            target = self.env[record._name].browse(data['target']).exists()
            target.check_access('read')
            if record._name == 'account.move':
                self._check_external_target(target)
            if (len(target) != 1 or target.company_id != self.target_company_id or
                    record.company_id not in self.source_company_ids or data.get('source') != record.id or data.get('model') != record._name):
                raise UserError('Saved document ownership or company differs. Review the original receipt before reuse.')
            if data['source_hash'] != self._fingerprint(record) or data['target_hash'] != self._fingerprint(target):
                raise UserError('Saved migration evidence changed or a native document was deleted: %s.' % record.display_name)
            if 'source_details' in data:
                original, destination = self._record_details(record), self._record_details(target)
                if data.get('evidence_version', 3) < 4:
                    # Preserve compatibility with the smaller pre-4.0 evidence schema.
                    original = {key: original.get(key) for key in data['source_details']}
                    destination = {key: destination.get(key) for key in data['target_details']}
                if data['source_details'] != original or data['target_details'] != destination:
                    raise UserError('Saved document reference, reconciliation or purchase billing settings changed; review the migration evidence.')
            if record._name == 'account.move' and record.is_invoice(include_receipts=True) and record.payment_reference != target.payment_reference:
                raise UserError('An earlier transfer has a different invoice payment reference. Review the existing copy; no second copy will be made.')
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise UserError('Saved migration link is damaged; accountant review is needed.') from exc

    def _explicit(self, record):
        rows = self.mapping_ids.filtered(lambda r: r.model_name == record._name and r.source_id == record.id)
        if len(rows) != 1 or not rows.target_id:
            raise UserError('Choose an explicit match for %s #%s (%s).' % (record._name, record.id, record.display_name))
        target = self.env[record._name].browse(rows.target_id).exists()
        if len(target) != 1:
            raise UserError('The selected destination match no longer exists.')
        target.check_access('read')
        companies = target.company_ids if 'company_ids' in target._fields else target.company_id if 'company_id' in target._fields else self.env['res.company']
        if companies and self.target_company_id not in companies:
            raise UserError('Destination match belongs to another company: %s.' % target.display_name)
        return target

    def _target(self, record):
        if not record:
            return record
        if record._name in {'res.currency', 'uom.uom'}:
            record.check_access('read')
            return record
        target = self._explicit(record)
        if record._name == 'res.partner' and record.vat and ''.join(record.vat.upper().split()) != ''.join((target.vat or '').upper().split()):
            raise UserError('Contact tax identifier differs or is missing: %s.' % record.display_name)
        if record._name == 'product.product' and (record.type != target.type or record.uom_id != target.uom_id):
            raise UserError('Product type or unit differs: %s.' % record.display_name)
        if record._name == 'account.account':
            if target.account_type != record.account_type or target.reconcile != record.reconcile or not target.active or target.currency_id != record.currency_id:
                raise UserError('Account type, reconciliation or active setting differs: %s.' % record.display_name)
        if record._name == 'account.journal' and (target.type != record.type or not target.active or target.currency_id != record.currency_id):
            raise UserError('Journal type, currency or active setting differs: %s.' % record.display_name)
        if record._name == 'account.tax':
            names = ['amount', 'amount_type', 'type_tax_use', 'price_include_override', 'include_base_amount', 'is_base_affected', 'tax_exigibility']
            if any(record[n] != target[n] for n in names):
                raise UserError('Tax configuration differs or needs unsupported cash-basis history: %s.' % record.display_name)
            if record.tax_exigibility == 'on_payment' and self._target(record.cash_basis_transition_account_id) != target.cash_basis_transition_account_id:
                raise UserError('Cash-basis transition account differs.')
            if record.amount_type == 'group':
                if sorted(self._target(t).id for t in record.children_tax_ids) != sorted(target.children_tax_ids.ids):
                    raise UserError('Grouped tax children differ.')
            for field in ('invoice_repartition_line_ids', 'refund_repartition_line_ids'):
                def rep(tax, original):
                    return sorted((r.repartition_type, r.factor_percent,
                        self._target(r.account_id).id if original and r.account_id else r.account_id.id,
                        sorted(r.tag_ids.ids)) for r in tax[field])
                if rep(record, True) != rep(target, False):
                    raise UserError('Tax repartition accounts, factors or reporting tags differ.')
        return target

    def _fingerprint(self, record):
        # ORM reads under the same maintenance locks as the final plan.
        if record._name == 'account.move':
            data = {'header': record.read(['company_id', 'journal_id', 'move_type', 'name', 'ref', 'date', 'invoice_date',
                'invoice_date_due', 'invoice_payment_term_id', 'invoice_origin', 'currency_id', 'partner_id', 'state',
                'amount_total', 'amount_residual', 'payment_state', 'reversed_entry_id'])[0],
                'lines': record.line_ids.read(['account_id', 'partner_id', 'currency_id', 'balance', 'amount_currency',
                    'amount_residual', 'amount_residual_currency', 'date_maturity', 'tax_ids', 'tax_line_id',
                    'display_type', 'name', 'sequence', 'quantity', 'price_unit', 'discount', 'product_id', 'product_uom_id', 'tax_tag_ids', 'tax_repartition_line_id',
                    'analytic_distribution', 'matched_debit_ids', 'matched_credit_ids'])}
            if 'purchase_line_id' in record.line_ids._fields:
                data['purchase_links'] = record.line_ids.read(['purchase_line_id'])
            return digest(data)
        if record._name == 'account.payment':
            return digest(record.read(['company_id', 'journal_id', 'date', 'state', 'amount', 'currency_id',
                'partner_id', 'payment_type', 'partner_type', 'payment_method_line_id', 'move_id',
                'invoice_ids', 'payment_reference', 'memo'])[0])
        return digest(record.read()[0])

    @api.constrains('batch_size', 'record_limit')
    def _check_limits(self):
        if any(not 1 <= j.batch_size <= 1000 or not j.batch_size <= j.record_limit <= 50000 for j in self):
            raise ValidationError('Choose 1–1,000 records per step and a review limit from that size to 50,000.')

    def _moves(self):
        if not self.include_accounting:
            return self.env['account.move']
        moves = self.env['account.move'].search([('company_id', 'in', self.source_company_ids.ids),
            ('date', '<=', self.cutover_date), ('state', 'in', ['draft', 'posted'])], order='date,id', limit=self.record_limit + 1)
        if len(moves) > self.record_limit:
            raise UserError('Accounting history exceeds the review limit (%s). Increase Maximum records to review after a restored-copy timing check.' % self.record_limit)
        return moves.filtered(lambda m: m.state == 'posted' or m.is_invoice(include_receipts=True))

    def _move_checks(self, move, all_moves, represented=None):
        self._target(move.journal_id)
        self._target(move.partner_id)
        if move.invoice_payment_term_id:
            self._target(move.invoice_payment_term_id)
        for line in move.line_ids:
            self._target(line.account_id)
            self._target(line.partner_id)
            self._target(line.product_id)
            for tax in line.tax_ids | line.tax_line_id:
                self._target(tax)
            if 'purchase_line_id' in line._fields and line.purchase_line_id:
                self._check_purchase_line(line, all_moves)
        self._check_stock_history(move)
        if getattr(move, 'transaction_ids', False):
            raise UserError('Recorded payment-provider history needs a provider-specific, read-only adoption adapter: %s. No online payment will be created or charged.' % move.display_name)
        if getattr(move, 'edi_document_ids', False):
            raise UserError('Electronic invoicing history requires localization review: %s.' % move.display_name)
        if getattr(self._target(move.journal_id), 'edi_format_ids', False) and not self._is_reused(move):
            raise UserError('The new-company journal would start electronic-invoice processing for %s. Use a verified existing document or review the localization before copying.' % move.display_name)
        if move.move_type == 'entry' and not move.tax_cash_basis_rec_id and (move.line_ids.tax_ids or move.line_ids.tax_line_id or move.line_ids.tax_tag_ids):
            raise UserError('Tax-reporting manual journal entries require repartition/tag mapping: %s.' % move.display_name)
        partials = move.line_ids.matched_debit_ids | move.line_ids.matched_credit_ids
        if (partials.debit_move_id.move_id | partials.credit_move_id.move_id) - all_moves:
            raise UserError('Reconciliation reaches outside cutover coverage; historical residual reconstruction is not implemented: %s.' % move.display_name)
        if move.reversed_entry_id and move.reversed_entry_id not in all_moves:
            raise UserError('Credit-note original is outside selected history.')
        journal = self._target(move.journal_id)
        if not self.mapping_ids.filtered(lambda r: r.model_name == 'account.move' and r.source_id == move.id) and not self._stored_link(move) and move.state == 'posted' and self.target_company_id._get_violated_lock_dates(move.date, bool(move.line_ids.tax_ids | move.line_ids.tax_line_id), journal):
            raise UserError('Destination locked date affects %s; unlock only after accountant approval.' % move.display_name)
        payment = move.origin_payment_id
        if payment:
            if (payment.payment_method_id.code != 'manual' or payment.paired_internal_transfer_payment_id or
                    getattr(payment, 'payment_transaction_id', False) or getattr(payment, 'payment_token_id', False) or
                    getattr(move, 'transaction_ids', False) or payment.state not in ('in_process', 'paid')):
                raise UserError('Only posted, journal-backed manual accounting payments are supported: %s.' % payment.display_name)
            methods = self.env['account.payment.method.line'].search([('journal_id', '=', journal.id),
                ('payment_method_id.code', '=', 'manual'), ('payment_type', '=', payment.payment_type)])
            if len(methods) != 1 or methods.payment_account_id != self._target(payment.outstanding_account_id):
                raise UserError('Choose exactly one manual destination payment method with the mapped outstanding account.')
            if payment.invoice_ids - all_moves:
                raise UserError('Payment invoice links reach outside migration coverage.')
        stored = self._stored_link(move)
        existing = self.env['account.move'].browse(stored['target']) if stored else self.env['account.move']
        rows = self.mapping_ids.filtered(lambda r: r.model_name == 'account.move' and r.source_id == move.id)
        if rows and not existing:
            existing, _ = self._adoption(move)
        if represented is None:
            represented = existing
        # Numbering often changes during imports. Compare business content as well.
        identifiers = [v for v in (move.name if move.name != '/' else None, move.ref) if v]
        date_domain = [('invoice_date', '=', move.invoice_date)] if move.is_invoice(include_receipts=True) else [('date', '=', move.date)]
        candidates = self.env['account.move'].search([
            ('company_id', '=', self.target_company_id.id), ('move_type', '=', move.move_type),
            ('state', '!=', 'cancel'), ('currency_id', '=', move.currency_id.id),
            ('partner_id', '=', self._target(move.partner_id).id),
            ('amount_total', '=', move.amount_total)] + date_domain) - represented
        source_content = self._overlap_content(move, source=True)
        suspicious = candidates.filtered(lambda r: r.name in identifiers or (r.ref and r.ref in identifiers)
                                         or self._overlap_content(r) == source_content)
        confirmed = self.overlap_ids.filtered(lambda r: r.source_move_id == move and r.separate and r.reason.strip())
        unresolved = suspicious - confirmed.target_move_id
        if unresolved:
            raise UserError('Possible existing document duplicate for %s: %s. Review each pair. If they are genuinely separate transactions, record the reason under Separate transactions. Existing copies must not be imported again.' %
                            (move.display_name, ', '.join(unresolved.mapped('display_name'))))

    def _overlap_content(self, move, source=False):
        get = self._target if source else lambda r: r
        # Invoice identity is commercial: importing into a later open period can
        # change ledger dates, accounts and FX balances without creating a new sale.
        if move.is_invoice(include_receipts=True):
            return sorted(packed([get(l.product_id).id, l.product_uom_id.id, l.quantity,
                l.price_unit, l.discount, sorted(get(t).id for t in l.tax_ids)])
                for l in move.invoice_line_ids if l.display_type not in ('line_section', 'line_note'))
        # Generic entries use ledger content; never auto-reuse.
        return sorted(packed([get(l.account_id).id, get(l.product_id).id,
            l.product_uom_id.id, l.quantity, l.price_unit, l.discount, l.currency_id.id,
            round(l.balance, 8), round(l.amount_currency, 8), l.display_type,
            sorted(get(t).id for t in l.tax_ids), get(l.tax_line_id).id]) for l in move.line_ids)

    def _record_details(self, record):
        return {'payment_reference': record.payment_reference} if record._name == 'account.move' else {}

    def _check_purchase_line(self, line, all_moves):
        raise UserError('Bill/PO line links need the received/billed purchase-history mapping dependency: %s.' % line.move_id.display_name)

    def _check_stock_history(self, move):
        if getattr(move, 'stock_move_ids', False):
            raise UserError('Stock valuation accounting needs verified existing history. Current stock may already be represented in new-company opening balances; do not add its original valuation entries again: %s.' % move.display_name)

    def _check_external_target(self, target):
        if (getattr(target, 'transaction_ids', False) or getattr(target, 'edi_document_ids', False) or
                getattr(target.origin_payment_id, 'payment_transaction_id', False) or
                getattr(target.origin_payment_id, 'payment_token_id', False) or
                target.origin_payment_id.paired_internal_transfer_payment_id):
            raise UserError('Existing provider, internal-transfer or electronic-invoice history needs a specialized read-only adoption adapter before reuse.')

    def _mapping_config(self, record):
        if record._name == 'account.move':
            return [self._fingerprint(record), self._record_details(record)]
        return record.read(['write_date'])[0]

    def _extra_plan(self, issues):
        return {}

    def _plan(self):
        self.ensure_one()
        self._operator()
        self.overlap_ids._guard()
        issues = []
        source, target = self.source_company_ids, self.target_company_id
        if source.sudo().all_child_ids - source.sudo():
            issues.append('Selected old-company scope omits a branch. Include and enable every branch before moving the root company.')
        if not source or target in source or target.parent_id or target.all_child_ids:
            issues.append('Choose old companies/branches and a different standalone new company.')
        if not self.same_legal_entity:
            issues.append('Accountant must verify the same legal entity and tax registration before document posting.')
        if any(c.currency_id != target.currency_id or (c.root_id.vat or '') != (target.vat or '') or
               c.root_id.account_fiscal_country_id != target.account_fiscal_country_id or c.root_id.compute_fiscalyear_dates(self.cutover_date) != target.compute_fiscalyear_dates(self.cutover_date) for c in source):
            issues.append('Accounting currency, tax registration, fiscal country or fiscal year settings differ.')
        if self.cutover_date > fields.Date.context_today(self):
            issues.append('Choose a cutover date through today.')
        if self.env['account.move'].search_count([('company_id', '=', target.id)]) and not self.existing_data_reviewed:
            issues.append('Accountant must review existing destination documents, payments and earlier transfers.')
        Parameter = self.env['ir.config_parameter'].sudo()
        claims={}
        if not self.include_accounting and not getattr(self, 'include_purchase', False):
            issues.append('Select accounting documents or purchase orders to move.')
        if self.include_accounting:
            for company in source:
                old = Parameter.get_param('company_financial_cutover.completed.source.%s' % company.id)
                claim = Parameter.get_param(SOURCE + str(company.id))
                if old and not claim:
                    issues.append('Opening balances already represent this source; do not add full historical entries on top.')
                if claim:
                    data = json.loads(claim)
                    claims[company.id]=data
                    if data['target'] != target.id or data['cutover'] > str(self.cutover_date):
                        issues.append('This source belongs to another destination or a later completed date.')
                    elif data['cutover'] != str(self.cutover_date) and not self.allow_incremental:
                        issues.append('Select Include additional history to extend the earlier cutover date.')
        try:
            moves = self._moves()
        except UserError as exc:
            issues.append(str(exc)); moves = self.env['account.move']
        for company_id,claim in claims.items():
            previous = set(claim.get('documents', []))
            current = set(moves.filtered(lambda m:m.company_id.id==company_id).ids)
            if previous - current:
                issues.append('Previously moved source documents are missing from coverage. Restore or review them before continuing.')
            elif previous != current and claim.get('complete', True) and not self.allow_incremental:
                issues.append('Source document coverage changed after completion. Select Include additional history after accountant review.')
        payments = self.env['account.payment'].search([('company_id', 'in', source.ids), ('date', '<=', self.cutover_date),
            ('state', 'not in', ['canceled', 'rejected'])]) if self.include_accounting else self.env['account.payment']
        if payments.filtered(lambda p: not p.move_id or p.move_id not in moves):
            issues.append('Payments without a covered native journal entry are not implemented.')
        represented = self.env['account.move']
        for item in moves:
            try:
                if data := self._stored_link(item):
                    represented |= self.env['account.move'].browse(data['target'])
            except UserError as exc:
                issues.append(str(exc))
        represented |= self.env['account.move'].browse(self.mapping_ids.filtered(lambda r: r.model_name == 'account.move' and r.source_id in moves.ids).mapped('target_id'))
        for move in moves:
            try:
                self._move_checks(move, moves, represented)
            except UserError as exc:
                issues.append(str(exc))
        try:
            self._check_existing_reconciliations(moves)
            self._check_read_only_history(moves)
        except UserError as exc:
            issues.append(str(exc))
        extra = self._extra_plan(issues)
        def evidence(record, reader):
            try:
                return reader(record)
            except UserError as exc:
                issues.append(str(exc))
                return {'unavailable': str(exc)}
        # Digest all input mappings, native configuration and pre-existing destination ledger.
        configs = {}
        for row in self.mapping_ids:
            for side in ('source_id', 'target_id'):
                r = self.env[row.model_name].browse(row[side]).exists()
                if r:
                    r.check_access('read')
                    names = {
                        'account.account': ['name','code','active','account_type','reconcile','company_ids','currency_id'],
                        'account.journal': ['name','code','type','company_id','currency_id','active','default_account_id'],
                        'account.tax': ['name','company_id','amount','amount_type','type_tax_use','price_include_override',
                            'include_base_amount','is_base_affected','tax_exigibility','cash_basis_transition_account_id','children_tax_ids','invoice_repartition_line_ids','refund_repartition_line_ids'],
                        'res.partner': ['name','vat','ref','company_id','active'],
                        'product.product': ['name','company_id','active','type','uom_id','purchase_ok','taxes_id','supplier_taxes_id'],
                        'account.payment.term': ['name','company_id','line_ids'],
                        'account.move': None,
                    }.get(row.model_name)
                    configs['%s:%s' % (row.model_name, r.id)] = r.read(names + (['purchase_method'] if row.model_name == 'product.product' and 'purchase_method' in r._fields else []))[0] if names else evidence(r, self._mapping_config)
                    if row.model_name == 'account.tax':
                        configs['taxrep:%s' % r.id] = (r.invoice_repartition_line_ids | r.refund_repartition_line_ids).read(['repartition_type','factor_percent','account_id','tag_ids'])
                    if row.model_name == 'account.payment.term':
                        configs['terms:%s' % r.id] = r.line_ids.read(['value','value_amount','nb_days','delay_type','days_next_month'])
        destination = self.env['account.move'].search([('company_id', '=', target.id)])
        data = {'source': sorted(source.ids), 'target': target.id, 'cutover': str(self.cutover_date),
            'moves': [(m.id, evidence(m, self._fingerprint), evidence(m, self._record_details)) for m in moves],
            'payments': [(p.id, self._fingerprint(p)) for p in payments],
            'limits': [self.batch_size, self.record_limit, self.allow_incremental],
            'mappings': self.mapping_ids.read(['model_name', 'source_id', 'target_id']), 'config': configs,
            'companies': (source | target).sorted('id').read(['currency_id', 'vat', 'account_fiscal_country_id', 'fiscalyear_last_day',
                'fiscalyear_last_month', 'hard_lock_date', 'fiscalyear_lock_date', 'tax_lock_date', 'sale_lock_date', 'purchase_lock_date',
                'tax_exigibility', 'tax_cash_basis_journal_id', 'account_cash_basis_base_account_id',
                'currency_exchange_journal_id', 'income_currency_exchange_account_id', 'expense_currency_exchange_account_id']),
            'rates': self.env['res.currency.rate'].search([]).read(['name', 'company_id', 'currency_id', 'rate']),
            'overlaps': self.overlap_ids.read(['source_move_id', 'target_move_id', 'separate', 'reason']),
            'destination': [(m.id, evidence(m, self._fingerprint), evidence(m, self._record_details)) for m in destination], 'extra': extra}
        return moves, extra, sorted(set(issues)), digest(data)

    def action_review(self):
        self.ensure_one()
        if self.state == 'done':
            return True
        moves, extra, issues, signature = self._plan()
        totals = Counter()
        for m in moves.filtered(lambda m: m.state == 'posted'):
            for line in m.line_ids:
                totals[line.account_id.display_name] += line.balance
        valid_links = {}
        for move in moves:
            try:
                valid_links[move.id] = self._stored_link(move)
            except UserError as exc:
                issues.append(str(exc))
        issues = sorted(set(issues))
        overview = {'method': 'Full history replay; NO additional opening entry', 'through': str(self.cutover_date),
            'documents': dict(Counter((m.move_type, m.state).__str__() for m in moves)),
            'posted_account_balances': dict(totals), 'new': sum(not valid_links.get(m.id) and not bool(self.mapping_ids.filtered(lambda r:r.model_name=='account.move' and r.source_id==m.id)) for m in moves),
            'already_moved': sum(bool(valid_links.get(m.id)) for m in moves), 'purchase': extra}
        labels={'out_invoice':'Customer invoices','in_invoice':'Vendor bills','out_refund':'Customer credit notes',
            'in_refund':'Vendor credit notes','entry':'Other accounting entries','out_receipt':'Customer receipts','in_receipt':'Vendor receipts'}
        adopted = len(self.mapping_ids.filtered(lambda r:r.model_name=='account.move' and r.source_id in moves.ids and not valid_links.get(r.source_id)))
        details=['Original accounting history through %s — no additional opening entry.' % self.cutover_date,
            'New documents: %s. Already moved: %s. Reuse existing documents: %s.' % (overview['new'],overview['already_moved'],adopted),
            'Each confirmation targets %s records; connected payments and credits stay together and may exceed that size. Review again to continue the next step.' % self.batch_size]
        for (kind,state),count in sorted(Counter((m.move_type,m.state) for m in moves).items()):
            details.append('%s (%s): %s' % (labels.get(kind,kind),state,count))
        if totals:
            details.append('Account balances included in this history:')
            for account,amount in sorted(totals.items()):details.append('  %s: %.*f' % (account,self.target_company_id.currency_id.decimal_places,amount))
        for order in extra.get('orders',[]):
            details.append('Purchase %s (%s):' % (order['name'],order['state'] + ('; locked' if order.get('locked') else '')))
            if order.get('history_target'):
                details.append('  Reuse verified existing PO, receipts and returns; no new delivery commitment will be created.')
            if order.get('audit_only'):
                details.append('  History only: retain original documents and quantities for reference. Current stock stays unchanged. No live PO, receipt or supplier commitment will be created.')
            for line in order['lines']:
                details.append('  %s (%s): ordered %s; received %s; billed %s; ordered less net received %s; ordered less billed %s.' %
                    (line['description'],line['unit'],line['ordered'],line['received'],line['billed'],line['remaining_delivery'],line['remaining_billing']))
                if 'pending_receipt' in line:
                    details.append('    %sOpen receipt commitments: %s. Quantity available to bill now: %s. Returns do not automatically create replacement receipts.' % ('In the old company only — ' if order.get('audit_only') else '',line['pending_receipt'],line['billable_now']))
        self._system_write({'state': 'blocked' if issues else 'ready', 'preview_hash': signature if not issues else False,
            'report': ('Choices to resolve:\n' + '\n'.join('• ' + i for i in issues) + '\n\n' if issues else '') + '\n'.join(details)})
        return True

    def _lock_tables(self):
        tables = ('account_move', 'account_move_line', 'account_partial_reconcile', 'account_full_reconcile',
            'account_payment', 'account_payment_method_line', 'account_account', 'account_journal', 'res_partner',
            'res_company', 'res_currency', 'res_currency_rate', 'account_tax', 'account_tax_repartition_line',
            'account_payment_term', 'account_payment_term_line', 'product_product', 'product_template',
            'ir_config_parameter', 'ir_attachment', 'company_document_cutover', 'company_document_mapping', 'company_document_overlap')
        optional = tuple(self.env[model]._table for model in ('payment.transaction', 'payment.token', 'payment.provider',
            'account.edi.document', 'account.edi.format') if model in self.env)
        relations = tuple(self.env[model]._fields[name].relation for model, name in (
            ('account.move', 'transaction_ids'), ('account.journal', 'edi_format_ids'))
            if name in self.env[model]._fields)
        return (*tables, *optional, *relations)

    def _lock(self):
        self.env.flush_all()
        self.env.cr.execute('SELECT txid_current_snapshot()::text')
        snapshot = self.env.cr.fetchone()[0]
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(SQL('LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE NOWAIT',
                    SQL(', ').join(SQL.identifier(t) for t in self._lock_tables())))
        except LockNotAvailable as exc:
            raise UserError('Accounting is busy. Execute during the agreed maintenance window.') from exc
        FinancialCutover._assert_fresh_snapshot(self, snapshot)
        (self.source_company_ids | self.target_company_id).check_access('write')
        self.env.cr.execute('UPDATE res_company SET write_date=write_date WHERE id=ANY(%s)',
            [(self.source_company_ids | self.target_company_id).ids])
        self.env.cr.execute('UPDATE company_document_cutover SET write_date=write_date WHERE id=%s', [self.id])
        self.env.invalidate_all()

    def _line_key(self, line, source=False):
        get = self._target if source else lambda r: r
        invoice_detail = [line.name, get(line.product_id).id, line.product_uom_id.id, line.quantity, line.price_unit, line.discount, line.sequence] if line.move_id.is_invoice(include_receipts=True) and line.display_type in ('product', 'line_section', 'line_note') else []
        return packed([invoice_detail, sorted(line.tax_tag_ids.ids), get(line.account_id).id, get(line.partner_id).id, line.currency_id.id,
            round(line.balance, 8), round(line.amount_currency, 8), str(line.date_maturity or ''), line.display_type,
            sorted(get(t).id for t in line.tax_ids), get(line.tax_line_id).id])

    def _line_map(self, source, target):
        bucket = {}
        for line in target.line_ids:
            bucket.setdefault(self._line_key(line), []).append(line.id)
        result = {}
        for line in source.line_ids:
            key = self._line_key(line, True)
            if not bucket.get(key):
                raise UserError('Native computed amount/account/tax/due date differs for %s; no amounts were forced.' % source.display_name)
            result[str(line.id)] = bucket[key].pop(0)
        if any(bucket.values()):
            raise UserError('Destination has additional journal items for %s.' % source.display_name)
        return result

    def _invoice_values(self, move):
        lines = []
        for line in move.invoice_line_ids:
            vals = {'display_type': line.display_type, 'name': line.name, 'sequence': line.sequence}
            if line.display_type not in ('line_section', 'line_note'):
                vals.update({'quantity': line.quantity, 'price_unit': line.price_unit, 'discount': line.discount,
                    'product_id': self._target(line.product_id).id, 'product_uom_id': line.product_uom_id.id,
                    'account_id': self._target(line.account_id).id, 'tax_ids': [Command.set([self._target(t).id for t in line.tax_ids])]})
            lines.append(Command.create(vals))
        return {'payment_reference': move.payment_reference, 'invoice_date': move.invoice_date, 'invoice_date_due': move.invoice_date_due,
                'invoice_payment_term_id': self._target(move.invoice_payment_term_id).id,
                'invoice_origin': move.invoice_origin, 'invoice_line_ids': lines}

    def _entry_lines(self, move):
        return [Command.create({'name': line.name, 'account_id': self._target(line.account_id).id,
            'partner_id': self._target(line.partner_id).id, 'currency_id': line.currency_id.id,
            'debit': line.debit, 'credit': line.credit, 'amount_currency': line.amount_currency,
            'date_maturity': line.date_maturity, 'display_type': line.display_type,
            'tax_ids': [Command.set([self._target(t).id for t in line.tax_ids])]}) for line in move.line_ids]

    def _copy_move(self, source):
        journal = self._target(source.journal_id)
        common = {'company_id': self.target_company_id.id, 'journal_id': journal.id,
            'date': source.date, 'ref': source.ref, 'currency_id': source.currency_id.id,
            'partner_id': self._target(source.partner_id).id, 'move_type': source.move_type}
        payment = source.origin_payment_id
        native_payment = False
        if payment:
            method = self.env['account.payment.method.line'].search([('journal_id', '=', journal.id),
                ('payment_method_id.code', '=', 'manual'), ('payment_type', '=', payment.payment_type)])
            native_payment = self.env['account.payment'].with_company(self.target_company_id).create({
                'company_id': self.target_company_id.id, 'journal_id': journal.id, 'date': payment.date,
                'amount': payment.amount, 'currency_id': payment.currency_id.id,
                'partner_id': self._target(payment.partner_id).id, 'partner_type': payment.partner_type,
                'payment_type': payment.payment_type, 'payment_method_line_id': method.id,
                'destination_account_id': self._target(payment.destination_account_id).id,
                'memo': payment.memo, 'payment_reference': payment.payment_reference,
                'line_ids': self._entry_lines(source)})
            native_payment.write({'state': payment.state})
            target = native_payment.move_id
            target.write({'ref': source.ref})
        else:
            common.update(self._invoice_values(source) if source.is_invoice(include_receipts=True)
                          else {'line_ids': self._entry_lines(source)})
            target = self.env['account.move'].with_company(self.target_company_id).create(common)
            if source.state == 'posted':
                target.action_post()
        if target.date != source.date or target.state != source.state or target.currency_id != source.currency_id:
            raise UserError('Native posting changed date, state or currency; rollback instead of bypassing accounting checks.')
        if source.is_invoice(include_receipts=True) and target.payment_reference != source.payment_reference:
            # Posting can regenerate an intentionally blank customer reference.
            # Restore through native write so other modules' hooks still run.
            target.write({'payment_reference': source.payment_reference})
            if target.payment_reference != source.payment_reference:
                raise UserError('Native posting changed the original invoice payment reference; rolled back.')
        return target, self._line_map(source, target), native_payment

    def _save_link(self, source, target, line_map=None):
        data = {'evidence_version': 4, 'source': source.id, 'model': source._name, 'target': target.id, 'job': self.id,
            'source_company': source.company_id.id, 'target_company': self.target_company_id.id,
            'adopted_existing': bool(self.mapping_ids.filtered(lambda r:r.model_name==source._name and r.source_id==source.id and r.target_id==target.id)),
            'source_number': source.name, 'source_label': source.display_name, 'original_ref': source.ref if source._name=='account.move' else source.partner_ref if source._name=='purchase.order' else getattr(source, 'payment_reference', getattr(source, 'reference', False)), 'source_hash': self._fingerprint(source),
            'target_hash': self._fingerprint(target), 'source_details': self._record_details(source), 'target_details': self._record_details(target), 'line_map': line_map or {}, 'cutover': str(self.cutover_date)}
        self.env['ir.config_parameter'].sudo().set_param(self._key(source), packed(data))
        return data

    def _extra_execute(self, extra, audit):
        return None

    def action_execute(self):
        self.ensure_one()
        self._operator()
        if self.state == 'done':
            # The immutable attachment and durable links outlive module uninstall.
            payload = json.loads(self.audit_attachment_id.with_context(bin_size=False).raw)
            for data in payload['links']:
                original=self.env[data['model']].browse(data['source']).exists()
                if len(original)!=1:
                    raise UserError('Source record was removed after migration; retain and review the saved receipt.')
                self._stored_link(original)
            return True
        try:
            with self.env.cr.savepoint():
                self._lock()
                moves, extra, issues, signature = self._plan()
                if issues:
                    raise UserError('\n'.join(issues))
                if self.state != 'ready' or self.preview_hash != signature:
                    raise UserError('Source, destination or setup changed. Review the move again before confirming.')
                all_moves = moves
                moves = self._execution_moves(all_moves)
                self = self._prepare_copies(moves)
                generated_moves = self._generated_moves(moves)
                targets, line_map, payments = {}, {}, {}
                for source in moves:
                    stored = self._stored_link(source)
                    if stored:
                        targets[source.id] = self.env['account.move'].browse(stored['target'])
                        line_map.update(stored['line_map'])
                    else:
                        if source in generated_moves and not self._is_reused(source):
                            continue
                        target, lines = self._adoption(source)
                        payment = False
                        if not target:
                            target, lines, payment = self._copy_move(source)
                        targets[source.id] = target
                        line_map.update(lines)
                        if payment:
                            payments[source.origin_payment_id.id] = payment
                self._after_copies(targets)
                for source in moves.filtered(lambda m:m.id in targets):
                    target = targets[source.id]
                    if source.reversed_entry_id:
                        original = targets[source.reversed_entry_id.id]
                        if target.reversed_entry_id != original:
                            if self._is_reused(source):
                                raise UserError('Existing credit note must already refer to the selected original invoice.')
                            target.write({'reversed_entry_id': original.id})
                    if source.origin_payment_id:
                        expected = self.env['account.move'].browse([targets[i.id].id for i in source.origin_payment_id.invoice_ids])
                        native = target.origin_payment_id
                        if native.invoice_ids - expected:
                            raise UserError('Existing payment links include an unrelated invoice; review that history before reuse.')
                        if native.invoice_ids != expected:
                            native.write({'invoice_ids': [Command.set(expected.ids)]})
                self._reconcile_moves(moves, targets, line_map)
                for source in moves:
                    for line in source.line_ids:
                        target_line = self.env['account.move.line'].browse(line_map[str(line.id)])
                        if not self.target_company_id.currency_id.is_zero(target_line.amount_residual - line.amount_residual) or \
                           not line.currency_id.is_zero(target_line.amount_residual_currency - line.amount_residual_currency):
                            raise UserError('Native residual differs for %s; rolled back.' % source.display_name)
                    if targets[source.id].payment_state != source.payment_state:
                        raise UserError('Native invoice payment state differs for %s; rolled back.' % source.display_name)
                audit = {'method': 'full-history replay, no additional opening', 'job': self.id,
                    'date': str(self.cutover_date), 'links': [],
                    'separate_transactions': self.overlap_ids.read(['source_move_id', 'target_move_id', 'separate', 'reason'])}
                for source in moves:
                    audit['links'].append(self._save_link(source, targets[source.id],
                        {str(l.id): line_map[str(l.id)] for l in source.line_ids}))
                    if source.origin_payment_id:
                        target_pay = targets[source.id].origin_payment_id
                        if target_pay.state != source.origin_payment_id.state:
                            raise UserError('Native payment state differs; rolled back.')
                        audit['links'].append(self._save_link(source.origin_payment_id, target_pay))
                remaining = all_moves.filtered(lambda m: not self._stored_link(m))
                if not remaining:
                    self._extra_execute(extra, audit)
                pending_purchase = self._remaining_extra(extra)
                complete = not remaining and not pending_purchase
                if self.include_accounting and all_moves:
                    for company in self.source_company_ids:
                        self.env['ir.config_parameter'].sudo().set_param(SOURCE + str(company.id), packed({
                            'target': self.target_company_id.id, 'cutover': str(self.cutover_date), 'job': self.id,
                            'documents': all_moves.filtered(lambda m:m.company_id==company and self._stored_link(m)).ids,
                            'complete': not remaining, 'reviewed_documents': all_moves.filtered(lambda m:m.company_id==company).ids}))
                        self.env['ir.config_parameter'].sudo().set_param('company_financial_cutover.completed.source.%s' % company.id,
                            packed({'document_job': self.id, 'date': str(self.cutover_date)}))
                audit['settings'] = self._recovery_settings()
                audit['mappings'] = self.mapping_ids.read(['model_name', 'source_id', 'target_id'])
                audit['complete'] = complete
                audit['links'] = self._recovery_links(all_moves, extra)
                attachment = self.env['ir.attachment'].sudo().create({'name': 'Accounting-document-move-%s.json' % self.id,
                    'type': 'binary', 'raw': packed(audit).encode(), 'mimetype': 'application/json',
                    'res_model': False, 'res_id': False, 'company_id': self.target_company_id.id, 'public': False})
                self.env['ir.config_parameter'].sudo().set_param('company_document_cutover.receipt.%s' % self.recovery_key, packed({'hash': digest(audit), 'target': self.target_company_id.id, 'attachment': attachment.id}))
                self._system_write({'state': 'done' if complete else 'partial', 'preview_hash': False, 'audit_attachment_id': attachment.id,
                    'report': ('Completed.' if complete else 'Step saved. %s accounting documents and %s purchase orders remain. Click Review my documents to continue.' % (len(remaining), pending_purchase)) + ' Original source records stayed unchanged.\nCopied or verified %s accounting documents and %s purchase orders.\nNo additional opening entry or validated delivery was created.\nThe saved receipt contains the original references and record matches.' % (len(audit['links']) - sum(l['model'] != 'account.move' for l in audit['links']),len(extra.get('orders',[])) - pending_purchase)})
        except (UserError, ValidationError) as exc:
            # get_param() caches can contain markers read inside the rolled-back step.
            self.env.registry.clear_cache('stable')
            self.env.invalidate_all()
            report='No records from this attempt were retained. Review these findings together:\n' + str(exc)
            failure=self.env['ir.attachment'].sudo().create({'name':'Document-move-%s-failed.json' % self.id,
                'type':'binary','raw':packed({'job':self.id,'status':'FAILED','error':str(exc),
                    'sources':self.source_company_ids.ids,'target':self.target_company_id.id,'when':fields.Datetime.now()}).encode(),
                'mimetype':'application/json','res_model':False,'res_id':False,'public':False,'company_id':self.target_company_id.id})
            self._system_write({'state': 'failed', 'preview_hash': False,'audit_attachment_id':failure.id,'report':report})
        return True


class DocumentMapping(models.Model):
    _name = 'company.document.mapping'
    _description = 'Explicit company document match'
    _unique_source = models.Constraint('UNIQUE(job_id,model_name,source_id)', 'Choose one destination per source.')
    job_id = fields.Many2one('company.document.cutover', required=True, ondelete='cascade')
    _choices = [('account.account', 'Account'), ('account.journal', 'Journal'), ('account.tax', 'Tax'),
        ('res.partner', 'Contact'), ('product.product', 'Product'), ('account.payment.term', 'Payment terms'), ('account.move', 'Existing accounting document')]
    source_record = fields.Reference(selection=_choices, string='Old-company record', required=True)
    target_record = fields.Reference(selection=_choices, string='New-company match', required=True)
    model_name = fields.Selection(_choices, compute='_compute_keys', store=True)
    source_id = fields.Integer(compute='_compute_keys', store=True)
    target_id = fields.Integer(compute='_compute_keys', store=True)

    @api.depends('source_record', 'target_record')
    def _compute_keys(self):
        for row in self:
            row.model_name = row.source_record._name if row.source_record else False
            row.source_id = row.source_record.id if row.source_record else 0
            row.target_id = row.target_record.id if row.target_record else 0

    def _guard(self):
        for row in self:
            row.job_id._operator()
            if row.job_id.state == 'done':
                raise UserError('Completed migration matches are read-only.')
            if not row.source_record or not row.target_record or row.source_record._name != row.target_record._name:
                raise UserError('Choose existing old and new records of the same kind.')
            source = self.env[row.model_name].browse(row.source_id).exists()
            if len(source) != 1:
                raise UserError('The source match no longer exists.')
            source.check_access('read')
            companies = source.company_ids if 'company_ids' in source._fields else source.company_id if 'company_id' in source._fields else self.env['res.company']
            if companies and not companies & row.job_id.source_company_ids:
                raise AccessError('Source match belongs outside selected old companies.')
            row.job_id._explicit(source)

    @api.model_create_multi
    def create(self, vals_list):
        if any({'model_name','source_id','target_id'} & set(v) for v in vals_list):
            raise AccessError('Match identities are derived from the selected records.')
        records = super().create(vals_list)
        records._guard()
        records.job_id._system_write({'state': 'draft', 'preview_hash': False})
        return records

    def write(self, values):
        if {'model_name','source_id','target_id'} & set(values):
            raise AccessError('Match identities are derived from the selected records.')
        self._guard()
        result = super().write(values)
        self._guard()
        self.job_id._system_write({'state': 'draft', 'preview_hash': False})
        return result

    def unlink(self):
        if self.env.context.get('_document_capability') is _TOKEN:
            return super().unlink()
        self._guard()
        self.job_id._system_write({'state': 'draft', 'preview_hash': False})
        return super().unlink()


class DocumentOverlap(models.Model):
    _name = 'company.document.overlap'
    _description = 'Accountant decision on similar documents'
    _unique_pair = models.Constraint('UNIQUE(job_id,source_move_id,target_move_id)', 'Review each document pair once.')
    job_id = fields.Many2one('company.document.cutover', required=True, ondelete='cascade')
    source_move_id = fields.Many2one('account.move', string='Old-company document', required=True, ondelete='restrict')
    target_move_id = fields.Many2one('account.move', string='Existing new-company document', required=True, ondelete='restrict')
    separate = fields.Boolean(string='These are separate transactions')
    reason = fields.Char(string='Why they are separate', required=True)

    def _guard(self):
        for row in self:
            row.job_id._operator()
            if row.job_id.state == 'done':
                raise UserError('Completed overlap decisions are read-only.')
            (row.source_move_id | row.target_move_id).check_access('read')
            if row.source_move_id.company_id not in row.job_id.source_company_ids or row.target_move_id.company_id != row.job_id.target_company_id:
                raise AccessError('Choose documents from the selected old companies and new company.')
            if not (row.reason or '').strip():
                raise UserError('Record why these are separate transactions.')

    @api.model_create_multi
    def create(self, values):
        rows = super().create(values)
        rows._guard()
        rows.job_id._system_write({'state': 'draft', 'preview_hash': False})
        return rows

    def write(self, values):
        self._guard()
        jobs = self.job_id
        result = super().write(values)
        self._guard()
        (jobs | self.job_id)._system_write({'state': 'draft', 'preview_hash': False})
        return result

    def unlink(self):
        self._guard()
        self.job_id._system_write({'state': 'draft', 'preview_hash': False})
        return super().unlink()
