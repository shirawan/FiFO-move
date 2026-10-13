# Company Accounting Document Migration — candidate

Native full-history replay, separate from opening-balance cutover. Settings and Accounting
administrators configure explicit native matches, review counts/totals and execute one atomic
job. Supported native invoices/bills/credit notes and manual journal-backed payments preserve
accounting amounts and reproducible reconciliations. Durable native links survive uninstall.

Test-only: see ../MIGRATION-GUIDE.md and ../VALIDATION-REPORT.md for supported cases, blockers,
limits and independent evidence. This is not complete historical document/PO migration.

Version19.0.0.3.0 preserves original payment references and flags renumbered existing
invoices. Pair-specific separate-transaction decisions require an accountant reason.
