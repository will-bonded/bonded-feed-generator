"""
The web app's side of on-demand background builds (the GitHub Actions workflow
.github/workflows/build-feed.yml): start a build, find its run, read its
status, and fetch the finished result.

Why this exists: Streamlit Community Cloud drops long-running browser sessions,
and a crawl of a big catalogue takes many minutes — the result used to be lost
when the connection dropped. Here the crawl runs in Actions and the app just
polls; the job id lives in the page URL, so a refresh or reconnect resumes.

Guardrails follow registry.py's principle — derived entirely from state GitHub
already holds (the workflow's own run list), nothing stored about any user.
The job id is a full random uuid4, which is also what makes the result link
unguessable: anyone with the link can view that build, nobody else can find it.

Needs a token with Actions: read & write on top of the Contents access the
registry already uses (README.md).
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import uuid
import zipfile
from datetime import datetime, timezone

import requests

from .idsource import rekey
from .normalize import META_FEED_COLUMNS

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 20
WORKFLOW_FILE = "build-feed.yml"
RUN_NAME_PREFIX = "build "          # must match `run-name` in the workflow
ARTIFACT_PREFIX = "feed-"           # must match the upload-artifact name in the workflow
ARTIFACT_HOURS = 24                 # retention-days: 1 in the workflow

MAX_BUILDS_PER_HOUR = 30            # global caps, like registry.py's — see module docstring
MAX_CONCURRENT_BUILDS = 8

# How long the app waits for a dispatched run to appear in the run list before
# giving up (it typically shows within a few seconds).
RUN_LOOKUP_TIMEOUT_SECONDS = 120

# Rough seconds-per-page range, from the one large real run measured (~0.6s per
# page) widened both ways, since per-page speed varies a lot by site.
_SECONDS_PER_PAGE = (0.4, 1.2)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RUN_PENDING_STATUSES = ("queued", "in_progress", "waiting", "requested", "pending")


class JobError(Exception):
    """Raised for anything the caller should show to the user."""


class JobExpired(JobError):
    """The build's result is gone (artifacts only live ~24 hours)."""


def new_job_id() -> str:
    return uuid.uuid4().hex


def is_valid_job_id(value) -> bool:
    return isinstance(value, str) and bool(_JOB_ID_RE.fullmatch(value))


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, url, headers=_headers(token),
                                timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise JobError(f"Could not reach GitHub: {e}") from e


def _workflow_url(repo: str) -> str:
    return f"{GITHUB_API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}"


def _runs_url(repo: str) -> str:
    return f"{_workflow_url(repo)}/runs"


def _parse_ts(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))


def check_capacity(repo: str, token: str, now: datetime | None = None) -> tuple[bool, str | None]:
    """Global caps derived from the workflow's own recent runs."""
    resp = _request("GET", _runs_url(repo), token, params={"per_page": 100})
    if resp.status_code != 200:
        raise JobError(_api_error("check build capacity", resp))
    now = now or datetime.now(timezone.utc)
    runs = resp.json().get("workflow_runs", [])

    running = [r for r in runs if r.get("status") in _RUN_PENDING_STATUSES]
    if len(running) >= MAX_CONCURRENT_BUILDS:
        return False, ("Lots of builds are running right now across all users of this tool. "
                       "Please try again in a few minutes.")

    recent = [r for r in runs if (now - _parse_ts(r["created_at"])).total_seconds() < 3600]
    if len(recent) >= MAX_BUILDS_PER_HOUR:
        return False, ("Too many full builds have been started in the last hour across all "
                       "users of this tool. Please try again shortly.")
    return True, None


def dispatch_build(repo: str, token: str, job_id: str, store_url: str,
                   id_field: str = "default", max_pages: int = 2000, ref: str = "main") -> None:
    resp = _request("POST", f"{_workflow_url(repo)}/dispatches", token, json={
        "ref": ref,
        "inputs": {"job_id": job_id, "store_url": store_url,
                   "id_field": id_field, "max_pages": str(max_pages)},
    })
    if resp.status_code != 204:
        raise JobError(_api_error("start the background build", resp))


def find_run(repo: str, token: str, job_id: str) -> dict | None:
    """The dispatch API doesn't return a run id, so the run is found by its
    title (the workflow's run-name embeds the job id). None until it appears."""
    resp = _request("GET", _runs_url(repo), token,
                    params={"event": "workflow_dispatch", "per_page": 50})
    if resp.status_code != 200:
        raise JobError(_api_error("look up the background build", resp))
    wanted = f"{RUN_NAME_PREFIX}{job_id}"
    for run in resp.json().get("workflow_runs", []):
        if run.get("display_title") == wanted:
            return run
    return None


def get_run(repo: str, token: str, run_id: int) -> dict:
    resp = _request("GET", f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}", token)
    if resp.status_code != 200:
        raise JobError(_api_error("read the background build's status", resp))
    return resp.json()


