#!/usr/bin/env bash
# Only the isolated cloud runtime; never use a production connection/config.
set -euo pipefail
DOC_REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DOC_RUNTIME_DIR=/workspace/fifo-document-work/runtime
DOC_PG_BIN=/workspace/.fifo-env/sysroot/usr/lib/postgresql/17/bin
DOC_PYTHON=/workspace/.fifo-env/venv/bin/python
DOC_ODOO_DIR=/workspace/odoo-19
DOC_EVIDENCE_DIR=/workspace/fifo-document-work/evidence
mkdir -p "$DOC_RUNTIME_DIR/socket" "$DOC_RUNTIME_DIR/data" "$DOC_EVIDENCE_DIR"
if [[ ! -e "$DOC_RUNTIME_DIR/pgdata/PG_VERSION" ]]; then
  "$DOC_PG_BIN/initdb" -D "$DOC_RUNTIME_DIR/pgdata" --auth-local=trust --auth-host=scram-sha-256 --encoding=UTF8 --locale=C.UTF-8
fi
if ! "$DOC_PG_BIN/pg_ctl" -D "$DOC_RUNTIME_DIR/pgdata" status >/dev/null 2>&1; then
  "$DOC_PG_BIN/pg_ctl" -D "$DOC_RUNTIME_DIR/pgdata" -l "$DOC_RUNTIME_DIR/postgres.log" -o "-k $DOC_RUNTIME_DIR/socket -p 55433 -c listen_addresses='' -c autovacuum=off" start
fi
DOC_SQL=("$DOC_PG_BIN/psql" -h "$DOC_RUNTIME_DIR/socket" -p 55433 -U agent -d postgres -At -v ON_ERROR_STOP=1)
test "$("${DOC_SQL[@]}" -c 'SHOW data_directory')" = "$DOC_RUNTIME_DIR/pgdata"
test -z "$("${DOC_SQL[@]}" -c 'SHOW listen_addresses')"
test "$(git -C "$DOC_ODOO_DIR" rev-parse HEAD)" = 9ec2b55d3fa343e6547a9db54d5f3f5c776c535d
DOC_DATABASE="fifo_doc_repeat_$(date -u +%Y%m%d%H%M%S)_$$"
DOC_LOG="$DOC_EVIDENCE_DIR/$DOC_DATABASE.log"
"$DOC_PG_BIN/createdb" -h "$DOC_RUNTIME_DIR/socket" -p 55433 -U agent -T template0 "$DOC_DATABASE"
"$DOC_PG_BIN/pg_dump" -h "$DOC_RUNTIME_DIR/socket" -p 55433 -U agent -Fc -f "$DOC_EVIDENCE_DIR/$DOC_DATABASE-before.dump" "$DOC_DATABASE"
"$DOC_PYTHON" "$DOC_ODOO_DIR/odoo-bin" -d "$DOC_DATABASE" --db_host="$DOC_RUNTIME_DIR/socket" --db_port=55433 --db_user=agent \
  --addons-path="$DOC_ODOO_DIR/addons,$DOC_REPO_DIR" --data-dir="$DOC_RUNTIME_DIR/data" --without-demo \
  -i company_document_purchase_cutover,company_purchase_cutover --test-enable \
  --test-tags=/company_financial_cutover,/company_purchase_cutover,/company_document_cutover,/company_document_purchase_cutover \
  --stop-after-init --no-http --http-interface=127.0.0.1 --http-port=18433 --max-cron-threads=0 --logfile="$DOC_LOG"
"$DOC_PYTHON" - "$DOC_LOG" <<'PY'
import pathlib,re,sys
matches=re.findall(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests',pathlib.Path(sys.argv[1]).read_text())
assert matches,'No native test summary found'
f,e,n=map(int,matches[-1]);assert n and not f and not e,matches[-1]
print(f'{n} native tests passed; log {sys.argv[1]}')
PY
printf 'Disposable database retained for evidence: %s\n' "$DOC_DATABASE"
