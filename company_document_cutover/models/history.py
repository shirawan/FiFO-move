"""Native document adoption, dependency-safe steps and recovery receipts."""
import json
import logging
import uuid
from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, UserError
from .migration import LINK, digest, packed

_logger = logging.getLogger(__name__)


class DocumentHistory(models.Model):
    _inherit = 'company.document.cutover'

    recovery_key = fields.Char(default=lambda self: str(uuid.uuid4()), copy=False, readonly=True)

    def _is_reused(self, source):
        return bool(self._stored_link(source) or self.mapping_ids.filtered(
            lambda r: r.model_name == 'account.move' and r.source_id == source.id))

    def _adoption(self, source):
        rows = self.mapping_ids.filtered(lambda r: r.model_name == 'account.move' and r.source_id == source.id)
        if not rows:
            return self.env['account.move'], {}
        target = self._explicit(source)
        aliases = self.mapping_ids.filtered(lambda r: r.model_name == 'account.move' and r.target_id == target.id)
        if len(aliases) != 1:
            raise UserError('An existing document can represent only one old-company document.')
        claims = self.env['ir.config_parameter'].sudo().search([('key', '=like', LINK + 'account.move.%')])
        for claim in claims:
            try:
                data = json.loads(claim.value)
                if not isinstance(data, dict):
                    raise ValueError()
            except (TypeError, ValueError) as exc:
                raise UserError('Saved document ownership evidence is damaged. Restore the original marker before adopting an existing document.') from exc
            if data.get('target') == target.id and data.get('source') != source.id:
                raise UserError('This existing document already represents a different old-company document.')
        if (target.company_id != self.target_company_id or target.move_type != source.move_type or
                target.state != source.state or target.date != source.date or
                target.invoice_date != source.invoice_date or target.invoice_date_due != source.invoice_date_due or
                target.currency_id != source.currency_id or target.partner_id != self._target(source.partner_id) or
                target.journal_id != self._target(source.journal_id) or
                target.invoice_payment_term_id != self._target(source.invoice_payment_term_id) or
                target.fiscal_position_id != self._target(source.fiscal_position_id) or
                target.invoice_cash_rounding_id != self._target(source.invoice_cash_rounding_id) or
                target.payment_reference != source.payment_reference):
            raise UserError('Existing document %s has different dates, state, partner, journal, currency, terms or payment reference. Review it before reuse.' % target.display_name)
        if bool(target.origin_payment_id) != bool(source.origin_payment_id):
            raise UserError('An existing payment must retain its native payment record, not only a journal entry.')
        if source.origin_payment_id:
            old, new = source.origin_payment_id, target.origin_payment_id
            if (old.amount != new.amount or old.currency_id != new.currency_id or old.state != new.state or
                    old.date != new.date or old.payment_type != new.payment_type or old.partner_type != new.partner_type or
                    new.partner_id != self._target(old.partner_id) or new.payment_method_id.code != 'manual'):
                raise UserError('Existing native payment details differ from the original payment.')
        return target, self._line_map(source, target)

    def _check_existing_reconciliations(self, moves):
        """Adoption never absorbs an unrelated destination payment or write-off."""
        reverse_lines = {}
        reverse_moves = {}
        targets = self.env['account.move']
        for source in moves:
            stored = self._stored_link(source)
            if stored:
                target, lines = self.env['account.move'].browse(stored['target']), stored['line_map']
            else:
                target, lines = self._adoption(source)
            targets |= target
            if target:
                reverse_moves[target.id] = source.id
            reverse_lines.update({int(new): int(old) for old, new in lines.items()})
        for source in moves.filtered(lambda m:m.origin_payment_id):
            matched = targets.filtered(lambda m:reverse_moves.get(m.id)==source.id)
            for invoice in matched.origin_payment_id.invoice_ids:
                if reverse_moves.get(invoice.id) not in source.origin_payment_id.invoice_ids.ids:
                    raise UserError('An existing payment links to an unrelated invoice; match its original history before reuse.')
        for partial in targets.line_ids.matched_debit_ids | targets.line_ids.matched_credit_ids:
            debit = reverse_lines.get(partial.debit_move_id.id)
            credit = reverse_lines.get(partial.credit_move_id.id)
            if not debit or not credit:
                raise UserError('An existing document is reconciled to a transaction outside the selected verified matches. Match that history before reuse.')
            source_debit = self.env['account.move.line'].browse(debit)
            matches = source_debit.matched_credit_ids.filtered(lambda p: p.credit_move_id.id == credit)
            if (len(matches) != 1 or not self.target_company_id.currency_id.is_zero(matches.amount - partial.amount) or
                    not partial.debit_currency_id.is_zero(matches.debit_amount_currency - partial.debit_amount_currency) or
                    not partial.credit_currency_id.is_zero(matches.credit_amount_currency - partial.credit_amount_currency)):
                raise UserError('Existing payment reconciliation differs from the original transaction. Reuse cannot change or absorb it.')

    def _execution_moves(self, moves):
        """Keep each reconciliation and credit-note component in one transaction."""
        graph = {m.id: set() for m in moves}
        for partial in moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids:
            a, b = partial.debit_move_id.move_id.id, partial.credit_move_id.move_id.id
            if a in graph and b in graph:
                graph[a].add(b); graph[b].add(a)
        for move in moves:
            if move.reversed_entry_id.id in graph:
                graph[move.id].add(move.reversed_entry_id.id)
                graph[move.reversed_entry_id.id].add(move.id)
        for a, b in self._dependency_edges(moves):
            if a in graph and b in graph:
                graph[a].add(b); graph[b].add(a)
        seen, selected = set(), set()
        for move in moves:
            if move.id in seen:
                continue
            component, pending = set(), [move.id]
            while pending:
                ident = pending.pop()
                if ident in component:
                    continue
                component.add(ident); pending.extend(graph[ident] - component)
            seen.update(component)
            records = moves.browse(sorted(component))
            if not any(not self._stored_link(r) for r in records):
                continue
            if selected and len(selected) + len(component) > self.batch_size:
                break
            selected.update(component)
            if len(selected) >= self.batch_size:
                break
        return moves.filtered(lambda m: m.id in selected)

    def _dependency_edges(self, moves):
        return [(m.id, invoice.id) for m in moves if m.origin_payment_id for invoice in m.origin_payment_id.invoice_ids]

    def _prepare_copies(self, moves):
        return self

    def _after_copies(self, targets):
        return None

    def _remaining_extra(self, extra):
        return 0

    def _recovery_settings(self):
        names = ['name', 'source_company_ids', 'target_company_id', 'cutover_date', 'same_legal_entity',
                 'existing_data_reviewed', 'old_operations_frozen', 'include_accounting', 'batch_size',
                 'record_limit', 'allow_incremental', 'recovery_key']
        if 'include_purchase' in self._fields:
            names.append('include_purchase')
        result = {name: self[name] for name in names}
        result['source_company_ids'] = self.source_company_ids.ids
        result['target_company_id'] = self.target_company_id.id
        result['cutover_date'] = str(self.cutover_date)
        return result

    def _recovery_links(self, moves, extra):
        result = []
        for move in moves:
            if data := self._stored_link(move):
                result.append(data)
                if move.origin_payment_id:
                    result.append(self._stored_link(move.origin_payment_id))
        return result

    @api.model
    def action_restore_saved_moves(self):
        self._operator()
        restored, issues = 0, []
        parameters = self.env['ir.config_parameter'].sudo().search([('key', '=like', 'company_document_cutover.receipt.%')])
        for parameter in parameters:
            # Each archive has its own savepoint: damage cannot prevent other recovery.
            try:
                with self.env.cr.savepoint():
                    marker = json.loads(parameter.value)
                    if marker['target'] not in self.env.companies.ids:
                        continue
                    receipt = self.env['ir.attachment'].sudo().browse(marker['attachment']).exists()
                    if not receipt or receipt.company_id.id != marker['target'] or receipt.public:
                        raise UserError('The original private receipt is missing or belongs to another company.')
                    payload = json.loads(receipt.raw)
                    if digest(payload) != marker['hash']:
                        raise UserError('Saved receipt content changed. Restore the original from backup.')
                    settings = payload['settings']
                    if set(settings['source_company_ids']) - set(self.env.companies.ids):
                        continue
                    if self.search_count([('recovery_key', '=', settings['recovery_key'])]):
                        continue
                    if settings.get('include_purchase') and 'include_purchase' not in self._fields:
                        raise UserError('Install the optional purchase document addon to recover this move.')
                    values = {k:v for k,v in settings.items() if k in self._fields}
                    values['source_company_ids'] = [Command.set(settings['source_company_ids'])]
                    job = self.create(values)
                    for row in payload['mappings']:
                        self.env['company.document.mapping'].create({'job_id': job.id,
                            'source_record': '%s,%s' % (row['model_name'], row['source_id']),
                            'target_record': '%s,%s' % (row['model_name'], row['target_id'])})
                    for row in payload.get('separate_transactions', []):
                        self.env['company.document.overlap'].create({'job_id': job.id,
                            'source_move_id': row['source_move_id'][0], 'target_move_id': row['target_move_id'][0],
                            'separate': row['separate'], 'reason': row['reason']})
                    for link in payload['links']:
                        original = self.env[link['model']].browse(link['source']).exists()
                        current = job._stored_link(original) if original else None
                        if not current or {k:v for k,v in current.items() if k != 'job'} != {k:v for k,v in link.items() if k != 'job'}:
                            raise UserError('Native records or durable evidence changed. Review the original receipt before recovery.')
                    job._system_write({'state': 'done' if payload['complete'] else 'partial',
                        'audit_attachment_id': receipt.id, 'report': 'Recovered the saved move and all record choices. Native transactions were reused without posting or copying. Review to continue any remaining steps.'})
                    restored += 1
            except (UserError, ValueError, KeyError, TypeError) as exc:
                issues.append('%s: %s' % (parameter.key, exc))
                _logger.warning('Document receipt recovery needs review: %s', issues[-1])
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'Saved move recovery', 'message': '%s move(s) restored.%s' %
                           (restored, '\n' + '\n'.join(issues) if issues else ''), 'sticky': bool(issues)}}
