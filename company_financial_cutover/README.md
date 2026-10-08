# Company Financial Cutover

Move financial opening balances and unpaid customer/vendor items from an old
company into a clean replacement company in the same Odoo 19 database. Source
accounting history stays in the old company. No old tax, journal or company
configuration is copied into the replacement. The **Move** dropdown offers
financial balances and unpaid items, purchase orders as read-only history,
both, or stock. Setup fields adapt to the selection. Stock opens the separate
warehouse mover; install its dependencies to enable that action.
Purchase-only moves do not require an empty destination ledger, matching fiscal
years, or financial account choices, and never create an accounting entry.

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

When financial balances are selected, the addon posts one balanced Miscellaneous opening entry. Unpaid items are
reconcilable journal items, **not recreated invoices**; settle them using native
customer/vendor payments and reconciliation in the destination. Historical
invoices, payments, bank statements and tax reports remain in the source.
Tax control-account balances move without duplicating tax tags or taxable invoices.

## Setup and guided steps

The screen highlights the next step: **Check existing data → Review selected data →
Move selected data** (or **Copy purchase history** for purchase-only moves). Messages explain what needs attention and who can help. Reviewed
amounts appear directly on the page; account and contact choices stay under **Review matches** and **Accountant setup**. A completed move shows its result and the
next stock step.

1. Install this addon from your custom addons directory. In Apps, update the Apps
   List and search for **Company Financial Cutover** with the Apps filter removed. Odoo installs its `account` and `purchase` dependencies.
2. For financial balances, configure the replacement company's chart, journals and fiscal year correctly.
   Both companies must use the same accounting currency and fiscal year start.
   Choose whether the replacement has no accounting yet or is already in use.
   In fresh-company mode, it must have **no draft or posted accounting entries,
   and no active payments**. For an existing company, review earlier transfers
   as described below. Take a verified
   database backup and use a restored test copy before the actual cutover.
3. Sign in as a Settings administrator with Accounting administrator access and
   enable both companies. Open **Accounting/Invoicing → Configuration → Move
   Company Data**. Choose what to move and the old and new companies. For financial balances, enter the date. The existing MISC journal is preferred; otherwise a single suitable general journal is selected. Cash-basis and exchange-difference journals are excluded. Retained earnings are selected only when there is one suitable account. An earnings choice is needed only when earlier years have a nonzero net profit/loss; multiple earnings accounts do not block a move that does not use one. Financial setup tabs are hidden for purchase-only moves.
4. Click **1. Check existing data**. Matching uses account codes and types
   against the destination's existing chart. Select accounts manually where the
   codes differ. Missing or incompatible account choices block posting; accounts
   are not silently created or source settings copied. The same shared contact record is reused. Different contact records are auto-matched only by a compatible tax ID or reference. Names help find possible matches but always require an explicit choice. Archived matches are included and need reactivation or review. A minimal destination contact is proposed only when no possible match exists. Proposed
   new contacts copy identity/address only, without old fiscal positions, bank
   accounts, payment terms or accounting properties.
   Purchase-only moves also show existing vendor choices. History can be copied
   without resolving every vendor; choose the correct destination vendor before
   cancelling an original order for a replacement draft.
5. If using the separate stock mover, review every source inventory valuation
   account and mark **Handled by Stock Cutover**. Known company/category stock
   valuation accounts are marked automatically when those fields are available.
   Select the destination stock clearing account; **Connect the stock move** creates/reuses the supplied stock addon's saved clearing account when
   that addon is installed. Otherwise configure a Current Assets clearing account
   and explicitly use that same account in the stock mover. A mismatch with the stock mover’s saved destination clearing account blocks the financial opening. Inventory is excluded
   from this financial entry and carried through clearing instead.
6. Read **Check results**. Independent setup, account and contact problems are listed together; resolve the blocking items. Review notices do not require source cleanup and stay visible during approval. Run Check existing data again after editing choices. Click **2. Review selected data**. This creates review/audit rows only, without
   destination contacts or journals. Reconcile the trial balance, retained
   earnings, stock exclusions and each unpaid item with the accountant. Rebuild
   Preview after any configuration, mapping or accounting changes.
