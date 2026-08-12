"""
Usage:
    python -m feed_tool.cli --url https://examplestore.com --output feed.csv

Detects the store's platform and builds a Meta Catalog-ready CSV feed.
"""
from __future__ import annotations

import argparse
import sys

from .detect import detect_platform
from .sources import shopify, crawl, generic
from ._http import get_text_with_status
from .feed import write_feed
from . import robots

# Floor for how many pages the content-based fallback retry (see
# _run_generic below) will sample, independent of --max-pages. That
# setting bounds the fast, hint-based pass, which the fallback retry only
# runs after concluding it can't be trusted — a site can list thousands of
# category pages before any individual product in its sitemap, so a
# fallback capped at the same (possibly much smaller) --max-pages the user
# set for the fast path can end up sampling nothing but categories and
# never reach a real product, even though real ones exist further in.
FALLBACK_RETRY_MIN_PAGES = 1000

# HTTP status codes commonly returned by bot-defense/rate-limiting
# infrastructure (Cloudflare, generic WAFs, API gateways). A run of these in
# a row is a clear, unambiguous signal — the site explicitly refusing the
# request, not a page that merely lacks product markup.
#
# Deliberately NOT also treating "many consecutive 200-OK-but-no-product
# pages" as a pushback signal, even though that's tempting: a real site's
# sitemap can legitimately cluster hundreds of non-product pages together
# with no blocking involved at all (seen in testing on a real Magento store
# whose sitemap lists ~900 category pages in a row before the next real
# product) — exactly the shape FALLBACK_RETRY_MIN_PAGES above exists to see
# past. A content-based "dry spell" threshold would either have to sit above
# that real depth (defeating its own purpose — rarely triggering in time to
# actually help) or below it (falsely aborting the exact fallback pass this
# tool relies on to find products in that shape of sitemap). Every response
# actually observed from that site during testing was a normal HTTP 200, no
# 403/429/503 ever seen — so there's no confirmed evidence a content-based
# signal would even be catching real blocking rather than ordinary structure.
_BOT_DEFENSE_STATUS_CODES = (403, 429, 503)

# How many consecutive bot-defense-status responses before concluding a run
# has been cut off, rather than just hitting the odd flaky request.
_BOT_STATUS_STREAK_THRESHOLD = 10


