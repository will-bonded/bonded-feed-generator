"""
Nightly job body: re-runs the feed_tool pipeline for every store registered
in registry.json and refreshes feeds/{id}.csv. Invoked by
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

A night's result only REPLACES the live feed when it's trustworthy: Meta
treats the latest file as the source of truth, so overwriting yesterday's
complete feed with an empty or partial one (the site down, blocking us
mid-run, or a flaky night) can drop real products out of the catalogue.
See decide_replacement.
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

# Running this as `python scripts/regenerate_feeds.py` puts scripts/ (not the
# repo root) on sys.path[0], so feed_tool wouldn't otherwise be importable
# regardless of the invoker's own cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feed_tool.cli import build_feed

REGISTRY_PATH = Path("registry.json")
FEEDS_DIR = Path("feeds")
CONSECUTIVE_EMPTY_LIMIT = 3

# A night's feed must keep at least this fraction of the products already live
# to replace them. Below it the run is more likely a bad night than a real
# catalogue change — but a real clearance or range cut IS possible, so after
# MAX_CONSECUTIVE_SHRINK_SKIPS nights in a row the smaller feed is accepted.
SHRINK_KEEP_FRACTION = 0.7
MAX_CONSECUTIVE_SHRINK_SKIPS = 3


class Decision(NamedTuple):
    replace: bool            # overwrite the live feed with this run's result?
    reason: str | None       # why the live feed was kept (None when replacing)
    failure: bool            # counts toward auto-pruning (empty/errored nights)
    shrink_skip: bool        # kept only because it shrank (has its own streak limit)


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def decide_replacement(summary: dict, previous_rows: int, shrink_skips: int) -> Decision:
    total = summary["total"]
    if total == 0:
        return Decision(False, "the run came back with 0 products", True, False)

    # Pushback is deliberately NOT counted toward pruning: pruning deletes the
    # feed outright, and a stale-but-valid feed is better for the client than a
    # missing one while a site is only temporarily refusing us.
    if summary.get("bot_pushback_detected") and previous_rows > 0:
        return Decision(False, f"the site pushed back mid-run (only {total} products captured "
                               f"vs {previous_rows} in the live feed)", False, False)

    if previous_rows > 0 and total < SHRINK_KEEP_FRACTION * previous_rows \
            and shrink_skips < MAX_CONSECUTIVE_SHRINK_SKIPS:
        return Decision(False, f"it shrank from {previous_rows} to {total} products", False, True)

    return Decision(True, None, False, False)


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
            # Built into a temp file first: build_feed always writes its output,
            # and a bad night must not clobber the live feed before we've looked.
            with tempfile.TemporaryDirectory() as tmp:
                tmp_csv = Path(tmp) / "feed.csv"
                summary = build_feed(store_url, str(tmp_csv),
                                     id_field=entry.get("id_field", "default"))
                decision = decide_replacement(
                    summary, count_csv_rows(out_path), entry.get("consecutive_skipped_runs", 0))
                if decision.replace:
                    shutil.copyfile(tmp_csv, out_path)

            print(f"[nightly] {feed_id}: {summary['total']} rows "
                  f"({summary['clean']} clean, {len(summary['flagged'])} flagged)")
            if decision.reason:
                print(f"[nightly] kept the previous feed for {feed_id}: {decision.reason}")

            if decision.replace:
                entry["consecutive_skipped_runs"] = 0
            elif decision.shrink_skip:
                entry["consecutive_skipped_runs"] = entry.get("consecutive_skipped_runs", 0) + 1
            succeeded = not decision.failure
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
