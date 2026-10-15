# Validation report — 19.0.0.4.0, 15 October 2026 UTC

This is a test candidate. It extends supported document migration; it does not certify
production, arbitrary custom modules or complete reconstruction of missing stock history.

## Changes and gap status

| Prior gap | Current result |
|---|---|
| Existing unmarked accounting copies | Implemented explicit verified adoption, including native payments and bill/PO links; no automatic similarity merge |
| 200-record ceiling | Replaced by resumable steps, default 100 records; native 201-document and 201-PO tests; up to 50,000 review bound, not performance certification |
| Incremental continuation | Additional independent history supported explicitly; edits/new matches on already moved history still require review |
| Recovery after uninstall | New receipts include settings and all choices; automatic completed/partial recovery; corrupt receipts isolated |
| Analytic allocations | Explicit plan/account mapping and native analytic amount verification |
| Fiscal positions and cash rounding | Explicit configuration matching and native posting checks |
| Foreign partial manual payments | Native regression added |
| Exchange/cash-basis history | Native generated-entry matching and exactly-once coverage for tested scenarios |
| Bank statements | Native statement grouping/line posting/reconciliation; entire statements required; generated-payment collections remain unsupported |
| Lots/serials/owners/packages | Verified reuse of existing native detailed history, without changing inventory |
| Missing destination stock history | Still requires complete movement/valuation reconstruction and existing-opening reconciliation; not implemented |
| Production/Enterprise/custom localization/large-data timings | Requires a restored copy and installed modules; not certified |

## Runtime and evidence

Odoo 19 Community commit `9ec2b55d3fa343e6547a9db54d5f3f5c776c535d`, Python 3.12,
PostgreSQL 17.11. Dedicated PostgreSQL socket port 55433, empty TCP listen address, own
`/workspace/fifo-document-work/runtime/pgdata`. Scheduled jobs disabled during committed
simulation, no outgoing mail servers or enabled payment providers. Stock mover not installed.

Executed current-release outcomes (overlapping runs, not additive):

| Check | Result | Evidence |
|---|---|---|
| Fresh complete four-addon suite | PASS 235, zero failures/errors | fifo_doc_repeat_20261015093055_576994.log |
| Final accounting suite with Purchase absent | PASS 49, zero failures/errors | gaps-final-accounting-only.log |
| Final ownership/adoption/continuation guards | PASS 4, zero failures/errors | gaps-final-ownership.log |
| Actual desktop and phone Chromium workflows | PASS 2, no skips | browser-fifo_doc_repeat_20261015093055_576994-20261015093709.log |
| Committed A+B+C, earlier-copy adoption, existing new-company trading | PASS, five saved steps | gaps-simulation.json |
| Actual native uninstall and absent-addon readback | PASS, unchanged business IDs/private receipts | gaps-uninstall.log; gaps-survival-absent.json |
| Automatic completed/partial recovery and exactly one remaining invoice | PASS | gaps-survival-reinstalled.json |

The full 235-test checkpoint precedes the last ownership-marker hardening; the final
49 accounting-only tests and four targeted tests exercise those final changes. The browser
runner also executes against the final code. RELEASE-VALIDATION.json records filenames,
results and current critical runtime hashes. Earlier checkpoint: 228 native tests passed in a fresh database
(`fifo_doc_repeat_20261015091335_574299.log`). A later 234-test run found one stale assertion:
the edited-evidence review now returns a combined blocked report instead of a popup error.
The test was updated to require blocked review, failed confirmation and no extra copy.
It was not changed to accept altered accounting evidence.

Chromium 151 desktop (1440×1000) and phone (390×844): both real browser workflows passed
in `gaps-browser-v4.log`; desktop exercises two confirmed steps, phone one confirmation.
Earlier skipped browser tests lacked websocket-client; installed pinned 1.8.0, then reran.
Earlier browser attempts exposed a mobile toolbar ordering issue and own-transaction false
freshness failures in Odoo's shared HTTP test cursor. The guard now considers committed
external versions, retaining real concurrency detection. Native retry/concurrent-change
regressions are included in the complete suite. Failed/interrupted logs remain evidence.

## Committed simulation and recovery

`gaps-simulation.py/.json/.log` builds old A+B+C and standalone new A with existing trading,
an already-copied invoice and a separate similar sale. It adopts the earlier copy, keeps the
accountant's separate-transaction reason, and confirms multiple steps across real commits.
Original source records, unrelated trading, native stock quantities/values and costs remain
unchanged. The historical PO retains ordered10, receipt4, return1, net received3, net billed2,
currently billable1 and pending incoming6. Destination posted balances: AR327, cash150,
AP−178, revenue−677, expense378, equity0; trial balance net0, profit299.

A second incremental job saves one sale11 and leaves sale22 pending. A backed-up clone
actually uninstalls both document addons. Native IDs and original private receipts are
compared while absent. Reinstall restored both jobs automatically, including all matches and reasons, then resumed
exactly one remaining invoice. Native IDs and receipts stayed identical until that resume. Final expected balances:
AR360, cash150, AP−178, revenue−710, expense378, equity0; net0, profit332.
See `gaps-survival-*.json` and uninstall/absent/reinstall logs for the executed outcome.

## Practical limits

Adoption requires matching native accounting dates, ledger content and configuration;
renumbering/reference differences are retained. Similarity review cannot prove identity of
arbitrarily edited imports or clean up duplicated business events inside source history.
New independent incremental history is supported; reinterpreting already changed history is
not. Complete connected groups may exceed step size. Locks/freshness still scan locked
tables, so batching does not replace a restored-database performance rehearsal.

Missing stock/valuation history, manual stock valuation, stock valuation journals, pending
supplier returns, complex routes, EDI/provider/internal payments, manual tax-reporting journal
adapters and localization-specific legal behavior remain outside tested automatic replay.
The supplied stock addon itself is not executed; newer repository stock source is preserved.
Historical independent review in INDEPENDENT-REVIEW.md covers 19.0.0.3.0 only. This release
was validated through the documented native/browser/survival checks; no new independent
review is claimed.
