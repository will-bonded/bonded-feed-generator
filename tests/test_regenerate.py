import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "regenerate_feeds.py"
spec = importlib.util.spec_from_file_location("regenerate_feeds", SCRIPT)
regen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(regen)


def write_csv(path, n):
    path.write_text("id,title\n" + "".join(f"{i},Product {i}\n" for i in range(n)), encoding="utf-8")


def summary(total, pushback=False):
    return {"total": total, "clean": total, "flagged": [], "bot_pushback_detected": pushback}


class DecideReplacement(unittest.TestCase):
    def test_empty_run_keeps_the_live_feed_and_counts_as_a_failure(self):
        d = regen.decide_replacement(summary(0), previous_rows=100, shrink_skips=0)
        self.assertEqual((d.replace, d.failure), (False, True))

    def test_pushback_keeps_a_live_feed_but_is_not_a_prune_strike(self):
        d = regen.decide_replacement(summary(40, pushback=True), previous_rows=2000, shrink_skips=0)
        self.assertEqual((d.replace, d.failure, d.shrink_skip), (False, False, False))

    def test_pushback_with_nothing_live_yet_is_accepted(self):
        d = regen.decide_replacement(summary(40, pushback=True), previous_rows=0, shrink_skips=0)
        self.assertTrue(d.replace)

    def test_big_shrink_is_held_back_then_accepted_after_the_streak_limit(self):
        held = regen.decide_replacement(summary(60), previous_rows=100, shrink_skips=0)
        self.assertEqual((held.replace, held.shrink_skip), (False, True))
        accepted = regen.decide_replacement(
            summary(60), previous_rows=100, shrink_skips=regen.MAX_CONSECUTIVE_SHRINK_SKIPS)
        self.assertTrue(accepted.replace)

    def test_ordinary_and_growing_runs_replace(self):
        self.assertTrue(regen.decide_replacement(summary(95), 100, 0).replace)
        self.assertTrue(regen.decide_replacement(summary(250), 100, 0).replace)


class NightlyRun(unittest.TestCase):
    def run_nightly(self, entries, results, previous):
        """results: {store_url: summary dict | Exception}; previous: {feed_id: row count}.
        Returns (registry_after, feeds_dir_listing, build_calls, stdout)."""
        calls = []

        def fake_build_feed(store_url, out_path, **kwargs):
            calls.append((store_url, kwargs))
            result = results[store_url]
            if isinstance(result, Exception):
                raise result
            write_csv(Path(out_path), result["total"])
            return result

        old_cwd = os.getcwd()
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("feeds").mkdir()
                for feed_id, n in previous.items():
                    write_csv(Path("feeds") / f"{feed_id}.csv", n)
                Path("registry.json").write_text(json.dumps(entries), encoding="utf-8")
                with mock.patch.object(regen, "build_feed", fake_build_feed), \
                     contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                    regen.main()
                after = json.loads(Path("registry.json").read_text(encoding="utf-8"))
                files = {p.name: regen.count_csv_rows(p) for p in Path("feeds").iterdir()}
            finally:
                os.chdir(old_cwd)
        return after, files, calls, buf.getvalue()

    def test_a_bad_night_never_clobbers_a_live_feed(self):
        entries = [
            {"id": "healthy", "store_url": "https://a.invalid"},
            {"id": "empty", "store_url": "https://b.invalid"},
            {"id": "pushed", "store_url": "https://c.invalid"},
            {"id": "shrunk", "store_url": "https://d.invalid"},
        ]
        results = {
            "https://a.invalid": summary(120),
            "https://b.invalid": summary(0),
            "https://c.invalid": summary(30, pushback=True),
            "https://d.invalid": summary(20),
        }
        previous = {"healthy": 100, "empty": 100, "pushed": 100, "shrunk": 100}
        after, files, _, out = self.run_nightly(entries, results, previous)

        self.assertEqual(files, {"healthy.csv": 120, "empty.csv": 100,
                                 "pushed.csv": 100, "shrunk.csv": 100})
        by_id = {e["id"]: e for e in after}
        self.assertEqual(by_id["healthy"]["consecutive_empty_runs"], 0)
        self.assertEqual(by_id["empty"]["consecutive_empty_runs"], 1)    # counts toward pruning
        self.assertEqual(by_id["pushed"]["consecutive_empty_runs"], 0)   # does not
        self.assertEqual(by_id["shrunk"]["consecutive_skipped_runs"], 1)
        self.assertIn("kept the previous feed for pushed", out)

    def test_repeated_empty_or_erroring_nights_still_prune(self):
        entries = [
            {"id": "dying", "store_url": "https://a.invalid", "consecutive_empty_runs": 2},
            {"id": "erroring", "store_url": "https://b.invalid", "consecutive_empty_runs": 2},
            {"id": "flaky", "store_url": "https://c.invalid", "consecutive_empty_runs": 1},
        ]
        results = {"https://a.invalid": summary(0),
                   "https://b.invalid": RuntimeError("boom"),
                   "https://c.invalid": summary(0)}
        after, files, _, _ = self.run_nightly(
            entries, results, {"dying": 5, "erroring": 5, "flaky": 5})
        self.assertEqual([e["id"] for e in after], ["flaky"])
        self.assertEqual(set(files), {"flaky.csv"})

    def test_a_shrink_is_accepted_after_three_held_nights(self):
        entries = [{"id": "s", "store_url": "https://a.invalid", "consecutive_skipped_runs": 3}]
        after, files, _, _ = self.run_nightly(
            entries, {"https://a.invalid": summary(20)}, {"s": 100})
        self.assertEqual(files, {"s.csv": 20})
        self.assertEqual(after[0]["consecutive_skipped_runs"], 0)

    def test_the_registered_id_source_is_passed_to_the_build(self):
        entries = [{"id": "a", "store_url": "https://a.invalid", "id_field": "sku"},
                   {"id": "b", "store_url": "https://b.invalid"}]
        results = {"https://a.invalid": summary(10), "https://b.invalid": summary(10)}
        _, _, calls, _ = self.run_nightly(entries, results, {"a": 10, "b": 10})
        self.assertEqual(calls[0][1], {"id_field": "sku"})
        self.assertEqual(calls[1][1], {"id_field": "default"})   # entries registered before id_field existed


if __name__ == "__main__":
    unittest.main()
