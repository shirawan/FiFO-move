# Company Accounting Document Migration — 19.0.0.4.0 test candidate

Full native accounting-history replay with explicit existing-document adoption, resumable
steps and automatic saved-choice recovery. Native bank statements, exchange/cash-basis
entries, analytic allocations, fiscal positions and cash rounding are checked against
source evidence. No additional opening is added. Accounting-only installation excludes Purchase.

Read ../MIGRATION-GUIDE.md and ../VALIDATION-REPORT.md for the supported subset and limits.
New receipts survive uninstall and recover completed/partial moves; retain a backup.
