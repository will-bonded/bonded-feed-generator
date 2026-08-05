"""
SSRF (server-side request forgery) protection.

This tool makes outbound HTTP requests to a URL supplied by whoever is using
it — originally just the person running the CLI locally, now also anyone
typing a URL into the public web app. That makes it a textbook SSRF vector:
without validation, a malicious input could point the server at its own
cloud-metadata endpoint, an internal admin panel, or anything else reachable
from wherever this process runs.

validate_public_url() is called from _http.py before every single outbound
request (not just the first one a user types in) so it also covers redirects
and any follow-up requests the crawler/paginator makes on its own.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}

# Reserved/private ranges beyond what ipaddress.is_private already flags —
# most notably the cloud metadata address (169.254.169.254 falls under
# is_link_local already, but it's called out explicitly since it's the
# single most commonly exploited SSRF target).
_EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("192.0.0.0/24"),    # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),    # TEST-NET-1
    ipaddress.ip_network("198.18.0.0/15"),   # benchmarking
    ipaddress.ip_network("198.51.100.0/24"), # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("::ffff:0:0/96"),   # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),    # NAT64
]


class SecurityError(ValueError):
    """Raised when a URL fails SSRF/validation checks."""


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED_NETWORKS)


def validate_public_url(url: str) -> None:
    """Raises SecurityError if `url` isn't a plain http(s) URL resolving only
    to public IP addresses. Call this immediately before every request, not
    just once up front — DNS answers and redirect targets can both change
    between checks."""
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SecurityError(f"URL must start with http:// or https:// (got: {parsed.scheme or 'none'})")

    hostname = parsed.hostname
    if not hostname:
        raise SecurityError("URL has no hostname")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise SecurityError(f"Could not resolve hostname: {hostname}")

    resolved_ips = {info[4][0] for info in addr_infos}
    if not resolved_ips:
        raise SecurityError(f"Could not resolve hostname: {hostname}")

    for ip_str in resolved_ips:
        ip_str = ip_str.split("%")[0]  # strip IPv6 zone id if present
        ip = ipaddress.ip_address(ip_str)
        if not _is_public_ip(ip):
            raise SecurityError(
                f"'{hostname}' resolves to a non-public address ({ip}) — refusing to fetch it"
            )
