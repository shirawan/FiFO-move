"""Destination purchase history and explicitly prepared, unconfirmed RFQs."""
import hashlib
import json
from decimal import Decimal
from html import escape

from psycopg2.errors import LockNotAvailable
from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, ConcurrencyError, UserError
from odoo.tools.float_utils import float_is_zero, float_repr, float_round


MAX_PURCHASE_ORDERS = 10000
MAX_PURCHASE_LINES = 50000
_PURCHASE_CREATE_TOKEN = object()
_PURCHASE_RESTORE_TOKEN = object()


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
    return True, env._("Nothing was received or billed when this history was copied. Check or choose the destination vendor first. Then cancel the original order in the old company and prepare a replacement draft here. Review the new draft's products, vendor and taxes before confirming it.")


def purchase_duplicate_signature(row, identity):
    return (row.get("company_id"), row["vendor_id"], identity(row["vendor_ref"], True), row["currency_id"], row["date_order"][:10],
        tuple((line["product_id"], line["qty"], line["uom_id"], line["price"], line["discount"])
            for line in row["lines"] if not line["display_type"]))


class FinancialCutover(models.Model):
    _inherit = "company.financial.cutover"

    purchase_history_ids = fields.One2many("company.financial.purchase.history", "cutover_id", readonly=True, copy=False)

    def _invalidate_preview(self):
        super()._invalidate_preview()
        self._system_write({"purchase_preview_data": False, "purchase_history_preview_ready": False})

    def _purchase_row(self, order):
        return {
            "id": order.id, "company_id": order.company_id.id, "company_name": order.company_id.name, "name": order.name, "vendor_id": order.partner_id.id,
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
            ("company_id", "in", self._source_companies().ids)], order="id", limit=MAX_PURCHASE_ORDERS + 1)
        if len(orders) > MAX_PURCHASE_ORDERS or len(orders.order_line) > MAX_PURCHASE_LINES:
            raise UserError("Purchase migration is limited to 10,000 orders and 50,000 lines per cutover.")
        existing = self.env["company.financial.purchase.history"].sudo().search([
            ("company_id", "=", self.target_company_id.id), ("source_company_id", "in", self._source_companies().ids),
            ("source_order_res_id", "in", orders.ids)])
        from odoo.addons.company_financial_cutover.models.retention import purchase_marker_key
        markers = self.env["ir.config_parameter"].sudo().search([
            ("key", "in", [purchase_marker_key(order.company_id.id, order.id) for order in orders])])
        copied = set(existing.mapped("source_order_res_id"))
        issues = []
        for parameter in markers:
            try:
                marker = self._read_purchase_marker(parameter)
            except UserError as exc:
                issues.append(str(exc))
                continue
            if (marker.get("source_company_id") not in self._source_companies().ids
                or parameter.key != purchase_marker_key(marker.get("source_company_id"), marker.get("source_order_id"))):
                issues.append("A purchase migration marker does not match its source order. Ask your administrator to recover it; no duplicate will be created.")
            if marker["target_company_id"] != self.target_company_id.id:
                issues.append("Some purchase orders already moved to another company. Ask your administrator to review the saved migration archive; another copy will not be created.")
            if marker.get("source_order_id") not in copied:
                issues.append("Purchase history was previously copied but its history screen is missing. Reinstall or recover its saved archive before continuing; no duplicate copy will be created.")
        self._raise_issues("Review these purchase-copy requirements together:", issues)
        all_rows = [self._purchase_row(order) for order in orders]
        rows = [row for row in all_rows if row["id"] not in copied]
        return rows

    def _review_notices(self, lines):
        notices = super()._review_notices(lines)
        if not self.include_purchase_history or not self.env.user.has_group("purchase.group_purchase_user"):
            return notices
        orders = self.env["purchase.order"].with_company(self.source_company_id).search([
            ("company_id", "in", self._source_companies().ids)], order="id", limit=MAX_PURCHASE_ORDERS + 1)
        if len(orders) > MAX_PURCHASE_ORDERS or len(orders.order_line) > MAX_PURCHASE_LINES:
            return notices
        seen = {}
        for order in orders:
            row = self._purchase_row(order)
            if not row["vendor_ref"] or not purchase_draft_advice(row, self.env)[0]:
                continue
            signature = purchase_duplicate_signature(row, self._identity)
            if signature in seen:
                notices.append("Possible duplicate purchase orders: %s and %s. Both are copied as separate history. No active order is created by copying. Before preparing a replacement, your purchase manager must review the originals and any existing destination order." % (seen[signature], row["name"]))
            seen[signature] = row["name"]
        return notices

    def _purchase_contacts(self):
        if not self.include_purchase_history:
            return self.env["res.partner"]
        if not self.env.user.has_group("purchase.group_purchase_user"):
            raise AccessError("Purchase user access is required when including purchase orders.")
        orders = self.env["purchase.order"].with_company(self.source_company_id).search([
            ("company_id", "in", self._source_companies().ids)], limit=MAX_PURCHASE_ORDERS + 1)
        if len(orders) > MAX_PURCHASE_ORDERS:
            raise UserError("Purchase migration is limited to 10,000 orders per cutover.")
        copied = set(self.env["company.financial.purchase.history"].sudo().search([
            ("company_id", "=", self.target_company_id.id), ("source_company_id", "in", self._source_companies().ids),
            ("source_order_res_id", "in", orders.ids)]).mapped("source_order_res_id"))
        # Vendor choices are needed only for new history that can produce a
        # replacement. Already copied, cancelled and received/billed history
        # must not inflate the choices shown to the operator.
        return orders.filtered(lambda order: order.id not in copied
            and purchase_draft_advice(self._purchase_row(order), self.env)[0]).partner_id

    def _plan(self):
        rows, digest, count = super()._plan()
        purchases = self._purchase_plan()
        digest = hashlib.sha256(json.dumps([digest, self.include_purchase_history, purchases],
            sort_keys=True, default=str).encode()).hexdigest()
        return rows, digest, count

    def _lock_tables(self):
        tables = super()._lock_tables()
        if self.include_purchase_history:
            tables += ("purchase_order", "purchase_order_line", "account_tax_purchase_order_line_rel",
                "company_financial_purchase_history", "product_product", "product_template", "uom_uom", "decimal_precision",
                "account_fiscal_position", "account_fiscal_position_account_tax_rel", "product_supplier_taxes_rel")
        return tables

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
                row.get("company_name", batch.source_company_id.name), row["name"], row["vendor"], purchase_status_label(row["state"], batch.env), len(row["lines"]),
                "%s %s" % (format(row["total"], ".%sf" % row.get("currency_decimals", 2)),
                    self.env["res.currency"].browse(row["currency_id"]).name))) + "</tr>" for row in rows)
            batch.purchase_preview_html = ("<p>All source purchase orders at preview time, including historical and cancelled orders. "
                "This is a read-only history copy. No order is confirmed, no receipt is created, and no vendor bill is recreated.</p>"
                "<div class='table-responsive'><table class='table table-sm'><thead><tr><th scope='col'>Old company</th><th scope='col'>Order</th><th scope='col'>Vendor</th><th scope='col'>Original status</th><th scope='col'>Lines</th><th scope='col'>Order total</th>"
                "</tr></thead><tbody>" + body + "</tbody></table></div>") if rows else (
                    "<p>No new purchase orders to copy. Already copied orders are skipped; earlier copies remain under Purchase → Migrated purchase history.</p>" if batch.purchase_preview_data is not False
                    else "<p>Click 2. Review selected data to see the purchase orders that will be copied.</p>")

    def action_apply(self):
        if not self.include_financial:
            return super().action_apply()
        with self.env.cr.savepoint():
            result = super().action_apply()
            self._create_purchase_history(self.purchase_preview_data or [])
            self._save_durable_archive()
            return result

    def _create_purchase_history(self, rows):
        return self.env["company.financial.purchase.history"]._system_create([
                {"cutover_id": self.id, "source_order_res_id": row["id"], "name": row["name"],
                    "source_company_id": row.get("company_id", self.source_company_id.id),
                    "source_company_name": row.get("company_name", self.source_company_id.name),
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
            self._save_durable_archive()
        return True


class PurchaseHistory(models.Model):
    _name = "company.financial.purchase.history"
    _description = "Migrated purchase order history"
    _order = "order_date desc, id desc"

    cutover_id = fields.Many2one("company.financial.cutover", required=True, readonly=True, ondelete="restrict")
    company_id = fields.Many2one(related="cutover_id.target_company_id", store=True)
    source_company_id = fields.Many2one("res.company", required=True, ondelete="restrict", string="Original company")
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
    replacement_vendor_id = fields.Many2one("res.partner", string="Chosen destination vendor", readonly=True,
        copy=False, ondelete="restrict")
    destination_vendor_id = fields.Many2one("res.partner", string="Vendor for the new order",
        compute="_compute_replacement_step")
    current_original_status = fields.Char(string="Current status in the old company", compute="_compute_replacement_step")
    replacement_note = fields.Char(compute="_compute_replacement_step")
    replacement_step = fields.Selection([
        ("vendor", "Choose a vendor"), ("cancel", "Cancel the original"),
        ("prepare", "Prepare a draft"), ("created", "View the replacement"),
        ("manual", "Manager review needed"),
    ], compute="_compute_replacement_step")
    _unique_order = models.Constraint("UNIQUE(company_id, source_company_id, source_order_res_id)", "This purchase order already has migrated history in the new company.")

    @api.depends("original_state", "snapshot", "target_order_id")
    def _compute_order_guidance(self):
        for history in self:
            history.original_status_label = purchase_status_label(history.original_state, history.env)
            eligible, guidance = purchase_draft_advice(history.snapshot or {}, history.env)
            history.draft_eligible = eligible and not history.target_order_id
            history.draft_guidance = (history.env._("A replacement order already exists. Use View replacement order to review it; a second order will not be created.")
                if history.target_order_id else guidance)

    @api.depends("snapshot", "source_order_res_id", "target_order_id", "replacement_vendor_id",
        "replacement_vendor_id.active", "replacement_vendor_id.company_id",
        "cutover_id.partner_mapping_ids.target_partner_id", "cutover_id.partner_mapping_ids.target_partner_id.active")
    def _compute_replacement_step(self):
        for history in self:
            # Saved history stays readable without old-company access. Current
            # status is shown only when the caller can read the original order.
            batch = history.cutover_id.sudo()
            source = self.env["purchase.order"].browse(history.source_order_res_id).exists()
            readable = True
            try:
                source.check_access("read")
            except AccessError:
                readable = False
                source = self.env["purchase.order"]
            mapping = batch.partner_mapping_ids.filtered(
                lambda row: row.source_partner_id.id == (history.snapshot or {}).get("vendor_id"))
            vendor = history.replacement_vendor_id or mapping.target_partner_id
            if not vendor:
                shared = self.env["res.partner"].sudo().browse((history.snapshot or {}).get("vendor_id")).exists()
                if shared and not shared.company_id:
                    vendor = shared
            history.destination_vendor_id = vendor
            history.current_original_status = (purchase_status_label(source.state, history.env) if source
                else history.env._("Not available with your company access") if not readable
                else history.env._("Original order unavailable"))
            eligible, advice = purchase_draft_advice(history.snapshot or {}, history.env)
            unchanged = False
            if source and eligible and not history.target_order_id:
                current = batch._purchase_row(source.sudo())
                unchanged = all(current[key] == history.snapshot.get(key) for key in ("vendor_id", "currency_id", "lines", "dropship"))
            draft_issues = []
            if source and eligible and unchanged and not history.target_order_id:
                _commands, draft_issues = history._draft_product_commands()
                draft_issues.extend(history._replacement_duplicate_issues())
                draft_issues.extend(history._replacement_destination_issues(vendor))
            if history.target_order_id:
                step = "created"
            elif not eligible or not source or not unchanged or draft_issues:
                step = "manual"
            elif not vendor or not vendor.active or vendor.company_id not in (self.env["res.company"], history.company_id):
                step = "vendor"
            elif source.state != "cancel":
                step = "cancel"
            else:
                step = "prepare"
            history.replacement_step = step
            if draft_issues:
                note = "\n".join(dict.fromkeys(draft_issues))
            elif not eligible:
                note = advice
            elif not readable:
                note = history.env._("Ask an authorized operator with access to both companies to review the original order and prepare any replacement.")
            elif not source:
                note = history.env._("The original order is unavailable. Ask your purchase manager to review the saved history before continuing.")
            elif not unchanged and not history.target_order_id:
                note = history.env._("The original order changed after this history was copied. The quantities below are from copy time. Ask your purchase manager to review the current order before continuing.")
            else:
                note = False
            history.replacement_note = note

    def _purchase_operator(self):
        self.ensure_one()
        self.check_access("read")
        from odoo.addons.company_financial_cutover.models.retention import ISSUE_PREFIX
        if self.env["ir.config_parameter"].sudo().get_param(ISSUE_PREFIX + (self.cutover_id.sudo().archive_key or "")):
            raise UserError("This move has a saved archive needing recovery. Ask your administrator to use Company move recovery before preparing replacements.")
        user = self.env.user
        if not (user.has_group("purchase.group_purchase_manager") or
                user.has_group("base.group_system") and user.has_group("account.group_account_manager")
                and user.has_group("purchase.group_purchase_user")):
            raise AccessError("Ask your purchase manager to prepare replacements or choose their vendors.")
        if not {self.source_company_id.id, self.company_id.id}.issubset(set(self.env.companies.ids)):
            raise UserError("Select this order's old company and its new company in the company switcher.")

    def _lock_replacement(self):
        # Day-to-day actions serialize only this history and its native orders.
        # Real database serialization errors are handled by Odoo's retry loop.
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("SELECT id FROM company_financial_purchase_history WHERE id = %s FOR UPDATE NOWAIT", [self.id])
                orders = sorted(set(filter(None, [self.source_order_res_id, self.target_order_id.id])))
                if orders:
                    self.env.cr.execute("SELECT id FROM purchase_order WHERE id IN %s ORDER BY id FOR UPDATE NOWAIT", [tuple(orders)])
        except LockNotAvailable:
            raise ConcurrencyError("This purchase is being updated. Please try again.") from None

    def action_open_original(self):
        self.ensure_one()
        self._purchase_operator()
        source = self.env["purchase.order"].browse(self.source_order_res_id).exists()
        if not source:
            raise UserError("The original order is unavailable. Ask your purchase manager to review the saved history.")
        source.check_access("read")
        return {"type": "ir.actions.act_window", "name": "Original order in the old company",
            "res_model": "purchase.order", "res_id": source.id, "view_mode": "form", "target": "new"}

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

    def _draft_product_commands(self):
        commands, issues = [], []
        Product = self.env["product.product"].with_company(self.company_id)
        for line in (self.snapshot or {}).get("lines", []):
            if line["display_type"]:
                continue
            product = Product.browse(line["product_id"]).exists()
            if product.company_id and product.company_id != self.company_id:
                identities = [("default_code", "=", product.default_code)] if product.default_code else []
                if product.barcode:
                    identities.append(("barcode", "=", product.barcode))
                product = Product.search([("company_id", "=", self.company_id.id)]
                    + ["|"] * (len(identities) - 1) + identities) if identities else Product.browse()
            if len(product) != 1 or not product.active or not product.purchase_ok or product.uom_id.id != line["uom_id"]:
                issues.append("Review the existing destination product and unit for %s first. Products and old settings are not copied by this action." % line["name"])
                continue
            commands.append(Command.create({"product_id": product.id, "name": line["name"],
                "product_qty": line["qty"], "product_uom_id": line["uom_id"], "price_unit": line["price"],
                "discount": line["discount"], "date_planned": line["date_planned"]}))
        return commands, issues

    def _replacement_duplicate_issues(self):
        snapshot = self.snapshot or {}
        if not snapshot.get("vendor_ref"):
            return []
        batch = self.cutover_id.sudo()
        others = self.env["purchase.order"].with_company(self.source_company_id).search([
            ("company_id", "=", snapshot.get("company_id", batch.source_company_id.id)), ("partner_id", "=", snapshot["vendor_id"]),
            ("state", "!=", "cancel"), ("id", "!=", self.source_order_res_id)])
        signature = purchase_duplicate_signature(snapshot, batch._identity)
        duplicates = [order.name for order in others if purchase_draft_advice(batch._purchase_row(order), self.env)[0]
            and purchase_duplicate_signature(batch._purchase_row(order), batch._identity) == signature]
        return (["Other active original orders may duplicate this purchase: %s. Ask your purchase manager to choose the correct order and cancel duplicates before preparing one replacement. The saved history can stay unchanged." % ", ".join(duplicates)]
            if duplicates else [])

    def _replacement_destination_issues(self, vendor):
        if len(vendor) != 1 or not vendor.active or vendor.company_id not in (self.env["res.company"], self.company_id):
            return []
        origin = "Migrated purchase %s/%s" % (self.source_company_id.id, self.source_order_res_id)
        domain = [("company_id", "=", self.company_id.id), ("partner_id", "=", vendor.id), ("state", "!=", "cancel")]
        candidates = self.env["purchase.order"].search([*domain, ("origin", "=", origin)])
        if self.snapshot["vendor_ref"]:
            candidates |= self.env["purchase.order"].search([*domain, ("partner_ref", "=", self.snapshot["vendor_ref"])])
        return (["An existing destination purchase order may already represent this order: %s. Review it instead of creating a duplicate." % ", ".join(candidates.mapped("name"))]
            if candidates else [])

    def _claim_destination_identity(self, vendor):
        """Serialize competing histories for the same destination vendor reference.

        A durable native parameter row avoids an insert phantom in repeatable
        read. ON CONFLICT updates that row, so a newer committed reservation
        raises a real PostgreSQL serialization error and Odoo retries.
        """
        reference = self.cutover_id.sudo()._identity(self.snapshot.get("vendor_ref", ""), True)
        if not reference:
            return False, []
        identity = [self.company_id.id, vendor.id, reference]
        key = "company_financial_cutover.replacement_identity." + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("SHOW lock_timeout")
                old_timeout = self.env.cr.fetchone()[0]
                self.env.cr.execute("SELECT set_config('lock_timeout', '250ms', true)")
                self.env.cr.execute("""INSERT INTO ir_config_parameter (key, value) VALUES (%s, '{}')
                    ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key RETURNING value""", [key])
                saved = self.env.cr.fetchone()[0]
                self.env.cr.execute("SELECT set_config('lock_timeout', %s, true)", [old_timeout])
        except LockNotAvailable:
            raise ConcurrencyError("Another replacement for this vendor reference is being prepared. Please try again.") from None
        try:
            marker = json.loads(saved)
            if not isinstance(marker, dict):
                raise ValueError()
            if marker and (marker.get("version") != 1 or marker.get("identity") != identity or not isinstance(marker.get("target_order_id"), int)):
                raise ValueError()
        except (TypeError, ValueError):
            raise UserError("A saved replacement-order marker needs recovery. Ask your administrator to restore it before preparing another draft.") from None
        previous = self.env['purchase.order'].sudo().browse(marker.get('target_order_id')).exists() if marker else self.env['purchase.order']
        if previous and (previous.company_id != self.company_id or previous.partner_id.id != vendor.id
                or self.cutover_id.sudo()._identity(previous.partner_ref or "", True) != reference):
            raise UserError("A saved replacement-order marker no longer matches its order. Ask your administrator to review it before preparing another draft.")
        issues = (["An existing destination purchase order already uses this vendor reference. Review it instead of creating a duplicate."]
            if previous and previous.state != 'cancel' else [])
        return (key, identity), issues

    def action_prepare_draft(self):
        self.ensure_one()
        self._purchase_operator()
        batch = self.cutover_id.sudo()
        with self.env.cr.savepoint():
            self._lock_replacement()
            if self.target_order_id:
                return self._open_draft()
            issues = []
            snapshot = self.snapshot
            if not purchase_draft_advice(snapshot, self.env)[0]:
                issues.append("This order is cancelled, partially received/billed, a down payment or a dropship order. "
                    "Keep its migrated history and ask your purchase manager to handle remaining work separately; no receipt or bill will be duplicated.")
            source = self.env["purchase.order"].browse(self.source_order_res_id).exists()
            if not source:
                issues.append("The original order no longer exists. Review its migrated history with your purchase manager.")
            if source and source.state != "cancel":
                issues.append("Cancel the original order in the old company before preparing its replacement. This keeps two active orders from surviving an addon uninstall.")
            current = batch._purchase_row(source) if source else snapshot
            for key in ("vendor_id", "currency_id", "lines", "dropship"):
                if current[key] != snapshot[key]:
                    issues.append("The original purchase order changed after migration. Review it before preparing a draft.")
            vendor = self.env["res.partner"].browse(snapshot["vendor_id"])
            mapping = batch.partner_mapping_ids.filtered(lambda m: m.source_partner_id == vendor)
            explicit = self.replacement_vendor_id or mapping.target_partner_id
            matches = explicit or batch._contact_candidates(vendor, batch._contact_pool())
            matches = matches.with_env(self.env)
            if matches:
                matches.check_access("read")
            valid_vendor = (len(matches) == 1 and matches.active
                and matches.company_id in (self.env["res.company"], self.company_id)
                and (explicit or batch._contact_compatible(vendor, matches)))
            if (len(matches) != 1 or not matches.active
                or matches.company_id not in (self.env["res.company"], self.company_id)
                or not explicit and not batch._contact_compatible(vendor, matches)):
                issues.append("Use Choose destination vendor to select the correct existing contact first. Name alone is not enough for automatic matching.")
            origin = "Migrated purchase %s/%s" % (self.source_company_id.id, self.source_order_res_id)
            identity_claim = False
            if valid_vendor:
                identity_claim, identity_issues = self._claim_destination_identity(matches)
                issues.extend(identity_issues)
                issues.extend(self._replacement_destination_issues(matches))
            commands, product_issues = self._draft_product_commands()
            issues.extend(product_issues)
            issues.extend(self._replacement_duplicate_issues())
            batch._raise_issues("Review these replacement requirements together:", issues)
            position = self.env["account.fiscal.position"].with_company(self.company_id)._get_fiscal_position(matches)
            order = self.env["purchase.order"].with_company(self.company_id).with_context(
                _financial_purchase_create=_PURCHASE_CREATE_TOKEN).create({
                    "company_id": self.company_id.id, "partner_id": matches.id, "currency_id": snapshot["currency_id"],
                    "origin": origin, "partner_ref": snapshot["vendor_ref"], "order_line": commands,
                    "fiscal_position_id": position.id,
                    "financial_purchase_history_id": self.id})
            # History is deliberately read-only in the public ACL. Only this
            # validated operator action may save its system-managed link.
            super(PurchaseHistory, self.sudo()).write({"target_order_id": order.id})
            if identity_claim:
                self.env['ir.config_parameter'].sudo().set_param(identity_claim[0], json.dumps({
                    'version': 1, 'identity': identity_claim[1], 'target_order_id': order.id}, sort_keys=True))
            return self._open_draft()

    def action_choose_vendor(self):
        self.ensure_one()
        self._purchase_operator()
        if self.target_order_id:
            raise UserError("The replacement already exists. Review its vendor on that order.")
        mapping = self.cutover_id.sudo().partner_mapping_ids.filtered(
            lambda row: row.source_partner_id.id == self.snapshot["vendor_id"])
        vendor = self.replacement_vendor_id or mapping.target_partner_id
        if vendor and (not vendor.active or vendor.company_id not in (self.env["res.company"], self.company_id)):
            vendor = self.env["res.partner"]
        return {"type": "ir.actions.act_window", "name": "Choose destination vendor",
            "res_model": "company.financial.purchase.vendor.choice", "view_mode": "form", "target": "new",
            "context": {**self.env.context, "default_history_id": self.id,
                "default_vendor_id": vendor.id}}

    def _choose_vendor(self, vendor):
        self.ensure_one()
        self._purchase_operator()
        self._lock_replacement()
        if self.target_order_id:
            raise UserError("The replacement already exists. Review its vendor on that order.")
        if not vendor.exists() or not vendor.active or vendor.company_id not in (self.env["res.company"], self.company_id):
            raise UserError("Choose an active existing vendor shared with or belonging to the new company.")
        vendor.check_access("read")
        return super(PurchaseHistory, self.sudo()).write({"replacement_vendor_id": vendor.id})

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
        capability = self.env.context.get("_financial_purchase_restore")
        if "financial_purchase_history_id" in values and not (
            isinstance(capability, tuple) and len(capability) == 2 and capability[0] is _PURCHASE_RESTORE_TOKEN
            and capability[1] == values["financial_purchase_history_id"]):
            raise AccessError("Migrated purchase links are system-managed.")
        if values.get("state") == "purchase":
            self._lock_migrated_orders()
            self._check_original_cancelled()
            for order in self:
                migrated = self.env["company.financial.purchase.history"].sudo().search([
                    ("source_company_id", "=", order.company_id.id), ("source_order_res_id", "=", order.id),
                    ("target_order_id.state", "not in", [False, "cancel"])], limit=1)
                if migrated:
                    raise UserError("A replacement draft already exists in the new company. Do not confirm the old order again.")
        return super().write(values)

    def _lock_migrated_orders(self):
        """Serialize approvals of both sides of a migrated order pair."""
        self.check_access("write")
        histories = self.financial_purchase_history_id.sudo() | self.env["company.financial.purchase.history"].sudo().search([
            ("source_order_res_id", "in", self.ids), ("target_order_id", "!=", False)])
        if not histories:
            return
        orders = self.sudo().browse(list(set(histories.mapped("source_order_res_id")) | set(histories.target_order_id.ids)))
        orders.flush_recordset(["state"])
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("SELECT id FROM purchase_order WHERE id = ANY(%s) ORDER BY id FOR UPDATE NOWAIT", [orders.ids])
        except LockNotAvailable as exc:
            raise ConcurrencyError("A related purchase order is being updated; retry approval with fresh data.") from exc
        orders.invalidate_recordset(["state"])

    def _restore_purchase_history_link(self, history_id):
        return self.with_context(_financial_purchase_restore=(_PURCHASE_RESTORE_TOKEN, history_id)).write({"financial_purchase_history_id": history_id})

    def copy(self, default=None):
        if self.financial_purchase_history_id:
            raise UserError("Do not duplicate a migrated purchase order. Prepare or view its one linked draft from migrated history.")
        return super().copy(default)

    def button_confirm(self):
        self._check_original_cancelled()
        return super().button_confirm()

    def _check_original_cancelled(self):
        for order in self.filtered("financial_purchase_history_id"):
            history = order.financial_purchase_history_id
            source = self.sudo().browse(history.source_order_res_id).exists()
            if not source or source.state != "cancel":
                raise UserError("Cancel the original order in the old company before confirming this replacement, so it cannot be received or billed twice.")


class PurchaseVendorChoice(models.TransientModel):
    _name = "company.financial.purchase.vendor.choice"
    _description = "Choose destination vendor for a migrated purchase"

    history_id = fields.Many2one("company.financial.purchase.history", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="history_id.company_id")
    original_vendor = fields.Char(related="history_id.vendor_name")
    vendor_id = fields.Many2one("res.partner", string="Use this existing vendor", required=True)
    vendor_reference = fields.Char(related="vendor_id.ref", string="Contact reference")
    vendor_tax_id = fields.Char(related="vendor_id.vat", string="Tax ID")
    vendor_email = fields.Char(related="vendor_id.email", string="Email")

    def action_confirm(self):
        self.ensure_one()
        self.history_id._choose_vendor(self.vendor_id)
        return {"type": "ir.actions.act_window_close"}
