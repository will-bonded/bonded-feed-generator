import unittest

from protego import Protego

from feed_tool import robots
from feed_tool._http import USER_AGENT
from tests.helpers import build, discovery, product_html, NON_PRODUCT_HTML

DOMAIN = "https://shop.invalid"


class RobotsBlockMessaging(unittest.TestCase):
    """A small blocked count next to many fetched-fine pages must not be blamed
    on robots.txt alone; only a fully blocked run gets the definitive message."""

    def setUp(self):
        self._saved = dict(robots._parsers)

    def tearDown(self):
        robots._parsers.clear()
        robots._parsers.update(self._saved)

    def run_case(self, robots_txt):
        robots._parsers[DOMAIN] = Protego.parse(robots_txt)
        urls = [f"{DOMAIN}/blocked/1", f"{DOMAIN}/blocked/2",
                f"{DOMAIN}/ok/1", f"{DOMAIN}/ok/2", f"{DOMAIN}/ok/3"]

        def fetch(url):
            if not robots.is_allowed(url, USER_AGENT):
                return None, None
            return NON_PRODUCT_HTML, 200

        return build(DOMAIN, discovery_result=discovery(urls), fetch=fetch)

    def test_partial_block_is_not_blamed_on_robots_alone(self):
        summary, out, _ = self.run_case("User-agent: *\nDisallow: /blocked/\n")
        self.assertEqual(summary["robots_blocked_count"], 2)
        self.assertEqual(summary["pages_attempted"], 5)
        self.assertEqual(summary["total"], 0)
        self.assertNotIn("nothing further this tool can do here", out)
        self.assertIn("2 of 5 candidate page(s)", out)

    def test_full_block_gets_the_definitive_message(self):
        summary, out, _ = self.run_case("User-agent: *\nDisallow: /\n")
        self.assertEqual(summary["robots_blocked_count"], summary["pages_attempted"])
        self.assertIn("nothing further this tool can do here", out)


class HintMatchFallback(unittest.TestCase):
    """One stray URL matching a hint keyword must not starve the content
    fallback when the site's real catalog uses none."""

    SPURIOUS = f"{DOMAIN}/product/"
    REAL = f"{DOMAIN}/adjustable-dumbbell-set.html"

    def test_retries_across_the_broader_page_set(self):
        pages = {self.SPURIOUS: NON_PRODUCT_HTML, self.REAL: product_html("Dumbbell", sku="DB-1")}
        summary, out, _ = build(
            DOMAIN,
            discovery_result=discovery([self.SPURIOUS], all_pages=[self.SPURIOUS, self.REAL]),
            fetch=lambda u: (pages.get(u), 200))
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["pages_attempted"], 2)
        self.assertEqual(summary["candidates_total"], 2)
        self.assertFalse(summary["capped"])
        self.assertIn("yielded no real products", out)

    def _category_heavy(self, **kwargs):
        # a real product buried 900 category pages deep, 600 more category pages after it
        categories = [f"{DOMAIN}/category-{i}.html" for i in range(1500)]
        real = f"{DOMAIN}/real-product.html"
        all_pages = [self.SPURIOUS] + categories[:900] + [real] + categories[900:]
        fetched = []

        def fetch(url):
            fetched.append(url)
            return (product_html() if url == real else NON_PRODUCT_HTML), 200

        summary, out, _ = build(
            DOMAIN, discovery_result=discovery([self.SPURIOUS], all_pages=all_pages),
            fetch=fetch, **kwargs)
        return summary, [u for u in fetched if u != self.SPURIOUS]

    def test_default_floor_reaches_past_a_small_max_pages(self):
        summary, fetched = self._category_heavy(max_pages=50)
        self.assertEqual(summary["total"], 1)       # found the product 900 pages in
        self.assertEqual(len(fetched), 1000)        # floor, not max_pages
        self.assertTrue(summary["capped"])          # 1501 existed, only 1000 sampled

    def test_explicit_fallback_cap_keeps_a_quick_build_quick(self):
        summary, fetched = self._category_heavy(max_pages=50, fallback_cap=300)
        self.assertEqual(len(fetched), 300)
        self.assertEqual(summary["total"], 0)       # the product is past the cap...
        self.assertTrue(summary["capped"])          # ...and the result says so


class CappedRuns(unittest.TestCase):
    def test_truncated_discovery_reports_capped_and_the_catalog_size(self):
        urls = [f"{DOMAIN}/p/{i}" for i in range(3)]
        summary, _, _ = build(
            DOMAIN, discovery_result=discovery(urls, truncated=True, candidates_total=1850),
            fetch=lambda u: (product_html(sku=u[-1]), 200))
        self.assertTrue(summary["capped"])
        self.assertEqual(summary["candidates_total"], 1850)
        self.assertFalse(summary["bot_pushback_detected"])

    def test_complete_run_is_not_capped(self):
        urls = [f"{DOMAIN}/p/{i}" for i in range(3)]
        summary, _, _ = build(DOMAIN, discovery_result=discovery(urls),
                              fetch=lambda u: (product_html(sku=u[-1]), 200))
        self.assertFalse(summary["capped"])
        self.assertEqual(summary["total"], 3)


class BotPushback(unittest.TestCase):
    NORMAL = [f"{DOMAIN}/normal-{i}.html" for i in range(5)]
    BLOCKED = [f"{DOMAIN}/blocked-{i}.html" for i in range(15)]
    NEVER = [f"{DOMAIN}/never-{i}.html" for i in range(10)]

    def test_sustained_403_streak_stops_the_run_and_skips_the_fallback(self):
        urls = self.NORMAL + self.BLOCKED + self.NEVER
        fetched = []

        def fetch(url):
            fetched.append(url)
            return (None, 403) if url in self.BLOCKED else (NON_PRODUCT_HTML, 200)

        summary, out, _ = build(DOMAIN, discovery_result=discovery(urls), fetch=fetch)
        self.assertTrue(summary["bot_pushback_detected"])
        self.assertEqual(summary["pages_attempted"], 15)            # 5 + the 10-in-a-row threshold
        self.assertFalse(any(u in fetched for u in self.NEVER))     # never fetched
        self.assertNotIn("retrying across", out)                    # no fallback pass
        self.assertIn("push back against automated access", out)
        self.assertIn(USER_AGENT, out)                              # allowlist ask is shown

    def test_partial_results_are_delivered_not_discarded(self):
        good = [f"{DOMAIN}/good-{i}.html" for i in range(3)]
        urls = good + self.BLOCKED
        summary, out, _ = build(
            DOMAIN, discovery_result=discovery(urls),
            fetch=lambda u: ((None, 403) if u in self.BLOCKED else (product_html(sku=u[-6:]), 200)))
        self.assertTrue(summary["bot_pushback_detected"])
        self.assertEqual(summary["total"], 3)
        self.assertIn("this feed is INCOMPLETE", out)

    def test_a_short_blip_does_not_trigger_it(self):
        blip = [f"{DOMAIN}/blip-{i}.html" for i in range(3)]
        real = f"{DOMAIN}/real.html"
        summary, _, _ = build(
            DOMAIN, discovery_result=discovery(blip + [real]),
            fetch=lambda u: ((None, 403) if u in blip else (product_html(), 200)))
        self.assertFalse(summary["bot_pushback_detected"])
        self.assertEqual(summary["total"], 1)


if __name__ == "__main__":
    unittest.main()
