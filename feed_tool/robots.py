"""
robots.txt compliance — deliberately partial by design, not full compliance:

- Disallow rules ARE respected (skip fetching any path a site has
  explicitly blocked) — this is the part that actually matters for not
  scraping somewhere you've been asked not to, and it costs nothing in
  speed.
- Crawl-delay / Request-rate ARE read, but capped at MAX_CRAWL_DELAY
  rather than honored in full — some platforms (e.g. SAP Commerce/Hybris
  storefronts) specify delays of 10s+ that would turn a several-hundred
  product catalog into an hours-long run. See DECISIONS.md for the
  reasoning and the residual risk this accepts: a site enforcing a longer
  delay than we're honoring may throttle or slow-walk responses to us.

Uses `protego` (the same robots.txt parser Scrapy uses) rather than the
stdlib `urllib.robotparser`, which turned out to have two real problems
against actual production robots.txt files: no support for the `*`
wildcard extension that most Disallow rules rely on in practice (e.g.
`Disallow: */cart`), and a blank line between a `User-agent:` line and its
`Disallow:` lines silently orphans that whole block, dropping the rules
entirely. protego is pure Python (no compiled deps) and handles both
correctly.

robots.txt itself is fetched through _http.get_text() — not any parser's
own fetcher — so it still goes through this tool's SSRF validation.
"""
from __future__ import annotations

import threading
from urllib.parse import urlparse

from protego import Protego

MAX_CRAWL_DELAY = 1.5  # seconds — hard cap regardless of what a site requests

_parsers: dict[str, Protego] = {}

# Thread-local (not a bare module global): the Streamlit app can run several
# users' build_feed() calls concurrently in the same process, each on its
# own thread, and this must not leak between them.
_stats = threading.local()


def reset_block_count() -> None:
    """Call once at the start of a single scrape run, so counts from a
    previous run (or another concurrent one) don't bleed into this one."""
    _stats.blocked_count = 0


def get_block_count() -> int:
    return getattr(_stats, "blocked_count", 0)


def _record_block() -> None:
    _stats.blocked_count = getattr(_stats, "blocked_count", 0) + 1


def _get_parser(url: str) -> Protego:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in _parsers:
        from ._http import get_text  # deferred: avoids a circular import with _http.py

        text = get_text(f"{origin}/robots.txt")
        _parsers[origin] = Protego.parse(text or "")

    return _parsers[origin]


def is_robots_txt_url(url: str) -> bool:
    return urlparse(url).path == "/robots.txt"


def is_allowed(url: str, user_agent: str) -> bool:
    try:
        allowed = _get_parser(url).can_fetch(url, user_agent)
    except Exception:
        return True  # fail open — a parsing hiccup shouldn't block legitimate scraping
    if not allowed:
        _record_block()
    return allowed


def crawl_delay(url: str, user_agent: str) -> float:
    try:
        rp = _get_parser(url)
        delay = rp.crawl_delay(user_agent)
        if delay is None:
            rate = rp.request_rate(user_agent)
            if rate:
                delay = rate.seconds / rate.requests
        if delay is None:
            return 0.0
        return min(float(delay), MAX_CRAWL_DELAY)
    except Exception:
        return 0.0
