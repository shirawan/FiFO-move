# Company Financial Cutover

Move financial opening balances and unpaid customer/vendor items from an old
company into a clean replacement company in the same Odoo 19 database. Source
accounting history stays in the old company. No old tax, journal or company
configuration is copied into the replacement.

## What moves

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
   List and search for **Company Financial Cutover** with the Apps filter removed.
2. Configure the replacement company's chart, journals and fiscal year correctly.
   Both companies must use the same accounting currency and fiscal year start.
   The destination must have **no draft or posted accounting entries, and no active payments**. Take a verified
   database backup and use a restored test copy before the actual cutover.
3. Sign in as a Settings administrator with Accounting administrator access and
   enable both companies. Open **Accounting/Invoicing → Configuration → Move
   Financial Opening**. Choose the old company, new company and date. Existing journal and retained-earnings settings are selected automatically only when a single suitable choice exists. Otherwise, ask your accountant to complete **Accounting setup**.
4. Click **1. Check existing data**. Matching uses account codes and types
   against the destination's existing chart. Select accounts manually where the
   codes differ. Missing or incompatible account choices block posting; accounts
   are not silently created or source settings copied. Shared contacts are reused;
   company-owned contacts are checked using tax IDs, references and names with normalized case/spacing. Archived matches are included and need reactivation or review. A minimal destination contact is proposed only when no possible match exists. Ambiguous contacts require an explicit choice. Proposed
   new contacts copy identity/address only, without old fiscal positions, bank
   accounts, payment terms or accounting properties.
5. If using the separate stock mover, review every source inventory valuation
   account and mark **Handled by Stock Cutover**. Known company/category stock
   valuation accounts are marked automatically when those fields are available.
   Select the destination stock clearing account; **Connect the stock move** creates/reuses the supplied stock addon's saved clearing account when
   that addon is installed. Otherwise configure a Current Assets clearing account
   and explicitly use that same account in the stock mover. Inventory is excluded
   from this financial entry and carried through clearing instead.
6. Read **Check results** and resolve any needs-attention message. Run Check existing data again after editing choices. Click **2. Review amounts**. This creates review/audit rows only, without
   destination contacts or journals. Reconcile the trial balance, retained
   earnings, stock exclusions and each unpaid item with the accountant. Rebuild
   Preview after any configuration, mapping or accounting changes.
7. Stop accounting activity in both companies during the agreed cutover window.
   Click **3. Move balances**. Busy accounting tables, changed
   previews, wrong posting dates or reconciliation differences refuse/roll back
   the operation. Review **View completed move** and **Amounts to move**.
   Run the stock cutover afterwards on the same agreed date and with the same
   destination clearing account. Reconcile its inventory/clearing entries before
   starting normal operations in the replacement.

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
- Concurrent commits before lock acquisition trigger a full Odoo request retry with a fresh database snapshot. Keep accounting activity paused during the move.
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
payment reconciliation after cutover. These tests use Odoo 19 Community's native
`account` module; the Enterprise stock addon integration requires its separate
dependencies and testing on a restored copy of your database.
