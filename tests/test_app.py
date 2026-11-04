"""Drives the real streamlit_app.py headlessly with Streamlit's AppTest. Each test
runs a clean temp copy of the app from a clean working directory, so a local
.streamlit/secrets.toml can never leak into (or out of) a test."""
import contextlib
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from feed_tool import cli, idsource, jobs, registry
from feed_tool.feed import build_summary, write_csv
from feed_tool.security import SecurityError
from feed_tool.sources import shopify
from tests.helpers import make_artifact, shopify_product

APP_SOURCE = Path(__file__).resolve().parent.parent / "streamlit_app.py"
JOB = "c" * 32
STORE = "https://shop.invalid"


def fake_build_feed(**flags):
    """A stand-in for cli.build_feed returning a real, fully-shaped summary."""
    empty = flags.pop("empty", False)

    def _build(url, out_path, **kwargs):
        rows = [] if empty else shopify.normalize(shopify_product(), url)
        summary = build_summary(rows)
        summary["platform"] = "shopify"
        summary = idsource.rekey(summary, "default", "shopify")
        write_csv(out_path, summary["rows"])
        summary.update(robots_blocked_count=0, pages_attempted=None, bot_pushback_detected=False,
                       candidates_total=None, capped=False)
        summary.update(flags)
        return summary
    return _build


@contextlib.contextmanager
def app(*, turnstile=False, github=False, background=False, build=None, query=None):
    """Yields (AppTest, stack). `build` replaces cli.build_feed; `query` seeds the URL."""
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        shutil.copy(APP_SOURCE, Path(tmp) / "streamlit_app.py")
        os.chdir(tmp)
        stack.callback(os.chdir, old_cwd)
        stack.enter_context(mock.patch("feed_tool.security.validate_public_url", lambda u: None))
        stack.enter_context(mock.patch("feed_tool.turnstile.verify_turnstile", lambda t, s: True))
        stack.enter_context(mock.patch.object(cli, "build_feed", build or fake_build_feed()))
        at = AppTest.from_file(str(Path(tmp) / "streamlit_app.py"), default_timeout=30)
        if turnstile:
            at.secrets["turnstile_site_key"] = "site"
            at.secrets["turnstile_secret_key"] = "secret"
        if github:
            at.secrets["github_repo"] = "owner/repo"
            at.secrets["github_token"] = "tok"
        if background:
            at.secrets["background_builds_enabled"] = True
        for key, value in (query or {}).items():
            at.query_params[key] = value
        yield at, stack


def verified_return(flow="oneoff", mode="inapp"):
    """The query string Turnstile's callback navigates the page to."""
    return {"turnstile_token": "tok", "url": STORE, "permission": "1", "flow": flow, "mode": mode}


def submit_form(at):
    at.run()
    at.text_input[0].set_value(STORE)
    at.checkbox[0].check()
    at.button[0].click().run()


def texts(elements):
    return [e.value for e in elements]


def buttons(at):
    return {b.label: b for b in at.button}


class FormView(unittest.TestCase):
    def test_renders_with_only_the_one_off_option_when_nothing_is_configured(self):
        with app() as (at, _):
            at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.radio[0].options, ["Get a one-off CSV"])
        self.assertTrue(any("may come back as a partial feed" in c for c in texts(at.caption)))

    def test_mentions_the_background_option_only_when_it_is_enabled(self):
        with app(turnstile=True, github=True, background=True) as (at, _):
            at.run()
        self.assertTrue(any("run the full build in the background" in c for c in texts(at.caption)))
        self.assertEqual(at.radio[0].options, ["Get a one-off CSV", "Keep this updated daily"])


