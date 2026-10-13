# Independent candidate challenge review

Reviewer role: separate read-only agent. No candidate source was edited or Git pushed.
Review date: 13 October 2026. Final supported paths reviewed against candidate runtime source and the verified native
regression/simulation evidence listed below. This report does not certify production use.

## Confirmed baseline root cause

Supplied bundle SHA256: 4e27269b39fa2ad0fb215cfdc52f870e250fb228455a17be6fd8c78b6d0d8d15.
Financial version 19.0.1.1.1; stock version 19.0.2.7.4; no purchase addon in supplied ZIP.
The supplied six-error report names normalized contact matching, posting rollback,
prior-year retained earnings, source stale hash, source change at confirmation, and
zero-net control-account open items. Every traceback ends at supplied
company_financial_cutover/models/cutover.py:501, through _lock():469.
They therefore exhibit one shared snapshot-guard interruption, not six demonstrated
accounting-calculation defects.

Independently reproduced with installed Odoo19, disposable DB fifo_doc_baseline,
Unix socket /workspace/fifo-document-work/runtime/socket port55433:
- Committing only a synthetic ir_logging row caused SerializationFailure, pgcode None.
- Odoo's actual retrying() raised KeyError(None) after one attempt.
- Synthetic log cleaned after verification; no real-company data used.
- Reproducible script: independent_snapshot_probe.py; result: independent-snapshot-probe.log.

Safety base 882b60b uses actual locked-table tuple visibility, including deleted versions,
and Odoo ConcurrencyError. Its tests cover unrelated commits and real retrying().
The old name-only contact-reuse test intentionally changes semantics in the safety base;
restoring unsafe name-only reuse to satisfy that legacy expectation would be a regression.

## Findings resolved and rechecked

1. FIXED (was HIGH) — Multi-source receipts readable with only one source enabled.
company_document_cutover/security/security.xml uses source_company_ids in company_ids,
meaning ANY source, not ALL. Read ACL permits accounting managers to read job reports
with hidden branch financial evidence. _operator() checks all sources only for mutations.
Final rules use not-any outside enabled companies; native hidden-source job and match read tests pass.

2. FIXED (was HIGH) — Private native audit attachment exposes financial evidence to ordinary staff.
company_document_cutover/models/migration.py attachment creation links res.company.
Odoo ir_attachment.py:515-524 grants access through the linked resource; public=False
only removes anonymous access. Independently confirmed an employee without accounting
groups can read raw bytes of a private company-linked attachment, using rollback-only
synthetic fixtures. Evidence independent_attachment_acl_probe.py and
independent-attachment-acl-probe.log. Final attachments are standalone native documents with owner/administrator access,
public=False, and native non-accounting employee read denial passes. Uninstall verification
retains raw audit evidence without widening its access through company membership.

3. FIXED (was HIGH; native overlap regression passed) — Duplicate commitments across new
and existing purchase workflows. New PO bridge stores document link keys, whereas old
purchase history/replacement logic uses purchase_marker_key plus origin
'Migrated purchase sourceCompany/sourceID'. New native POs lack that origin/shared marker.
With empty vendor reference, document-copy success followed by source cancellation and
old PO-only history/replacement may create a second destination commitment. Final old replacement checks document-owned source identity; new bridge checks old
replacement markers and global document source ownership. Native empty-reference cross-flow
regression passes without a second target order.

4. FIXED (was MEDIUM) — Mapping SQL uniqueness declaration is unsupported in Odoo19.
DocumentMapping uses _sql_constraints; odoo/orm/model_classes.py:162-164 explicitly says
that attribute is unsupported. ORM len-one checks help routine use but do not establish
a database uniqueness guarantee under concurrent mapping creation. Final uses models.Constraint UNIQUE(job_id,model_name,source_id); native raw SQL duplicate
INSERT test fails with UniqueViolation and therefore verifies installed schema enforcement.

## Other challenges corrected in final source

- Removed account.account.deprecated assumption; use native active setting.
- Invoice line correspondence includes product/UOM/quantity/price/discount and tax tags.
- Tax-reporting manual journals block rather than silently omit repartition/tag evidence.
- Stock move history dependency covers stock_move_ids.
- Financial fingerprints now include tax_tag_ids, tax_repartition_line_id and sequence;
  native edited-tax-evidence regression passes. Exact source.name and original references
  are saved separately from display labels.
- Computed mapping identities reject forged RPC writes; the native access test passes.
- Completed source coverage is frozen; newly added backdated history blocks catch-up.
- A real reinstall preview bug was reproduced: Many2many scope read back in company
  name order, so unchanged scope/configuration generated a different digest. Source
  IDs and company configuration rows are now canonically sorted by ID. The unsorted
  branch-scope regression passes without weakening stale-evidence detection. Evidence:
  diagnose-reinstall.log and reinstall-plan-diff.json before, reinstalled-verification.log
  and survival-results.json after.
