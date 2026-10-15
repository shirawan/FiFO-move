def post_init_hook(env):
    env['company.document.cutover'].with_context(allowed_company_ids=env['res.company'].sudo().search([]).ids).action_restore_saved_moves()
