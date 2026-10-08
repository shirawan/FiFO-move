"""Read-only destination matching for the whole-warehouse mover."""
from odoo import fields
from odoo.exceptions import UserError

from .snapshot import company_model, LOCATION_ROLES, cost_methods_compatible, valuation_modes_compatible


def product_matches(cutover, products):
    """Use a unique company-specific identity; never guess between candidates."""
    _ = cutover.env._
    Product = company_model(cutover, "product.product", cutover.target_company_id).with_context(
        active_test=False, lang="en_US")
    sources = products.product_tmpl_id.with_context(active_test=False, lang="en_US").product_variant_ids.sorted("id")
    if len(sources) > cutover.env["company.stock.fifo.migration"].MAX_PRODUCTS:
        raise UserError(_("These product families exceed the cutover product limit."))
    history = cutover.env["company.stock.fifo.product"].search([
        ("batch_id.state", "=", "done"),
        ("batch_id.target_company_id", "=", cutover.target_company_id.id),
        ("source_product_id", "in", sources.ids),
    ])
    previous = {product.id: rows.target_product_id for product, rows in history.grouped("source_product_id").items()}
    matches, issues, claimed = [], [], {}
    selections = {line.source_product_id.id: Product.browse(line.target_product_id.id)
                  for line in cutover.product_choice_ids}
    if set(selections) - set(sources.ids):
        raise UserError(_("Remove product choices that are not part of this warehouse's stocked product families."))
    families, chosen_families, ambiguous_families = {}, set(), set()
    Template = Product.env["product.template"]
    def literal_name(name):
        return name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    def variant_choices(template):
        return {(line.attribute_id.id, tuple(sorted(line.value_ids.ids))) for line in template.attribute_line_ids}
    for source in sources.product_tmpl_id:
        candidates = Template.search([("company_id", "=", cutover.target_company_id.id),
                                      ("name", "=ilike", literal_name(source.name))])
        family_products = sources.filtered(lambda product: product.product_tmpl_id == source)
        references = family_products.mapped("default_code")
        barcodes = family_products.mapped("barcode")
        identified = Product.search([("company_id", "=", cutover.target_company_id.id), "|",
                                     ("default_code", "in", [code for code in references if code]),
                                     ("barcode", "in", [code for code in barcodes if code])])
        candidates |= identified.product_tmpl_id
        prior = history.filtered(lambda row: row.source_product_id.product_tmpl_id == source).target_product_id.product_tmpl_id
        candidates |= Template.browse(prior.ids)
        chosen = Product.browse([selections[p.id].id for p in family_products if p.id in selections]).product_tmpl_id
        if chosen:
            candidates = chosen
            chosen_families.add(source.id)
        if len(candidates) > 1:
            ambiguous_families.add(source.id)
        elif candidates:
            if variant_choices(candidates) != variant_choices(source):
                issues.append(_("%s: the existing destination family has different variant choices. "
                                "Review that family; no duplicate siblings will be created.", source.name))
            else:
                families[source.id] = candidates
    for source in sources:
        identities = [("name", "=ilike", literal_name(source.name))]
        if source.default_code:
            identities.append(("default_code", "=", source.default_code))
        if source.barcode:
            identities.append(("barcode", "=", source.barcode))
        candidates = Product.search([("company_id", "=", cutover.target_company_id.id)] +
                                    ["|"] * (len(identities) - 1) + identities)
        attributes = set(source.product_template_attribute_value_ids.product_attribute_value_id.ids)
        candidates = candidates.filtered(lambda product: set(
            product.product_template_attribute_value_ids.product_attribute_value_id.ids) == attributes)
        family = families.get(source.product_tmpl_id.id, Template.browse())
        candidates |= family.product_variant_ids.filtered(lambda product: set(
            product.product_template_attribute_value_ids.product_attribute_value_id.ids) == attributes)
        prior = previous.get(source.id, Product.browse())
        candidates |= Product.browse(prior.ids)
        if source.id in selections:
            candidates = selections[source.id]
        elif family and source.product_tmpl_id.id in chosen_families:
            candidates = candidates.filtered(lambda product: product.product_tmpl_id == family)
        if len(candidates) > 1:
            issues.append(_("%(name)s: multiple destination products match (IDs %(ids)s). "
                            "Add a choice under Destination Product Choices, then Preview again. Neither product needs deleting.",
                            name=source.display_name, ids=", ".join(map(str, candidates.ids))))
            continue
        target = candidates
        if source.product_tmpl_id.id in ambiguous_families:
            issues.append(_("%s: multiple destination families match. Use Destination Product Choices "
                            "to choose variants from one family; no new family will be created.", source.display_name))
            continue
        if target:
            if family and target.product_tmpl_id != family:
                issues.append(_("%s: its reference and product family identify different destination products.", source.display_name))
                continue
            if variant_choices(target.product_tmpl_id) != variant_choices(source.product_tmpl_id):
                issues.append(_("%s: destination variant choices differ; no duplicate family will be created.", source.display_name))
                continue
            families[source.product_tmpl_id.id] = target.product_tmpl_id
            family = target.product_tmpl_id
            differences = []
            if set(target.product_template_attribute_value_ids.product_attribute_value_id.ids) != attributes:
                differences.append(_("variant values differ"))
            if target.company_id != cutover.target_company_id:
                differences.append(_("it belongs to another company"))
            if not target.active:
                differences.append(_("it is archived"))
            if not target.is_storable:
                differences.append(_("Track Inventory is off"))
            for field, label in (("uom_id", "Unit"), ("tracking", "Tracking"),
                                 ("lot_valuated", "Lot valuation"), ("cost_method", "Cost method"),
                                 ("valuation", "Inventory valuation")):
                if target[field] != source[field]:
                    if field == "cost_method" and cost_methods_compatible(source[field], target[field], True):
                        continue
                    if field == "valuation" and valuation_modes_compatible(source[field], target[field], True):
                        continue
                    old = source[field].display_name if field == "uom_id" else source[field]
                    new = target[field].display_name if field == "uom_id" else target[field]
                    differences.append(_("%(setting)s: source %(old)s; destination %(new)s",
                                         setting=label, old=old, new=new))
            if differences:
                issues.append(_("%(name)s: review destination product %(id)s: %(details)s. No duplicate will be created.",
                                name=source.display_name, id=target.id, details="; ".join(differences)))
                continue
            if target.cost_method == "standard" and abs(target.standard_price - source.standard_price) > 1e-9:
                issues.append(_("%s: the existing destination product uses a different Standard Price. "
                                "Review the costs before adding stock; its existing stock will not be repriced.", source.display_name))
                continue
            account = target._get_product_accounts()["stock_valuation"]
            if target.valuation == "real_time" and (
                    not account or not account.active or account.account_type != "asset_current"
                    or not account.filtered_domain(account._check_company_domain(cutover.target_company_id))):
                issues.append(_("%s: configure the existing destination product's inventory valuation account.", source.display_name))
                continue
        identities = [("target", target.id)] if target else [
            ("name", source.name.strip().casefold(), tuple(sorted(attributes)))]
        if not target and source.default_code:
            identities.append(("reference", source.default_code, tuple(sorted(attributes))))
        if not target and source.barcode:
            identities.append(("barcode", source.barcode))
        duplicate = next((identity for identity in identities if identity in claimed), None)
        if duplicate:
            issues.append(_("%(first)s and %(second)s identify the same destination product. "
                            "Review the duplicate source records before moving them together.",
                            first=claimed[duplicate], second=source.display_name))
            continue
        for identity in identities:
            claimed[identity] = source.display_name
        matches.append({"source": source.id, "target": target.id or 0,
                        "target_name": target.display_name if target else "",
                        "target_valuation": target.valuation if target else source.valuation,
                        "template_target": family.id or 0,
                        "target_write": fields.Datetime.to_string(target.write_date),
                        "template_write": fields.Datetime.to_string(target.product_tmpl_id.write_date),
                        "target_quantity": target._with_valuation_context().qty_available if target else 0,
                        "target_value": target.total_value if target else 0})
    if issues:
        raise UserError(_("Review destination product matches; nothing was created:") + "\n\n" + "\n\n".join(issues))
    return matches


