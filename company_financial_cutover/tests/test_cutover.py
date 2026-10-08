from datetime import date
from unittest.mock import patch
from uuid import uuid4

from odoo import Command, api
from odoo.exceptions import AccessError, ConcurrencyError, UserError
from odoo.service.model import retrying
from odoo.tests import Form, TransactionCase, tagged, new_test_user


class FinancialCutoverCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        currency = cls.env.company.currency_id
        cls.source = cls.env["res.company"].create({
            "name": "Old Business", "currency_id": currency.id,
            "account_fiscal_country_id": cls.env.ref("base.us").id,
        })
        cls.target = cls.env["res.company"].create({
            "name": "Clean Replacement", "currency_id": currency.id,
            "account_fiscal_country_id": cls.env.ref("base.us").id,
        })
        cls.env = cls.env(context={**cls.env.context, "allowed_company_ids": [cls.source.id, cls.target.id]})
        cls.cutoff = date(2024, 6, 30)
        cls.accounts = {}
        specs = [
            ("receivable", "1000", "asset_receivable"), ("bank", "1010", "asset_cash"),
            ("inventory", "1100", "asset_current"), ("clearing", "1190", "asset_current"),
            ("payable", "2000", "liability_payable"), ("equity", "3000", "equity"),
            ("retained", "3010", "equity_unaffected"), ("revenue", "4000", "income"),
            ("expense", "5000", "expense"),
        ]
        cls.journals = {}
        for company in (cls.source, cls.target):
            accounts = {}
            for name, code, account_type in specs:
                accounts[name] = cls.env["account.account"].with_company(company).create({
                    "name": name, "code": code, "account_type": account_type,
                    "reconcile": account_type in {"asset_receivable", "liability_payable", "asset_cash"},
                    "company_ids": [Command.set(company.ids)],
                })
            cls.accounts[company.id] = accounts
            cls.journals[company.id] = {}
            for name, journal_type in [("general", "general"), ("sale", "sale"), ("purchase", "purchase")]:
                cls.journals[company.id][name] = cls.env["account.journal"].with_company(company).create({
                    "name": name, "code": name[:3].upper(), "type": journal_type, "company_id": company.id,
                    "default_account_id": accounts["expense" if name == "purchase" else "revenue"].id,
                })
        cls.customer = cls.env["res.partner"].create({"name": "Shared Customer"})
        cls.vendor = cls.env["res.partner"].create({"name": "Shared Vendor"})
        for company in (cls.source, cls.target):
            (cls.customer | cls.vendor).with_company(company).write({
                "property_account_receivable_id": cls.accounts[company.id]["receivable"].id,
                "property_account_payable_id": cls.accounts[company.id]["payable"].id,
            })

    def _batch(self):
        return self.env["company.financial.cutover"].create({
            "source_company_id": self.source.id, "target_company_id": self.target.id,
            "cutover_date": self.cutoff, "journal_id": self.journals[self.target.id]["general"].id,
            "retained_earnings_account_id": self.accounts[self.target.id]["retained"].id,
        })

    def _entry(self, company, amounts, when=None, currency=None):
        commands = []
        for values in amounts:
            account_name, balance = values[:2]
            partner = values[2] if len(values) > 2 else self.env["res.partner"]
            foreign = values[3] if len(values) > 3 else balance
            commands.append(Command.create({
                "name": account_name, "account_id": self.accounts[company.id][account_name].id,
                "debit": max(balance, 0), "credit": max(-balance, 0),
                "partner_id": partner.id, "currency_id": (currency or company.currency_id).id,
                "amount_currency": foreign, "tax_ids": [Command.clear()],
            }))
        move = self.env["account.move"].with_company(company).create({
            "move_type": "entry", "company_id": company.id,
            "journal_id": self.journals[company.id]["general"].id, "date": when or self.cutoff,
            "line_ids": commands,
        })
        move.action_post()
        return move

    def _invoice(self, kind="out_invoice", amount=100, when=None, currency=None, taxes=None):
        sale = kind == "out_invoice"
        move = self.env["account.move"].with_company(self.source).create({
            "move_type": kind, "company_id": self.source.id,
            "journal_id": self.journals[self.source.id]["sale" if sale else "purchase"].id,
            "partner_id": (self.customer if sale else self.vendor).id,
            "invoice_date": when or self.cutoff, "date": when or self.cutoff,
            "invoice_date_due": date(2024, 7, 31),
            "currency_id": (currency or self.source.currency_id).id,
            "invoice_line_ids": [Command.create({
                "name": "Original invoice", "quantity": 1, "price_unit": amount,
                "account_id": self.accounts[self.source.id]["revenue" if sale else "expense"].id,
                "tax_ids": [Command.set(taxes.ids)] if taxes else [Command.clear()],
            })],
        })
        move.action_post()
        return move

    def _run(self, batch=None):
        batch = batch or self._batch()
        batch.action_match()
        batch.action_preview()
        batch.action_apply()
        return batch

