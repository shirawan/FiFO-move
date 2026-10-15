"""Stock-preserving purchase history. Native attachments and markers survive uninstall."""
import json
from decimal import Decimal
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import formatLang
from odoo.addons.company_document_cutover.models.migration import digest, packed

_ARCHIVE_CAPABILITY = object()


class PurchaseAuditHistory(models.Model):
    _name = 'company.document.purchase.archive'
    _description = 'Historical purchase reference — no live commitment'
    _order = 'id desc'
    _unique_original = models.Constraint('UNIQUE(source_order_id, company_id)', 'This purchase history is already retained.')

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, ondelete='restrict', string='New company')
    source_company_id = fields.Many2one('res.company', required=True, readonly=True, ondelete='restrict', string='Old company')
    source_company_ids = fields.Many2many('res.company', readonly=True, string='Companies covered by these documents')
    source_order_id = fields.Many2one('purchase.order', required=True, readonly=True, ondelete='restrict', string='Original order')
    job_id = fields.Many2one('company.document.cutover', readonly=True, ondelete='set null')
    attachment_id = fields.Many2one('ir.attachment', required=True, readonly=True, ondelete='restrict', string='Saved original documents')
    report = fields.Text(required=True, readonly=True, string='Original quantities and follow-up')
    accounting_matches = fields.Text(compute='_compute_accounting_matches', string='Accounting moved to the new company')
    saved_documents = fields.Binary(compute='_compute_saved_documents', string='Download historical record', readonly=True)
    saved_filename = fields.Char(compute='_compute_saved_documents')

    @api.depends('attachment_id')
    @api.depends_context('bin_size')
    def _compute_saved_documents(self):
        for history in self:
            # Record rules authorize this row; the surviving blob stays private
            # and unattached so uninstall cannot delete or expose it.
            history.saved_documents = history.attachment_id.sudo().datas
            history.saved_filename = history.attachment_id.sudo().name

    def _compute_accounting_matches(self):
        for history in self:
            try:
                payload = json.loads(history.attachment_id.sudo().with_context(bin_size=False).raw)
                key = 'company_document_cutover.link.purchase.order.%s.%s' % (history.source_order_id.id, history.company_id.id)
                marker = json.loads(self.env['ir.config_parameter'].sudo().get_param(key) or '{}')
                if not marker.get('audit_only') or marker['target'] != history.attachment_id.id or marker['target_hash'] != digest(payload):
                    raise ValueError()
                original_lines = payload['original']['lines']
            except (ValueError, KeyError, TypeError, AttributeError):
                history.accounting_matches = 'Saved historical record needs accountant review. Restore the original private record from backup before continuing.'
                continue
            matches = []
            for line in original_lines:
                for bill in line['bills']:
                    key = 'company_document_cutover.link.account.move.%s.%s' % (bill['move'], history.company_id.id)
                    try:
                        raw = self.env['ir.config_parameter'].sudo().get_param(key)
                        data = json.loads(raw) if raw else {}
                        if raw and (not isinstance(data, dict) or data.get('model') != 'account.move' or
                                data.get('source') != bill['move'] or data.get('target_company') != history.company_id.id or
                                not isinstance(data.get('target'), int) or not isinstance(data.get('line_map'), dict)):
                            raise ValueError()
                        destination = data.get('line_map', {}).get(str(bill['line']))
                        if raw and (not isinstance(destination, int) or destination <= 0):
                            raise ValueError()
                    except (ValueError, TypeError, AttributeError):
                        matches.append('%s: saved accounting match needs accountant review' % bill['number'])
                        continue
                    matches.append('%s: %s' % (bill['number'], 'saved accounting document #%s, line #%s (audit reference only)' %
                        (data['target'], destination) if destination else 'not moved by this tool'))
            history.accounting_matches = '\n'.join(matches) if matches else 'No original bill lines in this historical snapshot.'

    @api.model_create_multi
    def create(self, values):
        if self.env.context.get('_purchase_archive_capability') is not _ARCHIVE_CAPABILITY:
            raise AccessError('Historical references are created only by a reviewed document move.')
        return super().create(values)

    def write(self, values):
        if self.env.context.get('_purchase_archive_capability') is not _ARCHIVE_CAPABILITY:
            raise AccessError('Historical references are read-only. Retain the original attachment.')
        return super().write(values)

    def unlink(self):
        raise AccessError('Retain historical references. Their private attachments survive uninstall.')


