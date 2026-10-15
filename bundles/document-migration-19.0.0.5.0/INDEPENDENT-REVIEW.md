# Independent implementation review — v5 final bounded review

Reviewed 15 October 2026, read-only working tree at `/workspace/FiFO-move`, version 19.0.0.5.0. **Current status: supported paths reviewed; no unresolved material findings after the corrections below.** This is a bounded source review with independent committed SQL readback, not production certification or approval of unsupported migration paths. No source edits or stock/provider execution by the reviewer.

Definitive final evidence: 266 native tests passed (`archive-release-full.log`), 55 accounting-without-Purchase tests passed (`archive-release-accounting-only.log`), 25 affected optional-recovery tests passed, and four browser tests passed. Each final summary reports zero failures/errors. Actual removal/reinstall covers both document addons together and the optional purchase addon alone; the latter includes all seven optional mapping models. Independent read-only SQL corroborated the recovered ledger/stock economics and the final optional-only clone's native IDs, write dates, original archive and exact saved mappings. All 34 final source hashes were independently recomputed, match the refreshed freeze manifest and are saved in `independent-v5-final-source-hashes.json`.

Historical findings and intermediate pending states are preserved below. They are superseded by this current status and the definitive optional-only result at the end. Missing receipt history remains audit-only; provider/EDI/paired-transfer histories remain blocked pending specialized adapters. No production execution or readiness certification is claimed.

## Findings sent to the implementing agent

1. **Company coverage omits linked financial history (privacy/scope).** `company_document_purchase_cutover/models/archive.py:134` saves all linked bill numbers, dates, states and balances, whereas line 150 derives the archive's covered companies only from the PO and stock graph. Native Odoo `addons/purchase/models/account_invoice.py:529` defines `purchase_line_id` without `check_company=True`, so cross-company bill linkage is possible. An operator with additional enabled companies can retain an unselected-company bill inside an archive later downloadable by a manager with only the declared archive companies. Validate every linked bill/line and valuation-entry company and access before snapshotting; reject unselected scope and include all actual companies in the saved coverage. This issue is established by source control flow; no native reproduction claimed here.
2. **Complex-route monetary evidence is incomplete.** `models/native_history.py:89` compares recursive route nodes' identity, quantities, dates and locations, but omits monetary value. The root done receipts separately compare value in `models/purchase.py:319`; downstream/return nodes without a valuation JE or manual-value row are not covered by that check. Compare company-currency-rounded value for every mapped graph node and the relevant price evidence. Add a regression with a mismatched completed downstream node.
3. **Completed native PO evidence does not freeze recursive nodes.** `models/purchase.py:36` fingerprints direct `order_line.move_ids`; `models/native_history.py:15` adds neighboring IDs, not the field evidence of every descendant. The saved `receipt_matches` at `models/purchase.py:217` likewise covers only root done movements, and `_stored_link` at line 192 does not re-run full graph verification. A downstream node can be edited after completion without changing a root fingerprint. Save the full recursive source/target graph as supplementary durable evidence and revalidate it on completed reuse and reinstall. Preserve compatibility of prior nonhistory fingerprints.
4. **Unsupported external-history gates need target coverage.** `company_document_cutover/models/migration.py:207` rejects source provider/EDI histories; `_adoption` in `models/history.py:51` checks target payment method code but not target transaction/token lineage. A manual-coded target provider payment can therefore enter existing-payment reuse, where normal execution may update invoice links and reconcile it. Reject unsupported target provider/token/EDI histories, including stored-link targets, before mutation; alternatively implement a separately verified strictly read-only adoption path. No such adapter is assumed implemented in this release.

## Positive observations and limits

The audit-only marker explicitly identifies `ir.attachment`, carries `audit_only=True`, and avoids fictitious native purchase IDs. Archived bill copies omit `purchase_line_id`; durable bill markers retain original PO/line references. Archive creation runs in the same savepoint as the financial step. Source stock-linked journal entries cannot be newly copied through the reviewed guard: they need existing accounting adoption plus mapped stock movements. The private archive is unattached, and its read-only custom view uses an all-covered-companies rule.

The existing eight native archive tests passed in `archive-first.log`; this reviewer read that output but did not run or independently reproduce those tests. Provider/EDI execution remains unsupported, and no production-readiness claim is made.

## Follow-up review — four findings corrected

The current source corrects all four findings above. `archive.py:125` validates linked bills, valuation entries and covered companies. `native_history.py:126` checks route values and prices; `_record_details` retains complete recursive route and valuation history. `_check_external_target` is invoked by both adoption and stored account-move reuse. The read-only valuation-component guard additionally requires existing residuals, partial reconciliations, reversal links and payment invoice links to match before execution. This prevents the ordinary execution flow from repairing existing valuation history.

