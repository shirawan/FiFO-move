from odoo import Command
from odoo.tests import tagged
from odoo.addons.company_financial_cutover.tests.test_cutover import FinancialCutoverCase
from odoo.addons.company_financial_cutover.tests.test_consolidation import TestConsolidation

@tagged("post_install", "-at_install")
class TestPurchaseBranches(FinancialCutoverCase):
    _branches = TestConsolidation._branches
    def test_branch_purchase_history_keeps_actual_company_and_skips_repeats(self):
        branches = self._branches()
        product = self.env['product.product'].create({'name': 'Branch service', 'type': 'service',
            'supplier_taxes_id': [Command.clear()]})
        orders = self.env['purchase.order']
        for company in self.source | branches:
            orders |= self.env['purchase.order'].with_company(company).create({
                'company_id': company.id, 'partner_id': self.vendor.id, 'partner_ref': 'SAME-REF',
                'date_order': '2024-06-30 10:00:00',
                'order_line': [Command.create({'product_id': product.id, 'name': 'Branch order',
                    'product_qty': 5, 'price_unit': 10, 'product_uom_id': product.uom_id.id,
                    'date_planned': '2024-07-01 10:00:00', 'tax_ids': [Command.clear()]})]})
        batch = self._batch()
        batch.include_purchase_history = True
        batch.write({'include_source_branches': True, 'include_financial': False})
        self._run(batch)
        histories = batch.purchase_history_ids
        self.assertEqual(len(histories), 3)
        self.assertEqual(set(histories.source_company_id.ids), set((self.source | branches).ids))
        for history in histories:
            self.assertEqual(history.snapshot['company_id'], history.source_company_id.id)
            self.assertEqual(history.source_company_name, history.source_company_id.name)
            marker = self.env['ir.config_parameter'].sudo().get_param(
                'company_financial_cutover.purchase.%s.%s' % (history.source_company_id.id, history.source_order_res_id))
            self.assertTrue(marker)
        repeat = self.env['company.financial.cutover'].create({
            'source_company_id': branches[0].id, 'target_company_id': self.target.id,
            'include_financial': False, 'include_purchase_history': True})
        self.assertFalse(repeat._purchase_plan())
        # Each branch has a legitimate separate order with the same vendor ref.
        # It must not be mistaken for another active original in a different branch.
        history = histories.filtered(lambda row: row.source_company_id == branches[0])
        original = orders.filtered(lambda order: order.company_id == branches[0])
        original.button_cancel()
        history.action_prepare_draft()
        self.assertEqual(history.target_order_id.origin,
            'Migrated purchase %s/%s' % (branches[0].id, original.id))
