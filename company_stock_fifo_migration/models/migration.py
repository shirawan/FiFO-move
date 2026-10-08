import hashlib
import json

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .snapshot import build_snapshot, prepare_locations, reviewed_allocations
from .execution import execute_cutover, opening_chunks, lock_cutover_tables

_SYSTEM_CONTEXT = "_stock_fifo_cutover_write"
_SYSTEM_TOKEN = object()


def fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


class CompanyStockFifoMigration(models.Model):
    _name = "company.stock.fifo.migration"
    _description = "Company Stock FIFO Cutover"
    _order = "id desc"

    MAX_PRODUCTS = 1_000
    MAX_QUANTS = 10_000
    MAX_TRANCHES = 10_000
    MAX_MOVEMENTS = 250

    name = fields.Char(default="New", readonly=True, copy=False)
    source_company_id = fields.Many2one("res.company", required=True)
    target_company_id = fields.Many2one("res.company", required=True)
    currency_id = fields.Many2one(related="source_company_id.currency_id")
    mapping_batch_ref = fields.Reference(
        selection="_mapping_models", string="Product/Kit Mapping Batch"
    )
    warehouse_cutover_id = fields.Many2one("company.stock.warehouse.cutover", readonly=True, copy=False, ondelete="restrict")
    release_source_reservations = fields.Boolean(related="warehouse_cutover_id.release_source_reservations")
    cutover_at = fields.Datetime(default=fields.Datetime.now, required=True)
    source_clearing_account_id = fields.Many2one("account.account")
    target_clearing_account_id = fields.Many2one("account.account")
    state = fields.Selection([
        ("draft", "Draft"), ("mapped", "Mappings"), ("review", "Review Preview"),
        ("ready", "Ready"), ("done", "Completed"),
    ], default="draft", required=True, readonly=True, copy=False)
    product_line_ids = fields.One2many("company.stock.fifo.product", "batch_id")
    warehouse_line_ids = fields.One2many("company.stock.fifo.warehouse", "batch_id")
    location_line_ids = fields.One2many("company.stock.fifo.location", "batch_id")
    lot_line_ids = fields.One2many("company.stock.fifo.lot", "batch_id")
    tranche_line_ids = fields.One2many("company.stock.fifo.tranche", "batch_id")
    snapshot_data = fields.Json(readonly=True, copy=False)
    snapshot_hash = fields.Char(readonly=True, copy=False)
    partial_selection = fields.Boolean(readonly=True, copy=False)
    allocation_reviewed = fields.Boolean(
        string="I reviewed the company FIFO allocation for this partial transfer"
    )
    warehouse_review = fields.Text(readonly=True, copy=False)
    rounding_note = fields.Text(string="Native Currency Rounding", readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    completed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    created_move_ids = fields.Many2many("stock.move", readonly=True, copy=False)

    @api.model
    def _mapping_models(self):
        providers = [
            ("company.kit.bom.migration.batch", "Separate Kit BoM Mover"),
            ("company.product.bom.migration.batch", "Product and Kit BoM Mover"),
        ]
        return [item for item in providers if item[0] in self.env.registry.models]

    @api.constrains("source_company_id", "target_company_id")
    def _check_companies(self):
        for batch in self:
            if batch.source_company_id == batch.target_company_id:
                raise ValidationError(_("Select two different companies."))

    def _operator(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only a Settings administrator may run a stock cutover."))
        self.check_access("write")
        if not all(company in self.env.companies for company in (
            self.source_company_id, self.target_company_id
        )):
            raise AccessError(_("Enable both companies in the company switcher."))

    def _lock(self):
        if not self:
            return
        self.check_access("write")
        self.env.cr.execute(
            "SELECT id FROM company_stock_fifo_migration "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE", [self.ids]
        )
        self.invalidate_recordset(["state"])

    def _system(self):
        return self.with_context(**{_SYSTEM_CONTEXT: _SYSTEM_TOKEN})

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only a Settings administrator may create a stock cutover."))
        vals_list = [dict(vals) for vals in vals_list]
        for vals in vals_list:
            warehouse_cutover = vals.pop("warehouse_cutover_id", False)
            if warehouse_cutover:
                if self.env.context.get(_SYSTEM_CONTEXT) is not _SYSTEM_TOKEN:
                    raise AccessError(_("Migration audit fields are system-managed."))
                cutover = self.env["company.stock.warehouse.cutover"].browse(warehouse_cutover)
                cutover._operator()
                if (cutover.state != "review" or cutover.source_company_id.id != vals.get("source_company_id")
                        or cutover.target_company_id.id != vals.get("target_company_id")):
                    raise UserError(_("The warehouse cutover uses different companies or has no reviewed Preview."))
            elif not vals.get("mapping_batch_ref"):
                raise UserError(_("Select the existing Product / Kit Matches."))
            # Odoo saves these editable form defaults even on an empty draft.
            # Review rows and acknowledgements are generated/reviewed later.
            for field_name, default in (
                ("allocation_reviewed", False),
                ("product_line_ids", []), ("warehouse_line_ids", []),
                ("location_line_ids", []), ("lot_line_ids", []),
                ("tranche_line_ids", []),
            ):
                if field_name in vals:
                    if vals.pop(field_name) != default:
                        raise AccessError(_("Migration audit fields are system-managed."))
            if set(vals) - {
                "source_company_id", "target_company_id", "mapping_batch_ref",
                "cutover_at", "source_clearing_account_id", "target_clearing_account_id",
            }:
                raise AccessError(_("Migration audit fields are system-managed."))
            vals["name"] = self.env["ir.sequence"].next_by_code("company.stock.fifo.migration")
            if not vals["name"]:
                raise UserError(_("The stock cutover sequence is missing."))
            if warehouse_cutover:
                vals["warehouse_cutover_id"] = warehouse_cutover
        return super().create(vals_list)

    def write(self, vals):
        self._lock()
        if self.filtered(lambda batch: batch.state == "done"):
            raise UserError(_("A completed stock cutover is immutable."))
        if self.env.context.get(_SYSTEM_CONTEXT) is _SYSTEM_TOKEN:
            return super().write(vals)
        editable = {
            "source_company_id", "target_company_id", "mapping_batch_ref",
            "cutover_at", "source_clearing_account_id", "target_clearing_account_id",
            "allocation_reviewed",
        }
        review_fields = {
            "product_line_ids", "warehouse_line_ids", "location_line_ids",
            "lot_line_ids", "tranche_line_ids",
        }
        if set(vals) - editable - review_fields:
            raise AccessError(_("Migration audit fields are system-managed."))
        if {"source_company_id", "target_company_id", "mapping_batch_ref"}.intersection(vals):
            if self.filtered(lambda batch: batch.state != "draft"):
                raise UserError(_("Create a new batch to change the companies or mapping source."))
        updates = []
        if review_fields.intersection(vals):
            self.ensure_one()
            for field_name in review_fields.intersection(vals):
                commands = vals[field_name]
                if not isinstance(commands, (list, tuple)):
                    raise AccessError(_("Review rows only accept updates to existing rows."))
                for command in commands:
                    if (not isinstance(command, (list, tuple)) or len(command) != 3
                            or command[0] != Command.UPDATE
                            or not isinstance(command[1], int) or isinstance(command[1], bool)
                            or command[1] <= 0 or not isinstance(command[2], dict)):
                        raise AccessError(_("Review rows cannot be created, removed, or reassigned."))
                    line = self.env[self._fields[field_name].comodel_name].browse(command[1]).exists()
                    if not line or line.batch_id != self:
                        raise AccessError(_("The review row does not belong to this cutover batch."))
                    line.check_access("write")
                    updates.append((line, command[2]))
        # Child guards own field authorization and preview invalidation. Apply the
        # acknowledgement afterwards so a single form save can review new quantities.
        for line, values in updates:
            line.write(values)
        parent_vals = {name: value for name, value in vals.items() if name not in review_fields}
        result = super().write(parent_vals)
        if set(parent_vals) - {"allocation_reviewed"}:
            self._reset_preview()
        return result

    @api.ondelete(at_uninstall=False)
    def _prevent_completed_deletion(self):
        if self.filtered(lambda batch: batch.state == "done"):
            raise UserError(_("A completed stock cutover cannot be deleted."))

    def _reset_preview(self, allocation_only=False):
        for batch in self.filtered(lambda record: record.state in {"review", "ready"}):
            batch._system().write({
                "state": "review" if allocation_only else "mapped",
                "snapshot_hash": False,
                "allocation_reviewed": False,
            })

    def _validate_clearing_account(self, company, account):
        if (not account or account.company_ids != company or not account.active
                or account.account_type not in {"asset_current", "equity"}):
            raise UserError(_("Choose an active company-specific Current Asset or Equity clearing account."))
        if account == company.account_stock_valuation_id:
            raise UserError(_("The clearing account must differ from the stock valuation account."))

    def _company_clearing_account(self, company):
        Account = self.env["account.account"].with_context(
            allowed_company_ids=company.ids,
        ).with_company(company)
        Parameter = self.env["ir.config_parameter"]
        key = "company_stock_fifo_migration.clearing_account.%s" % company.id
        parameter = Parameter.search([("key", "=", key)])
        if parameter:
            if not parameter.value or not parameter.value.isdecimal():
                raise UserError(_("The saved clearing-account reference is invalid. Ask a Settings administrator to review it."))
            account = Account.browse(int(parameter.value)).exists()
            self._validate_clearing_account(company, account)
            return account
        account = Account.create({
            "name": _("Stock Migration Clearing"),
            "code": Account._search_new_account_code("STKCLR0001", cache=set()),
            "account_type": "asset_current",
            "company_ids": [Command.set(company.ids)],
        })
        # Native accounts and this reference have no addon XML IDs: both survive
        # uninstall. Do not identify an accounting record by its translated name.
        Parameter.create({"key": key, "value": str(account.id)})
        return account

    def action_create_clearing_accounts(self):
        self._operator()
        self._lock()
        if self.state == "done":
            raise UserError(_("A completed stock cutover is immutable."))
        pairs = (
            ("source_clearing_account_id", self.source_company_id),
            ("target_clearing_account_id", self.target_company_id),
        )
        for field_name, company in pairs:
            if self[field_name]:
                self._validate_clearing_account(company, self[field_name])
        companies = self.source_company_id | self.target_company_id
        companies.check_access("write")
        with self.env.cr.savepoint():
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [companies.ids],
            )
            # A real row update makes concurrent repeatable-read requests retry
            # with fresh data, rather than create a second account per company.
            self.env.cr.execute(
                "UPDATE res_company SET write_date = write_date WHERE id = ANY(%s)",
                [companies.ids],
            )
            values = {
                field_name: self._company_clearing_account(company).id
                for field_name, company in pairs if not self[field_name]
            }
            if values:
                self.write(values)
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_import_mappings(self):
        self._operator()
        self._lock()
        if self.state != "draft":
            raise UserError(_("Mappings have already been imported. Create a new batch."))
        provider = self.mapping_batch_ref
        provider.check_access("read")
        if (provider.source_company_id != self.source_company_id
                or provider.target_company_id != self.target_company_id):
            raise UserError(_("The selected mapping batch uses different companies."))
        if provider._name == "company.kit.bom.migration.batch":
            pairs = [(line.source_product_id, line.target_product_id)
                     for line in provider.mapping_line_ids if line.target_product_id]
        else:
            pairs = []
            for line in provider.product_line_ids.filtered(
                lambda row: row.selected and row.validation_status == "applied"
            ):
                source = line.source_product_tmpl_id.with_context(active_test=False).product_variant_ids
                target = line.mapped_product_tmpl_id.with_context(active_test=False).product_variant_ids
                if len(source) != 1 or len(target) != 1:
                    raise UserError(_("Product mover mappings must resolve to one variant: %s",
                                      line.source_product_tmpl_id.display_name))
                pairs.append((source, target))
        pairs = [(source, target) for source, target in pairs if source.is_storable]
        if not pairs or len(pairs) > self.MAX_PRODUCTS:
            raise UserError(_("Import between 1 and %s mapped inventory products.", self.MAX_PRODUCTS))
        self.env["company.stock.fifo.product"].with_context(
            **{_SYSTEM_CONTEXT: _SYSTEM_TOKEN}
        ).create([{
            "batch_id": self.id, "source_product_id": source.id,
            "target_product_id": target.id,
        } for source, target in pairs])
        warehouses = self.env["stock.warehouse"].search([
            ("company_id", "=", self.source_company_id.id),
        ])
        self.env["company.stock.fifo.warehouse"].with_context(
            **{_SYSTEM_CONTEXT: _SYSTEM_TOKEN}
        ).create([{
            "batch_id": self.id, "source_warehouse_id": warehouse.id,
            "target_name": warehouse.name, "target_code": warehouse.code,
        } for warehouse in warehouses])
        self._system().write({"state": "mapped"})
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_prepare_locations(self):
        self._operator()
        self._lock()
        if self.state in {"draft", "done"}:
            raise UserError(_("Import mappings and select warehouses first."))
        prepare_locations(self)
        self._reset_preview()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_preview(self):
        self._operator()
        self._lock()
        if self.state not in {"mapped", "review", "ready"}:
            raise UserError(_("Import mappings and prepare warehouse locations first."))
        snapshot = build_snapshot(self)
        self._store_preview(snapshot)
        return {"type": "ir.actions.client", "tag": "reload"}

    def _store_preview(self, snapshot):
        """Persist a server-built preview, without rebuilding its source plan."""
        self.tranche_line_ids._system().unlink()
        Tranche = self.env["company.stock.fifo.tranche"].with_context(
            **{_SYSTEM_CONTEXT: _SYSTEM_TOKEN}
        )
        Tranche.create([{
            "batch_id": self.id,
            "source_product_id": row["product"],
            "source_move_id": row["move"],
            "source_lot_id": row["lot"] or False,
            "sequence": number,
            "available_quantity": row["quantity"],
            "unit_value": row["unit_value"],
            "selected_quantity": row["quantity"] if not row["partial"] else 0,
        } for number, row in enumerate(snapshot["tranches"])])
        by_product = {row["source"]: row for row in snapshot["products"]}
        for line in self.product_line_ids.filtered("selected"):
            row = by_product[line.source_product_id.id]
            line._system().write({
                "company_quantity": row["company_quantity"],
                "source_quantity": row["selected_quantity"],
                "company_value": row["company_value"],
            })
        self._system().write({
            "state": "review", "snapshot_data": snapshot,
            "snapshot_hash": False, "allocation_reviewed": False,
            "partial_selection": any(row["partial"] for row in snapshot["tranches"]),
            "warehouse_review": snapshot["warehouse_review"],
        })

    def action_check(self):
        self._operator()
        self._lock()
        if self.state not in {"review", "ready"}:
            raise UserError(_("Build and review a stock preview first."))
        plan = self._validated_plan()
        self._store_checked_plan(plan)
        return {"type": "ir.actions.client", "tag": "reload"}

    def _store_checked_plan(self, plan):
        for line in self.product_line_ids.filtered("selected"):
            value = sum(row["selected_quantity"] * row["unit_value"]
                        for row in plan["allocations"] if row["product"] == line.source_product_id.id)
            line._system().write({"opening_value": value})
        self._system().write({"state": "ready", "snapshot_hash": fingerprint(plan)})

    def _validated_plan(self):
        current = build_snapshot(self)
        if not self.snapshot_data or fingerprint(current) != fingerprint(self.snapshot_data):
            raise UserError(_("Stock or configuration changed. Build a fresh preview and review it again."))
        allocations = reviewed_allocations(self, current)
        opening_chunks(self, current, allocations)
        return {"snapshot": current, "allocations": allocations}

    def action_apply(self):
        self._operator()
        self._lock()
        if self.state != "ready":
            raise UserError(_("Check the reviewed preview before moving stock."))
        with self.env.cr.savepoint():
            # One-off maintenance cutover: ordinary stock/config writes must wait.
            lock_cutover_tables(self)
            self.env.invalidate_all()
            plan = self._validated_plan()
            if fingerprint(plan) != self.snapshot_hash:
                raise UserError(_("The checked allocation changed. Review and Check again."))
            # Odoo uses repeatable-read. Check committed data after acquiring locks,
            # rather than trusting an older snapshot that waited for another writer.
            with self.env.registry.cursor() as cursor:
                fresh = api.Environment(cursor, self.env.uid, dict(self.env.context))[self._name].browse(self.id)
                if fingerprint(fresh._validated_plan()) != self.snapshot_hash:
                    raise UserError(_("Stock changed while the cutover was waiting. Build a fresh preview."))
            moves = execute_cutover(self, plan)
            self._system().write({
                "state": "done", "created_move_ids": [Command.set(moves.ids)],
                "completed_at": fields.Datetime.now(), "completed_by_id": self.env.user.id,
            })
            from ..hooks import save_stock_archive
            save_stock_archive(self)
        return {"type": "ir.actions.client", "tag": "reload"}


class StockFifoReview(models.AbstractModel):
    _name = "company.stock.fifo.review"
    _description = "Stock FIFO Cutover Review"

    batch_id = fields.Many2one("company.stock.fifo.migration", required=True, ondelete="cascade", index=True)
    batch_state = fields.Selection(related="batch_id.state")
    target_company_id = fields.Many2one(related="batch_id.target_company_id")
    _editable_fields = set()

    def _system(self):
        return self.with_context(**{_SYSTEM_CONTEXT: _SYSTEM_TOKEN})

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_SYSTEM_CONTEXT) is not _SYSTEM_TOKEN:
            raise AccessError(_("Review rows are generated by the cutover workflow."))
        return super().create(vals_list)

    def write(self, vals):
        batches = self.batch_id
        batches._lock()
        if batches.filtered(lambda batch: batch.state == "done"):
            raise UserError(_("A completed stock cutover is immutable."))
        if self.env.context.get(_SYSTEM_CONTEXT) is _SYSTEM_TOKEN:
            return super().write(vals)
        if set(vals) - self._editable_fields:
            raise AccessError(_("The source mappings and audit results are read-only."))
        result = super().write(vals)
        batches._reset_preview(allocation_only=self._name == "company.stock.fifo.tranche")
        return result

    @api.ondelete(at_uninstall=False)
    def _prevent_user_deletion(self):
        if self.env.context.get(_SYSTEM_CONTEXT) is not _SYSTEM_TOKEN:
            raise AccessError(_("Review rows can only be refreshed by the cutover workflow."))


