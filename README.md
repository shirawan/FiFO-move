# FiFO-move

Odoo 19 addons for moving an existing business into a clean replacement company
in the same database.

- `company_stock_fifo_migration`: Company Stock Cutover 19.0.2.7.7, based on the supplied
  19.0.2.7.3 addon, with a clearer existing-stock preview. Requires Odoo Enterprise `stock_accountant`,
  `product_expiry`, and the separate `company_kit_bom_migration` addon.
- `company_financial_cutover` 19.0.2.0.1: financial opening balances and unpaid
  customer/vendor journal items, plus all purchase orders as read-only destination
  history with explicit draft preparation for eligible unfinished orders. Requires
  Odoo 19 `account` and `purchase`. It uses the new
  company's account configuration and retains the old company's history.

**Move Company Data** has one **Move** selection: financial balances and unpaid
items, purchase orders as read-only history, both, or stock. The screen shows
only the setup for the chosen option. Purchase-only moves work with an existing
destination ledger and create no accounting entries. Choosing stock opens its
separate wizard when installed; sales and other historical documents stay in
the old company.

For old Company A with branches B and C, select A and **Include old branches**.
Their accounting and purchase history are combined into the standalone destination;
unpaid items and purchase histories retain their original company. Archived
branches are included and every included company must be selected in the switcher.

If the new company already has a mixture of activity, choose **New company is
already in use**. Review earlier imported openings and explicitly match any copied
invoices/bills with your accountant. The preview shows balances already there,
the carried opening, adjustments and resulting balances. New trading stays in
place. Selected earlier openings receive posted reversals, with existing payment
matches preserved. The mover cannot guess which transactions were previously
transferred. Copied source invoices with payments at the balance date and
foreign-currency earlier openings need a separate reviewed adjustment.

Completed native accounting entries and prepared purchase orders remain after
uninstalling the financial addon. Private native attachments preserve completed
reports and recovery archives; reinstalling restores financial audit rows and
purchase history without reposting or creating orders again. Download the report
before uninstalling. Unfinished previews must be rebuilt. Stock completed audit
data is also archived, but its wizard screens are not automatically restored.

See [financial cutover instructions](company_financial_cutover/README.md) and
[stock cutover instructions](company_stock_fifo_migration/README.md).

If using both movers, **run the financial opening first**. Exclude source inventory
asset accounts handled by the stock mover and use the same destination Stock
Migration Clearing account in both. The subsequent stock opening offsets that
clearing balance, preventing inventory from being opened twice.

## Development

`tools/setup_cloud.sh` prepares a pinned Odoo 19 Community checkout and a local
PostgreSQL 17 development cluster outside this repository. Python 3.12, `uv`,
GCC, Debian package tools and Debian's archive keyring are required.

```bash
cd /workspace/FiFO-move
bash tools/setup_cloud.sh
bash tools/test_financial.sh
bash tools/test_retention.sh
bash tools/start_cloud.sh
```

The test runner installs/upgrades only the financial addon in a disposable local
development database and exercises native Odoo accounting, forms and permissions.
It does not connect to a production database. The stock addon requires its
Enterprise and separate Kit BoM dependencies to run its existing tests. Stock
preview presentation can be checked separately with the runtime Python using
`python -m unittest discover -s tests -v` (requires `lxml`).

The retention test creates a separate local database, completes financial and
purchase-only moves, including A with branches B/C into an already-used standalone
company with an earlier opening, existing payment, copied invoice and new activity.
It runs native Odoo uninstall, removes only its isolated addon
symlink, then verifies native data and archives before reinstalling. It checks
that recovery and repeat attempts create no duplicates. It keeps its test
database and logs outside the repository for inspection.

The cloud task is already isolated; use this checkout without creating a Git
worktree. Runtime dependencies, data and logs live under `/workspace/.fifo-env`
and `/workspace/odoo-19`, outside the Git repository. Live services need restarting
after environment restoration.
