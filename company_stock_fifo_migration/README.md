# Company Stock Cutover

A one-time opening-stock cutover from an old company to its replacement company in Odoo 19 Enterprise. Choose a source warehouse and an existing destination warehouse in the target company. The tool reuses matching company-specific products and child locations, creating only missing ones. It does not create a warehouse. Shared All Companies products are included without changing their originals. No product-matching batch is needed. This addon does not migrate historical sales, purchases, POS sessions, manufacturing orders, BoMs or old accounting entries.

## Before starting

Release 19.0.2.7.3 allows Periodic source stock to move into an existing Perpetual destination product. Closing and opening accounting are checked using each side’s valuation setting; the destination product is reused and neither setting changes. Build a fresh Preview after upgrading.
Release 19.0.2.7.2 reconciles non-lot FIFO stock left in other source locations from the reviewed remaining receipt quantities and costs. Independently rounded source remainder and closing totals no longer create a false one-cent failure; the difference is recorded in Native Currency Rounding. Currency precision is not relaxed, and genuine value differences still roll back.
Release 19.0.2.7.1 fixes uncategorized products: their new copies use the named Opening Stock category with the source company's costing and valuation methods. Originals remain unchanged.
Release 19.0.2.7.0 adds optional destination location choices, alongside product choices and Standard-to-FIFO transfers.
Preview identifies each product as **Create** or **Reuse**. Repeated moves into
the same company reuse the matching product instead of creating a copy per warehouse.
Matching uses prior migration mappings, internal references, barcodes, and exact
names with matching variants. For ambiguous matches, use **Destination Product Choices**:
add only the affected source product and select its existing destination product.
The destination record ID and reference are shown to distinguish similarly named records.
Neither alternative needs deleting or archiving. Choices apply to this cutover only,
must use one destination family for sibling variants, and become read-only on completion.
An existing FIFO product may receive Standard Cost source stock at its carried value;
its costing settings and existing stock are preserved. Other method differences remain unsupported.
Missing child locations are created; matching paths are reused.
Use **Destination Location Choices** to route a source stock location to an existing
stock location inside the selected destination warehouse. Add only the locations
you want to redirect; other locations remain automatic. Several source locations
may use the same destination. Unmapped children follow their mapped parent;
matching children are reused and missing children are created once per destination path.
This does not change which source stock is selected: all recorded stock in the
source warehouse still moves. Choices are locked after completion.

Source-product validation is shared between the warehouse and
earlier matching screens. Independent product problems are listed together.
Warehouse Confirm reuses the source plan freshly checked under locks, instead
of replaying the earlier screen's Preview and Check actions. Target validation,
fresh-transaction stock checks and final native reconciliation remain in place.
Source-value reconciliation uses the actual native closing value, avoiding a
false half-cent remainder from subtracting an unrounded cost estimate.
Rebuild any existing Preview after upgrading.

- Use the validated release on a test database first. Do not use an unvalidated working copy for a live cutover.
- Install the **Separate Kit BoM Mover** dependency; it remains needed for earlier cutover records. You do not need to run it or create product matches for this screen.
- Sign in as a Settings administrator and enable both companies in the company switcher.
- Agree the cutover date with your accountant. Stop stock activity in both companies during the cutover and keep a verified backup.
- Both companies must have the same currency. Source products may use FIFO, Average Cost or Standard Price, with perpetual or periodic valuation. Their methods are preserved on the target copies. Perpetual products need valid stock accounts/journals; a source category valuation account is accepted without a company default. Periodic-only transfers do not require perpetual stock-account configuration.
- Branches may use parent-owned products, inventory accounts and stock journals accepted by Odoo's native company rules. Stock quantities remain scoped to the selected company's warehouse; stock in the parent or sibling branches is not moved. Unrelated-company products, accounts and journals are rejected. Source product ownership is preserved; new product copies belong to the Target Company.
- Confirm the opening-stock amounts and clearing-account treatment with your accountant. Confirm creates separate company-specific Stock Migration Clearing Current Assets accounts where needed, reusing accounts previously created by this tool.

## Guided steps

Open **Inventory → Configuration → Warehouse Management → Move Opening Stock**.

1. Click **New**. Choose **Warehouse to Move**, **Target Company**, and **Destination Warehouse**. The destination warehouse must already exist in that company.
2. Click **1. Preview Warehouse**. Review every product's **Create** or **Reuse** result, quantity, opening value and stock remaining outside this warehouse. Preview creates no products, locations, accounts or stock movements.
   Optionally add **Destination Location Choices** before Preview: choose **From Location** and **Move Stock To**. The latter must be inside the destination warehouse. Preview lists the chosen location paths. Rebuild Preview after changing a choice.
3. If several products match, add a row under **Destination Product Choices**, choose the source and destination, and Preview again. All other products stay automatic. Resolve any other blocking explanation, then build a fresh Preview. Changes to choices, stock or copied configuration require a fresh Preview.
   Stock blockers name the product, full location, on-hand/reserved quantities and exact reason (negative stock, reservation, consignment owner or package). Lot/serial is shown when applicable. Checks include the same products elsewhere in the Source Company, not only the selected warehouse. Up to 20 blocking rows are displayed with a count of any remaining rows; no inventory is changed by this diagnostic.
   **Move recorded stock; leave old operations behind** is enabled by default on new records. Existing drafts keep their saved selection. You do not need to finish or cancel old orders, clear Picked flags, or apply draft stock counts. Preview uses recorded on-hand quantities, shows the first 50 reservation rows, and retains the complete reservation audit including original Picked flags. Confirm releases only the reviewed reservation lines inside the selected warehouse, including Picked lines. Old deliveries/orders/MOs remain open in the old company; they are not migrated or cancelled. Draft stock counts are not applied or copied. Do not process old operations or apply old draft counts after cutover. Reservations in other warehouses or companies are not released or cleared. Packed/consigned reservations inside the selected warehouse and inconsistent reservation quantities still block the cutover. Changing this option or an affected reservation requires a fresh Preview. Any failure rolls the release back together with the stock/accounting changes.
