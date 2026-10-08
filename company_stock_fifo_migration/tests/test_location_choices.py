from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged, Form
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestLocationChoices(TestStockValuationCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Location choice target")
        cls.target_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.target_company.id)], limit=1)

    def _cutover(self):
        return self.env["company.stock.warehouse.cutover"].with_context(
            allowed_company_ids=(self.env.company | self.target_company).ids).create({
                "source_warehouse_id": self.warehouse.id,
                "target_company_id": self.target_company.id,
                "target_warehouse_id": self.target_warehouse.id})

    def _bin(self, parent, name):
        return self.env["stock.location"].create({
            "name": name, "company_id": parent.company_id.id,
            "usage": "internal", "location_id": parent.id})

    def test_form_choice_routes_stock_to_existing_bin_without_creating_location(self):
        source = self._bin(self.warehouse.lot_stock_id, "Source Shelf")
        target = self._bin(self.target_warehouse.lot_stock_id, "Destination Fridge")
        self._make_in_move(self.product_fifo_auto, 5, 12, location_dest_id=source.id)
        cutover = self._cutover()
        with Form(cutover) as form:
            with form.location_choice_ids.new() as choice:
                choice.source_location_id = source
                choice.target_location_id = target
        cutover.action_preview()
        self.assertIn("Destination Fridge", cutover.preview_html)
        self.registry_enter_test_mode()
        cutover.action_apply()
        product = cutover.stock_batch_id.product_line_ids.target_product_id
        quantity = sum(self.env["stock.quant"].search([
            ("product_id", "=", product.id), ("location_id", "=", target.id)]).mapped("quantity"))
        self.assertEqual(quantity, 5)
        self.assertFalse(cutover.stock_batch_id.location_line_ids.created_location_id)
        with self.assertRaises(UserError):
            cutover.location_choice_ids.unlink()
        with self.assertRaises(UserError):
            cutover.location_choice_ids.target_location_id = self.target_warehouse.lot_stock_id

    def test_merging_parent_locations_reuses_one_new_child_and_reconciles_stock(self):
        first = self._bin(self.warehouse.lot_stock_id, "Old room A")
        second = self._bin(self.warehouse.lot_stock_id, "Old room B")
        shelf_a = self._bin(first, "Shared Shelf")
        shelf_b = self._bin(second, "Shared Shelf")
        self._make_in_move(self.product_fifo_auto, 3, 10, location_dest_id=shelf_a.id)
        self._make_in_move(self.product_fifo_auto, 2, 20, location_dest_id=shelf_b.id)
        cutover = self._cutover()
        cutover.write({"location_choice_ids": [Command.create({
            "source_location_id": source.id,
            "target_location_id": self.target_warehouse.lot_stock_id.id}) for source in (first, second)]})
        cutover.action_preview()
        self.registry_enter_test_mode()
        cutover.action_apply()
        shelves = self.env["stock.location"].search([
            ("location_id", "=", self.target_warehouse.lot_stock_id.id), ("name", "=", "Shared Shelf")])
        self.assertEqual(len(shelves), 1)
        product = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(product.qty_available, 5)
        self.assertEqual(product.total_value, 70)
        self.assertEqual(product._run_fifo(3), 30)
        self.assertEqual(sum(self.env["stock.quant"].search([
            ("product_id", "=", product.id), ("location_id", "=", shelves.id)]).mapped("quantity")), 5)

    def test_choice_changes_invalidate_preview_and_outside_warehouse_is_rejected(self):
        self._make_in_move(self.product_fifo_auto, 3, 10)
        target = self._bin(self.target_warehouse.lot_stock_id, "Chosen Bin")
        cutover = self._cutover()
        cutover.action_preview()
        cutover.write({"location_choice_ids": [Command.create({
            "source_location_id": self.warehouse.lot_stock_id.id,
            "target_location_id": target.id})]})
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.preview_hash)
        cutover.action_preview()
        cutover.location_choice_ids.target_location_id = self.target_warehouse.lot_stock_id
        self.assertEqual(cutover.state, "draft")
        outside = self.env["stock.location"].create({
            "name": "Outside destination warehouse", "usage": "internal",
            "company_id": self.target_company.id})
        with self.assertRaises(UserError), self.env.cr.savepoint():
            cutover.location_choice_ids.target_location_id = outside
        with self.assertRaises(UserError), self.env.cr.savepoint():
            cutover.location_choice_ids.target_location_id = self.warehouse.lot_stock_id
