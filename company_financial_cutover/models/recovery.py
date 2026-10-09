from odoo import api, fields, models
from odoo.exceptions import AccessError

_TOKEN = object()

class RecoveryIssue(models.Model):
    _name = "company.financial.recovery.issue"
    _description = "Company move recovery issue"
    _rec_name = "archive_key"

    archive_key = fields.Char(readonly=True, required=True)
    reason = fields.Text(readonly=True)
    state = fields.Selection([("pending", "Needs recovery"), ("resolved", "Recovered")], default="pending", readonly=True)
    _unique_key = models.Constraint("UNIQUE(archive_key)", "This archive already has a recovery issue.")

    @api.model_create_multi
    def create(self, values):
        if self.env.context.get("_recovery_token") is not _TOKEN:
            raise AccessError("Recovery issues are system-managed.")
        return super().create(values)

    def write(self, values):
        if self.env.context.get("_recovery_token") is not _TOKEN:
            raise AccessError("Recovery issues are system-managed.")
        return super().write(values)

    def unlink(self):
        raise AccessError("Keep the recovery history. Use Retry recovery after repairing the original records or restoring backup files.")

    def _record_issue(self, key, reason):
        Model = self.sudo().with_context(_recovery_token=_TOKEN)
        issue = Model.search([("archive_key", "=", key)])
        values = {"archive_key": key, "reason": reason, "state": "pending"}
        issue.write(values) if issue else Model.create(values)

    def _resolve_issue(self, key):
        self.sudo().with_context(_recovery_token=_TOKEN).search([("archive_key", "=", key)]).write({"state": "resolved"})

    def action_retry(self):
        if not self.env.is_system():
            raise AccessError("Only the administrator can restore migration archives.")
        self.env["company.financial.cutover"]._restore_archives(strict=False, archive_keys=self.mapped("archive_key"))
        return True
