"""
Bonded Feed Generator — one-page web app for the Meta catalog feed builder.

Two flows, both gated behind a Cloudflare Turnstile human check whenever
Turnstile is configured (a public form that runs a server-side scrape with
zero rate limit would otherwise be an open scraping proxy for anyone):
  A. "Get a one-off CSV"        — runs feed_tool synchronously, offers a
                                   download, nothing persists anywhere.
  B. "Keep this updated daily"  — same, plus: commits the CSV + a registry
                                   entry to this repo via the GitHub
                                   Contents API, and hands back a stable
                                   raw.githubusercontent.com URL for Meta
                                   Commerce Manager to poll. A separate
                                   nightly GitHub Actions job
                                   (.github/workflows/nightly-regenerate.yml)
                                   re-runs every registered feed daily, and
                                   auto-prunes one that's failed or come
                                   back empty for several days running.

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

import streamlit as st

from feed_tool.cli import build_feed
from feed_tool.security import validate_public_url, SecurityError
from feed_tool.turnstile import verify_turnstile
from feed_tool import registry

APP_TITLE = "Bonded Feed Generator"
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


def get_secret(name: str):
    try:
        return st.secrets[name]
    except Exception:
        return None


def turnstile_configured() -> bool:
    return bool(get_secret("turnstile_site_key")) and bool(get_secret("turnstile_secret_key"))


def daily_flow_configured() -> bool:
    return turnstile_configured() and all(get_secret(k) for k in ("github_repo", "github_token"))


def run_pipeline(url: str) -> tuple[dict, bytes, str]:
    """Runs feed_tool against `url`, returns (summary, csv_bytes, captured_log)."""
    log_buffer = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = os.path.join(tmp_dir, "feed.csv")
        with contextlib.redirect_stdout(log_buffer):
            summary = build_feed(url, out_path)
        with open(out_path, "rb") as f:
            csv_bytes = f.read()
    return summary, csv_bytes, log_buffer.getvalue()


def show_summary(summary: dict, log_text: str) -> None:
    if summary["total"] == 0:
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
    else:
        st.success(
            f"Built {summary['total']} rows — {summary['clean']} clean, "
            f"{len(summary['flagged'])} flagged."
        )
        if summary["flagged"]:
            with st.expander(f"{len(summary['flagged'])} flagged rows (missing a required field)"):
                for f in summary["flagged"][:20]:
                    st.write(f"- **{f['id']}** \"{f['title']}\": {', '.join(f['problems'])}")
    with st.expander("Show run log"):
        st.code(log_text or "(no output)")


def render_turnstile_widget(site_key: str, url: str, permission: bool, flow_param: str) -> None:
    safe_url = json.dumps(url)
    safe_flow = json.dumps(flow_param)
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
        var navScript = window.top.document.createElement("script");
        navScript.textContent = "window.location.search = " + JSON.stringify(params.toString()) + ";";
        window.top.document.body.appendChild(navScript);
    }}
    </script>
    """
    st.components.v1.html(html, height=100)


def handle_oneoff(url: str) -> None:
    with st.spinner("Building your feed..."):
        summary, csv_bytes, log_text = run_pipeline(url)
    show_summary(summary, log_text)
    if summary["total"] > 0:
        st.download_button("Download feed.csv", csv_bytes, file_name="feed.csv", mime="text/csv")
        st.info("This file isn't saved anywhere — download it now.")


