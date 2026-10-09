import json
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from .test_cutover import FinancialCutoverCase


@tagged('post_install', '-at_install')
class TestArchiveRecovery(FinancialCutoverCase):
    def _remove_audit(self, batch):
        opening = batch.move_id
        self.env.flush_all()
        self.env.cr.execute('UPDATE account_move SET financial_cutover_id = NULL WHERE id = %s', [opening.id])
        self.env.cr.execute('DELETE FROM company_financial_cutover WHERE id = %s', [batch.id])
        self.env.invalidate_all()
        return opening

    def test_bad_archive_isolated_healthy_archive_restores_without_reposting(self):
        Parameter = self.env['ir.config_parameter'].sudo()
        Parameter.set_param('company_financial_cutover.archive.broken-first', 'invalid JSON')
        self._invoice()
        batch = self._run()
        key = batch.archive_key
        manifest = Parameter.get_param('company_financial_cutover.archive.' + key)
        opening = self._remove_audit(batch)
        move_ids = self.env['account.move'].search([]).ids
        self.env['company.financial.cutover']._restore_archives(strict=False, archive_keys=['broken-first', key])
        recovered = self.env['company.financial.cutover'].search([('archive_key', '=', key)])
        self.assertEqual(recovered.move_id, opening)
        self.assertEqual(opening.financial_cutover_id, recovered)
        self.assertEqual(self.env['account.move'].search([]).ids, move_ids)
        self.assertEqual(Parameter.get_param('company_financial_cutover.archive.' + key), manifest)
        issue = self.env['company.financial.recovery.issue'].search([('archive_key', '=', 'broken-first')])
        self.assertEqual(issue.state, 'pending')
        self.assertEqual(Parameter.get_param('company_financial_cutover.archive.broken-first'), 'invalid JSON')
        with self.assertRaises(AccessError):
            issue.with_context(_recovery_token=True).write({'state': 'resolved'})
        with self.assertRaises(AccessError):
            issue.unlink()

    def test_repaired_archive_retry_restores_once_and_clears_pending_issue(self):
        self._invoice()
        batch = self._run()
        archive, key = batch.archive_attachment_id, batch.archive_key
        raw = archive.raw
        opening = self._remove_audit(batch)
        archive._write_move_archive({'raw': b'changed archive'})
        Model = self.env['company.financial.cutover']
        Model._restore_archives(strict=False, archive_keys=[key])
        self.assertFalse(Model.search([('archive_key', '=', key)]))
        issue = self.env['company.financial.recovery.issue'].search([('archive_key', '=', key)])
        self.assertEqual(issue.state, 'pending')
        archive._write_move_archive({'raw': raw})
        issue.action_retry()
        self.assertEqual(issue.state, 'resolved')
        self.assertEqual(Model.search([('archive_key', '=', key)]).move_id, opening)
        self.assertFalse(self.env['ir.config_parameter'].sudo().get_param('company_financial_cutover.recovery_issue.' + key))
        issue.action_retry()
        self.assertEqual(Model.search_count([('archive_key', '=', key)]), 1)

    def test_pending_archive_checkpoint_keeps_original_evidence_and_reason(self):
        self._invoice()
        batch = self._run()
        raw, report = batch.archive_attachment_id.raw, batch.report_attachment_id.raw
        key = 'company_financial_cutover.recovery_issue.' + batch.archive_key
        Parameter = self.env['ir.config_parameter'].sudo()
        Parameter.set_param(key, 'Original recovery diagnosis')
        self.env['company.financial.cutover']._archive_completed()
        self.assertEqual(batch.archive_attachment_id.raw, raw)
        self.assertEqual(batch.report_attachment_id.raw, report)
        self.assertEqual(Parameter.get_param(key), 'Original recovery diagnosis')

    def test_settings_administrator_can_retry_without_accounting_manager_role(self):
        self._invoice()
        batch = self._run()
        archive, key = batch.archive_attachment_id, batch.archive_key
        raw = archive.raw
        opening = self._remove_audit(batch)
        archive._write_move_archive({'raw': b'changed'})
        self.env['company.financial.cutover']._restore_archives(strict=False, archive_keys=[key])
        issue = self.env['company.financial.recovery.issue'].search([('archive_key', '=', key)])
        archive._write_move_archive({'raw': raw})
        administrator = new_test_user(self.env(context={**self.env.context, 'no_reset_password': True}),
            login='recovery_settings_administrator', groups='base.group_system',
            company_id=self.source.id, company_ids=[self.source.id, self.target.id])
        self.assertFalse(administrator.has_group('account.group_account_manager'))
        issue.with_user(administrator).action_retry()
        self.assertEqual(issue.state, 'resolved')
        self.assertEqual(self.env['company.financial.cutover'].search([('archive_key', '=', key)]).move_id, opening)
