"""
Discovers candidate product page URLs on a site with no known product API.

Strategy: read sitemap.xml (and any nested sitemaps it points to), and keep
URLs that look like product pages (containing common path segments like
/product/, /products/, /shop/, /p/). This is a heuristic — every site's URL
structure differs (a jewellery site might use /jewellery/, a furniture site
/living-room/, and so on forever) — so the CLI also accepts an explicit
--url-contains filter and a hard --max-pages cap.

sitemap.xml (and robots.txt) are domain-root conventions, so discovery
always normalizes to scheme://netloc for those lookups rather than
whatever path the user's URL happened to include (a locale home page like
.../uk/home would otherwise look for a sitemap under that path and 404
even when one exists at the real root). Not every site has a sitemap at
the conventional /sitemap.xml path at all — many only declare one (or
several locale-specific ones) via robots.txt's Sitemap: directive, which
is checked too.

Nested sitemaps aren't always .xml-suffixed either — some platforms
paginate them as extension-less routes (e.g.
.../feeds/sitemap-and-route/sitemaps/products/se/0) — so anything with
"sitemap" appearing in a path segment is also treated as more sitemap
content to recurse into, not a final product-page candidate.

No fixed hint list can anticipate every site's naming scheme, so when the
hints (default or user-supplied) match nothing at all, this falls back to
returning every non-sitemap URL found instead of failing outright — letting
the content-based extractor (which requires an actual product signal:
JSON-LD Product type, or og:type="product"/a price tag) sort out what's
really a product instead of guessing from the URL. This only engages on a
true zero-match outcome, so it has no effect on sites the hints already
handle correctly — it's strictly a fallback for the "found nothing" case,
not a replacement for hint-based filtering where that already works.

A hint can also match something spurious rather than nothing at all: a
site whose real catalog uses no hint keyword can still have one stray,
unrelated URL that happens to contain one, which makes the hint-matched
set non-empty without containing any real products. `all_pages` (every
non-sitemap page found, hint-matched or not) is returned alongside `urls`
so the caller can retry via content-based extraction if the hint-matched
pages turn out to yield nothing real, rather than treating a non-empty
hint match as proof the real catalog was found.

A sitemap can list more than HTML product pages: image assets (with or
without a recognizable file extension — some platforms serve them through
a dynamic URL with none at all) are filtered out, since they'd otherwise
false-positive-match a hint like /products/ just from appearing in an
image folder path.

Everything discovered is also restricted to the same domain as the URL
the user gave. robots.txt is checked for Sitemap: directives (some sites
have no sitemap at the conventional path at all, only one declared there),
but a shared robots.txt for a multi-brand company can legitimately declare
sitemaps for entirely different sibling sites — following those would mix
an unrelated brand's whole catalog into what's supposed to be a feed for
the one store being asked about.
"""
from __future__ import annotations

import time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from .. import robots
from .._http import get_text

DEFAULT_PRODUCT_HINTS = ["/product/", "/products/", "/shop/", "/p/", "/item/"]

# max_pages only bounds the final candidate count, not how many sitemap
# FILES get fetched to build it — a site can have dozens of sub-sitemaps
# with wildly different response times (a real one hit during testing: most
# sub-sitemaps ~0.7s, but its "products" ones ~12s each, dynamically
# generated rather than cached, across many locales). A fetch-count cap
# wouldn't reliably bound total time given that variance, so this bounds
# wall-clock time spent on discovery instead — generous enough that a
# normal site's discovery (usually a few seconds) never comes close, but a
# hard backstop against a pathologically large/slow sitemap structure
# turning "build a feed" into an open-ended wait.
DISCOVERY_TIME_BUDGET_SECONDS = 90

# A sitemap can list more than HTML pages — Salesforce Commerce Cloud (and
# others) include product IMAGE assets in the same sitemap, often under a
# path like /Images/products/..., which naively matches the "/products/"
# hint despite not being a page at all. No real product page ever ends in
# one of these, regardless of what platform generated the sitemap.
_NON_PAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff", ".avif",
    ".pdf", ".csv", ".zip", ".doc", ".docx", ".xls", ".xlsx",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".mp3", ".webm", ".avi", ".mov",
    ".css", ".js", ".json",
)


def _looks_like_page(u: str) -> bool:
    path = urlparse(u).path.lower()
    if path.endswith(_NON_PAGE_EXTENSIONS):
        return False
    # Some platforms (Salesforce Commerce Cloud / Demandware) serve image
    # assets through a dynamic URL with no file extension at all — an
    # "/images/" path segment is still a strong, low-false-positive signal
    # this isn't a real page, extension or not.
    return "images" not in path.split("/")


