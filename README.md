# FiFO-move

Odoo 19 addons for moving an existing business into a clean replacement company
in the same database.

- `company_stock_fifo_migration`: the supplied Company Stock Cutover 19.0.2.7.3,
  imported without changes. Requires Odoo Enterprise `stock_accountant`,
  `product_expiry`, and the separate `company_kit_bom_migration` addon.
- `company_financial_cutover`: financial opening balances and unpaid
  customer/vendor journal items. Requires Odoo 19 `account` only. It uses the new
  company's account configuration and retains the old company's history.

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
bash tools/start_cloud.sh
```

The test runner installs/upgrades only the financial addon in a disposable local
development database and exercises native Odoo accounting, forms and permissions.
It does not connect to a production database. The stock addon requires its
Enterprise and separate Kit BoM dependencies to run its existing tests.

The cloud task is already isolated; use this checkout without creating a Git
worktree. Runtime dependencies, data and logs live under `/workspace/.fifo-env`
and `/workspace/odoo-19`, outside the Git repository. Live services need restarting
after environment restoration.
