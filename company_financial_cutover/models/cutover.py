import hashlib
import json
import re
from collections import defaultdict

from psycopg2.errors import LockNotAvailable

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, ConcurrencyError, UserError
from odoo.tools import SQL, formatLang


OPEN_ITEM_TYPES = {"asset_receivable", "liability_payable"}
PROFIT_TYPES = {"income", "income_other", "expense", "expense_depreciation", "expense_direct_cost"}
MAX_SOURCE_LINES = 100000
MAX_OPENING_LINES = 10000


class FinancialCutover(models.Model):
    _name = "company.financial.cutover"
    _description = "Company Financial Cutover"
    _order = "id desc"

    name = fields.Char(default="New", readonly=True, copy=False)
    source_company_id = fields.Many2one("res.company", string="Old company", required=True, ondelete="restrict")
    target_company_id = fields.Many2one("res.company", string="New company", required=True, ondelete="restrict")
    currency_id = fields.Many2one(related="source_company_id.currency_id")
    cutover_date = fields.Date(string="Balance date", required=True, default=fields.Date.context_today,
        help="Include posted balances and unpaid items dated on or before this day. Purchase history is copied at the time you review it.")
    journal_id = fields.Many2one("account.journal", string="Opening journal",
        help="Ask your accountant to select the new company's Miscellaneous journal, usually MISC.")
    retained_earnings_account_id = fields.Many2one("account.account", string="Previous years' earnings account",
        help="Your accountant chooses where the old company's accumulated profit or loss belongs in the new company.")
    offset_account_id = fields.Many2one("account.account", string="Stock clearing account",
        help="Balances inventory excluded for the separate stock mover. Use its destination Stock Migration Clearing account.")
    mapping_ids = fields.One2many("company.financial.account.mapping", "cutover_id", copy=False)
    partner_mapping_ids = fields.One2many("company.financial.partner.mapping", "cutover_id", copy=False)
    line_ids = fields.One2many("company.financial.cutover.line", "cutover_id", readonly=True, copy=False)
    state = fields.Selection([("draft", "Getting ready"), ("preview", "Ready to move"), ("done", "Completed")],
        default="draft", required=True, readonly=True, copy=False)
    snapshot_hash = fields.Char(readonly=True, copy=False)
    move_id = fields.Many2one("account.move", readonly=True, copy=False, ondelete="restrict")
    completed_at = fields.Datetime(readonly=True, copy=False)
    completed_by = fields.Many2one("res.users", readonly=True, copy=False)
    summary = fields.Text(readonly=True, copy=False)
    check_status = fields.Selection([("unchecked", "Check needed"), ("blocked", "Needs attention"),
        ("ready", "Ready to review")], default="unchecked", readonly=True, copy=False)
    check_report = fields.Text(string="Check results", readonly=True, copy=False)

    def _operator(self):
        if not (self.env.user.has_group("base.group_system")
                and self.env.user.has_group("account.group_account_manager")):
            raise AccessError("A Settings administrator with Accounting administrator access must run this cutover.")
        for batch in self:
            if not (batch._source_companies() | batch.target_company_id) <= self.env.companies:
                raise AccessError("Enable the old company, every included branch and the new company in the company switcher.")

    @api.model_create_multi
    def create(self, vals_list):
        protected = {"name", "state", "snapshot_hash", "move_id", "completed_at", "completed_by", "summary", "line_ids", "check_status", "check_report", "purchase_preview_data", "purchase_history_ids", "purchase_history_preview_ready", "archive_key", "archive_attachment_id", "report_attachment_id", "completed_source_ids", "correction_move_ids", "destination_balance_preview"}
        if any(protected.intersection(vals) for vals in vals_list):
            raise AccessError("Cutover audit fields are system-managed.")
        records = super().create(vals_list)
        records._operator()
        for record in records:
            record._system_write({"name": "FCUT/%06d" % record.id})
            record._suggest_settings()
        return records

    def _system_write(self, values):
        return super().write(values)

    def _invalidate_preview(self):
        if self.filtered(lambda b: b.state == "done"):
            raise UserError("Completed financial cutovers are read-only.")
        self.line_ids._system_unlink()
        self._system_write({"state": "draft", "snapshot_hash": False, "summary": False,
            "check_status": "unchecked", "check_report": False})

    def write(self, vals):
        self._operator()
        protected = {"name", "state", "snapshot_hash", "move_id", "completed_at", "completed_by", "summary", "line_ids", "check_status", "check_report", "purchase_preview_data", "purchase_history_ids", "purchase_history_preview_ready", "archive_key", "archive_attachment_id", "report_attachment_id", "completed_source_ids", "correction_move_ids", "destination_balance_preview"}
        if protected.intersection(vals):
            raise AccessError("Cutover audit fields are system-managed.")
        self._invalidate_preview()
        if {"source_company_id", "target_company_id"}.intersection(vals):
            self.mapping_ids.unlink()
            self.partner_mapping_ids.unlink()
        result = super().write(vals)
        self._operator()
        if "target_company_id" in vals:
            self._system_write({name: vals.get(name, False) for name in
                ("journal_id", "retained_earnings_account_id", "offset_account_id")})
            self._suggest_settings()
        return result

    def _suggest_settings(self):
        """Prefer the standard MISC journal; never guess an ambiguous equity account."""
        for batch in self:
            if not batch.target_company_id:
                continue
            values = {}
            if not batch.journal_id:
                Journal = self.env["account.journal"]
                domain = [("company_id", "=", batch.target_company_id.id), ("type", "=", "general")]
                matches = Journal.search([*domain, ("code", "=", "MISC")], limit=2)
                if not matches:
                    specialized = batch.target_company_id.currency_exchange_journal_id | batch.target_company_id.tax_cash_basis_journal_id
                    matches = Journal.search([*domain, ("id", "not in", specialized.ids),
                        ("code", "not in", ["CABA", "EXCH"])], limit=2)
                if len(matches) == 1:
                    values["journal_id"] = matches.id
            if not batch.retained_earnings_account_id:
                Account = self.env["account.account"].with_company(batch.target_company_id)
                matches = Account.search([*Account._check_company_domain(batch.target_company_id),
                    ("account_type", "=", "equity_unaffected")], limit=2)
                if len(matches) == 1:
                    values["retained_earnings_account_id"] = matches.id
            if values:
                batch._system_write(values)

    def _completion_key(self):
        return "company_financial_cutover.completed.source.%s" % self.source_company_id.id

    def unlink(self):
        self._operator()
        if self.filtered(lambda b: b.state == "done"):
            raise UserError("Keep completed cutovers as the opening-balance audit record.")
        self.line_ids._system_unlink()
        return super().unlink()

    def _validation_details(self):
        self.ensure_one()
        self._operator()
        issues = []
        source, target = self.source_company_id, self.target_company_id
        if self.state == "done":
            issues.append("This cutover is already completed.")
        if source == target or source.currency_id != target.currency_id:
            issues.append("Choose different companies with the same accounting currency.")
        if (source.sudo().all_child_ids and not self.include_source_branches) or target.sudo().parent_id or target.sudo().all_child_ids:
            issues.append("Choose Include old branches to carry their branch balances together. The new company must be standalone.")
        if self.cutover_date > fields.Date.context_today(self):
            issues.append("The cutover date cannot be in the future.")
        # Check across all previous destinations, including companies hidden by
        # the current switcher. Reveal only that this accessible source moved.
        if (self.sudo().search_count([("source_company_id", "=", source.id), ("state", "=", "done"), ("include_financial", "=", True)])
            or self.env["ir.config_parameter"].sudo().search_count([("key", "in", self._completion_keys())])):
            issues.append("This source company already has a completed financial cutover.")
        posted = self.env["account.move"].search_count([("company_id", "=", target.id), ("state", "=", "posted")])
        drafts = self.env["account.move"].search_count([("company_id", "=", target.id), ("state", "=", "draft")])
        payments = self.env["account.payment"].search_count([("company_id", "=", target.id),
            ("state", "not in", ["canceled", "rejected"])])
        if (posted or drafts or payments) and self.destination_mode == "fresh":
            issues.append("The destination already has posted entries: %s; draft entries: %s; payments: %s. "
                "Review these existing transactions with your accountant. Use a fresh destination to avoid duplicate balances."
                % (posted, drafts, payments))
        dates = source.compute_fiscalyear_dates(self.cutover_date)
        if dates["date_from"] != target.compute_fiscalyear_dates(self.cutover_date)["date_from"]:
            issues.append("Align the companies' fiscal year boundaries before cutover.")
        if not self.journal_id or self.journal_id.type != "general" or not self.journal_id.active:
            issues.append("Ask your accountant to choose the new company's Miscellaneous opening journal (normally MISC) in Accountant setup.")
        if self.journal_id:
            specialized = target.currency_exchange_journal_id | target.tax_cash_basis_journal_id
            if self.journal_id in specialized or self.journal_id.code in {"CABA", "EXCH"}:
                issues.append("Choose the Miscellaneous journal (normally MISC), rather than the cash-basis or exchange-difference journal.")
            if not self.journal_id.filtered_domain(self.journal_id._check_company_domain(target)):
                issues.append("The opening journal must belong to the destination company.")
            if self.journal_id.currency_id and self.journal_id.currency_id != target.currency_id:
                issues.append("Use an opening journal in the destination's accounting currency.")
        return dates["date_from"], issues

    @api.model
    def _raise_issues(self, title, issues):
        issues = list(dict.fromkeys(str(issue) for issue in issues if issue))
        if issues:
            raise UserError(title + "\n\n" + "\n".join("• " + issue for issue in issues))

    def _validate(self):
        fiscal_start, issues = self._validation_details()
        self._raise_issues("Review these move requirements together:", issues)
        return fiscal_start

    def _validate_stock_clearing(self):
        target = self.target_company_id
        account = self.offset_account_id
        issues = []
        if not account:
            issues.append("Select the stock mover's destination Stock Migration Clearing account for excluded inventory.")
        else:
            issues.extend(self._account_issues(account, target.currency_id))
            if account.account_type != "asset_current":
                issues.append("Use the stock mover's Current Assets clearing account for excluded inventory.")
            parameter = self.env["ir.config_parameter"].sudo().search([
                ("key", "=", "company_stock_fifo_migration.clearing_account.%s" % target.id)])
            if parameter:
                if not parameter.value or not parameter.value.isdecimal():
                    issues.append("The stock mover's saved clearing-account reference is invalid. Ask your accountant to review it.")
                elif account.id != int(parameter.value):
                    issues.append("The selected stock clearing account differs from the stock mover's saved account. "
                        "Use Connect the stock move or choose the saved account so inventory clearing nets to zero.")
        self._raise_issues("Review the inventory clearing setup:", issues)

    def _review_notices(self, lines):
        return self._duplicate_bill_notices(lines.move_id)

    def _source_lines(self):
        lines = self.env["account.move.line"].with_company(self.source_company_id).search([
            ("company_id", "in", self._source_companies().ids),
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

    def _duplicate_bill_notices(self, moves):
        seen = {}
        notices = []
        for move in moves.filtered(lambda m: m.move_type == "in_invoice" and m.ref):
            reversals = move.reversal_move_ids.filtered(lambda r: r.state == "posted" and r.date <= self.cutover_date
                and r.move_type == "in_refund" and r.currency_id == move.currency_id)
            if reversals and move.currency_id.is_zero(move.amount_total - sum(reversals.mapped("amount_total"))):
                continue
            key = (move.partner_id.commercial_partner_id.id, self._identity(move.ref, True),
                move.currency_id.id, str(move.invoice_date), move.amount_total)
            if key in seen:
                notices.append("Possible duplicate vendor bills: %s and %s. Both remain separate ledger items and are included at their recorded balances. Review them with your accountant; copying balances does not create new bills or correct old ones."
                    % (seen[key].display_name, move.display_name))
            seen[key] = move

        return notices

    def action_match(self):
        """Prepare editable choices without creating destination business records."""
        self.ensure_one()
        self._operator()
        self._invalidate_preview()
        if not self.include_financial:
            return self._check_purchase_only()
        self._suggest_settings()
        try:
            lines = self._source_lines()
        except UserError:
            lines = self.env["account.move.line"]
        Account = self.env["account.account"].with_company(self.target_company_id)
        account_domain = Account._check_company_domain(self.target_company_id)
        inventory_accounts = self.env["account.account"]
        for company in self._source_companies():
            if "account_stock_valuation_id" in company._fields:
                inventory_accounts |= company.account_stock_valuation_id
            if "property_stock_valuation_account_id" in self.env["product.category"]._fields:
                inventory_accounts |= self.env["product.category"].with_company(company).search([]).mapped("property_stock_valuation_account_id")
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
        financial_partners = lines.filtered(lambda l: l.account_id.account_type in OPEN_ITEM_TYPES
            and (not self.currency_id.is_zero(self._residual(l)[0]) or not l.currency_id.is_zero(self._residual(l)[1]))).partner_id
        self._match_contacts(financial_partners | self._purchase_contacts(), financial_partners)
        reused = len(self.partner_mapping_ids.filtered("target_partner_id"))
        new = len(self.partner_mapping_ids.filtered(lambda m: not m.target_partner_id and m.create_contact))
        report = ["Existing accounts and contacts are reused wherever a clear match is found.",
            "Contacts: %s existing; %s proposed new; %s need a choice." % (
                reused, new, len(self.partner_mapping_ids) - reused - new)]
        needs_choice = self.partner_mapping_ids.filtered(lambda m: not m.target_partner_id and not m.create_contact)
        if needs_choice:
            report.append("Choose the correct existing contact for: %s. Name alone is not enough to auto-match different records."
                % ", ".join(needs_choice[:10].source_partner_id.mapped("display_name")))
        issues = []
        try:
            self._plan()
        except UserError as exc:
            issues.append(str(exc))
        # Purchase checks are independent of financial setup. Run them even
        # when accounting needs attention, rather than revealing them later.
        try:
            self._purchase_plan()
        except UserError as exc:
            issues.append(str(exc))
        status = "blocked" if issues else "ready"
        if issues:
            report.append("Needs attention before moving:\n" + "\n\n".join(dict.fromkeys(issues)))
        else:
            report.append("Checks passed. Review the financial amounts with your accountant before moving.")
        notices = self._review_notices(lines)
        if notices:
            report.append("For review — these notices do not block copying:\n" + "\n".join("• " + note for note in notices))
        self._system_write({"check_status": status, "check_report": "\n\n".join(report)})
        return True

    def _match_contacts(self, partners, create_partners=None):
        """Purchase vendors need explicit choices too, without creating contacts."""
        create_partners = create_partners or self.env["res.partner"]
        existing = set(self.partner_mapping_ids.source_partner_id.ids)
        pool = self._contact_pool()
        for partner in partners.sorted("id"):
            if partner.id in existing:
                continue
            matches = self._contact_candidates(partner, pool)
            automatic = len(matches) == 1 and self._contact_compatible(partner, matches)
            self.env["company.financial.partner.mapping"].create({
                "cutover_id": self.id, "source_partner_id": partner.id,
                "target_partner_id": matches.id if automatic else False,
                "create_contact": not matches and partner in create_partners,
            })

    @staticmethod
    def _identity(value, identifier=False):
        value = (value or "").casefold()
        return re.sub(r"[^\w]", "", value) if identifier else " ".join(value.split())

    def _contact_pool(self):
        Partner = self.env["res.partner"].with_company(self.target_company_id).with_context(active_test=False)
        contacts = Partner.search([("company_id", "in", [False, self.target_company_id.id])], limit=MAX_SOURCE_LINES + 1)
        if len(contacts) > MAX_SOURCE_LINES:
            raise UserError("More than 100,000 destination contacts need a separately reviewed matching process.")
        pool = {key: defaultdict(lambda: Partner.browse()) for key in ("vat", "ref", "name")}
        for partner in contacts:
            for field in pool:
                key = self._identity(partner[field], field != "name")
                if key:
                    pool[field][(partner.is_company, key)] |= partner
        return pool

    def _contact_candidates(self, source, pool):
        if not source.company_id or source.company_id == self.target_company_id:
            return source
        for field in ("vat", "ref", "name"):
            key = self._identity(source[field], field != "name")
            matches = pool[field].get((source.is_company, key), self.env["res.partner"])
            # A VAT shared by several people is not a unique person's identity.
            if field == "vat" and not source.is_company:
                matches = matches.filtered(lambda p: self._identity(p.name) == self._identity(source.name))
            if matches:
                return matches
        return self.env["res.partner"]

    def _contact_compatible(self, source, target):
        # Reusing the very same shared record is safe. A matching display name
        # between different records is only a candidate for explicit review.
        identified = source == target or any(source[field] and target[field]
            and self._identity(source[field], True) == self._identity(target[field], True) for field in ("vat", "ref"))
        return identified and all(not source[field] or self._identity(source[field], True) == self._identity(target[field], True)
            for field in ("vat", "ref"))

    def _account_issues(self, account, currency):
        issues = []
        if not account.active or not account.filtered_domain(account._check_company_domain(self.target_company_id)):
            issues.append("Every destination account must be active and available to the destination company.")
        if account.currency_id and account.currency_id != currency:
            issues.append("A destination account's forced currency does not match the reviewed opening amount.")
        return issues

    def _check_account(self, account, currency):
        self._raise_issues("Review the destination account:", self._account_issues(account, currency))

    def _residual(self, line):
        """Reconstruct unpaid amounts by accounting date, including later-settled items."""
        debit = line.matched_credit_ids.filtered(lambda p: p.max_date <= self.cutover_date)
        credit = line.matched_debit_ids.filtered(lambda p: p.max_date <= self.cutover_date)
        return (
            self.currency_id.round(line.balance - sum(debit.mapped("amount")) + sum(credit.mapped("amount"))),
            line.currency_id.round(line.amount_currency - sum(debit.mapped("debit_amount_currency")) + sum(credit.mapped("credit_amount_currency"))),
        )

    def _bank_settlement_issues(self, lines):
        return [issue for company in self._source_companies()
            for issue in self._company_bank_settlement_issues(company, lines.filtered(lambda line: line.company_id == company))]

    def _company_bank_settlement_issues(self, source, lines):
        """Aggregated ledger balances cannot replace outstanding payment items."""
        settings_companies = source | source.root_id
        journals = self.env["account.journal"].with_company(source).search([
            ("company_id", "in", settings_companies.ids), ("type", "in", ["bank", "cash"])])
        accounts = (journals.inbound_payment_method_line_ids.payment_account_id
            | journals.outbound_payment_method_line_ids.payment_account_id | journals.suspense_account_id
            | settings_companies.account_journal_suspense_account_id | settings_companies.transfer_account_id
            | lines.move_id.origin_payment_id.outstanding_account_id)
        for company in settings_companies:
            template = self.env["account.chart.template"].with_company(company).with_context(allowed_company_ids=company.ids)
            for key in ("account_journal_payment_debit_account_id", "account_journal_payment_credit_account_id"):
                accounts |= template.ref(key, raise_if_not_found=False) or self.env["account.account"]
        accounts = accounts.filtered(lambda a: a.account_type not in OPEN_ITEM_TYPES | {"asset_cash"})
        issues = []
        for line in lines.filtered(lambda l: l.account_id in accounts):
            balance, foreign = self._residual(line)
            if not self.currency_id.is_zero(balance) or not line.currency_id.is_zero(foreign):
                issues.append("Finish bank reconciliation and outstanding receipts/payments in the old company "
                    "on or before the cutover date. Unsettled item: %s on %s. "
                    "These items cannot be carried as a lump balance without losing the items to match."
                    % (line.move_id.display_name, line.account_id.display_name))

        return issues

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
        fiscal_start, issues = self._validation_details()
        try:
            lines = self._source_lines()
        except UserError as exc:
            issues.append(str(exc))
            self._raise_issues("Review these move requirements together:", issues)
        issues.extend(self._bank_settlement_issues(lines))
        lines = self._lines_to_move(lines)
        mappings = {m.source_account_id.id: m for m in self.mapping_ids}
        partners = {m.source_partner_id.id: m for m in self.partner_mapping_ids}
        grouped = defaultdict(lambda: [0.0, 0.0])
        source_arap = defaultdict(float)
        residual_arap = defaultdict(float)
        previous_profit = 0.0
        rows = []
        source_evidence = []
        contact_pool = self._contact_pool()
        proposed = {}
        for line in lines:
            account = line.account_id
            currency = line.currency_id
            source_evidence.append([
                line.id, line.company_id.id, str(line.write_date), line.account_id.id, line.partner_id.id,
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
                    issues.append("Every unpaid customer/vendor item needs a partner. Missing on %s." % line.move_id.display_name)
                mapping = self.env["company.financial.partner.mapping"]
                if line.partner_id:
                    if "on_payment" in line.move_id.invoice_line_ids.tax_ids.flatten_taxes_hierarchy().mapped("tax_exigibility"):
                        issues.append("Unpaid cash-basis tax invoices need a separate accountant-reviewed tax migration. This opening would otherwise lose their future tax recognition: %s." % line.move_id.display_name)
                    mapping = partners.get(line.partner_id.id)
                    if not mapping or (not mapping.target_partner_id and not mapping.create_contact):
                        issues.append("Review the missing or ambiguous destination contact for %s. Name-only matches require an explicit choice; only a tax ID, reference or the same shared record permits automatic reuse." % line.partner_id.display_name)
                        # Keep the account requirement visible even when its contact
                        # is unresolved. Rows are never posted while issues exist.
                        mapping = mapping or self.env["company.financial.partner.mapping"]
                    if mapping.target_partner_id and (not mapping.target_partner_id.active or mapping.target_partner_id.company_id not in (self.env["res.company"], self.target_company_id)):
                        issues.append("The existing destination contact for %s is archived or belongs to another company. Review or reactivate it instead of creating a duplicate." % line.partner_id.display_name)
                    if not mapping.target_partner_id and mapping.create_contact:
                        if self._contact_candidates(line.partner_id, contact_pool):
                            issues.append("An existing or archived contact may match %s. Select and review the existing contact before moving; a duplicate will not be created." % line.partner_id.display_name)
                        for field in ("vat", "ref", "name"):
                            identity = self._identity(line.partner_id[field], field != "name")
                            if not identity:
                                continue
                            key = (field, line.partner_id.is_company, identity)
                            other = proposed.get(key)
                            if other and other != line.partner_id.id:
                                issues.append("Two source contacts may represent %s. Choose an existing destination contact for both, or ask your accountant to resolve their identities first." % line.partner_id.display_name)
                            proposed[key] = line.partner_id.id
                rows.append({
                    "source_account_id": account.id, "source_line_id": line.id,
                    "source_partner_id": line.partner_id.id,
                    "source_date": str(line.date),
                    "target_partner_id": mapping.target_partner_id.id,
                    "currency_id": currency.id, "balance": balance, "amount_currency": amount_currency,
                    "date_maturity": str(line.date_maturity or line.date),
                    "label": " | ".join(filter(None, [line.company_id.name if self.include_source_branches else False, line.move_id.name, line.move_id.ref, line.name or line.partner_id.name])), "kind": "open_item",
                })
            elif account.account_type in PROFIT_TYPES and line.date < fiscal_start:
                previous_profit += line.balance
            else:
                grouped[(account.id, currency.id)][0] += line.balance
                grouped[(account.id, currency.id)][1] += line.amount_currency
        for account_id, total in source_arap.items():
            if not self.currency_id.is_zero(total - residual_arap[account_id]):
                issues.append("Unpaid items do not reconcile to the source customer/vendor control account.")
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
                issues.append("Select a destination account for %s under Review matches." % self.env["account.account"].browse(row["source_account_id"]).display_name)
                continue
            source = mapping.source_account_id
            if mapping.handled_by_stock:
                if source.account_type not in {"asset_current", "asset_non_current"} or row["kind"] == "open_item":
                    issues.append("Only inventory asset balances may be excluded for the stock cutover.")
                row.update({"kind": "stock_excluded", "target_account_id": False})
                excluded += row["balance"]
            else:
                target = mapping.target_account_id
                if not target:
                    issues.append("Select a destination account for %s; missing or ambiguous codes are never guessed." % source.display_name)
                    continue
                issues.extend("%s: %s" % (source.display_name, issue) for issue in
                    self._account_issues(target, self.env["res.currency"].browse(row["currency_id"])))
                if source.account_type != target.account_type:
                    issues.append("%s: Source and destination account types must match; review the destination chart before proceeding." % source.display_name)
                if row["kind"] == "open_item" and not target.reconcile:
                    issues.append("%s: Customer/vendor destination accounts must allow reconciliation." % source.display_name)
                row["target_account_id"] = target.id
            account_evidence.append([
                self._account_signature(source, self.source_company_id),
                self._account_signature(mapping.target_account_id, self.target_company_id), mapping.handled_by_stock,
            ])
        previous_profit = self.currency_id.round(previous_profit)
        if not self.currency_id.is_zero(previous_profit):
            retained = self.retained_earnings_account_id
            if not retained or retained.account_type not in {"equity", "equity_unaffected"}:
                issues.append("Ask your accountant to choose Previous years' earnings account in Accountant setup. This carries the old company's prior-year retained earnings.")
            elif retained:
                issues.extend(self._account_issues(retained, self.currency_id))
            rows.append({"target_account_id": self.retained_earnings_account_id.id, "currency_id": self.currency_id.id,
                "balance": previous_profit, "amount_currency": previous_profit,
                "label": "Prior-year profit/loss brought forward", "kind": "retained"})
        excluded = self.currency_id.round(excluded)
        if not self.currency_id.is_zero(excluded):
            try:
                self._validate_stock_clearing()
            except UserError as exc:
                issues.append(str(exc))
            rows.append({"target_account_id": self.offset_account_id.id, "currency_id": self.currency_id.id,
                "balance": excluded, "amount_currency": excluded,
                "label": "Inventory carried separately by stock cutover", "kind": "stock_clearing"})
        included = [row for row in rows if row["kind"] != "stock_excluded"]
        if not issues and not included and self.copied_invoice_ids:
            issues.append("All selected financial data is already present in the matched destination documents. No additional financial opening is needed. Choose Purchase orders if you still need to copy purchase history.")
        elif not issues and (not included or len(included) > MAX_OPENING_LINES):
            issues.append("The preview must contain between 1 and 10,000 destination journal items.")
        if not issues and not self.currency_id.is_zero(sum(row["balance"] for row in included)):
            issues.append("The generated opening does not balance. No rounding or write-off line is added to hide differences.")
        self._raise_issues("Review these move requirements together:", issues)
        evidence = {
            "companies": [self._signature(c, ("name", "currency_id", "parent_id", "fiscalyear_last_day", "fiscalyear_last_month", "account_fiscal_country_id"))
                for c in (self._source_companies() | self.target_company_id)],
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
            "summary": "%d accounting items checked; %d unpaid customer/vendor items. Opening totals: debit %s / credit %s. These totals must match."
                % (count, sum(row["kind"] == "open_item" for row in rows),
                    formatLang(self.env, debit, currency_obj=self.currency_id),
                    formatLang(self.env, credit, currency_obj=self.currency_id))})
        return True

    def _lock_tables(self):
        return ("account_move", "account_move_line", "account_partial_reconcile", "account_payment",
            "account_account", "account_journal", "res_partner", "res_company", "res_currency",
            "account_tax", "account_tax_repartition_line", "res_currency_rate", "ir_config_parameter",
            "company_financial_cutover", "company_financial_account_mapping",
            "company_financial_partner_mapping", "company_financial_cutover_line")

    def _lock(self):
        self.env.flush_all()
        self.env.cr.execute("SELECT txid_current_snapshot()::text")
        snapshot = self.env.cr.fetchone()[0]
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(SQL("LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE NOWAIT",
                    SQL(", ").join(SQL.identifier(table) for table in self._lock_tables())))
        except LockNotAvailable as exc:
            raise UserError("Accounting is busy. Stop accounting activity and retry during the cutover maintenance window.") from exc
        self._assert_fresh_snapshot(snapshot)
        companies = self._source_companies() | self.target_company_id
        companies.check_access("write")
        # Odoo uses repeatable-read transactions. A real row update forces a
        # concurrent stale request to retry rather than reuse an old snapshot.
        self.env.cr.execute("UPDATE res_company SET write_date = write_date WHERE id = ANY(%s)", [companies.ids])
        self.env.cr.execute("UPDATE company_financial_cutover SET write_date = write_date WHERE id = %s", [self.id])
        self.env.invalidate_all()

    def _assert_fresh_snapshot(self, snapshot):
        """Compare locked tables with a fresh primary snapshot, including deletes.

        PostgreSQL transaction IDs span databases. Inspect actual tuple versions
        here instead of rejecting every cluster commit. Own uncommitted tuple
        versions are excluded: they are intentionally absent from the fresh
        cursor and cannot have been concurrently changed. Table locks keep the
        fresh committed view stable until the cutover transaction completes.
        """
        with self.env.registry.cursor() as fresh:
            fresh.execute("SELECT txid_snapshot_xmax(txid_current_snapshot())")
            anchor = fresh.fetchone()[0]
            # xmin is 32-bit; snapshot visibility/status functions expect the
            # epoch-qualified 64-bit ID. Choose the nearest ID to the current
            # snapshot (normal unfrozen XIDs are less than 2^31 transactions old).
            xid = SQL("xmin::text::bigint + %s + CASE WHEN xmin::text::bigint - %s > 2147483648 "
                "THEN -4294967296 WHEN %s - xmin::text::bigint > 2147483648 "
                "THEN 4294967296 ELSE 0 END", anchor // 4294967296 * 4294967296,
                anchor % 4294967296, anchor % 4294967296)
            for table in self._lock_tables():
                name = SQL.identifier(table)
                fresh.execute(SQL("SELECT EXISTS (SELECT 1 FROM %s WHERE xmin::text::bigint >= 3 "
                    "AND NOT txid_visible_in_snapshot(%s, %s::txid_snapshot))", name, xid, snapshot))
                if fresh.fetchone()[0]:
                    raise ConcurrencyError("Migration data changed before cutover locks; retry the full request.")
                self.env.cr.execute(SQL("SELECT ctid::text, xmin::text FROM %s "
                    "WHERE xmin::text::bigint < 3 OR txid_status(%s) = 'committed'", name, xid))
                while versions := self.env.cr.fetchmany(1000):
                    fresh.execute(SQL("SELECT ctid::text, xmin::text FROM %s WHERE ctid = ANY(%s::tid[])",
                        name, [row[0] for row in versions]))
                    if set(versions) != set(fresh.fetchall()):
                        raise ConcurrencyError("Migration data changed before cutover locks; retry the full request.")

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
        pool = self._contact_pool()
        for mapping in self.partner_mapping_ids.filtered(lambda m: m.source_partner_id.id in source_partner_ids):
            if mapping.target_partner_id:
                contacts[mapping.source_partner_id.id] = mapping.target_partner_id.id
                continue
            source = mapping.source_partner_id
            if self._contact_candidates(source, pool):
                raise UserError("An existing contact now matches %s. Check contacts again before moving." % source.display_name)
            values = {name: source[name] for name in ("name", "company_type", "vat", "street", "street2", "city", "zip", "email", "phone", "website", "lang")}
            values.update({"company_id": self.target_company_id.id,
                "country_id": source.country_id.id, "state_id": source.state_id.id,
                "ref": source.ref, "comment": "Opening contact from %s (%s)." % (self.source_company_id.name, source.id)})
            contact = Partner.create(values)
            for field in pool:
                key = self._identity(contact[field], field != "name")
                if key:
                    pool[field][(contact.is_company, key)] |= contact
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
            if (move.state != "posted" or move.date != self.cutover_date
                or move.company_id != self.target_company_id or move.financial_cutover_id != self):
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
            self._adjust_existing_openings(move)
            self._system_write({"state": "done", "move_id": move.id,
                "completed_at": fields.Datetime.now(), "completed_by": self.env.user.id})
            # No external ID: retain the completion marker across addon reinstall.
            for key in self._completion_keys():
                self.env["ir.config_parameter"].sudo().set_param(key, json.dumps({
                    "move_id": move.id, "cutover_id": self.id, "date": str(self.cutover_date)}))
        return {"type": "ir.actions.act_window", "name": "Completed company move",
            "res_model": self._name, "res_id": self.id, "view_mode": "form"}

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
    create_contact = fields.Boolean(string="Create a new contact",
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
