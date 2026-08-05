# Decisions worth not re-litigating

Short notes on choices that weren't obvious, so a future revisit starts from
where this left off instead of re-deriving the same conclusions.

## No Google Sheets / OAuth for the public tool

`feed_tool/sheets.py` exists and works (service-account auth, pushes a feed
to a Sheet) but is deliberately **not** wired into `streamlit_app.py`.
Reasoning:

- "Anyone on the internet" writing to shared infrastructure (one
  Bonded-owned Sheet or service account) doesn't scale safely — it needs
  per-user isolation.
- Per-user isolation via Google OAuth means storing refresh tokens for the
  public — a real security responsibility (a leaked token is roughly
  equivalent to leaked Google account access), which needs a proper
  database and encryption at rest, not a repo full of JSON files.
- Google's verification review for a public OAuth app is a real, multi-week
  process with its own paperwork (privacy policy, app homepage, scope
  justification), even when using the narrower `drive.file` scope.
- The stable-hosted-URL approach (commit `feeds/{id}.csv`, serve it from
  `raw.githubusercontent.com`) meets Meta's actual requirement — a
  fetchable URL on a schedule — without any of the above.

`sheets.py` is kept parked, not deleted, in case a purely internal
Bonded-only pipeline wants it later, where a single shared service account
isn't a problem because there's only one trusted user (us).

## Global rate cap instead of per-registrant tracking

The registration guardrails (`feed_tool/registry.py`) are a hard cap on
total active registrations (500) and a cap on new registrations per hour —
both derived purely from `created_at` timestamps already in `registry.json`.
No per-IP or other per-registrant identifier is stored anywhere. Two
reasons:

- Streamlit Community Cloud sits behind its own proxy and doesn't reliably
  expose a real client IP to the app — a per-IP limit would be built on a
  signal that might not even be there.
- `registry.json` is a file in a **public** repo. Storing any kind of
  per-registrant fingerprint (raw or hashed) in its permanent git history
  is a privacy commitment that isn't worth making just to catch a single
  actor hammering the registration endpoint — especially once Turnstile is
  already screening out non-human traffic.

If real usage later shows one actor quota-hogging the global cap is an
actual problem, that's the point to revisit this — with real data instead
of a guess.

## SSRF defense in depth

The web app accepts an arbitrary URL from the public and the server makes
outbound requests to it — a textbook SSRF vector, not just an edge case.
`feed_tool/security.py` + `feed_tool/_http.py` handle this at the shared
HTTP layer (not just as a one-time check on the Streamlit input box), so
it also covers every follow-up request the crawler/paginator makes on its
own, not only the first one a user types in:

- Scheme allowlist (`http`/`https` only — blocks `javascript:`, `file:`, etc).
- DNS resolution + rejection of private/reserved/loopback/link-local
  ranges (covers RFC1918, loopback, link-local including the
  `169.254.169.254` cloud metadata address specifically, plus the IPv6
  equivalents).
- Redirects are followed manually (not via `requests`' built-in
  `allow_redirects`), re-validating every hop — otherwise an initially
  public URL could redirect to an internal address after the SSRF check
  already passed.

Residual limitation, accepted rather than solved: this re-resolves DNS
before each request rather than pinning the validated IP for the actual
connection, so a sufficiently fast DNS-rebinding attack (changing the
DNS answer between the check and the TCP connect a moment later) isn't
fully closed. Full protection would mean a custom transport that connects
to a pinned IP while still sending the correct Host/SNI — more engineering
than an agency scraping tool (not a high-value target) warrants right now.
Documented here rather than silently accepted, in case that calculus
changes.

## Partial robots.txt compliance: Disallow yes, Crawl-delay capped

`feed_tool/robots.py` respects `Disallow` rules in full (skips fetching
any blocked path — costs nothing in speed, and is the part that actually
matters for not scraping somewhere a site has explicitly refused). But it
caps `Crawl-delay`/`Request-rate` at `MAX_CRAWL_DELAY` (1.5s) rather than
honoring it in full. Reasoning:

- Some real platforms specify long delays — a live SAP Commerce/Hybris
  storefront hit during testing specifies `Crawl-delay: 10`. Fully honoring
  that would turn a several-hundred-product catalog into an hours-long
  run, directly against this tool's "fast" priority.
- The accepted residual risk: a site enforcing a longer delay than we
  honor may throttle or slow-walk responses to us. That's a real
  possibility (and plausibly explains an earlier test run against that
  same site that looked hung rather than just slow, before this was
  understood), but it degrades gracefully — the run gets slower, it
  doesn't get blocked outright — which is an acceptable trade for this
  tool's use case (not a high-value or high-frequency target).
- Revisit the cap (or make it configurable) if a specific client site
  turns out to reliably throttle/block at the current setting.

Also worth recording: the stdlib `urllib.robotparser` was tried first and
turned out to be unreliable against a real production robots.txt file
encountered during testing — it doesn't support the `*` wildcard
extension that most real Disallow rules use in practice (`Disallow:
*/cart`), and a blank line between a `User-agent:` line and its
`Disallow:` lines silently dropped that whole block's rules. Switched to
`protego` (same parser Scrapy uses, still pure Python, no compiled deps)
after confirming both bugs concretely against that site's live robots.txt.

## Turnstile gates both flows, not just daily registration

Originally only the "keep this updated daily" flow required a solved
Turnstile challenge; "get a one-off CSV" ran with no gate at all. That's a
real gap once the app is genuinely public: a form that runs an
unauthenticated, server-side, unlimited scrape is an open scraping proxy —
usable to anonymously hit arbitrary third-party sites through this app's
own server (and its own `BondedFeedBot` User-Agent / contact info), or to
just burn through Streamlit Community Cloud's free-tier compute.

