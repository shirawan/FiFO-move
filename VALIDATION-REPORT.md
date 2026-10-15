# Validation report — 19.0.0.5.0, 15 October 2026 UTC

This candidate implements the selected **preserve current stock and retain missing history
for reference** option. It does not reconstruct missing native receipts or make historical
outstanding quantities into new supplier commitments.

| Area | Status and evidence |
| --- | --- |
| Missing historical PO/receipt/return history | PASS as immutable audit snapshots; no native PO, done move or received quantity invented |
| Original bills linked to audit-only POs | PASS: native accounting posting, durable original PO/line references; no fabricated purchase_line_id |
| Existing manual valuation / valuation journal history | PASS for explicit existing matches; no new adjustment or valuation posting |
| Existing pending returns and multi-step routes | PASS for complete one-to-one native graph matches, quantities, dates, values and edges |
| Current stock and original source | PASS in native regressions, committed simulation and independent SQL readback |
| Existing destination transactions / earlier transfers | PASS for verified reuse, duplicate-pair decisions and unchanged unrelated trading |
| Interrupted steps, retries, removal and reinstall | PASS: actual native uninstall/reinstall/resume; no duplicate records |
| Nontechnical UI | PASS: explicit stock-preserving choice, old-company follow-up quantities, read-only history screen; desktop/phone Chromium |
| Company access | PASS: every covered company required for archive read, cross-company bill scope rejected, read-only valuation ACLs retain company rules |
| Browser blob handling / damaged markers | PASS: binary-size context, exact download bytes, safe accountant-review message |
| Provider/token, EDI, paired internal transfers, statement-generated payments | BLOCKED pending specialized native-history adapters; source and target safety boundaries tested |
| Missing native stock reconstruction / automatic remaining-delivery draft for audit-only history | NOT IMPLEMENTED; outside the chosen audit-only method |
| Real data, custom localizations, production-size timing | NOT TESTED; restored-copy validation required |

## Native runtime and exact baseline

Odoo Community 19 commit `9ec2b55d3fa343e6547a9db54d5f3f5c776c535d`, Python 3.12,
PostgreSQL 17, Chromium 151. Isolated Unix socket
`/workspace/fifo-document-work/runtime/socket`, port 55433, PostgreSQL
`listen_addresses=''`; no production connection. The simulation asserts no outgoing mail
servers or enabled payment providers and disables crons. The stock-migration addon is not
installed or executed. Synthetic receipts/returns are fixture setup only.

The exact supplied ZIP SHA-256 remains
`4e27269b39fa2ad0fb215cfdc52f870e250fb228455a17be6fd8c78b6d0d8d15`.
`BASELINE.json` retains supplied file hashes. Document addons are 19.0.0.5.0; financial
19.0.3.1.1 and legacy optional purchase 19.0.3.1.0. The installer retains the exact supplied
stock source (19.0.2.7.4), unchanged and untested; the Git repository retains its newer stock
fixes. The patch applies against the exact supplied extracted baseline.

## Test evidence

These are overlapping checks, not counts to add together. Native Odoo runs execute actual
ORM, accounting posting/reconciliation, PostgreSQL savepoints/locks and installed views.
Some tests inject faults or poison external hooks; they do not simulate successful provider
operations or replace the native accounting engine.

| Run | Result | Evidence file |
| --- | --- | --- |
| All four relevant addons, optional EDI installed | 266 PASS, no failed/error tests | archive-release-full.log |
| Accounting without Purchase, optional EDI installed | 55 PASS | archive-release-accounting-only.log |
| Final affected archive/native-history/recovery cases | 25 PASS | archive-final-optional-regressions.log |
| Optional native provider/EDI safety boundaries | 5 PASS, no skips | archive-external-final.log |
| Desktop/phone accounting + history screens | 4 PASS, six real browser page flows | final browser log in evidence ZIP |
| Final restored-job read/download/retry | PASS; all native rows/write dates unchanged | archive-final-readback.json |
| Independent committed SQL readback | PASS | independent-v5-readback.json |

