"""Odoo-shell assertions for an isolated real uninstall/code removal/reinstall."""
import hashlib
import json
import os
from datetime import date
from pathlib import Path

from odoo import Command, api
from odoo.exceptions import ConcurrencyError, UserError

assert env.cr.dbname.startswith("fifo_survival_tests_")
mode = os.environ["FIFO_SURVIVAL_MODE"]
fixture = Path(os.environ["FIFO_SURVIVAL_FIXTURE"])

if mode == "setup":
    source, target = env["res.company"].create([
        {"name": name, "currency_id": env.company.currency_id.id,
         "account_fiscal_country_id": env.ref("base.us").id}
        for name in ("Survival old company", "Survival new company")])
    demo = env(context={**env.context, "allowed_company_ids": [source.id, target.id]})
    accounts, journals = {}, {}
    for company in (source, target):
        accounts[company.id] = {}
        for name, code, kind in (("receivable", "1000", "asset_receivable"),
                ("payable", "2000", "liability_payable"), ("retained", "3010", "equity_unaffected"),
                ("revenue", "4000", "income"), ("expense", "5000", "expense")):
            accounts[company.id][name] = demo["account.account"].with_company(company).create({
                "name": name, "code": code, "account_type": kind,
                "reconcile": kind in {"asset_receivable", "liability_payable"},
                "company_ids": [Command.set(company.ids)]})
        journals[company.id] = {}
        for name, code, kind in (("general", "MISC", "general"), ("sale", "INV", "sale")):
            journals[company.id][name] = demo["account.journal"].with_company(company).create({
                "name": name, "code": code, "type": kind, "company_id": company.id,
                "default_account_id": accounts[company.id]["revenue"].id})
    partner = demo["res.partner"].create({"name": "Survival shared contact"})
    for company in (source, target):
        partner.with_company(company).write({
            "property_account_receivable_id": accounts[company.id]["receivable"].id,
            "property_account_payable_id": accounts[company.id]["payable"].id})
    invoice = demo["account.move"].with_company(source).create({
        "move_type": "out_invoice", "company_id": source.id,
        "journal_id": journals[source.id]["sale"].id, "partner_id": partner.id,
        "invoice_date": date.today(), "date": date.today(),
        "invoice_line_ids": [Command.create({"name": "Unpaid survival invoice",
            "quantity": 1, "price_unit": 100, "account_id": accounts[source.id]["revenue"].id,
            "tax_ids": [Command.clear()]})]})
    invoice.action_post()
    product = demo["product.product"].create({"name": "Survival service", "type": "service",
        "supplier_taxes_id": [Command.clear()]})
    original = demo["purchase.order"].with_company(source).create({
        "company_id": source.id, "partner_id": partner.id, "partner_ref": "SURVIVAL-001",
        "order_line": [Command.create({"product_id": product.id, "name": "Keep this order",
            "product_qty": 5, "price_unit": 10, "product_uom_id": product.uom_id.id,
            "date_planned": str(date.today()) + " 10:00:00", "tax_ids": [Command.clear()]})]})
    original.button_confirm()
    financial = demo["company.financial.cutover"].create({
        "source_company_id": source.id, "target_company_id": target.id,
        "cutover_date": date.today(), "journal_id": journals[target.id]["general"].id,
        "retained_earnings_account_id": accounts[target.id]["retained"].id,
        "include_purchase_history": False})
    financial.action_match()
    financial.action_preview()
    financial.action_apply()
    purchases = demo["company.financial.cutover"].create({
        "source_company_id": source.id, "target_company_id": target.id,
        "include_financial": False, "include_purchase_history": True})
    purchases.action_match()
    purchases.action_preview()
    purchases.action_apply()
    original.button_cancel()
    history = purchases.purchase_history_ids
    history._choose_vendor(partner)
    history.action_prepare_draft()
    archives = financial.archive_attachment_id | financial.report_attachment_id | purchases.archive_attachment_id | purchases.report_attachment_id
    data = {"source": source.id, "target": target.id, "invoice": invoice.id,
        "opening": financial.move_id.id, "original": original.id, "replacement": history.target_order_id.id,
        "chosen_vendor": partner.id,
        "lines": len(financial.line_ids), "history_snapshot": history.snapshot,
        "financial_key": financial.archive_key, "purchase_key": purchases.archive_key,
        "markers": {p.key: p.value for p in demo["ir.config_parameter"].sudo().search([
            ("key", "like", "company_financial_cutover.%")])},
        "archives": {str(a.id): hashlib.sha256(a.raw).hexdigest() for a in archives}}
    assert len(archives) == 4 and financial.move_id.state == "posted" and history.target_order_id.state == "draft"
    env.cr.commit()
    fixture.write_text(json.dumps(data, sort_keys=True))
    print("SURVIVAL: completed financial opening, purchase-only history and replacement draft created.")
elif mode == "uninstall":
    module = env["ir.module.module"].search([("name", "=", "company_financial_cutover")])
    assert module.state == "installed"
    module.button_uninstall()
    env.cr.commit()
    print("SURVIVAL: native Odoo uninstall requested.")
