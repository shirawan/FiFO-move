from collections import defaultdict

from odoo import _, fields
from odoo.exceptions import AccessError, UserError, RedirectWarning

LOCATION_ROLES = (
    "view_location_id", "lot_stock_id", "wh_input_stock_loc_id",
    "wh_qc_stock_loc_id", "wh_output_stock_loc_id", "wh_pack_stock_loc_id",
    "pbm_loc_id", "sam_loc_id",
)
LOT_FIELDS = (
    "name", "ref", "expiration_date", "use_date", "removal_date", "alert_date",
)


def company_model(batch, model, company):
    return batch.env[model].with_context(
        allowed_company_ids=[company.id], warehouse_id=False,
        location=False, to_date=False, lot_id=None, owner_id=None, package_id=None,
        search_location=False, search_warehouse=False, strict=False,
    ).with_company(company)


def source_product_plans(record, products, quants, selected_quants, *, warehouse_id=None, copying=False):
    """One source-stock policy for both cutover interfaces; report product problems together."""
    _ = record.env._
    company = record.source_company_id
    compatible = products.filtered_domain(products._check_company_domain(company))
    owned = quants.filtered(lambda quant: not quant.owner_id or quant.owner_id == company.partner_id)
    by_product = owned.grouped("product_id")
    selected = selected_quants.grouped("product_id")
    plans, problems = [], []
    for product in products:
        try:
            if product not in compatible:
                raise UserError(_("Product %(product)s belongs to %(owner)s and is not available to Source Company %(company)s.",
                                  product=product.display_name, owner=product.company_id.display_name,
                                  company=company.display_name))
            if not product.is_storable:
                raise RedirectWarning(_(
                    "The stocked source record has Track Inventory OFF: %(name)s.\n"
                    "Product ID: %(product)s; template ID: %(template)s; Source Company: %(company)s.\n"
                    "A same-name product or target-company copy is a different record. "
                    "Open this exact source product, save Track Inventory if appropriate, then rebuild Preview. "
                    "Nothing has been moved.",
                    name=product.display_name, product=product.id, template=product.product_tmpl_id.id,
                    company=company.display_name,
                ), {"type": "ir.actions.act_window", "res_model": "product.product",
                    "res_id": product.id, "views": [(False, "form")],
                    "context": {"allowed_company_ids": company.ids, "active_test": False}}, _("Open Source Product"))
            account = product._get_product_accounts()["stock_valuation"]
            if product.valuation == "real_time" and (
                    not account or not account.active or account.account_type != "asset_current"
                    or not account.filtered_domain(account._check_company_domain(company))):
                raise UserError(_("Configure the effective inventory valuation account for %(product)s in %(company)s.",
                                  product=product.display_name, company=company.name))
            if copying and product.product_tmpl_id.attribute_line_ids.product_template_value_ids.exclude_for:
                raise UserError(_("Product %s has variant exclusions. Review this complex product separately before moving stock.", product.display_name))
            if record.env["ir.config_parameter"].search_count([
                    ("key", "=", marker_key(company.id, product.id, warehouse_id))], limit=1):
                raise UserError(_("This Source product already has a completed stock cutover: %s.", product.display_name))
            pool_quants = by_product.get(product, quants.browse())
            picked = selected.get(product, quants.browse())
            quantity, pool = sum(picked.mapped("quantity")), sum(pool_quants.mapped("quantity"))
            if product.uom_id.is_zero(quantity):
                raise UserError(_("Deselect products with no stock in the chosen warehouses: %s", product.display_name))
            if product.uom_id.compare(product._with_valuation_context().qty_available, pool):
                raise UserError(_("Valued stock quantities disagree for %s. Reconcile Odoo first.", product.display_name))
            if product.tracking != "none" and any(not quant.lot_id for quant in picked):
                raise UserError(_("Tracked stock is missing a lot/serial number: %s.", product.display_name))
            tranches = []
            if product.lot_valuated:
                for lot in picked.lot_id.with_env(product.env):
                    lot_quantity = sum(quant.quantity for quant in pool_quants if quant.lot_id.id == lot.id)
                    selected_quantity = sum(quant.quantity for quant in picked if quant.lot_id.id == lot.id)
                    if product.uom_id.compare(lot_quantity, selected_quantity):
                        raise UserError(_("Partial transfer of a lot-valued lot is unsupported: %s. "
                                          "Select all its stocked locations to preserve valuation.", lot.name))
                    tranches.extend(valuation_rows(product, lot_quantity, selected_quantity, lot=lot))
            else:
                tranches = valuation_rows(product, pool, quantity)
            plans.append({"source": product.id, "quantity": quantity, "company_quantity": pool,
                          "account": account.id, "tranches": tranches})
        except AccessError:
            raise
        except (UserError, RedirectWarning) as error:
            problems.append(error)
    if len(problems) == 1:
        raise problems[0]
    if problems:
        raise UserError(_("Review these stocked products, then rebuild Preview. Nothing was moved:") + "\n\n" +
                        "\n\n".join("%s. %s" % (number, error.args[0])
                                      for number, error in enumerate(problems, 1)))
    return plans


