from lxml import html
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError, RedirectWarning
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestWholeWarehouseCutover(TestStockValuationCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Whole Warehouse Target")
        cls.target_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.target_company.id),
        ], limit=1)

    def _cutover(self):
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        return self.env["company.stock.warehouse.cutover"].with_context(context).create({
            "source_warehouse_id": self.warehouse.id,
            "target_company_id": self.target_company.id,
            "target_warehouse_id": self.target_warehouse.id,
        })

    def _user(self, login, companies, group):
        return self.env["res.users"].create({
            "name": login,
            "login": login,
            "company_id": self.env.company.id,
            "company_ids": [Command.set(companies.ids)],
            "group_ids": [Command.set([self.env.ref(group).id])],
        })

    def _target_category(self, source):
        return self.env["product.category"].with_context(
            allowed_company_ids=(self.env.company | self.target_company).ids,
        ).with_company(self.target_company).create({
            "name": "Target inventory for %s" % source.name,
            "property_cost_method": source.cost_method,
            "property_valuation": source.valuation,
            "property_stock_valuation_account_id": self.target_company.account_stock_valuation_id.id,
            "property_stock_journal": self.target_company.account_stock_journal_id.id,
        })

    def test_preserves_native_costing_and_valuation_modes(self):
        self.registry_enter_test_mode()
        for method in ("fifo", "average", "standard"):
            for valuation in ("real_time", "periodic"):
                with self.subTest(method=method, valuation=valuation), self.env.cr.savepoint():
                    category = self.product_fifo_auto.categ_id.copy({
                        "property_cost_method": method, "property_valuation": valuation,
                    })
                    product = self.product_fifo_auto.copy({
                        "name": "Native %s %s" % (method, valuation),
                        "categ_id": category.id, "standard_price": 12,
                    })
                    self._make_in_move(product, 10, 12)
                    self._make_in_move(product, 10, 18)
                    before = product.total_value
                    cutover = self._cutover()
                    cutover.action_preview()
                    cutover.action_apply()
                    target = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
                    self.assertEqual(target.cost_method, method)
                    self.assertEqual(target.valuation, valuation)
                    self.assertEqual(target.qty_available, 20)
                    self.assertAlmostEqual(target.total_value, before, places=2)
                    self.assertAlmostEqual(product.total_value, 0, places=2)
                    journals = cutover.stock_batch_id.created_move_ids.account_move_id
                    self.assertEqual(bool(journals), valuation == "real_time")
                    self.assertTrue(all(move.state == "posted" for move in journals))

    def test_new_warehouse_cutover_leaves_old_operations_by_default(self):
        self.assertTrue(self._cutover().release_source_reservations)

    def test_apply_does_not_replay_legacy_interactive_checks(self):
        self.registry_enter_test_mode()
        self._make_in_move(self.product_fifo_auto, 2, 10)
        cutover = self._cutover()
        cutover.action_preview()
        Batch = type(self.env["company.stock.fifo.migration"])
        with patch.object(Batch, "action_preview", side_effect=AssertionError("replayed Preview")), \
             patch.object(Batch, "action_check", side_effect=AssertionError("replayed Check")), \
             patch.object(Batch, "_validated_plan", side_effect=AssertionError("rebuilt source plan")):
            cutover.action_apply()
        self.assertEqual(cutover.state, "done")
        self.assertTrue(cutover.stock_batch_id.snapshot_hash)
        self.assertEqual(cutover.stock_batch_id.product_line_ids.opening_value, 20)

    def test_preview_reports_multiple_product_problems_together(self):
        products = self.product_fifo_auto | self.product_fifo_auto.copy({"name": "Second untracked item"})
        for product in products:
            self._make_in_move(product, 2, 10)
        self.env.flush_all()
        self.env.cr.execute("UPDATE product_template SET is_storable = false WHERE id IN %s",
                            [tuple(products.product_tmpl_id.ids)])
        products.product_tmpl_id.invalidate_recordset(["is_storable"])
        products.invalidate_recordset(["is_storable"])
        with self.assertRaises(UserError) as raised:
            self._cutover().action_preview()
        for product in products:
            self.assertIn(product.display_name, str(raised.exception))
            self.assertIn("Product ID: %s" % product.id, str(raised.exception))

    def test_half_cent_source_residual_uses_native_closing_value(self):
        self.registry_enter_test_mode()
        product = self.product_fifo_auto
        product.name = "FreshMilk Greenfield"
        category = product.categ_id.copy({"property_cost_method": "average"})
        product.write({"categ_id": category.id, "standard_price": 19.739})
        self._make_in_move(product, 14275, 19.739)
        # Division reproduces the user's exact binary half-cent boundary,
        # rather than the adjacent float obtained from the literal 19.739.
        product.standard_price = 281774.225 / 14275
        cutover = self._cutover()
        cutover.action_preview()
        self.assertAlmostEqual(cutover.preview_data["products"][0]["value"], 281774.225)
        cutover.action_apply()
        target = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(target.qty_available, 14275)
        self.assertAlmostEqual(target.total_value, 281774.23, places=2)
        self.assertAlmostEqual(product.total_value, 0, places=2)

    def test_partial_fifo_preserves_fractional_source_remainder(self):
        self.registry_enter_test_mode()
        self._use_multi_warehouses()
        product = self.product_fifo_auto
        product.name = "Maria Brizard Syrup Green Mint"
        self._make_in_move(product, 8420, 1492464.97 / 8420)
        self._make_out_move(product, 8182)
        self._make_in_move(product, 3, location_id=self.warehouse.lot_stock_id.id,
                           location_dest_id=self.other_warehouse.lot_stock_id.id)
        self._make_in_move(product, 2797, 495774.89 / 2797,
                           location_dest_id=self.other_warehouse.lot_stock_id.id)
        self.assertEqual(product.total_value, 537960.95)
        cutover = self._cutover()
        cutover.action_preview()
        self.assertEqual(cutover.preview_data["products"][0]["quantity"], 235)
        self.assertAlmostEqual(cutover.preview_data["products"][0]["value"], 41654.30735748218)
        cutover.action_apply()
        target = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(target.qty_available, 235)
        self.assertEqual(target.total_value, 41654.31)
        self.assertEqual(product.qty_available, 2800)
        self.assertEqual(product.total_value, 496306.65)
        self.assertEqual(product.with_context(location=self.other_warehouse.lot_stock_id.id).qty_available, 2800)
        self.assertIn("remaining FIFO receipts reconcile", cutover.stock_batch_id.rounding_note)
        self.assertIn("0.01", cutover.stock_batch_id.rounding_note)

    def test_partial_fifo_rejects_actual_one_cent_remaining_value_change(self):
        from ..models import execution
        self.registry_enter_test_mode()
        self._use_multi_warehouses()
        product = self.product_fifo_auto
        self._make_in_move(product, 10, 10)
        remaining_receipt = self._make_in_move(product, 10, 20,
            location_dest_id=self.other_warehouse.lot_stock_id.id)
        cutover = self._cutover()
        cutover.action_preview()
        original = execution.reconcile

        def altered_remaining_value(batch, *args):
            remaining_receipt.value_manual = 200.01
            self.env.invalidate_all()
            return original(batch, *args)

        with patch.object(execution, "reconcile", side_effect=altered_remaining_value):
            with self.assertRaisesRegex(UserError, "did not reconcile"):
                cutover.action_apply()
        self.assertEqual(cutover.state, "review")
        self.assertFalse(cutover.stock_batch_id)
        self.assertEqual(product.qty_available, 20)
        self.assertEqual(product.total_value, 300)
        self.assertEqual(remaining_receipt.value, 200)

    def test_tracking_check_identifies_exact_duplicate_product_and_opens_it(self):
        product = self.product_fifo_auto
        product.name = "Plain Croissant"
        other = product.copy({"name": "Plain Croissant", "is_storable": True})
        # Simulate a legacy stock row for a product whose tracking was disabled.
        self.env["stock.quant"].create({"product_id": product.id,
            "location_id": self.stock_location.id, "quantity": 2})
        self.env.flush_all()
        self.env.cr.execute("UPDATE product_template SET is_storable = false WHERE id = %s",
                            [product.product_tmpl_id.id])
        product.product_tmpl_id.invalidate_recordset(["is_storable"])
        product.invalidate_recordset(["is_storable"])
        cutover = self._cutover()
        with self.assertRaises(RedirectWarning) as caught:
            cutover.action_preview()
        self.assertIn(str(product.id), str(caught.exception))
        self.assertIn(self.env.company.name, str(caught.exception))
        self.assertEqual(caught.exception.args[1]["res_id"], product.id)
        self.assertTrue(other.is_storable)
        self.assertFalse(product.is_storable)

    def test_periodic_stock_does_not_require_perpetual_account_configuration(self):
        self.registry_enter_test_mode()
        product = self.product_fifo_auto
        product.categ_id.property_valuation = "periodic"
        self._make_in_move(product, 10, 10)
        (self.env.company | self.target_company).write({
            "account_stock_valuation_id": False, "account_stock_journal_id": False,
        })
        cutover = self._cutover()
        cutover.action_preview()
        self.assertIn("no automatic closing journal", cutover.preview_html)
        self.assertIn("Periodic requires a separate general-ledger opening adjustment", cutover.preview_html)
        cutover.action_apply()
        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(copied.total_value, 100)
        self.assertEqual(copied.qty_available, 10)
        self.assertFalse(cutover.stock_batch_id.created_move_ids.account_move_id)

    def test_later_warehouse_can_move_remaining_product(self):
        self.registry_enter_test_mode()
        self._use_multi_warehouses()
        product = self.product_fifo_auto
        product.name = "Fresh Milk"
        self._make_in_move(product, 10, 10)
        self._make_in_move(product, 10, 20, location_dest_id=self.other_warehouse.lot_stock_id.id,
                           picking_type_id=self.other_warehouse.in_type_id.id)
        first = self._cutover()
        first.action_preview()
        first.action_apply()
        second = self._cutover()
        second.write({"source_warehouse_id": self.other_warehouse.id})
        second.action_preview()
        self.assertEqual(second.total_value, 200)
        second.action_apply()
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(product.total_value, 0)
        self.assertEqual(first.state, "done")
        self.assertEqual(second.state, "done")
        first_product = first.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(first_product, second.stock_batch_id.product_line_ids.target_product_id)
        self.assertEqual(first_product.qty_available, 20)
        self.assertAlmostEqual(first_product.total_value, 300, places=2)
        self.assertEqual(self.env["product.product"].search_count([
            ("company_id", "=", self.target_company.id), ("name", "=", product.name),
        ]), 1)
        with self.assertRaises(UserError):
            first.action_apply()

    def test_existing_matching_product_keeps_stock_and_receives_cutover(self):
        self.registry_enter_test_mode()
        source = self.product_fifo_auto
        source.name = "Fresh Milk"
        self._make_in_move(source, 3, 12)
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        target = source.with_context(context).with_company(self.target_company).copy({
            "name": source.name, "company_id": self.target_company.id,
            "categ_id": self._target_category(source).id,
        }).with_company(self.target_company)
        self._make_in_move(target, 2, 8, company=self.target_company,
                           location_dest_id=self.target_warehouse.lot_stock_id.id,
                           picking_type_id=self.target_warehouse.in_type_id.id)
        before = self.env["product.product"].search_count([("company_id", "=", self.target_company.id)])
        warehouse_count = self.env["stock.warehouse"].search_count([])
        cutover = self._cutover()
        cutover.action_preview()
        cutover.action_apply()
        self.assertEqual(cutover.stock_batch_id.product_line_ids.target_product_id, target)
        self.assertEqual(target.qty_available, 5)
        self.assertAlmostEqual(target.total_value, 52, places=2)
        self.assertEqual(self.env["product.product"].search_count([
            ("company_id", "=", self.target_company.id),
        ]), before)
        self.assertEqual(self.env["stock.warehouse"].search_count([]), warehouse_count)

    def test_ambiguous_destination_name_is_rejected_before_creation(self):
        source = self.product_fifo_auto
        source.name = "Fresh Milk"
        self._make_in_move(source, 3, 12)
        Product = source.with_context(
            allowed_company_ids=(self.env.company | self.target_company).ids,
        ).with_company(self.target_company)
        category = self._target_category(source)
        targets = Product.copy({"name": source.name, "company_id": self.target_company.id, "categ_id": category.id})
        targets |= Product.copy({"name": source.name, "company_id": self.target_company.id, "categ_id": category.id})
        count = self.env["product.product"].search_count([])
        cutover = self._cutover()
        with self.assertRaisesRegex(UserError, "multiple destination products match"):
            cutover.action_preview()
        self.assertEqual(self.env["product.product"].search_count([]), count)
        self.assertEqual(source.qty_available, 3)
        self.assertFalse(cutover.stock_batch_id)

    def test_existing_average_and_standard_stock_adds_without_repricing(self):
        self.registry_enter_test_mode()
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        for method in ("average", "standard"):
            with self.subTest(method=method):
                category = self.product_fifo_auto.categ_id.copy({"property_cost_method": method})
                source = self.product_fifo_auto.copy({
                    "name": "Existing Stock %s" % method, "categ_id": category.id, "standard_price": 12,
                })
                target = source.with_context(context).with_company(self.target_company).copy({
                    "name": source.name, "company_id": self.target_company.id, "standard_price": 12,
                    "categ_id": self._target_category(source).id,
                }).with_company(self.target_company)
                old_cost = 8 if method == "average" else 12
                self._make_in_move(target, 2, old_cost, company=self.target_company,
                                   location_dest_id=self.target_warehouse.lot_stock_id.id,
                                   picking_type_id=self.target_warehouse.in_type_id.id)
                self._make_in_move(source, 3, 12)
                before = target.total_value
                cutover = self._cutover()
                cutover.action_preview()
                cutover.action_apply()
                self.assertEqual(cutover.stock_batch_id.product_line_ids.target_product_id, target)
                self.assertEqual(target.qty_available, 5)
                self.assertAlmostEqual(target.total_value, before + 36, places=2)
                self.assertEqual(target.cost_method, method)
                self.assertAlmostEqual(target.standard_price, (before + 36) / 5, places=2)
                self.assertEqual(source.qty_available, 0)

    def test_reused_product_stock_in_other_target_warehouse_is_unchanged(self):
        self.registry_enter_test_mode()
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        other = self.env["stock.warehouse"].with_context(context).create({
            "name": "Untouched Target Stock", "code": "UTGT", "company_id": self.target_company.id,
        })
        source = self.product_fifo_auto
        source.name = "Fresh Milk"
        target = source.with_context(context).with_company(self.target_company).copy({
            "name": source.name, "company_id": self.target_company.id,
            "categ_id": self._target_category(source).id,
        }).with_company(self.target_company)
        receipt = self._make_in_move(target, 4, 8, company=self.target_company,
                                    location_dest_id=other.lot_stock_id.id,
                                    picking_type_id=other.in_type_id.id)
        quants = self.env["stock.quant"].search([
            ("product_id", "=", target.id), ("location_id", "=", other.lot_stock_id.id),
        ])
        before = quants.read(["quantity", "reserved_quantity", "write_date"])
        receipt_before = (receipt.value, receipt.write_date)
        self._make_in_move(source, 3, 12)
        cutover = self._cutover()
        cutover.action_preview()
        cutover.action_apply()
        self.assertEqual(cutover.stock_batch_id.product_line_ids.target_product_id, target)
        self.assertEqual(target.with_context(location=other.lot_stock_id.id).qty_available, 4)
        self.assertEqual(target.with_context(location=self.target_warehouse.lot_stock_id.id).qty_available, 3)
        self.assertEqual(target.qty_available, 7)
        self.assertAlmostEqual(target.total_value, 68, places=2)
        self.assertEqual(quants.read(["quantity", "reserved_quantity", "write_date"]), before)
        self.assertEqual((receipt.value, receipt.write_date), receipt_before)

    def test_destination_stock_change_invalidates_preview_before_apply(self):
        self.registry_enter_test_mode()
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        source = self.product_fifo_auto
        self._make_in_move(source, 3, 12)
        target = source.with_context(context).with_company(self.target_company).copy({
            "name": source.name, "company_id": self.target_company.id,
            "categ_id": self._target_category(source).id,
        }).with_company(self.target_company)
        cutover = self._cutover()
        cutover.action_preview()
        self._make_in_move(target, 2, 8, company=self.target_company,
                           location_dest_id=self.target_warehouse.lot_stock_id.id,
                           picking_type_id=self.target_warehouse.in_type_id.id)
        counts = {model: self.env[model].search_count([]) for model in
                  ("product.product", "stock.move", "stock.location", "company.stock.fifo.migration")}
        with self.assertRaisesRegex(UserError, "(?i)(changed|fresh Preview)"):
            cutover.action_apply()
        self.assertEqual(cutover.state, "review")
        self.assertFalse(cutover.stock_batch_id)
        self.assertEqual(source.qty_available, 3)
        self.assertEqual(target.qty_available, 2)
        self.assertAlmostEqual(target.total_value, 16, places=2)
        for model, count in counts.items():
            self.assertEqual(self.env[model].search_count([]), count)

    def test_target_warehouse_must_belong_to_target_company(self):
        self._make_in_move(self.product_fifo_auto, 3, 12)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            cutover = self._cutover()
            cutover.target_warehouse_id = self.warehouse
            cutover.action_preview()

    def test_preview_requires_existing_destination_warehouse(self):
        self._make_in_move(self.product_fifo_auto, 3, 12)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            cutover = self._cutover()
            cutover.target_warehouse_id = False
            cutover.action_preview()

    def test_archived_stock_and_category_account_do_not_require_source_cleanup(self):
        self.registry_enter_test_mode()
        product = self.product_fifo_auto
        self._make_in_move(product, 5, 10)
        account = product._get_product_accounts()["stock_valuation"]
        self.env.company.account_stock_valuation_id = False
        product.categ_id.property_stock_valuation_account_id = account
        self.assertEqual(product._get_product_accounts()["stock_valuation"], account)
        product.active = False
        cutover = self._cutover()
        cutover.action_preview()
        cutover.action_apply()
        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertTrue(copied.active)
        self.assertEqual(copied.qty_available, 5)
        self.assertEqual(copied.total_value, 50)
        self.assertFalse(product.active)

    def test_partial_average_cost_ignores_outside_packages_and_consignment(self):
        self.registry_enter_test_mode()
        self._use_multi_warehouses()
        product = self.product_fifo_auto
        product.categ_id.property_cost_method = "average"
        self._make_in_move(product, 10, 10)
        self._make_in_move(product, 10, 20, location_dest_id=self.other_warehouse.lot_stock_id.id,
                           picking_type_id=self.other_warehouse.in_type_id.id)
        owner = self.env["res.partner"].create({"name": "Outside Consignment Owner"})
        self._make_in_move(product, 4, 10, owner_id=owner.id,
                           location_dest_id=self.other_warehouse.lot_stock_id.id,
                           picking_type_id=self.other_warehouse.in_type_id.id)
        outside = self.env["stock.quant"].search([
            ("product_id", "=", product.id), ("location_id", "=", self.other_warehouse.lot_stock_id.id)])
        package = self.env["stock.package"].create({"name": "Outside package"})
        outside.package_id = package
        before = outside.read(["quantity", "reserved_quantity", "owner_id", "package_id"])
        cutover = self._cutover()
        cutover.action_preview()
        self.assertEqual(cutover.total_value, 150)
        cutover.action_apply()
        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 150)
        self.assertEqual(copied.standard_price, 15)
        self.assertEqual(product.total_value, 150)
        self.assertEqual(outside.read(["quantity", "reserved_quantity", "owner_id", "package_id"]), before)

    def _reserved_transfer(self, product, quantity, warehouse):
        picking = self.env["stock.picking"].with_company(warehouse.company_id).create({
            "picking_type_id": warehouse.out_type_id.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": self.customer_location.id,
            "move_ids": [Command.create({
                "product_id": product.id, "product_uom": product.uom_id.id,
                "product_uom_qty": quantity, "location_id": warehouse.lot_stock_id.id,
                "location_dest_id": self.customer_location.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def test_preview_includes_shared_inventory_without_creating_business_records(self):
        product = self.product_fifo_auto
        product.company_id = False
        self._make_in_move(product, 10, 10)
        self._make_in_move(product, 10, 20)
        self._make_out_move(product, 5)
        cutover = self._cutover()
        counts = {model: self.env[model].search_count([]) for model in (
            "product.product", "product.template", "product.category", "stock.warehouse",
            "stock.location", "account.account", "stock.move", "account.move",
        )}

        cutover.action_preview()

        self.assertEqual(cutover.state, "review")
        self.assertEqual(cutover.product_count, 1)
        self.assertEqual(cutover.total_value, 250)
        self.assertEqual(cutover.preview_data["products"][0]["quantity"], 15)
        self.assertEqual(cutover.preview_data["products"][0]["source"], product.id)
        self.assertFalse(product.company_id)
        self.assertEqual(product.qty_available, 15)
        self.assertEqual(product.total_value, 250)
        for model, before in counts.items():
            self.assertEqual(self.env[model].search_count([]), before)

    def test_preview_names_reserved_stock_without_changing_it(self):
        product = self.product_fifo_auto
        product.name = "Preview Reserved Ingredient"
        self._make_in_move(product, 10, 10)
        delivery = self.env["stock.move"].create({
            "product_id": product.id, "product_uom": product.uom_id.id,
            "product_uom_qty": 3, "company_id": self.env.company.id,
            "location_id": self.stock_location.id,
            "location_dest_id": self.customer_location.id,
            "picking_type_id": self.picking_type_out.id,
        })
        delivery._action_confirm()
        delivery._action_assign()
        quants = self.env["stock.quant"].search([
            ("company_id", "=", self.env.company.id), ("product_id", "=", product.id),
            ("location_id", "=", self.stock_location.id),
        ])
        self.assertEqual(sum(quants.mapped("quantity")), 10)
        self.assertEqual(sum(quants.mapped("reserved_quantity")), 3)
        before = quants.read(["quantity", "reserved_quantity", "write_date"])
        cutover = self._cutover()
        cutover.release_source_reservations = False

        with self.assertRaises(UserError) as caught:
            cutover.action_preview()

        message = str(caught.exception)
        self.assertIn("Preview Reserved Ingredient", message)
        self.assertIn(self.stock_location.complete_name, message)
        self.assertIn("On hand: 10", message)
        self.assertIn("Reserved: 3", message)
        self.assertIn("reservation", message)
        self.assertEqual(quants.read(["quantity", "reserved_quantity", "write_date"]), before)
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.stock_batch_id)

    def test_opt_in_releases_selected_reservations_but_leaves_order_open(self):
        product = self.product_fifo_auto
        product.name = '<img src=x onerror=alert(1)> & Ingredient'
        receipt = self._make_in_move(product, 10, 10)
        history = (receipt.date, receipt.value, receipt.write_date)
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.picking_type_out.id,
            "location_id": self.stock_location.id, "location_dest_id": self.customer_location.id,
            "move_ids": [Command.create({
                "product_id": product.id, "product_uom": product.uom_id.id,
                "product_uom_qty": 3, "location_id": self.stock_location.id,
                "location_dest_id": self.customer_location.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        cutover = self._cutover()
        cutover.release_source_reservations = True

        cutover.action_preview()

        self.assertEqual(picking.move_ids.quantity, 3)
        self.assertIn(picking.name, cutover.preview_html)
        preview = html.fromstring(cutover.preview_html)
        self.assertEqual(preview.xpath("//table[2]/tbody/tr/td[2]/text()"), [product.name])
        self.assertFalse(preview.xpath("//img | //script"))
        self.registry_enter_test_mode()
        cutover.action_apply()

        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(cutover.state, "done")
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 100)
        self.assertEqual(product.qty_available, 0)
        self.assertNotIn(picking.state, ("done", "cancel"))
        self.assertEqual(picking.move_ids.product_uom_qty, 3)
        self.assertEqual(picking.move_ids.quantity, 0)
        self.assertEqual(picking.company_id, self.env.company)
        self.assertEqual(picking.move_ids.product_id, product)
        self.assertEqual((receipt.date, receipt.value, receipt.write_date), history)

    def test_release_leaves_other_warehouse_and_company_reservations_unchanged(self):
        product = self.product_fifo_auto
        product.company_id = False
        self._use_multi_warehouses()
        self._make_in_move(product, 10, 10)
        self._make_in_move(product, 5, 20, location_dest_id=self.other_warehouse.lot_stock_id.id,
                           picking_type_id=self.other_warehouse.in_type_id.id)
        third = self._create_company(name="Reservation Isolation Company")
        third_product = product.with_context(allowed_company_ids=third.ids).with_company(third)
        third_product.categ_id.write({"property_cost_method": "fifo", "property_valuation": "real_time"})
        self._make_in_move(product, 7, 30, company=third)
        third_warehouse = self.env["stock.warehouse"].search([("company_id", "=", third.id)], limit=1)
        selected = self._reserved_transfer(product, 3, self.warehouse)
        other = self._reserved_transfer(product, 2, self.other_warehouse)
        unrelated = self._reserved_transfer(product, 4, third_warehouse)
        (selected | other | unrelated).move_ids.picked = True
        before = (other | unrelated).move_ids.read(["state", "quantity", "picked", "product_uom_qty", "write_date"])
        cutover = self._cutover()
        cutover.release_source_reservations = True
        cutover.action_preview()
        self.registry_enter_test_mode()

        cutover.action_apply()

        self.assertEqual(selected.move_ids.quantity, 0)
        self.assertEqual((other | unrelated).move_ids.read(
            ["state", "quantity", "picked", "product_uom_qty", "write_date"]), before)
        self.assertEqual(third_product.qty_available, 7)
        self.assertEqual(third_product.total_value, 210)
        self.assertEqual(product.qty_available, 5)
        self.assertEqual(product.total_value, 100)
        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 100)

    def test_changed_reservation_requires_new_preview_without_releasing_it(self):
        product = self.product_fifo_auto
        self._make_in_move(product, 10, 10)
        picking = self._reserved_transfer(product, 3, self.warehouse)
        cutover = self._cutover()
        cutover.release_source_reservations = True
        cutover.action_preview()
        picking.move_ids.product_uom_qty = 4
        picking.action_assign()

        with self.assertRaisesRegex(UserError, "Build a fresh Preview"):
            cutover.action_apply()

        self.assertEqual(picking.move_ids.quantity, 4)
        self.assertEqual(product.qty_available, 10)
        self.assertFalse(cutover.stock_batch_id)

    def test_cutover_leaves_picked_delivery_open_without_cleanup(self):
        product = self.product_fifo_auto
        self._make_in_move(product, 10, 10)
        picking = self._reserved_transfer(product, 3, self.warehouse)
        ready = self._reserved_transfer(product, 2, self.warehouse)
        picking.move_ids.picked = True
        before = picking.move_ids.move_line_ids.read(["quantity", "picked", "write_date"])
        cutover = self._cutover()
        cutover.release_source_reservations = True

        cutover.action_preview()

        self.assertEqual(picking.move_ids.move_line_ids.read(["quantity", "picked", "write_date"]), before)
        self.assertEqual(ready.move_ids.quantity, 2)
        self.assertEqual(picking.move_ids.quantity, 3)
        self.assertEqual(product.qty_available, 10)
        self.registry_enter_test_mode()

        cutover.action_apply()

        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(cutover.state, "done")
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 100)
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(product.total_value, 0)
        for operation in picking | ready:
            self.assertNotIn(operation.state, ("done", "cancel"))
            self.assertEqual(operation.move_ids.quantity, 0)
            self.assertEqual(operation.company_id, self.env.company)
        self.assertEqual(picking.move_ids.product_uom_qty, 3)
        self.assertEqual(ready.move_ids.product_uom_qty, 2)
        audit = cutover.stock_batch_id.snapshot_data["source_operations"]["lines"]
        self.assertTrue(any(row["picked"] for row in audit))

    def test_failed_cutover_restores_released_reservations(self):
        product = self.product_fifo_auto
        self._make_in_move(product, 3, 1 / 3)
        self._make_out_move(product, 1)
        picking = self._reserved_transfer(product, 1, self.warehouse)
        picking.move_ids.picked = True
        old_lines = picking.move_ids.move_line_ids.ids
        cutover = self._cutover()
        cutover.release_source_reservations = True
        cutover.action_preview()
        self.registry_enter_test_mode()

        # A genuine posting/reconciliation failure must still restore reservations.
        with patch("odoo.addons.company_stock_fifo_migration.models.execution.reconcile",
                   side_effect=UserError("Injected reconciliation failure")), \
                self.assertRaisesRegex(UserError, "reconciliation failure"):
            cutover.action_apply()

        self.assertEqual(picking.move_ids.quantity, 1)
        self.assertEqual(picking.move_ids.move_line_ids.ids, old_lines)
        self.assertTrue(picking.move_ids.picked)
        self.assertEqual(picking.state, "assigned")
        self.assertEqual(product.qty_available, 2)
        self.assertEqual(product.total_value, .67)
        self.assertEqual(cutover.state, "review")
        self.assertFalse(cutover.stock_batch_id)

    def test_release_option_refuses_orphan_reservation_quantities(self):
        product = self.product_fifo_auto
        self._make_in_move(product, 10, 10)
        quant = self.env["stock.quant"].search([
            ("product_id", "=", product.id), ("location_id", "=", self.stock_location.id),
            ("company_id", "=", self.env.company.id),
        ])
        quant.reserved_quantity = 3
        cutover = self._cutover()
        cutover.release_source_reservations = True

        with self.assertRaisesRegex(UserError, "Reservation quantities disagree"):
            cutover.action_preview()

        self.assertEqual(quant.quantity, 10)
        self.assertEqual(quant.reserved_quantity, 3)
        self.assertFalse(cutover.stock_batch_id)

    def test_preview_names_negative_stock_outside_positive_selected_warehouse(self):
        product = self.product_fifo_auto
        product.name = "Preview Outside Ingredient"
        self._use_multi_warehouses()
        self._make_in_move(product, 10, 10)
        self._make_out_move(
            product, 2, location_id=self.other_warehouse.lot_stock_id.id,
            picking_type_id=self.other_warehouse.out_type_id.id,
        )
        quants = self.env["stock.quant"].search([
            ("company_id", "=", self.env.company.id), ("product_id", "=", product.id),
        ])
        self.assertEqual(quants.filtered(lambda q: q.location_id == self.stock_location).quantity, 10)
        before = quants.read(["quantity", "reserved_quantity", "write_date"])
        cutover = self._cutover()

        for release in (False, True):
            cutover.release_source_reservations = release
            with self.assertRaises(UserError) as caught:
                cutover.action_preview()

        message = str(caught.exception)
        self.assertIn("Preview Outside Ingredient", message)
        self.assertIn(self.other_warehouse.lot_stock_id.complete_name, message)
        self.assertIn("On hand: -2", message)
        self.assertIn("negative stock", message)
        self.assertIn("outside the selected warehouse", message)
        self.assertEqual(quants.read(["quantity", "reserved_quantity", "write_date"]), before)
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.stock_batch_id)

    def test_preview_names_consignment_and_package_on_positive_stock(self):
        product = self.product_fifo_auto
        product.name = "Preview Owned Ingredient"
        owner = self.env["res.partner"].create({"name": "Preview Consignment Owner"})
        package = self.env["stock.package"].create({"name": "PREVIEW-PACKAGE"})
        self._make_in_move(product, 4, 10, owner_id=owner.id)
        quant = self.env["stock.quant"].search([
            ("company_id", "=", self.env.company.id), ("product_id", "=", product.id),
            ("location_id", "=", self.stock_location.id), ("owner_id", "=", owner.id),
        ])
        self.assertEqual(quant.quantity, 4)
        quant.package_id = package
        before = quant.read(["quantity", "reserved_quantity", "owner_id", "package_id", "write_date"])
        cutover = self._cutover()

        for release in (False, True):
            cutover.release_source_reservations = release
            with self.assertRaises(UserError) as caught:
                cutover.action_preview()

        message = str(caught.exception)
        self.assertIn("Preview Owned Ingredient", message)
        self.assertIn(self.stock_location.complete_name, message)
        self.assertIn("On hand: 4", message)
        self.assertIn("consignment owner: Preview Consignment Owner", message)
        self.assertIn("package: PREVIEW-PACKAGE", message)
        self.assertNotIn("negative stock", message)
        self.assertEqual(quant.read(["quantity", "reserved_quantity", "owner_id", "package_id", "write_date"]), before)
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.stock_batch_id)

    def test_confirm_creates_company_specific_products_and_preserves_fifo_history(self):
        product = self.product_fifo_auto
        product.write({"company_id": False, "barcode": "SHARED-WAREHOUSE-INGREDIENT"})
        receipts = self._make_in_move(product, 10, 10) | self._make_in_move(product, 10, 20)
        sale = self._make_out_move(product, 5)
        history = receipts | sale
        before = [(move.id, move.company_id.id, move.date, move.value, move.write_date) for move in history]
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()

        cutover.action_apply()

        self.assertEqual(cutover.state, "done")
        batch = cutover.stock_batch_id
        self.assertEqual(batch.state, "done")
        copied = batch.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertNotEqual(copied, product)
        self.assertEqual(copied.company_id, self.target_company)
        self.assertEqual(copied.qty_available, 15)
        self.assertEqual(copied.total_value, 250)
        self.assertEqual(copied.cost_method, "fifo")
        self.assertEqual(copied.valuation, "real_time")
        self.assertFalse(copied.barcode)
        self.assertFalse(product.company_id)
        self.assertEqual(product.barcode, "SHARED-WAREHOUSE-INGREDIENT")
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(product.total_value, 0)
        created_warehouse = batch.warehouse_line_ids.target_warehouse_id
        self.assertEqual(created_warehouse, self.target_warehouse)
        self.assertFalse(batch.warehouse_line_ids.created_warehouse_id)
        self.assertEqual(created_warehouse.company_id, self.target_company)
        self.assertTrue(all(move.account_move_id.state == "posted" for move in batch.created_move_ids))
        self.assertEqual([(move.id, move.company_id.id, move.date, move.value, move.write_date)
                          for move in history], before)
        outgoing = self._make_out_move(copied, 6, company=self.target_company,
                                      location_id=created_warehouse.lot_stock_id.id,
                                      picking_type_id=created_warehouse.out_type_id.id)
        self.assertEqual(outgoing.value, 70)

    def test_batched_movements_preserve_each_product_in_combined_journals(self):
        first = self.product_fifo_auto
        second = first.copy({"name": "Second Batch Ingredient"})
        free = first.copy({"name": "Zero Cost Batch Ingredient"})
        self._make_in_move(first, 10, 10)
        self._make_in_move(first, 10, 20)
        self._make_out_move(first, 5)
        self._make_in_move(second, 4, 7)
        self._make_in_move(free, 5, 0)
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()

        cutover.action_apply()

        batch = cutover.stock_batch_id
        self.assertEqual(cutover.state, "done")
        self.assertEqual(len(batch.created_move_ids.account_move_id), 2)
        expected = {first.id: (15, 250), second.id: (4, 28), free.id: (5, 0)}
        for line in batch.product_line_ids:
            target = line.target_product_id.with_company(self.target_company)
            quantity, value = expected[line.source_product_id.id]
            self.assertEqual(target.qty_available, quantity)
            self.assertEqual(target.total_value, value)
            self.assertEqual(line.source_product_id.qty_available, 0)
            self.assertEqual(line.source_product_id.total_value, 0)
            for product, company, direction in (
                (line.source_product_id, self.env.company, -1),
                (target, self.target_company, 1),
            ):
                journal = batch.created_move_ids.filtered(
                    lambda move: move.company_id == company
                ).account_move_id
                self.assertEqual(journal.state, "posted")
                stock_account = product.with_company(company)._get_product_accounts()["stock_valuation"]
                stock_lines = journal.line_ids.filtered(
                    lambda entry: entry.product_id == product and entry.account_id == stock_account
                )
                self.assertTrue(stock_lines)
                self.assertEqual(sum(stock_lines.mapped("balance")), direction * value)
        copied_first = batch.product_line_ids.filtered(
            lambda line: line.source_product_id == first
        ).target_product_id.with_company(self.target_company)
        self.assertEqual(copied_first._run_fifo(6), 70)

    def test_shared_product_stock_in_third_company_is_not_moved_or_repriced(self):
        third = self._create_company(name="Unrelated Shared Stock Company")
        product = self.product_fifo_auto
        product.company_id = False
        product.with_company(third).categ_id.write({
            "property_cost_method": "fifo", "property_valuation": "real_time",
        })
        self._make_in_move(product, 10, 10)
        third_receipt = self._make_in_move(product, 7, 30, company=third)
        third_product = product.with_company(third)
        history = (third_receipt.date, third_receipt.value, third_receipt.write_date)
        cutover = self._cutover()

        cutover.action_preview()

        self.assertEqual(cutover.product_count, 1)
        self.assertEqual(cutover.preview_data["products"][0]["quantity"], 10)
        self.assertEqual(cutover.total_value, 100)
        self.registry_enter_test_mode()
        cutover.action_apply()

        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 100)
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(third_product.qty_available, 7)
        self.assertEqual(third_product.total_value, 210)
        self.assertEqual((third_receipt.date, third_receipt.value, third_receipt.write_date), history)
        self.assertFalse(cutover.stock_batch_id.created_move_ids.filtered(
            lambda move: move.company_id == third
        ))

    def test_other_source_warehouse_keeps_stock_and_remaining_company_fifo(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._use_multi_warehouses()
        first = self._make_in_move(product, 10, 10)
        other = self._make_in_move(
            product, 10, 20,
            location_dest_id=self.other_warehouse.lot_stock_id.id,
            picking_type_id=self.other_warehouse.in_type_id.id,
        )
        cutover = self._cutover()

        cutover.action_preview()

        self.assertEqual(cutover.preview_data["products"][0]["quantity"], 10)
        self.assertEqual(cutover.total_value, 100)
        self.registry_enter_test_mode()
        cutover.action_apply()

        batch = cutover.stock_batch_id
        copied = batch.product_line_ids.target_product_id.with_company(self.target_company)
        target_warehouse = batch.warehouse_line_ids.target_warehouse_id
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 100)
        self.assertEqual(product.with_context(location=self.other_warehouse.lot_stock_id.id).qty_available, 10)
        self.assertEqual(product.with_context(location=self.warehouse.lot_stock_id.id).qty_available, 0)
        self.assertEqual(product.total_value, 200)
        self.assertEqual(first.value, 100)
        self.assertEqual(other.value, 200)
        copied_sale = self._make_out_move(
            copied, 2, company=self.target_company,
            location_id=target_warehouse.lot_stock_id.id,
            picking_type_id=target_warehouse.out_type_id.id,
        )
        source_sale = self._make_out_move(
            product, 2, location_id=self.other_warehouse.lot_stock_id.id,
            picking_type_id=self.other_warehouse.out_type_id.id,
        )
        self.assertEqual(copied_sale.value, 20)
        self.assertEqual(source_sale.value, 40)

    def test_stale_preview_refuses_before_creating_native_target_records(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        cutover = self._cutover()
        cutover.action_preview()
        self._make_in_move(product, 2, 20)
        counts = {model: self.env[model].search_count([]) for model in (
            "product.product", "product.template", "product.category", "stock.warehouse",
            "stock.location", "account.account", "stock.move", "account.move",
            "company.stock.fifo.migration",
        )}

        with self.assertRaisesRegex(UserError, "(?i)(changed|fresh Preview)"):
            cutover.action_apply()

        self.assertEqual(cutover.state, "review")
        self.assertFalse(cutover.stock_batch_id)
        self.assertEqual(product.qty_available, 12)
        self.assertEqual(product.total_value, 140)
        for model, before in counts.items():
            self.assertEqual(self.env[model].search_count([]), before)

    def test_preview_refuses_saved_unapplied_inventory_count(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        quant = self.env["stock.quant"].search([
            ("company_id", "=", self.env.company.id),
            ("product_id", "=", product.id),
            ("location_id", "=", self.warehouse.lot_stock_id.id),
        ])
        quant.with_context(inventory_mode=True).inventory_quantity = 12
        self.assertTrue(quant.inventory_quantity_set)
        self.assertEqual(quant.quantity, 10)
        cutover = self._cutover()
        cutover.release_source_reservations = False

        with self.assertRaisesRegex(UserError, "(?i)(inventory|count)"):
            cutover.action_preview()

        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.preview_data)
        self.assertEqual(product.qty_available, 10)
        self.assertEqual(product.total_value, 100)
        self.assertTrue(quant.inventory_quantity_set)
        self.assertEqual(quant.inventory_quantity, 12)

    def test_cutover_uses_recorded_stock_without_applying_old_draft_count(self):
        product = self.product_fifo_auto
        self._make_in_move(product, 10, 10)
        quant = self.env["stock.quant"].search([
            ("company_id", "=", self.env.company.id), ("product_id", "=", product.id),
            ("location_id", "=", self.warehouse.lot_stock_id.id),
        ])
        quant.with_context(inventory_mode=True).inventory_quantity = 12
        cutover = self._cutover()
        cutover.release_source_reservations = True

        cutover.action_preview()

        self.assertEqual(product.qty_available, 10)
        self.assertEqual(quant.inventory_quantity, 12)
        self.assertTrue(quant.inventory_quantity_set)
        self.assertIn("draft stock counts", cutover.preview_html)
        self.registry_enter_test_mode()
        cutover.action_apply()

        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(copied.qty_available, 10)
        self.assertEqual(copied.total_value, 100)
        self.assertEqual(product.qty_available, 0)
        self.assertEqual(product.total_value, 0)
        self.assertEqual(quant.inventory_quantity, 12)
        self.assertTrue(quant.inventory_quantity_set)

    def test_preview_refuses_unapplied_count_on_zero_stock_product(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        zero_stock_product = product.copy({"name": "Unapplied Zero Stock Ingredient"})
        quant = self.env["stock.quant"].with_context(inventory_mode=True).create({
            "product_id": zero_stock_product.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "inventory_quantity": 3,
        })
        self.assertEqual(quant.quantity, 0)
        self.assertTrue(quant.inventory_quantity_set)
        cutover = self._cutover()
        cutover.release_source_reservations = False

        with self.assertRaisesRegex(UserError, "(?i)(inventory|count)"):
            cutover.action_preview()

        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.preview_data)
        self.assertEqual(product.qty_available, 10)
        self.assertEqual(zero_stock_product.qty_available, 0)
        self.assertEqual(quant.inventory_quantity, 3)
        self.assertTrue(quant.inventory_quantity_set)

    def test_count_entered_after_preview_refuses_before_target_creation(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        cutover = self._cutover()
        cutover.release_source_reservations = False
        cutover.action_preview()
        quant = self.env["stock.quant"].search([
            ("company_id", "=", self.env.company.id),
            ("product_id", "=", product.id),
            ("location_id", "=", self.warehouse.lot_stock_id.id),
        ])
        quant.with_context(inventory_mode=True).inventory_quantity = 12
        counts = {model: self.env[model].search_count([]) for model in (
            "product.product", "product.template", "product.category", "stock.warehouse",
            "stock.location", "account.account", "stock.move", "account.move",
            "company.stock.fifo.migration",
        )}
        self.registry_enter_test_mode()

        with self.assertRaisesRegex(UserError, "(?i)(inventory|count)"):
            cutover.action_apply()

        self.assertEqual(cutover.state, "review")
        self.assertFalse(cutover.stock_batch_id)
        self.assertEqual(product.qty_available, 10)
        self.assertEqual(product.total_value, 100)
        self.assertTrue(quant.inventory_quantity_set)
        self.assertEqual(quant.inventory_quantity, 12)
        for model, before in counts.items():
            self.assertEqual(self.env[model].search_count([]), before)

    def test_non_administrator_cannot_create_or_run_whole_warehouse_cutover(self):
        cutover = self._cutover()
        user = self._user(
            "whole-warehouse-stock-manager",
            self.env.company | self.target_company,
            "stock.group_stock_manager",
        )
        restricted = cutover.with_user(user)

        with self.assertRaises(AccessError), self.env.cr.savepoint():
            restricted.env[cutover._name].create({
                "source_warehouse_id": self.warehouse.id,
                "target_company_id": self.target_company.id,
                "target_warehouse_id": self.target_warehouse.id,
            })
        for action in ("action_preview", "action_apply", "action_open_reconciliation",
                       "action_open_previous_cutovers"):
            with self.subTest(action=action), self.assertRaises(AccessError), self.env.cr.savepoint():
                getattr(restricted, action)()
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.stock_batch_id)

    def test_administrator_must_enable_both_companies_before_preview_or_apply(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        cutover = self._cutover()
        cutover.action_preview()
        user = self._user(
            "whole-warehouse-source-only-switcher",
            self.env.company | self.target_company,
            "base.group_system",
        )
        restricted = cutover.with_user(user).with_context(allowed_company_ids=self.env.company.ids)

        for action in ("action_preview", "action_apply", "action_open_reconciliation"):
            with self.subTest(action=action), self.assertRaises(AccessError), self.env.cr.savepoint():
                getattr(restricted, action)()

        self.assertEqual(cutover.state, "review")
        self.assertEqual(product.qty_available, 10)
        self.assertFalse(cutover.stock_batch_id)

    def test_client_cannot_forge_whole_warehouse_preview_or_completion_audit(self):
        cutover = self._cutover()
        values = {
            "source_warehouse_id": self.warehouse.id,
            "target_company_id": self.target_company.id,
            "target_warehouse_id": self.target_warehouse.id,
        }
        for forged in ({"state": "done"}, {"preview_data": {"products": []}},
                       {"preview_hash": "client-supplied"}, {"stock_batch_id": 1},
                       {"target_warehouse_name": "Forged Warehouse"}, {"target_warehouse_code": "FORGE"}):
            with self.subTest(forged=forged), self.assertRaises(AccessError), self.env.cr.savepoint():
                cutover.env[cutover._name].create({**values, **forged})
            with self.subTest(write=forged), self.assertRaises(AccessError), self.env.cr.savepoint():
                cutover.write(forged)
        for token in (True, "system"):
            with self.subTest(token=token), self.assertRaises(AccessError), self.env.cr.savepoint():
                cutover.with_context(_stock_fifo_cutover_write=token).write({
                    "state": "done", "preview_hash": "client-supplied",
                })
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.preview_data)
        self.assertFalse(cutover.preview_hash)
        self.assertFalse(cutover.stock_batch_id)
