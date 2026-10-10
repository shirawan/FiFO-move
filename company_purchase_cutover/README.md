# Company Purchase Cutover — Odoo 19

Optional purchase migration for Company Financial Cutover. Install this addon
explicitly; it requires Odoo's Purchase app. The financial addon alone does not.

Purchase managers can use **Purchase → Configuration → Move purchase history** for history-only moves. Financial administrators can choose Purchase orders or Both under Accounting → Configuration → Move Company Data. Use **Review my move**, then **Confirm history copy** (or **Confirm move** for combined moves). The full history copy still requires a maintenance window.
This copies every source-company order at preview time, including completed and
cancelled orders, as saved read-only history. Existing copied source IDs are skipped.
Receipts, bills and active orders are not created by copying history.

After go-live, Purchase managers use **Purchase → Purchase orders to continue** to
choose the destination vendor, cancel the original in native Purchase, and prepare
an eligible replacement draft. Select that original's company and the destination
company. Read the quantities and verify vendor, products, prices and taxes before
confirming. **Update order status** refreshes the history after cancelling the original in native Purchase. Cancelled orders, orders without product lines and fully received-and-billed orders show **History only — no action needed** and are excluded from the default work queue. Partially received/billed, dropship and down-payment orders still require a specific manager review. **Purchase → Migrated purchase history** contains all saved orders. Potential conflicting originals and destination orders
are reported together before a replacement is created.

Daily actions use per-history/order row locks, a bounded native vendor-reference reservation, and per-order archive updates. The reservation prevents stale-snapshot races between histories with the same destination vendor reference.
They do not run financial cutover locks/scans or rewrite the completed move report.
Purchase managers do not need Settings or Accounting administrator rights for
these follow-up actions. Ordinary purchase users can read history only.

Uninstall through Odoo before removing files. Native purchase orders and completed
financial entries remain. Full history and per-order follow-ups survive as private
native attachments and configuration markers. Reinstall restores history and links
without creating orders. Uninstall is refused if both an original and its active
replacement have been reopened; cancel the original first.

Copy both addon folders before upgrading the older combined financial addon. The
19.0.3.0.0 migration preserves its existing purchase feature and transfers ownership
of purchase metadata before cleanup. See ../INSTALL.txt and
../company_financial_cutover/README.md for cutover checks, archive recovery and tests.

Repeated history-only review shows **Already up to date** when there is nothing new to copy; it creates no additional history or archive. Buttons reflect the same role and company requirements enforced by their backend actions. Preparing a replacement creates an unconfirmed draft; confirmation of the new native purchase order remains a separate decision.