def source_operations_plan(record, company, products, location_ids, quants):
    """Review open operations; release only reservations inside the moved warehouse."""
    limit = record.env["company.stock.fifo.migration"].MAX_QUANTS
    moves = company_model(record, "stock.move", company).search([
        ("company_id", "=", company.id), ("product_id", "in", products.ids),
        ("state", "not in", ["done", "cancel"]),
        "|", ("location_id", "in", list(location_ids)), ("location_dest_id", "in", list(location_ids)),
    ], order="id", limit=limit + 1)
    if len(moves) > limit:
        raise UserError(_("Too many unfinished operations for one cutover."))
    lines = company_model(record, "stock.move.line", company).search([
        ("company_id", "=", company.id), ("product_id", "in", products.ids),
        ("location_id", "in", list(location_ids)), ("state", "not in", ["done", "cancel"]),
        ("quantity_product_uom", ">", 0),
    ], order="id", limit=limit + 1)
    if len(lines) > limit:
        raise UserError(_("Too many reservations for one cutover."))
    if any(line.package_id or line.result_package_id or line.owner_id
           or not line.move_id for line in lines):
        raise UserError(_("Packed or consigned reservations cannot be released by this mover."))
    # A move may have reservation lines in several warehouses. Include the
    # parents of the reviewed lines without touching their outside reservations.
    moves |= lines.move_id
    if len(moves) > limit:
        raise UserError(_("Too many unfinished operations for one cutover."))
    reserved, expected = defaultdict(float), defaultdict(float)
    for quant in quants:
        expected[(quant.product_id.id, quant.location_id.id, quant.lot_id.id)] += quant.reserved_quantity
    for line in lines:
        reserved[(line.product_id.id, line.location_id.id, line.lot_id.id)] += line.quantity_product_uom
    product_by_id = {product.id: product for product in products}
    if any(product_by_id[key[0]].uom_id.compare(reserved[key], expected[key])
           for key in expected.keys() | reserved.keys()):
        raise UserError(_("Reservation quantities disagree with the stock rows. Repair them before cutover."))
    return {
        "operations": [{"id": move.id, "name": move.picking_id.display_name or move.display_name,
                        "product": move.product_id.id, "quantity": move.product_uom_qty,
                        "picked": move.picked, "state": move.state, "write": fields.Datetime.to_string(move.write_date)}
                       for move in moves],
        "lines": [{"id": line.id, "move": line.move_id.id, "product": line.product_id.id,
                   "name": line.product_id.display_name, "location": line.location_id.id,
                   "location_name": line.location_id.complete_name, "lot": line.lot_id.id,
                   "quantity": line.quantity_product_uom, "unit": line.product_id.uom_id.display_name,
                   "picked": line.picked, "write": fields.Datetime.to_string(line.write_date)} for line in lines],
    }


def marker_key(company_id, product_id, warehouse_id=None):
    key = "company_stock_fifo_migration.applied.%s.%s" % (company_id, product_id)
    return "%s.warehouse.%s" % (key, warehouse_id) if warehouse_id else key


def valuation_rows(product, pool_quantity, selected_quantity, lot=False):
    """Use native FIFO receipts or native current average/standard closing cost."""
    if product.cost_method == "fifo":
        return _fifo_rows(product, pool_quantity, selected_quantity, lot=lot)
    cost = lot.standard_price if lot else product.standard_price
    if cost < 0:
        raise UserError(product.env._("Negative inventory cost needs review: %s", product.display_name))
    return [{"product": product.id, "move": False, "lot": lot.id if lot else 0,
             "date": "", "quantity": pool_quantity, "unit_value": cost,
             "partial": product.uom_id.compare(selected_quantity, pool_quantity) != 0}]


