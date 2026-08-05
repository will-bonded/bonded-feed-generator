"""
Detects which product-catalog strategy to use for a given site URL.

Strategy priority:
1. Shopify   -> public /products.json endpoint (no auth needed, on by default)
2. WooCommerce -> public Store API at /wp-json/wc/store/v1/products (on by default in modern WooCommerce)
3. generic   -> fall back to crawling category/product pages and reading
                JSON-LD (schema.org/Product) or Open Graph meta tags
"""
from __future__ import annotations

from urllib.parse import urljoin

from ._http import get as _get


def detect_platform(base_url: str) -> dict:
    """
    Returns {"platform": "shopify"|"woocommerce"|"generic", "base_url": str, "notes": str}
    """
    base_url = base_url.rstrip("/")

    # --- Shopify check ---
    shopify_probe = urljoin(base_url + "/", "products.json?limit=1")
    resp = _get(shopify_probe)
    if resp is not None and resp.status_code == 200:
        try:
            data = resp.json()
            if isinstance(data, dict) and "products" in data:
                return {
                    "platform": "shopify",
                    "base_url": base_url,
                    "notes": "Public /products.json endpoint responded with valid product data.",
                }
        except ValueError:
            pass

    # --- WooCommerce Store API check ---
    woo_probe = urljoin(base_url + "/", "wp-json/wc/store/v1/products?per_page=1")
    resp = _get(woo_probe)
    if resp is not None and resp.status_code == 200:
        try:
            data = resp.json()
            if isinstance(data, list):
                return {
                    "platform": "woocommerce",
                    "base_url": base_url,
                    "notes": "Public WooCommerce Store API responded with valid product data.",
                }
        except ValueError:
            pass

    # --- Fallback ---
    return {
        "platform": "generic",
        "base_url": base_url,
        "notes": "No known public product API detected — will fall back to JSON-LD / Open Graph scraping.",
    }
