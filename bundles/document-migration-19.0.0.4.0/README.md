# Document migration 19.0.0.4.0 — test candidate

Download the ZIP in this folder. It is the exact checksummed tested source bundle.
See root MIGRATION-GUIDE.md and VALIDATION-REPORT.md for supported cases and remaining gaps.

Verified native checkpoints: 235 full-suite tests, 49 final accounting-only tests, four
final ownership checks, two real Chromium workflows, committed multi-company simulation,
and actual uninstall/reinstall recovery of completed and partial jobs. Counts overlap.

The ZIP keeps the original supplied stock addon; it was not executed. Repository stock
source retains main's newer fixes. Missing stock-history reconstruction, provider/EDI cases
and production/restored-real-data validation remain outside automatic support.