- Human preview/result text now renders document counts, account amounts and PO
  commitments rather than technical hashes or raw JSON; primary Confirm precedes
  Update review. Each purchase preview line includes its description and unit, so
  users can identify multi-line commitments. The final presentation change passed
  28 affected native document/PO tests (final-po-readable-review.log). Execution
  contracts are unchanged by the presentation change.
- Repeated payment memo/reference collisions exclude only independently validated durable
  destinations for covered sources; repeat-job regression passes.
- Confirmed purchase copies require destination-specific products, avoiding source/shared
  supplierinfo changes from native button_confirm().

## Remaining scope gates

Full-history replay through cutover with no added opening avoids double counting when
all source posted entries, original openings and fiscal closings are covered exactly.
Existing unrelated destination activity is retained; existing matching imported documents
cannot yet be adopted explicitly. Native references use destination legal numbering with
original number retained in audit.

Supported path is bounded atomic history (up to 200 accounting records and 200 POs),
manual journal-backed payments, exact native invoice/tax/date/rate validation, closed
reconciliation coverage, untouched RFQs/POs, and explicitly adopted existing native
PO/receipt/return history with bill-line links. Historical adoption requires exact matches,
ordinary untracked goods, closed cutover coverage, preserved economic stock values and
product costs, and authentic native quantities. Unsupported approval states still block.
Exchange-difference, cash-basis, bank-statement, EDI, analytic history and cross-cutover
reconciliation are not implemented. Do not report these requested cases as PASS.
The stock module is restored byte-for-byte to the supplied ZIP (30 files independently
compared). It was not installed or executed; synthetic receipts are fixtures only. Installer packaging/hashes are a separate release step; this review records the source
and evidence hashes below so the delivered installer can be checked against reviewed code.

## Final review result

PASS for the implemented, bounded, test-only native replay paths. No HIGH finding from
this review remains unresolved for those supported paths. This is not an approval for
production deployment and is not a claim that the requested complete migration is done.

Installed Odoo19 / PostgreSQL17 verification:
- Exact uploaded baseline: 38 tests passed on a quiet cluster; independent concurrent
  unrelated-commit probe reproduced the original bug and real retry crash.
- Expanded historical support: fresh full runner193 tests, 0 failures/errors
  (fifo_doc_repeat_20261013014044_365775.log). Final locked-order follow-through was
  subsequently covered by44 affected tests, 0 failures/errors
  (history-final-lock-tests.log), plus1 native stale-lock-preview regression
  (history-lock-preview-regression.log). These overlapping runs are not summed as
  unique tests. Earlier logs retain genuine failures and fixes.
- The earlier full179 suite and separate fresh116 financial/document-only suite passed
  before expanded purchase history; current accounting-only22 affected tests also
  passed. Purchase remains an optional addon, not a forced accounting dependency.
- Independent reviewer raw SQL readback on disposable fifo_doc_simulation passed:
  target receivables227, bank150, payables-140, revenue-577, expense340; net profit237;
  original source receivables220, bank150, payables-140, revenue-570, expense340.
- Customer unpaid amounts70/150/0 and vendor amounts60/80/0; 15 destination documents
  (13 posted, 2 draft), 4 manual ledger payments; target existing invoice7 retained.
- Three purchase orders (draft/confirmed/cancelled); confirmed ordered10, received0,
  billed0; pending receipt commitment10; no validated target receipt or extra opening.
- Two real concurrent confirmations through Odoo retrying completed in 1 and 2 attempts;
  repeated confirmation after a lost response created no duplicate.
- Original move/PO fingerprints and existing destination trade fingerprint unchanged
  in simulation-results.json. Independent readback did not call the migration planner.
- Native document addons absent on clone: invoices, payments, purchase IDs, durable
  links and raw audit retained (uninstalled-verification.log). Reinstall then replay
  verification retained exactly15 documents/4 payments/3 POs with no second copies
  (reinstalled-verification.log and survival-results.json).
- Simulation has no active scheduled jobs or configured active outgoing mail servers.
- Controlled interruption uses fault injection around native creation, not mocked
  accounting: all created records roll back; the test distinguishes this from the
  real two-cursor/thread native concurrency checks.

Reproducible independent evidence: independent_snapshot_probe.py,
independent_attachment_acl_probe.py, independent_posted_reconciliation.py, their logs,
and independent-posted-reconciliation.json.