elif mode == "change_opening":
    data = json.loads(fixture.read_text())
    demo = env(context={**env.context, "allowed_company_ids": [data["source"], data["target"]]})
    opening = demo["account.move"].browse(data["opening"])
    assert "financial_cutover_id" not in opening._fields
    opening.button_draft()
    opening.write({"line_ids": [Command.update(line.id, {
        "debit": 120 if line.balance > 0 else 0, "credit": 120 if line.balance < 0 else 0,
        "amount_currency": 120 if line.balance > 0 else -120}) for line in opening.line_ids]})
    opening.action_post()
    env.cr.commit()
    print("SURVIVAL: native opening edited and reposted while addon is uninstalled and absent.")
elif mode == "approval_race":
    data = json.loads(fixture.read_text())
    context = {**env.context, "allowed_company_ids": [data["source"], data["target"]]}
    demo = env(context=context)
    replacement = demo["purchase.order"].browse(data["replacement"])
    replacement.button_cancel()
    env.cr.commit()
    try:
        with env.registry.cursor() as first, env.registry.cursor() as second:
            old_env = api.Environment(first, env.uid, context)
            new_env = api.Environment(second, env.uid, context)
            # Both requests start with a cancelled original and replacement.
            second.execute("SELECT txid_current_snapshot()")
            old = old_env["purchase.order"].browse(data["original"])
            old.button_draft()
            old.button_confirm()
            assert old.state == "purchase"
            try:
                new_env["purchase.order"].browse(data["replacement"]).button_approve()
            except ConcurrencyError:
                pass
            else:
                raise AssertionError("Concurrent replacement approval must retry instead of confirming both orders")
            first.rollback()
            second.rollback()
    finally:
        demo.invalidate_all()
        replacement.button_draft()
        env.cr.commit()
    assert demo["purchase.order"].browse(data["original"]).state == "cancel" and replacement.state == "draft"
    print("SURVIVAL: native concurrent original/replacement approvals serialize; fixture restored unchanged.")
else:
    data = json.loads(fixture.read_text())
    demo = env(context={**env.context, "allowed_company_ids": [data["source"], data["target"]]})
    opening = demo["account.move"].browse(data["opening"]).exists()
    original = demo["purchase.order"].browse(data["original"]).exists()
    replacement = demo["purchase.order"].browse(data["replacement"]).exists()
    invoice = demo["account.move"].browse(data["invoice"]).exists()
    assert opening.state == "posted" and invoice.state == "posted" and invoice.amount_residual == 100
    assert sum(opening.line_ids.mapped("balance")) == 0
    assert sorted(opening.line_ids.filtered(lambda row: row.account_id.account_type == "asset_receivable").mapped("amount_residual")) == [100]
    assert original.state == "cancel" and replacement.state == "draft"
    assert replacement.order_line.product_qty == 5 and replacement.amount_total == 50
    assert demo["account.move"].search_count([("company_id", "=", data["target"])]) == 1
    assert demo["purchase.order"].search_count([("company_id", "=", data["target"])]) == 1
    for key, value in data["markers"].items():
        assert demo["ir.config_parameter"].sudo().get_param(key) == value
    for attachment_id, digest in data["archives"].items():
        attachment = demo["ir.attachment"].sudo().browse(int(attachment_id)).exists()
        assert attachment and hashlib.sha256(attachment.raw).hexdigest() == digest
        assert not attachment.public and not attachment.res_model and not attachment.res_id
    if mode == "absent":
        assert "company.financial.cutover" not in demo
        assert "financial_cutover_id" not in opening._fields
        assert "financial_purchase_history_id" not in replacement._fields
        assert demo["ir.module.module"].search([("name", "=", "company_financial_cutover")]).state == "uninstalled"
        print("SURVIVAL: with addon uninstalled AND code absent, native records, archives and duplicate markers survived unchanged.")
    elif mode == "restored":
        batches = demo["company.financial.cutover"].search([])
        assert len(batches) == 2 and set(batches.mapped("state")) == {"done"}
        financial = batches.filtered(lambda b: b.archive_key == data["financial_key"])
        purchases = batches.filtered(lambda b: b.archive_key == data["purchase_key"])
        assert financial.move_id == opening and len(financial.line_ids) == data["lines"]
        assert opening.financial_cutover_id == financial
        history = purchases.purchase_history_ids
        assert len(history) == 1 and history.snapshot == data["history_snapshot"]
        assert history.target_order_id == replacement and replacement.financial_purchase_history_id == history
        assert history.replacement_vendor_id.id == data["chosen_vendor"]
        assert len(demo["company.financial.purchase.history"].search([])) == 1
        demo["company.financial.cutover"]._restore_archives()
        assert demo["company.financial.cutover"].search_count([]) == 2
        assert demo["company.financial.purchase.history"].search_count([]) == 1
        repeat = demo["company.financial.cutover"].create({
            "source_company_id": data["source"], "target_company_id": data["target"],
            "include_financial": False, "include_purchase_history": True})
        try:
            repeat.action_preview()
        except UserError as exc:
            assert "no new purchase orders" in str(exc)
        else:
            raise AssertionError("Already copied purchase must not be copied again")
        print("SURVIVAL: reinstall restored audit/history and native links; repeated restore and purchase retry created no duplicates.")
    else:
        raise AssertionError("Unknown lifecycle step")
