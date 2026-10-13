# Original accounting and purchase document migration — test candidate

This candidate is separate from the original supplied ZIP and from Git main. Do not
install it on production. It does not yet fulfill every requested document/history case.
The original opening-balance mover remains available, but is not full document migration.

## Provenance

Supplied package: FiFO-move-odoo19-f0f98f5.zip; SHA256
4e27269b39fa2ad0fb215cfdc52f870e250fb228455a17be6fd8c78b6d0d8d15.
Supplied financial version19.0.1.1.1, stock19.0.2.7.4; no supplied purchase addon.
Candidate incorporates prior reviewed financial/purchase fixes from882b60b, then adds
company_document_cutover19.0.0.3.0 and optional company_document_purchase_cutover19.0.0.3.0.
The stock module is preserved exactly as supplied, not installed/executed/tested here.
Original package, baseline per-file hashes, candidate per-file hashes and exact-source
patch accompany the deliverables. No Git push, merge or production deployment occurred.

## Choose the method with your accountant

This document candidate replays ALL original posted accounting through the inclusive
cutover date, including historical invoices, bills, credit notes, journal-backed manual
payments and original opening/closing journal entries. It copies covered draft invoices
as drafts. It does not add another opening at the cutover date. Thus old revenue, expense,
tax and receivables/payables are represented once. Historical P&L remains historical;
prior-year results are not separately folded into a new retained-earnings amount.
A shortened history window plus H-1 opening is NOT IMPLEMENTED.

Same legal entity/tax registration is confirmed by the user, and must be attested on each
job. Different company accounting currencies, root legal tax/fiscal configurations and
unsafe mapping stop execution. Root companies require all their branches included/enabled;
branch legal checks use parent settings. New company is standalone and can already trade.
Unrelated new-company activity stays intact. Identical unmarked earlier copies need an
accountant decision; adopting existing documents/overlap reversals is NOT IMPLEMENTED in
this new replay path. Earlier opening-balance migration prevents adding history on top.
The older financial mover's separate reviewed existing-data workflow remains distinct.

## Install on a restored/disposable copy

For accounting documents only, install Company Accounting Document Migration — Candidate.
It depends on the financial addon and does not install Purchase. For purchase documents,
add Company Purchase Document Migration — Candidate; this optional bridge installs
Purchase/Inventory relationships. Installing a module is not executing stock migration.
Settings and Accounting administrator permissions are required; Purchase manager is also
required for purchase commitments. Enable every selected source/branch and destination.

Use Original document moves (candidate): choose source companies, destination, cutover,
accounting/purchase selections and confirmations. In the accountant details choose the
old and new records by name: accounts, journals, taxes, contacts, products and payment terms.
No name-only contact identification is inferred. Shared records can be explicitly reused.
Company-owned records must map to accessible destination records. Tax IDs, tax configuration,
repartition accounts/tags, account types/currency and product units must agree.

Use Review my documents. It reports all found decisions, counts/states, account totals and
purchase ordered/received/billed/remaining quantities in one review. Confirm only after a
fresh review during an agreed maintenance window. Execution locks the relevant tables,
checks the transaction snapshot and recomputes the exact review digest. Native accounting
posting and reconciliation remain authoritative; no amount is forced and no lock is bypassed.

Destination uses its own numbering. The private audit keeps exact source number, label,
reference, native document/line mapping and fingerprints. Native ref, invoice origin, dates,
line quantities/prices/discounts/taxes, due dates and supported residuals are preserved.
Locked dates block rather than being silently moved into a different period.

## Supported payment/PO cases and explicit dependencies

Payments are manual accounting evidence only; no provider/token/payment transaction is
created. Covered native journal-backed manual payment relationships and reproducible native
partial/full reconciliations are carried. Cash-basis tax, exchange-difference reconciliation,
bank statements, internal/provider payments, analytic allocation, fiscal-position/cash-rounding
and EDI history are BLOCKED. Foreign invoices must reproduce native accounting amounts at
destination rates; discrepancies rollback. Foreign partial payments are NOT SEPARATELY TESTED.
Reconciliations reaching after cutover are BLOCKED; historical residual reconstruction is
NOT IMPLEMENTED. Payment terms whose schedules recompute differently also rollback.

Draft/sent RFQs, cancelled untouched POs and unreceived/unbilled confirmed POs can copy
natively. Confirmed copies have pending receipt commitments, never completed receipts.
Destination-specific products protect source/shared supplier records from native confirmation
side effects. Source POs stay unchanged: freeze old fulfilment operationally before using new
commitments. Cross-workflow source ownership prevents the older replacement-draft flow from
activating a second order.

Historical orders can now reuse an explicitly selected existing destination PO, its lines,
and completed receipts/returns. Every line and completed movement must have one exact match;
extra/missing movements, different quantities, terms, dates, return origins, values, pending
commitments or lock state stop the review. This includes partially and fully received/billed
orders and locked completed orders. No new PO or delivery commitment is created for adopted
history. Select these matches in the accountant details: Existing purchase order, Existing
purchase order line, Completed receipt or return. Missing matches are listed together.

Choose Accounting as well when linked bills/credits have not already been transferred.
Copied bills and vendor credits link to the existing destination PO lines during native
posting. The final received, billed and billable quantities must agree. Existing unmarked
bill lines block adoption: they need a separate overlap decision, not another copied bill.
The adapter does not adopt arbitrary unmarked existing invoices or payments.

