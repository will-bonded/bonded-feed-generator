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
from ._http import get_text
from .feed import write_feed
from . import robots


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

    if platform == "shopify":
        count = 0
        for raw in shopify.iter_products(base_url):
            rows.extend(shopify.normalize(raw, base_url))
            count += 1
        print(f"[shopify] fetched {count} products ({len(rows)} variant rows)")

    elif platform == "woocommerce":
        print("[woocommerce] Store API extraction not yet wired up in this build — "
              "falling back to generic JSON-LD scraping.")
        rows, pages_attempted = _run_generic(base_url, max_pages, url_contains)

    else:
        rows, pages_attempted = _run_generic(base_url, max_pages, url_contains)

    blocked_count = robots.get_block_count()
    if blocked_count:
        print(f"[robots] skipped {blocked_count} request(s) this site's robots.txt disallows for us")

    if not rows:
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
    return summary


def _run_generic(base_url: str, max_pages: int, url_contains) -> tuple[list[dict], int]:
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

    rows = []
    for i, url in enumerate(urls, 1):
        html = get_text(url)
        if not html:
            continue
        product = generic.extract_product_from_html(html, url)
        if product:
            rows.append(product)
        if i % 25 == 0:
            print(f"[generic] processed {i}/{len(urls)}")

    print(f"[generic] extracted {len(rows)} products from {len(urls)} pages")
    return rows, len(urls)


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
