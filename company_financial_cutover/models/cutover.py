import hashlib
import json
from collections import defaultdict

from psycopg2.errors import LockNotAvailable

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError


OPEN_ITEM_TYPES = {"asset_receivable", "liability_payable"}
PROFIT_TYPES = {"income", "income_other", "expense", "expense_depreciation", "expense_direct_cost"}
MAX_SOURCE_LINES = 100000
MAX_OPENING_LINES = 10000


class FinancialCutover(models.Model):
    _name = "company.financial.cutover"
    _description = "Company Financial Cutover"
    _order = "id desc"

    name = fields.Char(default="New", readonly=True, copy=False)
    source_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    target_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    currency_id = fields.Many2one(related="source_company_id.currency_id")
    cutover_date = fields.Date(required=True, default=fields.Date.context_today)
    journal_id = fields.Many2one("account.journal", string="Destination Opening Journal")
    retained_earnings_account_id = fields.Many2one("account.account", string="Destination Retained Earnings")
    offset_account_id = fields.Many2one("account.account", string="Destination Stock Clearing Account",
        help="Balances inventory excluded for the separate stock mover. Use its destination Stock Migration Clearing account.")
    mapping_ids = fields.One2many("company.financial.account.mapping", "cutover_id", copy=False)
    partner_mapping_ids = fields.One2many("company.financial.partner.mapping", "cutover_id", copy=False)
    line_ids = fields.One2many("company.financial.cutover.line", "cutover_id", readonly=True, copy=False)
    state = fields.Selection([("draft", "Draft"), ("preview", "Reviewed Preview"), ("done", "Completed")],
        default="draft", required=True, readonly=True, copy=False)
    snapshot_hash = fields.Char(readonly=True, copy=False)
    move_id = fields.Many2one("account.move", readonly=True, copy=False, ondelete="restrict")
    completed_at = fields.Datetime(readonly=True, copy=False)
    completed_by = fields.Many2one("res.users", readonly=True, copy=False)
    summary = fields.Text(readonly=True, copy=False)

    def _operator(self):
        if not (self.env.user.has_group("base.group_system")
                and self.env.user.has_group("account.group_account_manager")):
            raise AccessError("A Settings administrator with Accounting administrator access must run this cutover.")
        for batch in self:
            if not (batch.source_company_id | batch.target_company_id) <= self.env.companies:
                raise AccessError("Enable both the source and destination companies in the company switcher.")

    @api.model_create_multi
    def create(self, vals_list):
        protected = {"name", "state", "snapshot_hash", "move_id", "completed_at", "completed_by", "summary", "line_ids"}
        if any(protected.intersection(vals) for vals in vals_list):
            raise AccessError("Cutover audit fields are system-managed.")
        records = super().create(vals_list)
        records._operator()
        for record in records:
            record._system_write({"name": "FCUT/%06d" % record.id})
        return records

    def _system_write(self, values):
        return super().write(values)

    def _invalidate_preview(self):
        if self.filtered(lambda b: b.state == "done"):
            raise UserError("Completed financial cutovers are read-only.")
        self.line_ids._system_unlink()
        self._system_write({"state": "draft", "snapshot_hash": False, "summary": False})

    def write(self, vals):
        self._operator()
        protected = {"name", "state", "snapshot_hash", "move_id", "completed_at", "completed_by", "summary", "line_ids"}
        if protected.intersection(vals):
            raise AccessError("Cutover audit fields are system-managed.")
        self._invalidate_preview()
        if {"source_company_id", "target_company_id"}.intersection(vals):
            self.mapping_ids.unlink()
            self.partner_mapping_ids.unlink()
        result = super().write(vals)
        self._operator()
        return result

    def unlink(self):
        self._operator()
        if self.filtered(lambda b: b.state == "done"):
            raise UserError("Keep completed cutovers as the opening-balance audit record.")
        self.line_ids._system_unlink()
        return super().unlink()

    def _validate(self):
        self.ensure_one()
        self._operator()
        source, target = self.source_company_id, self.target_company_id
        if self.state == "done":
            raise UserError("This cutover is already completed.")
        if source == target or source.currency_id != target.currency_id:
            raise UserError("Choose different companies with the same accounting currency.")
        if self.cutover_date > fields.Date.context_today(self):
            raise UserError("The cutover date cannot be in the future.")
        if self.search_count([("source_company_id", "=", source.id), ("state", "=", "done")]):
            raise UserError("This source company already has a completed financial cutover.")
        if self.env["account.move"].search_count([("company_id", "=", target.id), ("state", "=", "posted")]):
            raise UserError("The destination already has posted accounting entries. Use a fresh destination and run the financial cutover before the stock cutover.")
        dates = source.compute_fiscalyear_dates(self.cutover_date)
        if dates["date_from"] != target.compute_fiscalyear_dates(self.cutover_date)["date_from"]:
            raise UserError("Align the companies' fiscal year boundaries before cutover.")
        if not self.journal_id or self.journal_id.type != "general" or not self.journal_id.active:
            raise UserError("Choose an active destination Miscellaneous opening journal.")
        if not self.journal_id.filtered_domain(self.journal_id._check_company_domain(target)):
            raise UserError("The opening journal must belong to the destination company.")
        if self.journal_id.currency_id and self.journal_id.currency_id != target.currency_id:
            raise UserError("Use an opening journal in the destination's accounting currency.")
        if not self.retained_earnings_account_id or self.retained_earnings_account_id.account_type not in {"equity", "equity_unaffected"}:
            raise UserError("Choose a destination equity account for prior-year retained earnings.")
        self._check_account(self.retained_earnings_account_id, target.currency_id)
        if self.offset_account_id:
            self._check_account(self.offset_account_id, target.currency_id)
            if self.offset_account_id.account_type != "asset_current":
                raise UserError("Use the stock mover's Current Assets clearing account for excluded inventory.")
        return dates["date_from"]

    def _source_lines(self):
        lines = self.env["account.move.line"].with_company(self.source_company_id).search([
            ("company_id", "=", self.source_company_id.id),
            ("parent_state", "=", "posted"), ("date", "<=", self.cutover_date),
            ("display_type", "not in", ["line_section", "line_subsection", "line_note"]),
        ], order="id", limit=MAX_SOURCE_LINES + 1)
        if len(lines) > MAX_SOURCE_LINES:
            raise UserError("More than 100,000 source journal items require a separately reviewed larger migration.")
        if not lines:
            raise UserError("No posted source journal items exist on or before the cutover date.")
        if not self.currency_id.is_zero(sum(lines.mapped("balance"))):
            raise UserError("The source trial balance is not balanced; repair it before cutover.")
        return lines

    def action_match(self):
        """Prepare editable choices without creating destination business records."""
        self.ensure_one()
        self._operator()
        self._invalidate_preview()
        if self.source_company_id == self.target_company_id:
            raise UserError("Choose different source and destination companies.")
        lines = self._source_lines()
        Account = self.env["account.account"].with_company(self.target_company_id)
        account_domain = Account._check_company_domain(self.target_company_id)
        inventory_accounts = self.env["account.account"]
        if "account_stock_valuation_id" in self.source_company_id._fields:
            inventory_accounts |= self.source_company_id.account_stock_valuation_id
        if "property_stock_valuation_account_id" in self.env["product.category"]._fields:
            inventory_accounts |= self.env["product.category"].with_company(self.source_company_id).search([]).mapped("property_stock_valuation_account_id")
        existing = set(self.mapping_ids.source_account_id.ids)
        for account in lines.account_id.sorted("id"):
            if account.id in existing:
                continue
            code = account.with_company(self.source_company_id).code
            matches = Account.search([*account_domain, ("code", "=", code), ("account_type", "=", account.account_type)])
            self.env["company.financial.account.mapping"].create({
                "cutover_id": self.id, "source_account_id": account.id,
                "target_account_id": matches.id if len(matches) == 1 else False,
                "handled_by_stock": account in inventory_accounts,
            })
        partners = lines.filtered(lambda l: l.account_id.account_type in OPEN_ITEM_TYPES).partner_id
        existing = set(self.partner_mapping_ids.source_partner_id.ids)
        Partner = self.env["res.partner"].with_company(self.target_company_id)
        for partner in partners.sorted("id"):
            if partner.id in existing:
                continue
            if not partner.company_id or partner.company_id == self.target_company_id:
                matches = partner if partner.active else Partner.browse()
            else:
                matches = Partner.search([
                    ("company_id", "in", [False, self.target_company_id.id]),
                    ("name", "=", partner.name), ("vat", "=", partner.vat or False),
                    ("is_company", "=", partner.is_company),
                ])
            self.env["company.financial.partner.mapping"].create({
                "cutover_id": self.id, "source_partner_id": partner.id,
                "target_partner_id": matches.id if len(matches) == 1 else False,
                "create_contact": not matches,
            })
        return True

    def _check_account(self, account, currency):
        if not account.active or not account.filtered_domain(account._check_company_domain(self.target_company_id)):
            raise UserError("Every destination account must be active and available to the destination company.")
        if account.currency_id and account.currency_id != currency:
            raise UserError("A destination account's forced currency does not match the reviewed opening amount.")

    def _residual(self, line):
        """Reconstruct unpaid amounts by accounting date, including later-settled items."""
        debit = line.matched_credit_ids.filtered(lambda p: p.max_date <= self.cutover_date)
        credit = line.matched_debit_ids.filtered(lambda p: p.max_date <= self.cutover_date)
        return (
            self.currency_id.round(line.balance - sum(debit.mapped("amount")) + sum(credit.mapped("amount"))),
            line.currency_id.round(line.amount_currency - sum(debit.mapped("debit_amount_currency")) + sum(credit.mapped("credit_amount_currency"))),
        )

    def _signature(self, record, names):
        """Hash actual settings too: write_date can be unchanged within a second."""
        values = [record.id, str(record.write_date)]
        for name in names:
            if name in record._fields:
                value = record[name]
                values.append([name, value.ids if isinstance(value, models.BaseModel) else value])
        return values

    def _account_signature(self, account, company):
        return self._signature(account.with_company(company),
            ("name", "code", "account_type", "reconcile", "currency_id", "active", "company_ids"))

    def _contact_signature(self, partner):
        return self._signature(partner, ("name", "company_type", "company_id", "active", "vat",
            "street", "street2", "city", "zip", "email", "phone", "website", "lang", "ref", "country_id", "state_id"))

    def _plan(self):
        fiscal_start = self._validate()
        lines = self._source_lines()
        mappings = {m.source_account_id.id: m for m in self.mapping_ids}
        partners = {m.source_partner_id.id: m for m in self.partner_mapping_ids}
        grouped = defaultdict(lambda: [0.0, 0.0])
        source_arap = defaultdict(float)
        residual_arap = defaultdict(float)
        previous_profit = 0.0
        rows = []
        source_evidence = []
        for line in lines:
            account = line.account_id
            currency = line.currency_id
            source_evidence.append([
                line.id, str(line.write_date), line.account_id.id, line.partner_id.id,
                str(line.date), str(line.date_maturity), line.name, line.move_id.name, line.move_id.ref,
                line.balance, line.amount_currency, currency.id,
                [(p.id, p.amount, p.debit_amount_currency, p.credit_amount_currency, str(p.max_date))
                 for p in (line.matched_credit_ids | line.matched_debit_ids).sorted("id")],
            ])
            if account.account_type in OPEN_ITEM_TYPES:
                source_arap[account.id] += line.balance
                balance, amount_currency = self._residual(line)
                residual_arap[account.id] += balance
                if self.currency_id.is_zero(balance) and currency.is_zero(amount_currency):
                    continue
                if not line.partner_id:
                    raise UserError("Every unpaid customer/vendor item needs a partner. Missing on %s." % line.move_id.display_name)
                if "on_payment" in line.move_id.invoice_line_ids.tax_ids.flatten_taxes_hierarchy().mapped("tax_exigibility"):
                    raise UserError("Unpaid cash-basis tax invoices need a separate accountant-reviewed tax migration. This opening would otherwise lose their future tax recognition: %s." % line.move_id.display_name)
                mapping = partners.get(line.partner_id.id)
                if not mapping or (not mapping.target_partner_id and not mapping.create_contact):
                    raise UserError("Review the missing or ambiguous destination contact for %s." % line.partner_id.display_name)
                if mapping.target_partner_id and (not mapping.target_partner_id.active or mapping.target_partner_id.company_id not in (self.env["res.company"], self.target_company_id)):
                    raise UserError("Destination contacts must be active and shared or belong to the destination company.")
                rows.append({
                    "source_account_id": account.id, "source_line_id": line.id,
                    "source_partner_id": line.partner_id.id,
                    "source_date": str(line.date),
                    "target_partner_id": mapping.target_partner_id.id,
                    "currency_id": currency.id, "balance": balance, "amount_currency": amount_currency,
                    "date_maturity": str(line.date_maturity or line.date),
                    "label": " | ".join(filter(None, [line.move_id.name, line.move_id.ref, line.name or line.partner_id.name])), "kind": "open_item",
                })
            elif account.account_type in PROFIT_TYPES and line.date < fiscal_start:
                previous_profit += line.balance
            else:
                grouped[(account.id, currency.id)][0] += line.balance
                grouped[(account.id, currency.id)][1] += line.amount_currency
        for account_id, total in source_arap.items():
            if not self.currency_id.is_zero(total - residual_arap[account_id]):
                raise UserError("Unpaid items do not reconcile to the source customer/vendor control account.")
        for (account_id, currency_id), (balance, amount_currency) in sorted(grouped.items()):
            currency = self.env["res.currency"].browse(currency_id)
            balance, amount_currency = self.currency_id.round(balance), currency.round(amount_currency)
            if self.currency_id.is_zero(balance) and currency.is_zero(amount_currency):
                continue
            account = self.env["account.account"].browse(account_id)
            rows.append({"source_account_id": account_id, "currency_id": currency_id,
                "balance": balance, "amount_currency": amount_currency,
                "label": "Opening: %s" % account.with_company(self.source_company_id).display_name, "kind": "balance"})
        excluded = 0.0
        account_evidence = []
        for row in rows:
            mapping = mappings.get(row["source_account_id"])
            if not mapping:
                raise UserError("Click Match Accounts and review all source account choices first.")
            source = mapping.source_account_id
            if mapping.handled_by_stock:
                if source.account_type not in {"asset_current", "asset_non_current"} or row["kind"] == "open_item":
                    raise UserError("Only inventory asset balances may be excluded for the stock cutover.")
                row.update({"kind": "stock_excluded", "target_account_id": False})
                excluded += row["balance"]
            else:
                target = mapping.target_account_id
                if not target:
                    raise UserError("Select a destination account for %s; missing or ambiguous codes are never guessed." % source.display_name)
                self._check_account(target, self.env["res.currency"].browse(row["currency_id"]))
                if source.account_type != target.account_type:
                    raise UserError("Source and destination account types must match; review the destination chart before proceeding.")
                if row["kind"] == "open_item" and not target.reconcile:
                    raise UserError("Customer/vendor destination accounts must allow reconciliation.")
                row["target_account_id"] = target.id
            account_evidence.append([
                self._account_signature(source, self.source_company_id),
                self._account_signature(mapping.target_account_id, self.target_company_id), mapping.handled_by_stock,
            ])
        previous_profit = self.currency_id.round(previous_profit)
        if not self.currency_id.is_zero(previous_profit):
            rows.append({"target_account_id": self.retained_earnings_account_id.id, "currency_id": self.currency_id.id,
                "balance": previous_profit, "amount_currency": previous_profit,
                "label": "Prior-year profit/loss brought forward", "kind": "retained"})
        excluded = self.currency_id.round(excluded)
        if not self.currency_id.is_zero(excluded):
            if not self.offset_account_id:
                raise UserError("Select the stock mover's destination Stock Migration Clearing account for excluded inventory.")
            rows.append({"target_account_id": self.offset_account_id.id, "currency_id": self.currency_id.id,
                "balance": excluded, "amount_currency": excluded,
                "label": "Inventory carried separately by stock cutover", "kind": "stock_clearing"})
        included = [row for row in rows if row["kind"] != "stock_excluded"]
        if not included or len(included) > MAX_OPENING_LINES:
            raise UserError("The preview must contain between 1 and 10,000 destination journal items.")
        if not self.currency_id.is_zero(sum(row["balance"] for row in included)):
            raise UserError("The generated opening does not balance. No rounding or write-off line is added to hide differences.")
        evidence = {
            "companies": [self._signature(c, ("name", "currency_id", "fiscalyear_last_day", "fiscalyear_last_month", "account_fiscal_country_id"))
                for c in (self.source_company_id | self.target_company_id)],
            "currencies": [self._signature(c, ("rounding", "active")) for c in lines.currency_id],
            "date": str(self.cutover_date), "fiscal_start": str(fiscal_start),
            "journal": self._signature(self.journal_id, ("name", "code", "type", "company_id", "currency_id", "active", "restrict_mode_hash_table", "account_control_ids")),
            "retained": self._account_signature(self.retained_earnings_account_id, self.target_company_id),
            "offset": self._account_signature(self.offset_account_id, self.target_company_id),
            "accounts": account_evidence, "source": source_evidence, "rows": rows,
            "contacts": [[self._contact_signature(p.source_partner_id), self._contact_signature(p.target_partner_id),
                p.create_contact] for p in self.partner_mapping_ids.sorted("id")],
        }
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
        return rows, digest, len(lines)

    def action_preview(self):
        self.ensure_one()
        self._operator()
        rows, digest, count = self._plan()
        self.line_ids._system_unlink()
        self.env["company.financial.cutover.line"]._system_create([
            {"cutover_id": self.id, "sequence": index, **row} for index, row in enumerate(rows, 1)
        ])
        debit = sum(max(row["balance"], 0) for row in rows if row["kind"] != "stock_excluded")
        credit = sum(max(-row["balance"], 0) for row in rows if row["kind"] != "stock_excluded")
        self._system_write({"state": "preview", "snapshot_hash": digest,
            "summary": "%d source journal items; %d unpaid items. Opening debit %.2f / credit %.2f. Source history and settings stay in the old company."
                % (count, sum(row["kind"] == "open_item" for row in rows), debit, credit)})
        return True

    def _lock(self):
        self.env.flush_all()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("""
                    LOCK TABLE account_move, account_move_line, account_partial_reconcile,
                    account_account, account_journal, res_partner, res_company, res_currency,
                    company_financial_cutover, company_financial_account_mapping,
                    company_financial_partner_mapping, company_financial_cutover_line
                    IN SHARE ROW EXCLUSIVE MODE NOWAIT
                """)
        except LockNotAvailable as exc:
            raise UserError("Accounting is busy. Stop accounting activity and retry during the cutover maintenance window.") from exc
        companies = self.source_company_id | self.target_company_id
        companies.check_access("write")
        # Odoo uses repeatable-read transactions. A real row update forces a
        # concurrent stale request to retry rather than reuse an old snapshot.
        self.env.cr.execute("UPDATE res_company SET write_date = write_date WHERE id = ANY(%s)", [companies.ids])
        self.env.invalidate_all()

    def action_prepare_stock_clearing(self):
        self.ensure_one()
        self._operator()
        self._invalidate_preview()
        if "company.stock.fifo.migration" not in self.env:
            raise UserError("Install Company Stock Cutover first, or select an accountant-configured Current Assets clearing account and use that same account in the stock mover.")
        with self.env.cr.savepoint():
            self._lock()
            account = self.env["company.stock.fifo.migration"]._company_clearing_account(self.target_company_id)
            if account.account_type != "asset_current":
                raise UserError("The stock mover's saved clearing account must be Current Assets for this workflow.")
            self._system_write({"offset_account_id": account.id})
        return True

    def _create_contacts(self, source_partner_ids):
        contacts = {}
        Partner = self.env["res.partner"].with_company(self.target_company_id)
        for mapping in self.partner_mapping_ids.filtered(lambda m: m.source_partner_id.id in source_partner_ids):
            if mapping.target_partner_id:
                contacts[mapping.source_partner_id.id] = mapping.target_partner_id.id
                continue
            source = mapping.source_partner_id
            values = {name: source[name] for name in ("name", "company_type", "vat", "street", "street2", "city", "zip", "email", "phone", "website", "lang")}
            values.update({"company_id": self.target_company_id.id,
                "country_id": source.country_id.id, "state_id": source.state_id.id,
                "ref": source.ref, "comment": "Opening contact from %s (%s)." % (self.source_company_id.name, source.id)})
            contact = Partner.create(values)
            mapping._system_write({"target_partner_id": contact.id})
            contacts[source.id] = contact.id
        return contacts

    def action_apply(self):
        self.ensure_one()
        self._operator()
        with self.env.cr.savepoint():
            self._lock()
            if self.state != "preview":
                raise UserError("Build and review a fresh Preview before confirming.")
            rows, digest, _count = self._plan()
            if digest != self.snapshot_hash:
                raise UserError("Source accounting, contacts or destination configuration changed. Build a fresh Preview.")
            contacts = self._create_contacts({row.get("source_partner_id") for row in rows if row["kind"] == "open_item"})
            included = [row for row in rows if row["kind"] != "stock_excluded"]
            commands = []
            for index, row in enumerate(included, 1):
                commands.append(Command.create({
                    "sequence": index, "name": row["label"], "account_id": row["target_account_id"],
                    "partner_id": contacts.get(row.get("source_partner_id"), False),
                    "date_maturity": row.get("date_maturity", False),
                    "debit": max(row["balance"], 0), "credit": max(-row["balance"], 0),
                    "currency_id": row["currency_id"], "amount_currency": row["amount_currency"],
                    "tax_ids": [Command.clear()], "tax_tag_ids": [Command.clear()],
                    "analytic_distribution": False,
                }))
            move = self.env["account.move"].with_company(self.target_company_id)._create_financial_cutover({
                "move_type": "entry", "company_id": self.target_company_id.id,
                "journal_id": self.journal_id.id, "date": self.cutover_date,
                "ref": "%s | Opening from %s" % (self.name, self.source_company_id.name),
                "financial_cutover_id": self.id, "line_ids": commands,
            })
            move.action_post()
            if move.state != "posted" or move.date != self.cutover_date or move.company_id != self.target_company_id:
                raise UserError("Odoo did not post the opening on the reviewed date in the destination company.")
            posted = move.line_ids.sorted("sequence")
            if len(posted) != len(included) or not self.currency_id.is_zero(sum(posted.mapped("balance"))):
                raise UserError("Native opening journal items do not match the reviewed balanced entry.")
            audit = self.line_ids.filtered(lambda l: l.kind != "stock_excluded").sorted("sequence")
            for row, line, review in zip(included, posted, audit):
                currency = self.env["res.currency"].browse(row["currency_id"])
                if (line.account_id.id != row["target_account_id"] or line.currency_id != currency
                    or line.partner_id.id != contacts.get(row.get("source_partner_id"), False)
                    or not self.currency_id.is_zero(line.balance - row["balance"])
                    or not currency.is_zero(line.amount_currency - row["amount_currency"])
                    or (row.get("date_maturity") and str(line.date_maturity) != row["date_maturity"])
                    or (row["kind"] == "open_item" and (
                        not self.currency_id.is_zero(line.amount_residual - row["balance"])
                        or not currency.is_zero(line.amount_residual_currency - row["amount_currency"])) )):
                    raise UserError("A native opening amount, unpaid item, currency, account, partner or due date did not reconcile.")
                review._system_write({"posted_line_id": line.id, "target_partner_id": line.partner_id.id})
            self._system_write({"state": "done", "move_id": move.id,
                "completed_at": fields.Datetime.now(), "completed_by": self.env.user.id})
        return self.action_open_entry()

    def action_open_entry(self):
        self.ensure_one()
        self._operator()
        return {"type": "ir.actions.act_window", "name": "Financial Opening Entry",
            "res_model": "account.move", "res_id": self.move_id.id, "view_mode": "form"}


