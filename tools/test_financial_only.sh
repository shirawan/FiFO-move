#!/usr/bin/env bash
# Fresh synthetic database: installing financial must not install Purchase.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
fifo_start_database
FIFO_ONLY_DB="fifo_account_only_$(date -u +%Y%m%d%H%M%S)_$$"
FIFO_ONLY_LOG="$FIFO_RUNTIME_DIR/$FIFO_ONLY_DB.log"
# Serialize test runners that alter maintenance on this dedicated cluster.
# TransactionCase fixtures bypass Odoo's request retry loop, so background
# vacuum must not take NOWAIT locks while those long fixture transactions run.
fifo_only_sql() {
    "$FIFO_PG_BIN/psql" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -d postgres -At -v ON_ERROR_STOP=1 -c "$1"
}
test "$(fifo_only_sql 'SHOW data_directory')" = "$FIFO_RUNTIME_DIR/pgdata"
FIFO_ONLY_AUTOVACUUM=$(fifo_only_sql 'SHOW autovacuum')
case "$FIFO_ONLY_AUTOVACUUM" in on|off) ;; *) exit 1 ;; esac
fifo_only_restore_maintenance() {
    fifo_only_sql "ALTER SYSTEM SET autovacuum = '$FIFO_ONLY_AUTOVACUUM'" >/dev/null
    fifo_only_sql 'SELECT pg_reload_conf()' >/dev/null
}
trap fifo_only_restore_maintenance EXIT
fifo_only_sql "ALTER SYSTEM SET autovacuum = 'off'" >/dev/null
fifo_only_sql 'SELECT pg_reload_conf()' >/dev/null
"$FIFO_PG_BIN/createdb" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -T template0 "$FIFO_ONLY_DB"
"$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" -d "$FIFO_ONLY_DB" --db_host="$FIFO_RUNTIME_DIR/socket" \
    --db_port=55432 --db_user=agent --addons-path="$FIFO_ODOO_DIR/addons,$FIFO_REPO_DIR" \
    --data-dir="$FIFO_RUNTIME_DIR/odoo-data" --without-demo -i company_financial_cutover \
    --test-enable --test-tags=/company_financial_cutover --stop-after-init --no-http \
    --http-port=18071 --max-cron-threads=0 --logfile="$FIFO_ONLY_LOG"
"$FIFO_PYTHON" - "$FIFO_ONLY_LOG" <<'PY'
import pathlib, re, sys
matches = re.findall(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests', pathlib.Path(sys.argv[1]).read_text())
assert matches, 'No test result reported'
failed, errors, count = map(int, matches[-1])
assert count and not failed and not errors, matches[-1]
print(f'{count} financial-only tests passed. Log: {sys.argv[1]}')
PY
FIFO_ONLY_STATES=$("$FIFO_PG_BIN/psql" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -d "$FIFO_ONLY_DB" -At -v ON_ERROR_STOP=1 \
    -c "SELECT count(*) FROM ir_module_module WHERE name IN ('purchase', 'company_purchase_cutover') AND state = 'installed'")
test "$FIFO_ONLY_STATES" = 0
printf 'Verified: financial installed without Purchase or purchase migration. Database: %s\n' "$FIFO_ONLY_DB"