def validate_warehouse(batch, line):
    _ = batch.env._
    source = line.source_warehouse_id
    if not source.active or source.company_id != batch.source_company_id:
        raise UserError(_("The Source warehouse is archived or belongs to another company: %s",
                          source.display_name))
    if line.action == "create":
        if line.target_warehouse_id or not line.target_name or not line.target_code:
            raise UserError(_("For Recreate, enter a name/code and clear Existing Warehouse: %s",
                              source.display_name))
        duplicate = batch.env["stock.warehouse"].with_context(active_test=False).search([
            ("company_id", "=", batch.target_company_id.id),
            "|", ("code", "=", line.target_code), ("name", "=", line.target_name),
        ], limit=1)
        if duplicate:
            raise UserError(_("A Target warehouse already uses this name/code: %s",
                              duplicate.display_name))
    else:
        target = line.target_warehouse_id
        if not target or not target.active or target.company_id != batch.target_company_id:
            raise UserError(_("Choose an active Target Company warehouse for %s.", source.display_name))
        if not batch.warehouse_cutover_id and (target.reception_steps != source.reception_steps
                or target.delivery_steps != source.delivery_steps
                or target.manufacture_steps != source.manufacture_steps):
            raise UserError(_("Receiving/delivery steps differ for %s. Configure the Target "
                              "warehouse first, or select Recreate.", source.display_name))
        validate_native_configuration(batch, target)


def validate_native_configuration(batch, warehouse):
    _ = batch.env._
    company = batch.target_company_id
    for role in LOCATION_ROLES:
        location = warehouse[role]
        if location and location.company_id != company:
            raise UserError(_("Target warehouse location belongs to another company: %s",
                              location.display_name))
    for role in (
        "in_type_id", "out_type_id", "int_type_id", "pick_type_id",
        "pack_type_id", "qc_type_id", "store_type_id", "xdock_type_id",
        "manu_type_id", "pbm_type_id", "sam_type_id",
    ):
        operation = warehouse[role]
        if not operation:
            continue
        if operation.company_id != company or operation.warehouse_id != warehouse:
            raise UserError(_("Target operation type has inconsistent company/warehouse: %s",
                              operation.display_name))
        for location in operation.default_location_src_id | operation.default_location_dest_id:
            if location.usage in {"internal", "transit", "view"}:
                if (location.company_id != company
                        or not location.parent_path.startswith(warehouse.view_location_id.parent_path)):
                    raise UserError(batch.env._("An operation type points outside its Target warehouse."))
    rules = batch.env["stock.rule"].search([("warehouse_id", "=", warehouse.id)])
    for rule in rules:
        if rule.company_id != company:
            raise UserError(_("A generated Target warehouse rule belongs to another company: %s",
                              rule.display_name))
        if rule.picking_type_id and rule.picking_type_id.company_id != company:
            raise UserError(_("A Target warehouse rule uses another company's operation type."))
        if rule.route_id.company_id and rule.route_id.company_id != company:
            raise UserError(_("A Target warehouse rule uses another company's route."))


def prepare_locations(batch):
    _ = batch.env._
    selected = batch.warehouse_line_ids.filtered("selected")
    if not selected:
        raise UserError(_("Select the Source warehouses to include; none are selected automatically."))
    if not batch.product_line_ids.filtered("selected"):
        raise UserError(_("Select at least one mapped inventory product."))
    for line in selected:
        validate_warehouse(batch, line)
    batch.location_line_ids._system().unlink()
    batch.lot_line_ids._system().unlink()
    LocationLine = batch.env["company.stock.fifo.location"]
    LotLine = batch.env["company.stock.fifo.lot"]
    context = batch._system().env.context
    values = []
    for line in selected:
        warehouse = line.source_warehouse_id
        roles = {warehouse[role].id: role for role in LOCATION_ROLES if warehouse[role]}
        locations = batch.env["stock.location"].with_context(active_test=False).search([
            ("id", "child_of", warehouse.view_location_id.id),
            ("company_id", "=", batch.source_company_id.id),
            ("usage", "in", ["view", "internal", "transit"]),
        ], order="parent_path, id")
        for location in locations:
            role = roles.get(location.id, "")
            target = line.target_warehouse_id[role] if role and line.action == "match" else False
            values.append({
                "batch_id": batch.id, "source_location_id": location.id,
                "warehouse_line_id": line.id, "native_role": role,
                "target_location_id": target.id if target else False,
                "proposed_path": location.complete_name,
            })
    if len(values) > batch.MAX_QUANTS:
        raise UserError(_("Too many warehouse locations for one cutover."))
    LocationLine.with_context(context).create(values)
    products = batch.product_line_ids.filtered("selected")
    quants = company_model(batch, "stock.quant", batch.source_company_id).search([
        ("company_id", "=", batch.source_company_id.id),
        ("product_id", "in", products.source_product_id.ids),
        ("location_id", "in", batch.location_line_ids.source_location_id.ids),
        ("quantity", ">", 0),
    ], limit=batch.MAX_QUANTS + 1)
    if len(quants) > batch.MAX_QUANTS:
        raise UserError(_("Too many stock rows for one cutover."))
    targets = {line.source_product_id.id: line.target_product_id.id for line in products}
    lot_qty = defaultdict(float)
    location_qty = defaultdict(float)
    for quant in quants:
        location_qty[quant.location_id.id] += quant.quantity
        if quant.lot_id:
            lot_qty[quant.lot_id.id] += quant.quantity
    for line in batch.location_line_ids:
        line._system().write({"source_quantity": location_qty[line.source_location_id.id]})
    lots = batch.env["stock.lot"].browse(list(lot_qty))
    LotLine.with_context(context).create([{
        "batch_id": batch.id, "source_lot_id": lot.id,
        "target_product_id": targets[lot.product_id.id],
        "source_quantity": lot_qty[lot.id],
    } for lot in lots])


