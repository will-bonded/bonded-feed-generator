import csv
import io
import unittest

from feed_tool import idsource
from feed_tool.feed import rows_to_csv_bytes
from tests.helpers import build, discovery, product_html, shopify_product, NON_PRODUCT_HTML

SHOP = "https://shop.invalid"


class ShopifyIdSource(unittest.TestCase):
    def build(self, **kwargs):
        return build(platform="shopify", shopify_products=[shopify_product()], **kwargs)

    def test_default_is_variant_id_with_product_id_as_group(self):
        summary, _, _ = self.build()
        self.assertEqual([r["id"] for r in summary["rows"]], ["901", "902"])
        self.assertEqual({r["item_group_id"] for r in summary["rows"]}, {"111"})
        self.assertEqual(summary["id_field"], "default")
        self.assertEqual(summary["platform"], "shopify")

    def test_sku_source_flags_rows_missing_a_sku_instead_of_filling_them(self):
        summary, out, csv_text = self.build(id_field="sku")
        self.assertEqual([r["id"] for r in summary["rows"]], ["B-1", ""])
        self.assertEqual(summary["missing_id_count"], 1)
        self.assertEqual(summary["clean"], 1)
        self.assertEqual(summary["flagged"][0]["problems"], ["missing SKU"])
        self.assertIn("no SKU", out)
        # item_group_id still carries the product id regardless of the id source
        self.assertEqual({r["item_group_id"] for r in summary["rows"]}, {"111"})
        self.assertIn("B-1", csv_text)

    def test_product_id_isnt_offered_on_shopify_and_degrades_with_a_warning(self):
        summary, out, _ = self.build(id_field="product_id")
        self.assertEqual(summary["id_field"], "default")
        self.assertEqual([r["id"] for r in summary["rows"]], ["901", "902"])
        self.assertIn("isn't available", out)
        self.assertEqual(idsource.options_for("shopify"), ("default", "sku"))

    def test_unknown_id_field_is_an_error(self):
        with self.assertRaises(ValueError):
            self.build(id_field="mpn")

    def test_duplicate_skus_are_flagged(self):
        variants = [
            {"id": 1, "title": "A", "price": "1", "compare_at_price": None, "available": True,
             "sku": "SAME", "image_id": None},
            {"id": 2, "title": "B", "price": "1", "compare_at_price": None, "available": True,
             "sku": "SAME", "image_id": None},
        ]
        summary, out, _ = build(platform="shopify", id_field="sku",
                                shopify_products=[shopify_product(variants=variants)])
        self.assertEqual(summary["duplicate_ids"], 1)
        self.assertTrue(all("duplicate id" in f["problems"] for f in summary["flagged"]))
        self.assertIn("more than one product", out)


class GenericIdSource(unittest.TestCase):
    PAGES = {
        f"{SHOP}/with-sku.html": product_html("With SKU", sku="SKU-1", product_id="PID-1"),
        f"{SHOP}/only-pid.html": product_html("Only PID", sku="", product_id="PID-2"),
        f"{SHOP}/neither.html": product_html("Neither", sku=""),
    }

    def build(self, **kwargs):
        urls = list(self.PAGES)
        return build(platform="generic", discovery_result=discovery(urls),
                     fetch=lambda u: (self.PAGES.get(u), 200), **kwargs)

    def test_default_falls_back_sku_then_product_id_then_page_url(self):
        summary, out, _ = self.build()
        self.assertEqual([r["id"] for r in summary["rows"]],
                         ["SKU-1", "PID-2", f"{SHOP}/neither.html"])
        self.assertEqual(summary["id_url_fallback_count"], 1)
        self.assertIn("page URL", out)

    def test_sku_only_and_product_id_only(self):
        sku, _, _ = self.build(id_field="sku")
        self.assertEqual([r["id"] for r in sku["rows"]], ["SKU-1", "", ""])
        self.assertEqual(sku["missing_id_count"], 2)
        pid, _, _ = self.build(id_field="product_id")
        self.assertEqual([r["id"] for r in pid["rows"]], ["PID-1", "PID-2", ""])
        self.assertEqual(pid["flagged"][0]["problems"], ["missing Product ID"])


class RekeyAfterTheBuild(unittest.TestCase):
    def setUp(self):
        self.built, _, _ = build(platform="shopify", shopify_products=[shopify_product()])

    def test_rekey_is_pure_and_reversible(self):
        original_ids = [r["id"] for r in self.built["rows"]]
        by_sku = idsource.rekey(self.built, "sku")
        self.assertEqual([r["id"] for r in by_sku["rows"]], ["B-1", ""])
        self.assertEqual([r["id"] for r in self.built["rows"]], original_ids)  # input untouched
        back = idsource.rekey(by_sku, "default")
        self.assertEqual([r["id"] for r in back["rows"]], original_ids)
        self.assertEqual(back["clean"], 2)

    def test_sample_ids_lists_every_candidate_for_comparison(self):
        sample = idsource.sample_ids(self.built, n=1)
        self.assertEqual(sample, [{"Product": "Bench - Black", "Variant ID (default)": "901",
                                   "SKU": "B-1", "Product ID": "111"}])

    def test_sample_table_is_safe_for_scraped_text(self):
        hostile = shopify_product(variants=[
            {"id": 1, "title": "x`|y\n![t](https://evil.invalid/p.png)", "price": "1",
             "compare_at_price": None, "available": True, "sku": "A|B`C", "image_id": None}])
        summary, _, _ = build(platform="shopify", shopify_products=[hostile])
        table = idsource.sample_ids_markdown(summary)
        lines = table.splitlines()
        self.assertEqual(len(lines), 3)                       # header, separator, ONE data row
        self.assertTrue(all(line.count("|") == 5 for line in lines))   # 4 columns, no extra cells
        data = lines[2]
        self.assertNotIn("\n", data)
        # every cell is a code span, so the image syntax can never render
        self.assertTrue(all(cell.strip().startswith("`") for cell in data.strip("|").split("|")))

    def test_csv_bytes_match_the_written_file(self):
        summary, _, csv_text = build(platform="shopify", shopify_products=[shopify_product()])
        self.assertEqual(rows_to_csv_bytes(summary["rows"]).decode("utf-8"), csv_text)
        parsed = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual([r["id"] for r in parsed], ["901", "902"])


if __name__ == "__main__":
    unittest.main()
