from odoo import Command
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon

from . import test_stock_fifo_migration
from ..models.snapshot import LOCATION_ROLES


@tagged("post_install", "-at_install")
class TestStockFifoWarehouseCutover(TestStockValuationCommon):
    _batch = test_stock_fifo_migration.TestCompanyStockFifoMigration._batch

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Warehouse Cutover Target")

    def test_recreated_warehouse_keeps_manufacturing_steps_and_custom_stock_location(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        source_custom = self.env["stock.location"].create({
            "name": "Cold Room", "usage": "internal",
            "company_id": self.env.company.id, "location_id": self.stock_location.id,
        })
        self._make_in_move(product, 4, 12, location_dest_id=source_custom.id)
        self._make_in_move(product, 6, 20)
        batch, target_product = self._batch(product, self.target_company)
        self.warehouse.manufacture_steps = "pbm_sam"
        warehouse_line = batch.warehouse_line_ids.filtered(
            lambda line: line.source_warehouse_id == self.warehouse
        )
        batch.write({"warehouse_line_ids": [Command.update(warehouse_line.id, {
            "action": "create", "target_warehouse_id": False,
            "target_name": "Recreated FIFO Warehouse", "target_code": "CUTWH",
        })]})
        batch.action_prepare_locations()
        batch.action_preview()
        batch.action_check()
        self.registry_enter_test_mode()

        batch.action_apply()

        target_warehouse = warehouse_line.created_warehouse_id
        self.assertTrue(target_warehouse)
        self.assertEqual(target_warehouse.company_id, self.target_company)
        self.assertEqual(target_warehouse.manufacture_steps, "pbm_sam")
        self.assertEqual(target_warehouse.reception_steps, self.warehouse.reception_steps)
        self.assertEqual(target_warehouse.delivery_steps, self.warehouse.delivery_steps)
        for role in LOCATION_ROLES:
            location = target_warehouse[role]
            if location:
                self.assertEqual(location.company_id, self.target_company)
                self.assertTrue(location.parent_path.startswith(target_warehouse.view_location_id.parent_path))
        for role in (
            "in_type_id", "out_type_id", "int_type_id", "pick_type_id",
            "pack_type_id", "qc_type_id", "store_type_id", "xdock_type_id",
            "manu_type_id", "pbm_type_id", "sam_type_id",
        ):
            operation = target_warehouse[role]
            if operation:
                self.assertEqual(operation.company_id, self.target_company)
                self.assertEqual(operation.warehouse_id, target_warehouse)
        target_custom = batch.location_line_ids.filtered(
            lambda line: line.source_location_id == source_custom
        ).created_location_id
        self.assertEqual(target_custom.name, "Cold Room")
        self.assertEqual(target_custom.location_id, target_warehouse.lot_stock_id)
        self.assertEqual(target_custom.company_id, self.target_company)
        self.assertEqual(self.env["stock.quant"]._get_available_quantity(target_product, target_custom), 4)
        self.assertEqual(self.env["stock.quant"]._get_available_quantity(
            target_product, target_warehouse.lot_stock_id, strict=True
        ), 6)
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(target_product.qty_available, 10)
        self.assertEqual(target_product.total_value, 168)
        self.assertEqual(batch.product_line_ids.quantity_difference, 0)
        self.assertEqual(batch.product_line_ids.value_difference, 0)

    def test_partial_warehouses_move_reviewed_fifo_prefix_and_leave_correct_source_cost(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        other_warehouse = self.env["stock.warehouse"].create({
            "name": "Unselected Source Warehouse", "code": "OTHWH",
            "company_id": self.env.company.id,
        })
        self._make_in_move(product, 10, 10)
        self._make_in_move(product, 10, 20)
        self._make_out_move(product, 5, location_dest_id=other_warehouse.lot_stock_id.id,
                            picking_type_id=self.warehouse.int_type_id.id)
        self.assertEqual(product.qty_available, 20)
        self.assertEqual(product.total_value, 300)
        batch, target_product = self._batch(product, self.target_company)
        batch.action_preview()
        self.assertTrue(batch.partial_selection)
        self.assertEqual(batch.product_line_ids.source_quantity, 15)
        self.assertEqual(batch.tranche_line_ids.mapped("selected_quantity"), [0, 0])
        self.assertFalse(batch.warehouse_line_ids.filtered(
            lambda line: line.source_warehouse_id == other_warehouse
        ).selected)
        batch.write({
            "tranche_line_ids": [
                Command.update(batch.tranche_line_ids[0].id, {"selected_quantity": 10}),
                Command.update(batch.tranche_line_ids[1].id, {"selected_quantity": 5}),
            ],
            "allocation_reviewed": True,
        })
        self.assertTrue(batch.allocation_reviewed)
        self.assertEqual(batch.state, "review")
        batch.action_check()
        self.assertEqual(batch.product_line_ids.opening_value, 200)
        self.registry_enter_test_mode()

        batch.action_apply()

        self.assertEqual(batch.state, "done")
        self.assertEqual(product.qty_available, 5)
        self.assertEqual(product.total_value, 100)
        self.assertEqual(self.env["stock.quant"]._get_available_quantity(
            product, other_warehouse.lot_stock_id
        ), 5)
        self.assertEqual(target_product.qty_available, 15)
        self.assertEqual(target_product.total_value, 200)
        self.assertEqual(batch.product_line_ids.actual_quantity, 15)
        self.assertEqual(batch.product_line_ids.actual_value, 200)
        self.assertEqual(batch.product_line_ids.value_difference, 0)
        outgoing = self._make_out_move(target_product, 11, company=self.target_company)
        self.assertEqual(outgoing.value, 120)
        self.assertEqual(target_product.total_value, 80)
