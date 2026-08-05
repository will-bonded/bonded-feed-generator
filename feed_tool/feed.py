from __future__ import annotations

import csv
from .normalize import META_FEED_COLUMNS, finalize_row, qc_row


def write_feed(rows: list[dict], output_path: str, default_currency: str = "GBP") -> dict:
    """
    Writes the Meta catalog CSV feed and returns a QC summary:
    {"total": int, "clean": int, "flagged": [{"id":..., "title":..., "problems": [...]}]}
    """
    clean_count = 0
    flagged = []
    finalized_rows = []

    for row in rows:
        final = finalize_row(row, default_currency=default_currency)
        problems = qc_row(final)
        if problems:
            flagged.append({"id": final.get("id"), "title": final.get("title"), "problems": problems})
        else:
            clean_count += 1
        finalized_rows.append(final)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=META_FEED_COLUMNS)
        writer.writeheader()
        writer.writerows(finalized_rows)

    return {
        "total": len(finalized_rows),
        "clean": clean_count,
        "flagged": flagged,
        "rows": finalized_rows,
    }