class QuickBuildResult(unittest.TestCase):
    def test_result_view_offers_sample_ids_and_an_id_source_picker(self):
        with app() as (at, _):
            submit_form(at)
        self.assertFalse(at.exception, at.exception)
        self.assertEqual([s.label for s in at.selectbox], ["Product ID source"])
        self.assertEqual(at.selectbox[0].options, ["Default (variant ID)", "SKU"])
        page = " ".join(texts(at.markdown))
        self.assertIn("| Product | Variant ID (default) | SKU | Product ID |", page)
        self.assertIn("`901`", page)
        self.assertIn("isn't saved anywhere", " ".join(texts(at.info)))
        self.assertEqual(len(at.get("download_button")), 1)
        self.assertEqual(at.get("download_button")[0].proto.label, "Download feed.csv")

    def test_switching_the_id_source_re_keys_locally_and_flags_missing_values(self):
        with app() as (at, _):
            submit_form(at)
            at.selectbox[0].select("SKU").run()
        self.assertFalse(at.exception, at.exception)
        page = " ".join(texts(at.markdown) + texts(at.warning))
        self.assertIn("1 flagged", page)
        self.assertIn("have no SKU", page)

    def test_start_over_returns_to_the_form(self):
        with app() as (at, _):
            submit_form(at)
            buttons(at)["Start over"].click().run()
        self.assertEqual([t.label for t in at.text_input], ["Store URL"])


class PartialAndBackground(unittest.TestCase):
    CAPPED = dict(capped=True, candidates_total=1850)

    def run_verified(self, flow="oneoff", background=True, **flags):
        ctx = app(turnstile=True, github=True, background=background,
                  build=fake_build_feed(**(flags or self.CAPPED)), query=verified_return(flow))
        return ctx

    def test_capped_build_is_labelled_partial_and_offers_the_full_build_with_an_eta(self):
        with self.run_verified() as (at, _):
            at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(at.get("download_button")[0].proto.label, "Download partial feed.csv")
        page = " ".join(texts(at.info) + texts(at.markdown))
        self.assertIn("of roughly 1850", page)
        self.assertIn("about 13–37 minutes", page)   # 1850 pages at 0.4–1.2 s
        self.assertIn("Run the full build in the background", buttons(at))

    def test_no_background_offer_when_the_feature_is_off(self):
        with self.run_verified(background=False) as (at, _):
            at.run()
        self.assertNotIn("Run the full build in the background", buttons(at))
        self.assertEqual(at.get("download_button")[0].proto.label, "Download partial feed.csv")

    def test_complete_builds_dont_get_the_offer(self):
        with self.run_verified(capped=False) as (at, _):
            at.run()
        self.assertNotIn("Run the full build in the background", buttons(at))
        self.assertEqual(at.get("download_button")[0].proto.label, "Download feed.csv")

    def test_pushback_offer_is_honest_that_the_full_build_may_not_get_further(self):
        with self.run_verified(bot_pushback_detected=True) as (at, _):
            at.run()
        page = " ".join(texts(at.markdown))
        self.assertIn("allowlist", page)
        self.assertIn("may get further — that isn't guaranteed", page)

    def test_clicking_the_full_build_starts_a_job_and_moves_to_its_page(self):
        with self.run_verified() as (at, stack):
            capacity = stack.enter_context(mock.patch.object(jobs, "check_capacity", return_value=(True, None)))
            dispatch = stack.enter_context(mock.patch.object(jobs, "dispatch_build"))
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=None))
            at.run()
            buttons(at)["Run the full build in the background"].click().run()
        self.assertFalse(at.exception, at.exception)
        job_id = at.query_params["job"][0] if isinstance(at.query_params["job"], list) else at.query_params["job"]
        self.assertTrue(jobs.is_valid_job_id(job_id))
        self.assertEqual(dispatch.call_args.args[2:4], (job_id, STORE))
        self.assertTrue(capacity.called)
        self.assertIn("Starting the build…", " ".join(texts(at.info)))
        self.assertTrue(any("Bookmark this page" in t for t in texts(at.markdown)))

    def test_a_busy_queue_is_reported_instead_of_starting_a_build(self):
        with self.run_verified() as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "check_capacity", return_value=(False, "Try again soon.")))
            dispatch = stack.enter_context(mock.patch.object(jobs, "dispatch_build"))
            at.run()
            buttons(at)["Run the full build in the background"].click().run()
        self.assertIn("Try again soon.", texts(at.error))
        self.assertFalse(dispatch.called)


