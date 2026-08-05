"""
Fallback product extraction for sites with no known public product API.

Priority order per page:
1. JSON-LD schema.org/Product blocks (<script type="application/ld+json">)
   — most reliable when present, since it's structured specifically for
   search engines and usually kept accurate for SEO reasons.
2. Open Graph / meta tags (og:title, og:image, product:price:amount, etc.)
   — weaker fallback, often missing availability/SKU.

This module only extracts from a page ALREADY FETCHED. Discovering the list
of product URLs to visit (sitemap.xml, category pages, etc.) is handled by
crawl.py, since discovery strategy varies a lot site to site.
"""
from __future__ import annotations

import json
from bs4 import BeautifulSoup


def _flatten_ld_products(node) -> list[dict]:
    """JSON-LD can nest Products inside @graph, or be a list, or a single dict."""
    found = []
    if isinstance(node, list):
        for item in node:
            found.extend(_flatten_ld_products(item))
    elif isinstance(node, dict):
        node_type = node.get("@type", "")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "Product" in types:
            found.append(node)
        if "@graph" in node:
            found.extend(_flatten_ld_products(node["@graph"]))
    return found


def extract_jsonld_products(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        results.extend(_flatten_ld_products(data))
    return results


def _price_from_offers(offers) -> tuple[str, str]:
    """Returns (price, availability)."""
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return "", ""
    price = str(offers.get("price", "") or offers.get("lowPrice", ""))
    currency = offers.get("priceCurrency", "")
    availability_raw = offers.get("availability", "")
    in_stock = "InStock" in str(availability_raw)
    price_str = f"{price} {currency}".strip() if price else ""
    return price_str, ("in stock" if in_stock else "out of stock")


def normalize_jsonld(product: dict, page_url: str) -> dict:
    price, availability = _price_from_offers(product.get("offers", {}))
    image = product.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")

    brand = product.get("brand", "")
    if isinstance(brand, dict):
        brand = brand.get("name", "")

    return {
        "id": str(product.get("sku") or product.get("productID") or page_url),
        "item_group_id": "",
        "title": product.get("name", ""),
        "description": product.get("description", ""),
        "availability": availability or "in stock",
        "condition": "new",
        "price": price,
        "sale_price": "",
        "link": page_url,
        "image_link": image,
        "brand": brand,
        "sku": product.get("sku", ""),
        "_source": "jsonld",
    }


def extract_opengraph_product(html: str, page_url: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag.get("content", "").strip() if tag else ""

    title = meta("og:title")
    if not title:
        return None

    og_type = meta("og:type")
    price = meta("product:price:amount") or meta("og:price:amount")

    # A page having an og:title doesn't make it a product — category/listing
    # pages usually carry one too. Require an actual product signal:
    # og:type="product" (the standard Open Graph convention for this) or a
    # price tag. Otherwise this quietly produced a "product" row for every
    # category page a broad --url-contains hint happened to match.
    if og_type != "product" and not price:
        return None

    currency = meta("product:price:currency") or meta("og:price:currency")
    availability_raw = meta("product:availability")
    image = meta("og:image")
    description = meta("og:description")
    brand = meta("product:brand") or meta("og:brand")
    sku = meta("product:retailer_item_id") or meta("product:item_group_id")

    return {
        "id": sku or page_url,
        "item_group_id": "",
        "title": title,
        "description": description,
        "availability": "in stock" if (not availability_raw or "in stock" in availability_raw.lower()) else "out of stock",
        "condition": "new",
        "price": f"{price} {currency}".strip() if price else "",
        "sale_price": "",
        "link": page_url,
        "image_link": image,
        "brand": brand,
        "sku": sku,
        "_source": "opengraph",
    }


def _pick_best_jsonld_product(ld_products: list[dict]) -> dict:
    """A page can carry more than one Product block — e.g. a near-empty stub
    (just name/url, maybe for breadcrumbs) alongside the full one with
    price/SKU/image. Prefer whichever actually has offer/price data instead
    of blindly taking the first block found."""
    for p in ld_products:
        price, _ = _price_from_offers(p.get("offers", {}))
        if price:
            return p
    return ld_products[0]


def extract_product_from_html(html: str, page_url: str) -> dict | None:
    """Try JSON-LD first, fall back to Open Graph. Returns None if neither found."""
    ld_products = extract_jsonld_products(html)
    if ld_products:
        return normalize_jsonld(_pick_best_jsonld_product(ld_products), page_url)

    og_product = extract_opengraph_product(html, page_url)
    if og_product:
        return og_product

    return None
