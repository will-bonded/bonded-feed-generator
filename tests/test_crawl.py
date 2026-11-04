import types
import unittest
from unittest import mock

from feed_tool import robots
from feed_tool.sources import crawl


def urlset(*urls):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + body + "</urlset>")


def sitemapindex(*urls):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + body + "</sitemapindex>")


def discover(files, declared=(), base="https://shop.invalid", **kwargs):
    """Runs discovery against an in-memory {url: xml} site."""
    with mock.patch.object(crawl, "get_text", side_effect=lambda u: files.get(u)), \
         mock.patch.object(robots, "declared_sitemaps", return_value=list(declared)):
        return crawl.discover_product_urls(base, **kwargs)


class SitemapLocation(unittest.TestCase):
    def test_path_prefixed_url_still_finds_the_domain_root_sitemap(self):
        files = {"https://shop.invalid/sitemap.xml": urlset("https://shop.invalid/shop/a")}
        result = discover(files, base="https://shop.invalid/uk/home")
        self.assertEqual(result["urls"], ["https://shop.invalid/shop/a"])

    def test_robots_declared_sitemap_used_when_the_default_is_missing(self):
        files = {"https://shop.invalid/sitemap_index.xml": urlset("https://shop.invalid/shop/b")}
        result = discover(files, declared=["https://shop.invalid/sitemap_index.xml"])
        self.assertEqual(result["urls"], ["https://shop.invalid/shop/b"])

    def test_extensionless_nested_sitemaps_are_recursed_into(self):
        nested = "https://shop.invalid/feeds/sitemap-and-route/sitemaps/products/se/0"
        files = {"https://shop.invalid/sitemap.xml": sitemapindex(nested),
                 nested: urlset("https://shop.invalid/products/real")}
        self.assertEqual(discover(files)["urls"], ["https://shop.invalid/products/real"])

    def test_sitemap_url_detection(self):
        self.assertTrue(crawl._is_sitemap_url("https://x.test/sitemap.xml"))
        self.assertTrue(crawl._is_sitemap_url("https://x.test/Product-en-GBP-1.xml?context=a"))
        self.assertTrue(crawl._is_sitemap_url("https://x.test/feeds/sitemap-and-route/sitemaps/p/0"))
        self.assertFalse(crawl._is_sitemap_url("https://x.test/products/widget"))


class PageFiltering(unittest.TestCase):
    def test_asset_urls_are_not_pages(self):
        for u in ("a.png", "a.jpeg", "a.css", "a.pdf", "a.woff2"):
            self.assertFalse(crawl._looks_like_page(f"https://x.test/products/{u}"), u)
        self.assertTrue(crawl._looks_like_page("https://x.test/products/widget"))

    def test_extensionless_image_paths_are_not_pages(self):
        self.assertFalse(crawl._looks_like_page("https://x.test/on/demandware.static/Images/products/a"))

    def test_assets_dont_become_candidates_just_because_they_match_a_hint(self):
        files = {"https://shop.invalid/sitemap.xml": urlset(
            "https://shop.invalid/products/widget",
            "https://shop.invalid/Images/products/widget.png")}
        self.assertEqual(discover(files)["urls"], ["https://shop.invalid/products/widget"])

    def test_off_domain_pages_and_sitemaps_are_ignored(self):
        files = {"https://shop.invalid/sitemap.xml": urlset(
            "https://shop.invalid/shop/own", "https://sibling.invalid/shop/theirs")}
        self.assertEqual(discover(files)["urls"], ["https://shop.invalid/shop/own"])
        # a sibling brand's sitemap declared in a shared robots.txt is never followed
        files = {"https://sibling.invalid/sitemap.xml": urlset("https://sibling.invalid/shop/x")}
        self.assertEqual(discover(files, declared=["https://sibling.invalid/sitemap.xml"])["urls"], [])


class HintMatching(unittest.TestCase):
    def test_sitemap_file_path_counts_as_a_hint_for_the_pages_inside_it(self):
        products = "https://shop.invalid/sitemaps/products/se/0"
        stories = "https://shop.invalid/sitemaps/stories/se/0"
        files = {
            "https://shop.invalid/sitemap.xml": sitemapindex(products, stories),
            products: urlset("https://shop.invalid/se/green-suspenders-a01", "https://shop.invalid/se/socks-a02"),
            stories: urlset("https://shop.invalid/se/how-to-wear-a-suit", "https://shop.invalid/se/our-stores"),
        }
        result = discover(files)
        self.assertFalse(result["used_content_fallback"])
        self.assertEqual(set(result["urls"]), {"https://shop.invalid/se/green-suspenders-a01",
                                               "https://shop.invalid/se/socks-a02"})

    def test_zero_hint_matches_falls_back_to_every_page(self):
        files = {"https://shop.invalid/sitemap.xml": urlset("https://shop.invalid/a.html", "https://shop.invalid/b.html")}
        result = discover(files)
        self.assertTrue(result["used_content_fallback"])
        self.assertEqual(len(result["urls"]), 2)

    def test_identical_sitemaps_dont_double_count_pages(self):
        same = urlset("https://shop.invalid/a.html", "https://shop.invalid/b.html")
        files = {"https://shop.invalid/sitemap.xml": same, "https://shop.invalid/pub/sitemap.xml": same}
        result = discover(files, declared=["https://shop.invalid/pub/sitemap.xml"])
        self.assertEqual(sorted(result["all_pages"]),
                         ["https://shop.invalid/a.html", "https://shop.invalid/b.html"])

    def test_all_pages_and_candidate_total_are_not_capped_by_max_pages(self):
        pages = [f"https://shop.invalid/products/{i}" for i in range(7)]
        result = discover({"https://shop.invalid/sitemap.xml": urlset(*pages)}, max_pages=3)
        self.assertEqual(len(result["urls"]), 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["candidates_total"], 7)
        self.assertEqual(len(result["all_pages"]), 7)


class DiscoveryTimeBudget(unittest.TestCase):
    def test_budget_exceeded_mid_traversal_stops_but_keeps_what_was_found(self):
        subs = [f"https://shop.invalid/sub/{i}.xml" for i in range(3)]
        files = {"https://shop.invalid/sitemap.xml": sitemapindex(*subs)}
        for i, s in enumerate(subs):
            files[s] = urlset(f"https://shop.invalid/products/widget-{i}")
        # start, index, first sub-sitemap, then the clock jumps past the budget
        clock = iter([0, 0, 0, 1000])
        fake_time = types.SimpleNamespace(monotonic=lambda: next(clock, 1000))
        with mock.patch.object(crawl, "time", fake_time):
            result = discover(files)
        self.assertTrue(result["discovery_time_capped"])
        self.assertEqual(result["urls"], ["https://shop.invalid/products/widget-0"])

    def test_ordinary_discovery_is_not_flagged(self):
        result = discover({"https://shop.invalid/sitemap.xml": urlset("https://shop.invalid/products/a")})
        self.assertFalse(result["discovery_time_capped"])


if __name__ == "__main__":
    unittest.main()
