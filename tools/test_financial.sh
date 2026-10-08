#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
fifo_start_database
FIFO_TEST_LOG="$FIFO_RUNTIME_DIR/test-$(date -u +%Y%m%dT%H%M%S)-$$.log"
if fifo_odoo --without-demo -u company_financial_cutover --test-enable \
    --test-tags=/company_financial_cutover --stop-after-init --http-port=18070 \
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