4. During the agreed maintenance window, stop inventory operations across the database and click **2. Confirm — Move All Stock**. Confirm reuses matched products and child locations, creates missing products/categories/locations and lots/serials where needed, and creates native closing/opening movements. Existing destination stock remains and the moved stock is added to it. No new warehouse is created. Perpetual products also create posted valuation entries; periodic products do not. It briefly locks stock/configuration tables and refuses if they are busy. Any failed reconciliation rolls the entire operation back.
5. Click **Open Stock Reconciliation**. Check quantities, values, lots and linked accounting entries. Configure target taxes, BoMs, suppliers, packaging, POS warehouse links and reordering rules separately before normal operations.

Existing company-specific target products are reused when uniquely matched and compatible with the transfer. The tool does not silently select between duplicate matches. Shared-product barcodes remain on originals and are blank on new copies because their original global barcode must stay unique. Variant attributes and their price extras are preserved on copies; complex variant exclusions require separate review and are refused. New categories preserve the source effective costing/valuation settings and use target-company stock accounts. Archived stocked originals remain archived; their new copies are active.

**Periodic valuation:** each company follows its own product valuation setting. The whole-warehouse mover can reuse an existing Perpetual destination product when the source is Periodic. Source closing stock has no automatic journal; destination opening stock has a posted valuation/clearing journal, checked independently. Neither product’s setting changes. For a Periodic side, its general-ledger inventory adjustment must be handled separately. Perpetual-to-Periodic mismatches remain blocked. Build a fresh Preview after upgrading to 19.0.2.7.3.

New product families include unstocked sibling variants. Preview shows the number of new variants and refuses more than 1,000.

Completed cutovers are read-only. **Review Earlier Stock Cutovers** opens audit records created by the previous matching-based screen.

Apply processes closing movements together, then opening movements together,
using Odoo's native batch operations. Odoo may combine multiple products into
one valuation journal entry per company. Each product's quantity, FIFO value,
valuation-account amount and clearing-account amount is reconciled separately.

Clearing accounts use an unused code starting at `STKCLR0001`. Unrelated accounts are never renamed or repurposed. An archived or invalid saved account causes an error instead of being silently restored or replaced. Native account permissions apply. Accounts and their references survive uninstall; uninstall is not an Undo or cleanup action.

## Boundaries

- This uses current remaining stock, not a reconstruction of a past balance. FIFO uses the company-wide remaining receipt stack; Average Cost and Standard Price use native current closing cost. Source stock activity after the requested cutover date is rejected.
- Partial transfers must take an oldest-first FIFO prefix. A product valued separately by lot must move whole lots, not part of a lot.
- Negative stock, consignment and packages need separate handling. On the whole-warehouse screen, **Move recorded stock; leave old operations behind** removes old-operation and draft-count cleanup requirements, including Picked-operation blockers. It does not bypass valuation/accounting, ownership, inconsistent reservations or rollback protections. Earlier matching-based batches retain their stricter unfinished-operation checks.
- Unselected warehouses and stock outside warehouse trees are not silently included. They remain in the Source Company and appear in the company-wide review.
- The selected destination warehouse keeps its existing operations and routes. Only missing child locations are created; matching paths are reused. Source-company routing, reordering rules, and POS links are not copied.
- A cutover is not an Undo button. Completed batches cannot be repeated. The whole-warehouse transfer marker is scoped to the source warehouse and product: the same product in another warehouse can move in a later batch. Earlier matching-based batches retain their company/product marker. Keep the reconciliation and backup as the cutover record.
- Your accountant must reconcile the Source inventory value to its existing general-ledger balance before cutover. This addon does not repair historical accounting or migrate sales, supplier balances, bank balances, or other opening accounts.
- Native currency rounding may change fractional opening unit costs and subsequent individual COGS amounts. Apply accepts currency-equivalent opening receipt totals only after quantity, product-value, FIFO-boundary and journal reconciliation pass. The rounding and before/after unit costs are recorded under Stock Reconciliation. Material discrepancies still roll back; no write-off account hides them.

Preview limits per batch: **1,000 mapped inventory products, 10,000 stock rows, and 10,000 remaining FIFO receipt rows**. Check also limits Apply to **250 closing/opening movements**. Split larger cutovers by different products; do not split a product's FIFO pool arbitrarily.

## Existing stock in the preview

The whole-warehouse preview shows **Already in new company**, **Moving now**, and
**New company total** for each product. Existing quantity is preserved and the
moved quantity is added. These destination totals cover all warehouses in the
new company; **Left elsewhere in old company** shows source stock outside the
selected warehouse. Older saved previews missing a destination baseline ask for
a fresh preview rather than assuming zero existing stock.

Quantities use fixed notation at the recorded Product Unit precision, including
large quantities kept in grams. Odoo 19 uses this decimal precision across units
of measure. Older previews without precision metadata display the full recorded
value without scientific notation; rebuild previews after upgrading.