7. If moving financial balances, finish bank reconciliation in the old company on or before the cutover date. Unsettled outstanding receipts/payments, suspense and interbank-transfer items block the financial move; these items are not carried individually. Pause writes, scheduled jobs, queue workers and other users across this database during the move window. Ask your Odoo administrator to stop cron workers, including cron workers in other Odoo processes; `--max-cron-threads=0` applies only to the process it starts.
   Click **3. Move selected data**, or **3. Copy purchase history** for purchase-only moves. Busy tables, changed
   previews, wrong posting dates or reconciliation differences refuse/roll back
   the operation. Review the completed data and, if financial balances were selected, **View opening entry**.
   Run the stock cutover afterwards on the same agreed date and with the same
   destination clearing account. Reconcile its inventory/clearing entries before
   starting normal operations in the replacement.

### Purchase orders

Select **Purchase orders (read-only history)** or **Financial balances and
purchase orders** in **Move** to include every source purchase order in the
review and approval. These snapshots preserve order and
vendor references, status, dates, currency, totals, original terms, products,
quantities, prices, discounts, original tax labels and bill references. The
**Review selected data** section shows what will be copied. After completion,
the **Purchase orders** tab shows saved history. Copies also appear under
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

For an order with **no received or billed quantities**, review its destination
vendor in **Review matches**, or open saved history and use **Choose destination vendor**.
An explicit choice can resolve a name-only match, including older histories
without saved contact mappings. Select an existing active destination/shared
vendor; no contact or purchase order is created by this choice. Then cancel the original order
in the old company and use **Prepare replacement draft**. The saved history
screen shows one primary next action: choose a vendor, open the original order
for cancellation, prepare a draft, or view the already linked replacement.
Opening the original uses a normal Odoo order window; this addon does not
cancel it automatically. Close that window to return to refreshed guidance.
Changed or unavailable originals are directed to purchase manager review.
The vendor chooser shows the selected contact's reference, tax ID and email.
Cancellation is required before creating the draft, so uninstalling cannot leave
both orders active. This action reuses eligible existing
vendor/product records, applies the destination vendor's fiscal position to the
new company's purchase taxes using Odoo's native line defaults, and creates
one unconfirmed RFQ. After a draft is prepared, **View replacement order** opens that same order. Review its taxes,
prices, units, expected arrival and company settings before confirming. While
this addon is installed, confirmation, approval and writes to the confirmed
state check the original remains cancelled. Both orders are locked during approval to prevent concurrent confirmation,
and the old order cannot be reconfirmed while a replacement exists. The migrated
replacement cannot be duplicated through this addon.

Partially received/billed orders, down payments, cancelled orders, dropshipping
and mismatched or ambiguous vendor/product identities require manual purchase
manager review. Their full history and remaining quantities are still included;
no operational draft is created automatically for these cases. Only shared
products or existing company-specific products with a unique reference/barcode
and matching unit are eligible for draft preparation. This action does not
create or configure products. Move stock first if it must create destination
products before preparing purchase drafts.

If the financial opening was already completed with an older version, use
**Review missing purchase orders** on the completed cutover, review the
Purchase orders tab, then **Copy reviewed purchase history**. This copies
only missing orders and never reposts the financial opening. Repeating it skips
existing history. Changed purchase previews block copying.

Limits: 10,000 purchase orders and 50,000 order lines per cutover.

Purchase history shows readable order statuses and **What happens next** for
each order. Cancelled or already processed orders explain why a replacement
cannot be prepared automatically. Preview totals show each order's currency.

### Stock example

The source has bank 50, inventory 100 and equity -150. The financial opening
posts bank +50, stock clearing +100 and equity -150, excluding inventory.
The stock mover then posts inventory +100 and stock clearing -100. The new
company ends with bank 50, inventory 100, equity -150 and clearing zero.

## Practical checks

