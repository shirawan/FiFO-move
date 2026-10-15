# Move original company documents — test candidate 19.0.0.5.0

Use a restored database first. This moves accounting and purchase history for the same
legal entity and tax registration, including A with branches B/C into a standalone new A.
The new company may already contain trading and verified earlier copies.

## Choose the right method

The optional `company_document_cutover` addon replays original accounting history through
an inclusive cutover date. It includes original opening/closing journal entries and creates
no second opening balance. The older financial opening-balance mover is a separate method;
do not place full history on top of a previously transferred opening.

Install `company_document_purchase_cutover` only when purchase documents are required.
Accounting-only installation does not install Purchase. Both document addons are version
19.0.0.5.0; the financial dependency is 19.0.3.1.1.

## The workflow

1. Enable every old company/branch and the new company in the company switcher. Open
   **Original document moves**, select the companies and cutover date, then choose
   accounting history, purchase orders, or both. For historical orders, the default is
   **Keep current stock; retain missing history for reference**. Explicitly matched existing
   orders remain native. Untouched orders can still become live commitments; review them.
2. Ask the accountant to verify the legal entity and review earlier transfers. For purchase
   commitments, stop old-company fulfilment before using the new commitments.
3. Use **Find setup matches** for unambiguous accounts, journals and other settings.
   Contacts use tax ID/reference or the exact shared record; names alone never merge contacts.
   Review the proposed settings and fill unresolved matches by record name.
4. Select **Review my documents**. The screen lists choices together, counts, account balances,
   purchase quantities and existing history. Resolve the list, then review again.
5. Select **Confirm document move** in the maintenance window. Each confirmation saves a
   durable step. If **Next step** appears, review and confirm the following step. Only
   **Result / Completed** means the selected accounting and purchase coverage has finished.

The phone layout keeps the primary Review/Confirm action visible. Desktop and phone
browser tests exercise the actual review and confirmation buttons.

## Records already in the new company

Select the old and existing new document under **Explicit accounts, taxes and document
matches**, using **Existing accounting document**. The mover checks native company, date,
state, journal, currency, partner, payment terms/reference, invoice details, ledger amounts,
taxes, analytics and reconciliation. Matching documents are reused without posting a second
copy. Original numbering and reference differences may remain in the existing document.
Other material differences require accountant review; no totals or dates are forced to fit.

Existing payments must retain their native payment record. Their reconciliations and
invoice links must correspond to the selected source history; unrelated destination payments
cannot be absorbed. Missing covered reconciliation/invoice links are established natively.
Vendor bills may be adopted into an explicitly matched historical PO when their native bill
lines already link to the correct destination PO lines.

Similarity warnings also catch renumbered invoices, using commercial invoice date, partner,
currency, amount and commercial line content. If two similar documents are separate real
transactions, record that exact pair and an accountant reason under **Separate transactions**.
This is a human decision; similarity cannot identify arbitrarily altered imports or clean up
all duplicate business transactions inside the old ledger.

## Larger histories and safe continuation

The default step target is 100 records. In the accountant's step-size section, choose
1–1,000 records per step and a review limit up to 50,000 (default 10,000). These are bounds,
not production-size performance guarantees. Connected invoices, credits, payments,
exchange/cash-basis entries and complete bank statements stay together, so a connected
group can exceed the target step size. Time such groups on your restored database.

Native purchase orders are processed in steps after the accounting coverage is present.
Audit-only purchase/stock snapshots are saved atomically with the first related bill step. Every step
uses native posting and maintenance locks. If a later step fails, only that step rolls back;
previous successful steps remain. Retrying reuses their durable links. Keep the new ledger
out of operational use until the chosen migration reaches Completed and is reconciled.

For additional independent source documents, explicitly choose **Include additional history**.
The destination must stay the same, the cutover date cannot move backwards, and previously
moved source documents must remain present. This does not rebaseline edited invoices,
changed source payments, or destination history that changed after go-live. Those cases need
an accountant reconciliation plan. Accounting-only continuation can verify previously linked
bills without copying purchase commitments again.

## Accounting coverage added in this version

- Native bank statement lines, statement grouping, balances and ledger reconciliation.
  Each statement must be wholly within coverage. Statement-generated payment collections
  still need an additional adapter.
- Exchange differences and cash-basis tax entries regenerated by native reconciliation.
  They are matched against the original ledger and mapped to durable links, rather than
  copied again. Different amounts, dates, ownership or unexpected generated entries roll back.
- Explicit analytic account/plan mappings, with native analytic amounts checked after posting.
- Matched fiscal-position rules and cash-rounding configurations, with native amounts checked.
- Foreign-currency invoices and matching-rate partial manual payments.
- Original invoice payment references, including deliberately blank references, in the audit.

This is tested Community coverage, not certification for every tax/localization combination,
all mixed exchange/cash-basis cases, EDI documents, payment providers or custom overrides.

## Historical POs and tracked inventory

Existing historical destination POs, lines, receipts and returns need explicit matches.
Native received/billed/currently billable quantities, billing policies and remaining incoming
commitments must agree. Copied or reused bills/credits link to those native PO lines.