The release runs include the final browser binary-size and optional-only recovery fixes.
The 25-case affected run covers exact downloads, damaged marker rendering, recursive stock
evidence, permissions, complete recovery and recovery on retained jobs after optional-only
removal. Chromium exercises the corrected screen. All named runs have zero failed/error
tests. Earlier failed development runs are diagnostic evidence and are superseded by these
results.
Reproduce the basic suite with `bash tools/test_documents.sh`; it creates and backs up a
fresh disposable database. The optional EDI cases skip when that optional app is absent.
Use `bash tools/test_document_external.sh <fifo_doc_repeat_database>` to require all five
external-boundary cases with native EDI installed. Use
`bash tools/test_document_ui.sh <fifo_doc_repeat_database>` to require four Chromium tests
and six successful page flows. The final combined run additionally installed `account_edi`
and tested all four addons, excluding the separately run `document_browser` tag.

## Committed simulation and actual uninstall/reinstall

`archive-simulation.py` builds old A with branches B/C and a standalone already-trading
new A. It includes unpaid/partial/paid invoices and bills, credits, manual accounting
payments, native untouched RFQs/POs and a historical order with receipts, a return, two
bills and a credit. The new company already has stock 17 and unrelated trading.

The original historical order has ordered 10, net received 3, net billed 2, billable 1 and
pending supplier delivery 6. The first related financial step atomically saves its complete
audit snapshot. The job is deliberately left partial, then cloned and backed up before
actual addon removal. Native accounting, PO, stock and private historical/partial receipt
records retain their IDs and contents while the addons are absent. Reinstall reconstructs
the history screen and choices automatically; resuming finishes exactly once.

Independent SQL sums after resume:

| Account | Balance |
| --- | ---: |
| Receivable | 227 |
| Bank | 150 |
| Payable | -178 |
| Revenue | -577 |
| Expense | 378 |
| Equity entries in this fixture | 0 |
| Total | 0 |

P&L and balance-sheet net profit both equal 199. Existing stock remains 17. All 43 saved
stock moves, 43 lines, 23 quants and 12 product-value rows are unchanged. Three historical
bills have distinct native target IDs and no fabricated native PO links. Only the three
untouched orders are new native POs; one audit-only historical record is present. Its old
pending deliveries remain follow-up information, not active destination commitments.

Evidence: archive-simulation.json, archive-survival-absent.json,
archive-survival-reinstalled.json, archive-final-readback.json, independent-v5-readback.json,
and their reproducible scripts/logs. A second backed-up clone verifies uninstalling only the
optional purchase document addon: existing base-job IDs/states, every native row ID/write
date and original private records survive; reinstall restores the screen, selection and
one-to-one PO/line/stock/lot/location/package/manual-value matches. Evidence:
archive-optional-survival.json and archive-optional-native-fixture.json. Database dumps and filestores stay local and are not
included in the distributable bundle.

## Review, limitations and rollout boundary

An independent reviewer challenged company coverage, recursive route valuation/evidence,
external target gates, read permissions and the browser blob crash. All confirmed findings
were corrected and rechecked. INDEPENDENT-V5-IMPLEMENTATION-REVIEW.md contains the findings,
resolutions, runtime hashes and independent SQL evidence. The final source hash manifest
identifies the delivered document-addon source.

Keeping stock does not prove its financial balance is missing in the destination. Source
stock-linked journal entries require verified existing adoption; no extra inventory balance
is replayed over unknown openings. Other existing ledger overlap still needs accountant
review. Similarity decisions cannot repair every duplicate or altered import in the old
ledger. Full-table freshness scans and table locks remain maintenance-window operations;
time large connected groups on a restored copy.

No production database, supplier, live bank/provider, EDI service or stock-migration workflow
was accessed. Provider/EDI execution and paired/statement-generated payment adoption remain
unsupported. This is a tested candidate for restored-copy validation, not production certification.
