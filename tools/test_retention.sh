#!/usr/bin/env bash
# Real Odoo lifecycle in a fresh local database; production is never used.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
fifo_start_database
FIFO_SURVIVAL_DB="fifo_survival_tests_$(date -u +%Y%m%d%H%M%S)_$$"
FIFO_SURVIVAL_DIR="$FIFO_RUNTIME_DIR/$FIFO_SURVIVAL_DB"
mkdir -p "$FIFO_SURVIVAL_DIR/addons"
FIFO_SURVIVAL_FIXTURE="$FIFO_SURVIVAL_DIR/fixture.json"
export FIFO_SURVIVAL_FIXTURE FIFO_REPO_DIR
fifo_survival_sql() {
    "$FIFO_PG_BIN/psql" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -d postgres -At -v ON_ERROR_STOP=1 -c "$1"
}
test "$(fifo_survival_sql 'SHOW data_directory')" = "$FIFO_RUNTIME_DIR/pgdata"
FIFO_SURVIVAL_AUTOVACUUM=$(fifo_survival_sql 'SHOW autovacuum')
case "$FIFO_SURVIVAL_AUTOVACUUM" in on|off) ;; *) exit 1 ;; esac
fifo_survival_cleanup() {
    fifo_survival_sql "ALTER SYSTEM SET autovacuum = '$FIFO_SURVIVAL_AUTOVACUUM'" >/dev/null
    fifo_survival_sql 'SELECT pg_reload_conf()' >/dev/null
    # Keep the isolated database and logs for inspection; never drop other data.
}
trap fifo_survival_cleanup EXIT
fifo_survival_sql "ALTER SYSTEM SET autovacuum = 'off'" >/dev/null
fifo_survival_sql 'SELECT pg_reload_conf()' >/dev/null
"$FIFO_PG_BIN/createdb" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -T template0 "$FIFO_SURVIVAL_DB"
ln -s "$FIFO_REPO_DIR/company_financial_cutover" "$FIFO_SURVIVAL_DIR/addons/company_financial_cutover"
FIFO_SURVIVAL_ARGS=(-d "$FIFO_SURVIVAL_DB" --db_host="$FIFO_RUNTIME_DIR/socket" --db_port=55432 --db_user=agent
    --addons-path="$FIFO_ODOO_DIR/addons,$FIFO_SURVIVAL_DIR/addons" --data-dir="$FIFO_RUNTIME_DIR/odoo-data"
    --without-demo --no-http --max-cron-threads=0)
fifo_survival_server() {
    "$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" "${FIFO_SURVIVAL_ARGS[@]}" --stop-after-init "$@"
}
fifo_survival_shell() {
    FIFO_SURVIVAL_MODE="$1" "$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" shell "${FIFO_SURVIVAL_ARGS[@]}" \
        --logfile="$FIFO_SURVIVAL_DIR/$1.log" < "$FIFO_REPO_DIR/tools/retention_lifecycle.py"
}
fifo_survival_server -i company_financial_cutover --logfile="$FIFO_SURVIVAL_DIR/install.log"
fifo_survival_shell setup
fifo_survival_shell approval_race
fifo_survival_shell uninstall
fifo_survival_server -u base --logfile="$FIFO_SURVIVAL_DIR/uninstall.log"
test -L "$FIFO_SURVIVAL_DIR/addons/company_financial_cutover"
rm "$FIFO_SURVIVAL_DIR/addons/company_financial_cutover"
fifo_survival_shell absent
# Clone only this verified disposable database to exercise genuine native edits
# while the addon is absent, followed by an expected reinstall refusal.
FIFO_SURVIVAL_CHANGED_DB="${FIFO_SURVIVAL_DB}_changed"
"$FIFO_PG_BIN/createdb" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -T "$FIFO_SURVIVAL_DB" "$FIFO_SURVIVAL_CHANGED_DB"
"$FIFO_PYTHON" - "$FIFO_RUNTIME_DIR/odoo-data/filestore" "$FIFO_SURVIVAL_DB" "$FIFO_SURVIVAL_CHANGED_DB" <<'PY'
from pathlib import Path
import shutil, sys
root = Path(sys.argv[1])
source = root / sys.argv[2]
if source.exists():
    shutil.copytree(source, root / sys.argv[3])
PY
FIFO_SURVIVAL_ARGS[1]="$FIFO_SURVIVAL_CHANGED_DB"
fifo_survival_shell change_opening
ln -s "$FIFO_REPO_DIR/company_financial_cutover" "$FIFO_SURVIVAL_DIR/addons/company_financial_cutover"
if fifo_survival_server -i company_financial_cutover --logfile="$FIFO_SURVIVAL_DIR/changed-reinstall.log"; then
    printf 'Changed opening was unexpectedly accepted during reinstall.\n' >&2
    exit 1
fi
rg -q 'archived financial opening has changed' "$FIFO_SURVIVAL_DIR/changed-reinstall.log"
printf 'SURVIVAL: reinstall correctly refused a changed native opening without reposting.\n'
FIFO_SURVIVAL_ARGS[1]="$FIFO_SURVIVAL_DB"
fifo_survival_server -i company_financial_cutover --logfile="$FIFO_SURVIVAL_DIR/reinstall.log"
fifo_survival_shell restored
printf 'Uninstall, code removal and reinstall checks passed. Evidence: %s\n' "$FIFO_SURVIVAL_DIR"