def _location_plan(batch):
    _ = batch.env._
    warehouses = batch.warehouse_line_ids.filtered("selected")
    if not warehouses:
        raise UserError(_("Select at least one warehouse."))
    selected_ids = set(warehouses.ids)
    locations = batch.location_line_ids.filtered(
        lambda line: line.warehouse_line_id.id in selected_ids
    )
    if not locations:
        raise UserError(_("Click Prepare Locations after selecting warehouses."))
    seen_warehouses = set()
    seen_locations = set()
    warehouse_rows = []
    for line in warehouses:
        validate_warehouse(batch, line)
        if line.action == "match":
            if line.target_warehouse_id.id in seen_warehouses:
                raise UserError(_("Two Source warehouses cannot map to the same Target warehouse."))
            seen_warehouses.add(line.target_warehouse_id.id)
        warehouse_rows.append({
            "line": line.id, "source": line.source_warehouse_id.id,
            "action": line.action, "target": line.target_warehouse_id.id or 0,
            "name": line.target_name, "code": line.target_code,
            "reception": line.source_warehouse_id.reception_steps,
            "delivery": line.source_warehouse_id.delivery_steps,
            "manufacture": line.source_warehouse_id.manufacture_steps,
        })
    rows = []
    for line in locations:
        source = line.source_location_id
        target = line.target_location_id
        if source.company_id != batch.source_company_id:
            raise UserError(_("A Source location belongs to another company."))
        if source.valuation_account_id:
            raise UserError(_("Custom valuation on internal locations needs separate review: %s",
                              source.display_name))
        if line.warehouse_line_id.action == "match":
            warehouse = line.warehouse_line_id.target_warehouse_id
            if not target and batch.warehouse_cutover_id:
                # Missing child paths are created beneath the already mapped
                # destination parent; never recreate the warehouse itself.
                pass
            elif (not target or target.company_id != batch.target_company_id
                    or target.usage != source.usage
                    or not target.parent_path.startswith(warehouse.view_location_id.parent_path)
                    or target.active != source.active):
                raise UserError(_("Map the Source location to an equivalent Target location: %s",
                                  source.display_name))
            if target.valuation_account_id:
                raise UserError(_("The Target internal location has a custom valuation account."))
            if target and target.id in seen_locations and not batch.warehouse_cutover_id:
                raise UserError(_("Each Source location must have its own Target location."))
            if target:
                seen_locations.add(target.id)
        elif target:
            raise UserError(_("Recreated warehouse locations are generated during Apply; clear this match."))
        rows.append({
            "line": line.id, "source": source.id, "parent": source.location_id.id,
            "name": source.name, "usage": source.usage, "active": source.active,
            "warehouse_line": line.warehouse_line_id.id, "role": line.native_role or "",
            "target": target.id or 0,
        })
    source_roots = warehouses.source_warehouse_id.view_location_id.ids
    prepared = {row["source"] for row in rows}
    actual = batch.env["stock.location"].with_context(active_test=False).search([
        ("id", "child_of", source_roots),
        ("company_id", "=", batch.source_company_id.id),
        ("usage", "in", ["view", "internal", "transit"]),
    ])
    if set(actual.ids) != prepared:
        raise UserError(_("The warehouse hierarchy changed. Prepare Locations again."))
    return warehouse_rows, rows


