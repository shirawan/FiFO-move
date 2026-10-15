# Optional Company Purchase Document Migration — 19.0.0.4.0 test candidate

Copies untouched RFQs/POs in resumable steps and reuses matched historical POs and their
existing native receipt/return history. Bills/credits link natively; verified existing bills
can be adopted. Matched lots/serials, owners, packages and locations are checked without
moving inventory. Missing receipts, valuation history and complex routes need separate work.

Native valuation hooks run and economic changes roll back. Read ../MIGRATION-GUIDE.md.
