import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from feed_tool import cli, idsource, jobs
from feed_tool.security import SecurityError
from feed_tool.sources import shopify
from tests.helpers import shopify_product

REPO = "owner/repo"
JOB = "a" * 32
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_feed_job.py"
spec = importlib.util.spec_from_file_location("build_feed_job", SCRIPT)
job_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_script)


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(status="queued", conclusion=None, title=f"build {JOB}", created=NOW, **extra):
    return {"id": 1, "status": status, "conclusion": conclusion, "display_title": title,
            "created_at": iso(created), "updated_at": iso(created), **extra}


def patch_requests(*responses):
    """Each call to jobs.requests.request pops the next canned response."""
    queue = list(responses)
    return mock.patch.object(jobs.requests, "request", side_effect=lambda *a, **k: queue.pop(0))


class JobIds(unittest.TestCase):
    def test_ids_are_full_random_hex_and_validated(self):
        job_id = jobs.new_job_id()
        self.assertTrue(jobs.is_valid_job_id(job_id))
        self.assertEqual(len(job_id), 32)
        self.assertNotEqual(job_id, jobs.new_job_id())
        for bad in (None, "", "abc", "A" * 32, "g" * 32, "a" * 31, "../" + "a" * 29):
            self.assertFalse(jobs.is_valid_job_id(bad), bad)


class Dispatch(unittest.TestCase):
    def test_sends_the_workflow_inputs(self):
        with patch_requests(Resp(204)) as req:
            jobs.dispatch_build(REPO, "tok", JOB, "https://s.invalid", id_field="sku", max_pages=900)
        method, url = req.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/actions/workflows/build-feed.yml/dispatches"))
        self.assertEqual(req.call_args.kwargs["json"], {
            "ref": "main",
            "inputs": {"job_id": JOB, "store_url": "https://s.invalid", "id_field": "sku",
                       "max_pages": "900"}})

    def test_permission_and_missing_workflow_errors_say_what_to_fix(self):
        with patch_requests(Resp(403, {"message": "Resource not accessible"})):
            with self.assertRaisesRegex(jobs.JobError, "Actions read & write"):
                jobs.dispatch_build(REPO, "tok", JOB, "https://s.invalid")
        with patch_requests(Resp(404, {})):
            with self.assertRaisesRegex(jobs.JobError, "main branch"):
                jobs.dispatch_build(REPO, "tok", JOB, "https://s.invalid")

    def test_network_failure_becomes_a_job_error(self):
        with mock.patch.object(jobs.requests, "request", side_effect=jobs.requests.ConnectionError("down")):
            with self.assertRaisesRegex(jobs.JobError, "Could not reach GitHub"):
                jobs.dispatch_build(REPO, "tok", JOB, "https://s.invalid")


class Capacity(unittest.TestCase):
    def test_busy_when_too_many_builds_are_running(self):
        runs = [run("in_progress", created=NOW - timedelta(minutes=5)) for _ in range(jobs.MAX_CONCURRENT_BUILDS)]
        with patch_requests(Resp(200, {"workflow_runs": runs})):
            ok, reason = jobs.check_capacity(REPO, "tok", now=NOW)
        self.assertFalse(ok)
        self.assertIn("running right now", reason)

    def test_hourly_cap_counts_only_the_last_hour(self):
        old = [run("completed", "success", created=NOW - timedelta(hours=3)) for _ in range(50)]
        recent = [run("completed", "success", created=NOW - timedelta(minutes=10))
                  for _ in range(jobs.MAX_BUILDS_PER_HOUR)]
        with patch_requests(Resp(200, {"workflow_runs": old})):
            self.assertTrue(jobs.check_capacity(REPO, "tok", now=NOW)[0])
        with patch_requests(Resp(200, {"workflow_runs": old + recent})):
            ok, reason = jobs.check_capacity(REPO, "tok", now=NOW)
        self.assertFalse(ok)
        self.assertIn("last hour", reason)


class RunLookup(unittest.TestCase):
    def test_finds_the_run_by_its_title_and_none_while_it_hasnt_appeared(self):
        other = run(title="build " + "b" * 32)
        mine = {**run(), "id": 77}
        with patch_requests(Resp(200, {"workflow_runs": [other, mine]})):
            self.assertEqual(jobs.find_run(REPO, "tok", JOB)["id"], 77)
        with patch_requests(Resp(200, {"workflow_runs": [other]})):
            self.assertIsNone(jobs.find_run(REPO, "tok", JOB))

    def test_describe_run_states_and_elapsed(self):
        started = NOW - timedelta(minutes=3, seconds=12)
        self.assertEqual(jobs.describe_run(run("queued"), now=NOW)["state"], "queued")
        running = jobs.describe_run(run("in_progress", run_started_at=iso(started)), now=NOW)
        self.assertEqual((running["state"], running["elapsed_seconds"]), ("running", 192))
        self.assertEqual(jobs.describe_run(run("completed", "success"), now=NOW)["state"], "done")
        for conclusion in ("failure", "cancelled", "timed_out"):
            self.assertEqual(jobs.describe_run(run("completed", conclusion), now=NOW)["state"], "failed")

    def test_formatting_and_estimates(self):
        self.assertEqual(jobs.format_elapsed(192), "3m 12s")
        self.assertEqual(jobs.format_elapsed(45), "45s")
        self.assertEqual(jobs.estimate_range_minutes(400), (3, 8))
        self.assertEqual(jobs.estimate_range_minutes(10), (1, 1))


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