def _lot_plan(batch, selected_quants):
    _ = batch.env._
    source_lots = selected_quants.lot_id
    lines = {line.source_lot_id.id: line for line in batch.lot_line_ids}
    result = []
    for source in source_lots:
        line = lines.get(source.id)
        if not line:
            raise UserError(_("Prepare Locations again to include lot %s.", source.name))
        target = line.target_lot_id
        if line.action == "match":
            if (not target or target.company_id != batch.target_company_id
                    or target.product_id != line.target_product_id
                    or any(target[field] != source[field] for field in LOT_FIELDS)):
                raise UserError(_("Match lot %s to the same lot identity/dates in the Target Company.", source.name))
        else:
            if target:
                raise UserError(_("Clear the Existing Lot when choosing Create Same Lot."))
            duplicate = batch.env["stock.lot"].search([
                ("name", "=", source.name), ("product_id", "=", line.target_product_id.id),
                ("company_id", "=", batch.target_company_id.id),
            ], limit=1)
            if duplicate:
                raise UserError(_("Target lot %s already exists; explicitly select Use Existing.", source.name))
        result.append({
            "line": line.id, "source": source.id, "product": line.target_product_id.id,
            "action": line.action, "target": target.id or 0,
            "data": {field: (
                fields.Datetime.to_string(source[field]) if field.endswith("_date") and source[field]
                else source[field] or False
            ) for field in LOT_FIELDS},
        })
    return result


def _fifo_rows(product, pool_quantity, selected_quantity, lot=False):
    _ = product.env._
    moves, first_quantity = product._run_fifo_get_stack(lot=lot)
    rows = []
    for index, move in enumerate(moves):
        quantity = first_quantity if index == 0 else move._get_valued_qty(lot=lot)
        denominator = move._get_valued_qty()
        if denominator <= 0 or quantity <= 0 or move.value < 0:
            raise UserError(_("Invalid FIFO receipt quantity/value for %s.", product.display_name))
        rows.append({
            "product": product.id, "move": move.id, "lot": lot.id if lot else 0,
            "date": fields.Datetime.to_string(move.date),
            "quantity": quantity, "unit_value": move.value / denominator,
            "receipt_quantity": denominator, "receipt_value": move.value,
            "partial": product.uom_id.compare(selected_quantity, pool_quantity) != 0,
        })
    if product.uom_id.compare(sum(row["quantity"] for row in rows), pool_quantity):
        raise UserError(_("FIFO receipts do not cover the recorded stock of %s; reconcile Odoo first.",
                          product.display_name))
    return rows


def cost_methods_compatible(source, target, whole_warehouse=False):
    return source == target or (whole_warehouse and source == "standard" and target == "fifo")


def valuation_modes_compatible(source, target, whole_warehouse=False):
    return source == target or (whole_warehouse and source == "periodic" and target == "real_time")


def target_stock_state(record, products):
    """Baseline existing destination stock so cutovers reconcile additions, not replacement."""
    company = record.target_company_id
    products = company_model(record, "product.product", company).browse(products.ids)
    quants = company_model(record, "stock.quant", company).search([
        ("company_id", "=", company.id), ("product_id", "in", products.ids),
        ("location_id.is_valued_internal", "=", True), ("quantity", "!=", 0),
    ], order="id", limit=record.env["company.stock.fifo.migration"].MAX_QUANTS + 1)
    if len(quants) > record.env["company.stock.fifo.migration"].MAX_QUANTS:
        raise UserError(record.env._("Too many existing destination stock rows for one cutover."))
    rows = []
    for product in products:
        quantity = product._with_valuation_context().qty_available
        if quantity < 0:
            raise UserError(record.env._("Destination product %s has negative stock; its opening value cannot be preserved.", product.display_name))
        fifo = []
        if product.cost_method == "fifo":
            owned = quants.filtered(lambda quant: quant.product_id == product and
                                    (not quant.owner_id or quant.owner_id == company.partner_id))
            pools = [(lot.id, sum(quant.quantity for quant in owned if quant.lot_id == lot))
                     for lot in owned.lot_id] if product.lot_valuated else [(0, quantity)]
            for lot_id, pool in pools:
                if not product.uom_id.is_zero(pool):
                    lot = record.env["stock.lot"].browse(lot_id).with_env(product.env) if lot_id else None
                    fifo.append({"lot": lot_id, "quantity": pool, "value": product._run_fifo(pool, lot=lot)})
        rows.append({"target": product.id, "quantity": quantity, "value": product.total_value, "fifo": fifo,
                     "write": fields.Datetime.to_string(product.write_date),
                     "category_write": fields.Datetime.to_string(product.categ_id.write_date)})
    return {"products": rows, "quants": [{"product": quant.product_id.id, "location": quant.location_id.id,
            "lot": quant.lot_id.id or 0, "quantity": quant.quantity,
            "in_date": fields.Datetime.to_string(quant.in_date)} for quant in quants]}


