#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
fifo_start_database
# TransactionCase keeps one repeatable-read fixture transaction for many tests.
# Keep automatic maintenance deterministic in this dedicated test cluster;
# restore its setting on every normal exit. The production guard checks tuple
# changes in locked tables, and the suite verifies unrelated commits are ignored.
fifo_test_sql() {
    "$FIFO_PG_BIN/psql" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -d "$FIFO_DATABASE" -At -v ON_ERROR_STOP=1 -c "$1"
}
test "$(fifo_test_sql 'SHOW data_directory')" = "$FIFO_RUNTIME_DIR/pgdata"
FIFO_TEST_AUTOVACUUM=$(fifo_test_sql 'SHOW autovacuum')
case "$FIFO_TEST_AUTOVACUUM" in on|off) ;; *) exit 1 ;; esac
fifo_restore_test_maintenance() {
    fifo_test_sql "ALTER SYSTEM SET autovacuum = '$FIFO_TEST_AUTOVACUUM'" >/dev/null
    fifo_test_sql 'SELECT pg_reload_conf()' >/dev/null
}
trap fifo_restore_test_maintenance EXIT
fifo_test_sql "ALTER SYSTEM SET autovacuum = 'off'" >/dev/null
fifo_test_sql 'SELECT pg_reload_conf()' >/dev/null
FIFO_TEST_LOG="$FIFO_RUNTIME_DIR/test-$(date -u +%Y%m%dT%H%M%S)-$$.log"
if fifo_odoo --without-demo -u company_financial_cutover -i company_purchase_cutover --test-enable \
    --test-tags=/company_financial_cutover,/company_purchase_cutover --stop-after-init --no-http --http-port=18070 --max-cron-threads=0 \
    --logfile="$FIFO_TEST_LOG"; then
    "$FIFO_PYTHON" - "$FIFO_TEST_LOG" <<'PY'
import pathlib, re, sys
log = pathlib.Path(sys.argv[1]).read_text()
matches = re.findall(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests', log)
if not matches:
    raise SystemExit('The runner did not report completed tests; inspect ' + sys.argv[1])
failed, errors, count = map(int, matches[-1])
if failed or errors or not count:
    raise SystemExit('Tests failed or no tests executed; inspect ' + sys.argv[1])
print(f'{count} tests passed; 0 failed, 0 errors. Log: {sys.argv[1]}')
PY
else
    FIFO_TEST_EXIT=$?
    tail -n 100 "$FIFO_TEST_LOG"
    exit "$FIFO_TEST_EXIT"
fi
