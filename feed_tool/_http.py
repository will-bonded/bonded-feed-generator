from __future__ import annotations

import time
from urllib.parse import urljoin

import requests

from .security import validate_public_url, SecurityError
from . import robots

USER_AGENT = "BondedFeedBot/1.0 (+catalog feed generation for client store; contact: agency)"
# 10s was too tight: a real site hit during testing serves a (dynamically
# generated, not cached) sitemap endpoint that consistently takes ~11.5s to
# respond — a hard 10s timeout failed on it every single time, not just
# occasionally, silently losing real product data with no error surfaced
# beyond "unreachable". 20s gives real slow-but-legitimate endpoints enough
# room without waiting excessively long on something that's truly dead.
TIMEOUT = 20
MAX_REDIRECTS = 5
DEFAULT_REQUEST_DELAY = 0.3  # baseline politeness delay when a site's robots.txt asks for nothing more


def get(url: str, **kwargs):
    """
    Fetches `url`, re-validating every hop (including redirects) against
    SSRF rules — a redirect is otherwise a well-known way to slip an
    initially-valid public URL to an internal address after the fact.
    Returns None on any network error, non-http(s) scheme, a target that
    resolves to a private/reserved address, or a path robots.txt disallows
    for us — so callers can treat all of those as "unreachable" without
    their own try/except.

    Also enforces a politeness delay after each request: the site's own
    robots.txt Crawl-delay/Request-rate if longer than our baseline, but
    capped (see feed_tool/robots.py) rather than honored in full — see
    DECISIONS.md for why. This is the single place every outbound request
    in the tool passes through, so it applies uniformly rather than as a
    per-caller sleep() that's easy to forget to add somewhere new.
    """
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)

    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            validate_public_url(current_url)
        except SecurityError:
            return None

        checking_robots_txt_itself = robots.is_robots_txt_url(current_url)
        if not checking_robots_txt_itself and not robots.is_allowed(current_url, USER_AGENT):
            return None

        try:
            resp = requests.get(current_url, headers=headers, timeout=TIMEOUT,
                                 allow_redirects=False, **kwargs)
        except requests.RequestException:
            return None

        if not checking_robots_txt_itself:
            delay = max(DEFAULT_REQUEST_DELAY, robots.crawl_delay(current_url, USER_AGENT))
            time.sleep(delay)

        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            if not location:
                return resp
            current_url = urljoin(current_url, location)
            continue

        return resp

    return None  # too many redirects


def get_json(url: str):
    resp = get(url)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def get_text(url: str):
    resp = get(url)
    if resp is None or resp.status_code != 200:
        return None
    return resp.text


def get_text_with_status(url: str) -> tuple[str | None, int | None]:
    """Like get_text(), but also returns the HTTP status code (None if the
    request never completed at all — network error, robots-disallowed, or
    SSRF-invalid). Callers that need to tell "no content because nothing's
    there" apart from "no content because the site is actively refusing us"
    (e.g. a run of 403/429/503 responses) need the status code for that;
    get_text() alone collapses every case to None."""
    resp = get(url)
    if resp is None:
        return None, None
    text = resp.text if resp.status_code == 200 else None
    return text, resp.status_code
