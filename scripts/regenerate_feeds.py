"""
Nightly job body: re-runs the feed_tool pipeline for every store registered
in registry.json and overwrites feeds/{id}.csv. Invoked by
.github/workflows/nightly-regenerate.yml, which does the actual git commit
(a single batch commit for everything this script touched, not one per
feed) after this script exits — including this script's own rewrite of
registry.json (updated run-streak counters, and any entries it pruned).

One store's failure doesn't stop the rest — logged to stderr and summarized
at the end, so a client site being temporarily down doesn't block everyone
else's daily refresh. A store that's unsuccessful (errors, or comes back
with 0 products) for CONSECUTIVE_EMPTY_LIMIT nights running is treated as
abandoned/dead and auto-pruned — dropped from registry.json and its stale
feeds/{id}.csv deleted — rather than left to sit forever consuming a slot
against the registration cap.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Running this as `python scripts/regenerate_feeds.py` puts scripts/ (not the
# repo root) on sys.path[0], so feed_tool wouldn't otherwise be importable
# regardless of the invoker's own cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feed_tool.cli import build_feed

REGISTRY_PATH = Path("registry.json")
FEEDS_DIR = Path("feeds")
CONSECUTIVE_EMPTY_LIMIT = 3


def main() -> int:
    entries = json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig")) if REGISTRY_PATH.exists() else []
    FEEDS_DIR.mkdir(exist_ok=True)

    if not entries:
        print("[nightly] registry.json has no entries — nothing to do.")
        return 0

    print(f"[nightly] regenerating {len(entries)} registered feed(s)...")
    failures = []
    pruned = []
    remaining = []

    for entry in entries:
        feed_id = entry["id"]
        store_url = entry["store_url"]
        out_path = FEEDS_DIR / f"{feed_id}.csv"
        print(f"\n[nightly] --- {feed_id} ({store_url}) ---")

        succeeded = False
        try:
            summary = build_feed(store_url, str(out_path))
            succeeded = summary["total"] > 0
            print(f"[nightly] {feed_id}: {summary['total']} rows "
                  f"({summary['clean']} clean, {len(summary['flagged'])} flagged)")
        except Exception as e:
            failures.append({"id": feed_id, "store_url": store_url, "error": str(e)})
            print(f"[nightly] FAILED {feed_id} ({store_url}): {e}", file=sys.stderr)

        if succeeded:
            entry["consecutive_empty_runs"] = 0
            remaining.append(entry)
            continue

        entry["consecutive_empty_runs"] = entry.get("consecutive_empty_runs", 0) + 1
        if entry["consecutive_empty_runs"] < CONSECUTIVE_EMPTY_LIMIT:
            remaining.append(entry)
            continue

        pruned.append(entry)
        out_path.unlink(missing_ok=True)
        print(f"[nightly] pruning {feed_id} ({store_url}) — "
              f"{entry['consecutive_empty_runs']} consecutive unsuccessful runs")

    REGISTRY_PATH.write_text(json.dumps(remaining, indent=2) + "\n", encoding="utf-8")

    print(f"\n[nightly] done: {len(entries) - len(failures)}/{len(entries)} feeds regenerated successfully")
    if failures:
        print(f"[nightly] {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f['id']} ({f['store_url']}): {f['error']}")
    if pruned:
        print(f"[nightly] pruned {len(pruned)} feed(s) after "
              f"{CONSECUTIVE_EMPTY_LIMIT}+ consecutive unsuccessful runs:")
        for p in pruned:
            print(f"  - {p['id']} ({p['store_url']})")

    return 0  # non-fatal: still commit whichever feeds DID regenerate successfully


if __name__ == "__main__":
    sys.exit(main())
