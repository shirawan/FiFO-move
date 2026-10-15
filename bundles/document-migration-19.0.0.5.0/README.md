# Company document migration 19.0.0.5.0

[Download the installer](FiFO-document-migration-19.0.0.5.0-9c5bc7e2.zip). Test on a restored database first.

Preserves current stock and retains missing receipt/return history as audit references. Historical references create no live supplier commitments. Existing matched native history can be verified and reused. Provider/EDI/internal-transfer histories remain blocked.

- [Migration guide](MIGRATION-GUIDE.md)
- [Validation report](VALIDATION-REPORT.md)
- [Independent review](INDEPENDENT-REVIEW.md)
- [Checksums](SHA256SUMS.txt)
- [Source patch](candidate-from-f0f98f5.patch)
- [Native evidence and reproduction scripts](native-evidence-20261015.zip)

Validated: 266 native tests, 55 accounting-only tests, 25 affected regressions and four real browser tests; actual complete and optional-only uninstall/reinstall. Counts overlap.
