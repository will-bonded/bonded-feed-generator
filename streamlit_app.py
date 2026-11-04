"""
Bonded Feed Generator — one-page web app for the Meta catalog feed builder.

Two flows, both gated behind a Cloudflare Turnstile human check whenever
Turnstile is configured (a public form that runs a server-side scrape with
zero rate limit would otherwise be an open scraping proxy for anyone):
  A. "Get a one-off CSV"        — builds the feed, offers a download, nothing
                                   persists anywhere.
  B. "Keep this updated daily"  — same, plus: on confirmation, commits the CSV
                                   + a registry entry to this repo via the
                                   GitHub Contents API, and hands back a
                                   stable raw.githubusercontent.com URL for
                                   Meta Commerce Manager to poll. A separate
                                   nightly GitHub Actions job
                                   (.github/workflows/nightly-regenerate.yml)
                                   re-runs every registered feed daily, and
                                   auto-prunes one that's failed or come
                                   back empty for several days running.

Builds run in two ways. A quick in-app build (capped at IN_APP_MAX_PAGES so it
finishes well inside what a hosted Streamlit session survives) gives an answer
in minutes; when a site has more than that — or pushed back mid-run — the
result offers a full build in the background (GitHub Actions, see
feed_tool/jobs.py). The background build's id lives in the page URL (?job=...),
so a refresh or dropped connection resumes instead of losing the result.
Background builds only switch on when the `background_builds_enabled` secret is
set (README.md); until then the app behaves as a quick-build-only app.

The result view lets the user choose which product field becomes the feed `id`
after seeing sample values (feed_tool/idsource.py) — most people don't know
their pixel's ID source until they see the options.

Why Flow B doesn't use Google Sheets/OAuth, why rate-limiting here is a
single global cap rather than per-IP tracking, and the SSRF/redirect
handling this all leans on: see DECISIONS.md.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import uuid
from urllib.parse import urlparse

import streamlit as st

from feed_tool.cli import build_feed
from feed_tool.feed import rows_to_csv_bytes
from feed_tool.security import validate_public_url, SecurityError
from feed_tool.turnstile import verify_turnstile
from feed_tool._http import USER_AGENT
from feed_tool import idsource, jobs, registry

APP_TITLE = "Bonded Feed Generator"

# Brand assets. Anchored to this file's own directory rather than a bare
# relative path — Streamlit itself resolves .streamlit/secrets.toml relative
# to the script's location, but plain os.path.exists()/open() calls resolve
# against the process's current working directory instead, which isn't
# guaranteed to be the repo root depending on how the process was launched.
# Both wirings below degrade gracefully (emoji favicon, no logo) if the
# files aren't there, so nothing breaks either way.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(_APP_DIR, "assets", "bonded-logo.png")
FAVICON_PATH = os.path.join(_APP_DIR, "assets", "favicon.png")

PERMISSION_LABEL = "I confirm I have permission to pull product data from this website."
TERMS_BLURB = (
    "This tool reads publicly available product data from the URL you provide. "
    "You're responsible for confirming you're allowed to do so. Feeds set to daily "
    "refresh are regenerated automatically and stored at a public URL for Meta to "
    "fetch — nothing sensitive is collected or stored."
)

FLOW_ONEOFF = "Get a one-off CSV"
FLOW_DAILY = "Keep this updated daily"
FLOW_PARAM_ONEOFF = "oneoff"
FLOW_PARAM_DAILY = "daily"

MODE_INAPP = "inapp"
MODE_BACKGROUND = "bg"

# A hosted Streamlit session can drop on long-running scripts (the browser then
# can't reattach and the result is lost), so the in-app build is deliberately
# small: ~400 pages is roughly 4 minutes at the one rate measured on a large
# real site. Bigger catalogues go to a background build.
IN_APP_MAX_PAGES = 400

# After passing Turnstile, the same browser session may start a background build
# without a second challenge for this long.
VERIFIED_SESSION_SECONDS = 600


def get_secret(name: str):
    try:
        return st.secrets[name]
    except Exception:
        return None


def turnstile_configured() -> bool:
    return bool(get_secret("turnstile_site_key")) and bool(get_secret("turnstile_secret_key"))


def daily_flow_configured() -> bool:
    return turnstile_configured() and all(get_secret(k) for k in ("github_repo", "github_token"))


def background_builds_enabled() -> bool:
    """Kill switch + prerequisites: the secret must be on AND the GitHub/Turnstile
    secrets the background path leans on must be present."""
    flag = get_secret("background_builds_enabled")
    return str(flag).strip().lower() in ("1", "true", "yes", "on") and daily_flow_configured()


def store_slug(url: str) -> str:
    """'https://www.examplestore.com/x' -> 'examplestore-com' (for filenames)."""
    host = urlparse(url if "//" in url else f"https://{url}").netloc.lower()
    host = host.removeprefix("www.")
    slug = "".join(c if c.isalnum() else "-" for c in host).strip("-")
    return slug or "store"


def is_partial(summary: dict) -> bool:
    return bool(summary.get("bot_pushback_detected") or summary.get("capped"))


def download_name(url: str, partial: bool) -> str:
    return f"{store_slug(url)}_feed{'-partial' if partial else ''}.csv"


def inject_brand_styles() -> None:
    """Google-hosted Poppins + the shared status-badge pill styling (same
    classes already proven in the budget pacer's HTML report, for a
    consistent look across both tools)."""
    st.markdown(
        """
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap"
              rel="stylesheet">
        <style>
        /* :not([data-testid="stIconMaterial"]) matters here — those spans
           render Material Symbols icons (expander arrows, etc.) as ligature
           text in a dedicated icon font. Overriding their font-family turns
           them into literal text like "keyboard_arrow_right" instead of an
           arrow glyph, so they're excluded rather than left to fight an
           !important rule with a guessed replacement font name. */
        html, body, [class*="css"],
        h1, h2, h3, h4, h5, h6, p, button, input, label, a,
        span:not([data-testid="stIconMaterial"]),
        div:not([data-testid="stIconMaterial"]) {
            font-family: 'Poppins', sans-serif !important;
        }
        .status-badge {
            padding: 2px 10px;
            border-radius: 10px;
            font-size: 12px;
            color: white;
            font-weight: 600;
        }
        .status-success { background: #1A7A4A; }
        .status-pending { background: #DE8344; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def run_pipeline(url: str) -> tuple[dict, str]:
    """Quick in-app build of `url`. Returns (summary, captured_log). The CSV is
    derived from summary["rows"] when needed (rows_to_csv_bytes), so the ID
    source can be switched later without a re-run."""
    log_buffer = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = os.path.join(tmp_dir, "feed.csv")
        with contextlib.redirect_stdout(log_buffer):
            summary = build_feed(url, out_path, max_pages=IN_APP_MAX_PAGES,
                                 fallback_cap=IN_APP_MAX_PAGES)
    return summary, log_buffer.getvalue()


# --------------------------------------------------------------------------- #
# Result display
# --------------------------------------------------------------------------- #

def show_warnings(summary: dict) -> None:
    if summary.get("bot_pushback_detected"):
        # A more specific, more actionable diagnosis than the generic
        # messages below, whenever it applies -- the site actively refused
        # requests partway through, a different situation from robots.txt
        # disallowing a path or the page just lacking product markup.
        if summary["total"] > 0:
            st.warning(
                f"This site appears to push back against automated access at volume — "
                f"stopped early and delivering a **partial** feed with the "
                f"**{summary['total']}** product(s) found before that happened."
            )
        else:
            st.warning(
                "This site appears to push back against automated access at volume — "
                "no products were captured before that happened."
            )
        st.markdown(
            "**Want a complete feed?** Ask the store's development or hosting team to "
            "allowlist this tool's User-Agent string in their firewall/bot-protection "
            "settings — once allowlisted, a re-run should get the full catalog instead "
            "of a partial one."
        )
        st.code(USER_AGENT)
    elif summary["total"] == 0:
        if summary.get("robots_blocked_count"):
            st.warning(
                "No products were extracted — this site's robots.txt explicitly disallows the "
                "page(s) this tool needed to fetch. That's not a bug; the site has asked crawlers "
                "not to access them, so there's nothing further this tool can do here."
            )
        else:
            st.warning(
                "No products were extracted. The site may block scraping, require "
                "JS rendering, or use a URL structure this tool doesn't recognize yet."
            )
    if summary.get("capped") and not summary.get("bot_pushback_detected") and summary["total"] > 0:
        total = summary.get("candidates_total")
        scope = f"of roughly {total} " if total and total > summary["total"] else ""
        st.info(f"This build covered {summary['total']} product(s) {scope}— it's a partial feed.")


def show_counts(view: dict) -> None:
    if view["total"] > 0:
        st.markdown(
            f"Built **{view['total']}** rows — "
            f"<span class=\"status-badge status-success\">{view['clean']} clean</span> "
            f"<span class=\"status-badge status-pending\">{len(view['flagged'])} flagged</span>",
            unsafe_allow_html=True,
        )
        if view["flagged"]:
            with st.expander(f"{len(view['flagged'])} flagged rows (first 20 shown)"):
                for f in view["flagged"][:20]:
                    st.write(f"- **{f['id']}** \"{f['title']}\": {', '.join(f['problems'])}")


def show_log(log_text: str) -> None:
    with st.expander("Show run log"):
        st.code(log_text or "(no output)")


def _default_label(platform: str) -> str:
    if idsource.platform_kind(platform) == idsource.PLATFORM_SHOPIFY:
        return "Default (variant ID)"
    return "Default (SKU, then product ID, then page URL)"


def render_id_source(result: dict) -> dict:
    """Sample IDs + the ID-source picker. Returns the summary re-keyed to the
    choice. Re-keying is local (every candidate ID travels with the rows), so
    switching is instant and never re-crawls."""
    base = result["summary"]
    platform = base.get("platform") or idsource.PLATFORM_GENERIC
    options = list(idsource.options_for(platform))

    st.markdown("#### Product ID")
    st.caption(
        "Your Meta pixel's `content_ids` must match this feed's `id` for dynamic ads to work. "
        "Check a product page with Meta Pixel Helper, then pick the source whose value matches."
    )
    st.markdown(idsource.sample_ids_markdown(base))

    chosen = st.selectbox(
        "Product ID source", options, key=f"id_field_{result['key']}",
        format_func=lambda o: _default_label(platform) if o == idsource.DEFAULT else idsource.LABELS[o],
    )
    view = idsource.rekey(base, chosen)

    if view["id_url_fallback_count"]:
        st.warning(f"{view['id_url_fallback_count']} product(s) had no SKU or product ID on their page, "
                   f"so their feed id is the page URL — that can never match a pixel's content_ids.")
    if view["duplicate_ids"]:
        st.warning(f"{view['duplicate_ids']} feed id(s) are shared by more than one product — "
                   f"Meta treats those as duplicates.")
    if chosen != idsource.DEFAULT and view["missing_id_count"]:
        st.warning(f"{view['missing_id_count']} product(s) have no {idsource.LABELS[chosen]} and are "
                   f"flagged below. They're not given a different ID, since a pixel sending only "
                   f"{idsource.LABELS[chosen]} could never match one.")
    return view


def render_result(result: dict) -> None:
    summary = result["summary"]
    show_warnings(summary)

    if summary["total"] == 0:
        show_log(result["log"])
        render_start_over()
        return

    view = render_id_source(result)
    show_counts(view)
    show_log(result["log"])

    partial = is_partial(summary)
    store_url = summary.get("store_url") or result["url"]

    if result["flow"] == FLOW_PARAM_DAILY:
        render_daily_registration(result, view, partial)
    else:
        st.download_button(
            "Download partial feed.csv" if partial else "Download feed.csv",
            rows_to_csv_bytes(view["rows"]),
            file_name=download_name(store_url, partial), mime="text/csv",
        )
        if result["source"] == "job":
            st.info("This page's link keeps the result for about 24 hours — download it now.")
        else:
            st.info("This file isn't saved anywhere — download it now.")

    render_background_offer(result, summary)
    render_pending_verification()
    render_start_over()


def render_daily_registration(result: dict, view: dict, partial: bool) -> None:
    repo, token = get_secret("github_repo"), get_secret("github_token")

    # A background build's feed id is derived from its job id, so registering
    # the same build twice (a refresh, a shared link) can only ever produce one
    # registry entry instead of duplicates.
    if result["source"] == "job" and not result.get("registry_checked"):
        result["registry_checked"] = True
        feed_id = result["job_id"][:10]
        try:
            entries, _ = registry.read_registry(repo, token)
        except Exception:
            entries = []
        if any(e.get("id") == feed_id for e in entries):
            result["registered"] = {"feed_id": feed_id}

    if result.get("registered"):
        feed_id = result["registered"]["feed_id"]
        st.success("Your daily feed is live.")
        st.code(f"https://raw.githubusercontent.com/{repo}/main/feeds/{feed_id}.csv")
        st.markdown(
            "**Next step:** in Meta Commerce Manager, go to **Catalog > Data Sources**, "
            "add a **Scheduled Feed**, paste the URL above, and set Meta's own daily fetch "
            "schedule there. Meta polls that URL itself from now on — a separate nightly "
            "job on our side keeps it refreshed with the latest data from your store."
        )
        return

    acknowledged = True
    if partial:
        st.warning(
            "This feed is incomplete. Meta treats the latest file as the source of truth, so "
            "products missing from it can drop out of your catalogue."
        )
        if result["summary"].get("bot_pushback_detected"):
            st.info(
                "Tomorrow's automatic nightly refresh will attempt this store again, and it will "
                "never replace a larger live feed with a smaller or partial one. That's another "
                "chance, not a guarantee the feed will become complete."
            )
        acknowledged = st.checkbox("Register this incomplete feed anyway",
                                   key=f"ack_partial_{result['key']}")

    if st.button("Register for daily refresh", disabled=not acknowledged,
                 key=f"register_{result['key']}"):
        feed_id = result["job_id"][:10] if result["source"] == "job" else uuid.uuid4().hex[:10]
        entry = {"id": feed_id, "store_url": result["summary"].get("store_url") or result["url"],
                 "created_at": registry.now_iso()}
        if view["id_field"] != idsource.DEFAULT:
            entry["id_field"] = view["id_field"]
        with st.spinner("Registering your feed..."):
            try:
                registry.put_feed_csv(repo, token, feed_id, rows_to_csv_bytes(view["rows"]))
                registry.append_registry_entry(repo, token, entry)
            except registry.RegistryError as e:
                st.error(str(e))
                return
        result["registered"] = {"feed_id": feed_id}
        st.rerun()


def render_background_offer(result: dict, summary: dict) -> None:
    """After a quick build that didn't cover everything, offer the full build."""
    if result["source"] != "inapp" or not background_builds_enabled() or not is_partial(summary):
        return

    st.markdown("---")
    st.markdown("#### Want the complete catalogue?")
    total = summary.get("candidates_total")
    if summary.get("bot_pushback_detected"):
        st.write(
            "The site pushed back during this quick build. A full build runs in the background "
            "from different servers, so it may get further — that isn't guaranteed."
        )
    elif total and total > summary["total"]:
        low, high = jobs.estimate_range_minutes(total)
        st.write(
            f"This quick build covered {summary['total']} of roughly {total} products. A full "
            f"build takes about {low}–{high} minutes (a rough range — it varies a lot by site) "
            f"and runs in the background: you can close this tab and come back to the link."
        )
    else:
        st.write("A full build runs in the background, so you can close this tab and come back to the link.")

    if st.button("Run the full build in the background", key=f"fullbuild_{result['key']}"):
        verified = time.time() - result.get("verified_at", 0) < VERIFIED_SESSION_SECONDS
        if verified:
            start_background_build(result["url"], result["flow"])
        else:
            st.session_state["pending_verification"] = {
                "url": result["url"], "permission": True, "mode": MODE_BACKGROUND,
                "flow": FLOW_DAILY if result["flow"] == FLOW_PARAM_DAILY else FLOW_ONEOFF,
            }
            st.rerun()


def render_start_over() -> None:
    if st.button("Start over", key="start_over"):
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()


# --------------------------------------------------------------------------- #
# Turnstile + starting builds
# --------------------------------------------------------------------------- #

def render_turnstile_widget(site_key: str, url: str, permission: bool, flow_param: str,
                            mode: str = MODE_INAPP) -> None:
    safe_url = json.dumps(url)
    safe_flow = json.dumps(flow_param)
    safe_mode = json.dumps(mode)
    permission_flag = "1" if permission else "0"
    html = f"""
    <div style="display:flex;justify-content:center;padding:8px 0;">
      <div class="cf-turnstile" data-sitekey="{site_key}" data-callback="onTurnstileVerified"></div>
    </div>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    <script>
    function onTurnstileVerified(token) {{
        // st.components.v1.html renders in a sandboxed iframe without
        // allow-top-navigation, so this frame can't navigate window.top
        // directly (browsers block it with a SecurityError even though
        // allow-same-origin lets us read/write its DOM). Getting the top
        // document to navigate *itself*, via a script we inject into it,
        // runs in the top document's own unsandboxed realm and works.
        var params = new URLSearchParams(window.top.location.search);
        params.set("turnstile_token", token);
        params.set("flow", {safe_flow});
        params.set("permission", "{permission_flag}");
        params.set("url", {safe_url});
        params.set("mode", {safe_mode});
        var navScript = window.top.document.createElement("script");
        navScript.textContent = "window.location.search = " + JSON.stringify(params.toString()) + ";";
        window.top.document.body.appendChild(navScript);
    }}
    </script>
    """
    st.components.v1.html(html, height=100)


def render_pending_verification() -> None:
    pending = st.session_state.get("pending_verification")
    if not pending:
        return
    st.write("Complete this check to confirm you're not a bot, then this will continue automatically:")
    flow_param = FLOW_PARAM_DAILY if pending["flow"] == FLOW_DAILY else FLOW_PARAM_ONEOFF
    render_turnstile_widget(get_secret("turnstile_site_key"), pending["url"],
                            pending["permission"], flow_param, pending.get("mode", MODE_INAPP))


def start_background_build(url: str, flow_param: str) -> None:
    """Starts the build and moves the page to its job view (?job=...). On any
    failure shows the reason and leaves the current page as it was."""
    repo, token = get_secret("github_repo"), get_secret("github_token")
    try:
        ok, reason = jobs.check_capacity(repo, token)
        if not ok:
            st.error(reason)
            return
        job_id = jobs.new_job_id()
        jobs.dispatch_build(repo, token, job_id, url)
    except jobs.JobError as e:
        st.error(str(e))
        return
    st.session_state.pop("pending_verification", None)
    st.query_params["job"] = job_id
    st.query_params["flow"] = flow_param
    st.query_params["url"] = url
    st.rerun()


def run_inapp_build(url: str, flow_param: str) -> None:
    with st.spinner("Building your feed..."):
        summary, log_text = run_pipeline(url)
    st.session_state["result"] = {
        "key": uuid.uuid4().hex[:8], "url": url, "flow": flow_param, "source": "inapp",
        "job_id": None, "summary": summary, "log": log_text,
        "verified_at": time.time(), "registered": None,
    }


def handle_verified_request(query_params) -> None:
    """The Turnstile widget navigates the page with the token + the request in
    the URL; resolve it here. Errors are shown and the form stays available."""
    url = query_params.get("url", "")
    permission_ok = query_params.get("permission") == "1"
    flow_param = query_params.get("flow", FLOW_PARAM_ONEOFF)
    mode = query_params.get("mode", MODE_INAPP)
    token = query_params.get("turnstile_token")
    # Consume the token immediately so a page refresh/back-nav can't replay it.
    del st.query_params["turnstile_token"]

    try:
        validate_public_url(url)
    except SecurityError as e:
        st.error(str(e))
        return
    if not permission_ok:
        st.error("Please confirm you have permission to pull data from this site.")
        return
    if not verify_turnstile(token, get_secret("turnstile_secret_key")):
        st.error("Verification failed or expired — please try again below.")
        return
    if flow_param == FLOW_PARAM_DAILY and not daily_flow_configured():
        st.error("Daily refresh isn't configured on this deployment.")
        return

    if mode == MODE_BACKGROUND and background_builds_enabled():
        start_background_build(url, flow_param)   # reruns on success
        return
    run_inapp_build(url, flow_param)


# --------------------------------------------------------------------------- #
# Background build view (?job=...)
# --------------------------------------------------------------------------- #

def _fail_job(job_id: str, message: str) -> None:
    st.session_state[f"job_error_{job_id}"] = message
    st.rerun()


@st.fragment(run_every=10)
def poll_job(job_id: str, flow_param: str) -> None:
    """Refreshes only this block every 10s (not the whole page), and moves the
    page on to the result when the build is done."""
    repo, token = get_secret("github_repo"), get_secret("github_token")
    started = st.session_state.setdefault(f"job_started_{job_id}", time.time())
    run_key = f"job_run_{job_id}"

    try:
        run_id = st.session_state.get(run_key)
        if run_id is None:
            run = jobs.find_run(repo, token, job_id)
            if run is None:
                if time.time() - started > jobs.RUN_LOOKUP_TIMEOUT_SECONDS:
                    _fail_job(job_id, "The background build didn't start. Please start a new build.")
                st.info("Starting the build…")
                return
            st.session_state[run_key] = run["id"]
        else:
            run = jobs.get_run(repo, token, run_id)

        status = jobs.describe_run(run)
        if status["state"] == "queued":
            st.info("Waiting for a build slot… this usually takes under a minute.")
        elif status["state"] == "running":
            st.info(f"Building your feed… {jobs.format_elapsed(status['elapsed_seconds'])} elapsed. "
                    f"Large catalogues can take 20+ minutes.")
        elif status["state"] == "failed":
            _fail_job(job_id, "The background build failed. Please start a new build.")
        else:
            fetched = jobs.fetch_result(repo, token, run["id"], job_id)
            meta = fetched["summary"]
            if meta.get("status") == "error":
                _fail_job(job_id, meta.get("error") or "The build failed. Please try again.")
            summary = jobs.summary_from_artifact(fetched)
            st.session_state[f"job_result_{job_id}"] = {
                "key": job_id[:8], "url": summary.get("store_url") or "", "flow": flow_param,
                "source": "job", "job_id": job_id, "summary": summary, "log": fetched["log"],
                "verified_at": 0, "registered": None,
            }
            st.rerun()
    except jobs.JobError as e:
        _fail_job(job_id, str(e))


def render_job_view(job_id: str) -> None:
    flow_param = st.query_params.get("flow", FLOW_PARAM_ONEOFF)

    if not jobs.is_valid_job_id(job_id):
        st.error("This build link isn't valid.")
        render_start_over()
        return
    if not background_builds_enabled():
        st.error("Background builds aren't available on this deployment.")
        render_start_over()
        return

    result = st.session_state.get(f"job_result_{job_id}")
    if result:
        render_result(result)
        return

    error = st.session_state.get(f"job_error_{job_id}")
    if error:
        st.error(error)
        render_start_over()
        return

    store = st.query_params.get("url")
    st.markdown("### Building your full feed in the background")
    if store:
        st.write(f"Store: `{store}`")
    st.write(
        "You can close this tab. **Bookmark this page** — its link brings you back to this "
        "build, and the result is kept for about 24 hours."
    )
    poll_job(job_id, flow_param)


# --------------------------------------------------------------------------- #
# Main page
# --------------------------------------------------------------------------- #

def render_form() -> None:
    with st.form("main_form"):
        url = st.text_input("Store URL", placeholder="https://examplestore.com")
        permission = st.checkbox(PERMISSION_LABEL)
        st.caption(TERMS_BLURB)

        flow_options = [FLOW_ONEOFF]
        if daily_flow_configured():
            flow_options.append(FLOW_DAILY)
        flow = st.radio("What do you want to do?", flow_options)

        if background_builds_enabled():
            st.caption("Builds usually finish in a few minutes. Very large catalogues are built "
                       "in part first, with the option to run the full build in the background.")
        else:
            st.caption("Builds usually finish in a few minutes. Very large catalogues may come "
                       "back as a partial feed.")

        submitted = st.form_submit_button("Continue")

    if submitted:
        if not url.strip():
            st.error("Enter a store URL.")
        elif not permission:
            st.error("Please confirm you have permission to pull data from this site.")
        else:
            try:
                validate_public_url(url)
            except SecurityError as e:
                st.error(str(e))
            else:
                flow_param = FLOW_PARAM_DAILY if flow == FLOW_DAILY else FLOW_PARAM_ONEOFF
                if turnstile_configured():
                    st.session_state["pending_verification"] = {
                        "url": url, "permission": permission, "flow": flow, "mode": MODE_INAPP,
                    }
                elif flow == FLOW_ONEOFF:
                    run_inapp_build(url, flow_param)
                    st.rerun()
                # else: FLOW_DAILY without turnstile_configured() can't happen —
                # daily_flow_configured() (which requires it) gates whether
                # FLOW_DAILY is even offered as an option above.

    render_pending_verification()

    if not daily_flow_configured():
        st.caption(
            "(\"Keep this updated daily\" isn't configured on this deployment yet — "
            "see README.md for the Turnstile + GitHub secrets it needs.)"
        )
    else:
        with st.expander("Want to stop a daily feed?"):
            st.markdown(
                "<p style=\"font-size: 14px;\">"
                "Registered feeds aren't self-service to remove — the registry that maps "
                "store URLs to feed IDs lives in this public repo, so a self-service "
                "\"remove by ID\" form would let anyone who can see that file deactivate "
                "<em>any</em> registered feed, not just their own. Click below to email us "
                "your feed ID or store URL and we'll deactivate it for you."
                "</p>",
                unsafe_allow_html=True,
            )
            st.link_button(
                "Contact us to deactivate a feed",
                "mailto:connect@bondedagency.com"
                "?subject=Deactivate%20a%20daily%20feed"
                "&body=Please%20include%20the%20feed%20ID%20or%20store%20URL%20"
                "you%27d%20like%20to%20stop%20refreshing%3A%0A%0A",
            )


def main() -> None:
    favicon = FAVICON_PATH if os.path.exists(FAVICON_PATH) else "🧩"
    st.set_page_config(page_title=APP_TITLE, page_icon=favicon, layout="centered")
    inject_brand_styles()

    if os.path.exists(LOGO_PATH):
        st.logo(LOGO_PATH)

    st.title(APP_TITLE)
    st.write("Build a Meta Commerce Manager product feed from any store URL.")

    query_params = st.query_params

    job_id = query_params.get("job")
    if job_id:
        render_job_view(job_id)
        return

    if query_params.get("turnstile_token"):
        handle_verified_request(query_params)

    result = st.session_state.get("result")
    if result:
        render_result(result)
        return

    render_form()


if __name__ == "__main__":
    main()