For example, 10 ordered, 4 received and 1 returned means net received3. If two bills total3
units and a credit returns1 billed unit, net billed2 and currently billable1. The existing
open incoming delivery can still be6: a return does not itself create a replacement receipt.
The review shows actual pending receipt commitments and currently billable quantity alongside
ordered-minus-net-received/billed totals. It preserves that native workflow distinction.

Only ordinary untracked goods with matched valuation evidence are covered. Lots/serials,
packages, owners, manual receipt valuation adjustments, valuation journal-entry histories,
pending supplier returns and nonstandard/multi-step pending stock routes remain BLOCKED.
Native bill posting hooks still run. Any change to existing stock quantities/values,
product costs, manual valuation records or an extra accounting entry rolls back the whole
attempt. Source/target stock values are compared in company currency, including foreign POs.
Receipt/return histories after cutover are blocked. Purchase/stock timestamps use UTC calendar
days through 23:59:59 on the selected date; accounting uses native accounting dates.

The user is unsure whether matching destination history exists. Missing history remains a
concrete dependency; this candidate neither reconstructs past deliveries nor executes the
stock mover. Test against a restored copy to establish which orders have verified matches.

## Transactions, failed attempts and uninstall survival

Current execution is one atomic full-history job, at most200 accounting documents and200
POs. Larger histories, chunked continuation and incremental catch-up are NOT IMPLEMENTED.
Interrupted native creates rollback together; failed reports are private native attachments.
Retry/repeat confirmation and separate same-source jobs validate durable identity/evidence
and reuse exact existing copies. Deleted/edited target evidence stops reuse. Company scope
is sorted so ORM relation ordering cannot create a false stale-data warning.

Private ir.attachment audit files and ir.config_parameter links have no module XML IDs and
remain when the document addons are uninstalled. Keep the native Accounting, Purchase
and Inventory apps installed; survival testing removes only the mover addons. Copied accounting/payment/PO records are
native records, with no cascade link to a migration job. Reinstall prevents second copies;
it does not automatically recreate the old custom job screen. Administrators can retain
and inspect the saved JSON receipt, including exact receipt/return IDs and evidence.
After reinstall, recreate the accountant matches from that receipt before reviewing again. Normal employees cannot read those private audit files.
Native corrections after go-live remain accountant work; changed evidence must be reviewed.

## Reproduce tests and rollback the disposable copy

Pinned native runtime: Odoo19 Community commit9ec2b55d3fa343e6547a9db54d5f3f5c776c535d,
Python3.12, PostgreSQL17.11. Existing pinned installation uses /workspace/odoo-19 and
/workspace/.fifo-env/venv. Run bash tools/test_documents.sh from the extracted candidate.
The script verifies its dedicated local data directory and no TCP listener, creates a NEW
fifo_doc_repeat_* database, backs it up, installs/tests all four relevant addons, disables
cron, uses a dedicated loopback test port and checks the native result count/failure status.
It never takes a production URL/config. Test databases are retained for evidence.

Standalone simulation scripts and commands are in the evidence package. Synthetic source
A+B+C and standalone target include existing activity; source receipts/returns are fixtures
only. No stock-migration workflow is called. No configured SMTP server or active provider,
no real contact email/bank account, no scheduled jobs or external integration actions are
used. Test signup/enrichment hooks are controlled by native test-mode/context; older initial
logs show hook messages, which are not evidence of an actual external delivery.

Backups before fixtures and after completed simulation are retained in the local evidence
workspace with the matching filestore. To rollback the synthetic exercise, restore into a
NEW disposable DB using pg_restore plus its copied filestore. Never reset another database.
Browser/mobile automation for the new candidate UI, enterprise/localization addons, real
company data, actual stock execution and production performance are NOT TESTED.

## Fixes in 19.0.0.3.0

Before copying invoices, review checks for existing documents even when their number,
accounting date or base-currency exchange rate differs. Candidates use the commercial
invoice date, customer/vendor, currency, amount and mapped commercial line details.
This is a duplicate warning, not proof that two transactions are identical. Arbitrarily
changed earlier imports still require accountant review.

If two candidates are genuinely separate transactions, use **Accountant: Separate
transactions with similar details**, select that exact old/new pair, tick the separate
transaction choice and record why. Each pair needs its own decision, saved in the audit.
An actual earlier copy must stay unresolved: adopting existing unmarked accounting
records or automatically correcting earlier imports is not implemented.

Purchase products must use the same ordered/received-quantity billing policy. Review
and final native checks verify quantities still to bill and invoice status. Pending
receipts must belong to the correct product, company, unit and purchase line; quantities
are converted through native units. Completed-job retries also check these conditions,
including older links without the new supplementary evidence.

Original invoice payment references, including intentionally blank references, are
preserved through native creation/posting/write and included in private audit evidence.
Changes invalidate review or reuse. An older copy already missing its original reference
is stopped for accountant review; the mover does not silently recopy or repair it.

After reinstall, create a new review and restore explicit matches and separate-transaction
decisions from the original audit. Native records and the audit survive uninstalling these
mover addons; the old custom job screens are not automatically reconstructed.
