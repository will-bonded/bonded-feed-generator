"""
Which product field becomes the feed `id` — and re-keying a finished build.

A Meta pixel's ViewContent/AddToCart `content_ids` have to match the catalog
item `id` for dynamic ads to work, and which field a store's pixel sends varies
by setup (variant ID, SKU, a parent product ID...). The default per platform is
the one that matched on the real store this was validated against (Shopify's
variant ID, with the product ID as item_group_id); this module lets the user
pick another source *after* seeing what each would look like.

Every source row carries its candidate IDs under `_ids` (never written to the
CSV). feed.build_summary collects them as `id_candidates`, so switching the ID
source is a local re-key of an already-built summary — no re-crawl.
"""
from __future__ import annotations

from collections import Counter

from .normalize import qc_row, _sanitize_for_csv

PLATFORM_SHOPIFY = "shopify"
PLATFORM_GENERIC = "generic"

DEFAULT = "default"
SKU = "sku"
PRODUCT_ID = "product_id"

# Shopify has no "Product ID" option on purpose: variants of one product would
# then all share an id, which Meta treats as duplicates — it would force a
# one-row-per-product collapse, and the product ID is already in the feed as
# item_group_id anyway.
_OPTIONS = {
    PLATFORM_SHOPIFY: (DEFAULT, SKU),
    PLATFORM_GENERIC: (DEFAULT, SKU, PRODUCT_ID),
}

LABELS = {DEFAULT: "Default", SKU: "SKU", PRODUCT_ID: "Product ID"}

# What each stashed candidate is called when shown to the user, in display order.
_CANDIDATE_LABELS = {
    PLATFORM_SHOPIFY: (("variant_id", "Variant ID (default)"), ("sku", "SKU"),
                       ("product_id", "Product ID")),
    PLATFORM_GENERIC: (("sku", "SKU"), ("product_id", "Product ID"), ("url", "Page URL")),
}


def platform_kind(platform: str) -> str:
    """detect.py's platforms collapse to the two source shapes that matter here:
    anything that isn't Shopify went through the generic page extractor."""
    return PLATFORM_SHOPIFY if platform == PLATFORM_SHOPIFY else PLATFORM_GENERIC


def options_for(platform: str) -> tuple[str, ...]:
    return _OPTIONS[platform_kind(platform)]


def effective_id_field(id_field: str | None, platform: str) -> tuple[str, str | None]:
    """Returns (field_to_use, warning_or_None). Unknown values are a bug and
    raise; a known value that just isn't offered for this platform degrades to
    the default with a warning rather than failing a build."""
    if id_field in (None, "", DEFAULT):
        return DEFAULT, None
    if id_field not in LABELS:
        raise ValueError(f"Unknown id_field {id_field!r} (expected one of {sorted(LABELS)})")
    if id_field not in options_for(platform):
        return DEFAULT, (f"ID source {LABELS[id_field]!r} isn't available for this kind of "
                         f"store — using the default.")
    return id_field, None


def rekey(summary: dict, id_field: str | None, platform: str | None = None) -> dict:
    """Returns a NEW summary whose row ids come from `id_field`, with QC, ID
    stats and the original ids (`default_ids`) recomputed/preserved — so it can
    be re-keyed again, to any option, from the result. Never mutates `summary`.

    Rows missing the chosen field are flagged ("missing SKU"), not filled from
    a different source: a feed mixing ID sources can't match a pixel sending
    only one of them, so a hole is more honest than a plausible-looking wrong id.
    """
    platform = platform or summary.get("platform") or PLATFORM_GENERIC
    field, _warning = effective_id_field(id_field, platform)

    rows_in = summary["rows"]
    candidates = summary.get("id_candidates") or [{} for _ in rows_in]
    default_ids = summary.get("default_ids") or [r.get("id", "") for r in rows_in]

    rows = []
    for row, cand, default_id in zip(rows_in, candidates, default_ids):
        new = dict(row)
        new["id"] = default_id if field == DEFAULT else _sanitize_for_csv(cand.get(field) or "")
        rows.append(new)

    id_counts = Counter(r["id"] for r in rows if r["id"])
    duplicated = {i for i, n in id_counts.items() if n > 1}

    flagged = []
    clean = 0
    url_fallback = 0
    for new, cand in zip(rows, candidates):
        problems = qc_row(new)
        if field != DEFAULT:
            problems = [f"missing {LABELS[field]}" if p == "missing id" else p for p in problems]
        if new["id"] and new["id"] in duplicated:
            problems.append("duplicate id")
        if field == DEFAULT and cand.get("url") and new["id"] == cand["url"]:
            url_fallback += 1
        if problems:
            flagged.append({"id": new["id"], "title": new["title"], "problems": problems})
        else:
            clean += 1

    out = dict(summary)
    out.update({
        "rows": rows,
        "flagged": flagged,
        "clean": clean,
        "total": len(rows),
        "id_field": field,
        "platform": platform,
        "default_ids": default_ids,
        "id_candidates": candidates,
        "duplicate_ids": len(duplicated),
        "id_url_fallback_count": url_fallback,
        "missing_id_count": sum(1 for r in rows if not r["id"]),
    })
    return out


def _md_cell(value, limit: int = 56) -> str:
    """One markdown table cell. Everything here was scraped from a third-party
    page, so it's rendered inside a code span: code spans are never parsed as
    links/images/HTML, and backticks, pipes and newlines (the only things that
    could break out of the span or the table) are neutralized first."""
    text = str(value).replace("`", "'").replace("|", "¦")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"`{text}`"


def sample_ids_markdown(summary: dict, n: int = 5) -> str:
    """sample_ids() as a markdown table — plain markdown on purpose: st.table /
    st.dataframe need pyarrow, which a Streamlit install can lack, and nothing
    else in the app depends on it."""
    sample = sample_ids(summary, n)
    if not sample:
        return ""
    headers = list(sample[0])
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    lines += ["| " + " | ".join(_md_cell(row[h]) for h in headers) + " |" for row in sample]
    return "\n".join(lines)


def sample_ids(summary: dict, n: int = 5) -> list[dict]:
    """First `n` products with every candidate ID side by side, for the user to
    compare against their pixel (e.g. in Meta Pixel Helper) before choosing."""
    platform = summary.get("platform") or PLATFORM_GENERIC
    labels = _CANDIDATE_LABELS[platform_kind(platform)]
    candidates = summary.get("id_candidates") or []
    out = []
    for row, cand in list(zip(summary["rows"], candidates))[:n]:
        entry = {"Product": row.get("title", "")}
        for key, label in labels:
            entry[label] = cand.get(key) or "—"
        out.append(entry)
    return out