class NothingFoundInAQuickBuild(unittest.TestCase):
    """A quick build can find nothing yet still have only looked at part of the
    site (products buried deep in the sitemap) — that must not be a dead end."""

    def run_empty(self, background=True, **flags):
        return app(turnstile=True, github=True, background=background,
                   build=fake_build_feed(empty=True, **flags), query=verified_return())

    def test_capped_with_no_products_says_so_and_offers_the_full_build(self):
        with self.run_empty(capped=True, candidates_total=3000, pages_attempted=400) as (at, _):
            at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertTrue(any("only covered part of the site" in t for t in texts(at.warning)))
        self.assertIn("Run the full build in the background", buttons(at))
        self.assertTrue(any("found no products in the part of the site it checked" in t
                            for t in texts(at.markdown)))

    def test_pushback_with_no_products_also_offers_it(self):
        with self.run_empty(bot_pushback_detected=True) as (at, _):
            at.run()
        self.assertIn("Run the full build in the background", buttons(at))

    def test_no_offer_when_the_feature_is_off_or_nothing_was_cut_short(self):
        with self.run_empty(background=False, capped=True, candidates_total=3000) as (at, _):
            at.run()
        self.assertNotIn("Run the full build in the background", buttons(at))
        with self.run_empty() as (at, _):                 # complete run, genuinely nothing there
            at.run()
        self.assertNotIn("Run the full build in the background", buttons(at))
        self.assertTrue(any("No products were extracted. The site may block" in t for t in texts(at.warning)))

    def test_robots_is_only_blamed_when_it_accounts_for_every_page(self):
        with self.run_empty(robots_blocked_count=2, pages_attempted=400) as (at, _):
            at.run()
        self.assertFalse(any("robots.txt" in t for t in texts(at.warning)))
        with self.run_empty(robots_blocked_count=5, pages_attempted=5) as (at, _):
            at.run()
        self.assertTrue(any("robots.txt explicitly disallows" in t for t in texts(at.warning)))


class DailyRegistration(unittest.TestCase):
    def registry_patches(self, stack):
        return (stack.enter_context(mock.patch.object(registry, "put_feed_csv")),
                stack.enter_context(mock.patch.object(registry, "append_registry_entry")),
                stack.enter_context(mock.patch.object(registry, "read_registry", return_value=([], None))))

    def test_registration_is_a_deliberate_step_and_carries_the_chosen_id_source(self):
        with app(turnstile=True, github=True, query=verified_return("daily")) as (at, stack):
            put, append, _ = self.registry_patches(stack)
            at.run()
            self.assertFalse(put.called)                    # building alone registers nothing
            at.selectbox[0].select("SKU").run()
            buttons(at)["Register for daily refresh"].click().run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(put.call_args.args[:2], ("owner/repo", "tok"))
        entry = append.call_args.args[2]
        self.assertEqual((entry["store_url"], entry["id_field"]), (STORE, "sku"))
        self.assertIn("Your daily feed is live.", texts(at.success))

    def test_default_id_source_is_left_out_of_the_registry_entry(self):
        with app(turnstile=True, github=True, query=verified_return("daily")) as (at, stack):
            _, append, _ = self.registry_patches(stack)
            at.run()
            buttons(at)["Register for daily refresh"].click().run()
        self.assertNotIn("id_field", append.call_args.args[2])

    def test_an_incomplete_feed_needs_an_explicit_confirmation(self):
        build = fake_build_feed(capped=True, candidates_total=1850)
        with app(turnstile=True, github=True, build=build, query=verified_return("daily")) as (at, stack):
            put, _, _ = self.registry_patches(stack)
            at.run()
            self.assertTrue(buttons(at)["Register for daily refresh"].disabled)
            at.checkbox[0].check().run()
            self.assertFalse(buttons(at)["Register for daily refresh"].disabled)
            buttons(at)["Register for daily refresh"].click().run()
        self.assertTrue(put.called)

    def test_pushback_note_promises_a_retry_not_completion(self):
        build = fake_build_feed(bot_pushback_detected=True)
        with app(turnstile=True, github=True, build=build, query=verified_return("daily")) as (at, stack):
            self.registry_patches(stack)
            at.run()
        page = " ".join(texts(at.info))
        self.assertIn("never replace a larger live feed", page)
        self.assertIn("not a guarantee", page)


