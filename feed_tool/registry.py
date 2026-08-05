"""
GitHub-as-a-database for the public "daily refresh" registrations.

There's no separate database here on purpose — the registry (registry.json)
and every registered feed (feeds/{id}.csv) just live as files in this same
public repo, committed via the GitHub Contents API. That's what lets the
whole "keep this updated daily" feature run on nothing but a free GitHub
repo + free GitHub Actions.

Guardrails are deliberately simple and derived entirely from data already in
registry.json — no per-registrant identifier (IP or otherwise) is stored
anywhere. Rationale: Streamlit Community Cloud doesn't reliably expose a
real client IP to the app (it sits behind its own proxy), and storing any
kind of per-user fingerprint in a *public* repo's history is a privacy
commitment not worth making for what Turnstile's bot-check already covers.
See DECISIONS.md.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

import requests

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 20

MAX_ACTIVE_REGISTRATIONS = 500
MAX_NEW_REGISTRATIONS_PER_HOUR = 20  # global cap, not per-IP — see module docstring


class RegistryError(Exception):
    """Raised for any registry operation the caller should show to the user."""


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_file(repo: str, path: str, token: str) -> tuple[str | None, str | None]:
    """Returns (content, sha). content is None if the file doesn't exist yet."""
    resp = requests.get(f"{GITHUB_API}/repos/{repo}/contents/{path}",
                         headers=_headers(token), timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def put_file(repo: str, path: str, content_bytes: bytes, message: str, token: str,
             sha: str | None = None, branch: str = "main") -> dict:
    """Creates or updates a file. Pass `sha` (from get_file) when updating an
    existing file — GitHub rejects the write with a 409 if it's stale/missing."""
    body = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(f"{GITHUB_API}/repos/{repo}/contents/{path}",
                         headers=_headers(token), json=body, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def delete_file(repo: str, path: str, token: str, sha: str, message: str, branch: str = "main") -> None:
    body = {"message": message, "sha": sha, "branch": branch}
    resp = requests.delete(f"{GITHUB_API}/repos/{repo}/contents/{path}",
                            headers=_headers(token), json=body, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()


def read_registry(repo: str, token: str) -> tuple[list, str | None]:
    content, sha = get_file(repo, "registry.json", token)
    entries = json.loads(content) if content else []
    return entries, sha


def _parse_ts(iso_ts: str) -> float:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()


def check_registration_allowed(entries: list) -> tuple[bool, str | None]:
    """Pure function over the current registry — no external state. Checked
    fresh (with a just-fetched registry) immediately before every write, since
    the counts can change between check and write under concurrent use."""
    if len(entries) >= MAX_ACTIVE_REGISTRATIONS:
        return False, (
            f"This tool has hit its capacity of {MAX_ACTIVE_REGISTRATIONS} active daily feeds. "
            f"Please try again later, or use \"Get a one-off CSV\" instead."
        )

    one_hour_ago = time.time() - 3600
    recent = [e for e in entries if _parse_ts(e["created_at"]) >= one_hour_ago]
    if len(recent) >= MAX_NEW_REGISTRATIONS_PER_HOUR:
        return False, (
            "Too many new daily feeds have been registered in the last hour across all users "
            "of this tool. Please try again shortly."
        )

    return True, None


def put_feed_csv(repo: str, token: str, feed_id: str, csv_bytes: bytes) -> None:
    try:
        put_file(repo, f"feeds/{feed_id}.csv", csv_bytes, f"Add feed {feed_id}", token)
    except requests.RequestException as e:
        raise RegistryError(f"Could not upload feed to GitHub: {e}") from e


def append_registry_entry(repo: str, token: str, entry: dict, max_retries: int = 3) -> None:
    """Re-reads the registry, re-checks the guardrails, and writes — retrying
    on a 409 (another registration landed first) by re-fetching a fresh sha
    and re-applying, rather than failing a request that was otherwise valid."""
    last_error = None
    for attempt in range(max_retries):
        try:
            entries, sha = read_registry(repo, token)
        except requests.RequestException as e:
            raise RegistryError(f"Could not read the feed registry from GitHub: {e}") from e

        allowed, reason = check_registration_allowed(entries)
        if not allowed:
            raise RegistryError(reason)

        entries.append(entry)
        content = (json.dumps(entries, indent=2) + "\n").encode("utf-8")

        try:
            put_file(repo, "registry.json", content, f"Register feed {entry['id']}", token, sha=sha)
            return
        except requests.HTTPError as e:
            last_error = e
            if e.response is not None and e.response.status_code == 409 and attempt < max_retries - 1:
                continue  # someone else's registration landed between our read and write — retry
            raise RegistryError(f"Could not register feed: {e}") from e

    raise RegistryError(f"Could not register feed after {max_retries} attempts: {last_error}")


def remove_registry_entry(repo: str, token: str, feed_id: str, max_retries: int = 3) -> bool:
    """Removes `feed_id` from registry.json and best-effort deletes its
    feeds/{feed_id}.csv. Returns False if no entry with that id exists (not
    an error — the caller can show "no such feed" rather than a failure).

    NOT wired to any public button in streamlit_app.py — call this directly
    (or from a small internal script) once a removal request has been
    manually verified as legitimate. It was originally exposed as a public
    "enter your feed ID to remove it" form, on the theory that the feed id
    was an unguessable-enough secret; that reasoning missed that
    registry.json (which maps every store_url to its feed id) is itself a
    plain file in this public repo, so anyone can already look up any
    store's feed id there. See DECISIONS.md.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            entries, sha = read_registry(repo, token)
        except requests.RequestException as e:
            raise RegistryError(f"Could not read the feed registry from GitHub: {e}") from e

        if not any(e["id"] == feed_id for e in entries):
            return False

        remaining = [e for e in entries if e["id"] != feed_id]
        content = (json.dumps(remaining, indent=2) + "\n").encode("utf-8")

        try:
            put_file(repo, "registry.json", content, f"Remove feed {feed_id}", token, sha=sha)
        except requests.HTTPError as e:
            last_error = e
            if e.response is not None and e.response.status_code == 409 and attempt < max_retries - 1:
                continue  # someone else's write landed between our read and write — retry
            raise RegistryError(f"Could not remove feed: {e}") from e

        # Registry is updated at this point — the csv file lingering a bit
        # longer on a transient failure here isn't worth failing over.
        try:
            _, csv_sha = get_file(repo, f"feeds/{feed_id}.csv", token)
            if csv_sha:
                delete_file(repo, f"feeds/{feed_id}.csv", token, csv_sha, f"Delete feed {feed_id}")
        except requests.RequestException:
            pass

        return True

    raise RegistryError(f"Could not remove feed after {max_retries} attempts: {last_error}")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