def build_feed(base_url: str, output_path: str, currency: str = "GBP",
                max_pages: int = 2000, url_contains=None) -> dict:
    robots.reset_block_count()  # isolate this run's count from any previous/concurrent one

    detection = detect_platform(base_url)
    platform = detection["platform"]
    print(f"[detect] platform={platform} — {detection['notes']}")

    rows: list[dict] = []
    # Only meaningfully known for the generic HTML-scraping path (one HTTP
    # request per candidate page) -- shopify pulls everything from one paged
    # JSON API, so "pages attempted" isn't a comparable count there.
    pages_attempted = None
    pushback_detected = False

    if platform == "shopify":
        count = 0
        for raw in shopify.iter_products(base_url):
            rows.extend(shopify.normalize(raw, base_url))
            count += 1
        print(f"[shopify] fetched {count} products ({len(rows)} variant rows)")

    elif platform == "woocommerce":
        print("[woocommerce] Store API extraction not yet wired up in this build — "
              "falling back to generic JSON-LD scraping.")
        rows, pages_attempted, pushback_detected = _run_generic(base_url, max_pages, url_contains)

    else:
        rows, pages_attempted, pushback_detected = _run_generic(base_url, max_pages, url_contains)

    blocked_count = robots.get_block_count()
    if blocked_count:
        print(f"[robots] skipped {blocked_count} request(s) this site's robots.txt disallows for us")

    if pushback_detected:
        # A more specific, more actionable diagnosis than any of the
        # messages below, whenever it applies -- supersedes them rather than
        # stacking alongside, since "the site cut us off partway through" is
        # a different situation from "robots.txt disallows this" or "no
        # signal found in what we fetched".
        if rows:
            print(f"[warn] this site appears to push back against automated access at volume — "
                  f"stopped early rather than continuing to hit the same wall. Delivering the "
                  f"{len(rows)} product(s) found before that happened (this feed is INCOMPLETE).")
        else:
            print("[warn] this site appears to push back against automated access at volume — "
                  "no products were captured before that happened.")
    elif not rows:
        # robots.txt is only the FULL explanation when it accounts for every
        # attempted page -- a small blocked_count next to a much larger
        # pages_attempted (e.g. 2 of 250) means most pages were fetched fine
        # and failed extraction for an unrelated reason, so blaming robots.txt
        # alone would be actively misleading about where to look next.
        if blocked_count and pages_attempted and blocked_count >= pages_attempted:
            print("[warn] No products extracted — this site's robots.txt explicitly disallows the "
                  "page(s) this tool needed to fetch. That's not a bug; the site has asked crawlers "
                  "not to access them, so there's nothing further this tool can do here.")
        elif blocked_count and pages_attempted:
            print(f"[warn] No products extracted — {blocked_count} of {pages_attempted} candidate page(s) "
                  f"were blocked by robots.txt, but the rest ({pages_attempted - blocked_count}) were "
                  f"fetched fine and still failed extraction. Site may block scraping in other ways, "
                  f"require JS rendering, or use a page structure this tool's extractor doesn't recognize.")
        elif blocked_count:
            print("[warn] No products extracted — this site's robots.txt explicitly disallows the "
                  "page(s) this tool needed to fetch. That's not a bug; the site has asked crawlers "
                  "not to access them, so there's nothing further this tool can do here.")
        else:
            print("[warn] No products extracted. Site may block scraping, require JS "
                  "rendering, or use a URL structure not covered by the default hints.")

    summary = write_feed(rows, output_path, default_currency=currency)
    summary["robots_blocked_count"] = blocked_count
    summary["pages_attempted"] = pages_attempted
    summary["bot_pushback_detected"] = pushback_detected
    return summary


def _extract_from_pages(urls: list[str]) -> tuple[list[dict], int, bool]:
    """Returns (rows, pages_attempted, pushback_detected). Stops short of
    len(urls) if a run of _BOT_STATUS_STREAK_THRESHOLD consecutive
    bot-defense-status responses suggests the site has cut this run off,
    rather than genuinely lacking product markup on the remaining pages."""
    rows = []
    consecutive_bot_defense = 0

    for i, url in enumerate(urls, 1):
        text, status = get_text_with_status(url)

        if status in _BOT_DEFENSE_STATUS_CODES:
            consecutive_bot_defense += 1
            if consecutive_bot_defense >= _BOT_STATUS_STREAK_THRESHOLD:
                return rows, i, True
        else:
            consecutive_bot_defense = 0

        if text:
            product = generic.extract_product_from_html(text, url)
            if product:
                rows.append(product)

        if i % 25 == 0:
            print(f"[generic] processed {i}/{len(urls)}")

    return rows, len(urls), False


