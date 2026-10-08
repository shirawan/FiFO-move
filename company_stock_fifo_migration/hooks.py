"""Keep completed stock audit data in native Odoo storage before uninstall."""
import hashlib
import json
from html import escape
from uuid import uuid4


def stored_values(record):
    names = [name for name, field in record._fields.items()
        if field.store and field.type != "binary" and not name.startswith("_")]
    return record.read(names)[0]


def save_stock_archive(batch):
    batch.ensure_one()
    if batch.state != "done":
        return
    payload = {"version": 1, "batch": stored_values(batch), "rows": {}}
    for name in ("product_line_ids", "warehouse_line_ids", "location_line_ids", "lot_line_ids", "tranche_line_ids"):
        payload["rows"][name] = [stored_values(row) for row in batch[name]]
    if batch.warehouse_cutover_id:
        wizard = batch.warehouse_cutover_id
        payload["warehouse_move"] = stored_values(wizard)
        payload["product_choices"] = [stored_values(row) for row in wizard.product_choice_ids]
        payload["location_choices"] = [stored_values(row) for row in wizard.location_choice_ids]
        report = wizard.preview_html or ""
    else:
        report = ""
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    report = ("<!doctype html><html><meta charset='utf-8'><body><h1>Completed stock move: %s</h1>%s"
        "<h2>Saved reconciliation and audit data</h2><pre>%s</pre></body></html>"
        % (escape(batch.name), report, escape(json.dumps(payload, indent=2, default=str)))).encode()
    # Native movement IDs stay stable even when this addon's sequence/table is
    # recreated. Existing per-product completion markers remain independent.
    identity = str(min(batch.created_move_ids.ids)) if batch.created_move_ids else str(uuid4())
    key = "company_stock_fifo_migration.archive.moves." + identity
    Parameter = batch.env["ir.config_parameter"].sudo()
    existing = Parameter.search([("key", "=", key)])
    manifest = json.loads(existing.value) if existing else {}
    Attachment = batch.env["ir.attachment"].sudo()
    for kind, content, mimetype in (("recovery", raw, "application/json"), ("report", report, "text/html")):
        values = {"name": "%s-%s.%s" % (batch.name.replace("/", "-"), kind, "json" if kind == "recovery" else "html"),
            "raw": content, "public": False, "res_model": False, "res_id": False,
            "company_id": batch.target_company_id.id, "mimetype": mimetype,
            "description": "FiFO-move protected archive: stock-" + identity}
        attachment = Attachment.browse(manifest.get(kind + "_id", False)).exists()
        create = getattr(Attachment, "_create_move_archive", None)
        if attachment:
            write = getattr(attachment, "_write_move_archive", None)
            write(values) if write else attachment.write(values)
        else:
            attachment = create(values) if create else Attachment.create(values)
        manifest[kind + "_id"] = attachment.id
        manifest[kind + "_sha256"] = hashlib.sha256(content).hexdigest()
    manifest.update({"version": 1, "source_company_id": batch.source_company_id.id,
        "target_company_id": batch.target_company_id.id, "native_move_ids": batch.created_move_ids.ids})
    Parameter.set_param(key, json.dumps(manifest, sort_keys=True))


def uninstall_hook(env):
    companies = env["res.company"].sudo().search([])
    batches = env["company.stock.fifo.migration"].sudo().with_context(allowed_company_ids=companies.ids).search([("state", "=", "done")])
    for batch in batches:
        save_stock_archive(batch)