class JobView(unittest.TestCase):
    RUN = {"id": 5, "status": "completed", "conclusion": "success", "display_title": f"build {JOB}",
           "created_at": "2026-11-04T12:00:00Z", "updated_at": "2026-11-04T12:10:00Z"}

    def job_app(self, stack_setup=None, flow="oneoff", **kwargs):
        return app(turnstile=True, github=True, background=True,
                   query={"job": JOB, "flow": flow, "url": STORE}, **kwargs)

    def test_a_finished_build_shows_its_result_with_the_same_id_picker(self):
        with self.job_app() as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=self.RUN))
            stack.enter_context(mock.patch.object(jobs, "fetch_result", return_value=make_artifact(JOB)))
            at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(at.selectbox[0].options, ["Default (variant ID)", "SKU"])
        self.assertIn("about 24 hours", " ".join(texts(at.info)))

    def test_daily_registration_from_a_build_is_idempotent(self):
        with self.job_app(flow="daily") as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=self.RUN))
            stack.enter_context(mock.patch.object(jobs, "fetch_result", return_value=make_artifact(JOB)))
            put = stack.enter_context(mock.patch.object(registry, "put_feed_csv"))
            append = stack.enter_context(mock.patch.object(registry, "append_registry_entry"))
            stack.enter_context(mock.patch.object(registry, "read_registry", return_value=([], None)))
            at.run()
            buttons(at)["Register for daily refresh"].click().run()
        self.assertEqual(put.call_args.args[2], JOB[:10])
        self.assertEqual(append.call_args.args[2]["id"], JOB[:10])
        self.assertEqual(append.call_args.args[2]["store_url"], STORE)

    def test_an_already_registered_build_shows_the_feed_instead_of_registering_again(self):
        with self.job_app(flow="daily") as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=self.RUN))
            stack.enter_context(mock.patch.object(jobs, "fetch_result", return_value=make_artifact(JOB)))
            put = stack.enter_context(mock.patch.object(registry, "put_feed_csv"))
            stack.enter_context(mock.patch.object(
                registry, "read_registry", return_value=([{"id": JOB[:10]}], None)))
            at.run()
        self.assertIn("Your daily feed is live.", texts(at.success))
        self.assertNotIn("Register for daily refresh", buttons(at))
        self.assertFalse(put.called)

    def test_a_running_build_shows_progress_without_a_percentage(self):
        running = {**self.RUN, "status": "in_progress", "conclusion": None,
                   "run_started_at": "2026-11-04T12:00:00Z"}
        with self.job_app() as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=running))
            stack.enter_context(mock.patch.object(jobs, "get_run", return_value=running))
            at.run()
        self.assertTrue(any("Building your feed…" in t and "elapsed" in t for t in texts(at.info)))

    def test_failures_are_reported_clearly(self):
        failed = {**self.RUN, "conclusion": "failure"}
        with self.job_app() as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=failed))
            at.run()
        self.assertTrue(any("background build failed" in t for t in texts(at.error)))

    def test_an_expired_result_says_so(self):
        with self.job_app() as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=self.RUN))
            stack.enter_context(mock.patch.object(
                jobs, "fetch_result", side_effect=jobs.JobExpired("This result has expired.")))
            at.run()
        self.assertIn("This result has expired.", texts(at.error))

    def test_a_script_level_error_is_shown(self):
        with self.job_app() as (at, stack):
            stack.enter_context(mock.patch.object(jobs, "find_run", return_value=self.RUN))
            stack.enter_context(mock.patch.object(
                jobs, "fetch_result", return_value=make_artifact(JOB, error=SecurityError("private"))))
            at.run()
        self.assertTrue(any("isn't allowed" in t for t in texts(at.error)))

    def test_invalid_links_and_a_disabled_feature_are_rejected(self):
        with app(turnstile=True, github=True, background=True, query={"job": "../etc"}) as (at, _):
            at.run()
        self.assertIn("This build link isn't valid.", texts(at.error))
        with app(turnstile=True, github=True, background=False, query={"job": JOB}) as (at, _):
            at.run()
        self.assertTrue(any("aren't available" in t for t in texts(at.error)))


if __name__ == "__main__":
    unittest.main()
