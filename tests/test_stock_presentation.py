"""Run with unittest without importing Odoo or unavailable Enterprise addons."""
import importlib.util
from pathlib import Path
import unittest

from lxml import html


MODULE = Path(__file__).resolve().parents[1] / "company_stock_fifo_migration/models/presentation.py"
spec = importlib.util.spec_from_file_location("stock_presentation", MODULE)
presentation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presentation)


class TestStockPresentation(unittest.TestCase):
    def _snapshot(self, target=7):
        return {"products": [{"name": "Coffee", "target": target, "target_name": "Existing Coffee",
            "quantity": 4.5, "company_quantity": 6, "unit": "kg", "value": 45, "shared": False}],
            "target_baseline": {"products": [{"target": 7, "quantity": 10}]}}

    def _cells(self, snapshot):
        return html.fromstring(presentation.product_preview_table(snapshot)).xpath("//tbody/tr/td/text()")

    def test_existing_stock_is_added_and_not_replaced(self):
        snapshot = self._snapshot()
        cells = self._cells(snapshot)
        self.assertEqual(cells[2:7], ["10", "4.5", "14.5", "1.5", "kg"])
        self.assertEqual(snapshot["target_baseline"]["products"][0]["quantity"], 10)

    def test_new_product_starts_at_zero(self):
        cells = self._cells(self._snapshot(target=False))
        self.assertEqual(cells[1:5], ["Create product", "0", "4.5", "4.5"])

    def test_older_preview_does_not_claim_unknown_stock_is_zero(self):
        snapshot = self._snapshot()
        del snapshot["target_baseline"]
        cells = self._cells(snapshot)
        self.assertIn("fresh preview", cells[2])
        self.assertEqual(cells[4], "Not recorded")

    def test_product_labels_are_text_and_cannot_insert_html(self):
        snapshot = self._snapshot()
        snapshot["products"][0].update({"name": "<script>alert(1)</script>", "target_name": '<img src=x onerror="alert(1)">'})
        rendered = html.fromstring(presentation.product_preview_table(snapshot))
        self.assertFalse(rendered.xpath("//script|//img"))
        self.assertEqual(rendered.xpath("//tbody/tr/td/text()")[0], "<script>alert(1)</script>")

    def test_large_gram_quantities_are_exact_and_use_fixed_notation(self):
        snapshot = self._snapshot()
        snapshot["products"][0].update({"quantity": 1234567, "company_quantity": 1234570,
            "unit": "g", "unit_decimals": 2})
        self.assertEqual(self._cells(snapshot)[2:7], ["10", "1234567", "1234577", "3", "g"])

    def test_quantities_round_half_up_at_the_reviewed_unit_precision(self):
        snapshot = self._snapshot()
        snapshot["products"][0].update({"quantity": 1.2345, "company_quantity": 1.2345, "unit_decimals": 3})
        self.assertEqual(self._cells(snapshot)[3:5], ["1.235", "11.235"])
        self.assertEqual(presentation.format_quantity(-0.0001, 3), "0")

    def test_old_preview_preserves_small_quantities_without_scientific_notation(self):
        snapshot = self._snapshot(target=False)
        snapshot["products"][0].update({"quantity": 0.00001234567, "company_quantity": 0.00001234567})
        self.assertEqual(self._cells(snapshot)[3:5], ["0.00001234567", "0.00001234567"])


if __name__ == "__main__":
    unittest.main()
