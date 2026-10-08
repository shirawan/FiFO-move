"""Native stock movements, followed by atomic quantity/value reconciliation."""
import json
from math import isclose
from collections import defaultdict

from odoo import fields
from odoo.exceptions import UserError
from psycopg2.errors import LockNotAvailable

from .snapshot import company_model, marker_key, validate_native_configuration


def lock_cutover_tables(record):
    """Both front screens use the same maintenance-mode stock/configuration lock."""
    try:
        with record.env.cr.savepoint():
            record.env.cr.execute(
                "LOCK TABLE stock_move, stock_move_line, stock_quant, stock_lot, product_value, "
                "stock_warehouse, stock_location, stock_picking_type, stock_rule, stock_route, "
                "product_product, product_template, product_category, account_account, "
                "account_journal, res_company, ir_config_parameter, product_attribute, "
                "product_attribute_value, product_template_attribute_line, "
                "product_template_attribute_value, product_template_attribute_exclusion, "
                "uom_uom, decimal_precision IN SHARE ROW EXCLUSIVE MODE NOWAIT"
            )
    except LockNotAvailable as error:
        raise UserError(record.env._("Inventory is busy. Pause inventory operations across this database, "
                                     "then try Apply again. No stock was moved.")) from error


def create_locations(batch, snapshot):
    Warehouse = company_model(batch, "stock.warehouse", batch.target_company_id)
    Location = company_model(batch, "stock.location", batch.target_company_id).with_context(active_test=False)
    warehouses = {}
    for row in snapshot["warehouses"]:
        if row["action"] == "create":
            warehouse = Warehouse.create({
                "name": row["name"], "code": row["code"],
                "company_id": batch.target_company_id.id,
                "reception_steps": row["reception"], "delivery_steps": row["delivery"],
                "manufacture_steps": row["manufacture"],
            })
            batch.env["company.stock.fifo.warehouse"].browse(row["line"])._system().write({
                "created_warehouse_id": warehouse.id,
            })
        else:
            warehouse = Warehouse.browse(row["target"])
        validate_native_configuration(batch, warehouse)
        warehouses[row["line"]] = warehouse
    result, created_paths = {}, {}
    for row in snapshot["locations"]:
        warehouse = warehouses[row["warehouse_line"]]
        if row["target"]:
            location = Location.browse(row["target"])
        elif row["role"]:
            location = warehouse[row["role"]]
            if not location or location.usage != row["usage"] or location.active != row["active"]:
                raise UserError(batch.env._("The generated warehouse location differs from the reviewed source: %s.", row["name"]))
        else:
            parent = result.get(row["parent"])
            if not parent:
                raise UserError(batch.env._("The location parent is missing: %s.", row["name"]))
            key = (parent.id, row["name"], row["usage"], row["active"])
            location = created_paths.get(key) if batch.warehouse_cutover_id else None
            if not location:
                location = Location.create({
                    "name": row["name"], "usage": row["usage"], "active": row["active"],
                    "company_id": batch.target_company_id.id, "location_id": parent.id,
                })
                created_paths[key] = location
        result[row["source"]] = location
        if not row["target"]:
            batch.env["company.stock.fifo.location"].browse(row["line"])._system().write({
                "created_location_id": location.id,
            })
    return result


def create_lots(batch, snapshot):
    Lot = company_model(batch, "stock.lot", batch.target_company_id)
    result = {}
    for row in snapshot["lots"]:
        if row["action"] == "create":
            lot = Lot.create({**row["data"], "product_id": row["product"],
                              "company_id": batch.target_company_id.id})
            batch.env["company.stock.fifo.lot"].browse(row["line"])._system().write({
                "created_lot_id": lot.id,
            })
        else:
            lot = Lot.browse(row["target"])
        result[row["source"]] = lot
    return result


def clearing_location(batch, company, account):
    return company_model(batch, "stock.location", company).create({
        "name": "%s — Stock Cutover" % batch.name,
        "usage": "inventory", "company_id": company.id,
        "valuation_account_id": account.id,
    })


