from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestStockFifoReviewUi(TestStockValuationCommon):
    def _ui_batch(self, target, target_product):
        context = {"allowed_company_ids": [self.env.company.id, target.id]}
        provider = self.env["company.kit.bom.migration.batch"].with_context(context).create({
            "source_company_id": self.env.company.id,
            "target_company_id": target.id,
        })
        batch = self.env["company.stock.fifo.migration"].with_context(context).create({
            "source_company_id": self.env.company.id,
            "target_company_id": target.id,
            "mapping_batch_ref": "company.kit.bom.migration.batch,%s" % provider.id,
            "source_clearing_account_id": self.env.company.account_stock_valuation_id.id,
            "target_clearing_account_id": target.account_stock_valuation_id.id,
        })
        self.env["company.stock.fifo.product"].with_context(batch._system().env.context).create({
            "batch_id": batch.id,
            "source_product_id": self.product_fifo_auto.id,
            "target_product_id": target_product.id,
        })
        self.env["company.stock.fifo.warehouse"].with_context(batch._system().env.context).create({
            "batch_id": batch.id, "source_warehouse_id": self.warehouse.id,
        })
        batch._system().write({"state": "mapped"})
        return batch

    def test_parent_save_updates_only_its_existing_review_rows(self):
        target = self._create_company(name="Review UI Target")
        target_product = self.product_fifo_auto.with_context(
            allowed_company_ids=[self.env.company.id, target.id]
        ).copy({"company_id": target.id, "name": "Review UI Target Product"})
        batch = self._ui_batch(target, target_product)
        other = self._ui_batch(target, target_product)
        target_warehouse = self.env["stock.warehouse"].search([
            ("company_id", "=", target.id),
        ], limit=1)

        batch.write({
            "product_line_ids": [Command.update(batch.product_line_ids.id, {"selected": False})],
            "warehouse_line_ids": [Command.update(batch.warehouse_line_ids.id, {
                "selected": True, "target_warehouse_id": target_warehouse.id,
            })],
        })

        self.assertFalse(batch.product_line_ids.selected)
        self.assertTrue(batch.warehouse_line_ids.selected)
        self.assertEqual(batch.warehouse_line_ids.target_warehouse_id, target_warehouse)
        self.assertTrue(other.product_line_ids.selected)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            batch.write({"product_line_ids": [Command.update(
                other.product_line_ids.id, {"selected": False}
            )]})
        self.assertTrue(other.product_line_ids.selected)
        for command in (
            Command.create({"selected": True}),
            Command.delete(batch.product_line_ids.id),
            Command.unlink(batch.product_line_ids.id),
            Command.link(other.product_line_ids.id),
            Command.clear(),
            Command.set(other.product_line_ids.ids),
        ):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                batch.write({"product_line_ids": [command]})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            batch.write({"product_line_ids": [Command.update(
                batch.product_line_ids.id, {"batch_id": other.id}
            )]})

    def test_parent_save_cannot_forge_child_audit_results(self):
        target = self._create_company(name="Review UI Audit Target")
        target_product = self.product_fifo_auto.with_context(
            allowed_company_ids=[self.env.company.id, target.id]
        ).copy({"company_id": target.id, "name": "Review UI Audit Product"})
        batch = self._ui_batch(target, target_product)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            batch.write({"product_line_ids": [Command.update(
                batch.product_line_ids.id, {"actual_value": 99}
            )]})
        self.assertEqual(batch.product_line_ids.actual_value, 0)
