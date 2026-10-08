"""Optional location routing within the selected destination warehouse."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class WarehouseLocationChoice(models.Model):
    _name = "company.stock.warehouse.location.choice"
    _description = "Warehouse Destination Location Choice"

    cutover_id = fields.Many2one("company.stock.warehouse.cutover", required=True, ondelete="cascade")
    source_location_id = fields.Many2one("stock.location", required=True, ondelete="restrict")
    target_location_id = fields.Many2one("stock.location", required=True, ondelete="restrict")
    _source_unique = models.Constraint("UNIQUE(cutover_id, source_location_id)",
                                      "Choose a destination only once for each source location.")

    def _editable(self):
        parents = self.cutover_id
        parents._lock()
        for parent in parents:
            parent._operator()
            if parent.state == "done":
                raise UserError(_("Completed cutover location choices cannot be changed."))
        return parents

    @api.constrains("cutover_id", "source_location_id", "target_location_id")
    def _check_locations(self):
        for line in self:
            for location, warehouse in (
                (line.source_location_id, line.cutover_id.source_warehouse_id),
                (line.target_location_id, line.cutover_id.target_warehouse_id),
            ):
                location.check_access("read")
                if (not warehouse or location.company_id != warehouse.company_id
                        or location.usage not in ("internal", "transit")
                        or not location.parent_path.startswith(warehouse.view_location_id.parent_path)):
                    raise UserError(_("Choose stock locations inside the selected source and destination warehouses."))

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env["company.stock.warehouse.cutover"].browse(
            [vals.get("cutover_id") for vals in vals_list])
        parents._lock()
        for parent in parents:
            parent._operator()
            if parent.state == "done":
                raise UserError(_("Completed cutover location choices cannot be changed."))
        rows = super().create(vals_list)
        parents._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return rows

    def write(self, vals):
        if "cutover_id" in vals:
            raise UserError(_("Location choices cannot be moved to another cutover."))
        parents = self._editable()
        result = super().write(vals)
        parents._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return result

    def unlink(self):
        parents = self._editable()
        result = super().unlink()
        parents._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return result