def _run_generic(base_url: str, max_pages: int, url_contains) -> tuple[list[dict], int, bool]:
    print("[generic] discovering product URLs via sitemap.xml ...")
    discovery = crawl.discover_product_urls(base_url, url_contains=url_contains, max_pages=max_pages)
    urls = discovery["urls"]

    if discovery["used_content_fallback"]:
        print(f"[generic] the default product-URL hints matched nothing on this site — falling back "
              f"to checking {len(urls)} sitemap page(s) directly by content instead of guessing the "
              f"URL structure (slower). Pass --url-contains if you know the right path segment, to "
              f"skip straight to it.")
    else:
        print(f"[generic] found {len(urls)} candidate product URLs")

    if discovery["truncated"]:
        print(f"[warn] sitemap has MORE matching product URLs than --max-pages ({max_pages}) — "
              f"this feed is INCOMPLETE. Re-run with a higher --max-pages to capture the rest.")

    if discovery["discovery_time_capped"]:
        print(f"[warn] this site's sitemap structure was too large/slow to fully explore within the "
              f"discovery time budget — some sub-sitemaps were never checked, so this feed may be "
              f"INCOMPLETE regardless of --max-pages. Found {len(urls)} candidate(s) from what was "
              f"checked before stopping.")

    rows, attempted, pushback = _extract_from_pages(urls)
    print(f"[generic] extracted {len(rows)} products from {attempted} pages")

    if pushback:
        # Already cut off once on this same site in this same run -- a large
        # fallback retry right after would almost certainly just hit the same
        # wall again, wasting time rather than finding anything new.
        return rows, attempted, True

    # A non-empty hint match isn't proof the real catalog was found: a site's
    # actual catalog can use no hint keyword at all while one unrelated,
    # spurious URL elsewhere happens to contain one (seen in testing: a
    # Magento store's several-thousand-page flat .html catalog, plus a
    # single stray literal "/product/" URL that alone made the hint match
    # non-empty and skipped content-fallback entirely). If the hint-matched
    # pages extracted nothing real, retry via content-based extraction
    # across every other page found before giving up.
    if not rows and not discovery["used_content_fallback"]:
        fallback_cap = max(max_pages, FALLBACK_RETRY_MIN_PAGES)
        fallback_urls = [u for u in discovery["all_pages"] if u not in urls][:fallback_cap]
        if fallback_urls:
            print(f"[generic] the {len(urls)} hint-matched page(s) yielded no real products — "
                  f"retrying across {len(fallback_urls)} other sitemap page(s) by content "
                  f"instead of trusting the hint match (slower).")
            rows, fb_attempted, pushback = _extract_from_pages(fallback_urls)
            print(f"[generic] extracted {len(rows)} products from {fb_attempted} pages (fallback pass)")
            attempted += fb_attempted

    return rows, attempted, pushback


def main():
    parser = argparse.ArgumentParser(description="Build a Meta Catalog feed CSV from any store URL.")
    parser.add_argument("--url", required=True, help="Store base URL, e.g. https://examplestore.com")
    parser.add_argument("--output", default="feed.csv", help="Output CSV path")
    parser.add_argument("--currency", default="GBP", help="Default currency code if not detected per-item")
    parser.add_argument("--max-pages", type=int, default=2000,
                         help="Cap on generic-crawl product pages (safety limit, not a target — "
                              "the tool warns if the site has more than this)")
    parser.add_argument("--url-contains", nargs="*", default=None,
                         help="Path fragments that mark a product URL, e.g. /product/ /shop/")
    parser.add_argument("--google-sheet-id", default=None,
                         help="If set (with --google-credentials), also push the feed to this "
                              "Google Sheet (the sheet ID from its URL), in addition to the CSV.")
    parser.add_argument("--google-credentials", default=None,
                         help="Path to a Google service-account JSON key file. The target sheet "
                              "must be shared with that service account's email as an Editor.")
    args = parser.parse_args()

    summary = build_feed(
        args.url, args.output,
        currency=args.currency,
        max_pages=args.max_pages,
        url_contains=args.url_contains,
    )

    print(f"\n[done] wrote {summary['total']} rows to {args.output}")
    print(f"[qc]   {summary['clean']} clean / {len(summary['flagged'])} flagged")
    if summary["flagged"]:
        print("[qc]   first flagged rows:")
        for f in summary["flagged"][:10]:
            print(f"        - {f['id']} \"{f['title']}\": {', '.join(f['problems'])}")

    if args.google_sheet_id and args.google_credentials:
        from .sheets import write_to_google_sheet
        print(f"\n[sheets] pushing {summary['total']} rows to Google Sheet {args.google_sheet_id} ...")
        write_to_google_sheet(summary["rows"], args.google_sheet_id, args.google_credentials)
        print("[sheets] done")
    elif args.google_sheet_id or args.google_credentials:
        print("\n[warn] both --google-sheet-id and --google-credentials are required to push to "
              "Google Sheets — skipping (only one was provided).")

    if summary["total"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
