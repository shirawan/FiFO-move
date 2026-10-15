from odoo import fields, models, Command
from odoo.tools.float_utils import float_compare
from odoo.exceptions import UserError, AccessError
from odoo.addons.company_document_cutover.models.migration import digest, remove_optional_document_matches


class PurchaseDocumentJob(models.Model):
    _inherit = 'company.document.cutover'
    include_purchase = fields.Boolean(string='Purchase orders')

    def _operator(self):
        super()._operator()
        if any(j.include_purchase for j in self) and not self.env.user.has_group('purchase.group_purchase_manager'):
            raise AccessError('Purchase manager access is required for purchase commitments.')

    def _target(self, record):
        target = super()._target(record)
        if record and record._name == 'product.product' and self.include_purchase and self._needs_native_product(record) and record.purchase_method != target.purchase_method:
            raise UserError('Product billing policy differs for %s. Choose matching bill-on-ordered or bill-on-received settings before moving purchase commitments.' % record.display_name)
        return target

    def _record_details(self, record):
        if record._name == 'purchase.order':
            return {'lines': [{'id': line.id, 'billing_policy': line.product_id.purchase_method,
                'billable': line.qty_to_invoice} for line in record.order_line.sorted('id') if not line.display_type],
                'invoice_status': record.invoice_status}
        return super()._record_details(record)

    def _fingerprint(self, record):
        if record._name == 'purchase.order':
            data = {'header': record.read(['company_id', 'name', 'state', 'partner_id', 'currency_id', 'date_order',
                'date_approve', 'partner_ref', 'payment_term_id', 'incoterm_id', 'note', 'user_id'])[0],
                'lines': record.order_line.read(['display_type', 'product_id', 'product_uom_id', 'product_qty',
                    'qty_received', 'qty_invoiced', 'price_unit', 'discount', 'tax_ids', 'date_planned', 'name',
                    'sequence', 'invoice_lines', 'move_ids'])}
            if self._has_history(record):
                data['locked'] = record.locked
                for row in data['lines']:
                    row['invoice_lines'] = sorted(row['invoice_lines'])
                    row['move_ids'] = sorted(row['move_ids'])
                data['stock'] = [self._mapping_config(m) for m in record.order_line.move_ids.sorted('id')]
                data['stock_products'] = record.order_line.product_id.with_company(record.company_id).sorted('id').read(['standard_price', 'cost_method', 'valuation', 'tracking'])
            return digest(data)
        return super()._fingerprint(record)

    def _extra_plan(self, issues):
        extra = super()._extra_plan(issues)
        if not self.include_purchase:
            return extra
        orders = self.env['purchase.order'].search([('company_id', 'in', self.source_company_ids.ids),
            ('date_order', '<=', str(self.cutover_date) + ' 23:59:59')], order='id', limit=self.record_limit + 1)
        if len(orders) > self.record_limit:
            issues.append('Purchase history exceeds Maximum records to review. Increase the limit after a restored-copy timing check.')
        info = []
        for order in orders:
            if any(m.date.date() > self.cutover_date for m in order.order_line.move_ids if m.state == 'done'):
                issues.append('PO %s: Receipt/return falls after the cutover date; include its full history.' % order.name)
            rows = [{'line': line.id, 'description':line.name, 'unit':line.product_uom_id.display_name, 'ordered': line.product_qty, 'received': line.qty_received,
                'billed': line.qty_invoiced, 'remaining_delivery': line.product_qty - line.qty_received,
                'remaining_billing': line.product_qty - line.qty_invoiced,
                'pending_receipt': sum(m.product_uom._compute_quantity(m.product_uom_qty, line.product_uom_id) for m in line.move_ids if m.state not in ('done', 'cancel') and m.location_id.usage == 'supplier'),
                'billable_now': line.qty_to_invoice} for line in order.order_line if not line.display_type]
            info.append({'source': order.id, 'name': order.name, 'state': order.state, 'locked': order.locked, 'lines': rows})
            try:
                info[-1].update({'hash': self._fingerprint(order), 'details': self._record_details(order)})
                claim=self.env['ir.config_parameter'].sudo().get_param('company_document_cutover.purchase.source.%s' % order.id)
                if claim and int(claim)!=self.target_company_id.id:
                    raise UserError('PO %s already has a document transfer to another destination.' % order.name)
                if self._audit_only(order):
                    info[-1]['audit_only'] = True
                    info[-1]['audit_hash'] = digest(self._purchase_snapshot(order))
                    self._check_archive_order(order)
                    continue
                if order.state == 'purchase' and not self._has_history(order) and not self.old_operations_frozen:
                    raise UserError('Confirm old-company fulfilment will stop before activating new PO commitments.')
                self._target(order.partner_id)
                self._target(order.payment_term_id)
                if order.state not in ('draft', 'sent', 'purchase', 'cancel'):
                    raise UserError('Purchase approval/completed state needs additional workflow mapping: %s.' % order.name)
                lines = order.order_line.filtered(lambda l: not l.display_type)
                if self._has_history(order):
                    target, line_map = self._history_matches(order)
                    info[-1]['history_target'] = target.id
                    info[-1]['history_hash'] = self._fingerprint(target)
                    info[-1]['history_details'] = self._record_details(target)
                    info[-1]['history_lines'] = line_map
                for line in lines:
                    product = self._target(line.product_id)
                    if order.state == 'purchase' and not self._has_history(order) and product.company_id != self.target_company_id:
                        raise UserError('Confirmed PO %s needs destination-specific products to protect source/shared supplier records.' % order.name)
                    if line.product_qty < 0:
                        raise UserError('PO %s contains negative quantity; return-history mapping is required.' % order.name)
                    for tax in line.tax_ids:
                        self._target(tax)
                legacy = self.env['ir.config_parameter'].sudo().get_param('company_financial_cutover.purchase.%s.%s' % (order.company_id.id,order.id))
                if legacy:
                    import json
                    marker=json.loads(legacy)
                    if marker.get('target_order_id') or marker.get('target_company_id') != self.target_company_id.id:
                        raise UserError('PO %s is already owned by a previous purchase transfer/replacement; do not activate a second commitment.' % order.name)
                stored = self._stored_link(order)
                existing = self.env['purchase.order'].browse(stored['target']) if stored else self._explicit(order) if self._has_history(order) else self.env['purchase.order']
                refs = [r for r in (order.name, order.partner_ref) if r]
                if self.env['purchase.order'].search([('company_id', '=', self.target_company_id.id),
                    '|', ('name', 'in', refs), ('partner_ref', 'in', refs)]) - existing:
                    raise UserError('Possible existing purchase duplicate %s needs an explicit accountant decision.' % order.name)
            except UserError as exc:
                issues.append(str(exc))
        extra['orders'] = info
        return extra

    def _lock_tables(self):
        return (*super()._lock_tables(), 'purchase_order', 'purchase_order_line', 'stock_move', 'stock_move_line',
            'stock_picking', 'stock_picking_type', 'stock_location', 'stock_warehouse', 'stock_rule',
            'product_supplierinfo', 'stock_reference', 'product_value', 'product_category')

    def _extra_execute(self, extra, audit):
        super()._extra_execute(extra, audit)
        if not self.include_purchase:
            return
        pending = [row for row in extra['orders'] if not self._stored_link(self.env['purchase.order'].browse(row['source']))]
        for row in pending[:self.batch_size]:
            source = self.env['purchase.order'].browse(row['source'])
            stored = self._stored_link(source)
            if stored:
                audit['links'].append(stored)
                continue
            if self._audit_only(source):
                audit['links'].append(self._archive_purchase(source))
                continue
            if self._has_history(source):
                target, line_map = self._history_matches(source, final=True)
                audit['links'].append(self._save_link(source, target, line_map))
                self.env['ir.config_parameter'].sudo().set_param('company_document_cutover.purchase.source.%s' % source.id, str(self.target_company_id.id))
                continue
            lines = []
            for line in source.order_line:
                vals = {'display_type': line.display_type, 'name': line.name, 'sequence': line.sequence}
                if not line.display_type:
                    vals.update({'product_id': self._target(line.product_id).id, 'product_uom_id': line.product_uom_id.id,
                        'product_qty': line.product_qty, 'price_unit': line.price_unit, 'discount': line.discount,
                        'tax_ids': [Command.set([self._target(t).id for t in line.tax_ids])], 'date_planned': line.date_planned})
                lines.append(Command.create(vals))
            target = self.env['purchase.order'].with_company(self.target_company_id).create({
                'company_id': self.target_company_id.id, 'partner_id': self._target(source.partner_id).id,
                'currency_id': source.currency_id.id, 'date_order': source.date_order, 'partner_ref': source.partner_ref,
                'payment_term_id': self._target(source.payment_term_id).id, 'incoterm_id': source.incoterm_id.id,
                'note': source.note, 'order_line': lines})
            if source.state == 'purchase':
                target.button_confirm()
                # Dates are original evidence; pending receipts remain unvalidated commitments.
                target.write({'date_order': source.date_order, 'date_approve': source.date_approve})
                if source.locked:
                    target.button_lock()
                elif target.locked:
                    target.button_unlock()
            elif source.state == 'sent':
                target.write({'state': 'sent'})  # no mail action
            elif source.state == 'cancel':
                target.button_cancel()
            if target.state != source.state or target.locked != source.locked or target.currency_id != source.currency_id:
                raise UserError('Native purchase confirmation produced a different state/currency.')
            source_lines = source.order_line.sorted('sequence')
            target_lines = target.order_line.sorted('sequence')
            if len(source_lines) != len(target_lines):
                raise UserError('Native PO line count changed.')
            line_map = {}
            for old, new in zip(source_lines, target_lines):
                if old.display_type != new.display_type or (not old.display_type and (
                    new.product_id != self._target(old.product_id) or new.product_uom_id != old.product_uom_id or
                    new.product_id.with_company(target.company_id).cost_method != old.product_id.with_company(source.company_id).cost_method or
                    new.product_id.with_company(target.company_id).valuation != old.product_id.with_company(source.company_id).valuation or
                    new.product_qty != old.product_qty or new.price_unit != old.price_unit or new.discount != old.discount or
                    new.qty_received != old.qty_received or new.qty_invoiced != old.qty_invoiced or new.qty_to_invoice != old.qty_to_invoice or
                    new.date_planned != old.date_planned or new.tax_ids != self.env['account.tax'].browse([self._target(t).id for t in old.tax_ids]))):
                    raise UserError('Native purchase quantity, pricing, tax or commitment differs; rolled back.')
                line_map[str(old.id)] = new.id
            if any(p.state == 'done' for p in target.picking_ids):
                raise UserError('No historical or real delivery may be created by this migration.')
            audit['links'].append(self._save_link(source, target, line_map))
            self.env['ir.config_parameter'].sudo().set_param('company_document_cutover.purchase.source.%s' % source.id,str(self.target_company_id.id))

    def _remaining_extra(self, extra):
        return sum(not bool(self._stored_link(self.env['purchase.order'].browse(r['source']))) for r in extra.get('orders', []))

    def _recovery_links(self, moves, extra):
        result = super()._recovery_links(moves, extra)
        for row in extra.get('orders', []):
            if data := self._stored_link(self.env['purchase.order'].browse(row['source'])):
                result.append(data)
        return result

    def _stored_link(self, record):
        data = super()._stored_link(record)
        if data and record._name == 'purchase.order' and 'lock_state' in data:
            target = self.env['purchase.order'].browse(data['target'])
            if data['lock_state'] != [record.locked, target.locked]:
                raise UserError('Saved PO lock state changed; review the completed migration evidence.')
        if data and record._name == 'purchase.order':
            target = self.env['purchase.order'].browse(data['target'])
            for line in record.order_line.filtered(lambda l: not l.display_type):
                matched = self.env['purchase.order.line'].browse(data.get('line_map', {}).get(str(line.id), 0)).exists()
                if (len(matched) != 1 or matched.order_id != target or
                        line.product_id.purchase_method != matched.product_id.purchase_method or
                        float_compare(line.qty_to_invoice, matched.qty_to_invoice, precision_rounding=line.product_uom_id.rounding)):
                    raise UserError('Saved PO billing policy or billable quantity differs. Review the existing order; no second copy will be made.')
                pending_issues = self._pending_identity_issues(line) + self._pending_identity_issues(matched)
                if pending_issues or float_compare(self._pending_demand(line), self._pending_demand(matched), precision_rounding=line.product_uom_id.rounding):
                    raise UserError('Saved pending receipt differs; review the existing order. ' + ' '.join(pending_issues))
            if record.invoice_status != target.invoice_status:
                raise UserError('Saved PO billing status differs; review the existing order.')
        return data

    def _save_link(self, source, target, line_map=None):
        data = super()._save_link(source, target, line_map)
        if source._name == 'purchase.order':
            data['lock_state'] = [source.locked, target.locked]
            from odoo.addons.company_document_cutover.models.migration import packed
            data['receipt_matches'] = [
                {'source': move.id, 'target': self._explicit(move).id,
                 'source_evidence': self._mapping_config(move),
                 'target_evidence': self._mapping_config(self._explicit(move))}
                for move in source.order_line.move_ids.filtered(lambda m: m.state == 'done').sorted('id')]
            self.env['ir.config_parameter'].sudo().set_param(self._key(source), packed(data))
        return data

    def _has_history(self, order):
        return any(l.qty_received or l.qty_invoiced or l.invoice_lines or
                   l.move_ids.filtered(lambda m: m.state == 'done') for l in order.order_line)

    def _mapping_config(self, record):
        if record._name == 'purchase.order':
            return self._fingerprint(record)
        if record._name == 'purchase.order.line':
            return self._fingerprint(record.order_id)
        if record._name == 'stock.move':
            return {'move': record.read(['state', 'company_id', 'product_id', 'product_uom', 'quantity',
                'product_uom_qty', 'date', 'location_id', 'location_dest_id', 'purchase_line_id',
                'origin_returned_move_id', 'to_refund', 'move_line_ids', 'write_date',
                'value', 'price_unit', 'account_move_id', 'is_in', 'is_out', 'is_dropship'])[0],
                'details': record.move_line_ids.read(['product_id', 'product_uom_id', 'quantity', 'lot_id',
                    'owner_id', 'package_id', 'result_package_id', 'location_id', 'location_dest_id', 'write_date'])}
        return super()._mapping_config(record)

    def _pending_demand(self, line):
        return sum(m.product_uom._compute_quantity(m.product_uom_qty, line.product_uom_id)
                   for m in line.move_ids if m.state not in ('done', 'cancel') and m.location_id.usage == 'supplier')

    def _pending_identity_issues(self, line):
        issues = []
        for movement in line.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            if (movement.product_id != line.product_id or movement.company_id != line.company_id or
                    movement.product_uom not in (line.product_uom_id | line.product_id.uom_id) or movement.purchase_line_id != line):
                issues.append('PO %s, %s: pending receipt product, unit, company or PO-line ownership differs.' % (line.order_id.name, line.name))
        return issues

    def _history_matches(self, order, final=False):
        """Verify existing native history, without writing stock or purchase quantities."""
        errors = []
        target = self.env['purchase.order']
        try:
            target = self._explicit(order)
        except UserError as exc:
            errors.append(str(exc))
        for move in order.order_line.move_ids.filtered(lambda m: m.state == 'done'):
            try:
                self._explicit(move)
            except UserError as exc:
                errors.append(str(exc))
        pairs = []
        for line in order.order_line:
            try:
                pairs.append((line, self._explicit(line)))
            except UserError as exc:
                errors.append(str(exc))
        if errors:
            raise UserError('PO %s has receipts/returns or bill links needing stock/bill-history mappings:\n%s' % (order.name, '\n'.join(errors)))
        if len(set(new.id for old, new in pairs)) != len(pairs) or set(target.order_line.ids) != {new.id for old, new in pairs}:
            raise UserError('PO %s: each old line must match one distinct line of the selected new PO, with no extra lines.' % order.name)
        if (target.company_id != self.target_company_id or target.state != order.state or target.locked != order.locked or
                target.partner_id != self._target(order.partner_id) or target.currency_id != order.currency_id or
                target.payment_term_id != self._target(order.payment_term_id) or target.incoterm_id != order.incoterm_id or
                target.date_order != order.date_order or target.date_approve != order.date_approve or
                target.partner_ref != order.partner_ref or target.note != order.note):
            raise UserError('PO %s: destination order terms, dates or state differ.' % order.name)
        # Reject aliases both within this job and across durable prior jobs.
        aliases = self.mapping_ids.filtered(lambda r: r.model_name == 'purchase.order' and r.target_id == target.id)
        if len(aliases) != 1:
            raise UserError('One destination PO cannot represent multiple old orders.')
        import json
        claims = self.env['ir.config_parameter'].sudo().search([('key', '=like', 'company_document_cutover.link.purchase.order.%')])
        if any((d := json.loads(c.value)).get('target') == target.id and d.get('source') != order.id for c in claims):
            raise UserError('The selected destination PO already represents another source order.')
        all_moves = self._moves()
        for old, new in pairs:
            if old.display_type != new.display_type or old.name != new.name or old.sequence != new.sequence:
                errors.append('PO %s: line description, section or order differs.' % order.name)
                continue
            if old.display_type:
                continue
            if (new.product_id != self._target(old.product_id) or new.product_uom_id != old.product_uom_id or
                    new.product_id.with_company(target.company_id).cost_method != old.product_id.with_company(order.company_id).cost_method or
                    new.product_id.with_company(target.company_id).valuation != old.product_id.with_company(order.company_id).valuation or
                    new.product_qty != old.product_qty or new.price_unit != old.price_unit or new.discount != old.discount or
                    new.date_planned != old.date_planned or new.tax_ids != self.env['account.tax'].browse([self._target(t).id for t in old.tax_ids]) or
                    float_compare(new.qty_received, old.qty_received, precision_rounding=old.product_uom_id.rounding)):
                errors.append('PO %s, %s: product, terms or native received quantity differs.' % (order.name, old.name))
            done = old.move_ids.filtered(lambda m: m.state == 'done')
            mapped = self.env['stock.move']
            for move in done:
                try:
                    dest = self._explicit(move)
                    if move.date.date() > self.cutover_date:
                        raise UserError('Receipt/return falls after the cutover date; choose a date covering the full history.')
                    self._verify_valuation_history(move, dest)
                    self._verify_tracked_receipt(move, dest)
                    if dest in mapped:
                        raise UserError('Two receipts/returns cannot use the same destination movement.')
                    mapped |= dest
                    if (dest.state != 'done' or dest.purchase_line_id != new or dest.product_id != self._target(move.product_id) or
                            dest.product_uom != move.product_uom or dest.quantity != move.quantity or dest.date != move.date or
                            dest.location_id.usage != move.location_id.usage or dest.location_dest_id.usage != move.location_dest_id.usage or
                            not self.target_company_id.currency_id.is_zero(dest.value - move.value) or
                            dest.to_refund != move.to_refund or bool(dest.origin_returned_move_id) != bool(move.origin_returned_move_id)):
                        raise UserError('Receipt/return evidence differs for %s.' % move.display_name)
                    if move.origin_returned_move_id and dest.origin_returned_move_id != self._explicit(move.origin_returned_move_id):
                        raise UserError('Return must match the corresponding original destination receipt.')
                except UserError as exc:
                    errors.append(str(exc))
            if set(mapped.ids) != set(new.move_ids.filtered(lambda m: m.state == 'done').ids):
                errors.append('PO %s, %s: destination receipt/return history contains unmatched movements.' % (order.name, old.name))
            pending = (old.move_ids | new.move_ids).filtered(lambda m: m.state not in ('done', 'cancel'))
            try:
                self._verify_pending_history(old, new)
            except UserError as exc:
                errors.append('PO %s: pending supplier returns and route history need review. %s' % (order.name, exc))
            errors.extend(self._pending_identity_issues(old) + self._pending_identity_issues(new))
            if float_compare(self._pending_demand(old), self._pending_demand(new), precision_rounding=old.product_uom_id.rounding):
                errors.append('PO %s, %s: remaining native receipt commitment differs.' % (order.name, old.name))
            represented = self.env['account.move.line']
            for bill_line in old.invoice_lines:
                if bill_line.move_id.state == 'cancel':
                    errors.append('PO %s has cancelled bill history; explicit accounting-history adoption is not implemented.' % order.name)
                    continue
                stored = self._stored_link(bill_line.move_id)
                adopted, adopted_lines = self._adoption(bill_line.move_id) if not stored else (False, {})
                if stored or adopted:
                    dest_line = self.env['account.move.line'].browse((stored['line_map'] if stored else adopted_lines)[str(bill_line.id)])
                    if dest_line.purchase_line_id != new:
                        errors.append('Saved bill is not linked to its selected destination PO line.')
                    represented |= dest_line
                elif bill_line.move_id not in all_moves or final:
                    errors.append('PO %s: include all linked bills/credits in accounting, or reuse their verified completed transfer.' % order.name)
            if new.invoice_lines - represented:
                errors.append('PO %s has existing destination bill lines without verified migration evidence; review duplicates first.' % order.name)
            if final and (float_compare(new.qty_invoiced, old.qty_invoiced, precision_rounding=old.product_uom_id.rounding) or
                          float_compare(new.qty_to_invoice, old.qty_to_invoice, precision_rounding=old.product_uom_id.rounding)):
                errors.append('PO %s: native billed quantity differs after linking documents.' % order.name)
        if final and target.invoice_status != order.invoice_status:
            errors.append('Native purchase billing status differs after linking documents.')
        if errors:
            raise UserError('PO %s: receipts/returns or bill links need review:\n%s' % (order.name, '\n'.join(sorted(set(errors)))))
        return target, {str(old.id): new.id for old, new in pairs}

    def _check_purchase_line(self, line, all_moves):
        order = line.purchase_line_id.order_id
        if self._audit_only(order):
            if order.company_id not in self.source_company_ids or order.date_order.date() > self.cutover_date:
                raise UserError('Bill/PO line links reach outside selected purchase history.')
            self._check_archive_order(order)
            return
        if not self.include_purchase:
            stored = self._stored_link(line.move_id)
            if stored:
                # Accounting-only continuation may verify an earlier linked bill.
                # Its original PO is not copied or made operational again.
                import json
                raw = self.env['ir.config_parameter'].sudo().get_param(self._key(line.purchase_line_id.order_id))
                try:
                    purchase = json.loads(raw or '{}')
                    new = self.env['account.move.line'].browse(stored['line_map'][str(line.id)])
                    if (new.purchase_line_id.id == purchase.get('line_map', {}).get(str(line.purchase_line_id.id)) and
                            new.purchase_line_id.order_id.id == purchase.get('target') and
                            new.company_id == self.target_company_id):
                        return
                except (KeyError, TypeError, ValueError):
                    pass
            return super()._check_purchase_line(line, all_moves)
        if order.company_id not in self.source_company_ids or order.date_order.date() > self.cutover_date:
            raise UserError('Bill/PO line links reach outside selected purchase history.')
        self._explicit(order)
        dest = self._explicit(line.purchase_line_id)
        if dest.order_id != self._explicit(order):
            raise UserError('Bill line must match a line of the selected destination PO.')

    def _copy_move(self, source):
        # Native bill posting recalculates stock valuation even without valuation
        # journal entries. Allow native hooks, but roll back any economic change.
        lines = source.invoice_line_ids.purchase_line_id
        if not lines:
            return super()._copy_move(source)
        targets = self.env['purchase.order.line']
        products = self.env['product.product']
        for line in lines:
            if not self._audit_only(line.order_id):
                targets |= self._explicit(line)
            products |= self._target(line.product_id)
        def stock_evidence():
            return {
                'moves': self.env['stock.move'].search([('company_id', '=', self.target_company_id.id), ('product_id', 'in', products.ids)], order='id').read([
                    'quantity', 'product_uom_qty', 'state', 'value', 'price_unit',
                    'account_move_id', 'location_id', 'location_dest_id', 'origin_returned_move_id']),
                'costs': products.with_company(self.target_company_id).sorted('id').read(['standard_price', 'cost_method', 'valuation']),
                'quants': self.env['stock.quant'].search([('company_id', '=', self.target_company_id.id), ('product_id', 'in', products.ids)], order='id').read(['product_id', 'location_id', 'lot_id', 'owner_id', 'package_id', 'quantity', 'reserved_quantity']),
                'manual_values': self.env['product.value'].search([('company_id', '=', self.target_company_id.id)], order='id').read(['product_id', 'lot_id', 'move_id', 'value', 'date']),
            }
        before = stock_evidence()
        existing_entries = self.env['account.move'].search([('company_id', '=', self.target_company_id.id)])
        result = super()._copy_move(source)
        extra_entries = self.env['account.move'].search([('company_id', '=', self.target_company_id.id)]) - existing_entries - result[0]
        if stock_evidence() != before or extra_entries:
            raise UserError('Native bill posting would change existing stock values, quantities or product costs. This requires valuation-history review; the whole attempt was rolled back.')
        return result

    def _invoice_values(self, move):
        values = super()._invoice_values(move)
        for line, command in zip(move.invoice_line_ids, values['invoice_line_ids']):
            if line.purchase_line_id and not self._audit_only(line.purchase_line_id.order_id):
                command[2]['purchase_line_id'] = self._explicit(line.purchase_line_id).id
        return values


class PurchaseDocumentMapping(models.Model):
    _inherit = 'company.document.mapping'
    _history_choices = [('purchase.order', 'Existing purchase order'),
                        ('purchase.order.line', 'Existing purchase order line'),
                        ('stock.move', 'Completed receipt or return')]
    source_record = fields.Reference(selection_add=_history_choices, ondelete={key: remove_optional_document_matches for key, label in _history_choices})
    target_record = fields.Reference(selection_add=_history_choices, ondelete={key: remove_optional_document_matches for key, label in _history_choices})
    model_name = fields.Selection(selection_add=_history_choices, ondelete={key: remove_optional_document_matches for key, label in _history_choices})
