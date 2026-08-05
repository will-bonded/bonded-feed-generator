"""
Meta Catalog feed column schema and row-level QC.
Reference: Meta Commerce Manager data feed spec.
"""
from __future__ import annotations

META_FEED_COLUMNS = [
    "id",
    "title",
    "description",
    "availability",
    "condition",
    "price",
    "sale_price",
    "link",
    "image_link",
    "brand",
    "item_group_id",
    "sku",
]

REQUIRED_FIELDS = ["id", "title", "availability", "condition", "price", "link", "image_link"]

DEFAULT_CURRENCY = "GBP"

# Excel/Sheets treats a cell starting with any of these as a formula to
# evaluate, not literal text. Every field here comes from scraping a
# third-party site, so a product title/description/SKU containing one of
# these (accidentally or by a hostile page) would otherwise execute as a
# formula for anyone who opens the CSV. Prefixing with a plain apostrophe
# is the standard mitigation — it's the same character Excel's own "format
# as text" convention uses to force literal interpretation.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_for_csv(value: str) -> str:
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def finalize_row(row: dict, default_currency: str = DEFAULT_CURRENCY) -> dict:
    """Ensures every column exists, price strings carry a currency code, and
    no field can be interpreted as a spreadsheet formula when opened."""
    out = {col: row.get(col, "") for col in META_FEED_COLUMNS}

    for price_field in ("price", "sale_price"):
        val = (out.get(price_field) or "").strip()
        if val and not any(c.isalpha() for c in val):
            # numeric only, no currency code attached yet
            out[price_field] = f"{val} {default_currency}"

    for col in META_FEED_COLUMNS:
        out[col] = _sanitize_for_csv(out[col] or "")

    return out


def qc_row(row: dict) -> list[str]:
    """Returns a list of problems with this row (empty list = clean)."""
    problems = []
    for field in REQUIRED_FIELDS:
        if not row.get(field):
            problems.append(f"missing {field}")
    return problems
