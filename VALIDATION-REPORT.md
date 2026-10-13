# PASS / FAIL / BLOCKED / NOT IMPLEMENTED report

Issued 13 October 2026 (UTC). Raw Odoo logs retain UTC timestamps.
Verdict: PASS for the supported bounded disposable-test paths. NOT a complete migration
of every requested accounting/purchase history, and NOT production-ready.

## Baseline and the six errors

The exact supplied ZIP is preserved. Financial19.0.1.1.1/stock19.0.2.7.4 and per-file
hashes are in BASELINE.json. The supplied accounting error report contains six native
tracebacks, all ending at the same overly broad freshness guard cutover.py501.
On a quiet new database the exact supplied suite PASSed38 tests (baseline-tests.log).
A real second-transaction unrelated ir_logging commit independently reproduced FAIL:
SerializationFailure with pgcode=None, then Odoo retrying() KeyError(None), one attempt.
Thus the six errors share a reproducible guard failure; their intended assertions were
interrupted, not six independently demonstrated accounting-calculation failures.

Candidate imports the reviewed locked-table tuple visibility check and ConcurrencyError
from safety base882b60b, preserving checks for changed inserts/updates/deletes and company
access. Existing native regression cases verify unrelated commits are ignored, relevant
changes retry via the real Odoo loop, and preview source changes remain rejected. The old
normalized-name contact test is intentionally replaced by explicit name-only contact
choice tests; silent name-based merging is not reintroduced to make that legacy test pass.

## Current four-fix release: 19.0.0.3.0

The prior a11e6e73 bundle had four reproduced gaps: renumbered invoice duplication,
changed PO billing policy, wrong-product pending receipts, and lost invoice payment
references. These are fixed. Pair-specific, reasoned accountant decisions allow genuinely
separate transactions while actual unmarked earlier copies remain blocked.

| Final release check | Result | Evidence |
|---|---|---|
| Fresh full four-addon suite before final edge-case guards | PASS 203, zero failures/errors | fifo_doc_repeat_20261013023220_369804.log |
| Final affected accounting/PO suites, including all added edge cases | PASS 56, zero failures/errors | four-fixes-final-56.log |
| Final accounting-only suite, Purchase absent | PASS 28, zero failures/errors | four-fixes-final-account-only.log |
| Blank reference before native-write correction | FAIL reproduced; fixed in final suite | four-fixes-blank-reference-before.log |
| Committed A+B+C / already-used new A simulation | PASS | four-fixes-simulation.json / .log |
| Independent SQL/native readback of final committed fixture | PASS; 19 documents, 4 POs, no extra opening | independent-four-fixes-reconciliation.json / .log |
| Actual addon removal and reinstall on backed-up synthetic clone | PASS, native IDs/audit and receipt links retained | four-fixes-survival-absent.json, four-fixes-survival-reinstalled.json |
| Reinstalled-record verification against final code | PASS | four-fixes-final-survival.log and four-fixes-survival-reinstalled.json |

Counts are separate overlapping runs, not additive. The final 56 tests rerun all affected
paths after the later-accounting-date overlap and blank-reference corrections. Earlier
full-suite results are retained as earlier checkpoints, not claimed to cover later code.

The current committed fixture includes an additional legitimate separate sale100 approved
with an exact-pair accountant reason. Expected balances are AR327, bank150, AP−178,
revenue−677, expense378, equity0; trial balance net0 and profit299. The historical order
remains ordered10, net received3, net billed2, billable1 and pending incoming6. Source
records, unrelated new-company trading and existing stock quantities/values/costs remain
unchanged. Original payment reference KEEP-REMITTANCE-FOUR-FIXES survives posting and
addon removal/reinstall. Recreated jobs restore explicit matches and separate-transaction
decisions from the original audit; they reuse the same native records.

Legacy completed mappings retain their old fingerprint format and are additionally checked
for current billing policy, billable quantities, pending receipt identity and original
payment reference. Already-damaged old copies stop for review; automatic repair is not
implemented. Commercial overlap checks tolerate different accounting dates/rates but cannot
guarantee detection of arbitrarily altered imports. Production and missing-stock-history
limits below still apply.

## Earlier checkpoints (historical evidence)

### Earlier native Odoo evidence