Existing lots/serials, ownership and packages can now be verified with explicit lot, package,
contact and location matches. Product tracking policies, lot/serial numbers, quantities,
package hierarchy/types and relevant expiry dates must agree. The mover changes no stock
quantity, receipt value or product cost. Native bill valuation hooks run; economic changes
cause rollback.

Missing native receipts and returns can now be kept as **history only**, following your
choice to preserve current stock. The historical record includes original PO terms and
quantities, completed and pending receipt/return graphs, lot/owner/package details, manual
valuation evidence and valuation-entry references. It is a snapshot, not a rebuilt native PO,
Inventory history or a copy of every original PDF attachment. Original source documents stay
in place. Migrated bill lines retain durable audit references to their original PO/line;
no native purchase-line relationship or received quantity is invented.

**Historical purchase references** appears in Purchase and in the completed move. The screen
shows old-company received/billed/billable quantities and pending deliveries separately from
current operations. For example: ordered 10, net received 3, billed 2, currently billable 1,
but actual pending supplier delivery 6. A return does not automatically make another
replacement delivery. The reference creates no live PO, receipt or supplier commitment.
Resolve the remaining delivery and received-but-unbilled follow-up with the accountant and
purchase manager before go-live. This release does not automatically prepare a replacement
order for audit-only history.

You can instead select **Match every historical order to existing live records**. Manual
stock-value adjustments need exact existing adjustment matches; valuation journal entries
need verified existing ledger/receipt matches. Pending returns and multi-step routes need
one-to-one movement/location mappings, including all linked internal legs and return parents.
Every mapped node's quantities, state, dates, monetary value and route links must agree.
The saved evidence covers recursive nodes so later edits cannot silently authorize another copy.
No native receipt, return, valuation adjustment or valuation journal entry is generated by
these adapters. Existing valuation accounting and its complete reconciled component must
already agree; the mover does not repair those records.

Keeping stock does **not** prove that its financial balance is missing in the new company.
Stock-linked journal entries cannot be replayed onto unknown existing openings. Reuse verified
native entries, or have an accountant resolve the overlap separately. Rebuilding missing
native stock/valuation history remains outside this stock-preserving option.

The migration operator needs Settings, Accounting administrator and (when selected) Purchase
administrator access. The optional purchase bridge gives Accounting administrators read-only
access to native manual-value, lot and package-type evidence under Odoo's existing company
rules. It grants no stock write permissions. Purchase managers can read saved historical
references only when every company covered by the record is enabled.

## Removal and recovery

Every successful step stores private native audit evidence, settings, explicit matches and
separate-transaction reasons, independently of custom module tables. Uninstall through
Odoo before deleting the addon files. Keep Accounting, Purchase and Inventory installed.
Download the latest receipt and keep a database/filestore backup.

Reinstall automatically restores supported saved completed and partial moves with all their
choices. Removing only the optional purchase document addon also restores its saved
selection, existing-job matches and historical-reference screen when reinstalled. Partial
moves can resume from the next review. **Recover saved document moves**
retries recovery from the menu. A damaged receipt is reported and isolated; healthy moves
still recover. Changed/deleted native evidence is never silently accepted or copied again.

Automatic choice recovery applies to receipts written by 19.0.0.4.0. Older receipts retain
native duplicate markers and their original audit, but do not contain every setting needed
to reconstruct their screens automatically. Restore their explicit choices manually.

## Checks before using your real database

The delivered evidence includes native regressions, a committed A+B+C simulation into an
already-used destination, original invoice adoption, saved steps, and actual addon removal /
reinstall with automatic completed/partial recovery, including an interrupted audit-history
move. Desktop and phone Chromium tests exercise accounting and historical-reference screens.

Your restored database is still required to establish earlier-transfer overlap, missing stock
history, custom/localized behavior and real-size lock/request timing. No production database
or real payments/integrations were accessed. The original supplied stock addon remains
unchanged in the installer and was not executed.

## External payment and electronic-invoice boundaries

Provider-linked or token-linked payments, internal bank transfers, electronic-invoice history
and statement-generated payments remain **BLOCKED**, including target records selected for
reuse. A manual payment-method label does not waive an actual transaction/token relationship.
A target journal configured for EDI cannot receive newly copied invoices through this tool.
The safety tests use disabled synthetic native provider/EDI records and poison external hooks
to verify that no charge, capture, send or processing method is called. These are boundary
tests, not support for migrating a real provider or localization.

## Reproduce the native tests in this cloud workspace

`bash tools/test_documents.sh` creates and backs up a new isolated synthetic database and
runs the financial, optional purchase and both document addons. The optional EDI tests skip
when EDI is absent; `bash tools/test_document_external.sh <fifo_doc_repeat_database>` installs
only the optional native EDI test app on that disposable database and requires all five
provider/EDI boundary checks to execute. `bash tools/test_document_ui.sh <fifo_doc_repeat_database>`
requires four real Chromium tests (six page flows), with no skips. These scripts are pinned
to this task's Unix-socket test runtime and must not be adapted to production credentials.
The evidence ZIP also contains the committed scenario and actual uninstall/reinstall scripts.
