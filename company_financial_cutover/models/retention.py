"""Native attachments and markers that remain when this addon's tables go away."""
import hashlib
import json
from html import escape
from uuid import uuid4

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


ARCHIVE_PREFIX = "company_financial_cutover.archive."
PURCHASE_PREFIX = "company_financial_cutover.purchase."
DESCRIPTION_PREFIX = "FiFO-move protected archive: "
_ARCHIVE_TOKEN = object()

BASE_FIELDS = ("source_company_id", "target_company_id", "cutover_date", "journal_id",
    "retained_earnings_account_id", "offset_account_id", "include_financial", "include_purchase_history")
AUDIT_FIELDS = ("name", "state", "snapshot_hash", "move_id", "completed_at", "completed_by", "summary", "check_status", "check_report")
LINE_FIELDS = ("sequence", "source_account_id", "target_account_id", "source_line_id", "source_date",
    "source_partner_id", "target_partner_id", "currency_id", "balance", "amount_currency", "date_maturity", "label", "kind", "posted_line_id")
HISTORY_FIELDS = ("source_order_res_id", "name", "vendor_name", "original_state", "source_company_name",
    "order_date", "currency_id", "amount_total", "snapshot", "target_order_id", "replacement_vendor_id")
OPENING_FIELDS = ("name", "ref", "date", "company_id", "currency_id", "journal_id", "move_type")
OPENING_LINE_FIELDS = ("sequence", "name", "account_id", "partner_id", "date", "date_maturity",
    "debit", "credit", "balance", "amount_currency", "currency_id", "company_id", "display_type",
    "tax_line_id", "tax_repartition_line_id", "analytic_distribution")


def archive_values(record, names):
    return {name: record[name].id if record._fields[name].type == "many2one" else record[name] for name in names}