Remaining requested features remain BLOCKED / NOT IMPLEMENTED: creating missing historical
receipts/returns; tracked lots/serials, owners, packages, pending returns/nonstandard
pending routes, manual valuation adjustments and stock valuation journal histories;
reconciliation closure beyond cutover; bank statements; exchange-difference histories;
cash-basis/EDI/analytic histories; pre-existing accounting imports without durable verified
links; incremental/chunked jobs and histories larger than200.
The user authorized the receipt/return mapping extension and native bill links for
synthetic testing. That authorization does not turn unmatched target history into an
automatic stock replay. Stock migration was not executed. Existing historical stock
records were adopted without creating receipts/returns or changing economic stock data.


## Independently reviewed historical extension

After explicit user authorization, the candidate now adopts explicitly selected existing
native destination POs, PO lines, completed receipts/returns and bill-to-PO relationships.
It never constructs missing completed stock records or forces received/billed counters.

Review challenges resolved:
- Receipt/return correspondence and movement/line/value evidence persist inside native
  durable PO links and private audit attachments; they survive module removal.
- Source/target movement authenticity includes company, product/UOM, date, quantity,
  return parent, usage direction and value. Extra or aliased destination movements and
  aliased PO ownership block; completeness compares ID sets, not creation order.
- Post-cutover receipt/return history blocks. Tracked/owned/packaged history, unresolved
  pending returns/nonstandard routes, manual valuation adjustments and valuation journals
  block until dedicated evidence is available.
- Native vendor-bill posting is preserved. Because stock_account recalculates stock
  valuation even when account_move_id is empty, the candidate checks before/after all
  related product stock movements, quantities, economic values, manual valuation records,
  product costs/cost methods and unexpected extra accounting entries. Any change rolls
  back the whole job instead of bypassing native hooks. Comparison uses company currency
  for stock values, including foreign PO currencies with different rounding.
- Source bill/credit rows are linked during native invoice creation. Unmarked existing
  destination bill rows block duplication. Native posting/residual checks and final
  billed-quantity verification precede atomic completion. Fault injection proves native
  bills, links, quantity counters and economic stock changes roll back together.
- Historical locked/completed PO state is compared and fingerprinted. Untouched copies
  preserve lock via native lock/unlock, with durable lock evidence. Source lock changes
  after preview invalidate the review.
- Preview distinguishes commercial ordered-minus-net quantities from actual native
  pending receipt commitments and quantity available to invoice now. Return alone does
  not manufacture a replacement incoming delivery.
- Legacy untouched-pilot fingerprint shape is retained. Independent read-only current
  runtime verification of prior fifo_doc_simulation draft/confirmed/cancelled PO links
  passed (independent-legacy-pilot-read.json).

Final independent SQL/native readback on fifo_doc_history passed:
- Trial balance: receivable227, bank150, payable-178, revenue-577, expense378, equity0;
  net profit and balance-sheet profit both199. No additional opening entry.
- Historical PO ordered10, net received3, net billed2, presently billable1. Actual
  pending incoming demand6; commercial ordered-minus-net-received7.
- Two native posted bills19 and38 plus credit19; net liability38. All three native
  migrated invoice lines link to the selected destination PO line.
- Real existing receipt quantity4/value76 and return quantity1/value0 exactly match
  source and destination IDs/dates/quantity/value. Both exact receipt maps are retained
  identically in native parameter and private audit. Preserved PO lock state matches.
- Target totals18 accounting documents and4 POs; no second PO or historical receipt
  was created for adoption. Native stock_account and purchase_stock are installed;
  company_stock_fifo_migration is absent. No active cron or SMTP server exists.

Reproducible independent evidence: independent_history_reconciliation.py,
independent-history-reconciliation.log/json, independent_legacy_pilot_read.py and
independent-legacy-pilot-read.log/json. Committed simulation before/after native stock,
product-cost, source and existing-trade invariants passed in history-simulation-final.log
and history-simulation.json. These are synthetic tests, not production validation.

Real final module-removal/reinstall clone fifo_doc_history_survival_final passed:
- history-final-uninstall.log and history-final-absent.log: native document/order/stock
  IDs and receipt maps/private raw audit retained while the document addon models are
  absent.
- history-final-reinstalled.log and history-survival-reinstalled.json: repeating verified
  transfer retains the same native IDs and received3/billed2/billable1, without new copies.
- Survival comparison normalizes only the bookkeeping verification-job ID; all native
  business IDs, hashes, quantities and receipt evidence must remain identical.

No remaining material finding from this independent review is open for these supported,
bounded synthetic paths. Production readiness and unsupported-history claims remain
unchanged: this is a test-only candidate requiring real-data restoration validation.

## Final four-gap correction review — 19.0.0.3.0