class PurchaseArchiveJob(models.Model):
    _inherit = 'company.document.cutover'

    purchase_history_mode = fields.Selection([
        ('preserve_stock', 'Keep current stock; retain missing history for reference'),
        ('native', 'Match every historical order to existing live records'),
    ], default='preserve_stock', required=True, string='Historical receipts and returns',
        help='History for reference creates no live purchase order or delivery. Explicitly matched existing orders remain native history.')
    purchase_archive_ids = fields.One2many('company.document.purchase.archive', 'job_id', readonly=True,
        string='Historical purchase references', groups='purchase.group_purchase_manager')

    def _archive_marker(self, order):
        raw = self.env['ir.config_parameter'].sudo().get_param(self._key(order))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError) as exc:
            raise UserError('Saved purchase history evidence is damaged. Restore its original marker before continuing.') from exc

    def _audit_only(self, order):
        if not order:
            return False
        data = self._archive_marker(order)
        if data:
            return bool(data.get('audit_only'))
        return bool(self.include_purchase and self.purchase_history_mode == 'preserve_stock' and
            self._has_history(order) and not self.mapping_ids.filtered(
                lambda r: r.model_name == 'purchase.order' and r.source_id == order.id))

    def _needs_native_product(self, product):
        if self.purchase_history_mode == 'native':
            return True
        lines = self.env['purchase.order.line'].search([
            ('product_id', '=', product.id), ('company_id', 'in', self.source_company_ids.ids),
            ('order_id.date_order', '<=', str(self.cutover_date) + ' 23:59:59')])
        return any(not self._audit_only(line.order_id) for line in lines)

    def _purchase_snapshot(self, order):
        """Explicit source fields only. No stock writes or credential-bearing reads."""
        order.check_access('read')
        if order.company_id not in self.source_company_ids:
            raise UserError('Purchase history is outside the selected old companies.')
        graph = order.order_line.move_ids
        seen = self.env['stock.move']
        while graph - seen:
            frontier = graph - seen
            if any(m.company_id not in self.source_company_ids for m in frontier):
                raise UserError('Receipt history reaches an unselected company. Include its scope before retaining the history.')
            seen |= frontier
            if len(seen) > self.record_limit:
                raise UserError('This receipt history exceeds Maximum records to review. Review its size on a restored copy.')
            graph |= frontier.move_orig_ids | frontier.move_dest_ids | frontier.origin_returned_move_id | frontier.returned_move_ids
        seen.check_access('read')
        if seen.filtered(lambda m:m.state == 'done' and m.date.date() > self.cutover_date):
            raise UserError('Receipt/return route includes completed history after the cutover date. Include its full history.')
        bills = order.order_line.invoice_lines
        bills.check_access('read')
        bills.move_id.check_access('read')
        covered = order.company_id | seen.company_id | bills.company_id | bills.move_id.company_id | seen.account_move_id.company_id
        if covered - self.source_company_ids:
            raise UserError('Purchase receipts, valuation entries or linked bills reach an unselected company. Include that company before retaining its history.')
        seen.account_move_id.check_access('read')
        header = order.read(['name', 'company_id', 'state', 'locked', 'partner_id', 'partner_ref', 'currency_id',
            'date_order', 'date_approve', 'payment_term_id', 'incoterm_id', 'note', 'invoice_status'])[0]
        lines = []
        for line in order.order_line.sorted('id'):
            row = line.read(['name', 'sequence', 'display_type', 'product_id', 'product_uom_id', 'product_qty',
                'qty_received', 'qty_invoiced', 'qty_to_invoice', 'price_unit', 'discount', 'tax_ids', 'date_planned'])[0]
            row['unit_rounding'] = line.product_uom_id.rounding if line.product_uom_id else 0.01
            row['billing_policy'] = line.product_id.purchase_method if line.product_id else False
            row['pending_supplier_delivery'] = self._pending_demand(line)
            row['pending_supplier_return'] = sum(m.product_uom._compute_quantity(m.product_uom_qty, line.product_uom_id)
                for m in line.move_ids if m.state not in ('done', 'cancel') and m.location_dest_id.usage == 'supplier')
            row['bills'] = [{'move': b.move_id.id, 'number': b.move_id.name, 'line': b.id,
                'state': b.move_id.state, 'type': b.move_id.move_type, 'quantity': b.quantity,
                'date': str(b.move_id.date), 'currency': b.currency_id.id, 'balance': b.balance}
                for b in line.invoice_lines.sorted('id')]
            lines.append(row)
        stock = []
        for move in seen.sorted('id'):
            evidence = self._mapping_config(move)
            evidence['graph'] = {'original_moves': sorted(move.move_orig_ids.ids),
                'next_moves': sorted(move.move_dest_ids.ids), 'returns': sorted(move.returned_move_ids.ids)}
            values = self.env['product.value'].search([('move_id', '=', move.id)], order='id')
            if values.company_id - self.source_company_ids:
                raise UserError('Manual valuation history reaches an unselected company.')
            if values.filtered(lambda v:v.date.date() > self.cutover_date):
                raise UserError('Manual receipt valuation history falls after the cutover date. Include its full history.')
            evidence['manual_values'] = values.read(
                ['company_id', 'product_id', 'lot_id', 'move_id', 'value', 'date'])
            evidence['valuation_entry'] = {'id': move.account_move_id.id, 'number': move.account_move_id.name,
                'state': move.account_move_id.state, 'date': str(move.account_move_id.date)} if move.account_move_id else False
            stock.append(evidence)
        return json.loads(packed({'schema': 1, 'order': header, 'lines': lines, 'stock_history': stock,
            'source_companies': sorted((covered | self.env['product.value'].search([('move_id', 'in', seen.ids)]).company_id).ids)}))

    def _check_archive_order(self, order):
        if order.order_line.invoice_lines.move_id.filtered(lambda m:m.date > self.cutover_date):
            raise UserError('PO %s has bill history after the cutover date. Include the full history so its billed quantities are accurate.' % order.name)
        data = self._stored_link(order)
        if data and not data.get('audit_only'):
            raise UserError('This order already has a live destination transfer; retain that verified match.')
        if self.mapping_ids.filtered(lambda r:r.model_name == 'purchase.order' and r.source_id == order.id):
            raise UserError('This order is already retained as history only. Do not also activate a live copy.')
        parameter = self.env['ir.config_parameter'].sudo()
        claim = parameter.get_param('company_document_cutover.purchase.source.%s' % order.id)
        if claim and int(claim) != self.target_company_id.id:
            raise UserError('This purchase history already belongs to another destination.')
        legacy = parameter.get_param('company_financial_cutover.purchase.%s.%s' % (order.company_id.id, order.id))
        if legacy:
            try:
                prior = json.loads(legacy)
                if prior.get('target_order_id') or prior.get('target_company_id') != self.target_company_id.id:
                    raise UserError('This order already has a previous transfer or replacement. Review it before retaining another copy.')
            except (ValueError, AttributeError) as exc:
                raise UserError('Previous purchase transfer evidence is damaged.') from exc
        refs = [r for r in (order.name, order.partner_ref) if r]
        if self.env['purchase.order'].search_count([('company_id', '=', self.target_company_id.id),
                '|', ('name', 'in', refs), ('partner_ref', 'in', refs)]):
            raise UserError('Possible existing purchase duplicate %s: explicitly match its existing PO and receipt history before proceeding.' % order.name)

    def _archive_bill_links(self, order):
        result = []
        for line in order.order_line.invoice_lines.sorted('id'):
            data = self._stored_link(line.move_id)
            result.append({'source_bill': line.move_id.id, 'source_line': line.id,
                'target_bill': data['target'] if data else False,
                'target_line': data['line_map'].get(str(line.id), False) if data else False})
        return result

    def _archive_report(self, snapshot):
        header = snapshot['order']
        text = ['HISTORY ONLY — current stock stays unchanged.',
            'Original order: %s (%s). These are quantities in the old company, not live receipts or commitments in the new company.' % (header['name'], header['state']),
            'No purchase order or delivery is activated by this reference. Before arranging remaining deliveries, review existing new-company orders and stop old-company fulfilment. Previously received but unbilled goods need accountant follow-up; do not order them again.']
        for row in snapshot['lines']:
            if row['display_type']:
                text.append(row['name']); continue
            digits = max(0, -Decimal(str(row['unit_rounding'])).as_tuple().exponent)
            qty = lambda key: formatLang(self.env, row[key], digits=digits)
            text.append('%s (%s): ordered %s; net received %s; billed %s; billable in old company %s. Pending supplier delivery %s; pending supplier return %s.' %
                (row['name'], row['product_uom_id'][1], qty('product_qty'), qty('qty_received'), qty('qty_invoiced'),
                 qty('qty_to_invoice'), qty('pending_supplier_delivery'), qty('pending_supplier_return')))
        text.append('The private saved record contains the original receipt/return graph, valuation references, detailed stock records and accounting matches. They remain after uninstalling this addon.')
        return '\n'.join(text)

    def _archive_cache(self, order, attachment, payload):
        model = self.env['company.document.purchase.archive'].sudo().with_context(_purchase_archive_capability=_ARCHIVE_CAPABILITY)
        existing = model.search([('source_order_id', '=', order.id), ('company_id', '=', self.target_company_id.id)])
        values = {'name': order.name, 'company_id': self.target_company_id.id, 'source_company_id': order.company_id.id,
            'source_company_ids': [(6, 0, payload['original']['source_companies'])],
            'source_order_id': order.id, 'job_id': self.id, 'attachment_id': attachment.id,
            'report': self._archive_report(payload['original'])}
        if existing:
            if existing.attachment_id != attachment:
                raise UserError('Another saved archive already owns this purchase history.')
            if not existing.job_id:
                existing.write({'job_id': self.id})
            return existing
        return model.create(values)

    def _archive_purchase(self, order):
        self._check_archive_order(order)
        original = self._purchase_snapshot(order)
        payload = {'method': 'audit history only; preserve current stock', 'original': original,
            'source_company': order.company_id.id, 'target_company': self.target_company_id.id,
            'bill_matches': self._archive_bill_links(order)}
        attachment = self.env['ir.attachment'].sudo().create({'name': 'Historical-purchase-%s-%s.json' % (order.id, self.target_company_id.id),
            'type': 'binary', 'raw': packed(payload).encode(), 'mimetype': 'application/json', 'res_model': False,
            'res_id': False, 'company_id': self.target_company_id.id, 'public': False})
        data = {'evidence_version': 5, 'model': order._name, 'source': order.id, 'target': attachment.id,
            'target_model': 'ir.attachment', 'audit_only': True, 'source_company': order.company_id.id,
            'target_company': self.target_company_id.id, 'source_number': order.name, 'job': self.id,
            'source_hash': digest(original), 'target_hash': digest(payload), 'line_map': {}, 'cutover': str(self.cutover_date)}
        self.env['ir.config_parameter'].sudo().set_param(self._key(order), packed(data))
        self.env['ir.config_parameter'].sudo().set_param('company_document_cutover.purchase.source.%s' % order.id, str(self.target_company_id.id))
        self._archive_cache(order, attachment, payload)
        return data

    def _prepare_copies(self, moves):
        job = super()._prepare_copies(moves)
        # Save original purchase/stock evidence atomically with the first bill
        # step. A later step may finish the ledger after the addon is reinstalled.
        for order in moves.invoice_line_ids.purchase_line_id.order_id:
            if job._audit_only(order) and not job._stored_link(order):
                job._archive_purchase(order)
        return job

    def _recovery_links(self, moves, extra):
        links = super()._recovery_links(moves, extra)
        for order in moves.invoice_line_ids.purchase_line_id.order_id:
            data = self._stored_link(order)
            if data and data.get('audit_only') and data not in links:
                links.append(data)
        return links

    def _stored_link(self, record):
        if record._name != 'purchase.order':
            return super()._stored_link(record)
        data = self._archive_marker(record)
        if not data or not data.get('audit_only'):
            return super()._stored_link(record)
        try:
            attachment = self.env['ir.attachment'].sudo().browse(data['target']).exists()
            if (not attachment or attachment.public or attachment.res_model or attachment.company_id != self.target_company_id or
                    record.company_id not in self.source_company_ids or data['source'] != record.id or data['model'] != record._name or
                    data['target_model'] != 'ir.attachment' or data['target_company'] != self.target_company_id.id or
                    data['source_company'] != record.company_id.id):
                raise UserError('Saved historical purchase ownership differs. No second copy will be made.')
            payload = json.loads(attachment.with_context(bin_size=False).raw)
            if (digest(payload) != data['target_hash'] or digest(payload['original']) != data['source_hash'] or
                    digest(self._purchase_snapshot(record)) != data['source_hash'] or
                    payload['target_company'] != self.target_company_id.id or payload['source_company'] != record.company_id.id):
                raise UserError('Saved historical purchase evidence changed. Retain and review the original archive; no second copy will be made.')
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise UserError('Saved historical purchase evidence is damaged. Restore the original archive before continuing.') from exc

    def _save_link(self, source, target, line_map=None):
        data = super()._save_link(source, target, line_map)
        if source._name == 'account.move':
            references = [{'source_order': line.purchase_line_id.order_id.id, 'source_order_number': line.purchase_line_id.order_id.name,
                'source_purchase_line': line.purchase_line_id.id, 'source_bill_line': line.id,
                'target_bill_line': data['line_map'][str(line.id)], 'relationship': 'audit reference only'}
                for line in source.invoice_line_ids if line.purchase_line_id and self._audit_only(line.purchase_line_id.order_id)]
            if references:
                data['purchase_history_references'] = references
                self.env['ir.config_parameter'].sudo().set_param(self._key(source), packed(data))
        return data

    def _recovery_settings(self):
        return {**super()._recovery_settings(), 'purchase_history_mode': self.purchase_history_mode}

    def _lock_tables(self):
        return (*super()._lock_tables(), 'stock_move_move_rel', 'company_document_purchase_archive')

    def _restore_extra_history(self, payload):
        super()._restore_extra_history(payload)
        for link in payload['links']:
            if link.get('audit_only') and link['model'] == 'purchase.order':
                order = self.env['purchase.order'].browse(link['source']).exists()
                if not order:
                    raise UserError('The original historical order was removed; retain its saved archive for manual review.')
                data = self._stored_link(order)
                attachment = self.env['ir.attachment'].sudo().browse(data['target'])
                self._archive_cache(order, attachment, json.loads(attachment.with_context(bin_size=False).raw))

    def _restore_surviving_job(self, payload, receipt):
        self.ensure_one()
        settings = payload['settings']
        expected = {(r['model_name'], r['source_id'], r['target_id']) for r in payload['mappings']}
        current = {(r.model_name, r.source_id, r.target_id) for r in self.mapping_ids}
        choices = {name: settings[name] for name in ('include_purchase', 'purchase_history_mode') if name in settings}
        if settings.get('include_purchase') and 'purchase_history_mode' not in choices:
            choices['purchase_history_mode'] = 'native'  # Pre-audit receipts used strict native matching.
        missing_screen = any(link.get('audit_only') and link['model'] == 'purchase.order' and not
            self.env['company.document.purchase.archive'].sudo().search_count([
                ('source_order_id', '=', link['source']), ('company_id', '=', self.target_company_id.id)])
            for link in payload['links'])
        if not (expected - current or missing_screen or any(self[name] != value for name, value in choices.items())):
            return super()._restore_surviving_job(payload, receipt)
        if (self.state not in ('done', 'partial') or self.audit_attachment_id != receipt or
                self.target_company_id.id != settings['target_company_id'] or
                set(self.source_company_ids.ids) != set(settings['source_company_ids']) or
                str(self.cutover_date) != settings['cutover_date'] or current - expected):
            raise UserError('The retained move or its choices changed. Review its original receipt before restoring optional purchase history.')
        state = self.state
        # Only lost optional settings and missing saved matches are restored.
        # The per-receipt savepoint rolls this back if any evidence fails.
        self._system_write({**choices, 'state': 'draft'})
        self._operator()
        for model, source, target in sorted(expected - current):
            self.env['company.document.mapping'].create({'job_id': self.id,
                'source_record': '%s,%s' % (model, source), 'target_record': '%s,%s' % (model, target)})
        self._restore_extra_history(payload)
        for link in payload['links']:
            original = self.env[link['model']].browse(link['source']).exists()
            saved = self._stored_link(original) if original else None
            if not saved or {k:v for k,v in saved.items() if k != 'job'} != {k:v for k,v in link.items() if k != 'job'}:
                raise UserError('Native records or durable evidence changed. Review the original receipt before restoring optional history.')
        self._system_write({'state': state, 'preview_hash': False})
        return True
