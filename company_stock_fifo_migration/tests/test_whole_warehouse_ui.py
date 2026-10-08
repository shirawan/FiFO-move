from lxml import html

from odoo.tests import Form, tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon


@tagged("post_install", "-at_install")
class TestWholeWarehouseUi(TestStockValuationCommon):
    def test_native_form_saves_and_previews_without_a_matching_batch(self):
        target = self._create_company(name="Whole Warehouse Form Target")
        warehouse = self.env["stock.warehouse"].search([("company_id", "=", target.id)], limit=1)
        self.product_fifo_auto.company_id = False
        self._make_in_move(self.product_fifo_auto, 3, 12)
        Cutover = self.env["company.stock.warehouse.cutover"].with_context(
            allowed_company_ids=(self.env.company | target).ids,
        )
        with Form(Cutover, view="company_stock_fifo_migration.whole_warehouse_cutover_form") as form:
            form.source_warehouse_id = self.warehouse
            form.target_company_id = target
            form.target_warehouse_id = warehouse
            self.assertTrue(form.release_source_reservations)
            form.release_source_reservations = True
        cutover = form.record

        cutover.action_preview()

        self.assertEqual(cutover.state, "review")
        self.assertEqual(cutover.product_count, 1)
        self.assertEqual(cutover.total_value, 36)
        self.assertIn(self.product_fifo_auto.name, cutover.preview_html)
        preview = html.fromstring(cutover.preview_html)
        self.assertEqual(len(preview.xpath("//table")), 2)
        self.assertEqual(preview.xpath("//h4/text()"), ["Reservations released on Confirm"])
        self.assertFalse(cutover.stock_batch_id)
        self.assertTrue(cutover.release_source_reservations)
        action = self.env.ref("company_stock_fifo_migration.stock_fifo_migration_menu").action
        self.assertEqual(action.res_model, "company.stock.warehouse.cutover")