class CutoverMappingMixin(models.AbstractModel):
    _name = "company.financial.mapping.mixin"
    _description = "Editable choices for a financial cutover"

    cutover_id = fields.Many2one("company.financial.cutover", required=True, ondelete="cascade")
    source_company_id = fields.Many2one(related="cutover_id.source_company_id")
    target_company_id = fields.Many2one(related="cutover_id.target_company_id")

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env["company.financial.cutover"].browse([v["cutover_id"] for v in vals_list])
        parents._operator()
        parents._invalidate_preview()
        return super().create(vals_list)

    def write(self, vals):
        if "cutover_id" in vals:
            raise AccessError("Choices cannot be reassigned to another cutover.")
        self.cutover_id._operator()
        self.cutover_id._invalidate_preview()
        return super().write(vals)

    def _system_write(self, values):
        return super().write(values)

    def unlink(self):
        self.cutover_id._operator()
        self.cutover_id._invalidate_preview()
        return super().unlink()


class AccountMapping(models.Model):
    _name = "company.financial.account.mapping"
    _inherit = "company.financial.mapping.mixin"
    _description = "Financial Cutover Account Choice"

    source_account_id = fields.Many2one("account.account", required=True, ondelete="restrict")
    target_account_id = fields.Many2one("account.account", ondelete="restrict")
    handled_by_stock = fields.Boolean(string="Handled by Stock Cutover",
        help="Exclude this inventory asset account and carry its balance through the destination stock clearing account.")
    _unique_source = models.Constraint("UNIQUE(cutover_id, source_account_id)", "Only one choice is allowed per source account.")


