#!/usr/bin/env bash
# Upgrade from the last published combined addon, preserving completed history.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime.sh"
: "${FIFO_LEGACY_BUNDLE:?Set FIFO_LEGACY_BUNDLE to the published company_financial_cutover-19.0.2.0.1.zip}"
fifo_start_database
FIFO_UPGRADE_DB="fifo_upgrade_tests_$(date -u +%Y%m%d%H%M%S)_$$"
FIFO_UPGRADE_DIR="$FIFO_RUNTIME_DIR/$FIFO_UPGRADE_DB"
mkdir -p "$FIFO_UPGRADE_DIR/addons" "$FIFO_UPGRADE_DIR/legacy"
"$FIFO_PYTHON" - "$FIFO_LEGACY_BUNDLE" "$FIFO_UPGRADE_DIR/legacy" <<'PY'
from pathlib import Path
from zipfile import ZipFile
import ast, sys
with ZipFile(sys.argv[1]) as archive:
    for name in archive.namelist():
        path = Path(name)
        assert not path.is_absolute() and '..' not in path.parts, 'Unsafe archive path'
    archive.extractall(sys.argv[2])
manifest = ast.literal_eval((Path(sys.argv[2]) / 'company_financial_cutover/__manifest__.py').read_text())
assert manifest['version'] == '19.0.2.0.1' and 'purchase' in manifest['depends']
PY
ln -s "$FIFO_UPGRADE_DIR/legacy/company_financial_cutover" "$FIFO_UPGRADE_DIR/addons/company_financial_cutover"
"$FIFO_PG_BIN/createdb" -h "$FIFO_RUNTIME_DIR/socket" -p 55432 -U agent -T template0 "$FIFO_UPGRADE_DB"
FIFO_UPGRADE_ARGS=(-d "$FIFO_UPGRADE_DB" --db_host="$FIFO_RUNTIME_DIR/socket" --db_port=55432 --db_user=agent
    --addons-path="$FIFO_ODOO_DIR/addons,$FIFO_UPGRADE_DIR/addons" --data-dir="$FIFO_RUNTIME_DIR/odoo-data"
    --without-demo --no-http --max-cron-threads=0)
"$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" "${FIFO_UPGRADE_ARGS[@]}" -i company_financial_cutover --stop-after-init --logfile="$FIFO_UPGRADE_DIR/legacy-install.log"
export FIFO_REPO_DIR
FIFO_SURVIVAL_FIXTURE="$FIFO_UPGRADE_DIR/fixture.json" FIFO_SURVIVAL_MODE=setup \
    "$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" shell "${FIFO_UPGRADE_ARGS[@]}" \
    --logfile="$FIFO_UPGRADE_DIR/setup.log" < "$FIFO_REPO_DIR/tools/retention_lifecycle.py"
rm "$FIFO_UPGRADE_DIR/addons/company_financial_cutover"
ln -s "$FIFO_REPO_DIR/company_financial_cutover" "$FIFO_UPGRADE_DIR/addons/company_financial_cutover"
ln -s "$FIFO_REPO_DIR/company_purchase_cutover" "$FIFO_UPGRADE_DIR/addons/company_purchase_cutover"
# Deliberately upgrade only financial: existing Purchase should activate the bridge.
"$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" "${FIFO_UPGRADE_ARGS[@]}" -u company_financial_cutover --stop-after-init --logfile="$FIFO_UPGRADE_DIR/upgrade.log"
FIFO_SURVIVAL_FIXTURE="$FIFO_UPGRADE_DIR/fixture.json" FIFO_SURVIVAL_MODE=restored \
    "$FIFO_PYTHON" "$FIFO_ODOO_DIR/odoo-bin" shell "${FIFO_UPGRADE_ARGS[@]}" \
    --logfile="$FIFO_UPGRADE_DIR/verify.log" < "$FIFO_REPO_DIR/tools/retention_lifecycle.py"
printf 'Published-bundle upgrade checks passed. Evidence: %s\n' "$FIFO_UPGRADE_DIR"