@tagged("post_install", "-at_install")
class TestFinancialCutover(FinancialCutoverCase):
    def test_native_invoices_partial_payment_and_unpaid_bill(self):
        invoice = self._invoice()
        bill = self._invoice("in_invoice", 80)
        payment = self._entry(self.source, [("bank", 40), ("receivable", -40, self.customer)])
        ar = invoice.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable")
        (ar | payment.line_ids.filtered(lambda l: l.account_id == ar.account_id)).reconcile()
        before = [(m.id, m.state, m.amount_residual, m.write_date) for m in invoice | bill]
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self.assertEqual(batch.state, "preview")
        self.assertFalse(batch.move_id)
        self.assertFalse(self.env["account.move"].search([("company_id", "=", self.target.id)]))
        self.assertEqual(sorted(batch.line_ids.filtered(lambda l: l.kind == "open_item").mapped("balance")), [-80, 60])
        batch.action_apply()
        self.assertEqual(batch.move_id.state, "posted")
        self.assertEqual(batch.move_id.company_id, self.target)
        self.assertEqual(batch.move_id.date, self.cutoff)
        self.assertEqual(sum(batch.move_id.line_ids.mapped("balance")), 0)
        self.assertEqual(before, [(m.id, m.state, m.amount_residual, m.write_date) for m in invoice | bill])
        opened = batch.line_ids.filtered(lambda l: l.kind == "open_item")
        self.assertTrue(all(line.posted_line_id.date_maturity == date(2024, 7, 31) for line in opened))
        self.assertIn(invoice.name, opened.filtered(lambda l: l.balance > 0).label)
        self.assertFalse(batch.move_id.line_ids.tax_ids)
        self.assertFalse(batch.move_id.line_ids.tax_tag_ids)

    def test_later_reconciled_invoice_is_unpaid_at_cutover(self):
        invoice = self._invoice()
        payment = self._entry(self.source, [("bank", 100), ("receivable", -100, self.customer)], date(2024, 7, 1))
        ar = invoice.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable")
        (ar | payment.line_ids.filtered(lambda l: l.account_id == ar.account_id)).reconcile()
        self.assertEqual(invoice.amount_residual, 0)
        batch = self._run()
        self.assertEqual(batch.line_ids.filtered(lambda l: l.kind == "open_item").balance, 100)

    def test_zero_net_control_account_keeps_separate_unpaid_items(self):
        self._invoice()
        self._entry(self.source, [("bank", 100), ("receivable", -100, self.customer)])
        batch = self._run()
        items = batch.line_ids.filtered(lambda l: l.kind == "open_item")
        self.assertEqual(sorted(items.mapped("balance")), [-100, 100])
        self.assertEqual(sorted(items.posted_line_id.mapped("amount_residual")), [-100, 100])

    def test_prior_year_profit_goes_to_retained_earnings(self):
        self._entry(self.source, [("bank", 50), ("revenue", -50)], date(2023, 12, 31))
        self._entry(self.source, [("bank", 20), ("revenue", -20)])
        batch = self._run()
        self.assertEqual(batch.line_ids.filtered(lambda l: l.kind == "retained").balance, -50)
        self.assertEqual(batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["revenue"]).balance, -20)
        self.assertEqual(batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["bank"]).balance, 70)

    def test_foreign_currency_open_items_and_bank_balance(self):
        foreign = self.env.ref("base.EUR")
        if foreign == self.source.currency_id:
            foreign = self.env.ref("base.USD")
        foreign.active = True
        self._entry(self.source, [("receivable", 120, self.customer, 100), ("revenue", -120, self.env["res.partner"], -100)], currency=foreign)
        payment = self._entry(self.source, [("bank", 48, self.env["res.partner"], 40), ("receivable", -48, self.customer, -40)], currency=foreign)
        ar = self.env["account.move.line"].search([("company_id", "=", self.source.id), ("account_id", "=", self.accounts[self.source.id]["receivable"].id)])
        ar.reconcile()
        batch = self._run()
        item = batch.line_ids.filtered(lambda l: l.kind == "open_item")
        self.assertEqual((item.balance, item.amount_currency), (72, 60))
        self.assertEqual(item.posted_line_id.currency_id, foreign)
        self.assertEqual(item.posted_line_id.amount_residual_currency, 60)
        bank = batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["bank"])
        self.assertEqual((bank.balance, bank.amount_currency), (48, 40))

    def test_inventory_exclusion_uses_stock_clearing_not_double_opening(self):
        self._entry(self.source, [("inventory", 100), ("bank", 50), ("equity", -150)])
        batch = self._batch()
        batch.action_match()
        batch.mapping_ids.filtered(lambda m: m.source_account_id == self.accounts[self.source.id]["inventory"]).handled_by_stock = True
        with self.assertRaisesRegex(UserError, "Stock Migration Clearing"):
            batch.action_preview()
        batch.offset_account_id = self.accounts[self.target.id]["clearing"]
        batch.action_preview()
        self.assertEqual(batch.line_ids.filtered(lambda l: l.kind == "stock_excluded").balance, 100)
        batch.action_apply()
        self.assertFalse(batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["inventory"]))
        self.assertEqual(batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["clearing"]).balance, 100)

    def test_unpaid_cash_basis_tax_requires_separate_tax_migration(self):
        self.accounts[self.source.id]["inventory"].reconcile = True
        tax_account = self.env["account.account"].with_company(self.source).create({
            "name": "Tax payable", "code": "2100", "account_type": "liability_current",
            "company_ids": [Command.set(self.source.ids)],
        })
        tax = self.env["account.tax"].with_company(self.source).create({
            "name": "Cash-basis VAT", "amount": 10, "amount_type": "percent", "type_tax_use": "sale",
            "company_id": self.source.id, "tax_exigibility": "on_payment",
            "tax_group_id": self.env["account.tax.group"].create({"name": "VAT", "company_id": self.source.id}).id,
            "cash_basis_transition_account_id": self.accounts[self.source.id]["inventory"].id,
            "invoice_repartition_line_ids": [
                Command.create({"repartition_type": "base"}),
                Command.create({"repartition_type": "tax", "account_id": tax_account.id}),
            ],
            "refund_repartition_line_ids": [
                Command.create({"repartition_type": "base"}),
                Command.create({"repartition_type": "tax", "account_id": tax_account.id}),
            ],
        })
        self._invoice(taxes=tax)
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "cash-basis tax invoices"):
            batch.action_preview()
        self.assertFalse(batch.move_id)

    def test_foreign_only_residual_with_zero_base_balance(self):
        foreign = self.env.ref("base.EUR")
        if foreign == self.source.currency_id:
            foreign = self.env.ref("base.USD")
        foreign.active = True
        self._entry(self.source, [("receivable", 0, self.customer, 10),
            ("revenue", 0, self.env["res.partner"], -10)], currency=foreign)
        batch = self._run()
        item = batch.line_ids.filtered(lambda l: l.kind == "open_item")
        self.assertEqual(item.posted_line_id.amount_residual, 0)
        self.assertEqual(item.posted_line_id.amount_residual_currency, 10)

    def test_missing_account_requires_choice(self):
        self._invoice()
        self.accounts[self.target.id]["revenue"].code = "NEW4000"
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "Select a destination account"):
            batch.action_preview()
        batch.mapping_ids.filtered(lambda m: m.source_account_id == self.accounts[self.source.id]["revenue"]).target_account_id = self.accounts[self.target.id]["revenue"]
        batch.action_preview()
        batch.action_apply()
        self.assertEqual(batch.state, "done")

    def test_incompatible_account_type_is_rejected(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.mapping_ids.filtered(lambda m: m.source_account_id == self.accounts[self.source.id]["revenue"]).target_account_id = self.accounts[self.target.id]["equity"]
        with self.assertRaisesRegex(UserError, "account types must match"):
            batch.action_preview()

    def test_company_contact_created_without_bad_accounting_settings(self):
        owned = self.env["res.partner"].with_company(self.source).create({
            "name": "Old company-only customer", "company_id": self.source.id,
            "property_account_receivable_id": self.accounts[self.source.id]["receivable"].id,
        })
        self._entry(self.source, [("receivable", 100, owned), ("revenue", -100)])
        batch = self._batch()
        batch.action_match()
        self.assertTrue(batch.partner_mapping_ids.create_contact)
        batch.action_preview()
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        batch.action_apply()
        target_contact = batch.partner_mapping_ids.target_partner_id
        self.assertEqual(target_contact.company_id, self.target)
        self.assertEqual(target_contact.name, owned.name)
        self.assertNotEqual(target_contact, owned)
        self.assertFalse(target_contact.bank_ids)
        self.assertNotEqual(target_contact.with_company(self.target).property_account_receivable_id, self.accounts[self.source.id]["receivable"])

    def test_ambiguous_contact_requires_explicit_choice(self):
        owned = self.env["res.partner"].create({"name": "Duplicate identity", "company_id": self.source.id})
        matches = self.env["res.partner"].create([
            {"name": owned.name, "company_id": self.target.id},
            {"name": owned.name, "company_id": self.target.id},
        ])
        self._entry(self.source, [("receivable", 100, owned), ("revenue", -100)])
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "ambiguous destination contact"):
            batch.action_preview()
        batch.partner_mapping_ids.target_partner_id = matches[0]
        batch.action_preview()
        batch.action_apply()
        self.assertEqual(batch.partner_mapping_ids.target_partner_id, matches[0])

    def test_source_change_invalidates_preview_at_confirmation(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self._entry(self.source, [("bank", 10), ("equity", -10)])
        # Match the new accounts without changing the reviewed record itself.
        with self.assertRaises(UserError):
            batch.action_apply()
        self.assertFalse(batch.move_id)
        batch.action_match()
        batch.action_preview()
        batch.action_apply()
        self.assertEqual(batch.state, "done")

    def test_same_accounts_changed_source_rejects_stale_hash(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self._invoice(amount=50)
        with self.assertRaisesRegex(UserError, "changed.*fresh Preview"):
            batch.action_apply()
        self.assertFalse(batch.move_id)

    def test_destination_change_rejects_stale_preview(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self.accounts[self.target.id]["receivable"].name = "Reviewed new name"
        with self.assertRaisesRegex(UserError, "changed.*fresh Preview"):
            batch.action_apply()
        self.assertFalse(batch.move_id)

    def test_destination_existing_entry_is_blocked(self):
        self._invoice()
        self._entry(self.target, [("bank", 1), ("equity", -1)])
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "destination already has posted"):
            batch.action_preview()

    def test_duplicate_source_and_repeat_confirmation_are_blocked(self):
        self._invoice()
        second = self._batch()
        batch = self._run()
        with self.assertRaisesRegex(UserError, "fresh Preview"):
            batch.action_apply()
        second.action_match()
        with self.assertRaisesRegex(UserError, "already has a completed"):
            second.action_preview()
        self.assertEqual(self.env["account.move"].search_count([("company_id", "=", self.target.id)]), 1)

    def test_audit_and_opening_are_immutable_but_later_payments_reconcile(self):
        self._invoice()
        batch = self._run()
        item = batch.line_ids.filtered(lambda l: l.kind == "open_item").posted_line_id
        for exception, operation in (
            (UserError, lambda: batch.write({"cutover_date": date(2024, 6, 29)})),
            (AccessError, lambda: batch.line_ids[0].write({"balance": 1})),
            (UserError, lambda: batch.mapping_ids[0].write({"handled_by_stock": True})),
            (UserError, lambda: batch.move_id.button_draft()),
            (UserError, lambda: item.write({"name": "Changed identity"})),
            (UserError, lambda: item.write({"company_id": self.source.id})),
            (AccessError, lambda: batch.move_id.write({"financial_cutover_id": False})),
        ):
            with self.assertRaises(exception), self.env.cr.savepoint():
                operation()
        payment = self._entry(self.target, [("bank", 100), ("receivable", -100, self.customer)], date(2024, 7, 1))
        (item | payment.line_ids.filtered(lambda l: l.account_id == item.account_id)).reconcile()
        self.assertEqual(item.amount_residual, 0)
        self.assertEqual(batch.line_ids.filtered(lambda l: l.kind == "open_item").balance, 100)

    def test_forged_audit_fields_and_preview_rows_are_rejected(self):
        batch = self._batch()
        with self.assertRaises(AccessError):
            batch.write({"state": "done"})
        with self.assertRaises(AccessError):
            batch.copy({"snapshot_hash": "forged"})
        with self.assertRaises(AccessError):
            self.env["company.financial.cutover.line"].create({"cutover_id": batch.id, "balance": 999})

    def test_both_companies_and_settings_admin_are_required(self):
        self._invoice()
        batch = self._batch()
        accounting_only = new_test_user(self.env(context={**self.env.context, "no_reset_password": True}), login="financial-accounting-only",
            groups="account.group_account_manager", company_id=self.source.id,
            company_ids=[Command.set((self.source | self.target).ids)])
        with self.assertRaises(AccessError):
            batch.with_user(accounting_only).action_match()
        with self.assertRaises(AccessError):
            batch.with_context(allowed_company_ids=self.source.ids).action_match()

    def test_post_failure_rolls_back_contacts_and_entry(self):
        owned = self.env["res.partner"].create({"name": "Rollback Customer", "company_id": self.source.id})
        self._entry(self.source, [("receivable", 100, owned), ("revenue", -100)])
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        with patch.object(type(self.env["account.move"]), "action_post", side_effect=UserError("Posting blocked")):
            with self.assertRaisesRegex(UserError, "Posting blocked"):
                batch.action_apply()
        self.assertEqual(batch.state, "preview")
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        self.assertFalse(self.env["res.partner"].search([("name", "=", owned.name), ("company_id", "=", self.target.id)]))
        self.assertFalse(self.env["account.move"].search([("company_id", "=", self.target.id)]))

    def test_form_and_views_support_saved_draft(self):
        with Form(self.env["company.financial.cutover"]) as form:
            form.source_company_id = self.source
            form.target_company_id = self.target
            form.cutover_date = self.cutoff
            form.journal_id = self.journals[self.target.id]["general"]
            form.retained_earnings_account_id = self.accounts[self.target.id]["retained"]
        self.assertEqual(form.record.state, "draft")

    def _owned_customer(self, name="Contact to move", **values):
        contact = self.env["res.partner"].create({"name": name, "company_id": self.source.id, **values})
        self._entry(self.source, [("receivable", 100, contact), ("revenue", -100)])
        return contact

    def test_archived_matching_contact_is_not_duplicated(self):
        source = self._owned_customer(ref="ARCH-001")
        archived = self.env["res.partner"].create({
            "name": source.name, "ref": source.ref, "company_id": self.target.id, "active": False})
        batch = self._batch()
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        self.assertEqual(batch.partner_mapping_ids.target_partner_id, archived)
        self.assertFalse(batch.partner_mapping_ids.create_contact)
        with self.assertRaisesRegex(UserError, "archived"):
            batch.action_preview()
        archived.active = True
        self._run(batch)
        self.assertEqual(batch.partner_mapping_ids.target_partner_id, archived)

    def test_contact_added_after_preview_stops_new_contact_creation(self):
        source = self._owned_customer()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        existing = self.env["res.partner"].create({
            "name": source.name.upper(), "company_id": self.target.id})
        with self.assertRaisesRegex(UserError, "existing or archived"):
            batch.action_apply()
        self.assertFalse(batch.move_id)
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        batch.partner_mapping_ids.target_partner_id = existing
        self._run(batch)
        self.assertEqual(batch.partner_mapping_ids.target_partner_id, existing)

    def test_name_only_match_requires_explicit_choice(self):
        self._owned_customer("  Acme   Trading  ")
        existing = self.env["res.partner"].create({"name": "ACME Trading", "company_id": self.target.id})
        batch = self._batch()
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        self.assertFalse(batch.partner_mapping_ids.create_contact)
        with self.assertRaisesRegex(UserError, "Name-only matches require an explicit choice"):
            batch.action_preview()
        batch.partner_mapping_ids.target_partner_id = existing
        self._run(batch)
        self.assertEqual(batch.partner_mapping_ids.target_partner_id, existing)

    def test_conflicting_identity_requires_manual_contact_choice(self):
        self._owned_customer(ref="OLD-001")
        self.env["res.partner"].create({"name": "Contact to move", "ref": "OTHER-002", "company_id": self.target.id})
        batch = self._batch()
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        self.assertFalse(batch.partner_mapping_ids.create_contact)
        with self.assertRaisesRegex(UserError, "missing or ambiguous"):
            batch.action_preview()

    def test_two_new_source_contacts_with_same_identity_are_blocked(self):
        self._owned_customer("Possible Duplicate")
        self._owned_customer("possible duplicate")
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "Two source contacts"):
            batch.action_preview()
        self.assertFalse(self.env["res.partner"].search([
            ("company_id", "=", self.target.id), ("name", "ilike", "Possible Duplicate")]))

    def test_destination_draft_added_after_preview_blocks_move(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self.env["account.move"].with_company(self.target).create({
            "company_id": self.target.id, "journal_id": self.journals[self.target.id]["general"].id,
            "date": self.cutoff})
        with self.assertRaisesRegex(UserError, "draft entries: 1"):
            batch.action_apply()
        self.assertFalse(batch.move_id)

    def test_payment_without_journal_entry_blocks_move(self):
        self._invoice()
        bank = self.env["account.journal"].with_company(self.target).create({
            "name": "New company bank", "code": "BNK", "type": "bank", "company_id": self.target.id,
            "default_account_id": self.accounts[self.target.id]["bank"].id})
        bank.inbound_payment_method_line_ids.payment_account_id = self.accounts[self.target.id]["clearing"]
        payment = self.env["account.payment"].with_company(self.target).create({
            "company_id": self.target.id, "payment_type": "inbound", "partner_type": "customer",
            "partner_id": self.customer.id, "amount": 10, "date": self.cutoff, "journal_id": bank.id,
            "payment_method_line_id": bank.inbound_payment_method_line_ids[:1].id})
        self.assertFalse(payment.move_id)
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "payments: 1"):
            batch.action_preview()

    def test_completed_source_is_blocked_outside_current_company_selection(self):
        self._invoice()
        self._run()
        third = self.env["res.company"].create({"name": "Another replacement", "currency_id": self.source.currency_id.id})
        env = self.env(context={**self.env.context, "allowed_company_ids": [self.source.id, third.id]})
        batch = env["company.financial.cutover"].create({
            "source_company_id": self.source.id, "target_company_id": third.id, "cutover_date": self.cutoff})
        with self.assertRaisesRegex(UserError, "already has a completed"):
            batch._validate()

    def test_persistent_completion_marker_blocks_new_cutover(self):
        self._invoice()
        batch = self._batch()
        self.env["ir.config_parameter"].sudo().set_param(batch._completion_key(), "previous opening")
        with self.assertRaisesRegex(UserError, "already has a completed"):
            batch.action_preview()

    def test_hidden_or_archived_source_branch_is_not_silently_omitted(self):
        self._invoice()
        self.env["res.company"].create({"name": "Archived source branch", "parent_id": self.source.id, "active": False})
        batch = self._batch()
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        with self.assertRaisesRegex(UserError, "branch balances"):
            batch.action_preview()

    def test_destination_branch_added_after_preview_is_blocked(self):
        self._invoice()
        batch = self._batch()
        batch.action_match()
        batch.action_preview()
        self.env["res.company"].create({"name": "Destination branch", "parent_id": self.target.id})
        with self.assertRaisesRegex(UserError, "branch balances"):
            batch.action_apply()
        self.assertFalse(batch.move_id)

    def test_existing_settings_are_suggested_without_creating_accounts(self):
        self._invoice()
        before = self.env["account.account"].search_count([])
        batch = self.env["company.financial.cutover"].create({
            "source_company_id": self.source.id, "target_company_id": self.target.id, "cutover_date": self.cutoff})
        self.assertEqual(batch.journal_id, self.journals[self.target.id]["general"])
        self.assertEqual(batch.retained_earnings_account_id, self.accounts[self.target.id]["retained"])
        batch.action_match()
        self.assertEqual(batch.check_status, "ready")
        self.assertIn("existing", batch.check_report)
        self.assertEqual(self.env["account.account"].search_count([]), before)
        batch.partner_mapping_ids.create_contact = True
        self.assertEqual(batch.check_status, "unchecked")

    def test_reference_match_can_reuse_a_differently_named_contact(self):
        self._owned_customer("Previous name", ref="CUST-001")
        existing = self.env["res.partner"].create({
            "name": "Updated legal name", "ref": "cust001", "company_id": self.target.id})
        batch = self._run()
        self.assertEqual(batch.partner_mapping_ids.target_partner_id, existing)

    def test_name_only_match_to_a_different_shared_contact_requires_choice(self):
        self._owned_customer("John Smith")
        self.env["res.partner"].create({"name": "John Smith"})
        batch = self._batch()
        batch.action_match()
        self.assertFalse(batch.partner_mapping_ids.target_partner_id)
        self.assertFalse(batch.partner_mapping_ids.create_contact)
        with self.assertRaisesRegex(UserError, "Name-only matches require an explicit choice"):
            batch.action_preview()

    def test_misc_suggestion_with_three_general_journals_and_ambiguous_equity(self):
        self._invoice()
        self.journals[self.target.id]["general"].code = "MISC"
        self.env["account.journal"].with_company(self.target).create([
            {"name": code, "code": code, "type": "general", "company_id": self.target.id}
            for code in ("CABA", "EXCH")])
        self.env["account.account"].with_company(self.target).create({
            "name": "Another earnings account", "code": "3011", "account_type": "equity_unaffected",
            "company_ids": [Command.set(self.target.ids)]})
        batch = self.env["company.financial.cutover"].create({
            "source_company_id": self.source.id, "target_company_id": self.target.id, "cutover_date": self.cutoff})
        self.assertEqual(batch.journal_id.code, "MISC")
        self.assertFalse(batch.retained_earnings_account_id)
        batch.retained_earnings_account_id = self.accounts[self.target.id]["retained"]
        self._run(batch)

    def test_cash_basis_journal_cannot_be_used_for_an_opening(self):
        self._invoice()
        batch = self._batch()
        batch.journal_id = self.env["account.journal"].with_company(self.target).create({
            "name": "Cash basis", "code": "CABA", "type": "general", "company_id": self.target.id})
        batch.action_match()
        with self.assertRaisesRegex(UserError, "Miscellaneous journal"):
            batch.action_preview()

    def test_saved_stock_clearing_mismatch_added_after_preview_is_blocked(self):
        self._entry(self.source, [("inventory", 100), ("equity", -100)])
        batch = self._batch()
        batch.action_match()
        batch.mapping_ids.filtered(lambda m: m.source_account_id == self.accounts[self.source.id]["inventory"]).handled_by_stock = True
        batch.offset_account_id = self.accounts[self.target.id]["clearing"]
        batch.action_preview()
        parameter = self.env["ir.config_parameter"].sudo().create({
            "key": "company_stock_fifo_migration.clearing_account.%s" % self.target.id,
            "value": str(self.accounts[self.target.id]["inventory"].id)})
        with self.assertRaisesRegex(UserError, "differs from the stock mover's saved account"):
            batch.action_apply()
        self.assertFalse(batch.move_id)
        parameter.value = str(batch.offset_account_id.id)
        batch.action_apply()
        self.assertEqual(batch.state, "done")

    def test_opening_runs_later_create_overrides(self):
        self._invoice()
        model_type = type(self.env["account.move"])
        original_create = model_type.create
        visited = []

        def later_override(records, values):
            if isinstance(values, dict) and values.get("financial_cutover_id"):
                visited.append(values["financial_cutover_id"])
                values = {**values, "ref": values["ref"] + " | downstream hook"}
            return original_create(records, values)

        with patch.object(model_type, "create", later_override):
            batch = self._run()
        self.assertEqual(visited, [batch.id])
        self.assertIn("downstream hook", batch.move_id.ref)

    def test_rpc_context_cannot_forge_internal_opening_creation(self):
        batch = self._batch()
        for flag in (True, "internal", [True, batch.id], ("internal", batch.id)):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self.env["account.move"].with_context(_financial_cutover_create=flag).create({
                    "company_id": self.target.id, "journal_id": self.journals[self.target.id]["general"].id,
                    "financial_cutover_id": batch.id, "date": self.cutoff})

    def _outstanding_payment(self):
        invoice = self._invoice(amount=300)
        clearing = self.accounts[self.source.id]["clearing"]
        clearing.reconcile = True
        journal = self.env["account.journal"].with_company(self.source).create({
            "name": "Old bank", "code": "BANK", "type": "bank", "company_id": self.source.id,
            "default_account_id": self.accounts[self.source.id]["bank"].id,
            "suspense_account_id": clearing.id})
        journal.inbound_payment_method_line_ids.payment_account_id = clearing
        payment = self.env["account.payment"].with_company(self.source).create({
            "company_id": self.source.id, "journal_id": journal.id, "partner_id": self.customer.id,
            "payment_type": "inbound", "partner_type": "customer", "amount": 300, "date": self.cutoff,
            "payment_method_line_id": journal.inbound_payment_method_line_ids[:1].id})
        payment.action_post()
        receivables = (invoice | payment.move_id).line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        receivables.reconcile()
        return journal, payment.move_id.line_ids.filtered(lambda l: l.account_id == clearing)

    def _clear_payment(self, journal, outstanding, when):
        statement = self.env["account.bank.statement.line"].with_company(self.source).create({
            "journal_id": journal.id, "amount": 300, "date": when,
            "payment_ref": "Customer receipt cleared", "partner_id": self.customer.id,
            "counterpart_account_id": outstanding.account_id.id})
        counterpart = statement.move_id.line_ids.filtered(lambda l: l.account_id == outstanding.account_id)
        (outstanding | counterpart).reconcile()

    def test_unsettled_native_customer_receipt_blocks_cutover(self):
        self._outstanding_payment()
        batch = self._batch()
        batch.action_match()
        self.assertEqual(batch.check_status, "blocked")
        with self.assertRaisesRegex(UserError, "Finish bank reconciliation"):
            batch.action_preview()
        self.assertFalse(batch.move_id)

    def test_receipt_cleared_by_cutover_moves_only_bank_balance(self):
        journal, outstanding = self._outstanding_payment()
        self._clear_payment(journal, outstanding, self.cutoff)
        batch = self._run()
        bank = batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["bank"])
        self.assertEqual(bank.balance, 300)
        self.assertFalse(batch.move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[self.target.id]["clearing"]))

    def test_receipt_cleared_after_cutover_is_still_blocked_at_cutover(self):
        journal, outstanding = self._outstanding_payment()
        self._clear_payment(journal, outstanding, date(2024, 7, 1))
        self.assertEqual(outstanding.amount_residual, 0)
        batch = self._batch()
        batch.action_match()
        with self.assertRaisesRegex(UserError, "Finish bank reconciliation"):
            batch.action_preview()