class PartnerMapping(models.Model):
    _name = "company.financial.partner.mapping"
    _inherit = "company.financial.mapping.mixin"
    _description = "Financial Cutover Contact Choice"

    source_partner_id = fields.Many2one("res.partner", required=True, ondelete="restrict")
    target_partner_id = fields.Many2one("res.partner", ondelete="restrict")
    create_contact = fields.Boolean(string="Create Destination Contact",
        help="Create contact identity and address only. Source fiscal positions, payment terms, bank accounts and company settings are not copied.")
    _unique_source = models.Constraint("UNIQUE(cutover_id, source_partner_id)", "Only one choice is allowed per source contact.")


class FinancialCutoverLine(models.Model):
    _name = "company.financial.cutover.line"
    _description = "Reviewed Financial Opening Amount"
    _order = "sequence, id"

    cutover_id = fields.Many2one("company.financial.cutover", required=True, ondelete="cascade")
    sequence = fields.Integer()
    source_account_id = fields.Many2one("account.account", ondelete="restrict")
    target_account_id = fields.Many2one("account.account", ondelete="restrict")
    source_line_id = fields.Many2one("account.move.line", ondelete="restrict")
    source_date = fields.Date()
    source_partner_id = fields.Many2one("res.partner", ondelete="restrict")
    target_partner_id = fields.Many2one("res.partner", ondelete="restrict")
    currency_id = fields.Many2one("res.currency", required=True)
    company_currency_id = fields.Many2one(related="cutover_id.currency_id", string="Accounting Currency")
    balance = fields.Monetary(currency_field="company_currency_id")
    amount_currency = fields.Monetary()
    date_maturity = fields.Date()
    label = fields.Char()
    kind = fields.Selection([("balance", "Ledger Balance"), ("open_item", "Unpaid Item"),
        ("retained", "Prior-year Retained Earnings"), ("stock_excluded", "Inventory Moved Separately"),
        ("stock_clearing", "Stock Clearing Offset")], required=True)
    posted_line_id = fields.Many2one("account.move.line", ondelete="restrict")

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError("Preview amounts are system-managed; rebuild Preview instead.")

    def write(self, vals):
        raise AccessError("Preview amounts are system-managed; rebuild Preview instead.")

    def unlink(self):
        raise AccessError("Preview amounts are system-managed; rebuild Preview instead.")

    def _system_create(self, values):
        return super().create(values)

    def _system_write(self, values):
        return super().write(values)

    def _system_unlink(self):
        return super().unlink()
