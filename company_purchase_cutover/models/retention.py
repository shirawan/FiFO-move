"""Purchase snapshots and small per-order updates survive module removal."""
import hashlib
import json
from html import escape

from odoo import models
from odoo.exceptions import UserError
from odoo.addons.company_financial_cutover.models.retention import (
    archive_values, HISTORY_FIELDS, ARCHIVE_PREFIX, DESCRIPTION_PREFIX, purchase_marker_key)

UPDATE_PREFIX = "company_financial_cutover.purchase_update."


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    def _archive_purchase_rows(self):
        return [archive_values(row, HISTORY_FIELDS) for row in self.purchase_history_ids.sorted(lambda row: (row.source_company_id.id, row.source_order_res_id))]

    def _archive_purchase_report(self, rows):
        html = ""
        for history in self.purchase_history_ids.sorted(lambda row: (row.source_company_id.id, row.source_order_res_id)):
            html += "<h3>%s — %s</h3><p>%s; total %s %s; replacement order %s</p>%s" % (
                escape(history.name), escape(history.vendor_name or ""), escape(history.original_status_label),
                history.amount_total, escape(history.currency_id.name), escape(history.target_order_id.name or "None"), history.details_html)
        return html

    def _save_purchase_markers(self):
        for history in self.purchase_history_ids:
            history._save_purchase_marker()

    def _restore_purchase_rows(self, payload):
        History = self.env["company.financial.purchase.history"]
        Parameter = self.env["ir.config_parameter"].sudo()
        for original in payload.get("purchases", []):
            row = dict(original)
            row.setdefault("source_company_id", payload["settings"]["source_company_id"])
            update_key = "%s%s.%s.%s" % (UPDATE_PREFIX, payload["archive_key"], row["source_company_id"], row["source_order_res_id"])
            update = Parameter.search([("key", "=", update_key)])
            if update:
                try:
                    manifest = json.loads(update.value)
                    attachment = self.env["ir.attachment"].sudo().browse(manifest["attachment_id"]).exists()
                    raw = attachment.raw if attachment else b""
                    data = json.loads(raw)
                    if (not raw or hashlib.sha256(raw).hexdigest() != manifest["sha256"]
                        or data["version"] != 1 or data["archive_key"] != payload["archive_key"]
                        or data["source_company_id"] != row["source_company_id"]
                        or data["source_order_res_id"] != row["source_order_res_id"]
                        or data["target_company_id"] != self.target_company_id.id):
                        raise ValueError()
                    row.update({name: data[name] for name in ("replacement_vendor_id", "target_order_id")})
                except (KeyError, TypeError, ValueError):
                    raise UserError("A saved purchase follow-up is missing or changed. Restore it from backup before preparing another replacement.") from None
            for name, model in (("currency_id", "res.currency"), ("source_company_id", "res.company"),
                                ("replacement_vendor_id", "res.partner"), ("target_order_id", "purchase.order")):
                if row.get(name) and not self.env[model].browse(row[name]).exists():
                    raise UserError("An archived purchase references a missing %s. Restore the missing record from backup before retrying recovery." % model)
            existing = History.search([("cutover_id", "=", self.id), ("source_company_id", "=", row["source_company_id"]), ("source_order_res_id", "=", row["source_order_res_id"])])
            if existing:
                current = json.loads(json.dumps(archive_values(existing, HISTORY_FIELDS), sort_keys=True, default=str))
                expected = {name: row.get(name, False) for name in HISTORY_FIELDS}
                if current != expected:
                    raise UserError("The saved purchase history differs from its archived evidence. Ask your administrator to recover it before preparing another replacement.")
            history = existing or History._system_create([{**row, "cutover_id": self.id}])
            if history.target_order_id:
                if history.target_order_id.company_id != self.target_company_id:
                    raise UserError("An archived replacement purchase order belongs to a different company. Ask your administrator to review recovery.")
                link = history.target_order_id.financial_purchase_history_id
                if link and link != history:
                    raise UserError("The archived purchase replacement is already linked to another history. Review recovery before continuing.")
                history.target_order_id._restore_purchase_history_link(history.id)


class PurchaseHistory(models.Model):
    _inherit = "company.financial.purchase.history"

    def _save_purchase_marker(self):
        batch = self.cutover_id.sudo()
        Parameter = self.env["ir.config_parameter"].sudo()
        key = purchase_marker_key(self.source_company_id.id, self.source_order_res_id)
        old = Parameter.search([("key", "=", key)])
        if old:
            marker = batch._read_purchase_marker(old)
            if marker["target_company_id"] != self.company_id.id or marker["archive_key"] != batch.archive_key:
                raise UserError("A purchase order already has a different completed move. No duplicate copy will be created.")
        Parameter.set_param(key, json.dumps({"version": 1, "archive_key": batch.archive_key,
            "target_company_id": self.company_id.id, "source_company_id": self.source_company_id.id,
            "source_order_id": self.source_order_res_id, "target_order_id": self.target_order_id.id}, sort_keys=True))

    def _save_followup(self):
        batch = self.cutover_id.sudo()
        if not batch.archive_key:
            raise UserError("Recover this move's completed archive before preparing a replacement.")
        data = {"version": 1, "archive_key": batch.archive_key,
            "source_company_id": self.source_company_id.id, "source_order_res_id": self.source_order_res_id,
            "target_company_id": self.company_id.id, "replacement_vendor_id": self.replacement_vendor_id.id,
            "target_order_id": self.target_order_id.id}
        raw = json.dumps(data, sort_keys=True).encode()
        key = "%s%s.%s.%s" % (UPDATE_PREFIX, batch.archive_key, self.source_company_id.id, self.source_order_res_id)
        Parameter = self.env["ir.config_parameter"].sudo()
        old = Parameter.search([("key", "=", key)])
        attachment = self.env["ir.attachment"].sudo().browse()
        if old:
            try:
                manifest = json.loads(old.value)
                attachment = self.env["ir.attachment"].sudo().browse(manifest["attachment_id"]).exists()
                if not attachment or hashlib.sha256(attachment.raw).hexdigest() != manifest["sha256"]:
                    raise ValueError()
                previous = json.loads(attachment.raw)
                if not isinstance(previous, dict):
                    raise ValueError()
                if any(previous.get(name) != data[name] for name in ("version", "archive_key", "source_company_id", "source_order_res_id", "target_company_id")):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise UserError("The saved purchase follow-up is missing or changed. Recover it from backup before continuing.") from None
        values = {"name": "Purchase follow-up %s-%s.json" % (self.source_company_id.id, self.source_order_res_id),
            "raw": raw, "mimetype": "application/json", "public": False, "res_model": False, "res_id": False,
            "company_id": self.company_id.id, "description": DESCRIPTION_PREFIX + batch.archive_key + " purchase follow-up"}
        if attachment:
            attachment._write_move_archive(values)
        else:
            attachment = self.env["ir.attachment"].sudo()._create_move_archive(values)
        Parameter.set_param(key, json.dumps({"attachment_id": attachment.id, "sha256": hashlib.sha256(raw).hexdigest()}, sort_keys=True))
        self._save_purchase_marker()

    def action_prepare_draft(self):
        with self.env.cr.savepoint():
            previous = self.target_order_id.id
            result = super().action_prepare_draft()
            if self.target_order_id.id != previous:
                self._save_followup()
            return result

    def _choose_vendor(self, vendor):
        with self.env.cr.savepoint():
            result = super()._choose_vendor(vendor)
            self._save_followup()
            return result