| Check | Status | Evidence |
|---|---|---|
| Exact supplied native suite, quiet isolated DB | PASS —38 | baseline-tests.log |
| Unrelated commit + supplied real retry loop | FAIL confirmed baseline defect | independent-snapshot-probe.log / .py |
| Fresh full suite including historical PO extension (before final two lock tests) | PASS —193,0 failures,0 errors | fifo_doc_repeat_20261013014044_365775.log |
| Earlier candidate fresh install using shipped runner | PASS —179 | fifo_doc_repeat_20261013010325_362666.log |
| Final affected accounting-document/PO suites, including locked completed orders | PASS —44,0 failures,0 errors | history-final-lock-tests.log |
| Source PO locked after preview requires a fresh review | PASS —1 targeted regression | history-lock-preview-regression.log |
| Accounting document suite after extension, Purchase absent | PASS —22; earlier combined financial/document suite116 | history-accounting-only.log; document-account-only-final.log |
| Native A+B+C to standalone new A with existing activity | PASS supported subset | simulation-results.json, final-simulation-verification.json |
| Independent SQL/native readback, no planner totals trusted | PASS | independent-posted-reconciliation.json / .py / .log |
| Two simultaneous confirmations through real retrying() | PASS —one and two attempts; no duplicates | simulation.log/results |
| Repeat confirmation / lost response | PASS | simulation-results.json |
| Controlled interruption after native creation | PASS —rollback + restart | TestDocumentMigration.test_controlled_interruption_rolls_back_native_records |
| Existing historical receipts/returns plus multiple bills and credit | PASS —native links, no economic stock change | history-simulation-final.log / history-simulation.json |
| Fully received/billed/locked PO, no remaining commitment | PASS | test_fully_received_billed_locked_order_preserved |
| Stock value differences, unknown existing bill lines, pending returns, duplicate PO ownership | PASS expected BLOCKED / atomic rollback | history-final-lock-tests.log |
| History addon removal and reinstall | PASS —same native IDs, bill links, receipt matches and original audit | history-final-absent.log, history-final-reinstalled.log; history-survival-*.json |
| Independent historical SQL and native readback | PASS | independent-history-reconciliation.json |
| Optional addons uninstalled; native docs and audit remain | PASS | uninstalled-verification.log |
| Reinstall, new job, repeat copies | PASS —same15 documents/4 payments/3 POs | reinstalled-verification.log, survival-results.json |
| Scope sorting after cache refresh | PASS; confirmed fail-before reinstall evidence retained | diagnose-reinstall.log, reinstall-plan-diff.json, unsorted_branch_scope regression |
| Unauthorized cross-company read/write, forged fields and private audit access | PASS | native document regression suite |
| Both purchase workflows cannot activate a second commitment | PASS | old_and_new_purchase_workflows native test |
| Native database uniqueness for explicit source mappings | PASS | mapping_native_uniqueness test |
| Independent review | See current scoped review and limitations | INDEPENDENT-REVIEW.md |
| Production/enterprise/localization/browser/performance verification | NOT TESTED | No such claim |
| Actual stock mover execution | NOT TESTED / NOT EXECUTED | Stock module unchanged from supplied ZIP |

Native installed runtime: Odoo19 Community9ec2b55d3fa343e6547a9db54d5f3f5c776c535d,
Python3.12, PostgreSQL17.11. Own PG data directory /workspace/fifo-document-work/runtime/pgdata,
Unix socket:55433, listen_addresses empty; DBs fifo_doc_baseline, fifo_doc_candidate,
fifo_doc_simulation, fifo_doc_survival, fifo_doc_account_only, fifo_doc_history,
fifo_doc_history_survival_final and fresh fifo_doc_repeat_*.
No production connection or real-company data. Independent readback verified zero active
scheduled jobs/outgoing mail servers in the committed simulation. No payment providers,
real customer email/bank information, WhatsApp/integration credentials or real deliveries
were used. Native synthetic receipt/return fixtures ran in rollback tests and the isolated committed
history simulation. The migration reused those existing fixtures; it did not validate receipts.
Backups before fixtures and after completion, plus matching filestores, remain locally.

Tests use native account.move/account.payment posting/reconciliation and native purchase/
stock fixture logic. Deliberate fault injection is used only to trigger interruption/posting
failure; it is not a mock of the accounting outcomes. The concurrency and independent
SQL checks use real transactions. Initial unsuccessful candidate/harness attempts remain
in evidence; the successful final counts are distinct from them. New candidate UI views
were validated by installed Odoo; no new desktop/mobile browser automation was performed.

## Independently reconciled synthetic outcome