ARTIFACT = {"artifacts": [{"name": f"feed-{JOB}", "expired": False, "archive_download_url": "https://dl.invalid/z"}]}


class FetchResult(unittest.TestCase):
    def test_reads_every_file_in_the_artifact(self):
        blob = make_zip({"summary.json": json.dumps({"status": "ok", "total": 2}),
                         "feed.csv": "id,title\n1,A\n", "ids.json": json.dumps({"platform": "shopify"}),
                         "run.log": "hello"})
        with patch_requests(Resp(200, ARTIFACT), Resp(200, content=blob)):
            result = jobs.fetch_result(REPO, "tok", 1, JOB)
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["csv_bytes"], b"id,title\n1,A\n")
        self.assertEqual(result["ids"], {"platform": "shopify"})
        self.assertEqual(result["log"], "hello")

    def test_missing_expired_or_garbled_results_are_clear_errors(self):
        with patch_requests(Resp(200, {"artifacts": []})):
            with self.assertRaises(jobs.JobExpired):
                jobs.fetch_result(REPO, "tok", 1, JOB)
        with patch_requests(Resp(200, ARTIFACT), Resp(410)):
            with self.assertRaises(jobs.JobExpired):
                jobs.fetch_result(REPO, "tok", 1, JOB)
        with patch_requests(Resp(200, ARTIFACT), Resp(200, content=make_zip({"feed.csv": "x"}))):
            with self.assertRaisesRegex(jobs.JobError, "without producing a result"):
                jobs.fetch_result(REPO, "tok", 1, JOB)
        with patch_requests(Resp(200, ARTIFACT), Resp(200, content=b"not a zip")):
            with self.assertRaisesRegex(jobs.JobError, "couldn't be read"):
                jobs.fetch_result(REPO, "tok", 1, JOB)


class BuildScriptToWebApp(unittest.TestCase):
    """The script's output, fed through the app-side reader: the whole
    background path except the two GitHub calls."""

    def run_script(self, *, products=None, error=None, **argv_overrides):
        args = {"job-id": JOB, "store-url": "https://shop.invalid", "id-field": "default",
                "max-pages": "2000"}
        args.update({k.replace("_", "-"): v for k, v in argv_overrides.items()})
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(job_script, "validate_public_url",
                                                  side_effect=error or (lambda u: None)))
            stack.enter_context(mock.patch.object(
                cli, "detect_platform", return_value={"platform": "shopify", "notes": "t"}))
            stack.enter_context(mock.patch.object(
                shopify, "iter_products", side_effect=lambda b: iter(products or [shopify_product()])))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            argv = [x for k, v in args.items() for x in (f"--{k}", v)] + ["--out-dir", tmp]
            code = job_script.main(argv)
            files = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
        return code, files

    def test_outputs_round_trip_into_a_summary_the_app_can_re_key(self):
        code, files = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(set(files), {"feed.csv", "summary.json", "ids.json", "run.log"})
        meta = json.loads(files["summary.json"])
        self.assertEqual((meta["status"], meta["total"], meta["platform"]), ("ok", 2, "shopify"))

        result = {"summary": meta, "csv_bytes": files["feed.csv"],
                  "ids": json.loads(files["ids.json"]), "log": files["run.log"].decode()}
        summary = jobs.summary_from_artifact(result)
        self.assertEqual([r["id"] for r in summary["rows"]], ["901", "902"])
        self.assertEqual(summary["store_url"], "https://shop.invalid")
        # the ID source can be switched from the artifact alone — no re-crawl
        by_sku = idsource.rekey(summary, "sku")
        self.assertEqual([r["id"] for r in by_sku["rows"]], ["B-1", ""])
        self.assertEqual(by_sku["flagged"][0]["problems"], ["missing SKU"])

    def test_a_build_with_a_non_default_id_source_can_still_switch_back(self):
        _, files = self.run_script(id_field="sku")
        result = {"summary": json.loads(files["summary.json"]), "csv_bytes": files["feed.csv"],
                  "ids": json.loads(files["ids.json"]), "log": ""}
        summary = jobs.summary_from_artifact(result)
        self.assertEqual([r["id"] for r in summary["rows"]], ["B-1", ""])
        back = idsource.rekey(summary, "default")
        self.assertEqual([r["id"] for r in back["rows"]], ["901", "902"])

    def test_failures_are_reported_in_the_summary_not_as_a_crashed_job(self):
        code, files = self.run_script(error=SecurityError("private address"))
        self.assertEqual(code, 0)
        meta = json.loads(files["summary.json"])
        self.assertEqual(meta["status"], "error")
        self.assertIn("isn't allowed", meta["error"])

    def test_bad_job_ids_are_rejected(self):
        code, files = self.run_script(job_id="../../etc")
        self.assertEqual(code, 2)
        self.assertEqual(files, {})

    def test_max_pages_is_clamped(self):
        with mock.patch.object(job_script, "build_feed",
                               return_value={"flagged": [], "platform": "generic", "id_candidates": [],
                                             "default_ids": []}) as fake, \
             tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(job_script, "validate_public_url"), \
             contextlib.redirect_stdout(io.StringIO()):
            job_script.main(["--job-id", JOB, "--store-url", "https://x.invalid",
                             "--max-pages", "999999", "--out-dir", tmp])
        self.assertEqual(fake.call_args.kwargs["max_pages"], job_script.MAX_PAGES_CEILING)


if __name__ == "__main__":
    unittest.main()
