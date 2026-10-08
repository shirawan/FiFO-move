"""Stock preview rendering, independent of database writes."""
from html import escape


def product_preview_table(snapshot):
    rows = snapshot.get("products", [])
    baseline = {row["target"]: row["quantity"]
                for row in snapshot.get("target_baseline", {}).get("products", [])}
    content = []
    for row in rows:
        target = row.get("target")
        before = baseline.get(target) if target else 0
        after = before + row["quantity"] if before is not None else None
        action = "Reuse %s" % row.get("target_name", "") if target else "Create product"
        origin = ("Archived original; active new copy" if row.get("archived") else
                  "Shared product" if row["shared"] else "Source-company product")
        cells = [row["name"], action,
                 "%g" % before if before is not None else "Not recorded — build a fresh preview",
                 "%g" % row["quantity"], "%g" % after if after is not None else "Not recorded",
                 "%g" % (row["company_quantity"] - row["quantity"]), row["unit"],
                 "%.2f" % row["value"], origin]
        content.append("<tr>" + "".join("<td>%s</td>" % escape(str(cell)) for cell in cells) + "</tr>")
    return (
        "<p>Existing stock is kept and the moved quantity is added. "
        "Already in new company and New company total cover all its warehouses, in the product's unit.</p>"
        '<table class="table table-sm"><thead><tr><th>Product</th><th>Destination action</th>'
        '<th>Already in new company</th><th>Moving now</th><th>New company total</th>'
        '<th>Left elsewhere in old company</th><th>Unit</th><th>Opening value</th><th>Source</th>'
        '</tr></thead><tbody>' + "".join(content) + "</tbody></table>"
    )