def purchase_marker_key(source_company, source_order):
    return "%s%s.%s" % (PURCHASE_PREFIX, source_company, source_order)


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    archive_key = fields.Char(readonly=True, copy=False)
    archive_attachment_id = fields.Many2one("ir.attachment", readonly=True, copy=False, ondelete="restrict")
    report_attachment_id = fields.Many2one("ir.attachment", readonly=True, copy=False, ondelete="restrict")
    _unique_archive = models.Constraint("UNIQUE(archive_key)", "This completed move already has an archive.")

    def action_apply(self):
        with self.env.cr.savepoint():
            result = super().action_apply()
            self._save_durable_archive()
            return result

    def action_import_purchases(self):
        with self.env.cr.savepoint():
            result = super().action_import_purchases()
            self._save_durable_archive()
            return result

    def _archive_payload(self):
        return {
            "version": 1, "archive_key": self.archive_key,
            "companies": {"old": self.source_company_id.name, "new": self.target_company_id.name},
            "settings": archive_values(self, BASE_FIELDS), "audit": archive_values(self, AUDIT_FIELDS),
            "accounts": [archive_values(row, ("source_account_id", "target_account_id", "handled_by_stock")) for row in self.mapping_ids],
            "contacts": [archive_values(row, ("source_partner_id", "target_partner_id", "create_contact")) for row in self.partner_mapping_ids],
            "lines": [archive_values(row, LINE_FIELDS) for row in self.line_ids],
            "purchases": [archive_values(row, HISTORY_FIELDS) for row in self.purchase_history_ids],
            "opening": self._opening_evidence(self.move_id) if self.move_id else False,
        }

    def _opening_evidence(self, move):
        evidence = {"id": move.id, **archive_values(move, OPENING_FIELDS),
            "lines": [{"id": line.id, **archive_values(line, OPENING_LINE_FIELDS),
                "tax_ids": sorted(line.tax_ids.ids), "tax_tag_ids": sorted(line.tax_tag_ids.ids)}
                for line in move.line_ids.sorted("id")]}
        # Normalize dates to the exact representation saved in JSON. Payment
        # reconciliation fields are intentionally excluded from this evidence.
        return json.loads(json.dumps(evidence, sort_keys=True, default=str))

    def _validate_archived_opening(self, payload):
        settings, audit = payload["settings"], payload["audit"]
        move = self.env["account.move"].browse(audit["move_id"]).exists()
        if not settings["include_financial"]:
            if move:
                raise UserError("A purchase-only archive unexpectedly references a financial opening. Ask your administrator to review recovery.")
            return
        changed = (not move or move.state != "posted" or move.move_type != "entry"
            or move.company_id.id != settings["target_company_id"] or move.journal_id.id != settings["journal_id"]
            or str(move.date) != settings["cutover_date"])
        if not changed and payload.get("opening"):
            changed = self._opening_evidence(move) != payload["opening"]
        elif not changed:
            # Older archives have no full native signature. Validate everything
            # they did capture instead of accepting any posted entry by ID.
            rows = sorted((row for row in payload["lines"] if row["kind"] != "stock_excluded"), key=lambda row: row["sequence"])
            posted = {line.id: line for line in move.line_ids}
            changed = set(posted) != {row["posted_line_id"] for row in rows}
            for sequence, row in enumerate(rows, 1):
                line = posted.get(row["posted_line_id"])
                if not line:
                    changed = True
                    break
                if (line.account_id.id != row["target_account_id"] or line.partner_id.id != row["target_partner_id"]
                    or line.currency_id.id != row["currency_id"] or line.name != row["label"]
                    or line.sequence != sequence or str(line.date_maturity or False) != str(row["date_maturity"])
                    or not move.company_id.currency_id.is_zero(line.balance - row["balance"])
                    or not line.currency_id.is_zero(line.amount_currency - row["amount_currency"])):
                    changed = True
                    break
        if changed:
            raise UserError("The archived financial opening has changed or is missing. Ask your accountant to review its date, journal and journal items before restoring migration history. Nothing was reposted.")

    def _save_durable_archive(self):
        self.ensure_one()
        if self.state != "done":
            raise UserError("Only completed moves can be archived.")
        if not self.archive_key:
            self._system_write({"archive_key": str(uuid4())})
        payload = self._archive_payload()
        raw = json.dumps(payload, sort_keys=True, default=str).encode()
        html = ("<!doctype html><html><meta charset='utf-8'><title>Completed company move</title><body>"
            "<h1>Completed company move: %s</h1><p>%s → %s</p><p>%s</p>"
            % tuple(escape(str(v)) for v in (self.name, self.source_company_id.name, self.target_company_id.name, self.summary or "")))
        html += "<h2>Financial amounts</h2><table><tr><th>Description</th><th>Amount</th><th>Currency</th></tr>"
        for line in self.line_ids:
            html += "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(escape(str(v)) for v in (line.label, line.balance, self.currency_id.name))
        html += "</table><h2>Purchase history</h2>"
        for history in self.purchase_history_ids:
            html += "<h3>%s — %s</h3><p>%s; total %s %s; replacement order %s</p>%s" % (
                escape(history.name), escape(history.vendor_name or ""), escape(history.original_status_label),
                history.amount_total, escape(history.currency_id.name), escape(history.target_order_id.name or "None"), history.details_html)
        html = (html + "</body></html>").encode()
        Attachment = self.env["ir.attachment"].sudo()
        for field, content, suffix, mimetype in (
            ("archive_attachment_id", raw, "recovery.json", "application/json"),
            ("report_attachment_id", html, "report.html", "text/html"),
        ):
            values = {"name": "%s-%s" % (self.name.replace("/", "-"), suffix),
                "raw": content, "mimetype": mimetype, "public": False,
                # Standalone native attachments are administrator/creator-only.
                # Neither their data nor their access depends on custom models.
                "res_model": False, "res_id": False, "company_id": self.target_company_id.id,
                "description": DESCRIPTION_PREFIX + self.archive_key}
            attachment = self[field].sudo()
            if attachment:
                attachment._write_move_archive(values)
            else:
                attachment = Attachment._create_move_archive(values)
                self._system_write({field: attachment.id})
        Parameter = self.env["ir.config_parameter"].sudo()
        manifest = {"version": 1, "archive_key": self.archive_key,
            "attachment_id": self.archive_attachment_id.id, "sha256": hashlib.sha256(raw).hexdigest(),
            "report_id": self.report_attachment_id.id, "report_sha256": hashlib.sha256(html).hexdigest(),
            "target_company_id": self.target_company_id.id, "source_company_id": self.source_company_id.id}
        Parameter.set_param(ARCHIVE_PREFIX + self.archive_key, json.dumps(manifest, sort_keys=True))
        for history in self.purchase_history_ids:
            key = purchase_marker_key(self.source_company_id.id, history.source_order_res_id)
            old = Parameter.search([("key", "=", key)])
            if old:
                marker = self._read_purchase_marker(old)
                if marker["target_company_id"] != self.target_company_id.id or marker["archive_key"] != self.archive_key:
                    raise UserError("A purchase order already has a different completed move. No duplicate copy will be created.")
            Parameter.set_param(key, json.dumps({"version": 1, "archive_key": self.archive_key,
                "target_company_id": self.target_company_id.id, "source_company_id": self.source_company_id.id,
                "source_order_id": history.source_order_res_id, "target_order_id": history.target_order_id.id}, sort_keys=True))

    def _read_purchase_marker(self, parameter):
        try:
            value = json.loads(parameter.value)
            if value["version"] != 1 or not value["archive_key"] or not isinstance(value["target_company_id"], int):
                raise ValueError()
            return value
        except (KeyError, ValueError, TypeError):
            raise UserError("An existing purchase migration marker is unreadable. Ask your administrator to recover its archive; no duplicate will be created.") from None

    def _load_durable_archive(self, parameter):
        try:
            manifest = json.loads(parameter.value)
            attachment = self.env["ir.attachment"].sudo().browse(manifest["attachment_id"]).exists()
            raw = attachment.raw if attachment else b""
            if (not raw or hashlib.sha256(raw).hexdigest() != manifest["sha256"]
                or manifest["version"] != 1 or parameter.key != ARCHIVE_PREFIX + manifest["archive_key"]):
                raise ValueError()
            payload = json.loads(raw)
            if (payload["version"] != 1 or payload["archive_key"] != manifest["archive_key"]
                or payload["settings"]["target_company_id"] != manifest["target_company_id"]
                or payload["settings"]["source_company_id"] != manifest["source_company_id"]
                or payload["audit"]["state"] != "done"):
                raise ValueError()
            return manifest, payload
        except (KeyError, ValueError, TypeError):
            raise UserError("A saved migration archive is missing or changed. Ask your administrator to restore the archive from backup before reinstalling or copying data again.") from None

    def _archive_completed(self):
        companies = self.env["res.company"].sudo().search([])
        batches = self.sudo().with_context(allowed_company_ids=companies.ids).search([("state", "=", "done")])
        for batch in batches:
            batch._save_durable_archive()

    def _restore_archives(self):
        if not self.env.is_system():
            raise AccessError("Only the administrator can restore migration archives.")
        Parameter = self.env["ir.config_parameter"].sudo()
        for parameter in Parameter.search([("key", "=like", ARCHIVE_PREFIX + "%")], order="id"):
            manifest, payload = self._load_durable_archive(parameter)
            settings = payload["settings"]
            companies = self.env["res.company"].sudo().browse([settings["source_company_id"], settings["target_company_id"]]).exists()
            if len(companies) != 2:
                raise UserError("A company referenced by a completed migration archive no longer exists. Keep its archive and ask your administrator to review recovery.")
            Cutover = self.sudo().with_context(allowed_company_ids=companies.ids)
            batch = Cutover.search([("archive_key", "=", payload["archive_key"])])
            if batch:
                continue
            Cutover._validate_archived_opening(payload)
            batch = Cutover.create({key: settings[key] for key in BASE_FIELDS})
            for name, model, rows in (
                ("accounts", "company.financial.account.mapping", payload["accounts"]),
                ("contacts", "company.financial.partner.mapping", payload["contacts"]),
            ):
                for row in rows:
                    batch.env[model].create({**row, "cutover_id": batch.id})
            if payload["lines"]:
                batch.env["company.financial.cutover.line"]._system_create([
                    {**row, "cutover_id": batch.id} for row in payload["lines"]])
            histories = batch.env["company.financial.purchase.history"]._system_create([
                {**row, "cutover_id": batch.id} for row in payload["purchases"]]) if payload["purchases"] else batch.purchase_history_ids
            audit = {key: payload["audit"][key] for key in AUDIT_FIELDS}
            batch._system_write({**audit, "archive_key": payload["archive_key"],
                "archive_attachment_id": manifest["attachment_id"], "report_attachment_id": manifest["report_id"]})
            if batch.move_id:
                batch.move_id._restore_financial_cutover_link(batch.id)
            for history in histories.filtered("target_order_id"):
                if history.target_order_id.company_id != batch.target_company_id:
                    raise UserError("An archived replacement purchase order belongs to a different company. Ask your administrator to review recovery.")
                history.target_order_id._restore_purchase_history_link(history.id)

    def action_download_move_report(self):
        self.ensure_one()
        self._operator()
        if not self.report_attachment_id:
            self._save_durable_archive()
        return {"type": "ir.actions.act_url", "url": "/web/content/%s?download=1" % self.report_attachment_id.id, "target": "self"}


