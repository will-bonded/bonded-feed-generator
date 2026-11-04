"""Shared helpers: run cli.build_feed fully offline against fake discovery and
fake page fetches. Run the whole suite from the repo root with:

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from unittest import mock

from feed_tool import cli, robots
from feed_tool.sources import crawl, shopify

NON_PRODUCT_HTML = "<html><body>a category page with no product markup</body></html>"


def product_html(name="Real Product", sku="RP-1", price="19.99", product_id=None):
    data = {
        "@context": "https://schema.org", "@type": "Product", "name": name, "sku": sku,
        "image": "https://shop.invalid/img.jpg",
        "offers": {"price": price, "priceCurrency": "GBP",
                   "availability": "https://schema.org/InStock"},
    }
    if product_id:
        data["productID"] = product_id
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(data) + "</script></head><body>x</body></html>")


def discovery(urls, all_pages=None, **overrides):
    result = {
        "urls": list(urls),
        "all_pages": list(urls if all_pages is None else all_pages),
        "candidates_total": len(urls),
        "truncated": False,
        "used_content_fallback": False,
        "discovery_time_capped": False,
    }
    result.update(overrides)
    return result


def shopify_product(product_id=111, variants=None):
    variants = variants if variants is not None else [
        {"id": 901, "title": "Black", "price": "10.00", "compare_at_price": None,
         "available": True, "sku": "B-1", "image_id": None},
        {"id": 902, "title": "Red", "price": "10.00", "compare_at_price": None,
         "available": True, "sku": "", "image_id": None},
    ]
    return {"id": product_id, "handle": "bench", "title": "Bench", "vendor": "Brand",
            "body_html": "", "images": [{"id": 1, "src": "https://shop.invalid/i.jpg"}],
            "variants": variants}


def make_artifact(job_id="a" * 32, *, products=None, platform="shopify", error=None, **argv_overrides):
    """Runs scripts/build_feed_job.py offline and returns what the web app would
    fetch from the finished build: {"summary", "csv_bytes", "ids", "log"} — the
    same dict jobs.fetch_result returns (None if the script rejected its input)."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "build_feed_job.py"
    spec = importlib.util.spec_from_file_location("build_feed_job", script)
    job_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job_script)

    args = {"job-id": job_id, "store-url": "https://shop.invalid", "id-field": "default",
            "max-pages": "2000"}
    args.update({k.replace("_", "-"): v for k, v in argv_overrides.items()})
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            job_script, "validate_public_url", side_effect=error or (lambda u: None)))
        stack.enter_context(mock.patch.object(
            cli, "detect_platform", return_value={"platform": platform, "notes": "t"}))
        stack.enter_context(mock.patch.object(
            shopify, "iter_products",
            side_effect=lambda b: iter(products if products is not None else [shopify_product()])))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        argv = [x for k, v in args.items() for x in (f"--{k}", v)] + ["--out-dir", tmp]
        code = job_script.main(argv)
        files = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
    if code != 0:
        return None
    return {"summary": json.loads(files["summary.json"]), "csv_bytes": files.get("feed.csv", b""),
            "ids": json.loads(files["ids.json"]) if "ids.json" in files else None,
            "log": files["run.log"].decode("utf-8")}


def build(base="https://shop.invalid", *, platform="generic", discovery_result=None,
          fetch=None, shopify_products=None, **kwargs):
    """Runs cli.build_feed offline. `fetch(url) -> (html_or_None, status_or_None)`
    stands in for get_text_with_status. Returns (summary, stdout_text, csv_text)."""
    robots.reset_block_count()
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            cli, "detect_platform", return_value={"platform": platform, "notes": "test"}))
        if discovery_result is not None:
            stack.enter_context(mock.patch.object(
                crawl, "discover_product_urls", return_value=discovery_result))
        if fetch is not None:
            stack.enter_context(mock.patch.object(cli, "get_text_with_status", side_effect=fetch))
        if shopify_products is not None:
            stack.enter_context(mock.patch.object(
                shopify, "iter_products", side_effect=lambda base_url: iter(shopify_products)))
        out = os.path.join(tmp, "feed.csv")
        with contextlib.redirect_stdout(buf):
            summary = cli.build_feed(base, out, **kwargs)
        with open(out, encoding="utf-8", newline="") as f:
            csv_text = f.read()
    return summary, buf.getvalue(), csv_text
