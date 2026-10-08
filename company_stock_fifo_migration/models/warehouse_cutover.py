"""Small, whole-warehouse interface over the existing native FIFO cutover."""
from html import escape
from math import prod

from odoo import _, Command, api, fields, models
from odoo.exceptions import AccessError, UserError

from .execution import opening_chunks, execute_cutover, lock_cutover_tables
from .migration import _SYSTEM_CONTEXT, _SYSTEM_TOKEN, fingerprint
from .destination import product_matches, location_matches
from .presentation import product_preview_table
from .snapshot import (company_model, LOCATION_ROLES, LOT_FIELDS, source_operations_plan,
                       source_product_plans, build_snapshot, reviewed_allocations, target_stock_state)


class WholeWarehouseCutover(models.Model):
    _name = "company.stock.warehouse.cutover"
    _description = "Move Whole Warehouse"
    _order = "id desc"
    _rec_name = "target_warehouse_id"

    source_warehouse_id = fields.Many2one("stock.warehouse", required=True, ondelete="restrict")
    source_company_id = fields.Many2one(related="source_warehouse_id.company_id", store=True)
    target_company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    target_warehouse_id = fields.Many2one("stock.warehouse", ondelete="restrict")
    source_root_location_id = fields.Many2one(related="source_warehouse_id.view_location_id")
    target_root_location_id = fields.Many2one(related="target_warehouse_id.view_location_id")
    location_choice_ids = fields.One2many(
        "company.stock.warehouse.location.choice", "cutover_id", copy=False,
        string="Destination Location Choices")
    product_choice_ids = fields.One2many(
        "company.stock.warehouse.product.choice", "cutover_id", copy=False,
        string="Destination Product Choices")
    # Retain names on historical audit records; new moves select a warehouse.
    target_warehouse_name = fields.Char(readonly=True)
    target_warehouse_code = fields.Char(readonly=True)
    release_source_reservations = fields.Boolean(
        string="Move recorded stock; leave old operations behind", default=True, copy=False,
        help="Move recorded on-hand quantities without applying draft counts or finishing old operations. "
             "Confirm releases reservations only in this warehouse, including Picked lines. "
             "Old orders remain open in the Source Company and must not be processed after cutover.",
    )
    currency_id = fields.Many2one(related="source_company_id.currency_id")
    state = fields.Selection([
        ("draft", "Choose Warehouse"), ("review", "Review Preview"), ("done", "Completed"),
    ], default="draft", readonly=True, required=True, copy=False)
    cutover_at = fields.Datetime(readonly=True, copy=False)
    preview_data = fields.Json(readonly=True, copy=False)
    preview_hash = fields.Char(readonly=True, copy=False)
    product_count = fields.Integer(compute="_compute_preview")
    total_value = fields.Monetary(compute="_compute_preview")
    preview_html = fields.Html(compute="_compute_preview", sanitize=True)
    stock_batch_id = fields.Many2one("company.stock.fifo.migration", readonly=True, copy=False, ondelete="restrict")

    @api.depends("preview_data")
    def _compute_preview(self):
        for cutover in self:
            rows = (cutover.preview_data or {}).get("products", [])
            cutover.product_count = len(rows)
            cutover.total_value = sum(row["value"] for row in rows)
            preview_html = (
                '<p>%s company-specific product variants will be created, including unstocked sibling variants.</p>'
                % (cutover.preview_data or {}).get("copied_variant_count", len(rows))
                + product_preview_table(cutover.preview_data)
            ) if rows else False
            if rows and cutover.preview_data.get("location_matches"):
                locations = cutover.preview_data["location_matches"]
                preview_html = ('<p>Destination: <strong>%s</strong>. %s location mappings reuse existing locations; '
                                '%s need missing child locations (shared destination paths are created once). '
                                'No new warehouse will be created.</p>' % (
                                    escape(cutover.preview_data["target_warehouse_name"]),
                                    sum(bool(row["target"]) for row in locations),
                                    sum(not row["target"] for row in locations))) + preview_html
                if cutover.location_choice_ids:
                    preview_html += '<p><strong>Chosen destination locations</strong></p><ul>' + "".join(
                        '<li>%s → %s</li>' % (escape(row["source_name"]), escape(row["target_name"]))
                        for row in locations if row["source"] in cutover.location_choice_ids.source_location_id.ids
                    ) + '</ul>'
            periodic = [row["name"] for row in rows if row.get("valuation") == "periodic"]
            if periodic:
                preview_html += (
                    '<p><strong>Source uses Periodic valuation: no automatic closing journal.</strong> '
                    'Review the old company’s general-ledger closing inventory separately for: %s. '
                    'The destination follows its own setting: Perpetual creates a posted opening journal; '
                    'Periodic requires a separate general-ledger opening adjustment.</p>'
                    % escape(", ".join(periodic))
                )
            if rows and (cutover.preview_data or {}).get("release_source_reservations"):
                plan = cutover.preview_data["source_operations"]
                names = {row["id"]: row["name"] for row in plan["operations"]}
                preview_html += (
                    '<h4>Reservations released on Confirm</h4>'
                    '<p>Old operations remain open in the Source Company. They are not migrated or cancelled. '
                    'Picked reservations are included. Unapplied draft stock counts are not applied or copied; '
                    'only recorded on-hand quantities are moved. Do not process old operations or apply old draft counts '
                    'after cutover. Reservations outside this warehouse are unchanged.</p>'
                    '<p>%s open stock operations remain behind. %s reservation rows will be released; '
                    'the first 50 are shown below. The complete audit is retained in the cutover snapshot.</p>'
                    % (len(plan["operations"]), len(plan["lines"]))
                    + '<table class="table table-sm"><thead><tr><th>Operation</th><th>Product</th>'
                    '<th>Location</th><th>Reserved quantity</th></tr></thead><tbody>'
                    + "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%g %s</td></tr>" % (
                        escape(names[row["move"]]), escape(row["name"]), escape(row["location_name"]),
                        row["quantity"], escape(row["unit"]),
                    ) for row in plan["lines"][:50]) + "</tbody></table>"
                )
            cutover.preview_html = preview_html

    @api.onchange("target_company_id")
    def _onchange_target_company(self):
        if self.target_warehouse_id.company_id != self.target_company_id:
            self.target_warehouse_id = False

    def _system(self):
        return self.with_context(**{_SYSTEM_CONTEXT: _SYSTEM_TOKEN})

    def _check_stock_blockers(self, quants, selected_ids):
        blocked = quants.filtered(
            lambda quant: quant.quantity < 0 or (quant.id in selected_ids and (
                (quant.reserved_quantity and not self.release_source_reservations)
                or quant.owner_id or quant.package_id))
        )
        if not blocked:
            return
        details = []
        for quant in blocked[:20]:
            reasons = []
            if quant.quantity < 0:
                reasons.append(_("negative stock"))
            if quant.reserved_quantity:
                reasons.append(_("reservation"))
            if quant.owner_id:
                reasons.append(_("consignment owner: %s", quant.owner_id.display_name))
            if quant.package_id:
                reasons.append(_("package: %s", quant.package_id.display_name))
            details.append(_(
                "%(product)s — %(location)s\n"
                "On hand: %(quantity)s %(unit)s; Reserved: %(reserved)s %(unit)s.\n"
                "Blocked by: %(reasons)s",
                product=quant.product_id.display_name, location=quant.location_id.complete_name,
                quantity=format(quant.quantity, ".16g"), reserved=format(quant.reserved_quantity, ".16g"),
                unit=quant.product_id.uom_id.display_name, reasons="; ".join(reasons),
            ))
            if quant.lot_id:
                details.append(_("Lot/serial: %s", quant.lot_id.name))
        if len(blocked) > 20:
            details.append(_("%s more blocked stock rows are not shown. Resolve these rows, then Preview again.",
                             len(blocked) - 20))
        raise UserError(_(
            "Preview blocked by %(count)s stock row(s) in %(company)s.\n"
            "Checks include these products in all internal/transit locations of the Source Company, "
            "including outside the selected warehouse.\n\n%(details)s",
            count=len(blocked), company=self.source_company_id.display_name,
            details="\n\n".join(details),
        ))

    @api.model
    def _validate_selection(self, warehouse, target):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only a Settings administrator may move a warehouse."))
        if not warehouse or not target:
            raise UserError(_("Choose the Source warehouse and Target Company first."))
        warehouse.check_access("read")
        target.check_access("read")
        if warehouse.company_id == target:
            raise UserError(_("Select a different Target Company."))
        if warehouse.company_id not in self.env.companies or target not in self.env.companies:
            raise AccessError(_("Enable both companies in the company switcher."))

    @api.model_create_multi
    def create(self, vals_list):
        allowed = {"source_warehouse_id", "target_company_id", "target_warehouse_id",
                   "release_source_reservations", "product_choice_ids", "location_choice_ids"}
        for vals in vals_list:
            if set(vals) - allowed:
                raise AccessError(_("Warehouse cutover audit fields are system-managed."))
            self._validate_selection(
                self.env["stock.warehouse"].browse(vals.get("source_warehouse_id")).exists(),
                self.env["res.company"].browse(vals.get("target_company_id")).exists(),
            )
        return super().create(vals_list)

    def _operator(self):
        self.ensure_one()
        self.check_access("write")
        self._validate_selection(self.source_warehouse_id, self.target_company_id)

    def _lock(self):
        self.check_access("write")
        self.env.cr.execute(
            "SELECT id FROM company_stock_warehouse_cutover WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [self.ids],
        )
        self.invalidate_recordset(["state"])

    def write(self, vals):
        self._lock()
        if self.filtered(lambda cutover: cutover.state == "done"):
            raise UserError(_("A completed warehouse cutover is immutable."))
        if self.env.context.get(_SYSTEM_CONTEXT) is _SYSTEM_TOKEN:
            return super().write(vals)
        if set(vals) - {"source_warehouse_id", "target_company_id", "target_warehouse_id",
                        "release_source_reservations", "product_choice_ids", "location_choice_ids"}:
            raise AccessError(_("Warehouse cutover audit fields are system-managed."))
        result = super().write(vals)
        for cutover in self:
            cutover._operator()
        self._system().write({"state": "draft", "preview_data": False, "preview_hash": False})
        return result

    @api.ondelete(at_uninstall=False)
    def _prevent_completed_deletion(self):
        if self.filtered(lambda cutover: cutover.state == "done"):
            raise UserError(_("A completed warehouse cutover cannot be deleted."))

    def _preview_data(self):
        self._operator()
        source, target = self.source_company_id, self.target_company_id
        warehouse = self.source_warehouse_id
        engine = self.env["company.stock.fifo.migration"]
        if not warehouse.active or not source.active or not target.active:
            raise UserError(_("Choose active companies and an active Source warehouse."))
        if source.currency_id != target.currency_id:
            raise UserError(_("Both companies must use the same currency."))
        destination = self.target_warehouse_id
        if not destination or not destination.active or destination.company_id != target:
            raise UserError(_("Choose an active existing warehouse in the Target Company."))
        destination.check_access("read")
        accounts = []
        for company in source | target:
            account = company.account_stock_valuation_id
            journal = company.account_stock_journal_id
            if ((journal and (not journal.active
                    or not journal.filtered_domain(journal._check_company_domain(company))))
                    or (account and (not account.active
                    or not account.filtered_domain(account._check_company_domain(company))
                    or account.account_type != "asset_current"))):
                raise UserError(_("Configure the inventory valuation account and stock journal for %s first.", company.name))
            accounts.append({"company": company.id, "account": account.id,
                             "journal": company.account_stock_journal_id.id,
                             "journal_write": fields.Datetime.to_string(company.account_stock_journal_id.write_date),
                             "account_write": fields.Datetime.to_string(account.write_date),
                             "company_write": fields.Datetime.to_string(company.write_date)})
        locations = self.env["stock.location"].with_context(active_test=False).search([
            ("id", "child_of", warehouse.view_location_id.id),
            ("company_id", "=", source.id), ("usage", "in", ["view", "internal", "transit"]),
        ], order="parent_path, id", limit=engine.MAX_QUANTS + 1)
        if len(locations) > engine.MAX_QUANTS:
            raise UserError(_("Too many locations for one cutover."))
        if locations.filtered("valuation_account_id"):
            raise UserError(_("Custom valuation accounts on Source locations need separate review."))
        Quant = company_model(self, "stock.quant", source)
        if not self.release_source_reservations and Quant.search_count([
            ("company_id", "=", source.id), ("location_id", "in", locations.ids),
            ("inventory_quantity_set", "=", True),
        ], limit=1):
            raise UserError(_("Apply or discard pending inventory counts in this warehouse before Preview."))
        selected = Quant.search([
            ("company_id", "=", source.id), ("location_id", "in", locations.ids),
            ("location_id.is_valued_internal", "=", True), ("quantity", "!=", 0),
        ], order="product_id, lot_id, location_id, in_date, id", limit=engine.MAX_QUANTS + 1)
        if not selected:
            raise UserError(_("This warehouse has no stock to move."))
        if len(selected) > engine.MAX_QUANTS or len(selected.product_id) > engine.MAX_PRODUCTS:
            raise UserError(_("This warehouse exceeds the safe cutover size limit."))
        products = company_model(self, "product.product", source).browse(selected.product_id.ids)
        all_quants = Quant.search([
            ("company_id", "=", source.id), ("product_id", "in", products.ids),
            ("location_id.is_valued_internal", "=", True), ("quantity", "!=", 0),
        ], order="product_id, lot_id, location_id, in_date, id", limit=engine.MAX_QUANTS + 1)
        if len(all_quants) > engine.MAX_QUANTS:
            raise UserError(_("Too many stock rows for one cutover."))
        if not self.release_source_reservations and Quant.search_count([
            ("company_id", "=", source.id), ("product_id", "in", products.ids),
            ("location_id.is_valued_internal", "=", True), ("inventory_quantity_set", "=", True),
        ], limit=1):
            raise UserError(_("Apply or discard pending inventory counts for these products before Preview."))
        self._check_stock_blockers(all_quants, set(selected.ids))
        operations = source_operations_plan(self, source, products, locations.ids, selected) \
            if self.release_source_reservations else False
        Move = company_model(self, "stock.move", source)
        if not self.release_source_reservations and Move.search_count([("company_id", "=", source.id), ("product_id", "in", products.ids),
                              ("state", "not in", ["done", "cancel"])], limit=1):
            raise UserError(_("Finish or cancel open stock operations for these products first."))
        if Move.search_count([("company_id", "=", source.id), ("product_id", "in", products.ids),
                              ("state", "=", "done"), ("date", ">", self.cutover_at)], limit=1):
            raise UserError(_("Stock changed after Preview. Build a fresh Preview."))
        selected_ids = set(selected.ids)
        source_plans = source_product_plans(self, products, all_quants, selected,
                                          warehouse_id=warehouse.id, copying=True)
        matches = product_matches(self, products)
        matched = {row["source"]: row for row in matches}
        if any(product.valuation == "real_time" for product in products):
            for company in source | target:
                if not company.account_stock_journal_id:
                    raise UserError(_("Perpetual inventory needs a stock journal in %s.", company.name))
            if not target.account_stock_valuation_id and any(
                    product.valuation == "real_time" and not matched[product.id]["target"] for product in products):
                raise UserError(_("Set the Target Company's inventory account before creating new inventory products."))
        target_baseline = target_stock_state(self, self.env["product.product"].browse(
            [row["target"] for row in matches if row["target"]]))
        location_plan = location_matches(self, locations)
        plans_by_product = {plan["source"]: plan for plan in source_plans}
        rows, tranches, allocations = [], [], []
        for product in products:
            plan = plans_by_product[product.id]
            account = self.env["account.account"].browse(plan["account"])
            quantity, pool, product_rows = plan["quantity"], plan["company_quantity"], plan["tranches"]
            remaining = quantity
            value = 0
            for row in product_rows:
                take = row["quantity"] if product.lot_valuated else min(remaining, row["quantity"])
                if not product.uom_id.is_zero(take):
                    allocations.append({**row, "selected_quantity": take})
                    value += take * row["unit_value"]
                remaining -= take
            rows.append({"source": product.id, "name": product.display_name, "unit": product.uom_id.name,
                         "target": matched[product.id]["target"],
                         "target_name": matched[product.id]["target_name"],
                         "cost_method": product.cost_method, "valuation": product.valuation,
                         "target_valuation": matched[product.id]["target_valuation"],
                         "archived": not product.active,
                         "account": account.id, "account_write": fields.Datetime.to_string(account.write_date),
                         "quantity": quantity, "value": value, "company_quantity": pool,
                         "shared": not bool(product.company_id), "lot_valuated": product.lot_valuated,
                         "write": fields.Datetime.to_string(product.write_date),
                         "template_write": fields.Datetime.to_string(product.product_tmpl_id.write_date),
                         "category_write": fields.Datetime.to_string(product.categ_id.write_date),
                         "company_value": product.total_value})
            tranches.extend(product_rows)
        if len(tranches) > engine.MAX_TRANCHES:
            raise UserError(_("Too many remaining FIFO receipts for one cutover."))
        quant_rows = [{"id": quant.id, "product": quant.product_id.id, "location": quant.location_id.id,
                       "lot": quant.lot_id.id or 0, "quantity": quant.quantity,
                       "in_date": fields.Datetime.to_string(quant.in_date), "selected": quant.id in selected_ids}
                      for quant in all_quants]
        opening_chunks(engine, {"products": rows, "quants": quant_rows}, allocations)
        templates = products.product_tmpl_id
        new_products = products.filtered(lambda product: not matched[product.id]["target"])
        copied_variant_count = sum(
            len(new_products.filtered(lambda product: product.product_tmpl_id == template))
            if template.has_dynamic_attributes() else prod(
                len(line.value_ids) for line in template.attribute_line_ids
                if line.attribute_id.create_variant != "no_variant"
            ) for template in new_products.product_tmpl_id
        )
        if copied_variant_count > engine.MAX_PRODUCTS:
            raise UserError(_("These product families would create more than %s variants. Review a smaller cutover separately.", engine.MAX_PRODUCTS))
        categories = products.categ_id
        parents = categories.parent_id
        while parents:
            categories |= parents
            parents = parents.parent_id
        roles = {warehouse[role].id: role for role in LOCATION_ROLES if warehouse[role]}
        return {"warehouse": warehouse.id, "warehouse_write": fields.Datetime.to_string(warehouse.write_date),
                "target_company": target.id, "target_warehouse": destination.id,
                "target_warehouse_name": destination.display_name,
                "target_warehouse_write": fields.Datetime.to_string(destination.write_date),
                "cutover": fields.Datetime.to_string(self.cutover_at),
                "accounts": accounts, "products": rows, "tranches": tranches, "allocations": allocations,
                "source_plans": source_plans,
                "product_matches": matches, "location_matches": location_plan,
                "target_baseline": target_baseline,
                "release_source_reservations": self.release_source_reservations, "source_operations": operations,
                "copied_variant_count": copied_variant_count,
                "categories": [{"id": category.id, "name": category.name, "parent": category.parent_id.id,
                    "write": fields.Datetime.to_string(category.write_date)} for category in categories.sorted("id")],
                "units": [{"id": unit.id, "factor": unit.factor, "rounding": unit.rounding,
                    "write": fields.Datetime.to_string(unit.write_date)} for unit in products.uom_id.sorted("id")],
                "attributes": [{"template": template.id, "lines": [
                    {"attribute": line.attribute_id.id,
                     "attribute_write": fields.Datetime.to_string(line.attribute_id.write_date),
                     "values": [{"id": value.product_attribute_value_id.id, "price": value.price_extra,
                         "write": fields.Datetime.to_string(value.product_attribute_value_id.write_date)}
                         for value in line.product_template_value_ids.sorted("id")]}
                    for line in template.attribute_line_ids.sorted("id")]} for template in templates.sorted("id")],
                "quants": quant_rows, "locations": [{"id": location.id, "name": location.name,
                    "parent": location.location_id.id, "usage": location.usage, "active": location.active,
                    "role": roles.get(location.id, "")} for location in locations],
                "lots": [{"id": lot.id, "write": fields.Datetime.to_string(lot.write_date),
                          **{field: str(lot[field]) if lot[field] else False for field in LOT_FIELDS}}
                         for lot in selected.lot_id]}

    def action_preview(self):
        self._operator()
        self._lock()
        if self.state == "done":
            raise UserError(_("A completed warehouse cutover is immutable."))
        with self.env.cr.savepoint():
            self._system().write({"cutover_at": fields.Datetime.now()})
            data = self._preview_data()
            self._system().write({"state": "review", "preview_data": data, "preview_hash": fingerprint(data)})
        return {"type": "ir.actions.client", "tag": "reload"}

    def _copy_products(self, data):
        """Reuse reviewed target identities; copy only products absent from that company."""
        target = self.target_company_id
        TargetProduct = company_model(self, "product.product", target)
        selected_ids = {row["source"] for row in data["products"]}
        result = {row["source"]: TargetProduct.browse(row["target"])
                  for row in data["product_matches"] if row["target"] and row["source"] in selected_ids}
        products = company_model(self, "product.product", self.source_company_id).browse(
            [row["source"] for row in data["products"] if row["source"] not in result],
        )
        if not products:
            return result
        Category = company_model(self, "product.category", target)
        Template = company_model(self, "product.template", target).with_context(
            tracking_disable=True, mail_create_nolog=True,
        )
        root = Category.create({
            "name": "%s / Opening Stock" % target.name,
            "property_cost_method": self.source_company_id.cost_method,
            "property_valuation": self.source_company_id.inventory_valuation,
            "property_stock_valuation_account_id": target.account_stock_valuation_id.id,
            "property_stock_journal": target.account_stock_journal_id.id,
        })
        categories = {}
        templates = {products.browse(row["source"]).product_tmpl_id.id: Template.browse(row["template_target"])
                     for row in data["product_matches"] if row["template_target"]}
        new_template_ids = set()

        def category_copy(source):
            # Odoo 19 products may legitimately have no category. Their cost
            # and valuation then come from the source company, as on this root.
            if not source:
                return root
            if source.id not in categories:
                categories[source.id] = Category.create({
                    "name": source.name,
                    "parent_id": category_copy(source.parent_id).id if source.parent_id else root.id,
                    "property_cost_method": source.with_company(self.source_company_id).property_cost_method
                        or self.source_company_id.cost_method,
                    "property_valuation": source.with_company(self.source_company_id).property_valuation
                        or self.source_company_id.inventory_valuation,
                    "property_stock_valuation_account_id": target.account_stock_valuation_id.id,
                    "property_stock_journal": target.account_stock_journal_id.id,
                })
            return categories[source.id]

        for product in products:
            source = product.product_tmpl_id
            if source.id not in templates:
                values = {name: source[name] for name in (
                    "name", "type", "is_storable", "sale_ok", "purchase_ok", "list_price",
                    "tracking", "lot_valuated", "use_expiration_date", "expiration_time",
                    "use_time", "removal_time", "alert_time", "available_in_pos",
                )}
                values.update({
                    "company_id": target.id, "uom_id": source.uom_id.id,
                    "categ_id": category_copy(source.categ_id).id,
                    "pos_categ_ids": [Command.set(source.pos_categ_ids.ids)],
                    "attribute_line_ids": [Command.create({
                        "attribute_id": line.attribute_id.id, "value_ids": [Command.set(line.value_ids.ids)],
                    }) for line in source.attribute_line_ids],
                })
                template = Template.create(values)
                new_template_ids.add(template.id)
                original_prices = {
                    value.product_attribute_value_id.id: value.price_extra
                    for value in source.attribute_line_ids.product_template_value_ids
                }
                for value in template.attribute_line_ids.product_template_value_ids:
                    value.price_extra = original_prices[value.product_attribute_value_id.id]
                templates[source.id] = template
            template = templates[source.id]
            value_ids = product.product_template_attribute_value_ids.product_attribute_value_id.ids
            combination = template.attribute_line_ids.product_template_value_ids.filtered(
                lambda value: value.product_attribute_value_id.id in value_ids,
            )
            if template.id not in new_template_ids and template.product_variant_ids.filtered(
                    lambda variant: set(variant.product_template_attribute_value_ids.ids) == set(combination.ids)):
                raise UserError(_("Destination variant for %s changed. Rebuild Preview; no existing product was modified.", product.display_name))
            copied = template._create_product_variant(combination)
            if not copied:
                raise UserError(_("Odoo could not create the matching variant for %s.", product.display_name))
            copied.write({
                "default_code": product.default_code,
                "standard_price": product.standard_price,
                # A shared barcode must remain globally unique while its original survives.
                "barcode": product.barcode if product.company_id else False,
                "weight": product.weight, "volume": product.volume,
            })
            result[product.id] = copied
        return result

    def action_apply(self):
        self._operator()
        self._lock()
        if self.state != "review" or not self.preview_hash:
            raise UserError(_("Build and review Preview before moving the warehouse."))
        with self.env.cr.savepoint():
            lock_cutover_tables(self)
            self.env.invalidate_all()
            data = self._preview_data()
            if fingerprint(data) != self.preview_hash:
                raise UserError(_("Stock or configuration changed. Build a fresh Preview; nothing was moved."))
            # Check committed data before creating any target records. The same
            # stock/configuration locks remain held throughout native execution.
            with self.env.registry.cursor() as cursor:
                fresh = api.Environment(cursor, self.env.uid, dict(self.env.context))[self._name].browse(self.id)
                if fingerprint(fresh._preview_data()) != self.preview_hash:
                    raise UserError(_("Stock changed while the cutover was waiting. Build a fresh Preview."))
            copied = self._copy_products(data)
            batch = self.env["company.stock.fifo.migration"]._system().create({
                "source_company_id": self.source_company_id.id,
                "target_company_id": self.target_company_id.id,
                "warehouse_cutover_id": self.id, "cutover_at": self.cutover_at,
            })
            batch.action_create_clearing_accounts()
            context = batch._system().env.context
            self.env["company.stock.fifo.product"].with_context(context).create([
                {"batch_id": batch.id, "source_product_id": source_id, "target_product_id": target.id}
                for source_id, target in copied.items()
            ])
            self.env["company.stock.fifo.warehouse"].with_context(context).create({
                "batch_id": batch.id, "selected": True, "action": "match",
                "source_warehouse_id": self.source_warehouse_id.id,
                "target_warehouse_id": self.target_warehouse_id.id,
            })
            batch._system().write({"state": "mapped"})
            batch.action_prepare_locations()
            location_map = {row["source"]: row["target"] for row in data["location_matches"]}
            for line in batch.location_line_ids:
                line._system().write({"target_location_id": location_map[line.source_location_id.id] or False,
                                      "native_role": False})
            # Reuse matching lot identities too; never duplicate a previously moved lot.
            for line in batch.lot_line_ids:
                lot = company_model(self, "stock.lot", self.target_company_id).search([
                    ("company_id", "=", self.target_company_id.id),
                    ("product_id", "=", line.target_product_id.id),
                    ("name", "=", line.source_lot_id.name),
                ])
                if lot:
                    if len(lot) != 1:
                        raise UserError(_("Multiple destination lots match %s.", line.source_lot_id.name))
                    line._system().write({"action": "match", "target_lot_id": lot.id})
            # Source plans were just rebuilt under the cutover locks, including
            # the fresh-cursor check above. Bind the new targets once; do not
            # replay the interactive legacy Preview/Check workflow.
            snapshot = build_snapshot(batch, source_plans=data["source_plans"])
            batch._store_preview(snapshot)
            remaining = {row["source"]: row["quantity"] for row in data["products"]}
            for tranche in batch.tranche_line_ids:
                quantity = min(remaining[tranche.source_product_id.id], tranche.available_quantity)
                tranche._system().write({"selected_quantity": quantity})
                remaining[tranche.source_product_id.id] -= quantity
            batch._system().write({"allocation_reviewed": True})
            allocations = reviewed_allocations(batch, snapshot)
            opening_chunks(batch, snapshot, allocations)
            plan = {"snapshot": snapshot, "allocations": allocations}
            batch._store_checked_plan(plan)
            reviewed_products = {item["source"]: item for item in data["products"]}
            for row in plan["snapshot"]["products"]:
                reviewed = reviewed_products[row["source"]]
                value = sum(item["selected_quantity"] * item["unit_value"]
                            for item in plan["allocations"] if item["product"] == row["source"])
                product = company_model(self, "product.product", self.source_company_id).browse(row["source"])
                if (product.uom_id.compare(row["selected_quantity"], reviewed["quantity"])
                        or self.currency_id.compare_amounts(value, reviewed["value"])):
                    raise UserError(_("The native cutover plan differs from the reviewed warehouse Preview. Nothing was moved."))
            moves = execute_cutover(batch, plan)
            batch._system().write({
                "state": "done", "created_move_ids": [Command.set(moves.ids)],
                "completed_at": fields.Datetime.now(), "completed_by_id": self.env.user.id,
            })
            self._system().write({"state": "done", "stock_batch_id": batch.id})
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_open_reconciliation(self):
        self._operator()
        if not self.stock_batch_id:
            raise UserError(_("There is no completed stock cutover yet."))
        return {"type": "ir.actions.act_window", "name": _("Stock Reconciliation"),
                "res_model": "company.stock.fifo.migration", "res_id": self.stock_batch_id.id,
                "view_mode": "form", "target": "current"}

    @api.model
    def action_open_previous_cutovers(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only a Settings administrator may review stock cutovers."))
        return {"type": "ir.actions.act_window", "name": _("Earlier Stock Cutovers"),
                "res_model": "company.stock.fifo.migration", "view_mode": "list,form",
                "domain": [("warehouse_cutover_id", "=", False)],
                "context": {**self.env.context, "create": False}, "target": "current"}
