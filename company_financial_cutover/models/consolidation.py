"""Explicit branch scope and reviewed overlap in an already-used destination."""
import hashlib
import json
from collections import defaultdict
from html import escape

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import formatLang

from .cutover import OPEN_ITEM_TYPES


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    include_source_branches = fields.Boolean(string="Include old branches", default=False,
        help="Combine the old company and all its branches, including archived branches. The new company remains standalone.")
    source_scope_description = fields.Char(compute="_compute_source_scope")
    completed_source_ids = fields.Json(readonly=True, copy=False)
    destination_mode = fields.Selection([("fresh", "New company has no accounting yet"),
        ("existing", "New company is already in use")], required=True, default="fresh", string="New company status")
    existing_data_reviewed = fields.Boolean(string="Accountant has reviewed existing data", copy=False,
        help="Confirm that all previously transferred balances are identified below. Other destination records must be separate new activity. The mover cannot infer this from amounts alone.")
    prior_opening_move_ids = fields.Many2many("account.move", "financial_cutover_prior_opening_rel",
        "cutover_id", "move_id", string="Old opening entries already entered here", copy=False,
        help="Select only general journal entries that imported the old company's balances. They will receive a posted reversal on the balance date. Existing invoices, bills and new trading entries are preserved.")
    destination_balance_preview = fields.Json(readonly=True, copy=False)
    destination_balance_html = fields.Html(compute="_compute_destination_balance_html", sanitize=True)
    copied_invoice_ids = fields.One2many("company.financial.copied.invoice", "cutover_id", copy=False,
        string="Invoices and bills already copied here")
    correction_move_ids = fields.Many2many("account.move", "financial_cutover_correction_rel",
        "cutover_id", "move_id", readonly=True, copy=False, string="Adjustments to earlier openings")

    @api.depends("source_company_id", "include_source_branches", "completed_source_ids", "state")
    def _compute_source_scope(self):
        for batch in self:
            batch.source_scope_description = "Included: " + ", ".join(batch._source_companies().mapped("name"))

    def _source_companies(self):
        self.ensure_one()
        Company = self.env["res.company"].sudo().with_context(active_test=False)
        if self.state == "done" and self.completed_source_ids:
            return Company.browse(self.completed_source_ids).exists().with_env(self.env)
        if self.include_source_branches and self.source_company_id:
            return Company.search([("id", "child_of", self.source_company_id.id)], order="id").with_env(self.env)
        return self.source_company_id

    def _completion_keys(self):
        return ["company_financial_cutover.completed.source.%s" % company.id for company in self._source_companies()]

    def _scope_evidence(self):
        return [self.include_source_branches, [self._signature(company,
            ("name", "currency_id", "parent_id", "active", "fiscalyear_last_day", "fiscalyear_last_month"))
            for company in self._source_companies()]]

    def write(self, vals):
        # Company/data-scope changes always require a new overlap decision.
        if {"source_company_id", "target_company_id", "include_source_branches", "destination_mode"}.intersection(vals):
            vals = {**vals, "existing_data_reviewed": False}
        if "target_company_id" in vals:
            vals = {**vals, "prior_opening_move_ids": [(5, 0, 0)]}
            self.copied_invoice_ids.unlink()
        return super().write(vals)

    def _validation_details(self):
        fiscal_start, issues = super()._validation_details()
        for company in self._source_companies():
            if company.currency_id != self.currency_id:
                issues.append("%s uses a different accounting currency. A combined opening needs the same currency." % company.name)
            if company.compute_fiscalyear_dates(self.cutover_date)["date_from"] != fiscal_start:
                issues.append("%s has a different financial year. Review its prior-year earnings separately." % company.name)
        if self.destination_mode == "fresh" and (self.prior_opening_move_ids or self.copied_invoice_ids):
            issues.append("Choose New company is already in use before selecting previously transferred data.")
        if self.destination_mode == "existing":
            if not self.existing_data_reviewed:
                issues.append("Review Existing data below with your accountant, identify earlier openings and copied invoices, then confirm the review. New trading activity will be preserved.")
            issues.extend(self._overlap_issues())
        return fiscal_start, issues

    def _overlap_issues(self):
        issues = []
        for move in self.prior_opening_move_ids:
            if (move.company_id != self.target_company_id or move.state != "posted" or move.move_type != "entry"
                    or move.journal_id.type != "general" or move.date > self.cutover_date):
                issues.append("%s: select a posted Miscellaneous opening entry in the new company dated on or before the balance date." % move.display_name)
            if (move.financial_cutover_id or move.origin_payment_id or move.reversed_entry_id
                    or move.reversal_move_ids.filtered(lambda reversal: reversal.state == "posted")
                    or move.line_ids.tax_ids or move.line_ids.tax_tag_ids or move.line_ids.tax_line_id
                    or move.tax_cash_basis_origin_move_id or move.tax_cash_basis_rec_id):
                issues.append("%s: this entry is a payment, tax entry, protected migration or already reversed. It cannot be treated as an earlier opening." % move.display_name)
            if any(line.currency_id != self.currency_id for line in move.line_ids):
                issues.append("%s: an earlier opening in foreign currency needs a separate accountant-reviewed adjustment." % move.display_name)
        for pair in self.copied_invoice_ids:
            source, target = pair.source_move_id, pair.target_move_id
            prefix = "%s → %s: " % (source.display_name, target.display_name)
            if (source.company_id not in self._source_companies() or target.company_id != self.target_company_id
                    or source.state != "posted" or target.state != "posted"
                    or source.move_type not in {"out_invoice", "in_invoice", "out_refund", "in_refund"}
                    or source.move_type != target.move_type or source.date > self.cutover_date
                    or target.date != source.date or target.invoice_date != source.invoice_date
                    or target.currency_id != source.currency_id
                    or not source.currency_id.is_zero(source.amount_total - target.amount_total)):
                issues.append(prefix + "choose the same posted invoice/bill type, dates, currency and total in each company.")
                continue
            if source.line_ids.filtered(lambda line: line.account_id.account_type in OPEN_ITEM_TYPES
                    and (line.matched_debit_ids | line.matched_credit_ids).filtered(lambda partial: partial.max_date <= self.cutover_date)):
                issues.append(prefix + "the old invoice has payments at the balance date. Its copied payments need a separate accountant-reviewed adjustment; it cannot be skipped by itself.")
            tax_fields = ("amount", "amount_type", "tax_exigibility", "price_include")
            source_taxes = sorted(tuple(tax[name] for name in tax_fields) for tax in source.invoice_line_ids.tax_ids.flatten_taxes_hierarchy())
            target_taxes = sorted(tuple(tax[name] for name in tax_fields) for tax in target.invoice_line_ids.tax_ids.flatten_taxes_hierarchy())
            if source_taxes != target_taxes:
                issues.append(prefix + "the copied taxes have different rates or payment recognition. Review the native copy before excluding it.")
            accounts = {row.source_account_id.id: row.target_account_id.id for row in self.mapping_ids if not row.handled_by_stock}
            contacts = {row.source_partner_id.id: row.target_partner_id.id for row in self.partner_mapping_ids if row.target_partner_id}
            expected = self._ledger_signature(source.line_ids, accounts, contacts)
            actual = self._ledger_signature(target.line_ids)
            if expected != actual or not expected:
                issues.append(prefix + "the posted accounts, contacts and amounts do not match your choices. Review the copied document; it will not be silently skipped.")
        if self.journal_id and self.target_company_id._get_violated_lock_dates(self.cutover_date, False, self.journal_id):
            issues.append("The new company's books are locked on the balance date. Ask your accountant to approve a valid posting date before moving.")
        return issues

    def _ledger_signature(self, lines, accounts=None, contacts=None):
        totals = defaultdict(lambda: [0.0, 0.0])
        for line in lines.filtered(lambda line: line.display_type not in {"line_section", "line_subsection", "line_note"}):
            account = accounts.get(line.account_id.id, False) if accounts is not None else line.account_id.id
            # All invoice lines normally carry the invoice contact. Mapping also
            # applies to P&L/tax lines, so a copied document retains its identity.
            partner = contacts.get(line.partner_id.id, False) if contacts is not None else line.partner_id.id
            if not account or (line.partner_id and not partner):
                return False
            values = totals[(account, partner, line.currency_id.id)]
            values[0] += line.balance
            values[1] += line.amount_currency
        return sorted((key, self.currency_id.round(value[0]), self.env["res.currency"].browse(key[2]).round(value[1]))
            for key, value in totals.items())

    def _lines_to_move(self, lines):
        if self.destination_mode != "existing":
            return lines
        return lines.filtered(lambda line: line.move_id not in self.copied_invoice_ids.source_move_id)

    def _destination_totals(self):
        return self.env["account.move.line"]._read_group([
            ("company_id", "=", self.target_company_id.id), ("parent_state", "=", "posted"),
            ("date", "<=", self.cutover_date)], ["account_id"], ["balance:sum"])

    def _overlap_evidence(self):
        moves = self.prior_opening_move_ids | self.copied_invoice_ids.source_move_id | self.copied_invoice_ids.target_move_id
        return [self.destination_mode, self.existing_data_reviewed,
            [(account.id, self.currency_id.round(balance)) for account, balance in sorted(self._destination_totals(), key=lambda row: row[0].id)]
                if self.include_financial and self.destination_mode == "existing" else [], [
            [self._signature(move, ("name", "ref", "state", "company_id", "date", "invoice_date", "move_type", "currency_id", "journal_id", "reversed_entry_id")),
             [self._signature(line, ("name", "account_id", "partner_id", "balance", "amount_currency", "currency_id", "date_maturity", "tax_ids", "tax_tag_ids", "amount_residual", "amount_residual_currency", "matched_debit_ids", "matched_credit_ids"))
                for line in move.line_ids.sorted("id")]] for move in moves.sorted("id")]]

    def _plan(self):
        rows, digest, count = super()._plan()
        digest = hashlib.sha256(json.dumps([digest, self._scope_evidence(), self._overlap_evidence()],
            sort_keys=True, default=str).encode()).hexdigest()
        return rows, digest, count

    def _review_notices(self, lines):
        notices = super()._review_notices(lines)
        if self.include_source_branches:
            notices.append(self.source_scope_description + ". Balances are combined; unpaid items and purchase histories retain their original company. Check any balances between the old branches with your accountant; no intercompany elimination is guessed.")
        if self.include_financial and self.destination_mode == "existing":
            notices.append("The new company is already in use. Its invoices, bills, payments and new trading entries stay in place. Only the earlier opening entries you select receive reversals; explicitly matched copied invoices/bills are omitted from this opening.")
            # Candidate detection is advisory. Same totals/references are not
            # enough to decide that two legitimate transactions are duplicates.
            source_moves = lines.move_id.filtered(lambda move: move.is_invoice(include_receipts=False))
            keys = {(move.move_type, self._identity(move.ref or move.name, True), move.currency_id.id, move.amount_total): move for move in source_moves}
            targets = self.env["account.move"].search([("company_id", "=", self.target_company_id.id),
                ("state", "=", "posted"), ("move_type", "in", list({move.move_type for move in source_moves}))], limit=100001)
            candidates = []
            for target in targets:
                key = (target.move_type, self._identity(target.ref or target.name, True), target.currency_id.id, target.amount_total)
                if key in keys and target not in self.copied_invoice_ids.target_move_id:
                    candidates.append("%s / %s" % (keys[key].display_name, target.display_name))
            if candidates:
                notices.append("Possible invoices/bills already copied here: " + ", ".join(candidates[:20]) + ". Match confirmed copies under Existing data. Similar references alone never skip a balance.")
            if len(targets) > 100000:
                notices.append("More than 100,000 destination invoices/bills exist; the candidate scan is partial. Review the complete ledger before confirming existing data.")
        return notices

    def _adjust_existing_openings(self, opening):
        if self.destination_mode != "existing" or not self.prior_opening_move_ids:
            return
        reversals = self.env["account.move"]
        for old in self.prior_opening_move_ids.with_company(self.target_company_id).sorted("id"):
            reverse = old._reverse_moves([{"date": self.cutover_date, "journal_id": self.journal_id.id,
                "ref": "%s | Replace earlier opening %s" % (self.name, old.display_name)}], cancel=False)
            reverse._restore_financial_cutover_link(self.id)
            reverse.action_post()
            if reverse.state != "posted" or reverse.date != self.cutover_date:
                raise UserError("Odoo could not post the reviewed adjustment on the balance date. Nothing was moved.")
            expected = self._ledger_signature(old.line_ids)
            actual = self._ledger_signature(reverse.line_ids)
            if actual != [(key, -balance, -foreign) for key, balance, foreign in expected]:
                raise UserError("An adjustment differs from the selected earlier opening. Nothing was moved.")
            reversals |= reverse
            # Preserve all prior payment matches. Reconcile only the old
            # opening with its opposite; any remainder offsets the carried item.
            self._reconcile_opening_groups(old.line_ids | reverse.line_ids)
        self._reconcile_opening_groups(reversals.line_ids | opening.line_ids, require_reversal=reversals)
        self._system_write({"correction_move_ids": [(6, 0, reversals.ids)]})

    def _reconcile_opening_groups(self, lines, require_reversal=None):
        grouped = defaultdict(lambda: self.env["account.move.line"])
        for line in lines.filtered(lambda line: line.account_id.account_type in OPEN_ITEM_TYPES and not line.reconciled):
            grouped[(line.account_id.id, line.partner_id.id, line.currency_id.id)] |= line
        for group in grouped.values():
            if require_reversal and not (group.move_id & require_reversal):
                continue
            if len(group) > 1 and any(line.amount_residual > 0 for line in group) and any(line.amount_residual < 0 for line in group):
                group.reconcile()

    def action_preview(self):
        result = super().action_preview()
        if self.include_financial and self.destination_mode == "existing":
            details = "\nExisting data: %s earlier opening entries will receive posted reversals; %s matched invoices/bills are already present and are excluded. Other new-company records stay in place." % (len(self.prior_opening_move_ids), len(self.copied_invoice_ids))
            if self.prior_opening_move_ids:
                details += "\nEarlier openings to reverse: " + ", ".join(self.prior_opening_move_ids.mapped("display_name"))
            before = {account.id: balance for account, balance in self._destination_totals()}
            added, removed = defaultdict(float), defaultdict(float)
            for row in self.line_ids.filtered(lambda line: line.kind != "stock_excluded"):
                added[row.target_account_id.id] += row.balance
            for line in self.prior_opening_move_ids.line_ids:
                removed[line.account_id.id] += line.balance
            preview = []
            for account_id in sorted(set(added) | set(removed)):
                account = self.env["account.account"].browse(account_id).with_company(self.target_company_id)
                preview.append({"account": account.display_name, "before": self.currency_id.round(before.get(account_id, 0)),
                    "added": self.currency_id.round(added[account_id]), "removed": self.currency_id.round(-removed[account_id]),
                    "after": self.currency_id.round(before.get(account_id, 0) + added[account_id] - removed[account_id])})
            self._system_write({"summary": (self.summary or "") + details, "destination_balance_preview": preview})
        return result

    @api.depends("destination_balance_preview", "currency_id")
    def _compute_destination_balance_html(self):
        for batch in self:
            rows = batch.destination_balance_preview or []
            body = "".join("<tr><td>%s</td>%s</tr>" % (escape(row["account"]),
                "".join("<td class='text-end'>%s</td>" % escape(formatLang(batch.env, row[key], currency_obj=batch.currency_id))
                    for key in ("before", "added", "removed", "after"))) for row in rows)
            batch.destination_balance_html = ("<p>Posted balances at the balance date, for the accounts this move changes. "
                "Transactions after that date stay in place and are outside these totals.</p>"
                "<div class='table-responsive'><table class='table table-sm'><thead><tr><th>Account</th>"
                "<th>Already here</th><th>Carried opening</th><th>Earlier opening adjustment</th><th>After this move</th>"
                "</tr></thead><tbody>" + body + "</tbody></table></div>") if rows else False

    def _invalidate_preview(self):
        super()._invalidate_preview()
        self._system_write({"destination_balance_preview": False})

    def action_apply(self):
        with self.env.cr.savepoint():
            source_ids = self._source_companies().ids
            result = super().action_apply()
            self._system_write({"completed_source_ids": source_ids})
            return result

    def _lock_tables(self):
        return super()._lock_tables() + ("company_financial_copied_invoice", "financial_cutover_prior_opening_rel", "financial_cutover_correction_rel")


class CopiedInvoice(models.Model):
    _name = "company.financial.copied.invoice"
    _inherit = "company.financial.mapping.mixin"
    _description = "Reviewed invoice already present in the new company"

    source_move_id = fields.Many2one("account.move", required=True, ondelete="restrict", string="Old invoice or bill")
    target_move_id = fields.Many2one("account.move", required=True, ondelete="restrict", string="Existing copy in new company")
    _unique_source = models.Constraint("UNIQUE(cutover_id, source_move_id)", "Choose each old invoice/bill only once.")
    _unique_target = models.Constraint("UNIQUE(cutover_id, target_move_id)", "An existing invoice/bill can match only one old document.")
