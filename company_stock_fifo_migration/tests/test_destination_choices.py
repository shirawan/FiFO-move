from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged, Form
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestDestinationChoices(TestStockValuationCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Explicit destination target")
        cls.target_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.target_company.id)], limit=1)

    def _cutover(self):
        return self.env["company.stock.warehouse.cutover"].with_context(
            allowed_company_ids=(self.env.company | self.target_company).ids).create({
                "source_warehouse_id": self.warehouse.id,
                "target_company_id": self.target_company.id,
                "target_warehouse_id": self.target_warehouse.id})

    def _target(self, source, method=None):
        Product = source.with_context(allowed_company_ids=(self.env.company | self.target_company).ids
                                     ).with_company(self.target_company)
        category = self.env["product.category"].with_env(Product.env).create({
            "name": "Explicit destination category", "property_cost_method": method or source.cost_method,
            "property_valuation": source.valuation,
            "property_stock_valuation_account_id": self.target_company.account_stock_valuation_id.id,
            "property_stock_journal": self.target_company.account_stock_journal_id.id})
        return Product.copy({"name": source.name, "company_id": self.target_company.id,
                             "categ_id": category.id, "barcode": False})

    def test_explicit_choice_resolves_duplicates_without_deleting_or_creating(self):
        source = self.product_fifo_auto
        self._make_in_move(source, 3, 12)
        chosen, other = self._target(source), self._target(source)
        cutover = self._cutover()
        with self.assertRaisesRegex(UserError, "Destination Product Choices"):
            cutover.action_preview()
        with Form(cutover) as form:
            with form.product_choice_ids.new() as choice:
                choice.source_product_id = source
                choice.target_product_id = chosen
        count = self.env["product.product"].search_count([])
        cutover.action_preview()
        self.registry_enter_test_mode()
        cutover.action_apply()
        self.assertEqual(cutover.stock_batch_id.product_line_ids.target_product_id, chosen)
        self.assertEqual(chosen.qty_available, 3)
        self.assertEqual(other.qty_available, 0)
        self.assertTrue(other.active)
        self.assertEqual(self.env["product.product"].search_count([]), count)
        with self.assertRaises(UserError):
            cutover.product_choice_ids.write({"target_product_id": other.id})
        with self.assertRaises(UserError):
            cutover.product_choice_ids.unlink()

    def test_choice_change_invalidates_preview_and_wrong_company_rejected(self):
        source = self.product_fifo_auto
        self._make_in_move(source, 3, 12)
        chosen, other = self._target(source), self._target(source)
        cutover = self._cutover()
        cutover.write({"product_choice_ids": [Command.create({
            "source_product_id": source.id, "target_product_id": chosen.id})]})
        cutover.action_preview()
        cutover.product_choice_ids.target_product_id = other
        self.assertEqual(cutover.state, "draft")
        self.assertFalse(cutover.preview_hash)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            cutover.product_choice_ids.target_product_id = source

    def test_standard_to_fifo_preserves_existing_stock_and_future_consumption(self):
        source = self.product_standard_auto
        source.standard_price = 12
        self._make_in_move(source, 3, 12)
        target = self._target(source, "fifo")
        self._make_in_move(target, 2, 9, company=self.target_company,
                           location_dest_id=self.target_warehouse.lot_stock_id.id)
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()
        cutover.action_apply()
        self.assertEqual(source.cost_method, "standard")
        self.assertEqual(target.cost_method, "fifo")
        self.assertEqual(source.qty_available, 0)
        self.assertEqual(target.qty_available, 5)
        self.assertEqual(target.total_value, 54)
        self.assertEqual(target._run_fifo(2), 18)
        self.assertEqual(target._run_fifo(3), 30)
        self.assertEqual(target._run_fifo(5), 54)

    def test_standard_to_empty_fifo_periodic_keeps_value_without_journals(self):
        source = self.product_standard
        source.standard_price = 7
        self._make_in_move(source, 4, 7)
        target = self._target(source, "fifo")
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()
        cutover.action_apply()
        self.assertEqual(target.qty_available, 4)
        self.assertEqual(target.total_value, 28)
        self.assertEqual(target._run_fifo(4), 28)
        self.assertEqual(target.valuation, "periodic")
        self.assertFalse(self.env["stock.move"].search([
            ("product_id", "=", target.id)]).account_move_id)

    def test_other_cost_method_difference_still_requires_review(self):
        source = self.product_fifo_auto
        self._make_in_move(source, 3, 12)
        target = self._target(source, "average")
        with self.assertRaisesRegex(UserError, "Cost method"):
            self._cutover().action_preview()
        self.assertEqual(source.qty_available, 3)
        self.assertEqual(target.qty_available, 0)

    def test_periodic_to_perpetual_reuses_product_and_checks_opening_accounts(self):
        source = self.product_fifo
        self._make_in_move(source, 3, 12)
        target = self._target(source)
        target.categ_id.property_valuation = "real_time"
        self._make_in_move(target, 2, 9, company=self.target_company,
                           location_dest_id=self.target_warehouse.lot_stock_id.id)
        count = self.env["product.product"].search_count([])
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()
        cutover.action_apply()
        self.assertEqual(cutover.stock_batch_id.product_line_ids.target_product_id, target)
        self.assertEqual(self.env["product.product"].search_count([]), count)
        self.assertEqual(source.valuation, "periodic")
        self.assertEqual(target.valuation, "real_time")
        self.assertEqual(source.qty_available, 0)
        self.assertEqual(target.qty_available, 5)
        self.assertEqual(target.total_value, 54)
        self.assertEqual(target._run_fifo(2), 18)
        self.assertEqual(target._run_fifo(5), 54)
        moves = cutover.stock_batch_id.created_move_ids
        self.assertFalse(moves.filtered(lambda move: move.company_id == self.env.company).account_move_id)
        opening = moves.filtered(lambda move: move.company_id == self.target_company)
        self.assertTrue(opening.account_move_id)
        self.assertTrue(all(move.state == "posted" for move in opening.account_move_id))
        stock_lines = opening.account_move_id.line_ids.filtered(
            lambda line: line.account_id == self.target_company.account_stock_valuation_id)
        clearing_lines = opening.account_move_id.line_ids.filtered(
            lambda line: line.account_id == cutover.stock_batch_id.target_clearing_account_id)
        self.assertEqual(sum(stock_lines.mapped("balance")), 36)
        self.assertEqual(sum(clearing_lines.mapped("balance")), -36)

    def test_periodic_standard_to_perpetual_fifo_keeps_reviewed_value(self):
        source = self.product_standard
        source.standard_price = 7
        self._make_in_move(source, 4, 7)
        target = self._target(source, "fifo")
        target.categ_id.property_valuation = "real_time"
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()
        cutover.action_apply()
        self.assertEqual(source.cost_method, "standard")
        self.assertEqual(source.valuation, "periodic")
        self.assertEqual(target.cost_method, "fifo")
        self.assertEqual(target.valuation, "real_time")
        self.assertEqual(target.qty_available, 4)
        self.assertEqual(target._run_fifo(4), 28)
        self.assertEqual(target.total_value, 28)

    def test_periodic_to_perpetual_rejects_wrong_opening_account_atomically(self):
        from ..models import execution
        source = self.product_fifo
        self._make_in_move(source, 3, 12)
        target = self._target(source)
        target.categ_id.property_valuation = "real_time"
        wrong_account = self.target_company.account_stock_valuation_id.copy({
            "code": "BADOPEN", "name": "Wrong opening account"})
        cutover = self._cutover()
        cutover.action_preview()
        self.registry_enter_test_mode()
        original = execution.reconcile

        def corrupt_opening(batch, plan, closing, opening, locations, lots):
            journals = opening.account_move_id
            journals.button_draft()
            journals.line_ids.filtered(lambda line: line.account_id ==
                self.target_company.account_stock_valuation_id).write({"account_id": wrong_account.id})
            journals.action_post()
            return original(batch, plan, closing, opening, locations, lots)

        with patch.object(execution, "reconcile", side_effect=corrupt_opening):
            with self.assertRaisesRegex(UserError, "valuation/clearing amounts"):
                cutover.action_apply()
        self.assertEqual(source.qty_available, 3)
        self.assertEqual(target.qty_available, 0)
        self.assertNotEqual(cutover.state, "done")

    def test_perpetual_to_periodic_still_requires_review(self):
        source = self.product_fifo_auto
        self._make_in_move(source, 3, 12)
        target = self._target(source)
        target.categ_id.property_valuation = "periodic"
        with self.assertRaisesRegex(UserError, "Inventory valuation"):
            self._cutover().action_preview()

    def test_destination_valuation_change_requires_fresh_preview(self):
        source = self.product_fifo
        self._make_in_move(source, 3, 12)
        target = self._target(source)
        target.categ_id.property_valuation = "real_time"
        cutover = self._cutover()
        cutover.action_preview()
        target.categ_id.property_valuation = "periodic"
        self.registry_enter_test_mode()
        with self.assertRaisesRegex(UserError, "fresh Preview"):
            cutover.action_apply()
        self.assertEqual(source.qty_available, 3)
        self.assertEqual(target.qty_available, 0)