def build_snapshot(batch, *, source_plans=None):
    _ = batch.env._
    source_company = batch.source_company_id
    target_company = batch.target_company_id
    realtime = any(product.with_company(source_company).valuation == "real_time"
                   for product in batch.product_line_ids.filtered("selected").source_product_id)
    if source_company.currency_id != target_company.currency_id:
        raise UserError(_("Both companies must use the same currency for this cutover."))
    if not batch.cutover_at or batch.cutover_at > fields.Datetime.now():
        raise UserError(_("Cutover time must be now or earlier."))
    for company, account in (
        (source_company, batch.source_clearing_account_id),
        (target_company, batch.target_clearing_account_id),
    ):
        batch._validate_clearing_account(company, account)
        journal = company.account_stock_journal_id
        if realtime and (not journal or not journal.active):
            raise UserError(_("Configure the stock journal for %s.", company.name))
        if journal and not journal.filtered_domain(journal._check_company_domain(company)):
            raise UserError(_("The stock journal belongs to another company: %s.", company.name))
    warehouses, locations = _location_plan(batch)
    location_ids = {row["source"] for row in locations if row["usage"] in {"internal", "transit"}}
    products = batch.product_line_ids.filtered("selected")
    if not products:
        raise UserError(_("Select at least one inventory product."))
    source_products = products.source_product_id
    source_env = company_model(batch, "product.product", source_company)
    target_env = company_model(batch, "product.product", target_company)
    source_records = {product.id: product for product in source_env.browse(source_products.ids)}
    target_records = {product.id: product for product in target_env.browse(products.target_product_id.ids)}
    Quant = company_model(batch, "stock.quant", source_company)
    quants = Quant.search([
        ("company_id", "=", source_company.id),
        ("product_id", "in", source_products.ids),
        ("location_id.is_valued_internal", "=", True),
        ("quantity", "!=", 0),
    ], order="product_id, lot_id, location_id, in_date, id", limit=batch.MAX_QUANTS + 1)
    if len(quants) > batch.MAX_QUANTS:
        raise UserError(_("Too many stock rows for one cutover."))
    for quant in quants:
        if (quant.quantity < 0 or (quant.location_id.id in location_ids and (
                (quant.reserved_quantity and not batch.release_source_reservations)
                or quant.owner_id or quant.package_id))):
            raise UserError(_("Resolve negative stock, reservations, consignment and packages first: %s / %s",
                              quant.product_id.display_name, quant.location_id.display_name))
    selected_quants = quants.filtered(lambda quant: quant.location_id.id in location_ids)
    owned_quants = quants.filtered(lambda quant: not quant.owner_id or quant.owner_id == source_company.partner_id)
    by_product = {product.id: group for product, group in owned_quants.grouped("product_id").items()}
    selected_by_product = {product.id: group for product, group in selected_quants.grouped("product_id").items()}
    if not selected_quants:
        raise UserError(_("No stock exists for the selected products and warehouses."))
    Move = company_model(batch, "stock.move", source_company)
    operations = source_operations_plan(batch, source_company, source_products, location_ids,
                                        selected_quants) if batch.release_source_reservations else False
    if not batch.release_source_reservations and Move.search_count([
        ("product_id", "in", source_products.ids),
        ("company_id", "=", source_company.id),
        ("state", "not in", ["done", "cancel"]),
    ], limit=1):
        raise UserError(_("Finish or cancel open stock operations for the selected Source products first."))
    if Move.search_count([
        ("product_id", "in", source_products.ids),
        ("company_id", "=", source_company.id),
        ("state", "=", "done"), ("date", ">", batch.cutover_at),
    ], limit=1):
        raise UserError(_("Source stock changed after the requested cutover time. Choose a current snapshot."))
    target_moves = company_model(batch, "stock.move", target_company)
    if not batch.warehouse_cutover_id and target_moves.search_count([
        ("product_id", "in", products.target_product_id.ids),
        ("company_id", "=", target_company.id),
        ("state", "!=", "cancel"),
    ], limit=1):
        raise UserError(_("Mapped Target products must have no existing stock-move history."))
    if not batch.warehouse_cutover_id and company_model(batch, "stock.quant", target_company).search_count([
        ("company_id", "=", target_company.id),
        ("product_id", "in", products.target_product_id.ids), ("quantity", "!=", 0),
    ], limit=1):
        raise UserError(_("Mapped Target products already have stock."))
    target_baseline = target_stock_state(batch, products.target_product_id)
    baseline_by_product = {row["target"]: row for row in target_baseline["products"]}
    # Only the whole-warehouse Apply supplies a plan, freshly recomputed under
    # table locks. This module function is not an RPC method or context bypass.
    if source_plans is None:
        source_plans = source_product_plans(
            batch, source_env.browse(source_products.ids), quants, selected_quants,
            warehouse_id=batch.warehouse_cutover_id.source_warehouse_id.id or None,
        )
    plans_by_product = {row["source"]: row for row in source_plans}
    if set(plans_by_product) != set(source_products.ids):
        raise UserError(_("The source stock plan does not match the selected products."))
    product_rows = []
    tranches = []
    outside = []
    for line in products:
        source = source_records[line.source_product_id.id]
        target = target_records[line.target_product_id.id]
        if (target.company_id != target_company
                or not target.active or not target.is_storable
                or not cost_methods_compatible(source.cost_method, target.cost_method, bool(batch.warehouse_cutover_id))
                or not valuation_modes_compatible(source.valuation, target.valuation, bool(batch.warehouse_cutover_id))
                or source.uom_id != target.uom_id or source.tracking != target.tracking
                or source.lot_valuated != target.lot_valuated):
            raise UserError(_("Check company, target active status, matching costing/valuation methods, unit, tracking "
                              "and lot valuation for the matched product: %s", source.display_name))
        for product, company, clearing in (
            (source, source_company, batch.source_clearing_account_id),
            (target, target_company, batch.target_clearing_account_id),
        ):
            if product.valuation == "periodic":
                continue
            account = product._get_product_accounts()["stock_valuation"]
            if (not account or not account.active
                    or not account.filtered_domain(account._check_company_domain(company))
                    or account.account_type != "asset_current"):
                raise UserError(_("Configure the stock valuation account for %s.", product.display_name))
            if account == clearing:
                raise UserError(batch.env._("The clearing account must differ from the stock valuation account."))
        all_product_quants = by_product.get(source.id, Quant)
        selected_product_quants = selected_by_product.get(source.id, Quant)
        source_plan = plans_by_product[source.id]
        company_quantity, quantity = source_plan["company_quantity"], source_plan["quantity"]
        tranches.extend(source_plan["tranches"])
        company_value = source.total_value
        product_rows.append({
            "line": line.id, "source": source.id, "target": target.id,
            "company_quantity": company_quantity, "selected_quantity": quantity,
            "company_value": company_value, "lot_valuated": source.lot_valuated,
            "tracking": source.tracking, "uom": source.uom_id.id,
            "cost_method": source.cost_method, "target_cost_method": target.cost_method,
            "valuation": source.valuation, "target_valuation": target.valuation,
            "source_account": source._get_product_accounts()["stock_valuation"].id,
            "target_account": target._get_product_accounts()["stock_valuation"].id,
            "target_before": baseline_by_product[target.id],
        })
        for quant in all_product_quants - selected_product_quants:
            outside.append("%s / %s: %s" % (
                source.display_name, quant.location_id.display_name, quant.quantity
            ))
    tranches.sort(key=lambda row: (row["product"], row["date"], row["move"], row["lot"]))
    if len(tranches) > batch.MAX_TRANCHES:
        raise UserError(_("Too many remaining FIFO tranches for one cutover."))
    source_warehouses = batch.warehouse_line_ids.filtered("selected").source_warehouse_id
    orderpoints = batch.env["stock.warehouse.orderpoint"].search_count([
        ("warehouse_id", "in", source_warehouses.ids),
    ])
    pos_configs = batch.env["pos.config"].search([
        ("company_id", "=", source_company.id),
        ("picking_type_id.warehouse_id", "in", source_warehouses.ids),
    ])
    review = _("Reordering rules to review manually: %(rules)s. POS registers to review: %(pos)s. "
               "Use the Target warehouse's generated routes/operation types; POS picking type and "
               "ship-later configuration must be reviewed before opening the replacement company.",
               rules=orderpoints, pos=", ".join(pos_configs.mapped("name")) or "None")
    if outside:
        review += "\n" + _("Valued stock outside the selected warehouse trees remains in the Source Company:") + "\n" + "\n".join(outside)
    return {
        "source_company": source_company.id, "target_company": target_company.id,
        "cutover": fields.Datetime.to_string(batch.cutover_at),
        "source_clearing": batch.source_clearing_account_id.id,
        "target_clearing": batch.target_clearing_account_id.id,
        "source_journal": source_company.account_stock_journal_id.id,
        "target_journal": target_company.account_stock_journal_id.id,
        "warehouses": warehouses, "locations": locations,
        "release_source_reservations": batch.release_source_reservations,
        "source_operations": operations,
        "lots": _lot_plan(batch, selected_quants),
        "lot_costs": [{"lot": lot.id, "unit_value": lot.standard_price}
                      for lot in selected_quants.lot_id.with_env(source_env.env) if lot.lot_valuated],
        "products": product_rows, "tranches": tranches,
        "target_quants": target_baseline["quants"],
        "quants": [{
            "id": quant.id, "product": quant.product_id.id, "location": quant.location_id.id,
            "lot": quant.lot_id.id or 0, "quantity": quant.quantity,
            "in_date": fields.Datetime.to_string(quant.in_date),
            "selected": quant.location_id.id in location_ids,
        } for quant in quants],
        "warehouse_review": review,
    }