class StockFifoProduct(models.Model):
    _name = "company.stock.fifo.product"
    _inherit = "company.stock.fifo.review"
    _description = "Stock Cutover Product Reconciliation"
    _editable_fields = {"selected"}

    selected = fields.Boolean(default=True)
    source_product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    target_product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    currency_id = fields.Many2one(related="batch_id.currency_id")
    company_quantity = fields.Float(readonly=True)
    source_quantity = fields.Float(readonly=True)
    company_value = fields.Monetary(readonly=True)
    opening_value = fields.Monetary(readonly=True)
    actual_quantity = fields.Float(readonly=True)
    actual_value = fields.Monetary(readonly=True)
    quantity_difference = fields.Float(readonly=True)
    value_difference = fields.Monetary(readonly=True)

    _source_unique = models.Constraint("UNIQUE(batch_id, source_product_id)", "Each source product can be mapped once.")
    _target_unique = models.Constraint("UNIQUE(batch_id, target_product_id)", "Each target product can be mapped once.")


class StockFifoWarehouse(models.Model):
    _name = "company.stock.fifo.warehouse"
    _inherit = "company.stock.fifo.review"
    _description = "Stock Cutover Warehouse Selection"
    _editable_fields = {"selected", "action", "target_warehouse_id", "target_name", "target_code"}

    selected = fields.Boolean(default=False)
    source_warehouse_id = fields.Many2one("stock.warehouse", required=True, ondelete="restrict")
    action = fields.Selection([("match", "Use Existing"), ("create", "Recreate")], default="match", required=True)
    target_warehouse_id = fields.Many2one("stock.warehouse", ondelete="restrict")
    target_name = fields.Char()
    target_code = fields.Char()
    created_warehouse_id = fields.Many2one("stock.warehouse", readonly=True, ondelete="restrict")