Native evidence inspected: `archive-final-targeted.log` reports 19 tests with zero failures/errors, and `archive-external-final.log` reports five optional provider/EDI boundary tests with zero failures/errors. The boundary tests poison the named provider/EDI execution hooks. Final browser/full-suite runs were still pending when this addendum was written.

**Independent committed readback:** `independent_v5_readback.py` opened a read-only SQL transaction against disposable clone `fifo_doc_archive_survival`. Its output `independent-v5-readback.json` passes: 43 saved stock moves, 43 move lines, 23 quants and 12 product-value rows exactly match the pre-resume evidence. Three distinct posted historical bills have no native purchase links. One archive and a recovered completed job exist; three untouched POs are the only native destination orders. The ledger balances with receivable 227, bank 150, payable -178, revenue -577 and expense 378 (profit 199). This independently corroborates the uninstall/reinstall/resume economics without executing stock or provider operations.

### Remaining access finding

The new accounting-manager ACL for `stock.package.type` initially lacked a company rule. This was corrected with a group-specific `company_ids + [False]` rule. `test_accountant_reads_history_without_stock_write_access_or_company_leaks` verifies that a financial/purchase operator without stock-manager privileges can read valuation history, cannot write it, cannot read a third-company package type, and can read a shared type. `archive-final-permissions-native.log` reports 20 affected tests with zero failures/errors. Native `product.value` and `stock.lot` already have global company rules; their new read-only ACLs retain those boundaries. Restricting the archive field and form section to purchase managers appropriately avoids exposing its comodel to an accounting-only screen.

## Final bounded pass — browser issue awaiting correction

All 34 entries in `archive-final-source-hashes.json` matched the working tree at the bounded manifest check. No new accounting/stock/recovery defect was identified in that source pass. The browser run `browser-fifo_doc_repeat_20261015112659_586129-20261015114101.log`, however, demonstrates a current archive detail-screen exception: `archive.py:40` parses attachment `raw` under the browser's binary-size context, and `json.loads` raises `JSONDecodeError: Extra data`. Read JSON blobs with `bin_size=False`; add an archive read regression under `bin_size=True` and repeat the affected browser flow. This UI defect prevents a final clean-review conclusion until corrected. Full-suite and accounting-only runs were still active at this check.

## Final result after correction

The browser defect above is resolved. JSON reads in the archive screen, completed execution and receipt recovery explicitly use `bin_size=False`. The downloadable field declares its binary-size context dependency. A native regression reads archive matches under the browser context, checks the original downloadable bytes and verifies completed/recovered receipt handling. Rendering damaged accounting markers now reports accountant review rather than raising a traceback; the new regression covers a non-dictionary line map.

Final evidence inspected:

- `archive-final-full.log`: 261 native tests, zero failures/errors, all four addons, with optional EDI models installed. This run preceded the final blob-context correction.
- `archive-final-accounting-only.log`: 54 native tests, zero failures/errors, accounting without Purchase. This run preceded the final blob-context correction.
- `archive-final-regressions.log`: 23 affected archive/native-history/recovery tests, zero failures/errors, after the final blob-context and damaged-marker corrections.
- `archive-external-final.log`: five optional provider/EDI boundary tests, zero failures/errors, with named external hooks poisoned.
- `browser-fifo_doc_repeat_20261015112659_586129-20261015114443.log`: four browser tests, zero failures/errors, including accounting and archive desktop/phone flows.
- `archive-survival-reinstalled.json` and independent `independent-v5-readback.json`: real synthetic uninstall/reinstall/resume, balanced ledger and unchanged saved stock evidence as detailed above.

After the final corrections, all 34 entries in refreshed `archive-final-source-hashes.json` were independently recomputed and matched. The independent copy is `independent-v5-final-source-hashes.json`. Critical SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `company_document_cutover/models/migration.py` | `d7d763df1b5e7d27ce9df5c0b8d1f9ab95a4b17aa7c5a463dfd9e42cbd52cb76` |
| `company_document_cutover/models/history.py` | `09d2e7f11dc83751eb7e34469db43bf4a53f700e5b750a23553fe7a89cb77824` |
| `company_document_cutover/models/banking.py` | `cc0879733b4b69427bfe2414838f3bd879994f9a008a60d3fc3835fab59bc063` |
| `company_document_purchase_cutover/models/archive.py` | `2ca5b5d8676fa1a9721e62659ca85c6c49c51389d7b38b899f383a075e029281` |
| `company_document_purchase_cutover/models/native_history.py` | `826e0685c06a28237a7d155fd73ea5182a34493a194b4868fba321aaddb90ad1` |
| `company_document_purchase_cutover/models/purchase.py` | `ba5d0efe9099a8c90559e108282f4cb97c61a7102a6180e6a61fcb6ac572a13b` |
| `company_document_purchase_cutover/models/tracking.py` | `4d34a7ad0a09b9cf71dffd721fd5b3492d476c52706e1a02aaaeeed42ea0f084` |
| `company_document_purchase_cutover/security/archive_rules.xml` | `0a34e06d0e89a6d70865055e9b6e744b487711391e12d3234714f9dbcf3b253e` |

