#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
fifo_start_database
FIFO_SERVER_RUNNING=false
if [[ -f "$FIFO_RUNTIME_DIR/odoo.pid" ]]; then
    if "$FIFO_PYTHON" - "$FIFO_RUNTIME_DIR/odoo.pid" "$FIFO_ODOO_DIR/odoo-bin" "$FIFO_RUNTIME_DIR/odoo.log" <<'PY'
from pathlib import Path
import sys
try:
    pid = int(Path(sys.argv[1]).read_text().strip())
    args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
    valid = sys.argv[2] in args and '--http-port=18069' in args and ('--logfile=' + sys.argv[3]) in args
except (ValueError, OSError):
    valid = False
sys.exit(0 if valid else 1)
PY
    then
        FIFO_SERVER_RUNNING=true
    fi
fi
if [[ "$FIFO_SERVER_RUNNING" == false ]]; then
    nohup "$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" \
        -d "$FIFO_DATABASE" --db-filter="^$FIFO_DATABASE$" --no-database-list \
        --db_host="$FIFO_RUNTIME_DIR/socket" --db_port=55432 --db_user=agent \
        --addons-path="$FIFO_ODOO_DIR/addons,$FIFO_REPO_DIR" --data-dir="$FIFO_RUNTIME_DIR/odoo-data" \
        --http-interface=127.0.0.1 --http-port=18069 --max-cron-threads=0 \
        --logfile="$FIFO_RUNTIME_DIR/odoo.log" > "$FIFO_RUNTIME_DIR/odoo-console.log" 2>&1 &
    echo "$!" > "$FIFO_RUNTIME_DIR/odoo.pid"
fi
"$FIFO_PYTHON" - <<'PY'
import time, urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:18069/web/login?db=fifo_financial_tests', timeout=3) as response:
            page = response.read().decode()
            if response.status == 200 and 'name="login"' in page and 'name="password"' in page:
                print('Odoo login page returned HTTP 200 with the expected login form.')
                break
    except (OSError, ValueError):
        pass
    time.sleep(1)
else:
    raise SystemExit('Odoo did not become ready; inspect /workspace/.fifo-env/odoo.log')
PY
