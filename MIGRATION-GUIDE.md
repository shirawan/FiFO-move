# Move original company documents — test candidate 19.0.0.4.0

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
19.0.0.4.0; the financial dependency is 19.0.3.1.1.

## The workflow

1. Enable every old company/branch and the new company in the company switcher. Open
   **Original document moves**, select the companies and cutover date, then choose
   accounting history, purchase orders, or both.
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

Purchase orders are processed in steps after the accounting coverage is present. Every step
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

Missing destination history is still a blocker. Replaying only old receipts could overstate
inventory when the same goods were already sold, returned, counted or opened in the new
company. Full reconstruction requires the complete movement/valuation history and explicit
reconciliation of existing destination stock/openings. This version does not invent receipts.
Manual receipt valuation, valuation journals, pending supplier returns and nonstandard
multi-step pending routes still require additional reviewed history adapters.

## Removal and recovery

Every successful step stores private native audit evidence, settings, explicit matches and
separate-transaction reasons, independently of custom module tables. Uninstall through
Odoo before deleting the addon files. Keep Accounting, Purchase and Inventory installed.
Download the latest receipt and keep a database/filestore backup.

Reinstall automatically restores supported saved completed and partial moves with all their
choices. Partial moves can resume from the next review. **Recover saved document moves**
retries recovery from the menu. A damaged receipt is reported and isolated; healthy moves
still recover. Changed/deleted native evidence is never silently accepted or copied again.

Automatic choice recovery applies to receipts written by 19.0.0.4.0. Older receipts retain
native duplicate markers and their original audit, but do not contain every setting needed
to reconstruct their screens automatically. Restore their explicit choices manually.

## Checks before using your real database

The delivered evidence includes native regressions, a committed A+B+C simulation into an
already-used destination, original invoice adoption, saved steps, and actual addon removal /
reinstall with automatic completed/partial recovery. Desktop and phone Chromium tests run.

Your restored database is still required to establish earlier-transfer overlap, missing stock
history, custom/localized behavior and real-size lock/request timing. No production database
or real payments/integrations were accessed. The original supplied stock addon remains
unchanged in the installer and was not executed.
