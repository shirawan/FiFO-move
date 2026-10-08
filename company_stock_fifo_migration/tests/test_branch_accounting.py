from odoo.tests import tagged
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestWarehouseBranchAccounting(TestStockValuationCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_parent = cls._create_company(name="Target Accounting Parent")

    def _preview_with_unrelated_configuration(self, field, value):
        source = self.env["res.company"].create({
            "name": "Branch With Unrelated Accounting", "parent_id": self.env.company.id,
            "account_stock_valuation_id": self.env.company.account_stock_valuation_id.id,
            "account_stock_journal_id": self.env.company.account_stock_journal_id.id,
        })
        with self.assertRaises(UserError), self.env.cr.savepoint():
            source.write({field: value.id})
        # Simulate a legacy invalid setting that bypassed Odoo's native write gate.
        # Preview must still reject it before creating any cutover business records.
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE res_company SET %s = %s WHERE id = %s",
            SQL.identifier(field), value.id, source.id,
        ))
        source.invalidate_recordset([field])
        context = {"allowed_company_ids": (self.env.company | self.target_parent | source).ids}
        warehouse = self.env["stock.warehouse"].with_context(context).create({
            "name": "Unrelated Accounting Stock", "code": "BADAC", "company_id": source.id,
        })
        cutover = self.env["company.stock.warehouse.cutover"].with_context(context).create({
            "source_warehouse_id": warehouse.id, "target_company_id": self.target_parent.id,
            "target_warehouse_id": self.env["stock.warehouse"].search([
                ("company_id", "=", self.target_parent.id),
            ], limit=1).id,
        })
        with self.assertRaisesRegex(UserError, "Configure the inventory valuation account and stock journal"):
            cutover.action_preview()
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.stock_batch_id)

    def test_preview_rejects_unrelated_company_valuation_account(self):
        self._preview_with_unrelated_configuration(
            "account_stock_valuation_id", self.target_parent.account_stock_valuation_id,
        )

    def test_preview_rejects_unrelated_company_stock_journal(self):
        self._preview_with_unrelated_configuration(
            "account_stock_journal_id", self.target_parent.account_stock_journal_id,
        )

    def test_parent_owned_valuation_accounts_and_journals_work_through_confirm(self):
        self._assert_branch_cutover()

    def test_parent_owned_product_moves_only_source_branch_stock(self):
        self._assert_branch_cutover(parent_owned=True)

    def _assert_branch_cutover(self, parent_owned=False):
        source_parent = self.env.company
        target_parent = self.target_parent
        source = self.env["res.company"].create({
            "name": "Source Accounting Branch", "parent_id": source_parent.id,
            "account_stock_valuation_id": source_parent.account_stock_valuation_id.id,
            "account_stock_journal_id": source_parent.account_stock_journal_id.id,
        })
        target = self.env["res.company"].create({
            "name": "Target Accounting Branch", "parent_id": target_parent.id,
            "account_stock_valuation_id": target_parent.account_stock_valuation_id.id,
            "account_stock_journal_id": target_parent.account_stock_journal_id.id,
        })
        context = {"allowed_company_ids": (source_parent | target_parent | source | target).ids}
        warehouse = self.env["stock.warehouse"].with_context(context).create({
            "name": "Source Branch Stock", "code": "BRSRC", "company_id": source.id,
        })
        product = self.product_fifo_auto.with_context(context).with_company(source)
        product.company_id = source_parent if parent_owned else False
        product.categ_id.write({
            "property_cost_method": "fifo", "property_valuation": "real_time",
            "property_stock_valuation_account_id": source_parent.account_stock_valuation_id.id,
            "property_stock_journal": source_parent.account_stock_journal_id.id,
        })
        history = self._make_in_move(
            product, 10, 10, company=source, location_dest_id=warehouse.lot_stock_id.id,
            picking_type_id=warehouse.in_type_id.id,
        ) | self._make_in_move(
            product, 10, 20, company=source, location_dest_id=warehouse.lot_stock_id.id,
            picking_type_id=warehouse.in_type_id.id,
        )
        history |= self._make_out_move(
            product, 5, company=source, location_id=warehouse.lot_stock_id.id,
            picking_type_id=warehouse.out_type_id.id,
        )
        before = [(move.id, move.company_id.id, move.value, move.write_date) for move in history]
        outside_quants = self.env["stock.quant"]
        if parent_owned:
            sibling = self.env["res.company"].create({
                "name": "Sibling With Same Parent Product", "parent_id": source_parent.id,
            })
            context = {"allowed_company_ids": (source_parent | target_parent | source | target | sibling).ids}
            sibling_warehouse = self.env["stock.warehouse"].with_context(context).create({
                "name": "Sibling Stock", "code": "BRSIB", "company_id": sibling.id,
            })
            self._make_in_move(product, 4, 8, company=source_parent,
                               location_dest_id=self.warehouse.lot_stock_id.id,
                               picking_type_id=self.warehouse.in_type_id.id)
            self._make_in_move(product, 6, 7, company=sibling,
                               location_dest_id=sibling_warehouse.lot_stock_id.id,
                               picking_type_id=sibling_warehouse.in_type_id.id)
            outside_quants = self.env["stock.quant"].search([
                ("product_id", "=", product.id),
                ("company_id", "in", (source_parent | sibling).ids),
                ("location_id.usage", "=", "internal"),
            ])
            self.assertEqual(sum(outside_quants.mapped("quantity")), 10)
        outside_before = outside_quants.read(["quantity", "reserved_quantity", "write_date"])
        target_warehouse = self.env["stock.warehouse"].with_context(context).create({
            "name": "Existing Target Branch Stock", "code": "BRTGT", "company_id": target.id,
        })
        cutover = self.env["company.stock.warehouse.cutover"].with_context(context).create({
            "source_warehouse_id": warehouse.id, "target_company_id": target.id,
            "target_warehouse_id": target_warehouse.id,
        })

        cutover.action_preview()

        self.assertEqual(cutover.total_value, 250)
        self.registry_enter_test_mode()
        cutover.action_apply()

        self.assertEqual(cutover.state, "done")
        batch = cutover.stock_batch_id
        copied = batch.product_line_ids.target_product_id.with_company(target)
        self.assertEqual(copied.company_id, target)
        self.assertEqual(copied.qty_available, 15)
        self.assertEqual(copied.total_value, 250)
        branch_product = product.with_context(allowed_company_ids=source.ids)
        self.assertEqual(branch_product.qty_available, 0)
        self.assertEqual(branch_product.total_value, 0)
        self.assertEqual(product.company_id, source_parent if parent_owned else self.env["res.company"])
        self.assertEqual(outside_quants.read(["quantity", "reserved_quantity", "write_date"]), outside_before)
        self.assertEqual(batch.source_clearing_account_id.company_ids, source)
        self.assertEqual(batch.target_clearing_account_id.company_ids, target)
        self.assertEqual(source.account_stock_valuation_id, source_parent.account_stock_valuation_id)
        self.assertEqual(target.account_stock_valuation_id, target_parent.account_stock_valuation_id)
        for move in batch.created_move_ids:
            expected = source if move.company_id == source else target
            self.assertEqual(move.account_move_id.state, "posted")
            self.assertEqual(move.account_move_id.company_id, expected)
            self.assertEqual(move.account_move_id.journal_id,
                             expected.account_stock_journal_id)
        self.assertEqual([(move.id, move.company_id.id, move.value, move.write_date)
                          for move in history], before)
