from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon

from .test_stock_fifo_migration import TestCompanyStockFifoMigration


@tagged("post_install", "-at_install")
class TestStockFifoCutoverSafety(TestStockValuationCommon):
    _batch = TestCompanyStockFifoMigration._batch

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Stock Cutover Safety Target")

    def _cutover(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 10, 10)
        batch, target_product = self._batch(product, self.target_company)
        return batch, product, target_product

    def _user(self, login, companies, group):
        return self.env["res.users"].create({
            "name": login,
            "login": login,
            "company_id": self.env.company.id,
            "company_ids": [Command.set(companies.ids)],
            "group_ids": [Command.set([self.env.ref(group).id])],
        })

    def test_preview_requires_settings_administrator(self):
        batch, source, target = self._cutover()
        user = self._user(
            "stock-cutover-non-admin",
            self.env.company | self.target_company,
            "stock.group_stock_manager",
        )

        with self.assertRaisesRegex(AccessError, "Settings administrator"):
            batch.with_user(user).action_preview()

        self.assertEqual(batch.state, "mapped")
        self.assertEqual(source.qty_available, 10)
        self.assertEqual(target.qty_available, 0)

    def test_preview_requires_access_to_both_companies(self):
        batch, source, target = self._cutover()
        user = self._user(
            "stock-cutover-source-only-admin",
            self.env.company,
            "base.group_system",
        )

        with self.assertRaises(AccessError):
            batch.with_user(user).with_context(
                allowed_company_ids=self.env.company.ids,
            ).action_preview()

        self.assertEqual(batch.state, "mapped")
        self.assertEqual(source.qty_available, 10)
        self.assertEqual(target.qty_available, 0)

    def test_client_context_cannot_forge_completion_audit(self):
        batch, source, target = self._cutover()

        with self.assertRaises(AccessError):
            batch.with_context(_stock_fifo_cutover_write=True).write({
                "state": "done",
                "completed_by_id": self.env.user.id,
            })

        self.assertEqual(batch.state, "mapped")
        self.assertFalse(batch.completed_by_id)
        self.assertFalse(batch.created_move_ids)
        self.assertEqual(source.qty_available, 10)
        self.assertEqual(target.qty_available, 0)

    def test_client_context_cannot_reprice_reviewed_fifo(self):
        batch, source, target = self._cutover()
        batch.action_preview()

        with self.assertRaises(AccessError):
            batch.tranche_line_ids.with_context(
                _stock_fifo_cutover_write="system",
            ).write({"unit_value": 0})

        self.assertEqual(batch.tranche_line_ids.unit_value, 10)
        self.assertEqual(source.total_value, 100)
        self.assertEqual(target.qty_available, 0)

    def test_clearing_account_cannot_be_inventory_valuation_account(self):
        batch, source, target = self._cutover()
        batch.source_clearing_account_id = source._get_product_accounts()["stock_valuation"]

        with self.assertRaisesRegex(UserError, "(?i)(clearing.*valuation|valuation.*clearing)"):
            batch.action_preview()

        self.assertEqual(batch.state, "mapped")
        self.assertEqual(source.qty_available, 10)
        self.assertEqual(target.qty_available, 0)

    def test_target_existing_stock_is_rejected(self):
        batch, source, target = self._cutover()
        self._make_in_move(target, 1, 10, company=self.target_company)

        with self.assertRaisesRegex(UserError, "no existing stock-move history"):
            batch.action_preview()

        self.assertEqual(source.qty_available, 10)
        self.assertEqual(target.qty_available, 1)
        self.assertFalse(batch.created_move_ids)

    def test_target_history_is_rejected_even_when_stock_is_zero(self):
        batch, source, target = self._cutover()
        self._make_in_move(target, 1, 10, company=self.target_company)
        self._make_out_move(target, 1, company=self.target_company)
        self.assertEqual(target.qty_available, 0)

        with self.assertRaisesRegex(UserError, "no existing stock-move history"):
            batch.action_preview()

        self.assertEqual(source.qty_available, 10)
        self.assertFalse(batch.created_move_ids)

    def test_reserved_source_stock_is_rejected(self):
        batch, source, target = self._cutover()
        outgoing = self.env["stock.move"].create({
            "product_id": source.id,
            "product_uom": source.uom_id.id,
            "product_uom_qty": 2,
            "location_id": self.stock_location.id,
            "location_dest_id": self.customer_location.id,
            "picking_type_id": self.picking_type_out.id,
        })
        outgoing._action_confirm()
        outgoing._action_assign()
        self.assertEqual(outgoing.state, "assigned")

        with self.assertRaisesRegex(UserError, "reservations"):
            batch.action_preview()

        self.assertEqual(source.qty_available, 10)
        self.assertEqual(target.qty_available, 0)
        self.assertFalse(batch.created_move_ids)

    def test_source_stock_change_after_check_blocks_apply_without_target_changes(self):
        batch, source, target = self._cutover()
        batch.action_preview()
        batch.action_check()
        self.assertEqual(batch.state, "ready")
        self._make_out_move(source, 2)

        with self.assertRaisesRegex(UserError, "(?i)(changed|stale|refresh|preview)"):
            batch.action_apply()

        self.assertEqual(batch.state, "ready")
        self.assertEqual(source.qty_available, 8)
        self.assertEqual(source.total_value, 80)
        self.assertEqual(target.qty_available, 0)
        self.assertFalse(batch.created_move_ids)