def _is_sitemap_url(u: str) -> bool:
    path = urlparse(u).path.lower()
    # Check the URL's path, not the raw string — some platforms (e.g. SAP
    # Commerce/Hybris) append a query string after the .xml extension, like
    # .../Product-en-GBP-123.xml?context=..., which a plain
    # u.endswith(".xml") misses entirely.
    if path.endswith(".xml"):
        return True
    # Some platforms paginate sub-sitemaps as extension-less routes instead
    # of .xml files (e.g. .../feeds/sitemap-and-route/sitemaps/products/se/0)
    # — "sitemap" appearing within a path segment (not necessarily as the
    # whole segment — "sitemaps", "sitemap-and-route" etc. all count) is
    # still a strong, low-false-positive signal that this is more sitemap
    # content to recurse into, not a final product page.
    return any("sitemap" in segment for segment in path.split("/"))


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
    Returns {"urls": [...], "all_pages": [...], "truncated": bool,
    "used_content_fallback": bool, "discovery_time_capped": bool}.
    `truncated` is True when more matching URLs existed than max_pages
    allowed — the caller should surface this rather than silently shipping
    an incomplete feed. `used_content_fallback` is True when the hints
    matched nothing and every non-sitemap URL found was returned instead —
    the caller should let the content-based extractor do the real filtering
    in that case, and may want to say so, since it's slower.
    `all_pages` is every non-sitemap page found, regardless of hint
    matching — a superset of `urls` when hints DID match something. A
    hint can match a single spurious URL on a site whose real catalog uses
    no hint keyword at all (seen in testing: a Magento store's sitemap
    had thousands of flat, keyword-free product/category URLs plus one
    unrelated literal "/product/" page, and that one match alone was
    enough to skip content-fallback and miss the entire real catalog) — so
    the caller should retry against `all_pages` via content-based
    extraction if the hint-matched `urls` turn out to yield nothing real,
    rather than trusting a hint match just because it's non-empty.
    `discovery_time_capped` is True when DISCOVERY_TIME_BUDGET_SECONDS was
    hit before every known sitemap file was fetched — a distinct condition
    from `truncated`: that one means "found more candidates than you asked
    for", this one means "the site's sitemap structure itself was too
    large/slow to fully traverse", and the two can happen independently.
    """
    base_url = base_url.rstrip("/")
    hints = url_contains or DEFAULT_PRODUCT_HINTS

    # sitemap.xml and robots.txt are domain-root conventions — if the user
    # gave a URL with a path (e.g. a locale home page like .../uk/home),
    # {base_url}/sitemap.xml would look under that path, not the site's
    # actual root, and 404 even when a real sitemap exists at the root.
    parsed = urlparse(base_url)
    domain_root = f"{parsed.scheme}://{parsed.netloc}"

    to_check = [f"{domain_root}/sitemap.xml"]
    if base_url != domain_root:
        to_check.append(f"{base_url}/sitemap.xml")  # still worth trying, cheap if it 404s

    # Not every site has a sitemap at the conventional path at all — many
    # only declare one (or several locale-specific ones) via robots.txt's
    # Sitemap: directive instead, which is the standard way to point
    # crawlers at a non-default location. Restricted to the same domain —
    # a multi-brand company's shared robots.txt can legitimately declare
    # sitemaps for entirely different sibling sites (seen in testing: a
    # skincare brand's robots.txt also listing two of its sibling brands'
    # sitemaps), and following those would mix an unrelated brand's whole
    # catalog into what's supposed to be a feed for the one store the user
    # asked about.
    for sitemap_url in robots.declared_sitemaps(f"{domain_root}/"):
        if urlparse(sitemap_url).netloc != parsed.netloc:
            continue
        if sitemap_url not in to_check:
            to_check.append(sitemap_url)

    seen_sitemaps = set()
    seen_pages: set[str] = set()
    all_pages: list[str] = []
    hinted_pages: list[str] = []
    discovery_time_capped = False
    discovery_start = time.monotonic()

    while to_check:
        if time.monotonic() - discovery_start > DISCOVERY_TIME_BUDGET_SECONDS:
            discovery_time_capped = True
            break

        sitemap_url = to_check.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)

        xml_text = get_text(sitemap_url)
        if not xml_text:
            continue

        pages, nested = _parse_sitemap(xml_text)
        to_check.extend(
            urljoin(domain_root + "/", n) for n in nested
            if n not in seen_sitemaps and urlparse(n).netloc in ("", parsed.netloc)
        )

        # Some sites name their sitemap FILES by content type
        # (".../sitemaps/products/se/0" vs ".../sitemaps/stories/se/0") but
        # give individual PAGE urls flat, locale-prefixed slugs with no hint
        # keyword in them at all (e.g. /se/green-suspenders-a01090) — the
        # exact same shape as their CMS story pages (/se/how-to-wear-a-suit),
        # so no page-URL hint can tell them apart. When the sitemap file's own
        # path already matches a hint, that's a strong, low-false-positive
        # signal from the site's own naming that everything inside it is the
        # real thing, even though none of the individual page URLs would
        # match on their own.
        sitemap_itself_hinted = any(hint in sitemap_url.lower() for hint in hints)

        for p in pages:
            if urlparse(p).netloc not in ("", parsed.netloc):
                continue  # off-domain page — see the robots.txt cross-domain note above
            if not _looks_like_page(p):
                continue
            # Some sites declare the exact same sitemap content at more than
            # one URL (seen in testing: a Magento store's /sitemap.xml and
            # /pub/sitemap.xml were byte-identical) — without this, every
            # page in it gets queued for extraction twice.
            if p in seen_pages:
                continue
            seen_pages.add(p)
            all_pages.append(p)
            if sitemap_itself_hinted or any(hint in p.lower() for hint in hints):
                hinted_pages.append(p)

    used_content_fallback = not hinted_pages and bool(all_pages)
    candidates = all_pages if used_content_fallback else hinted_pages

    return {
        "urls": candidates[:max_pages],
        # Deliberately NOT capped to max_pages like `urls` is: its purpose is
        # to give the caller something BROADER to retry against when the
        # hint-matched `urls` turn out to be wrong (see the docstring above)
        # — capping it to the same limit the hint pass already used would
        # defeat that (seen in testing: a site's sitemap lists thousands of
        # category pages before any individual product, so the first
        # max_pages entries of a capped list were almost all categories,
        # never reaching real products that existed further in). The 90s
        # discovery time budget already bounds how much this can grow to.
        "all_pages": all_pages,
        "truncated": len(candidates) > max_pages,
        "used_content_fallback": used_content_fallback,
        "discovery_time_capped": discovery_time_capped,
    }
