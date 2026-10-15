#!/usr/bin/env bash
# Optional native EDI/provider boundary tests, only on the task's isolated test server.
set -euo pipefail
DOC_EXTERNAL_DB=${1:?Pass a disposable fifo_doc_repeat_* database from test_documents.sh}
case "$DOC_EXTERNAL_DB" in fifo_doc_repeat_*) ;; *) echo 'Only disposable repeat-test databases are allowed.' >&2; exit 2;; esac
DOC_EXTERNAL_REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DOC_EXTERNAL_PG=/workspace/.fifo-env/sysroot/usr/lib/postgresql/17/bin
DOC_EXTERNAL_SOCKET=/workspace/fifo-document-work/runtime/socket
DOC_EXTERNAL_LOG="/workspace/fifo-document-work/evidence/external-$DOC_EXTERNAL_DB-$(date -u +%Y%m%d%H%M%S).log"
test "$("$DOC_EXTERNAL_PG/psql" -h "$DOC_EXTERNAL_SOCKET" -p 55433 -U agent -d postgres -At -c 'SHOW data_directory')" = /workspace/fifo-document-work/runtime/pgdata
test -z "$("$DOC_EXTERNAL_PG/psql" -h "$DOC_EXTERNAL_SOCKET" -p 55433 -U agent -d postgres -At -c 'SHOW listen_addresses')"
/workspace/.fifo-env/venv/bin/python /workspace/odoo-19/odoo-bin -d "$DOC_EXTERNAL_DB" \
 --db_host="$DOC_EXTERNAL_SOCKET" --db_port=55433 --db_user=agent \
 --addons-path="/workspace/odoo-19/addons,$DOC_EXTERNAL_REPO" \
 --data-dir=/workspace/fifo-document-work/runtime/data -i account_edi \
 --test-enable --test-tags=/company_document_cutover:TestExternalBoundaries --stop-after-init \
 --no-http --http-interface=127.0.0.1 --http-port=18435 --max-cron-threads=0 --logfile="$DOC_EXTERNAL_LOG"
/workspace/.fifo-env/venv/bin/python - "$DOC_EXTERNAL_LOG" <<'PY'
import pathlib,re,sys
text=pathlib.Path(sys.argv[1]).read_text()
summary=re.findall(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests',text)
assert summary and tuple(map(int,summary[-1]))==(0,0,5),summary
assert 'skipped TestExternalBoundaries' not in text,'Native external boundary tests must not skip'
print('5 native external boundary tests passed:',sys.argv[1])
PY