class StockFifoLocation(models.Model):
    _name = "company.stock.fifo.location"
    _inherit = "company.stock.fifo.review"
    _description = "Stock Cutover Location Mapping"
    _editable_fields = {"target_location_id"}

    source_location_id = fields.Many2one("stock.location", required=True, ondelete="restrict")
    warehouse_line_id = fields.Many2one("company.stock.fifo.warehouse", required=True, ondelete="cascade")
    target_location_id = fields.Many2one("stock.location", ondelete="restrict")
    native_role = fields.Char(readonly=True)
    proposed_path = fields.Char(readonly=True)
    source_quantity = fields.Float(readonly=True)
    created_location_id = fields.Many2one("stock.location", readonly=True, ondelete="restrict")


class StockFifoLot(models.Model):
    _name = "company.stock.fifo.lot"
    _inherit = "company.stock.fifo.review"
    _description = "Stock Cutover Lot Mapping"
    _editable_fields = {"action", "target_lot_id"}

    source_lot_id = fields.Many2one("stock.lot", required=True, ondelete="restrict")
    target_product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    action = fields.Selection([("create", "Create Same Lot"), ("match", "Use Existing")], default="create", required=True)
    target_lot_id = fields.Many2one("stock.lot", ondelete="restrict")
    source_quantity = fields.Float(readonly=True)
    created_lot_id = fields.Many2one("stock.lot", readonly=True, ondelete="restrict")


class StockFifoTranche(models.Model):
    _name = "company.stock.fifo.tranche"
    _inherit = "company.stock.fifo.review"
    _description = "Stock Cutover FIFO Allocation"
    _order = "sequence, id"
    _editable_fields = {"selected_quantity"}

    source_product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    source_move_id = fields.Many2one("stock.move", ondelete="restrict")
    source_date = fields.Datetime(related="source_move_id.date")
    source_lot_id = fields.Many2one("stock.lot", ondelete="restrict")
    sequence = fields.Integer(readonly=True)
    available_quantity = fields.Float(readonly=True)
    selected_quantity = fields.Float()
    unit_value = fields.Float(readonly=True)
    allocated_value = fields.Monetary(compute="_compute_allocated_value")
    currency_id = fields.Many2one(related="batch_id.currency_id")

    @api.depends("selected_quantity", "unit_value")
    def _compute_allocated_value(self):
        for line in self:
            line.allocated_value = line.selected_quantity * line.unit_value
