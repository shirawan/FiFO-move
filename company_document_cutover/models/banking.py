"""Copy native historical statement records and their ledger reconciliations."""
import json
from odoo import fields, models
from .migration import packed
from odoo.exceptions import UserError


class BankHistory(models.Model):
    _inherit = 'company.document.cutover'

    def _move_checks(self, move, all_moves, represented=None):
        line = move.statement_line_id
        if line:
            if line.payment_ids:
                raise UserError('Statement-generated payment links need explicit existing-history adoption: %s.' % move.display_name)
            if line.statement_id and line.statement_id.line_ids.move_id - all_moves:
                raise UserError('Include the entire bank statement through the cutover date; it cannot be split across history windows.')
            self._target(line.partner_id)
        return super()._move_checks(move, all_moves, represented)

    def _dependency_edges(self, moves):
        result = super()._dependency_edges(moves)
        for statement in moves.statement_line_id.statement_id:
            ids = statement.line_ids.move_id.ids
            if ids:
                result += [(ids[0], ident) for ident in ids[1:]]
        return result

    def _prepare_copies(self, moves):
        prepared = {}
        statements = moves.filtered(lambda m: m.statement_line_id and not self._is_reused(m)).statement_line_id.statement_id
        for statement in statements:
            target = self.env['account.bank.statement'].with_company(self.target_company_id).create({
                'name': statement.name, 'reference': statement.reference, 'date': statement.date,
                'balance_start': statement.balance_start, 'balance_end_real': statement.balance_end_real})
            prepared[statement.id] = target.id
        return super()._prepare_copies(moves).with_context(_document_statements=prepared)

    def _copy_move(self, source):
        old = source.statement_line_id
        if not old:
            return super()._copy_move(source)
        statements = self.env.context.get('_document_statements', {})
        target_line = self.env['account.bank.statement.line'].with_company(self.target_company_id).create({
            'company_id': self.target_company_id.id, 'journal_id': self._target(source.journal_id).id,
            'date': source.date, 'ref': source.ref, 'partner_id': self._target(old.partner_id).id,
            'statement_id': statements.get(old.statement_id.id, False), 'payment_ref': old.payment_ref,
            'sequence': old.sequence, 'amount': old.amount, 'foreign_currency_id': old.foreign_currency_id.id,
            'amount_currency': old.amount_currency, 'partner_name': old.partner_name,
            'transaction_type': old.transaction_type, 'line_ids': self._entry_lines(source)})
        target = target_line.move_id
        if target.date != source.date or target.state != source.state or target.currency_id != source.currency_id:
            raise UserError('Native statement posting changed accounting date, currency or state.')
        return target, self._line_map(source, target), False

    def _adoption(self, source):
        target, lines = super()._adoption(source)
        if target:
            old, new = source.statement_line_id, target.statement_line_id
            if bool(old) != bool(new):
                raise UserError('Existing bank history must retain its native statement line.')
            if old and (old.amount != new.amount or old.foreign_currency_id != new.foreign_currency_id or
                        old.amount_currency != new.amount_currency or old.payment_ref != new.payment_ref):
                raise UserError('Existing native bank transaction details differ.')
            if new.payment_ids:
                raise UserError('Existing statement-generated payment links need explicit read-only history adoption before reuse.')
        return target, lines

    def _after_copies(self, targets):
        super()._after_copies(targets)
        for old_id, new_id in self.env.context.get('_document_statements', {}).items():
            old = self.env['account.bank.statement'].browse(old_id)
            new = self.env['account.bank.statement'].browse(new_id)
            new.write({'balance_start':old.balance_start,'balance_end_real':old.balance_end_real,'date':old.date})
            if (len(new.line_ids) != len(old.line_ids) or new.balance_end != old.balance_end or
                    new.balance_start != old.balance_start or new.balance_end_real != old.balance_end_real):
                raise UserError('Native bank statement totals differ; the step was rolled back.')
            self._save_link(old, new)

    def _fingerprint(self, record):
        if record._name == 'account.bank.statement':
            from .migration import digest
            return digest(record.read(['name','reference','date','journal_id','balance_start','balance_end_real','balance_end','line_ids'])[0])
        return super()._fingerprint(record)

    def _record_details(self, record):
        result = super()._record_details(record)
        if record._name == 'account.move' and record.statement_line_id:
            result['bank_statement'] = json.loads(packed(record.statement_line_id.read(['amount','amount_currency','foreign_currency_id',
                'statement_id','payment_ref','partner_id','sequence','payment_ids','is_reconciled'])[0]))
        return result

    def _recovery_links(self, moves, extra):
        result = super()._recovery_links(moves, extra)
        for statement in moves.statement_line_id.statement_id:
            if data := self._stored_link(statement):
                result.append(data)
        return result

    def _lock_tables(self):
        return (*super()._lock_tables(), 'account_bank_statement', 'account_bank_statement_line',
                'account_payment_account_bank_statement_line_rel')
