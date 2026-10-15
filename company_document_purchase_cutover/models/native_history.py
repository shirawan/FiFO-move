"""Explicit, read-only reuse of existing valuation and complex route history."""
import json
from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare
from odoo.addons.company_document_cutover.models.migration import packed, remove_optional_document_matches


class NativePurchaseHistory(models.Model):
    _inherit = 'company.document.cutover'

    def _mapping_config(self, record):
        if record._name == 'product.value':
            return record.read(['company_id', 'product_id', 'lot_id', 'move_id', 'value', 'date', 'write_date'])[0]
        result = super()._mapping_config(record)
        if record._name == 'stock.move':
            if record.move_orig_ids or record.move_dest_ids:
                result['route'] = {'origins': sorted(record.move_orig_ids.ids), 'destinations': sorted(record.move_dest_ids.ids)}
            adjustments = self.env['product.value'].search([('move_id', '=', record.id)], order='id')
            if adjustments:
                result['manual_values'] = [self._mapping_config(row) for row in adjustments]
        return result

    def _verify_valuation_history(self, source, target):
        old_values = self.env['product.value'].search([('move_id', '=', source.id)])
        new_values = self.env['product.value'].search([('move_id', '=', target.id)])
        matched = self.env['product.value']
        for old in old_values:
            new = self._explicit(old)
            if (new in matched or new.move_id != target or new.company_id != self.target_company_id or
                    new.product_id != self._target(old.product_id) or new.lot_id != self._target(old.lot_id) or
                    new.date != old.date or not self.target_company_id.currency_id.is_zero(new.value - old.value)):
                raise UserError('Manual receipt valuation adjustment differs from its explicit existing match.')
            matched |= new
        if matched != new_values:
            raise UserError('Manual receipt valuation adjustments need one-to-one existing history mappings.')
        if bool(source.account_move_id) != bool(target.account_move_id):
            raise UserError('Stock valuation accounting is missing on one receipt. Do not replay it over existing inventory balances.')
        if source.account_move_id:
            saved = self._stored_link(source.account_move_id)
            existing = self.env['account.move'].browse(saved['target']) if saved else self._adoption(source.account_move_id)[0]
            if existing != target.account_move_id:
                raise UserError('Choose the existing valuation journal entry linked to the matched receipt. No second valuation entry will be created.')

    def _check_stock_history(self, move):
        stock = getattr(move, 'stock_move_ids', False)
        if not stock:
            return super()._check_stock_history(move)
        saved = self._stored_link(move)
        target = self.env['account.move'].browse(saved['target']) if saved else self._adoption(move)[0]
        if not target:
            return super()._check_stock_history(move)
        matched = self.env['stock.move']
        for source in stock:
            new = self._explicit(source)
            if (new in matched or source.company_id not in self.source_company_ids or new.company_id != self.target_company_id or
                    new.account_move_id != target or new.state != source.state or new.product_id != self._target(source.product_id) or
                    new.product_uom != source.product_uom or new.date != source.date or
                    new.location_id != self._target(source.location_id) or new.location_dest_id != self._target(source.location_dest_id) or
                    new.to_refund != source.to_refund or
                    bool(new.origin_returned_move_id) != bool(source.origin_returned_move_id) or
                    float_compare(new.quantity, source.quantity, precision_rounding=source.product_uom.rounding) or
                    not self.target_company_id.currency_id.is_zero(new.value - source.value)):
                raise UserError('Existing stock valuation entry has different native receipt evidence.')
            if source.origin_returned_move_id and new.origin_returned_move_id != self._explicit(source.origin_returned_move_id):
                raise UserError('Existing stock valuation return refers to a different original receipt.')
            self._verify_tracked_receipt(source, new)
            self._verify_valuation_history(source, new)
            for name in ('move_orig_ids', 'move_dest_ids', 'returned_move_ids'):
                expected = self.env['stock.move'].browse([self._explicit(r).id for r in source[name]])
                if new[name] != expected:
                    raise UserError('Existing stock valuation route history differs.')
            matched |= new
        if matched != target.stock_move_ids:
            raise UserError('Match every receipt represented by the existing valuation entry, including grouped orders.')

    def _record_details(self, record):
        details = super()._record_details(record)
        if record._name == 'account.move' and record.stock_move_ids:
            details['valuation_stock_history'] = [self._mapping_config(m) for m in self._native_route_graph(record.stock_move_ids).sorted('id')]
        if record._name == 'purchase.order':
            roots = record.order_line.move_ids
            if roots.filtered(lambda m:m.move_orig_ids or m.move_dest_ids or (m.state not in ('done', 'cancel') and m.origin_returned_move_id)):
                graph = self._native_route_graph(roots)
                details['complete_route_history'] = [self._mapping_config(m) for m in graph.sorted('id')]
        return json.loads(packed(details))

    def _native_route_graph(self, roots):
        graph, frontier = roots, roots
        while frontier:
            related = frontier.move_orig_ids | frontier.move_dest_ids | frontier.origin_returned_move_id | frontier.returned_move_ids
            frontier = related - graph
            graph |= frontier
            if len(graph) > self.record_limit:
                raise UserError('Complex receipt history exceeds Maximum records to review.')
        graph.check_access('read')
        if graph.company_id - (self.source_company_ids | self.target_company_id):
            raise UserError('Complex receipt history reaches an unselected company.')
        return graph

    def _verify_pending_history(self, old, new):
        roots = old.move_ids | new.move_ids
        advanced = roots.filtered(lambda m:m.move_orig_ids or m.move_dest_ids or
            (m.state not in ('done', 'cancel') and (m.origin_returned_move_id or m.location_id.usage != 'supplier' or m.location_dest_id.usage != 'internal')))
        if not advanced:
            return
        source = self._native_route_graph(old.move_ids)
        mapped = self.env['stock.move']
        for movement in source.sorted('id'):
            try:
                destination = self._explicit(movement)
            except UserError as exc:
                raise UserError('PO %s: pending supplier returns and multi-step routes need explicit matches for every stock movement. %s' % (old.order_id.name, exc)) from exc
            if movement.company_id not in self.source_company_ids or destination.company_id != self.target_company_id:
                raise UserError('Complex route reaches an unselected company.')
            if movement.state == 'done' and movement.date.date() > self.cutover_date:
                raise UserError('Complex receipt history falls after the cutover date; include the full history.')
            if destination in mapped:
                raise UserError('Two stock movements cannot share the same destination history.')
            mapped |= destination
            if (destination.state != movement.state or destination.product_id != self._target(movement.product_id) or
                    destination.product_uom != movement.product_uom or destination.date != movement.date or
                    float_compare(destination.product_uom_qty, movement.product_uom_qty, precision_rounding=movement.product_uom.rounding) or
                    float_compare(destination.quantity, movement.quantity, precision_rounding=movement.product_uom.rounding) or
                    destination.to_refund != movement.to_refund or
                    not self.target_company_id.currency_id.is_zero(destination.value - movement.value) or
                    destination.price_unit != movement.price_unit or
                    destination.location_id != self._target(movement.location_id) or destination.location_dest_id != self._target(movement.location_dest_id)):
                raise UserError('Existing complex route has different state, quantity, date or location.')
            expected_line = self._explicit(movement.purchase_line_id) if movement.purchase_line_id else self.env['purchase.order.line']
            if destination.purchase_line_id != expected_line:
                raise UserError('Complex route PO-line ownership differs.')
            for name in ('move_orig_ids', 'move_dest_ids', 'origin_returned_move_id', 'returned_move_ids'):
                expected = self.env['stock.move'].browse([self._explicit(r).id for r in movement[name]])
                if destination[name] != expected:
                    raise UserError('Existing receipt/return route links differ from the original history.')
            self._verify_tracked_receipt(movement, destination)
            self._verify_valuation_history(movement, destination)
        # No extra target internal legs or return branches may be silently omitted.
        target_graph = self._native_route_graph(new.move_ids)
        if mapped != target_graph:
            raise UserError('Destination complex route contains unmatched stock movements.')


class NativeValueMapping(models.Model):
    _inherit = 'company.document.mapping'
    _value_choices = [('product.value', 'Existing manual stock valuation adjustment')]
    source_record = fields.Reference(selection_add=_value_choices, ondelete={'product.value': remove_optional_document_matches})
    target_record = fields.Reference(selection_add=_value_choices, ondelete={'product.value': remove_optional_document_matches})
    model_name = fields.Selection(selection_add=_value_choices, ondelete={'product.value': remove_optional_document_matches})