Target: AR227 (source220 plus existing sale7), bank150, AP−140, revenue−577,
expense340, trial balance net0, P&L/net balance-sheet profit237.
Customer residuals70/150/0; vendor residuals60/80/0.
15 native accounting documents including2 drafts,4 manual accounting payments,
3 native POs (draft/confirmed/cancelled); confirmed ordered10, received0, billed0,
pending delivery/billing10. Zero validated destination receipts and zero new opening entries.
Source accounting/PO fingerprints and existing destination sale fingerprint unchanged.

## Historical purchase extension: independent expected results

The user approved mapping existing receipts/returns and bill-to-PO links, with synthetic
testing and stock-migration execution disabled. New addons version19.0.0.2.0 implement
that bounded adapter. The committed final scenario has18 destination accounting documents,
4 POs,4 manual accounting payments, and retains unrelated new-company trading.

The adopted order has10 ordered, receipt4, return1, net received3. Two bills for1 and2 units
and a vendor credit for1 leave net billed2 and currently billable1. Commercial unreceived
quantity is7 while the actual existing pending incoming delivery remains6. No replacement
receipt is invented for the return. Bills19+38 less credit19 add38 to net liabilities/expense.
Expected target balances: AR227, bank150, AP−178, revenue−577, expense378, equity0;
trial balance net0, net profit199. Exact native receipt values, quantities, product costs,
source records and unrelated target trading remain unchanged. Independent review checks
raw SQL/native records rather than trusting preview totals.

Native `stock_account` is installed through `purchase_stock`. Bill posting invokes its
normal valuation hooks; before/after guards reject any economic change rather than
skipping those hooks. Tests exercise normal posting and separate injected failure paths.
The module remains a test-only candidate, not certification for every warehouse setup.

Both document addons were actually removed from a backed-up clone. Native invoices,
payments, POs, stock movements, bill-line relationships, durable receipt matches and private
original audit survived. Reinstall plus a new job reused all native IDs. Only verification-job
bookkeeping may update in native parameters; all saved business evidence is compared exactly.
Old custom job screens are not reconstructed automatically. Keep the original audit receipt
and recreate explicit matches for a new review.

Initial unsuccessful logs are retained. One extension error was an undefined local name in
untouched PO verification, fixed before the193-test run. A stale-review failure came from
noncanonical multiple invoice-line ordering, now sorted. Reinstall harness comparisons
initially treated a new verification-job ID as changed business evidence; the final harness
normalizes only that bookkeeping field and verifies every other field plus native IDs.

## Remaining unsupported cases

| Requirement / case | Status and practical reason |
|---|---|
| Received/returned/partially or fully billed and locked completed POs with verified target history | PASS within documented ordinary untracked-goods scope |
| Vendor bill/credit lines linked to adopted destination PO lines | PASS through native creation/posting and final quantity checks |
| Missing destination receipt/return/PO history | BLOCKED; reconstruction and new historical stock migration NOT IMPLEMENTED |
| Unmarked existing invoice/payment/bill lines | BLOCKED; explicit accounting adoption/overlap correction NOT IMPLEMENTED |
| Lots/serials, packages, owners, manual receipt valuation, valuation journals, pending returns, multi-step pending routes | BLOCKED; additional history mappings needed |
| Native posting changes existing stock economics or creates extra accounting | Atomic rollback; separate valuation review required |
| Post-cutover stock movements or reconciliation counterparts | BLOCKED; true historical as-of reconstruction NOT IMPLEMENTED |
| Exchange-difference/cash-basis/statement/analytic/EDI/fiscal-position/cash-rounding histories | BLOCKED; reviewed mapping logic NOT IMPLEMENTED |
| Provider/internal payments or payments without a covered native journal | BLOCKED; only manual ledger-backed accounting payments supported |
| Foreign invoices with matching native amounts | PASS unpaid invoice tests; foreign partial payments NOT SEPARATELY TESTED |
| Shorter history window plus H−1 opening | NOT IMPLEMENTED; full history replay, no added opening |
| More than200 accounting documents or200 POs, incremental/chunked continuation | NOT IMPLEMENTED; atomic job stops at bound |
| Production/Enterprise/custom localization/browser/performance | NOT TESTED |
| Actual stock mover execution | NOT EXECUTED; original stock files unchanged |

The user is unsure whether matching destination stock history exists. A restored-copy
review must establish that before applying this adapter. No approval to deploy production,
push Git or create historical stock transactions has been assumed or used.
