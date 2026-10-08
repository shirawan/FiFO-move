"""Destination purchase history and explicitly prepared, unconfirmed RFQs."""
import hashlib
import json
from decimal import Decimal
from html import escape

from psycopg2.errors import LockNotAvailable
from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_is_zero, float_repr, float_round


MAX_PURCHASE_ORDERS = 10000
MAX_PURCHASE_LINES = 50000
_PURCHASE_CREATE_TOKEN = object()


def purchase_status_label(state, env):
    return {
        "draft": env._("Draft order"), "sent": env._("Sent for quotation"),
        "to approve": env._("Waiting for approval"), "purchase": env._("Confirmed order"),
        "done": env._("Completed"), "cancel": env._("Cancelled"),
    }.get(state, str(state or "").replace("_", " ").capitalize())


def purchase_draft_advice(snapshot, env):
    lines = [line for line in snapshot.get("lines", []) if not line.get("display_type")]
    if snapshot.get("state") == "cancel":
        return False, env._("This order was cancelled. Keep it as history; there is no replacement to prepare.")
    if snapshot.get("dropship"):
        return False, env._("This order delivers directly to a customer. Ask your purchase manager to review any remaining work.")
    if not lines:
        return False, env._("This order has no product lines. Keep it as history.")
    if any(line.get("downpayment") for line in lines):
        return False, env._("This order includes a down payment. Ask your purchase manager to review any remaining work.")
    if any(not float_is_zero(line[key], precision_digits=snapshot.get("unit_decimals", 2))
            for line in lines for key in ("received", "billed")):
        return False, env._("Some quantities were already received or billed. Review the remaining quantities below with your purchase manager.")
    return True, env._("Nothing was received or billed when this history was copied. You can prepare a draft in the new company, then review its products, vendor and taxes. Cancel the old order before confirming the replacement.")


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    include_purchase_history = fields.Boolean(string="Include all purchase orders", default=True,
        help="Copy all source orders, including cancelled and completed orders, as read-only history at preview time. No receipts or bills are recreated.")
    purchase_preview_data = fields.Json(readonly=True, copy=False)
    purchase_preview_html = fields.Html(compute="_compute_purchase_preview", sanitize=True)
    purchase_history_ids = fields.One2many("company.financial.purchase.history", "cutover_id", readonly=True, copy=False)
    purchase_history_preview_ready = fields.Boolean(readonly=True, copy=False)

    def _invalidate_preview(self):
        super()._invalidate_preview()
        self._system_write({"purchase_preview_data": False, "purchase_history_preview_ready": False})

    def _purchase_row(self, order):
        return {
            "id": order.id, "name": order.name, "vendor_id": order.partner_id.id,
            "vendor": order.partner_id.display_name, "vendor_ref": order.partner_ref or "",
            "state": order.state, "locked": order.locked, "date_order": str(order.date_order),
            "date_approve": str(order.date_approve or ""), "currency_id": order.currency_id.id,
            "untaxed": order.amount_untaxed, "tax": order.amount_tax, "total": order.amount_total,
            "currency_decimals": order.currency_id.decimal_places,
            "note": str(order.note or ""), "origin": order.origin or "",
            "dropship": bool(order.dest_address_id),
            "bills": [{"id": bill.id, "name": bill.name, "state": bill.state} for bill in order.invoice_ids.sorted("id")],
            "unit_decimals": self.env["decimal.precision"].precision_get("Product Unit"),
            "lines": [{"id": line.id, "sequence": line.sequence, "display_type": line.display_type,
                "product_id": line.product_id.id, "product": line.product_id.display_name,
                "name": line.name, "qty": line.product_qty, "received": line.qty_received,
                "billed": line.qty_invoiced, "uom_id": line.product_uom_id.id,
                "unit": line.product_uom_id.display_name, "price": line.price_unit,
                "discount": line.discount, "subtotal": line.price_subtotal, "total": line.price_total,
                "taxes": line.tax_ids.mapped("display_name"), "date_planned": str(line.date_planned or ""),
                "downpayment": line.is_downpayment} for line in order.order_line.sorted("id")],
        }

    def _purchase_plan(self):
        if not self.include_purchase_history:
            return []
        if not self.env.user.has_group("purchase.group_purchase_user"):
            raise AccessError("Purchase user access is also required when including purchase orders.")
        orders = self.env["purchase.order"].with_company(self.source_company_id).search([
            ("company_id", "=", self.source_company_id.id)], order="id", limit=MAX_PURCHASE_ORDERS + 1)
        if len(orders) > MAX_PURCHASE_ORDERS or len(orders.order_line) > MAX_PURCHASE_LINES:
            raise UserError("Purchase migration is limited to 10,000 orders and 50,000 lines per cutover.")
        existing = self.env["company.financial.purchase.history"].sudo().search([
            ("company_id", "=", self.target_company_id.id), ("source_company_id", "=", self.source_company_id.id),
            ("source_order_res_id", "in", orders.ids)])
        if existing and self.state != "done":
            raise UserError("Some purchase orders already have history in the new company. No duplicate history will be created.")
        if existing:
            copied = set(existing.mapped("source_order_res_id"))
            orders = orders.filtered(lambda order: order.id not in copied)
        return [self._purchase_row(order) for order in orders]

    def _plan(self):
        rows, digest, count = super()._plan()
        purchases = self._purchase_plan()
        digest = hashlib.sha256(json.dumps([digest, self.include_purchase_history, purchases],
            sort_keys=True, default=str).encode()).hexdigest()
        return rows, digest, count

    def _lock(self):
        super()._lock()
        if self.include_purchase_history:
            try:
                with self.env.cr.savepoint():
                    self.env.cr.execute("LOCK TABLE purchase_order, purchase_order_line, account_tax_purchase_order_line_rel, "
                        "company_financial_purchase_history, product_product, product_template, uom_uom, decimal_precision "
                        "IN SHARE ROW EXCLUSIVE MODE NOWAIT")
            except LockNotAvailable as exc:
                raise UserError("Purchasing is busy. Pause purchase activity and retry during the maintenance window.") from exc
            self.env.cr.execute("SELECT txid_current_snapshot()::text")
            self._assert_fresh_snapshot(self.env.cr.fetchone()[0])

    def action_preview(self):
        result = super().action_preview()
        purchases = self._purchase_plan()
        self._system_write({"purchase_preview_data": purchases,
            "summary": (self.summary or "") + "\nPurchase orders: %s will be copied as read-only history; receipts and bills are not recreated." % len(purchases)})
        return result

    @api.depends("purchase_preview_data")
    def _compute_purchase_preview(self):
        for batch in self:
            rows = batch.purchase_preview_data or []
            body = "".join("<tr>" + "".join("<td>%s</td>" % escape(str(value)) for value in (
                row["name"], row["vendor"], purchase_status_label(row["state"], batch.env), len(row["lines"]),
                "%s %s" % (format(row["total"], ".%sf" % row.get("currency_decimals", 2)),
                    self.env["res.currency"].browse(row["currency_id"]).name))) + "</tr>" for row in rows)
            batch.purchase_preview_html = ("<p>All source purchase orders at preview time, including historical and cancelled orders. "
                "This is a read-only history copy. No order is confirmed, no receipt is created, and no vendor bill is recreated.</p>"
                "<div class='table-responsive'><table class='table table-sm'><thead><tr><th scope='col'>Order</th><th scope='col'>Vendor</th><th scope='col'>Original status</th><th scope='col'>Lines</th><th scope='col'>Order total</th>"
                "</tr></thead><tbody>" + body + "</tbody></table></div>") if rows else (
                    "<p>No purchase orders were found for this review.</p>" if batch.purchase_preview_data is not False
                    else "<p>Click 2. Review amounts to see the purchase orders that will be copied.</p>")

    def action_apply(self):
        with self.env.cr.savepoint():
            result = super().action_apply()
            self._create_purchase_history(self.purchase_preview_data or [])
            return result

    def _create_purchase_history(self, rows):
        return self.env["company.financial.purchase.history"]._system_create([
                {"cutover_id": self.id, "source_order_res_id": row["id"], "name": row["name"],
                    "source_company_name": self.source_company_id.name,
                    "vendor_name": row["vendor"], "original_state": row["state"], "order_date": row["date_order"],
                    "currency_id": row["currency_id"], "amount_total": row["total"], "snapshot": row}
                for row in rows])

    def action_preview_purchases(self):
        self.ensure_one()
        self._operator()
        if self.state != "done":
            raise UserError("Use the normal financial preview before the financial move is completed.")
        with self.env.cr.savepoint():
            self._system_write({"include_purchase_history": True})
            self._lock()
            rows = self._purchase_plan()
            self._system_write({"purchase_preview_data": rows, "purchase_history_preview_ready": True})
        return True

    def action_import_purchases(self):
        self.ensure_one()
        self._operator()
        with self.env.cr.savepoint():
            self._lock()
            if self.state != "done" or not self.purchase_history_preview_ready:
                raise UserError("Preview the missing purchase orders before copying their history.")
            rows = self._purchase_plan()
            if rows != (self.purchase_preview_data or []):
                raise UserError("Purchase orders changed. Preview the missing purchase orders again.")
            self._create_purchase_history(rows)
            self._system_write({"purchase_history_preview_ready": False})
        return True