@tagged("post_install", "-at_install")
class TestSnapshotFreshness(TransactionCase):
    """Independent database transactions exercise PostgreSQL snapshot behavior."""

    def _check(self, cursor, snapshot):
        env = api.Environment(cursor, self.env.uid, self.env.context)
        env["company.financial.cutover"]._assert_fresh_snapshot(snapshot)

    def _snapshot(self, cursor):
        cursor.execute("SELECT txid_current_snapshot()::text")
        return cursor.fetchone()[0]

    def test_new_commit_after_first_read_requires_full_retry(self):
        with self.registry.cursor() as original:
            snapshot = self._snapshot(original)
            with self.registry.cursor() as concurrent:
                concurrent.execute("SELECT txid_current()")
                concurrent.commit()
            with self.assertRaises(ConcurrencyError):
                self._check(original, snapshot)

    def test_already_running_transaction_committing_requires_full_retry(self):
        with self.registry.cursor() as concurrent, self.registry.cursor() as original:
            concurrent.execute("SELECT txid_current()")
            snapshot = self._snapshot(original)
            concurrent.commit()
            with self.assertRaises(ConcurrencyError):
                self._check(original, snapshot)

    def test_uncommitted_and_own_subtransactions_do_not_require_retry(self):
        with self.registry.cursor() as concurrent, self.registry.cursor() as original:
            concurrent.execute("SELECT txid_current()")
            snapshot = self._snapshot(original)
            with original.savepoint():
                original.execute("SELECT txid_current()")
                self._check(original, snapshot)
            concurrent.rollback()
            self._check(original, snapshot)

    def test_commits_before_first_read_are_already_visible(self):
        with self.registry.cursor() as concurrent:
            concurrent.execute("SELECT txid_current()")
            concurrent.commit()
        with self.registry.cursor() as original:
            self._check(original, self._snapshot(original))

    def test_real_odoo_retry_loop_refreshes_snapshot_and_rolls_back_first_attempt(self):
        attempts = []
        key = "company_financial_cutover.test.retry.%s" % uuid4().hex
        with self.registry.cursor() as original:
            env = api.Environment(original, self.env.uid, self.env.context)

            def request():
                attempts.append(len(attempts) + 1)
                snapshot = self._snapshot(original)
                if len(attempts) == 1:
                    original.execute("INSERT INTO ir_config_parameter (key, value) VALUES (%s, 'first attempt')", [key])
                    with self.registry.cursor() as concurrent:
                        concurrent.execute("SELECT txid_current()")
                        concurrent.commit()
                env["company.financial.cutover"]._assert_fresh_snapshot(snapshot)
                original.execute("SELECT count(*) FROM ir_config_parameter WHERE key = %s", [key])
                self.assertEqual(original.fetchone()[0], 0)
                return "retried with fresh data"

            with patch("odoo.service.model.time.sleep"):
                result = retrying(request, env)
        self.assertEqual(result, "retried with fresh data")
        self.assertEqual(attempts, [1, 2])
