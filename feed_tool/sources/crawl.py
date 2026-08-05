"""
Discovers candidate product page URLs on a site with no known product API.

Strategy: read sitemap.xml (and any nested sitemaps it points to), and keep
URLs that look like product pages (containing common path segments like
/product/, /products/, /shop/, /p/). This is a heuristic — every site's URL
structure differs (a jewellery site might use /jewellery/, a furniture site
/living-room/, and so on forever) — so the CLI also accepts an explicit
--url-contains filter and a hard --max-pages cap.

No fixed hint list can anticipate every site's naming scheme, so when the
hints (default or user-supplied) match nothing at all, this falls back to
returning every non-sitemap URL found instead of failing outright — letting
the content-based extractor (which requires an actual product signal:
JSON-LD Product type, or og:type="product"/a price tag) sort out what's
really a product instead of guessing from the URL. This only engages on a
true zero-match outcome, so it has no effect on sites the hints already
handle correctly — it's strictly a fallback for the "found nothing" case,
not a replacement for hint-based filtering where that already works.
"""
from __future__ import annotations

from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from .._http import get_text

DEFAULT_PRODUCT_HINTS = ["/product/", "/products/", "/shop/", "/p/", "/item/"]


def _is_sitemap_url(u: str) -> bool:
    # Check the URL's path, not the raw string — some platforms (e.g. SAP
    # Commerce/Hybris) append a query string after the .xml extension, like
    # .../Product-en-GBP-123.xml?context=..., which a plain
    # u.endswith(".xml") misses entirely.
    return urlparse(u).path.endswith(".xml")


def _parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Returns (page_urls, nested_sitemap_urls)."""
    soup = BeautifulSoup(xml_text, "lxml-xml")
    urls = [loc.get_text(strip=True) for loc in soup.find_all("loc")]
    sitemaps = [u for u in urls if _is_sitemap_url(u)]
    pages = [u for u in urls if not _is_sitemap_url(u)]
    return pages, sitemaps


def discover_product_urls(
    base_url: str,
    url_contains: list[str] | None = None,
    max_pages: int = 500,
) -> dict:
    """
    Returns {"urls": [...], "truncated": bool, "used_content_fallback": bool}.
    `truncated` is True when more matching URLs existed than max_pages
    allowed — the caller should surface this rather than silently shipping
    an incomplete feed. `used_content_fallback` is True when the hints
    matched nothing and every non-sitemap URL found was returned instead —
    the caller should let the content-based extractor do the real filtering
    in that case, and may want to say so, since it's slower.
    """
    base_url = base_url.rstrip("/")
    hints = url_contains or DEFAULT_PRODUCT_HINTS

    to_check = [f"{base_url}/sitemap.xml"]
    seen_sitemaps = set()
    all_pages: list[str] = []
    hinted_pages: list[str] = []

    while to_check:
        sitemap_url = to_check.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)

        xml_text = get_text(sitemap_url)
        if not xml_text:
            continue

        pages, nested = _parse_sitemap(xml_text)
        to_check.extend(urljoin(base_url + "/", n) for n in nested if n not in seen_sitemaps)

        for p in pages:
            all_pages.append(p)
            if any(hint in p.lower() for hint in hints):
                hinted_pages.append(p)

    used_content_fallback = not hinted_pages and bool(all_pages)
    candidates = all_pages if used_content_fallback else hinted_pages

    return {
        "urls": candidates[:max_pages],
        "truncated": len(candidates) > max_pages,
        "used_content_fallback": used_content_fallback,
    }
