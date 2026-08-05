"""
Fetches the full product catalog from a Shopify store's public /products.json
endpoint. This endpoint is exposed by default on every Shopify theme (it's
how the storefront itself renders products) and needs no API token.

Pagination: walks forward using since_id (Shopify's current recommended
method — fetch a page, note the highest product id seen, request the next
page with since_id=<that id>). Some storefronts silently ignore since_id
(e.g. a CDN caching the endpoint regardless of query string) and just keep
serving the same first page forever. We detect that stall — a page bringing
back zero products we haven't already seen — and fall back to the legacy
page= parameter instead of hammering the site until it rate-limits us.

Politeness delay between requests is handled centrally in _http.get() (it
factors in the store's own robots.txt, capped) — not duplicated here.
"""
from __future__ import annotations

from typing import Iterator

from .._http import get_json

PAGE_LIMIT = 250
MAX_PAGES = 1000  # safety cap (250k products) in case a store ignores all pagination strategies


def iter_products(base_url: str) -> Iterator[dict]:
    base_url = base_url.rstrip("/")
    seen_ids: set[int] = set()
    since_id = 0
    page = 1
    use_page_fallback = False

    for _ in range(MAX_PAGES):
        if use_page_fallback:
            url = f"{base_url}/products.json?limit={PAGE_LIMIT}&page={page}"
        else:
            url = f"{base_url}/products.json?limit={PAGE_LIMIT}&since_id={since_id}"

        data = get_json(url)
        if not data or "products" not in data:
            break

        products = data["products"]
        if not products:
            break

        new_products = [p for p in products if p["id"] not in seen_ids]

        if not new_products:
            if use_page_fallback:
                break  # page fallback stalled too — genuinely nothing left
            # since_id isn't advancing us — switch to page-based pagination,
            # starting from the next page (we've already consumed page 1's worth).
            use_page_fallback = True
            page = 2
            continue

        for p in new_products:
            seen_ids.add(p["id"])
            yield p

        if use_page_fallback:
            page += 1
        else:
            since_id = max(p["id"] for p in products)

        if len(products) < PAGE_LIMIT:
            break


def normalize(raw_product: dict, base_url: str) -> list[dict]:
    """
    A Shopify 'product' can have multiple variants (size/colour). Meta wants
    one feed row per sellable variant, grouped with item_group_id so Meta
    can treat them as one product with options.
    """
    rows = []
    handle = raw_product.get("handle", "")
    product_url = f"{base_url.rstrip('/')}/products/{handle}"
    images = raw_product.get("images", []) or []
    main_image = images[0]["src"] if images else raw_product.get("image", {}).get("src", "")
    brand = raw_product.get("vendor", "")
    title_base = raw_product.get("title", "")
    body_html = raw_product.get("body_html", "") or ""
    product_id = raw_product.get("id")

    variants = raw_product.get("variants", []) or []
    for v in variants:
        variant_title = v.get("title", "")
        full_title = title_base if variant_title in ("", "Default Title") else f"{title_base} - {variant_title}"

        available = v.get("available", False)
        current_price = v.get("price", "")
        compare_at = v.get("compare_at_price")

        # Shopify: `price` is what the customer actually pays right now;
        # `compare_at_price` is the pre-discount price, shown struck through.
        # Meta wants the opposite framing: `price` = regular/list price,
        # `sale_price` = the discounted price, only when actually on sale.
        on_sale = compare_at and float(compare_at) > float(current_price or 0)
        price = compare_at if on_sale else current_price
        sale_price = current_price if on_sale else ""

        # find variant-specific image if one exists
        image_link = main_image
        variant_image_id = v.get("image_id")
        if variant_image_id:
            for img in images:
                if img.get("id") == variant_image_id:
                    image_link = img.get("src", main_image)
                    break

        rows.append({
            "id": str(v.get("id")),
            "item_group_id": str(product_id),
            "title": full_title,
            "description": body_html,
            "availability": "in stock" if available else "out of stock",
            "condition": "new",
            "price": f"{price} " if price else "",  # currency appended by feed.py
            "sale_price": f"{sale_price} " if sale_price else "",
            "link": product_url,
            "image_link": image_link,
            "brand": brand or "",
            "sku": v.get("sku", ""),
        })

    return rows
