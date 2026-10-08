"""Stock preview rendering, independent of database writes."""
from html import escape
from decimal import Decimal, ROUND_HALF_UP


def format_quantity(value, decimals=None):
    """Fixed notation at reviewed UoM precision; never truncate significant digits.

    Older previews without precision retain their full recorded decimal value.
    New previews store Odoo 19's Product Unit precision with each row.
    """
    quantity = Decimal(str(value))
    if decimals is not None:
        quantum = Decimal(1).scaleb(-int(decimals))
        quantity = quantity.quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(quantity, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if quantity.is_zero() else text


def product_preview_table(snapshot):
    rows = snapshot.get("products", [])
    baseline = {row["target"]: row["quantity"]
                for row in snapshot.get("target_baseline", {}).get("products", [])}
    content = []
    for row in rows:
        decimals = row.get("unit_decimals")
        target = row.get("target")
        before = baseline.get(target) if target else 0
        after = Decimal(str(before)) + Decimal(str(row["quantity"])) if before is not None else None
        action = "Reuse %s" % row.get("target_name", "") if target else "Create product"
        origin = ("Archived original; active new copy" if row.get("archived") else
                  "Shared product" if row["shared"] else "Source-company product")
        cells = [row["name"], action,
                 format_quantity(before, decimals) if before is not None else "Not recorded — build a fresh preview",
                 format_quantity(row["quantity"], decimals),
                 format_quantity(after, decimals) if after is not None else "Not recorded",
                 format_quantity(Decimal(str(row["company_quantity"])) - Decimal(str(row["quantity"])), decimals), row["unit"],
                 "%.2f" % row["value"], origin]
        content.append("<tr>" + "".join("<td>%s</td>" % escape(str(cell)) for cell in cells) + "</tr>")
    return (
        "<p>Existing stock is kept and the moved quantity is added. "
        "Already in new company and New company total cover all its warehouses, in the product's unit.</p>"
        '<div class="table-responsive"><table class="table table-sm"><thead><tr><th scope="col">Product</th><th scope="col">What will happen</th>'
        '<th>Already in new company</th><th>Moving now</th><th>New company total</th>'
        '<th>Left elsewhere in old company</th><th>Unit</th><th>Opening value</th><th>Source</th>'
        '</tr></thead><tbody>' + "".join(content) + "</tbody></table></div>"
    )
