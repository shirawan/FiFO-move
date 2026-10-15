"""Verify already-existing tracked receipt history without changing stock."""
from collections import Counter
from odoo import fields, models
from odoo.exceptions import UserError
from odoo.addons.company_document_cutover.models.migration import packed, remove_optional_document_matches


class TrackedPurchaseHistory(models.Model):
    _inherit = 'company.document.cutover'

    def _target(self, record):
        target = super()._target(record)
        if not record:
            return target
        if record._name == 'product.product' and self.include_purchase and self._needs_native_product(record) and record.tracking != target.tracking:
            raise UserError('Product lots/serials tracking differs. Match products with the same tracking policy.')
        if record._name == 'stock.lot':
            if target.product_id != self._target(record.product_id) or target.name != record.name:
                raise UserError('Lot/serial must match the same product and original lot/serial number.')
            for name in ('expiration_date', 'use_date', 'removal_date', 'alert_date'):
                if name in record._fields and record[name] != target[name]:
                    raise UserError('Lot/serial expiry dates differ.')
        if record._name == 'stock.location' and (record.usage != target.usage or not target.active):
            raise UserError('Matched stock locations have different purposes.')
        if record._name == 'stock.package':
            if record.package_type_id != target.package_type_id or self._target(record.parent_package_id) != target.parent_package_id:
                raise UserError('Package type or parent package differs.')
        return target

    def _tracked_receipt_evidence(self, movement, source=False):
        get = self._target if source else lambda r:r
        totals = Counter()
        for line in movement.move_line_ids:
            key = packed([get(line.product_id).id, get(line.lot_id).id,
                          get(line.owner_id).id, get(line.package_id).id, get(line.result_package_id).id,
                          get(line.location_id).id, get(line.location_dest_id).id])
            totals[key] += line.product_uom_id._compute_quantity(line.quantity, movement.product_id.uom_id, round=False)
        return {key:round(value,8) for key,value in sorted(totals.items())}

    def _verify_tracked_receipt(self, source, target):
        detailed = source.product_id.tracking != 'none' or any(
            line.lot_id or line.owner_id or line.package_id or line.result_package_id
            for line in source.move_line_ids | target.move_line_ids)
        if detailed and self._tracked_receipt_evidence(source, True) != self._tracked_receipt_evidence(target):
            raise UserError('Lot/serial, owner, package, location or detailed receipt quantity differs.')

    def _mapping_config(self, record):
        if record._name == 'stock.lot':
            names = ['name','product_id','company_id','write_date']
            names += [n for n in ('expiration_date','use_date','removal_date','alert_date') if n in record._fields]
            return record.read(names)[0]
        if record._name == 'stock.location':
            return record.read(['usage','company_id','active','write_date'])[0]
        if record._name == 'stock.package':
            return record.read(['package_type_id','parent_package_id','write_date'])[0]
        return super()._mapping_config(record)

    def _lock_tables(self):
        return (*super()._lock_tables(), 'stock_lot', 'stock_package', 'stock_package_type', 'stock_quant')


class TrackedHistoryMapping(models.Model):
    _inherit = 'company.document.mapping'
    _tracking_choices = [('stock.lot','Existing lot or serial number'), ('stock.location','Stock location'),
                         ('stock.package','Existing package')]
    source_record = fields.Reference(selection_add=_tracking_choices, ondelete={k:remove_optional_document_matches for k,v in _tracking_choices})
    target_record = fields.Reference(selection_add=_tracking_choices, ondelete={k:remove_optional_document_matches for k,v in _tracking_choices})
    model_name = fields.Selection(selection_add=_tracking_choices, ondelete={k:remove_optional_document_matches for k,v in _tracking_choices})
