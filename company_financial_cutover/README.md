# Company Financial Cutover

Move financial opening balances and unpaid customer/vendor items from an old
company into a clean replacement company in the same Odoo 19 database. Source
accounting history stays in the old company. No old tax, journal or company
configuration is copied into the replacement.

## What moves

- All purchase orders from the old company at preview time, including completed, cancelled and historical orders, copied as read-only purchase history in the new company.

- Posted cash, bank, asset, liability and equity balances through the cutover date.
- Current fiscal year's revenue and expense balances. Earlier years' net
  profit/loss is carried to a selected destination retained-earnings account.
- Individual unpaid receivable/payable journal items, including partially paid
  invoices/bills, credit notes and unapplied payments. Amounts are reconstructed
  by accounting date, so an invoice settled after cutover remains open at cutover.
- Original journal/invoice and vendor references, source accounting dates, due
  dates, partners and foreign-currency amounts. Zero-net control accounts retain
  their separate unpaid debit and credit items.

The addon posts one balanced Miscellaneous opening entry. Unpaid items are
reconcilable journal items, **not recreated invoices**; settle them using native
customer/vendor payments and reconciliation in the destination. Historical
invoices, payments, bank statements and tax reports remain in the source.
Tax control-account balances move without duplicating tax tags or taxable invoices.

## Setup and guided steps

1. Install this addon from your custom addons directory. In Apps, update the Apps
   List and search for **Company Financial Cutover** with the Apps filter removed. Odoo installs its `account` and `purchase` dependencies.
2. Configure the replacement company's chart, journals and fiscal year correctly.
   Both companies must use the same accounting currency and fiscal year start.
   The destination must have **no draft or posted accounting entries, and no active payments**. Take a verified
   database backup and use a restored test copy before the actual cutover.
3. Sign in as a Settings administrator with Accounting administrator access and
   enable both companies. Open **Accounting/Invoicing → Configuration → Move
   Financial Opening**. Choose the old company, new company and date. The existing MISC journal is preferred; otherwise a single suitable general journal is selected. Cash-basis and exchange-difference journals are excluded. Retained earnings are selected only when there is one suitable account. Ask your accountant to complete **Accounting setup** when the chart has multiple earnings accounts.
4. Click **1. Check existing data**. Matching uses account codes and types
   against the destination's existing chart. Select accounts manually where the
   codes differ. Missing or incompatible account choices block posting; accounts
   are not silently created or source settings copied. The same shared contact record is reused. Different contact records are auto-matched only by a compatible tax ID or reference. Names help find possible matches but always require an explicit choice. Archived matches are included and need reactivation or review. A minimal destination contact is proposed only when no possible match exists. Proposed
   new contacts copy identity/address only, without old fiscal positions, bank
   accounts, payment terms or accounting properties.
5. If using the separate stock mover, review every source inventory valuation
   account and mark **Handled by Stock Cutover**. Known company/category stock
   valuation accounts are marked automatically when those fields are available.
   Select the destination stock clearing account; **Connect the stock move** creates/reuses the supplied stock addon's saved clearing account when
   that addon is installed. Otherwise configure a Current Assets clearing account
   and explicitly use that same account in the stock mover. A mismatch with the stock mover’s saved destination clearing account blocks the financial opening. Inventory is excluded
   from this financial entry and carried through clearing instead.
6. Read **Check results** and resolve any needs-attention message. Run Check existing data again after editing choices. Click **2. Review amounts**. This creates review/audit rows only, without
   destination contacts or journals. Reconcile the trial balance, retained
   earnings, stock exclusions and each unpaid item with the accountant. Rebuild
   Preview after any configuration, mapping or accounting changes.
7. Finish bank reconciliation in the old company on or before the cutover date. Unsettled outstanding receipts/payments, suspense and interbank-transfer items block the move; these items are not carried individually. Then pause writes, scheduled jobs, queue workers and other users across this database during the cutover window. Ask your Odoo administrator to stop cron workers, including cron workers in other Odoo processes; `--max-cron-threads=0` applies only to the process it starts.
   Click **3. Move balances**. Busy accounting tables, changed
   previews, wrong posting dates or reconciliation differences refuse/roll back
   the operation. Review **View completed move** and **Amounts to move**.
   Run the stock cutover afterwards on the same agreed date and with the same
   destination clearing account. Reconcile its inventory/clearing entries before
   starting normal operations in the replacement.

### Purchase orders

Leave **Include all purchase orders** enabled to include every source purchase
order in the financial preview and approval. These snapshots preserve order and
vendor references, status, dates, currency, totals, original terms, products,
quantities, prices, discounts, original tax labels and bill references. The
**Purchase orders** tab shows what will be copied. Copies appear under
**Purchase → Migrated purchase history** after approval. Purchase user access is
required to include orders; the financial operator still needs Settings and
Accounting administrator access.