Fixed by gating both flows behind the same Turnstile check whenever it's
configured (`turnstile_configured()`, split out from
`daily_flow_configured()` which still also needs the GitHub secrets and
only controls whether the daily option is *offered*). When Turnstile
isn't configured at all — e.g. local/dev use — both flows still run
ungated, same graceful-degradation pattern already used elsewhere in this
app rather than hard-failing on missing config.

## Feed removal is contact-based, not self-service

First attempt at this was a public "enter your feed ID to remove it"
form, on the theory that a random 10-hex-char feed id — never guessable,
shown only to whoever registered it — was equivalent to the "unlisted
URL" model already used for the feed CSV itself (anyone with the
`raw.githubusercontent.com` link can already read it; this just extended
"possession of the id" to "can also remove it").

That reasoning missed something the unlisted-URL model doesn't have:
`registry.json` — the file mapping every `store_url` to its feed id — is
itself a plain file in this **public** repo. It's not obscure at all;
anyone can open it on GitHub and read the exact id for any registered
store's feed. So the "self-service by ID" form wasn't gated by possession
of a hard-to-guess secret — it was gated by nothing, since the id for any
target was one file-open away. Concretely: a competitor could look up a
business's feed id in `registry.json` and deactivate their live Meta
catalog feed, entirely unauthenticated.

There's no account system in this tool by design (see "No Google
Sheets / OAuth" above), so removal can't check "is this actually your
feed" against a login either. Given that, the right fix isn't a cleverer
public form — it's not exposing a public write path for removal at all.
The app now points people at a `mailto:` link to Bonded instead, and
`feed_tool/registry.py`'s `remove_registry_entry()`/`delete_file()`
functions are kept working but unwired from any public button — they're
what Bonded calls directly (or from a small internal script) once a
request's been manually verified as legitimate.

A real fix that would preserve self-service: split `registry.json` into a
separate *private* repo (keep only `feeds/*.csv` public), so the mapping
itself isn't publicly readable. Not done — it roughly doubles the setup
this tool asks of anyone deploying their own copy (second repo, second
secret, and the nightly job would need cross-repo API calls instead of
plain local-file git operations just for the registry), which doesn't fit
this tool's "minimal setup" premise for what's ultimately a low-volume
admin action.

## Auto-prune threshold: 3 consecutive unsuccessful nights

The nightly job drops a feed (from `registry.json` and its `feeds/{id}.csv`)
after 3 consecutive nights of coming back empty or erroring outright,
rather than 1 or some larger number. Reasoning: a single bad night is
usually transient (the target site was briefly down, rate-limited us, had
a one-off outage) and pruning on the first failure would be too eager,
silently dropping feeds that would've recovered on their own. But letting
a truly dead feed (a site that's gone, or whose URL structure changed such
that this tool can no longer extract anything from it) sit forever isn't
right either — it just consumes a slot against the 500-feed cap
indefinitely. 3 consecutive nights is a middle ground: enough to absorb a
transient blip, not so many that a genuinely abandoned feed lingers for
weeks. Revisit if real usage shows this number is wrong in either
direction.

## Turnstile widget → Streamlit communication

`st.components.v1.html` renders in a sandboxed iframe that has
`allow-same-origin` but **not** `allow-top-navigation` — so the natural
approach (Turnstile's callback sets `window.parent.location.search` to
hand the verification token back to the app) throws a `SecurityError`,
sandbox restrictions on navigating an ancestor frame apply regardless of
same-origin. Worked around by having the callback inject a `<script>`
element into `window.top.document` instead: that script then executes in
the top document's own (unsandboxed) realm and navigates itself. Confirmed
working end-to-end in-browser during development, including the
downstream query-param verification path.

## Brand CSS: exclude Material icons from the global font override

`inject_brand_styles()` forces Poppins everywhere with `!important`, since
Streamlit's own emotion-generated CSS otherwise wins on specificity for
plain-inheritance rules (a bare `body { font-family: ... }` doesn't reach
`<h1>`/`<p>` etc., because Streamlit sets font-family on those directly,
and any explicit rule beats pure inheritance regardless of specificity).

First pass applied `!important` to every element including bare `span`s,
which broke Streamlit's own icons (expander arrows, etc.) — they're
rendered as ligature text (e.g. literally `keyboard_arrow_right`) in a
dedicated icon font (`data-testid="stIconMaterial"`), and forcing Poppins
onto them makes that ligature text render as literal text instead of a
glyph. Fixed by excluding `[data-testid="stIconMaterial"]` from the
override via `:not()` rather than guessing the icon font's exact name and
fighting it with a second `!important` rule — simpler, and correct
regardless of which icon font a future Streamlit version actually uses
internally. Worth remembering if the brand CSS gets touched again: any
new broad selector added here needs the same exclusion, or Streamlit's
icons will silently break again.

## Asset paths anchored to the script's own directory, not cwd

`LOGO_PATH`/`FAVICON_PATH` resolve via `os.path.dirname(os.path.abspath(__file__))`
rather than a bare relative path like `"assets/favicon.png"`. Caught
during testing: Streamlit resolves `.streamlit/secrets.toml` relative to
the script's own location, but a plain `os.path.exists()`/`open()` call
resolves relative to the process's current working directory instead —
and those aren't guaranteed to be the same directory depending on how the
process gets launched (they differed in the local dev-container-style
launcher used during this build, silently falling back to the emoji
favicon and no logo with no error at all). Anchoring to `__file__` is
correct regardless of launch method — including Streamlit Community
Cloud's own — so this isn't a workaround for one specific environment.