This addendum is the current review result. The prior positive supported-path review
did not cover the four negative combinations subsequently reproduced in
/workspace/artifacts/document-history-review-20261013/RECHECK-REVIEW.md. Those findings
were real; their original probe successes confirmed defects, not safe migration.

The four confirmed gaps and independent follow-through are now FIXED:
- Renumbered existing invoices are searched by commercial invoice date, partner,
  currency, total and commercial line content, including imports booked into a later
  period or at a different FX balance. Existing native numbering, ledger account and
  remittance-label changes do not hide an otherwise matching commercial transaction.
  Each unresolved similar source/destination pair blocks. Only a specifically selected
  pair with an accountant's separate-transaction declaration and reason can proceed.
  No blanket overlap waiver or automatic invoice merge/adoption is introduced. The
  decision model has all-source company rules, native pair uniqueness, operator/read
  checks, immutable completed decisions and preview invalidation after editing. Its
  exact pair/reason is retained in private native audit evidence.
- Product billing policy is validated, hashed in configuration/supplementary evidence,
  and native qty_to_invoice plus aggregate invoice_status are checked after linking
  bills. Saved PO line_map comparisons also reject mismatched policy/billable status
  in legacy receipts lacking supplementary record details; old completed jobs cannot
  silently re-certify an already incorrect ordered-versus-received billing outcome.
- Pending receipt product, company, native unit and PO-line ownership are checked
  before aggregate demand comparisons. The same helper runs for saved legacy links,
  so a wrong-product receipt accepted by an old version is blocked on reuse rather
  than blessed by its old hash. No pending stock record is altered to make checks pass.
- Invoice payment_reference is copied through normal native creation, restored through
  normal native write if posting regenerates a reference (including an explicitly
  blank original), then validated and saved in source/target record_details/audit.
  Legacy links retain their original fingerprint format; differing old invoice
  remittance references block review of the existing copy, never create a second copy.

Evidence reviewed:
- Fresh full native suite203 passed (fifo_doc_repeat_20261013023220_369804.log); final
  affected56 native tests passed after later-booking, blank-reference and legacy
  follow-through fixes (four-fixes-final-56.log). Accounting without Purchase28 passed
  (four-fixes-final-account-only.log). These overlapping runs are not summed.
- The blank-reference regression reproduced failure before the native metadata-write
  correction (four-fixes-blank-reference-before.log). Pair scope, edited decisions,
  legacy policy and pending identity protections are covered in native regressions.
- Independent reviewer read-only SQL/native readback of committed fifo_doc_history
  passed: receivable327, bank150, payable-178, revenue-677, expense378, profit299;
  19 native target documents,4 POs, no extra opening. The extra100 is an explicitly
  reasoned independent transaction, not an automatic duplicate exemption.
- Historical PO remains received3/billed2/billable1, with native pending receipts6.
  Receipt4/value76 and return1/value0 retain exact native source/destination matches.
  Three migrated bill/credit lines remain linked to the selected target PO.
- KEEP-REMITTANCE-FOUR-FIXES is identical on native original/copied invoice and in both
  supplementary audit details. The declared source/target pair and reason match
  native overlap rows and the retained private audit. Reproducible evidence:
  independent_four_fixes_reconciliation.py, independent-four-fixes-reconciliation.log/json.
- Actual removal/reinstall on fifo_doc_history_survival_fixes passed again: same native
  IDs, receipt maps, raw audit and quantities, no duplicated copies
  (four-fixes-absent.log, four-fixes-reinstalled.log and survival JSON files).

No concrete unresolved defect from this four-gap challenge remains in the reviewed
supported paths. This is sampled correctness/concurrency review, not exhaustive
security, real-data, legal/localization or production-performance certification.
Commercial similarity cannot prove whether two real transactions are the same; the
accountant remains responsible for each reasoned distinction. Arbitrarily changed
prior imports and adopting unmarked existing accounting records remain outside
automatic support. All dedicated history/valuation/tax/provider/batch limitations
above remain in force. Candidate remains test-only.

## Reviewed runtime file hashes

- `company_document_cutover/models/migration.py`: `71dbec50b6235344b34ce86fa579a58a60cf103f0fd1408fafb097f99891679f`
- `company_document_cutover/security/security.xml`: `b64df77f6835377b94bad085f7a752f9a6b46e14ea1d9578b360d8f680273dba`
- `company_document_purchase_cutover/models/purchase.py`: `6b6a920506225f98442c1fef2ea8787d00ab4ff438789b7ad172e5dc990f91c8`
- `company_purchase_cutover/models/purchase_history.py`: `42ba765112e2d3dc9a48fb409997ef5ff7088b1258ef5db6b517aa3efba71239`
