from __future__ import annotations

import csv
import io

from .normalize import META_FEED_COLUMNS, finalize_row, qc_row


def build_summary(rows: list[dict], default_currency: str = "GBP") -> dict:
    """
    Finalizes and QC-checks rows without writing anything. Returns:
    {"total": int, "clean": int, "flagged": [{"id":..., "title":..., "problems": [...]}],
     "rows": [finalized row dicts], "id_candidates": [per-row dict of every field
     that could serve as the feed id, aligned with `rows`]}

    `id_candidates` carries the fields each source stashed under `_ids` (which
    never reach the CSV) so the ID source can be switched after the build —
    see idsource.rekey.
    """
    clean_count = 0
    flagged = []
    finalized_rows = []
    id_candidates = []

    for row in rows:
        final = finalize_row(row, default_currency=default_currency)
        problems = qc_row(final)
        if problems:
            flagged.append({"id": final.get("id"), "title": final.get("title"), "problems": problems})
        else:
            clean_count += 1
        finalized_rows.append(final)
        id_candidates.append(row.get("_ids", {}))

    return {
        "total": len(finalized_rows),
        "clean": clean_count,
        "flagged": flagged,
        "rows": finalized_rows,
        "id_candidates": id_candidates,
    }


def write_csv(output_path: str, finalized_rows: list[dict]) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=META_FEED_COLUMNS)
        writer.writeheader()
        writer.writerows(finalized_rows)


def rows_to_csv_bytes(finalized_rows: list[dict]) -> bytes:
    """Same bytes write_csv would put in a file — for the web app, which
    re-keys a finished build locally and needs fresh CSV content without a
    round trip through the filesystem."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=META_FEED_COLUMNS)
    writer.writeheader()
    writer.writerows(finalized_rows)
    return buf.getvalue().encode("utf-8")


def write_feed(rows: list[dict], output_path: str, default_currency: str = "GBP") -> dict:
    """Writes the Meta catalog CSV feed and returns build_summary()'s result."""
    summary = build_summary(rows, default_currency=default_currency)
    write_csv(output_path, summary["rows"])
    return summary
