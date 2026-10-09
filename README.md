# FiFO-move

Odoo 19 addons for moving an existing business into a clean replacement company
in the same database.

- `company_stock_fifo_migration`: Company Stock Cutover 19.0.2.7.7, based on the supplied
  19.0.2.7.3 addon, with a clearer existing-stock preview. Requires Odoo Enterprise `stock_accountant`,
  `product_expiry`, and the separate `company_kit_bom_migration` addon.
- `company_financial_cutover` 19.0.3.0.0: opening balances and unpaid customer/vendor
  items. Requires Odoo 19 `account`; it does not install Purchase.
- `company_purchase_cutover` 19.0.3.0.0: optional purchase history and replacement
  drafts. Requires the financial addon and Odoo's Purchase app. Install this addon
  explicitly when you want purchase migration. Upgrading the previously combined
  addon preserves its existing purchase feature by installing the new addon.

New moves default to **Financial balances and unpaid items**. Purchase migration
copies all selected-company orders, including completed and cancelled orders,
only when you explicitly select Purchase orders or Both. These are saved history;
operational replacement orders require a separate manager action.

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
bash tools/test_financial_only.sh
bash tools/test_financial.sh
bash tools/test_retention.sh
bash tools/start_cloud.sh
```

The combined test runner installs/upgrades the financial and optional purchase addons in a disposable local
development database and exercises native Odoo accounting, forms and permissions.
It does not connect to a production database. The stock addon requires its
Enterprise and separate Kit BoM dependencies to run its existing tests. Stock
preview presentation can be checked separately with the runtime Python using
`python -m unittest discover -s tests -v` (requires `lxml`).

The retention test creates a separate local database, completes financial and
purchase-only moves, including A with branches B/C into an already-used standalone
company with an earlier opening, existing payment, copied invoice and new activity.
It runs native Odoo uninstall, removes only its isolated addon
symlinks, then verifies native data and archives before reinstalling. It checks
that recovery and repeat attempts create no duplicates. It keeps its test
database and logs outside the repository for inspection.

The cloud task is already isolated; use this checkout without creating a Git
worktree. Runtime dependencies, data and logs live under `/workspace/.fifo-env`
and `/workspace/odoo-19`, outside the Git repository. Live services need restarting
after environment restoration.

## Upgrade and recovery

See [INSTALL.txt](INSTALL.txt) for fresh installs and upgrades. Copy both financial
and purchase addon folders before upgrading the old combined release; its migration
transfers model, field, constraint, view and permission ownership before cleanup.
Completed entries, orders and archive bytes are retained. A fresh financial-only
installation needs just the financial folder.

Purchase managers can choose destination vendors and prepare eligible replacement
orders after go-live without Settings or Accounting administrator rights. These
per-order actions lock only that history and its original/replacement orders and
save a small private per-order follow-up. A native vendor-reference reservation also prevents concurrent histories from preparing the same purchase twice. The completed financial archive and
report are not rewritten. Select the order's old company and its new company.

A damaged archive no longer stops the whole reinstall. Healthy moves recover;
Settings administrators can find blocked archives under Accounting → Company move
recovery. Restore missing files/records or original financial evidence from a
verified backup, then use Retry recovery. Financial evidence checks and persistent
duplicate markers still apply; recovery never reposts entries or creates orders.

The full financial/purchase cutover retains table locks and a freshness scan.
Benchmark on a restored copy of your own database and pause writes for the move
window. `tools/time_cutover_check.py` measures the full lock/check in an isolated
local `fifo_*` database through Odoo shell, using `FIFO_TIMING_BATCH_ID`; it always
rolls back. Its five-attempt estimate excludes planning and posting. Day-to-day
replacement actions do not run this scan.

`tools/test_upgrade.sh` accepts `FIFO_LEGACY_BUNDLE` pointing to the published
19.0.2.0.1 ZIP and verifies a real upgrade. `tools/test_retention.sh` tests optional
purchase-only removal, complete removal, native concurrency, recovery, damaged
archive isolation and duplicate prevention. Evidence stays outside the repository.

`python tools/build_bundle.py /workspace/artifacts` builds bundles from a clean
committed checkout. The full bundle includes all addon folders, top-level README,
INSTALL.txt, tests and tools; runtime databases, credentials and caches are excluded.