def handle_daily_registration(url: str) -> None:
    repo = get_secret("github_repo")
    token = get_secret("github_token")

    feed_id = uuid.uuid4().hex[:10]
    with st.spinner("Building your feed..."):
        summary, csv_bytes, log_text = run_pipeline(url)

    show_summary(summary, log_text)
    if summary["total"] == 0:
        return

    with st.spinner("Registering your feed..."):
        try:
            registry.put_feed_csv(repo, token, feed_id, csv_bytes)
            registry.append_registry_entry(repo, token, {
                "id": feed_id,
                "store_url": url,
                "created_at": registry.now_iso(),
            })
        except registry.RegistryError as e:
            st.error(str(e))
            return

    raw_url = f"https://raw.githubusercontent.com/{repo}/main/feeds/{feed_id}.csv"
    st.success("Your daily feed is live.")
    st.code(raw_url)
    st.markdown(
        "**Next step:** in Meta Commerce Manager, go to **Catalog > Data Sources**, "
        "add a **Scheduled Feed**, paste the URL above, and set Meta's own daily fetch "
        "schedule there. Meta polls that URL itself from now on — a separate nightly "
        "job on our side keeps it refreshed with the latest data from your store."
    )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🧩")
    st.title(APP_TITLE)
    st.write("Build a Meta Commerce Manager product feed from any store URL.")

    query_params = st.query_params
    turnstile_token = query_params.get("turnstile_token")

    if turnstile_token:
        url = query_params.get("url", "")
        permission_ok = query_params.get("permission") == "1"
        flow_param = query_params.get("flow", FLOW_PARAM_ONEOFF)
        # Consume the token immediately so a page refresh/back-nav can't replay it.
        del st.query_params["turnstile_token"]

        try:
            validate_public_url(url)
            url_ok = True
        except SecurityError as e:
            st.error(str(e))
            url_ok = False

        if url_ok and not permission_ok:
            st.error("Please confirm you have permission to pull data from this site.")
        elif url_ok:
            secret_key = get_secret("turnstile_secret_key")
            if not verify_turnstile(turnstile_token, secret_key):
                st.error("Verification failed or expired — please try again below.")
            elif flow_param == FLOW_PARAM_DAILY:
                handle_daily_registration(url)
            else:
                handle_oneoff(url)
        return  # this render is entirely about resolving the redirect above

    with st.form("main_form"):
        url = st.text_input("Store URL", placeholder="https://examplestore.com")
        permission = st.checkbox(PERMISSION_LABEL)
        st.caption(TERMS_BLURB)

        flow_options = [FLOW_ONEOFF]
        if daily_flow_configured():
            flow_options.append(FLOW_DAILY)
        flow = st.radio("What do you want to do?", flow_options)

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
                if turnstile_configured():
                    st.session_state["pending_verification"] = {
                        "url": url, "permission": permission, "flow": flow,
                    }
                elif flow == FLOW_ONEOFF:
                    handle_oneoff(url)
                # else: FLOW_DAILY without turnstile_configured() can't happen —
                # daily_flow_configured() (which requires it) gates whether
                # FLOW_DAILY is even offered as an option above.

    pending = st.session_state.get("pending_verification")
    if pending:
        st.write("Complete this check to confirm you're not a bot, then this will continue automatically:")
        flow_param = FLOW_PARAM_DAILY if pending["flow"] == FLOW_DAILY else FLOW_PARAM_ONEOFF
        render_turnstile_widget(get_secret("turnstile_site_key"), pending["url"],
                                 pending["permission"], flow_param)

    if not daily_flow_configured():
        st.caption(
            "(\"Keep this updated daily\" isn't configured on this deployment yet — "
            "see README.md for the Turnstile + GitHub secrets it needs.)"
        )
    else:
        with st.expander("Want to stop a daily feed?"):
            st.write(
                "Registered feeds aren't self-service to remove — the registry that maps "
                "store URLs to feed IDs lives in this public repo, so a self-service "
                "\"remove by ID\" form would let anyone who can see that file deactivate "
                "*any* registered feed, not just their own. Click below to email us your "
                "feed ID or store URL and we'll deactivate it for you."
            )
            st.link_button(
                "Contact us to deactivate a feed",
                "mailto:connect@bondedagency.com"
                "?subject=Deactivate%20a%20daily%20feed"
                "&body=Please%20include%20the%20feed%20ID%20or%20store%20URL%20"
                "you%27d%20like%20to%20stop%20refreshing%3A%0A%0A",
            )


if __name__ == "__main__":
    main()
