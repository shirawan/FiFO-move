def post_init_hook(env):
    env["company.financial.cutover"]._restore_archives(strict=False)


def uninstall_hook(env):
    # The optional purchase addon runs its own uninstall guard and checkpoint.
    env["company.financial.cutover"]._archive_completed()