class PurchaseHistory(models.Model):
    _name = "company.financial.purchase.history"
    _description = "Migrated purchase order history"
    _order = "order_date desc, id desc"

    cutover_id = fields.Many2one("company.financial.cutover", required=True, readonly=True, ondelete="restrict")
    company_id = fields.Many2one(related="cutover_id.target_company_id", store=True)
    source_company_id = fields.Many2one(related="cutover_id.source_company_id", store=True, string="Source company identifier")
    source_company_name = fields.Char(string="Old company", readonly=True)
    source_order_res_id = fields.Integer(required=True, readonly=True)
    name = fields.Char(required=True, readonly=True)
    vendor_name = fields.Char(readonly=True)
    original_state = fields.Char(readonly=True)
    original_status_label = fields.Char(string="Original status", compute="_compute_order_guidance")
    draft_eligible = fields.Boolean(compute="_compute_order_guidance")
    draft_guidance = fields.Char(string="What happens next", compute="_compute_order_guidance")
    order_date = fields.Datetime(readonly=True)
    currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    amount_total = fields.Monetary(readonly=True)
    snapshot = fields.Json(readonly=True)
    details_html = fields.Html(compute="_compute_details", sanitize=True)
    original_note = fields.Html(compute="_compute_details", sanitize=True, string="Original terms and notes")
    target_order_id = fields.Many2one("purchase.order", readonly=True, copy=False, ondelete="restrict")
    _unique_order = models.Constraint("UNIQUE(company_id, source_company_id, source_order_res_id)", "This purchase order already has migrated history in the new company.")

    @api.depends("original_state", "snapshot", "target_order_id")
    def _compute_order_guidance(self):
        for history in self:
            history.original_status_label = purchase_status_label(history.original_state, history.env)
            eligible, guidance = purchase_draft_advice(history.snapshot or {}, history.env)
            history.draft_eligible = eligible and not history.target_order_id
            history.draft_guidance = (history.env._("A replacement order already exists. Use View replacement order to review it; a second order will not be created.")
                if history.target_order_id else guidance)

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError("Purchase history is system-managed by the reviewed financial move.")

    def write(self, values):
        raise AccessError("Migrated purchase history is read-only.")

    def unlink(self):
        raise AccessError("Keep migrated purchase history for audit.")

    def _system_create(self, values):
        return super().create(values)

    @api.depends("snapshot")
    def _compute_details(self):
        for history in self:
            snapshot = history.snapshot or {}
            history.original_note = snapshot.get("note", "")
            rows = []
            digits = snapshot.get("unit_decimals", 2)
            for line in snapshot.get("lines", []):
                if line.get("display_type"):
                    rows.append("<tr><td colspan='10'>%s</td></tr>" % escape(line["name"]))
                    continue
                values = [line["name"], line["unit"], *[
                    float_repr(float_round(value, precision_digits=digits), digits) for value in (line["qty"], line["received"], line["billed"],
                        max(line["qty"] - line["received"], 0), max(line["qty"] - line["billed"], 0))],
                    format(Decimal(str(line["price"])), "f"), ", ".join(line["taxes"]),
                    format(line["total"], ".%sf" % snapshot.get("currency_decimals", 2))]
                rows.append("<tr>" + "".join("<td>%s</td>" % escape(str(v)) for v in values) + "</tr>")
            history.details_html = ("<p>Vendor reference: %s. Original bills: %s. Read-only history captured at approval; remaining quantities reflect that snapshot.</p>"
                % (escape(snapshot.get("vendor_ref") or history.env._("Not supplied")), escape(", ".join(b["name"] or "Draft bill" for b in snapshot.get("bills", [])) or history.env._("None")))
                + "<div class='table-responsive'><table class='table table-sm'><thead><tr><th scope='col'>Item</th><th scope='col'>Unit</th><th scope='col'>Ordered</th><th scope='col'>Received</th><th scope='col'>Billed</th>"
                "<th scope='col'>Left to receive</th><th scope='col'>Left to bill</th><th scope='col'>Unit price</th><th scope='col'>Original taxes</th><th scope='col'>Total</th></tr></thead>"
                "<tbody>" + "".join(rows) + "</tbody></table></div>")

    def action_prepare_draft(self):
        self.ensure_one()
        batch = self.cutover_id
        batch._operator()
        if not self.env.user.has_group("purchase.group_purchase_user"):
            raise AccessError("Purchase user access is required to prepare a draft order.")
        with self.env.cr.savepoint():
            batch._lock()
            if self.target_order_id:
                return self._open_draft()
            snapshot = self.snapshot
            lines = [line for line in snapshot["lines"] if not line["display_type"]]
            if not purchase_draft_advice(snapshot, self.env)[0]:
                raise UserError("This order is cancelled, partially received/billed, a down payment or a dropship order. "
                    "Keep its migrated history and ask your purchase manager to handle remaining work separately; no receipt or bill will be duplicated.")
            source = self.env["purchase.order"].browse(self.source_order_res_id).exists()
            if not source:
                raise UserError("The original order no longer exists. Review its migrated history with your purchase manager.")
            current = batch._purchase_row(source)
            for key in ("vendor_id", "currency_id", "lines", "dropship"):
                if current[key] != snapshot[key]:
                    raise UserError("The original purchase order changed after migration. Review it before preparing a draft.")
            vendor = self.env["res.partner"].browse(snapshot["vendor_id"])
            mapping = batch.partner_mapping_ids.filtered(lambda m: m.source_partner_id == vendor)
            matches = mapping.target_partner_id or batch._contact_candidates(vendor, batch._contact_pool())
            if (len(matches) != 1 or not matches.active
                or matches.company_id not in (self.env["res.company"], self.company_id)
                or not mapping.target_partner_id and not batch._contact_compatible(vendor, matches)):
                raise UserError("Create or review an existing active destination vendor with the same tax ID/reference first. Name alone is not enough.")
            origin = "Migrated purchase %s/%s" % (batch.source_company_id.id, self.source_order_res_id)
            domain = [("company_id", "=", self.company_id.id), ("partner_id", "=", matches.id)]
            candidates = self.env["purchase.order"].search([*domain, ("origin", "=", origin)])
            if snapshot["vendor_ref"]:
                candidates |= self.env["purchase.order"].search([*domain, ("partner_ref", "=", snapshot["vendor_ref"])])
            if candidates:
                raise UserError("An existing destination purchase order may already represent this order. Review it instead of creating a duplicate.")
            commands = []
            Product = self.env["product.product"].with_company(self.company_id)
            for line in lines:
                product = Product.browse(line["product_id"])
                if product.company_id and product.company_id != self.company_id:
                    identities = [("default_code", "=", product.default_code)] if product.default_code else []
                    if product.barcode:
                        identities.append(("barcode", "=", product.barcode))
                    product = Product.search([("company_id", "=", self.company_id.id)] + ["|"] * (len(identities) - 1) + identities) if identities else Product.browse()
                if len(product) != 1 or not product.active or not product.purchase_ok or product.uom_id.id != line["uom_id"]:
                    raise UserError("Review the existing destination product and unit for %s first. Products and old settings are not copied by this action." % line["name"])
                commands.append(Command.create({"product_id": product.id, "name": line["name"],
                    "product_qty": line["qty"], "product_uom_id": line["uom_id"], "price_unit": line["price"],
                    "discount": line["discount"], "date_planned": line["date_planned"],
                    "tax_ids": [Command.set(product.supplier_taxes_id.filtered_domain(
                        product.supplier_taxes_id._check_company_domain(self.company_id)).ids)]}))
            order = self.env["purchase.order"].with_company(self.company_id).with_context(
                _financial_purchase_create=_PURCHASE_CREATE_TOKEN).create({
                    "company_id": self.company_id.id, "partner_id": matches.id, "currency_id": snapshot["currency_id"],
                    "origin": origin, "partner_ref": snapshot["vendor_ref"], "order_line": commands,
                    "financial_purchase_history_id": self.id})
            super(PurchaseHistory, self).write({"target_order_id": order.id})
            return self._open_draft()

    def _open_draft(self):
        return {"type": "ir.actions.act_window", "res_model": "purchase.order", "res_id": self.target_order_id.id,
            "view_mode": "form", "name": "Review destination purchase draft"}


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    financial_purchase_history_id = fields.Many2one("company.financial.purchase.history", readonly=True, copy=False, ondelete="restrict")
    _unique_history = models.Constraint("UNIQUE(financial_purchase_history_id)", "A draft has already been prepared from this migrated order.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get("financial_purchase_history_id") for v in vals_list) and self.env.context.get("_financial_purchase_create") is not _PURCHASE_CREATE_TOKEN:
            raise AccessError("Migrated purchase links are system-managed.")
        return super().create(vals_list)

    def write(self, values):
        if "financial_purchase_history_id" in values:
            raise AccessError("Migrated purchase links are system-managed.")
        if values.get("state") == "purchase":
            for order in self:
                migrated = self.env["company.financial.purchase.history"].sudo().search([
                    ("source_company_id", "=", order.company_id.id), ("source_order_res_id", "=", order.id),
                    ("target_order_id.state", "not in", [False, "cancel"])], limit=1)
                if migrated:
                    raise UserError("A replacement draft already exists in the new company. Do not confirm the old order again.")
        return super().write(values)

    def copy(self, default=None):
        if self.financial_purchase_history_id:
            raise UserError("Do not duplicate a migrated purchase order. Prepare or view its one linked draft from migrated history.")
        return super().copy(default)

    def button_confirm(self):
        for order in self.filtered("financial_purchase_history_id"):
            history = order.financial_purchase_history_id
            source = self.sudo().browse(history.source_order_res_id).exists()
            if not source or source.state != "cancel":
                raise UserError("Cancel the original order in the old company before confirming this replacement, so it cannot be received or billed twice.")
        return super().button_confirm()