def reviewed_allocations(batch, snapshot):
    """Only native FIFO-prefix removal preserves the Source's remaining stack."""
    if len(batch.tranche_line_ids) != len(snapshot["tranches"]):
        raise UserError(batch.env._("The FIFO preview rows changed. Build Preview again."))
    products = {row["source"]: row for row in snapshot["products"]}
    remaining = {row["source"]: row["selected_quantity"] for row in snapshot["products"]}
    allocations = []
    lot_values = defaultdict(float)
    lot_quantities = defaultdict(float)
    for line, row in zip(batch.tranche_line_ids, snapshot["tranches"]):
        if (line.source_product_id.id != row["product"] or line.source_move_id.id != row["move"]
                or (line.source_lot_id.id or 0) != row["lot"]
                or line.available_quantity != row["quantity"] or line.unit_value != row["unit_value"]):
            raise UserError(batch.env._("The FIFO preview rows changed. Build Preview again."))
        product = line.source_product_id
        expected = row["quantity"] if products[row["product"]]["lot_valuated"] else min(
            remaining[row["product"]], row["quantity"]
        )
        if (product.uom_id.compare(line.selected_quantity, expected)
                or line.selected_quantity < 0 or line.selected_quantity > row["quantity"]):
            raise UserError(batch.env._(
                "%(product)s: allocate %(quantity)s from receipt %(receipt)s. "
                "Use the oldest remaining costs first; no receipt can be skipped.",
                product=product.display_name, quantity=expected, receipt=line.source_move_id.display_name,
            ))
        remaining[row["product"]] -= line.selected_quantity
        if not product.uom_id.is_zero(line.selected_quantity):
            allocations.append({**row, "selected_quantity": line.selected_quantity})
            if row["lot"]:
                lot_values[row["lot"]] += line.selected_quantity * row["unit_value"]
                lot_quantities[row["lot"]] += line.selected_quantity
    for source_id, quantity in remaining.items():
        product = batch.env["product.product"].browse(source_id)
        if not product.uom_id.is_zero(quantity):
            raise UserError(batch.env._("The FIFO allocation does not cover the selected stock: %s.", product.display_name))
    if any(row["partial"] for row in snapshot["tranches"]) and not batch.allocation_reviewed:
        raise UserError(batch.env._("Acknowledge the reviewed partial-company FIFO allocation before Check."))
    for row in snapshot["lot_costs"]:
        if batch.currency_id.compare_amounts(
                lot_quantities[row["lot"]] * row["unit_value"], lot_values[row["lot"]]):
            raise UserError(batch.env._("A lot's current closing cost differs from its remaining FIFO value. "
                                      "Reconcile the Source lot valuation before cutover."))
    return allocations