def describe_run(run: dict, now: datetime | None = None) -> dict:
    """{"state": "queued"|"running"|"done"|"failed", "elapsed_seconds": int}"""
    status = run.get("status")
    if status == "completed":
        state = "done" if run.get("conclusion") == "success" else "failed"
    elif status == "in_progress":
        state = "running"
    else:
        state = "queued"

    now = now or datetime.now(timezone.utc)
    start = run.get("run_started_at") or run.get("created_at")
    end = _parse_ts(run["updated_at"]) if state in ("done", "failed") and run.get("updated_at") else now
    elapsed = max(int((end - _parse_ts(start)).total_seconds()), 0) if start else 0
    return {"state": state, "elapsed_seconds": elapsed}


def fetch_result(repo: str, token: str, run_id: int, job_id: str) -> dict:
    """Downloads the finished build's artifact. Returns
    {"summary": dict, "csv_bytes": bytes, "ids": dict | None, "log": str}."""
    resp = _request("GET", f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/artifacts", token)
    if resp.status_code != 200:
        raise JobError(_api_error("find the build's result", resp))
    wanted = f"{ARTIFACT_PREFIX}{job_id}"
    artifact = next((a for a in resp.json().get("artifacts", []) if a.get("name") == wanted), None)
    if artifact is None or artifact.get("expired"):
        raise JobExpired(f"This build's result is no longer available (results are kept for "
                         f"about {ARTIFACT_HOURS} hours). Please start a new build.")

    dl = _request("GET", artifact["archive_download_url"], token)
    if dl.status_code == 410:
        raise JobExpired("This build's result has expired. Please start a new build.")
    if dl.status_code != 200:
        raise JobError(_api_error("download the build's result", dl))

    try:
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            names = set(zf.namelist())
            if "summary.json" not in names:
                raise JobError("The build finished without producing a result. Please try again.")
            return {
                "summary": json.loads(zf.read("summary.json").decode("utf-8")),
                "csv_bytes": zf.read("feed.csv") if "feed.csv" in names else b"",
                "ids": json.loads(zf.read("ids.json").decode("utf-8")) if "ids.json" in names else None,
                "log": zf.read("run.log").decode("utf-8", "replace") if "run.log" in names else "",
            }
    except (zipfile.BadZipFile, ValueError) as e:
        raise JobError(f"The build's result couldn't be read: {e}") from e


def summary_from_artifact(result: dict) -> dict:
    """Rebuilds the same summary dict an in-app build produces (see
    feed.build_summary / idsource.rekey) from a fetched artifact, so the app's
    results view treats both identically — including switching the ID source,
    which works from ids.json with no re-crawl."""
    meta = result["summary"]
    csv_text = result["csv_bytes"].decode("utf-8-sig") if result["csv_bytes"] else ""
    rows = [{col: row.get(col, "") or "" for col in META_FEED_COLUMNS}
            for row in csv.DictReader(io.StringIO(csv_text, newline=""))]
    ids = result["ids"] or {}
    candidates = ids.get("candidates")
    if candidates is None or len(candidates) != len(rows):
        candidates = [{} for _ in rows]   # no ids.json: the ID source just can't be switched

    base = {"rows": rows, "id_candidates": candidates, "platform": ids.get("platform") or meta.get("platform")}
    if ids.get("default_ids") and len(ids["default_ids"]) == len(rows):
        base["default_ids"] = ids["default_ids"]
    # rekey with the field the build used: recomputes QC/ID stats from the rows,
    # and lets the app re-key to any other source from here.
    summary = rekey(base, meta.get("id_field") or "default")
    for key in ("robots_blocked_count", "pages_attempted", "bot_pushback_detected",
                "candidates_total", "capped"):
        summary[key] = meta.get(key)
    summary["store_url"] = meta.get("store_url")
    return summary


def estimate_range_minutes(pages: int) -> tuple[int, int]:
    low, high = (math.ceil(pages * s / 60) for s in _SECONDS_PER_PAGE)
    return max(low, 1), max(high, 1)


def format_elapsed(seconds: int) -> str:
    minutes, secs = divmod(max(int(seconds), 0), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _api_error(action: str, resp: requests.Response) -> str:
    if resp.status_code in (401, 403):
        return (f"Could not {action}: GitHub rejected the request. The token this app uses "
                f"likely needs Actions read & write permission (see README.md).")
    if resp.status_code == 404:
        return (f"Could not {action}: not found. The {WORKFLOW_FILE} workflow may not be on the "
                f"main branch yet, or the token can't see this repo.")
    try:
        detail = resp.json().get("message", "")
    except ValueError:
        detail = ""
    return f"Could not {action} (GitHub returned {resp.status_code}{': ' + detail if detail else ''})."
