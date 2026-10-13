# Company Purchase Document Migration — optional candidate bridge

Copies untouched RFQs/POs and reuses explicitly matched historical destination POs,
receipts and returns. Copied vendor bills/credits link natively to matched PO lines.
No historical stock move is created or validated. Missing or different destination
history is reported together; unmarked existing bills are not silently adopted.

Native valuation hooks run, with full rollback if existing stock economics change.
Untracked goods only; see ../MIGRATION-GUIDE.md for supported history and remaining limits.
Purchase/Inventory is optional for the accounting-only addon. Version19.0.0.3.0 is test-only.