The mover preserves the old company's recorded data. It does not require fixing
suspected duplicate bills or orders before copying balances/history. Zero-balance
ledger accounts and fully settled contacts need no destination choice. An unused
earnings or stock-clearing setting does not block the opening; these accounts
are validated only when their amounts actually appear in it.

Essential checks still prevent unsafe posting: unreviewed existing destination accounting,
invalid account/contact choices for amounts being moved, unbalanced entries,
unsettled bank-matching items, unsupported cash-basis tax obligations, changed
previews, and repeated financial moves. Replacement preparation collects known
vendor, original-order, duplicate and product issues into one message before
creating anything. The history screen also shows known product/duplicate issues
before the original is cancelled. Configuration or data changed later may require
a refreshed check, and native Odoo or other installed modules may reject posting.

## Duplicates and uninstalling

Already copied source purchase IDs are skipped. Native completion markers survive
uninstalling and block copying the same history again, including to another
company. If a marker exists but its history is missing, the mover stops and asks
for archive recovery rather than creating another copy. Existing destination
purchase references also block preparation of a conflicting replacement draft.

Possible repeated vendor bills with the same vendor, normalized reference,
currency, invoice date and total appear as **review notices**, not cleanup
requirements. Each recorded unpaid bill stays a separate opening item at its
actual ledger balance. No source bill is merged, removed, reversed or recreated.
Possible repeated active, unreceived and unbilled purchase orders also appear as
review notices. Each new source ID is copied once as its own history, including
when a new order resembles an earlier copy. Copying history creates no active
order, receipt or bill.

Before creating an active replacement, the purchase manager must resolve any
matching active originals and review existing active destination orders. Cancelled
destination orders do not block a new draft. Fully reversed bills and cancelled
source purchase orders stay as historical records. Records without references
cannot be reliably identified as suspected duplicates. These checks do not
merge or correct existing data.

Completed moves save a readable report and full financial/purchase recovery
archive as private native Odoo attachments, with native hash manifests and
purchase markers. Saving must succeed before the move commits. Upgrade and
uninstall hooks also archive completed older batches. Native opening entries,
their unpaid journal items and prepared native purchase orders remain after
uninstalling. Custom audit/history tables are removed by Odoo, but reinstalling
restores their completed rows and native record links from the saved archives.
Recovery never reposts the opening or creates replacement orders again.
Recovery checks the opening's accounting identity, date, journal, accounts,
partners, currencies and amounts against its saved evidence. Editing and
reposting the native opening while the addon is uninstalled stops recovery for
accountant review. Normal payment reconciliation remains valid. Older archives
without a full native signature are checked against their saved financial rows.
Explicit destination vendor choices also survive uninstall/reinstall.

Use **Download completed report** before uninstalling. Settings administrators
can also find the private recovery JSON and report under Technical → Attachments;
they are not visible to ordinary purchase users because financial details are
included. Keep the database and filestore together in backups. Do not delete or
change the archives or completion markers. Missing or changed recovery data
stops restoration for administrator review.

Only completed moves are archived; unfinished previews and unsaved choices must
be rebuilt. Uninstall this addon through Odoo before removing its files. Custom
edit/confirmation guards run only while the addon is installed; afterwards
native Odoo permissions and workflows apply. Uninstall is refused if an original
purchase has been reopened while an active replacement exists. Financial and
purchase archives restore automatically on reinstall; stock audit archives
remain readable files, with no automatic stock-wizard reconstruction.

## Old company with branches; new company already in use

Select the old parent company and **Include old branches**. Select the standalone
new company. Choose **New company is already in use** when its books already
contain activity. Existing destination data alone is not a reason to clean it up.

Under **Existing data**, your accountant identifies all previously transferred
amounts before confirming **Accountant has reviewed existing data**:

- **Old opening entries already entered here**: select only posted general-journal
  entries that imported the old balances. Preview shows their opposite amounts.
  On confirmation, native Odoo posts reversals at the balance date and carries the
  complete source opening. Originals and new trading remain posted. Existing
  customer/vendor payment matches are preserved; remaining reversal items offset
  the carried opening. For example, an earlier 100 opening already paid by 40
  becomes a carried unpaid amount of 60, rather than another 100 to collect.
- **Invoices and bills already copied here**: explicitly pair each old document
  with its existing destination copy. The mover verifies type, dates, currency,
  total, mapped accounts, contacts and both company/foreign amounts. The balanced
  source document is excluded from the opening; its native destination copy stays
  in place. Name/reference or amount similarity never automatically skips it.
- Everything else must be separate new destination activity. Similar invoice/bill
  references are review notices; the scan is advisory and can miss manual imports
  with changed references. Checking the complete ledger is necessary when the
  history is unknown. Confirming a checkbox cannot establish that data is new.

The review shows **Already here → Carried opening → Earlier opening adjustment →
After this move** for each affected account, using posted balances at the balance
date. Later transactions remain in place outside those totals. Changes to these
balances or selected records invalidate approval and require a refreshed preview.
All detected requirements appear in one check report; suspected duplicate source
bills/orders are notices rather than a forced cleanup exercise.

Earlier opening adjustments support the accounting currency. Foreign-currency
previous openings and copied source invoices already paid at the balance date
need a separate accountant-reviewed adjustment: copying/skipping an invoice alone
would lose the effect of its old payment. Payment, tax and already-reversed entries
cannot be selected as earlier openings. Books locked at the balance date need an
accountant-approved posting date. Unsettled bank-matching items and unsupported
cash-basis tax obligations retain their safeguards.

Posted reversals, reviewed choices, exact completed branch scope, before/after
balances and original branch purchase identifiers are included in the durable
archive. Uninstall preserves native entries and archives; reinstall validates
native openings/adjustments and restores links without reposting them.

## Boundaries

- The destination must be standalone. Select **Include old branches** to combine
  the source parent and all descendants, including archived branches. Enable every
  included company in the switcher. Branch accounting currencies and financial
  year boundaries must agree. The preview lists included companies, retains the
  branch on unpaid items and purchase histories, and snapshots the scope. Each
  included company receives a durable completion marker so later overlapping
  parent/branch cutovers cannot repeat. Intercompany eliminations are not guessed.

- This transfers opening balances and open items, rather than repairing source
  ledger errors or migrating full accounting history. Source posted entries must
  balance. Existing source/target configuration errors and ambiguous mappings
  need review before posting. **New company has no accounting yet** keeps the
  fresh-ledger safeguard. **New company is already in use** allows posted entries,
  drafts and payments; it requires an explicit review of earlier transfers.
- Committed inserts, updates or deletes in the locked accounting/configuration and selected purchase tables before lock acquisition trigger a full Odoo request retry with a fresh snapshot using `ConcurrencyError`. The guard compares actual tuple versions in this database, including deleted rows, and ignores the current request's own uncommitted changes. Commits in other PostgreSQL databases, transactions without changes to these tables, and ordinary automatic vacuum no longer trigger retries. These locks still cover the selected tables across companies in this database. Odoo retries at most five times; pause accounting and purchase writes during the move window.
- In fresh-ledger mode, destination draft entries and unposted payments block posting. Archived contacts and contacts added after preview are checked before posting. A source completion marker also prevents repeating a move to a different replacement company or after reinstalling the addon. Possible duplicates among proposed new contacts need manual resolution.
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

Run `bash tools/test_retention.sh` for a real isolated uninstall, code removal
and reinstall, checking unchanged native data, archive bytes and markers,
restored completed history, and prevention of duplicate recovery/copying.
It also exercises concurrent native purchase approvals and a cloned disposable
database where a native opening is changed while the addon is absent; reinstall
must refuse that changed opening. No production data is used.

The development test runner temporarily pauses automatic vacuum only in its
verified local PostgreSQL cluster and restores the original setting on exit.
This keeps long-lived test fixture transactions quiet while preserving the
production concurrency check and the real retry-loop regression.