def location_matches(cutover, sources):
    """Map native roots, then reuse exact child paths or plan missing locations."""
    _ = cutover.env._
    warehouse = cutover.target_warehouse_id
    Location = company_model(cutover, "stock.location", cutover.target_company_id).with_context(active_test=False)
    existing = Location.search([("id", "child_of", warehouse.view_location_id.id)], order="parent_path, id")
    roles = {cutover.source_warehouse_id[role].id: role for role in LOCATION_ROLES
             if cutover.source_warehouse_id[role]}
    cutover.location_choice_ids._check_locations()
    choices = {line.source_location_id.id: Location.browse(line.target_location_id.id)
               for line in cutover.location_choice_ids}
    if set(choices) - set(sources.ids):
        raise UserError(_("Remove location choices outside the selected source warehouse."))
    mapped, rows = {}, []
    for source in sources:
        role = roles.get(source.id)
        target = choices.get(source.id, warehouse[role] if role else Location.browse())
        if not target:
            parent = mapped.get(source.location_id.id)
            target = existing.filtered(lambda location: parent and location.location_id == parent
                                       and location.name == source.name)
        if len(target) > 1:
            raise UserError(_("Multiple destination locations match %s. Review that path before Preview.", source.complete_name))
        if target and (target.company_id != cutover.target_company_id or target.usage != source.usage
                       or target.active != source.active or target.valuation_account_id):
            raise UserError(_("Destination location %s has incompatible company, usage, active status or valuation.", target.complete_name))
        mapped[source.id] = target
        rows.append({"source": source.id, "target": target.id or 0,
                     "source_name": source.complete_name,
                     "target_name": target.complete_name if target else "",
                     "target_write": fields.Datetime.to_string(target.write_date)})
    return rows