class PurchaseHistory(models.Model):
    _inherit = "company.financial.purchase.history"

    def action_prepare_draft(self):
        with self.env.cr.savepoint():
            result = super().action_prepare_draft()
            self.cutover_id._save_durable_archive()
            return result

    def _choose_vendor(self, vendor):
        with self.env.cr.savepoint():
            result = super()._choose_vendor(vendor)
            self.cutover_id._save_durable_archive()
            return result


class Attachment(models.Model):
    _inherit = "ir.attachment"

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get("_fifo_archive_token") is not _ARCHIVE_TOKEN and any(
            str(values.get("description") or "").startswith(DESCRIPTION_PREFIX) for values in values_list):
            raise AccessError("Migration archives are system-managed.")
        return super().create(values_list)

    def write(self, values):
        if self.env.context.get("_fifo_archive_token") is not _ARCHIVE_TOKEN and (
            any(str(row.description or "").startswith(DESCRIPTION_PREFIX) for row in self)
            or str(values.get("description") or "").startswith(DESCRIPTION_PREFIX)):
            raise AccessError("Keep completed migration archives unchanged.")
        return super().write(values)

    def unlink(self):
        if any(str(row.description or "").startswith(DESCRIPTION_PREFIX) for row in self):
            raise AccessError("Keep completed migration archives. They protect against duplicates after reinstalling.")
        return super().unlink()

    def _create_move_archive(self, values):
        return self.with_context(_fifo_archive_token=_ARCHIVE_TOKEN).create(values).with_env(self.env)

    def _write_move_archive(self, values):
        return self.with_context(_fifo_archive_token=_ARCHIVE_TOKEN).write(values)