def native_moves(batch, company, rows):
    """Run one company's reviewed movements through Odoo as one recordset."""
    Move = company_model(batch, "stock.move", company).with_context(
        force_period_date=fields.Date.to_date(batch.cutover_at),
    )
    products = company_model(batch, "product.product", company).browse(
        [row["product"] for row in rows],
    )
    units = {product.id: product.uom_id.id for product in products}
    moves = Move.create([{
        "company_id": company.id, "product_id": row["product"],
        "product_uom": units[row["product"]], "product_uom_qty": row["quantity"],
        "location_id": row["source"], "location_dest_id": row["destination"],
        "is_inventory": True, "inventory_name": batch.name, "origin": batch.name,
    } for row in rows])
    moves._action_confirm(merge=False)
    # Confirmation may prefill incoming lines; replace only these new moves'
    # lines so the reviewed physical lot/location is counted exactly once.
    moves.move_line_ids.unlink()
    Move.env["stock.move.line"].create([{
        "move_id": move.id, "company_id": company.id, "product_id": row["product"],
        "product_uom_id": units[row["product"]], "quantity": row["quantity"],
        "location_id": row["source"], "location_dest_id": row["destination"],
        "lot_id": row["lot"], "picked": True,
    } for move, row in zip(moves, rows, strict=True)])
    # Zero is also an explicit native manual opening valuation.
    values = [{
        "move_id": move.id, "value": row["value"], "company_id": company.id,
        "date": batch.cutover_at, "description": "%s — reviewed FIFO opening" % batch.name,
    } for move, row in zip(moves, rows, strict=True) if row["value"] is not None]
    if values:
        company_model(batch, "product.value", company).create(values)
    moves.picked = True
    done = moves._action_done()
    if set(done.ids) != set(moves.ids) or any(move.state != "done" for move in moves):
        raise UserError(batch.env._("Odoo did not finish the reviewed cutover movement exactly as requested."))
    moves.date = batch.cutover_at
    return moves


def opening_chunks(batch, snapshot, allocations):
    """Attach the FIFO cost prefix to current physical locations/lots explicitly."""
    remaining = {row["id"]: row["quantity"] for row in snapshot["quants"] if row["selected"]}
    quants = [row for row in snapshot["quants"] if row["selected"]]
    def check_size(openings):
        if len(quants) + openings > batch.MAX_MOVEMENTS:
            raise UserError(batch.env._(
                "This cutover exceeds %(limit)s closing/opening movements. "
                "Split by different products, not by slices of one product's FIFO pool.",
                limit=batch.MAX_MOVEMENTS,
            ))
    check_size(0)
    products = {row["source"]: row for row in snapshot["products"]}
    chunks = []
    for allocation in allocations:
        quantity = allocation["selected_quantity"]
        product = batch.env["product.product"].browse(allocation["product"])
        for quant in quants:
            if quant["product"] != allocation["product"] or product.uom_id.is_zero(remaining[quant["id"]]):
                continue
            if products[quant["product"]]["lot_valuated"] and quant["lot"] != allocation["lot"]:
                continue
            take = min(quantity, remaining[quant["id"]])
            if product.uom_id.is_zero(take):
                continue
            chunks.append({**quant, "quantity": take,
                           "value": take * allocation["unit_value"], "receipt": allocation["move"]})
            check_size(len(chunks))
            remaining[quant["id"]] -= take
            quantity -= take
            if product.uom_id.is_zero(quantity):
                break
        if not product.uom_id.is_zero(quantity):
            raise UserError(batch.env._("The reviewed FIFO quantity cannot be assigned to physical stock."))
    for quant in quants:
        product = batch.env["product.product"].browse(quant["product"])
        if not product.uom_id.is_zero(remaining[quant["id"]]):
            raise UserError(batch.env._("The FIFO allocation leaves selected physical stock unassigned."))
    return chunks


