"""Explicit destination choices; stock and valuation remain owned by the cutover."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class WarehouseProductChoice(models.Model):
    _name = "company.stock.warehouse.product.choice"
    _description = "Warehouse Destination Product Choice"

    cutover_id = fields.Many2one("company.stock.warehouse.cutover", required=True, ondelete="cascade")
    source_product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    target_product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    target_record_id = fields.Integer(related="target_product_id.id", string="Destination ID")
    target_reference = fields.Char(related="target_product_id.default_code", string="Reference")
    _source_unique = models.Constraint("UNIQUE(cutover_id, source_product_id)",
                                      "Choose a destination only once for each source product.")

    def _editable(self):
        parents = self.cutover_id
        parents._lock()
        for parent in parents:
            parent._operator()
            if parent.state == "done":
                raise UserError(_("Completed cutover product choices cannot be changed."))
        return parents

    @api.constrains("cutover_id", "source_product_id", "target_product_id")
    def _check_company(self):
        for line in self:
            line.source_product_id.check_access("read")
            line.target_product_id.check_access("read")
            if line.target_product_id.company_id != line.cutover_id.target_company_id:
                raise UserError(_("Choose a product belonging to the Target Company."))

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env["company.stock.warehouse.cutover"].browse(
            [vals.get("cutover_id") for vals in vals_list])
        parents._lock()
        for parent in parents:
            parent._operator()
            if parent.state == "done":
                raise UserError(_("Completed cutover product choices cannot be changed."))
        rows = super().create(vals_list)
        parents._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return rows

    def write(self, vals):
        if "cutover_id" in vals:
            raise UserError(_("Product choices cannot be moved to another cutover."))
        parents = self._editable()
        result = super().write(vals)
        parents._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return result

    def unlink(self):
        parents = self._editable()
        result = super().unlink()
        parents._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return result