History quantities are captured at preview/approval time, rather than being
reconstructed at the financial cutover date. Completed orders stay read-only
history and never create another order, receipt or vendor bill. Vendor unpaid
amounts already covered by financial opening are not billed again. Purchase
history uses the new company's access rules, so its purchase users can read it
without access to the old company's live orders. Source chatter, attachments,
receipt documents and purchase analytics are not copied into native Purchase
reports; original records remain in the old company.

For an order with **no received or billed quantities**, open its history and use
**Prepare or view replacement draft**. This action reuses eligible existing
vendor/product records, uses the new company's tax configuration, and creates
one unconfirmed RFQ. Clicking again opens the same draft. Review its taxes,
prices, units, expected arrival and company settings before confirming. Cancel
the original order before confirming the replacement; confirmation is blocked
until the original is cancelled, and the old order cannot be reconfirmed while
a replacement exists. The migrated replacement cannot be duplicated.

Partially received/billed orders, down payments, cancelled orders, dropshipping
and mismatched or ambiguous vendor/product identities require manual purchase
manager review. Their full history and remaining quantities are still included;
no operational draft is created automatically for these cases. Only shared
products or existing company-specific products with a unique reference/barcode
and matching unit are eligible for draft preparation. This action does not
create or configure products. Move stock first if it must create destination
products before preparing purchase drafts.

If the financial opening was already completed with an older version, use
**1. Preview missing purchase orders** on the completed cutover, review the
Purchase orders tab, then **2. Copy reviewed purchase history**. This copies
only missing orders and never reposts the financial opening. Repeating it skips
existing history. Changed purchase previews block copying.

Limits: 10,000 purchase orders and 50,000 order lines per cutover.

### Stock example

The source has bank 50, inventory 100 and equity -150. The financial opening
posts bank +50, stock clearing +100 and equity -150, excluding inventory.
The stock mover then posts inventory +100 and stock clearing -100. The new
company ends with bank 50, inventory 100, equity -150 and clearing zero.

## Boundaries

- Choose standalone companies. Companies or branches in a branch hierarchy need a separately reviewed cutover, so branch balances are never silently omitted.

- This transfers opening balances and open items, rather than repairing source
  ledger errors or migrating full accounting history. Source posted entries must
  balance. Existing source/target configuration errors and ambiguous mappings
  need review before posting. The destination is a new ledger, not a merger into
  an already active ledger.
- Concurrent commits with assigned transaction IDs anywhere in the database before lock acquisition trigger a full Odoo request retry with a fresh snapshot using `ConcurrencyError`. This is deliberately database-wide, including unrelated jobs. PostgreSQL automatic maintenance can also trigger this guard. Odoo retries at most five times; continued background writes can exhaust retries. Pause scheduled jobs and other writes across the database, rather than relying on automatic retry to create a maintenance window.
- Draft entries, unposted payments, archived contacts and contacts added after preview are checked before posting. A source completion marker also prevents repeating a move to a different replacement company or after reinstalling the addon. Possible duplicates among proposed new contacts need manual resolution.
- Rebuild the preview after changing source entries, reconciliations, contacts,
  account choices, currency precision or destination configuration. Completed
  cutovers cannot repeat or be edited, deleted, or reset to draft. Post corrections
  as separate accountant-reviewed entries. Future native payment reconciliation
  of the opening items remains available.
- Do not settle carried items in the old company after cutover. Older history
  remains there for audit, but later source operations are not synchronized into
  the replacement.
- Unpaid invoices with cash-basis taxes are blocked: future cash-basis tax
  recognition needs a separate accountant-reviewed migration. The addon does not
  copy tax-report history, statutory/e-invoicing documents, asset/depreciation or
  deferred-recognition schedules, analytic history, bank statement history,
  recurring transactions, payment mandates, or company-specific settings.
- Limits: 100,000 source posted journal items and 10,000 destination opening
  items. Larger migrations need separate sizing/review. The source company's
  financial cutover is one-time; do not split it into overlapping batches.

## Validation

Run `bash tools/test_financial.sh` from the repository root. The suite exercises
native invoices and bills, partial/later payments, credit balances, foreign
currencies, retained earnings, inventory clearing, contact/account mapping,
changed-preview checks, rollback, access control, forms, audit protection and
payment reconciliation after cutover. The suite also exercises the real Odoo `retrying()` loop, rollback between attempts, native outstanding payments and bank-statement reconciliation, MISC selection on a multi-journal chart, clearing-account mismatches and downstream create hooks. These tests use Odoo 19 Community's native
`account` module; the Enterprise stock addon integration requires its separate
dependencies and testing on a restored copy of your database.

The development test runner temporarily pauses automatic vacuum only in its
verified local PostgreSQL cluster and restores the original setting on exit.
This keeps long-lived test fixture transactions quiet while preserving the
production concurrency check and the real retry-loop regression.