def reconcile(batch, plan, closing, opening, locations, lots):
    snapshot = plan["snapshot"]
    currency = batch.currency_id
    rounding = []
    source_products = company_model(batch, "product.product", batch.source_company_id)
    target_products = company_model(batch, "product.product", batch.target_company_id)
    accounting_lines = []
    for moves in (closing, opening):
        valued = moves.filtered(lambda move: move.product_id.valuation == "real_time")
        if any(not move.account_move_id or move.account_move_id.state != "posted"
               or move.account_move_id.company_id != move.company_id for move in valued):
            raise UserError(batch.env._("Every cutover stock move must have a posted valuation entry in its company."))
        if (moves - valued).account_move_id:
            raise UserError(batch.env._("Periodic stock unexpectedly generated a valuation journal entry."))
        lines = moves.account_move_id.line_ids
        product_ids = set(moves.product_id.ids)
        if any(line.product_id.id not in product_ids for line in lines):
            raise UserError(batch.env._("Cutover valuation entries contain an unreviewed product."))
        accounting_lines.append({product.id: group for product, group in lines.grouped("product_id").items()})
    expected_source = defaultdict(float)
    actual_source = defaultdict(float)
    for quant in snapshot["quants"]:
        key = (quant["product"], quant["location"], quant["lot"])
        expected_source[key] += 0 if quant["selected"] else quant["quantity"]
    remaining_quants = company_model(batch, "stock.quant", batch.source_company_id).search([
        ("company_id", "=", batch.source_company_id.id),
        ("product_id", "in", [row["source"] for row in snapshot["products"]]),
        ("location_id.is_valued_internal", "=", True), ("quantity", "!=", 0),
    ])
    for quant in remaining_quants:
        actual_source[(quant.product_id.id, quant.location_id.id, quant.lot_id.id or 0)] += quant.quantity
    for key in expected_source.keys() | actual_source.keys():
        if source_products.browse(key[0]).uom_id.compare(actual_source[key], expected_source[key]):
            raise UserError(batch.env._("Source location/lot quantities changed outside the reviewed closing stock. "
                                      "No cutover changes were saved."))
    for row in snapshot["products"]:
        source = source_products.browse(row["source"])
        target = target_products.browse(row["target"])
        allocated = [item for item in plan["allocations"] if item["product"] == row["source"]]
        expected = sum(item["selected_quantity"] * item["unit_value"] for item in allocated)
        qty = target._with_valuation_context().qty_available
        value = target.total_value
        closed = closing.filtered(lambda move: move.product_id.id == source.id)
        opened = opening.filtered(lambda move: move.product_id.id == target.id)
        before = row["target_before"]
        closed_value = sum(closed.mapped("value"))
        remaining_value = row["company_value"] - closed_value
        source_value_mismatch = currency.compare_amounts(
            source.total_value + closed_value, row["company_value"])
        if row["cost_method"] == "fifo" and not row["lot_valuated"]:
            # Compare the remaining receipt stack itself. Adding separately
            # rounded closing/remainder totals can differ by a currency unit.
            selected_by_move = {item["move"]: item["selected_quantity"] for item in allocated}
            remaining_value = sum(
                (item["quantity"] - selected_by_move.get(item["move"], 0)) * item["unit_value"]
                for item in snapshot["tranches"] if item["product"] == source.id
            )
            source_value_mismatch = currency.compare_amounts(source.total_value, remaining_value)
        if (target.uom_id.compare(qty, before["quantity"] + row["selected_quantity"])
                or source.uom_id.compare(source._with_valuation_context().qty_available,
                                         row["company_quantity"] - row["selected_quantity"])
                or currency.compare_amounts(value, before["value"] + expected)
                or source_value_mismatch
                or currency.compare_amounts(sum(closed.mapped("value")), expected)
                or currency.compare_amounts(sum(opened.mapped("value")), expected)):
            raise UserError(batch.env._(
                "%(product)s did not reconcile. Target: %(qty)s units / %(value)s value; "
                "expected %(expected_qty)s / %(expected_value)s. Source remaining: %(source_qty)s / "
                "%(source_value)s (expected remaining value %(remaining_value)s). "
                "Closing moves: %(closed)s; opening moves: %(opened)s. "
                "No cutover changes were saved.",
                product=source.display_name, qty=qty, value=value,
                expected_qty=before["quantity"] + row["selected_quantity"], expected_value=before["value"] + expected,
                source_qty=source._with_valuation_context().qty_available, source_value=source.total_value,
                remaining_value=currency.round(remaining_value),
                closed=sum(closed.mapped("value")), opened=sum(opened.mapped("value")),
            ))
        if currency.compare_amounts(source.total_value + closed_value, row["company_value"]):
            rounding.append(batch.env._(
                "%(product)s: remaining FIFO receipts reconcile to %(remaining)s; separately rounded "
                "source remainder plus closing value differ from the pre-cutover total by %(difference)s %(currency)s.",
                product=source.display_name, remaining=currency.round(remaining_value),
                difference=currency.round(source.total_value + closed_value - row["company_value"]),
                currency=currency.name))
        for moves, account, clearing, direction, lines, valuation in (
            (closed, row["source_account"], snapshot["source_clearing"], -1, accounting_lines[0].get(source.id), row["valuation"]),
            (opened, row["target_account"], snapshot["target_clearing"], 1, accounting_lines[1].get(target.id), row["target_valuation"]),
        ):
            if valuation == "periodic":
                continue
            journals = moves.account_move_id
            if not journals or not lines:
                raise UserError(batch.env._("Every cutover product must have valuation journal lines."))
            if any(journal.date != fields.Date.to_date(batch.cutover_at) for journal in journals):
                raise UserError(batch.env._("Odoo changed the accounting date. Review company lock dates before cutover; "
                                          "no changes were saved."))
            stock_amount = sum(line.balance for line in lines if line.account_id.id == account)
            clearing_amount = sum(line.balance for line in lines if line.account_id.id == clearing)
            if (currency.compare_amounts(stock_amount, direction * expected)
                    or currency.compare_amounts(clearing_amount, -direction * expected)
                    or any(line.account_id.id not in {account, clearing} for line in lines)):
                raise UserError(batch.env._("Posted valuation/clearing amounts do not reconcile for %s. "
                                          "Review currency rounding and stock accounts; no changes were saved.", source.display_name))
        # Verify every FIFO cost boundary, not just the total value.
        by_lot = defaultdict(list)
        for allocation in allocated:
            by_lot[allocation["lot"]].append(allocation)
        for lot_id, pool in by_lot.items() if row["target_cost_method"] == "fifo" else ():
            target_lot_id = lots[lot_id].id if lot_id else 0
            previous = next((item for item in before["fifo"] if item["lot"] == target_lot_id), None)
            quantity = previous["quantity"] if previous else 0
            expected_value = previous["value"] if previous else 0
            for allocation in pool:
                quantity += allocation["selected_quantity"]
                expected_value += allocation["selected_quantity"] * allocation["unit_value"]
                lot = lots[lot_id] if lot_id else None
                if currency.compare_amounts(target._run_fifo(quantity, lot=lot), expected_value):
                    raise UserError(batch.env._("The Target FIFO consumption order does not match the reviewed receipts."))
        batch.env["company.stock.fifo.product"].browse(row["line"])._system().write({
            "actual_quantity": qty - before["quantity"], "actual_value": value - before["value"],
            "quantity_difference": qty - before["quantity"] - row["selected_quantity"],
            "value_difference": value - before["value"] - expected,
        })
    Quant = company_model(batch, "stock.quant", batch.target_company_id)
    expected_quantities = defaultdict(float)
    in_dates = {}
    existing_keys = set()
    for quant in snapshot["target_quants"]:
        key = (quant["product"], quant["location"], quant["lot"])
        expected_quantities[key] += quant["quantity"]
        existing_keys.add(key)
    for quant in snapshot["quants"]:
        if not quant["selected"]:
            continue
        target_product = next(row["target"] for row in snapshot["products"] if row["source"] == quant["product"])
        key = (target_product, locations[quant["location"]].id, lots[quant["lot"]].id if quant["lot"] else 0)
        expected_quantities[key] += quant["quantity"]
        if quant["in_date"]:
            in_dates[key] = min(in_dates.get(key, quant["in_date"]), quant["in_date"])
    for (product_id, location_id, lot_id), quantity in expected_quantities.items():
        quants = Quant.search([
            ("company_id", "=", batch.target_company_id.id),
            ("product_id", "=", product_id), ("location_id", "=", location_id),
            ("lot_id", "=", lot_id or False), ("quantity", "!=", 0),
        ])
        product = target_products.browse(product_id)
        if product.uom_id.compare(sum(quants.mapped("quantity")), quantity):
            raise UserError(batch.env._("Target location/lot quantities do not reconcile."))
        key = (product_id, location_id, lot_id)
        if key in in_dates and key not in existing_keys:
            quants.write({"in_date": in_dates[key]})
    return rounding


