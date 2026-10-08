from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon

from .test_stock_fifo_migration import TestCompanyStockFifoMigration


@tagged("post_install", "-at_install")
class TestStockFifoCutoverLots(TestStockValuationCommon):
    _batch = TestCompanyStockFifoMigration._batch

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Stock Cutover Lot Target")

    def _zero_receipt(self, product, quantity):
        move = self.env["stock.move"].create({
            "product_id": product.id,
            "product_uom": product.uom_id.id,
            "product_uom_qty": quantity,
            "location_id": self.supplier_location.id,
            "location_dest_id": self.stock_location.id,
            "picking_type_id": self.picking_type_in.id,
        })
        move._action_confirm()
        move._action_assign()
        move.picked = True
        # Native manual valuation, recorded before completion: zero is a real cost.
        self.env["product.value"].create({
            "move_id": move.id,
            "company_id": self.env.company.id,
            "value": 0,
        })
        move._action_done()
        return move

    def _lot_stock(self):
        product = self.product_fifo_auto
        product.write({
            "company_id": self.env.company.id,
            "tracking": "lot",
            "lot_valuated": True,
        })
        lot = self.env["stock.lot"].create({
            "name": "FIFO-LOT-CUTOVER",
            "ref": "Supplier lot reference",
            "company_id": self.env.company.id,
            "product_id": product.id,
            "expiration_date": "2027-03-01 12:00:00",
            "use_date": "2027-02-01 12:00:00",
            "removal_date": "2027-02-15 12:00:00",
            "alert_date": "2027-01-15 12:00:00",
        })
        receipts = (
            self._make_in_move(product, 10, 10, lot_ids=lot)
            | self._make_in_move(product, 10, 20, lot_ids=lot)
        )
        return product, lot, receipts

    def test_zero_cost_fifo_tranche_is_not_repriced_at_target_product_cost(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        receipts = self._zero_receipt(product, 5) | self._make_in_move(product, 5, 20)
        self.assertEqual(receipts.mapped("value"), [0, 100])
        batch, target = self._batch(product, self.target_company)
        target.standard_price = 99
        batch.action_preview()
        self.assertEqual(batch.tranche_line_ids.mapped("unit_value"), [0, 20])
        batch.action_check()
        self.registry_enter_test_mode()

        batch.action_apply()

        self.assertEqual(batch.state, "done")
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(target.qty_available, 10)
        self.assertEqual(target.total_value, 100)
        opening = batch.created_move_ids.filtered(
            lambda move: move.company_id == self.target_company
        )
        self.assertEqual(opening.mapped("value"), [0, 100])
        free_units = self._make_out_move(target, 5, company=self.target_company)
        paid_unit = self._make_out_move(target, 1, company=self.target_company)
        self.assertEqual(free_units.value, 0)
        self.assertEqual(paid_unit.value, 20)
        self.assertEqual(target.qty_available, 4)
        self.assertEqual(target.total_value, 80)
        self.assertEqual(receipts.mapped("value"), [0, 100])

    def test_whole_lot_valued_stock_keeps_identity_dates_quantity_and_value(self):
        product, source_lot, receipts = self._lot_stock()
        self.assertEqual(product.qty_available, 20)
        self.assertEqual(product.total_value, 300)
        self.assertEqual(source_lot.standard_price, 15)
        batch, target = self._batch(product, self.target_company)
        batch.action_preview()
        self.assertEqual(batch.tranche_line_ids.mapped("available_quantity"), [10, 10])
        self.assertEqual(batch.tranche_line_ids.mapped("unit_value"), [10, 20])
        batch.action_check()
        self.registry_enter_test_mode()

        batch.action_apply()

        target_lot = batch.lot_line_ids.created_lot_id.with_company(
            self.target_company
        ).with_context(allowed_company_ids=self.target_company.ids)
        self.assertEqual(batch.state, "done")
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(target.qty_available, 20)
        self.assertEqual(target.total_value, 300)
        self.assertEqual(target_lot.company_id, self.target_company)
        self.assertEqual(target_lot.product_id, target)
        self.assertEqual(target_lot.product_qty, 20)
        self.assertEqual(target_lot.standard_price, 15)
        for name in ("name", "ref", "expiration_date", "use_date", "removal_date", "alert_date"):
            self.assertEqual(target_lot[name], source_lot[name])
        closing = batch.created_move_ids.filtered(
            lambda move: move.company_id == self.env.company
        )
        opening = batch.created_move_ids.filtered(
            lambda move: move.company_id == self.target_company
        )
        self.assertEqual(sum(closing.mapped("value")), 300)
        self.assertEqual(opening.mapped("value"), [100, 200])
        self.assertEqual(receipts.mapped("value"), [100, 200])
        outgoing = self._make_out_move(
            target, 2, company=self.target_company, lot_ids=target_lot,
        )
        # Odoo 19 lot-valued deliveries use the lot's native average unit cost.
        self.assertEqual(outgoing.value, 30)
        self.assertEqual(target_lot.product_qty, 18)

    def test_partial_lot_across_source_warehouses_is_rejected(self):
        product, lot, receipts = self._lot_stock()
        other_warehouse = self.env["stock.warehouse"].create({
            "name": "Other Source Lot Warehouse",
            "code": "LOT2",
            "company_id": self.env.company.id,
        })
        transfer = self.env["stock.move"].create({
            "product_id": product.id,
            "product_uom": product.uom_id.id,
            "product_uom_qty": 5,
            "location_id": self.stock_location.id,
            "location_dest_id": other_warehouse.lot_stock_id.id,
            "picking_type_id": self.warehouse.int_type_id.id,
        })
        transfer._action_confirm()
        transfer.move_line_ids.unlink()
        transfer.move_line_ids = [Command.create({
            "product_id": product.id,
            "product_uom_id": product.uom_id.id,
            "quantity": 5,
            "location_id": self.stock_location.id,
            "location_dest_id": other_warehouse.lot_stock_id.id,
            "lot_id": lot.id,
            "picked": True,
        })]
        transfer.picked = True
        transfer._action_done()
        batch, target = self._batch(product, self.target_company)
        self.assertEqual(len(batch.warehouse_line_ids.filtered("selected")), 1)

        with self.assertRaisesRegex(UserError, "Partial transfer of a lot-valued lot"):
            batch.action_preview()

        self.assertEqual(product.qty_available, 20)
        self.assertEqual(product.total_value, 300)
        self.assertEqual(target.qty_available, 0)
        self.assertEqual(receipts.mapped("value"), [100, 200])
        self.assertFalse(batch.created_move_ids)
