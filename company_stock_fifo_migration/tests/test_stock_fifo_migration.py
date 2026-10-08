from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestCompanyStockFifoMigration(TestStockValuationCommon):
    def _batch(self, product, target):
        context = {"allowed_company_ids": [self.env.company.id, target.id]}
        target_product = product.with_context(context).with_company(target).copy({
            "company_id": target.id, "name": "Target FIFO Ingredient",
        })
        target_category = self.env["product.category"].with_context(context).with_company(target).create({
            "name": "Target FIFO Inventory", "property_cost_method": "fifo",
            "property_valuation": "real_time",
        })
        target_product.categ_id = target_category
        source_kit = self.env["product.template"].create({
            "name": "Source POS Kit", "company_id": self.env.company.id,
            "available_in_pos": True,
        })
        target_kit = source_kit.with_context(context).copy({"company_id": target.id})
        self.env["mrp.bom"].create({
            "product_tmpl_id": source_kit.id, "company_id": self.env.company.id,
            "type": "phantom", "bom_line_ids": [Command.create({
                "product_id": product.id, "product_qty": 1,
            })],
        })
        provider = self.env["company.kit.bom.migration.batch"].with_context(context).create({
            "source_company_id": self.env.company.id, "target_company_id": target.id,
        })
        provider.action_scan()
        for line in provider.mapping_line_ids:
            line.target_product_id = (
                target_product if line.source_product_id == product
                else target_kit.product_variant_id
            )
        source_clearing = self.env["account.account"].create({
            "name": "Source Cutover Clearing", "code": "CUT01",
            "account_type": "asset_current",
        })
        target_clearing = self.env["account.account"].with_context(context).with_company(target).create({
            "name": "Target Cutover Clearing", "code": "CUT02",
            "account_type": "asset_current", "company_ids": [Command.set(target.ids)],
        })
        batch = self.env["company.stock.fifo.migration"].with_context(context).create({
            "source_company_id": self.env.company.id, "target_company_id": target.id,
            "mapping_batch_ref": "company.kit.bom.migration.batch,%s" % provider.id,
            "source_clearing_account_id": source_clearing.id,
            "target_clearing_account_id": target_clearing.id,
        })
        batch.action_import_mappings()
        warehouse_line = batch.warehouse_line_ids.filtered(
            lambda line: line.source_warehouse_id == self.warehouse
        )
        target_warehouse = self.env["stock.warehouse"].search([
            ("company_id", "=", target.id),
        ], limit=1)
        warehouse_line.write({"selected": True, "target_warehouse_id": target_warehouse.id})
        batch.action_prepare_locations()
        return batch, target_product

    def test_preview_reports_remaining_fifo_without_changing_stock(self):
        source = self.env.company
        target = self._create_company(name="FIFO Cutover Target")
        product = self.product_fifo_auto
        product.company_id = source
        self._make_in_move(product, 10, 10)
        self._make_in_move(product, 10, 20)
        self._make_out_move(product, 5)
        batch, target_product = self._batch(product, target)
        stock_moves_before = self.env["stock.move"].search_count([])
        account_moves_before = self.env["account.move"].search_count([])
        warehouse_count_before = self.env["stock.warehouse"].search_count([])

        batch.action_preview()

        self.assertEqual(product.qty_available, 15)
        self.assertEqual(product.total_value, 250)
        self.assertEqual(batch.product_line_ids.source_quantity, 15)
        self.assertEqual(batch.tranche_line_ids.mapped("available_quantity"), [5, 10])
        self.assertEqual(batch.tranche_line_ids.mapped("unit_value"), [10, 20])
        self.assertEqual(target_product.qty_available, 0)
        self.assertEqual(self.env["stock.move"].search_count([]), stock_moves_before)
        self.assertEqual(self.env["account.move"].search_count([]), account_moves_before)
        self.assertEqual(self.env["stock.warehouse"].search_count([]), warehouse_count_before)

    def test_apply_preserves_fifo_and_posts_opening_entries(self):
        target = self._create_company(name="FIFO Apply Target")
        product = self.product_fifo_auto
        product.company_id = self.env.company
        receipts = self._make_in_move(product, 10, 10) | self._make_in_move(product, 10, 20)
        previous_sale = self._make_out_move(product, 5)
        history = receipts | previous_sale
        before = [(move.id, move.company_id.id, move.date, move.value, move.write_date) for move in history]
        batch, target_product = self._batch(product, target)
        batch.action_preview()

        batch.action_check()
        self.registry_enter_test_mode()
        batch.action_apply()

        self.assertEqual(batch.state, "done")
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(product.total_value, 0)
        self.assertEqual(target_product.qty_available, 15)
        self.assertEqual(target_product.total_value, 250)
        self.assertEqual(batch.product_line_ids.actual_quantity, 15)
        self.assertEqual(batch.product_line_ids.actual_value, 250)
        self.assertEqual(batch.product_line_ids.value_difference, 0)
        self.assertEqual([(move.id, move.company_id.id, move.date, move.value, move.write_date)
                          for move in history], before)
        closing = batch.created_move_ids.filtered(lambda move: move.company_id == self.env.company)
        opening = batch.created_move_ids.filtered(lambda move: move.company_id == target)
        self.assertEqual(sum(closing.mapped("value")), 250)
        self.assertEqual(opening.mapped("value"), [50, 200])
        self.assertTrue(all(move.account_move_id.state == "posted" for move in batch.created_move_ids))
        self.assertEqual(sum(closing.account_move_id.line_ids.filtered(
            lambda line: line.account_id == self.env.company.account_stock_valuation_id
        ).mapped("credit")), 250)
        self.assertEqual(sum(opening.account_move_id.line_ids.filtered(
            lambda line: line.account_id == target.account_stock_valuation_id
        ).mapped("debit")), 250)
        outgoing = self._make_out_move(target_product, 6, company=target)
        self.assertEqual(outgoing.value, 70)
        self.assertEqual(target_product.total_value, 180)

    def test_completed_cutover_is_immutable_and_source_product_cannot_run_again(self):
        target = self._create_company(name="FIFO Repeat Target")
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        batch, target_product = self._batch(product, target)
        batch.action_preview()
        batch.action_check()
        self.registry_enter_test_mode()
        batch.action_apply()
        with self.assertRaisesRegex(UserError, "immutable"):
            batch.write({"cutover_at": fields.Datetime.now()})
        with self.assertRaisesRegex(UserError, "immutable"):
            batch.product_line_ids.write({"selected": False})
        with self.assertRaisesRegex(UserError, "cannot be deleted"):
            batch.unlink()
        with self.assertRaisesRegex(UserError, "immutable"):
            batch.action_create_clearing_accounts()
        with self.assertRaisesRegex(UserError, "Check"):
            batch.action_apply()

        # A replenishment and a fresh, empty Target product must not make a
        # previously completed Source-product cutover eligible again.
        self._make_in_move(product, 2, 30)
        new_target = target_product.copy({"name": "Second Target Ingredient"})
        batch.mapping_batch_ref.mapping_line_ids.filtered(
            lambda line: line.source_product_id == product
        ).target_product_id = new_target
        second = self.env[batch._name].with_context(batch.env.context).create({
            "source_company_id": batch.source_company_id.id,
            "target_company_id": target.id,
            "mapping_batch_ref": "company.kit.bom.migration.batch,%s" % batch.mapping_batch_ref.id,
            "source_clearing_account_id": batch.source_clearing_account_id.id,
            "target_clearing_account_id": batch.target_clearing_account_id.id,
        })
        second.action_import_mappings()
        warehouse = batch.warehouse_line_ids.filtered("selected")
        second.warehouse_line_ids.filtered(
            lambda line: line.source_warehouse_id == warehouse.source_warehouse_id
        ).write({"selected": True, "target_warehouse_id": warehouse.target_warehouse_id.id})
        second.action_prepare_locations()
        with self.assertRaisesRegex(UserError, "already has a completed"):
            second.action_preview()
        self.assertEqual(product.qty_available, 2)
        self.assertEqual(new_target.qty_available, 0)

    def test_currency_rounding_difference_rolls_back_every_cutover_change(self):
        target = self._create_company(name="FIFO Rounding Target")
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 3, 1 / 3)
        self._make_in_move(product, 3, 1 / 3)
        self._make_out_move(product, 1)
        source_bin = self.env["stock.location"].create({
            "name": "Fractional Cost Bin", "usage": "internal",
            "company_id": self.env.company.id, "location_id": self.stock_location.id,
        })
        self._make_out_move(product, 4, location_dest_id=source_bin.id,
                            picking_type_id=self.warehouse.int_type_id.id)
        batch, target_product = self._batch(product, target)
        target_warehouse = batch.warehouse_line_ids.filtered("selected").target_warehouse_id
        target_bin = self.env["stock.location"].with_context(batch.env.context).create({
            "name": "Fractional Cost Bin", "usage": "internal",
            "company_id": target.id, "location_id": target_warehouse.lot_stock_id.id,
        })
        batch.location_line_ids.filtered(
            lambda line: line.source_location_id == source_bin
        ).target_location_id = target_bin
        batch.action_preview()
        batch.action_check()
        counts_before = {model: self.env[model].search_count([]) for model in (
            "stock.move", "account.move", "stock.location", "product.value",
        )}
        self.registry_enter_test_mode()
        with self.assertRaisesRegex(UserError, "did not reconcile"):
            batch.action_apply()
        self.assertEqual(batch.state, "ready")
        self.assertEqual(product.qty_available, 5)
        self.assertEqual(product.total_value, 1.67)
        self.assertEqual(target_product.qty_available, 0)
        self.assertFalse(batch.created_move_ids)
        self.assertFalse(self.env["ir.config_parameter"].search_count([
            ("key", "=", "company_stock_fifo_migration.applied.%s.%s" % (self.env.company.id, product.id)),
        ]))
        self.assertEqual(counts_before, {
            model: self.env[model].search_count([]) for model in counts_before
        })

    def test_native_currency_rounding_keeps_totals_and_records_unit_cost_change(self):
        target = self._create_company(name="FIFO Unit Precision Target")
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 3, 1 / 3)
        self._make_out_move(product, 1)
        batch, target_product = self._batch(product, target)
        batch.action_preview()
        batch.action_check()
        self.registry_enter_test_mode()
        batch.action_apply()
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(target_product.qty_available, 2)
        self.assertEqual(target_product.total_value, .67)
        self.assertEqual(batch.state, "done")
        self.assertIn("0.333333", batch.rounding_note)
        self.assertIn("0.335", batch.rounding_note)