def execute_cutover(batch, plan):
    snapshot = plan["snapshot"]
    chunks = opening_chunks(batch, snapshot, plan["allocations"])
    locations = create_locations(batch, snapshot)
    lots = create_lots(batch, snapshot)
    source = batch.source_company_id
    target = batch.target_company_id
    if snapshot["release_source_reservations"]:
        lines = company_model(batch, "stock.move.line", source).browse(
            [row["id"] for row in snapshot["source_operations"]["lines"]],
        ).exists()
        if len(lines) != len(snapshot["source_operations"]["lines"]):
            raise UserError(batch.env._("Reviewed reservations changed. Build a fresh Preview."))
        # Native unlink releases these exact move-line reservations and recomputes
        # availability without cancelling orders or altering procurement links.
        lines.unlink()
    closing_location = clearing_location(batch, source, batch.source_clearing_account_id)
    opening_location = clearing_location(batch, target, batch.target_clearing_account_id)
    product_map = {row["source"]: row["target"] for row in snapshot["products"]}
    closing = native_moves(batch, source, [{
        "product": quant["product"], "quantity": quant["quantity"],
        "source": quant["location"], "destination": closing_location.id,
        "lot": quant["lot"] or False, "value": None,
    } for quant in snapshot["quants"] if quant["selected"]])
    opening = native_moves(batch, target, [{
        "product": product_map[chunk["product"]], "quantity": chunk["quantity"],
        "source": opening_location.id, "destination": locations[chunk["location"]].id,
        "lot": lots[chunk["lot"]].id if chunk["lot"] else False, "value": chunk["value"],
    } for chunk in chunks])
    batch.env.invalidate_all()
    rounding = reconcile(batch, plan, closing, opening, locations, lots)
    # Native receipt values are monetary: rounded opening receipts can change
    # a fractional unit cost. Accept only currency-equivalent receipt totals,
    # after quantity, product value and journal reconciliation have all passed.
    for move, chunk in zip(opening, chunks, strict=True):
        if batch.currency_id.compare_amounts(move.value, chunk["value"]):
            raise UserError(batch.env._("Opening stock value exceeds normal currency rounding for %s. "
                                       "No changes were saved.", move.product_id.display_name))
        if not isclose(move.value / move._get_valued_qty(),
                       chunk["value"] / chunk["quantity"],
                       rel_tol=1e-12, abs_tol=1e-12):
            rounding.append(batch.env._(
                "%(product)s (opening move %(move)s): quantity %(quantity)s; "
                "reviewed unit cost %(before)s; opening unit cost %(after)s; "
                "receipt rounding %(difference)s %(currency)s.",
                product=move.product_id.display_name, move=move.id,
                quantity=format(chunk["quantity"], ".12g"),
                before=format(chunk["value"] / chunk["quantity"], ".12g"),
                after=format(move.value / move._get_valued_qty(), ".12g"),
                difference=format(move.value - chunk["value"], ".12g"), currency=batch.currency_id.name,
            ))
    batch._system().write({"rounding_note": "\n".join(rounding) or False})
    # No XML IDs: durable cutover markers survive removal/reinstallation of this addon.
    batch.env["ir.config_parameter"].create([{
        "key": marker_key(source.id, row["source"], batch.warehouse_cutover_id.source_warehouse_id.id or None),
        "value": json.dumps({"batch": batch.name, "target_company": target.id,
                             "target_product": row["target"], "cutover": snapshot["cutover"],
                             "quantity": row["selected_quantity"],
                             "moves": (closing | opening).ids}),
    } for row in snapshot["products"]])
    return closing | opening
