from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestWholeWarehouseProducts(TestStockValuationCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Warehouse Product Target")
        cls.target_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.target_company.id),
        ], limit=1)

    def _cutover(self):
        return self.env["company.stock.warehouse.cutover"].with_context(
            allowed_company_ids=(self.env.company | self.target_company).ids,
        ).create({
            "source_warehouse_id": self.warehouse.id,
            "target_company_id": self.target_company.id,
            "target_warehouse_id": self.target_warehouse.id,
        })

    def _variant_stock(self):
        attribute = self.env["product.attribute"].create({"name": "Warehouse Size"})
        value = self.env["product.attribute.value"].create({
            "name": "Large", "attribute_id": attribute.id,
        })
        template = self.product_fifo_auto.product_tmpl_id
        template.write({"company_id": self.env.company.id, "attribute_line_ids": [Command.create({
            "attribute_id": attribute.id, "value_ids": [Command.set(value.ids)],
        })]})
        variant = template.product_variant_ids
        variant.product_template_attribute_value_ids.price_extra = 5
        self._make_in_move(variant, 4, 12)
        return variant

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

    def test_uncategorized_product_preserves_company_costing_without_blank_category(self):
        self.registry_enter_test_mode()
        for method, valuation in (("average", "periodic"), ("standard", "real_time")):
            with self.subTest(method=method, valuation=valuation):
                self.env.company.write({"cost_method": method, "inventory_valuation": valuation})
                source = self.product_fifo_auto.copy({
                    "name": "Uncategorized %s %s" % (method, valuation),
                    "company_id": self.env.company.id, "categ_id": False, "standard_price": 11})
                self.assertFalse(source.categ_id)
                self.assertEqual(source.cost_method, method)
                self.assertEqual(source.valuation, valuation)
                self._make_in_move(source, 3, 11)
                cutover = self._cutover()
                cutover.action_preview()
                cutover.action_apply()
                target = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
                self.assertTrue(target.categ_id.name)
                self.assertFalse(source.categ_id)
                self.assertEqual(target.cost_method, method)
                self.assertEqual(target.valuation, valuation)
                self.assertEqual(target.qty_available, 3)
                self.assertEqual(target.total_value, 33)

    def test_changed_variant_price_requires_fresh_preview_before_product_creation(self):
        product = self._variant_stock()
        cutover = self._cutover()
        cutover.action_preview()
        product.product_template_attribute_value_ids.price_extra = 8
        before = self.env["product.template"].search_count([])
        self.registry_enter_test_mode()

        with self.assertRaisesRegex(UserError, "(?i)(changed|fresh Preview)"):
            cutover.action_apply()

        self.assertEqual(self.env["product.template"].search_count([]), before)
        self.assertEqual(product.qty_available, 4)

    def test_company_specific_variant_preserves_attributes_price_and_native_cost(self):
        product = self._variant_stock()
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()

        cutover.action_apply()

        copied = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(copied.company_id, self.target_company)
        self.assertEqual(copied.product_template_attribute_value_ids.product_attribute_value_id,
                         product.product_template_attribute_value_ids.product_attribute_value_id)
        self.assertEqual(copied.product_template_attribute_value_ids.price_extra, 5)
        self.assertEqual(copied.qty_available, 4)
        self.assertEqual(copied.total_value, 48)

    def test_whole_warehouse_copies_lot_identity_dates_and_custom_location(self):
        product = self.product_fifo_auto
        product.write({"company_id": self.env.company.id, "tracking": "lot", "lot_valuated": True})
        location = self.env["stock.location"].create({
            "name": "Warehouse Cold Shelf", "usage": "internal",
            "company_id": self.env.company.id, "location_id": self.warehouse.lot_stock_id.id,
        })
        lot = self.env["stock.lot"].create({
            "name": "WHOLE-WAREHOUSE-LOT", "ref": "Supplier lot", "product_id": product.id,
            "company_id": self.env.company.id, "expiration_date": "2027-06-01 00:00:00",
        })
        self._make_in_move(product, 5, 12, lot_ids=lot, location_dest_id=location.id)
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()

        cutover.action_apply()

        batch = cutover.stock_batch_id
        copied_lot = batch.lot_line_ids.created_lot_id
        copied_location = batch.location_line_ids.filtered(
            lambda line: line.source_location_id == location
        ).created_location_id
        self.assertEqual(copied_lot.name, lot.name)
        self.assertEqual(copied_lot.ref, lot.ref)
        self.assertEqual(copied_lot.expiration_date, lot.expiration_date)
        self.assertEqual(copied_lot.company_id, self.target_company)
        self.assertEqual(copied_location.name, location.name)
        self.assertEqual(copied_location.company_id, self.target_company)
        self.assertEqual(copied_lot.product_id.with_company(self.target_company).qty_available, 5)
        self.assertEqual(copied_lot.product_id.with_company(self.target_company).total_value, 60)

    def test_unrelated_product_company_still_blocks_preview(self):
        product = self.product_fifo_auto
        self._make_in_move(product, 4, 12)
        # Reproduce legacy inconsistent ownership without weakening native
        # product writes. A branch must not adopt an unrelated company's SKU.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE product_template SET company_id = %s WHERE id = %s",
            [self.target_company.id, product.product_tmpl_id.id],
        )
        product.product_tmpl_id.invalidate_recordset(["company_id"])
        product.invalidate_recordset(["company_id"])
        cutover = self._cutover()
        with self.assertRaisesRegex(UserError, "not available to Source Company"):
            cutover.action_preview()
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.stock_batch_id)

    def test_reuses_existing_bin_and_creates_only_missing_child(self):
        self.registry_enter_test_mode()
        source_bin = self.env["stock.location"].create({
            "name": "Cold Room", "usage": "internal", "company_id": self.env.company.id,
            "location_id": self.warehouse.lot_stock_id.id,
        })
        source_shelf = source_bin.copy({"name": "Milk Shelf", "location_id": source_bin.id})
        target_bin = self.env["stock.location"].create({
            "name": source_bin.name, "usage": "internal", "company_id": self.target_company.id,
            "location_id": self.target_warehouse.lot_stock_id.id,
        })
        self._make_in_move(self.product_fifo_auto, 5, 12, location_dest_id=source_shelf.id)
        warehouse_count = self.env["stock.warehouse"].search_count([])
        cutover = self._cutover()
        cutover.action_preview()
        cutover.action_apply()
        lines = cutover.stock_batch_id.location_line_ids
        matched = lines.filtered(lambda line: line.source_location_id == source_bin)
        created = lines.filtered(lambda line: line.source_location_id == source_shelf)
        self.assertEqual(matched.target_location_id, target_bin)
        self.assertFalse(matched.created_location_id)
        self.assertEqual(created.created_location_id.location_id, target_bin)
        self.assertEqual(created.created_location_id.name, source_shelf.name)
        self.assertEqual(self.env["stock.location"].search_count([
            ("location_id", "=", self.target_warehouse.lot_stock_id.id), ("name", "=", source_bin.name),
        ]), 1)
        self.assertEqual(self.env["stock.warehouse"].search_count([]), warehouse_count)

    def test_product_name_underscore_is_literal_not_wildcard(self):
        self.registry_enter_test_mode()
        source = self.product_fifo_auto
        source.write({"name": "Milk_1", "default_code": False, "barcode": False})
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        unrelated = source.with_context(context).with_company(self.target_company).copy({
            "name": "MilkX1", "company_id": self.target_company.id,
            "default_code": False, "barcode": False,
            "categ_id": self._target_category(source).id,
        })
        self._make_in_move(source, 3, 12)
        cutover = self._cutover()
        cutover.action_preview()
        cutover.action_apply()
        target = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertNotEqual(target, unrelated)
        self.assertEqual(target.name, "Milk_1")
        self.assertEqual(target.qty_available, 3)
        self.assertEqual(unrelated.with_company(self.target_company).qty_available, 0)

    def test_duplicate_source_reference_blocks_preview_without_target_copies(self):
        source = self.product_fifo_auto
        source.write({"name": "Fresh Milk", "default_code": "MILK-DUPLICATE"})
        other = source.copy({"name": "Different Milk", "default_code": source.default_code})
        self._make_in_move(source, 3, 12)
        self._make_in_move(other, 2, 10)
        counts = {model: self.env[model].search_count([]) for model in
                  ("product.template", "product.product", "stock.move", "company.stock.fifo.migration")}
        cutover = self._cutover()
        with self.assertRaises(UserError):
            cutover.action_preview()
        for model, count in counts.items():
            self.assertEqual(self.env[model].search_count([]), count)
        self.assertFalse(cutover.stock_batch_id)
        self.assertEqual(source.qty_available, 3)
        self.assertEqual(other.qty_available, 2)

    def test_missing_dynamic_variant_reuses_existing_destination_family(self):
        self.registry_enter_test_mode()
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        attribute = self.env["product.attribute"].create({
            "name": "Milk Family Size", "create_variant": "dynamic",
        })
        values = self.env["product.attribute.value"].create([
            {"name": "Small", "attribute_id": attribute.id},
            {"name": "Large", "attribute_id": attribute.id},
        ])
        template_values = {
            "name": "Dynamic Milk Family", "type": "consu", "is_storable": True,
            "categ_id": self.product_fifo_auto.categ_id.id,
            "uom_id": self.product_fifo_auto.uom_id.id,
            "attribute_line_ids": [Command.create({
                "attribute_id": attribute.id, "value_ids": [Command.set(values.ids)],
            })],
        }
        source_template = self.env["product.template"].create({
            **template_values, "company_id": self.env.company.id,
        })
        target_template = self.env["product.template"].with_context(context).with_company(self.target_company).create({
            **template_values, "company_id": self.target_company.id,
            "categ_id": self._target_category(self.product_fifo_auto).id,
        })
        source_combinations = source_template.attribute_line_ids.product_template_value_ids
        target_combinations = target_template.attribute_line_ids.product_template_value_ids
        source_small = source_template._create_product_variant(source_combinations.filtered(
            lambda value: value.product_attribute_value_id == values[0]))
        source_large = source_template._create_product_variant(source_combinations.filtered(
            lambda value: value.product_attribute_value_id == values[1]))
        target_small = target_template._create_product_variant(target_combinations.filtered(
            lambda value: value.product_attribute_value_id == values[0]))
        self.assertEqual(target_template.product_variant_ids, target_small)
        self.assertEqual(source_small.qty_available, 0)
        self._make_in_move(source_large, 3, 12)
        before_templates = self.env["product.template"].search_count([])
        cutover = self._cutover()
        cutover.action_preview()
        self.assertEqual(target_template.product_variant_ids, target_small)
        cutover.action_apply()
        target_large = cutover.stock_batch_id.product_line_ids.target_product_id.with_company(self.target_company)
        self.assertEqual(target_large.product_tmpl_id, target_template)
        self.assertEqual(target_large.product_template_attribute_value_ids.product_attribute_value_id, values[1])
        self.assertEqual(target_large.qty_available, 3)
        self.assertEqual(self.env["product.template"].search_count([]), before_templates)
        self.assertEqual(set(target_template.product_variant_ids.ids), {target_small.id, target_large.id})
        self.assertEqual(target_small.with_company(self.target_company).qty_available, 0)

    def test_renamed_family_discovered_by_later_sibling_reference_reuses_both(self):
        self.registry_enter_test_mode()
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        attribute = self.env["product.attribute"].create({"name": "Renamed Milk Family Size"})
        values = self.env["product.attribute.value"].create([
            {"name": "A", "attribute_id": attribute.id},
            {"name": "B", "attribute_id": attribute.id},
        ])
        template_values = {
            "type": "consu", "is_storable": True,
            "categ_id": self.product_fifo_auto.categ_id.id,
            "uom_id": self.product_fifo_auto.uom_id.id,
            "attribute_line_ids": [Command.create({
                "attribute_id": attribute.id, "value_ids": [Command.set(values.ids)],
            })],
        }
        source_template = self.env["product.template"].create({
            **template_values, "name": "Original Milk Family", "company_id": self.env.company.id,
        })
        target_template = self.env["product.template"].with_context(context).with_company(self.target_company).create({
            **template_values, "name": "Renamed Destination Family", "company_id": self.target_company.id,
            "categ_id": self._target_category(self.product_fifo_auto).id,
        })
        source_a = source_template.product_variant_ids.filtered(
            lambda product: product.product_template_attribute_value_ids.product_attribute_value_id == values[0])
        source_b = source_template.product_variant_ids - source_a
        target_a = target_template.product_variant_ids.filtered(
            lambda product: product.product_template_attribute_value_ids.product_attribute_value_id == values[0])
        target_b = target_template.product_variant_ids - target_a
        source_a.default_code = False
        source_b.default_code = "FAMILY-B-REFERENCE"
        target_a.default_code = "KEEP-TARGET-A"
        target_b.default_code = source_b.default_code
        target_receipt = self._make_in_move(target_a, 2, 10, company=self.target_company,
                                           location_dest_id=self.target_warehouse.lot_stock_id.id,
                                           picking_type_id=self.target_warehouse.in_type_id.id)
        source_receipt = self._make_in_move(source_a, 3, 10)
        self._make_in_move(source_b, 4, 12)
        # Different metadata cost must not be copied over the matched product.
        # The actual source and target FIFO receipts both remain at 10/unit.
        source_a.with_context(disable_auto_revaluation=True).standard_price = 99
        self.assertEqual(source_receipt.value, 30)
        target_receipt_before = (target_receipt.value, target_receipt.write_date)
        target_a_price = target_a.standard_price
        self.assertAlmostEqual(target_a_price, 10, places=2)
        before_templates = self.env["product.template"].search_count([])
        before_products = self.env["product.product"].search_count([])
        cutover = self._cutover()
        cutover.action_preview()
        matches = {row["source"]: row["target"] for row in cutover.preview_data["product_matches"]}
        self.assertEqual(matches[source_a.id], target_a.id)
        self.assertEqual(matches[source_b.id], target_b.id)
        before_preview = cutover.preview_data
        self.env.invalidate_all()
        after_preview = cutover._preview_data()
        for key in before_preview:
            self.assertEqual(before_preview[key], after_preview[key], key)
        cutover.action_apply()
        self.assertEqual(set(cutover.stock_batch_id.product_line_ids.target_product_id.ids),
                         {target_a.id, target_b.id})
        self.assertEqual(target_a.default_code, "KEEP-TARGET-A")
        self.assertAlmostEqual(target_a.standard_price, target_a_price, places=2)
        self.assertEqual(target_a.qty_available, 5)
        self.assertAlmostEqual(target_a.total_value, 50, places=2)
        self.assertEqual((target_receipt.value, target_receipt.write_date), target_receipt_before)
        self.assertEqual(target_b.qty_available, 4)
        self.assertEqual(self.env["product.template"].search_count([]), before_templates)
        self.assertEqual(self.env["product.product"].search_count([]), before_products)
