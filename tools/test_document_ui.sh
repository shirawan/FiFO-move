#!/usr/bin/env bash
# Real Chromium tests against the task's own disposable PostgreSQL only.
set -euo pipefail
DOC_UI_DATABASE=${1:?Pass the disposable fifo_doc_repeat_* database printed by test_documents.sh}
case "$DOC_UI_DATABASE" in fifo_doc_repeat_*|fifo_doc_candidate) ;; *) echo 'Only disposable document test databases are allowed.' >&2; exit 2;; esac
DOC_UI_REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DOC_UI_PG=/workspace/.fifo-env/sysroot/usr/lib/postgresql/17/bin
DOC_UI_SOCKET=/workspace/fifo-document-work/runtime/socket
DOC_UI_PYTHON=/workspace/.fifo-env/venv/bin/python
DOC_UI_EVIDENCE=/workspace/fifo-document-work/evidence
"$DOC_UI_PYTHON" -c 'import websocket' || { echo 'Install websocket-client==1.8.0 in the runtime venv for UI tests.' >&2; exit 2; }
test "$("$DOC_UI_PG/psql" -h "$DOC_UI_SOCKET" -p 55433 -U agent -d postgres -At -c 'SHOW data_directory')" = /workspace/fifo-document-work/runtime/pgdata
test -z "$("$DOC_UI_PG/psql" -h "$DOC_UI_SOCKET" -p 55433 -U agent -d postgres -At -c 'SHOW listen_addresses')"
DOC_UI_LOG="$DOC_UI_EVIDENCE/browser-$DOC_UI_DATABASE-$(date -u +%Y%m%d%H%M%S).log"
"$DOC_UI_PYTHON" /workspace/odoo-19/odoo-bin -d "$DOC_UI_DATABASE" \
 --db_host="$DOC_UI_SOCKET" --db_port=55433 --db_user=agent \
 --addons-path="/workspace/odoo-19/addons,$DOC_UI_REPO" \
 --data-dir=/workspace/fifo-document-work/runtime/data \
 --test-enable --test-tags=document_browser --stop-after-init \
 --http-interface=127.0.0.1 --http-port=18436 --max-cron-threads=0 \
 --screenshots="$DOC_UI_EVIDENCE/gaps-browser" --logfile="$DOC_UI_LOG"
"$DOC_UI_PYTHON" - "$DOC_UI_LOG" <<'PY'
import pathlib,re,sys
text=pathlib.Path(sys.argv[1]).read_text()
summary=re.findall(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests',text)
assert summary and tuple(map(int,summary[-1]))==(0,0,2),summary
assert 'skipped TestDocumentBrowser' not in text,'Browser tests skipped'
assert text.count('.browser: test successful')==2,'Both real browser flows must finish'
print('2 Chromium browser tests passed:',sys.argv[1])
PY