Remaining boundaries are explicit functionality limits: missing target receipt history is retained as audit evidence rather than activated as native stock history; the archive creates no outstanding delivery commitment. Provider, token, EDI and paired internal-transfer histories remain blocked pending specialized read-only adapters. Existing target financial coverage still needs accountant reconciliation; the stock-linked JE guard prevents copying those source valuations over existing stock. All execution evidence is synthetic, and no production stock migration or provider operation was performed by this reviewer.

## Additional optional-addon-only removal challenge

After the preceding freeze, a real optional-purchase-only removal/reinstall exposed a separate recovery gap: the base job survived, but its purchase fields and archive cache were lost, and existing-key recovery skipped reconstruction. The earlier successful survival evidence removed both document addons; it did not prove this optional-only scenario. The implementing agent retained the failed clone evidence in `archive-optional-restored-first.log`.

The new bounded source review found no concrete defect in the correction. The bridge `_restore_surviving_job` checks the retained job state, exact receipt, source/destination scope, cutover and existing mapping subset before restoring optional settings/missing saved mappings and the archive cache. It validates every saved native link inside the receipt savepoint and restores the original state. The uninstall callback bypasses ordinary completed-mapping deletion only through an identity capability supplied by a module-level function after checking system access and native uninstall context; an RPC context value cannot recreate the identity token. Ordinary mapping deletion retains its completed-job guard.

`archive-final-optional-regressions.log` reports 25 affected tests, zero failures/errors, including native record retention and optional-choice reconstruction. At this interim addendum, an actual optional-only uninstall/reinstall repeat and refreshed hashes were still pending. The prior hash table and 34-file manifest verification describe the earlier freeze and must be superseded by the new final manifest. An audit-only survival fixture can have no optional PO/receipt mapping rows, so a real native-matched-history removal test is desirable to exercise the changed selection-ondelete callbacks as well.

## Definitive optional-only recovery and release result

The pending optional-only challenge above is resolved. Actual native removal, absence and reinstall verification completed on disposable clone `fifo_doc_archive_optional_survival`; `archive-optional-survival.json` reports PASS. The added fixture in `archive-optional-native-fixture.json` exercises all seven optional mapping types: purchase order, PO line, stock movement, lot, location, package and manual product value. The absence phase confirms optional mapping rows were removed while native records and base-job states remained unchanged. Reinstall restores those exact choices on the retained base jobs, as well as the original audit attachment and history screen. The final verified run checks native IDs and write dates, not only record counts.

The reviewer independently ran `independent_v5_optional_readback.py` in a read-only SQL transaction against that committed clone. `independent-v5-optional-readback.json` passes: the saved IDs and write dates of 198 accounting moves, 396 accounting lines, 44 payments, 46 POs, 48 stock moves, 48 move lines, 27 quants and 16 product values are unchanged. All saved mapping triples on native-history job 5 exactly match its pre-removal fixture; audit job 4 remains completed with original archive attachment 3140. This readback did not post, reconcile, move stock or call a provider.

Final current-source summaries inspected:

- `archive-release-full.log`: **266 tests, zero failures/errors**, all four addons.
- `archive-release-accounting-only.log`: **55 tests, zero failures/errors**, accounting without Purchase.
- `archive-final-optional-regressions.log`: **25 affected tests, zero failures/errors**.
- `browser-fifo_doc_repeat_20261015112659_586129-20261015114925.log`: **four browser tests, zero failures/errors**, desktop and phone accounting/history flows.
- `archive-optional-{uninstall,absent,reinstall,restored}-verified.log` plus `archive-optional-survival.json`: native optional-only lifecycle, exact choices restored and unchanged native write dates.

The final 34-file freeze manifest was independently recomputed after these changes with zero mismatches. `independent-v5-final-source-hashes.json` exactly equals `archive-final-source-hashes.json`; the critical hash table above has been updated to this definitive freeze. No unresolved material finding remains in the supported paths reviewed. The stated audit-only and external-history limitations continue to apply.
