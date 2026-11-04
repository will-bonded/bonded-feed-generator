"""
Body of the on-demand build workflow (.github/workflows/build-feed.yml): runs
feed_tool for ONE store and writes everything the web app needs into --out-dir,
which the workflow uploads as a short-lived artifact:

  feed.csv      the feed (header-only if nothing was found)
  summary.json  status + the run's flags (pushback, capped, counts...)
  ids.json      every row's candidate IDs, so the web app can switch the feed
                id source after the build without re-crawling
  run.log       the build log

A crash or an empty result still exits 0 with status recorded in summary.json,
so the artifact always uploads and the app can show a real message; only
invalid input (a bad job id) exits non-zero.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feed_tool.cli import build_feed
from feed_tool.security import SecurityError, validate_public_url

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_PAGES_CEILING = 5000   # the workflow input is a free-form string; don't trust it

_SUMMARY_KEYS = ("total", "clean", "robots_blocked_count", "pages_attempted",
                 "bot_pushback_detected", "candidates_total", "capped", "platform", "id_field")


class _Tee(io.TextIOBase):
    """Writes to the log buffer AND the real stdout, so the Actions log streams
    live instead of only appearing when the build ends."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for stream in self._streams:
            stream.write(s)
        return len(s)

    def flush(self):
        for stream in self._streams:
            stream.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--store-url", required=True)
    parser.add_argument("--id-field", default="default")
    parser.add_argument("--max-pages", default="2000")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    if not JOB_ID_RE.fullmatch(args.job_id):
        print("invalid job id", file=sys.stderr)
        return 2

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        max_pages = max(1, min(int(args.max_pages), MAX_PAGES_CEILING))
    except ValueError:
        max_pages = 2000

    log = io.StringIO()
    real_stdout = sys.stdout
    result = {"status": "ok", "job_id": args.job_id, "store_url": args.store_url}
    try:
        validate_public_url(args.store_url)  # defense in depth: same SSRF check the app applies
        with contextlib.redirect_stdout(_Tee(log, real_stdout)):
            summary = build_feed(args.store_url, str(out / "feed.csv"),
                                 max_pages=max_pages, id_field=args.id_field)
        result.update({k: summary.get(k) for k in _SUMMARY_KEYS})
        result["flagged_count"] = len(summary["flagged"])
        (out / "ids.json").write_text(json.dumps({
            "platform": summary["platform"],
            "candidates": summary["id_candidates"],
            "default_ids": summary["default_ids"],
        }), encoding="utf-8")
    except SecurityError as e:
        result.update(status="error", error=f"That URL isn't allowed: {e}")
    except Exception as e:  # report it to the app instead of failing the workflow
        result.update(status="error", error=f"{type(e).__name__}: {e}")
    finally:
        (out / "run.log").write_text(log.getvalue(), encoding="utf-8")
        (out / "summary.json").write_text(json.dumps(result), encoding="utf-8")

    print(f"[job] {result['status']}: {result.get('total', 0)} products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
