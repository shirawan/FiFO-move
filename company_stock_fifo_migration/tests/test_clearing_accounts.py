from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged
from odoo.addons.stock_account.tests.common import TestStockValuationCommon

from .test_stock_fifo_migration import TestCompanyStockFifoMigration


@tagged("post_install", "-at_install")
class TestStockCutoverClearingAccounts(TestStockValuationCommon):
    _batch = TestCompanyStockFifoMigration._batch

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_company = cls._create_company(name="Clearing Account Target")

    def _draft(self):
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        provider = self.env["company.kit.bom.migration.batch"].with_context(context).create({
            "source_company_id": self.env.company.id,
            "target_company_id": self.target_company.id,
        })
        return self.env["company.stock.fifo.migration"].with_context(context).create({
            "source_company_id": self.env.company.id,
            "target_company_id": self.target_company.id,
            "mapping_batch_ref": "%s,%s" % (provider._name, provider.id),
        })

    def test_new_form_saves_draft_before_account_button(self):
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        provider = self.env["company.kit.bom.migration.batch"].with_context(context).create({
            "source_company_id": self.env.company.id,
            "target_company_id": self.target_company.id,
        })
        with Form(self.env["company.stock.fifo.migration"].with_context(context)) as form:
            form.source_company_id = self.env.company
            form.target_company_id = self.target_company
            form.mapping_batch_ref = provider
        batch = form.record
        self.assertEqual(batch.state, "draft")
        self.assertFalse(batch.source_clearing_account_id)
        self.assertFalse(batch.target_clearing_account_id)
        batch.action_create_clearing_accounts()
        self.assertEqual(batch.source_clearing_account_id.company_ids, self.env.company)
        self.assertEqual(batch.target_clearing_account_id.company_ids, self.target_company)

    def test_new_draft_still_rejects_review_rows_and_forged_audit(self):
        context = {"allowed_company_ids": (self.env.company | self.target_company).ids}
        provider = self.env["company.kit.bom.migration.batch"].with_context(context).create({
            "source_company_id": self.env.company.id,
            "target_company_id": self.target_company.id,
        })
        Batch = self.env["company.stock.fifo.migration"].with_context(context)
        values = {
            "source_company_id": self.env.company.id,
            "target_company_id": self.target_company.id,
            "mapping_batch_ref": "%s,%s" % (provider._name, provider.id),
        }
        for forged in (
            {"allocation_reviewed": True}, {"name": "Forged audit identity"},
            {"state": "done"}, {"snapshot_data": {"forged": True}},
            *({field: [Command.create({})]} for field in (
                "product_line_ids", "warehouse_line_ids", "location_line_ids",
                "lot_line_ids", "tranche_line_ids",
            )),
        ):
            with self.subTest(fields=list(forged)), self.assertRaisesRegex(AccessError, "system-managed"):
                Batch.create(values | forged)
        self.assertFalse(Batch.search_count([("mapping_batch_ref", "=", values["mapping_batch_ref"])]))

    def test_button_creates_company_accounts_and_fills_saved_draft(self):
        batch = self._draft()
        before = {model: self.env[model].search_count([]) for model in (
            "account.account", "stock.move", "account.move",
        )}

        batch.action_create_clearing_accounts()

        self.assertEqual(batch.source_clearing_account_id.name, "Stock Migration Clearing")
        self.assertEqual(batch.target_clearing_account_id.name, "Stock Migration Clearing")
        self.assertEqual(batch.source_clearing_account_id.company_ids, self.env.company)
        self.assertEqual(batch.target_clearing_account_id.company_ids, self.target_company)
        self.assertNotEqual(batch.source_clearing_account_id, batch.target_clearing_account_id)
        self.assertEqual(batch.source_clearing_account_id.account_type, "asset_current")
        self.assertEqual(batch.target_clearing_account_id.account_type, "asset_current")
        self.assertEqual(batch.state, "draft")
        self.assertEqual(self.env["account.account"].search_count([]), before["account.account"] + 2)
        for model in ("stock.move", "account.move"):
            self.assertEqual(self.env[model].search_count([]), before[model])

    def test_repeat_clicks_and_new_batches_reuse_accounts_even_after_rename(self):
        first = self._draft()
        first.action_create_clearing_accounts()
        source_account = first.source_clearing_account_id
        target_account = first.target_clearing_account_id
        source_account.name = "Accountant-reviewed clearing"
        count = self.env["account.account"].search_count([])

        first.action_create_clearing_accounts()
        second = self._draft()
        second.action_create_clearing_accounts()

        self.assertEqual(first.source_clearing_account_id, source_account)
        self.assertEqual(second.source_clearing_account_id, source_account)
        self.assertEqual(second.target_clearing_account_id, target_account)
        self.assertEqual(source_account.name, "Accountant-reviewed clearing")
        self.assertEqual(self.env["account.account"].search_count([]), count)

    def test_existing_approved_selection_is_not_replaced(self):
        batch = self._draft()
        approved = self.env["account.account"].create({
            "name": "Approved opening equity", "code": "APPROVED01",
            "account_type": "equity", "company_ids": [Command.set(self.env.company.ids)],
        })
        batch.source_clearing_account_id = approved
        count = self.env["account.account"].search_count([])

        batch.action_create_clearing_accounts()

        self.assertEqual(batch.source_clearing_account_id, approved)
        self.assertEqual(approved.account_type, "equity")
        self.assertEqual(batch.target_clearing_account_id.company_ids, self.target_company)
        self.assertEqual(self.env["account.account"].search_count([]), count + 1)

    def test_archived_saved_account_is_not_unarchived_or_replaced(self):
        batch = self._draft()
        batch.action_create_clearing_accounts()
        archived = batch.source_clearing_account_id
        batch.source_clearing_account_id = False
        archived.active = False
        count = self.env["account.account"].with_context(active_test=False).search_count([])

        with self.assertRaisesRegex(UserError, "active company-specific"):
            batch.action_create_clearing_accounts()

        self.assertFalse(archived.active)
        self.assertFalse(batch.source_clearing_account_id)
        self.assertEqual(self.env["account.account"].with_context(active_test=False).search_count([]), count)

    def test_account_code_collision_does_not_modify_unrelated_account(self):
        unrelated = self.env["account.account"].create({
            "name": "Unrelated existing expense", "code": "STKCLR0001",
            "account_type": "expense", "company_ids": [Command.set(self.env.company.ids)],
        })
        batch = self._draft()

        batch.action_create_clearing_accounts()

        self.assertEqual(batch.source_clearing_account_id.with_company(self.env.company).code, "STKCLR0002")
        self.assertEqual(unrelated.code, "STKCLR0001")
        self.assertEqual(unrelated.name, "Unrelated existing expense")
        self.assertEqual(unrelated.account_type, "expense")

    def test_button_requires_settings_admin_and_both_companies(self):
        batch = self._draft()
        count = self.env["account.account"].search_count([])
        for login, companies, group in (
            ("clearing-non-admin", self.env.company | self.target_company, "stock.group_stock_manager"),
            ("clearing-source-only", self.env.company, "base.group_system"),
        ):
            user = self.env["res.users"].create({
                "name": login, "login": login, "company_id": self.env.company.id,
                "company_ids": [Command.set(companies.ids)],
                "group_ids": [Command.set([self.env.ref(group).id])],
            })
            with self.assertRaises(AccessError):
                batch.with_user(user).with_context(allowed_company_ids=companies.ids).action_create_clearing_accounts()
        self.assertFalse(batch.source_clearing_account_id)
        self.assertFalse(batch.target_clearing_account_id)
        self.assertEqual(self.env["account.account"].search_count([]), count)

    def test_preview_without_accounts_refuses_without_creating_them(self):
        product = self.product_fifo_auto
        product.company_id = self.env.company
        self._make_in_move(product, 2, 10)
        batch, target_product = self._batch(product, self.target_company)
        batch.write({"source_clearing_account_id": False, "target_clearing_account_id": False})
        before = {model: self.env[model].search_count([]) for model in (
            "account.account", "stock.move", "account.move",
        )}

        with self.assertRaisesRegex(UserError, "Choose an active company-specific"):
            batch.action_preview()

        self.assertEqual(product.qty_available, 2)
        self.assertEqual(target_product.qty_available, 0)
        for model, count in before.items():
            self.assertEqual(self.env[model].search_count([]), count)
