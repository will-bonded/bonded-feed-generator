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

## Quick in-app builds, plus full builds in the background

Streamlit Community Cloud drops long-running browser sessions (no documented
limit; forum reports range from ~5 to 30 minutes), and when the connection
drops the browser can't reattach — the finished result is simply lost. A big
catalogue crawls at roughly 0.6s/page (one large real run, 2000 pages ≈ 20
minutes), so the old "build everything synchronously" design could never
reliably finish for large stores.

So the in-app build is capped (`IN_APP_MAX_PAGES = 400`, ~4 minutes at that
rate) and, when a store has more or pushes back, the result offers a **full
build in the background** (`.github/workflows/build-feed.yml`, started via
`workflow_dispatch`, polled by `feed_tool/jobs.py`). Choices worth keeping:

- **The job id lives in the page URL** (`?job=<uuid4 hex>`), so a refresh or
  dropped connection resumes instead of losing the build. It's also what makes
  the result link unguessable — there's no listing of anyone's builds.
- **Hybrid, not everything-through-the-job.** A background build adds a
  queue + install wait (~1–2 min, estimated) to every run; routing small
  stores through it would make the common case slower. Only builds that were
  capped or cut off are offered the upgrade.
- **No fake progress bar.** GitHub reports queued / running / done, not "250 of
  400 pages". The page shows elapsed time and a rough range instead.
- **Caps come from GitHub's own run list** (builds per hour, builds running at
  once), the same "derive it from existing state, store nothing about users"
  principle as the registration caps in `registry.py`.
- **The dispatch API doesn't return a run id** (as understood — worth
  re-checking), so the workflow's `run-name` embeds the job id and the app
  finds its run by that title.
- **One-off results are artifacts, not commits**, retained one day. Cost:
  they sit in a public repo for that day (likely downloadable by any signed-in
  GitHub user who finds the run) and store URLs show in run inputs/logs. The
  data is public product data, but the result page says so rather than keeping
  the old "saved nowhere" claim on this path. A private repo would avoid it
  but splits setup and burns the free Actions minutes quota; not done.
- **Kill switch:** `background_builds_enabled` (secret) gates the whole thing,
  so the code can ship ahead of the token permission it needs.
- Actions runners use datacenter IPs that some sites treat differently from
  Streamlit's — the User-Agent allowlist request remains the only ask we can
  make of a blocking site.
- **Daily registration stays app-side** for background builds, with the feed id
  derived from the job id (`job_id[:10]`) so reopening or re-sharing a build
  link can't register it twice. Trade-off: the user has to come back to the
  link to finish registering. Having the workflow register itself is a
  possible later step (it would need to share a concurrency group with the
  nightly job, which also commits).

## Which field is the feed `id` — chosen after seeing the options

A Meta pixel's `content_ids` must match the catalogue item `id`, and which field
a store's pixel sends varies (variant ID, SKU, a parent product ID). Static
detection of the pixel was investigated and **dropped**: on every store
checked, the pixel code wasn't in the raw page HTML (it's loaded through Tag
Manager or Shopify's sandboxed web-pixel runtime, whose script bundle the
store's own robots.txt disallows us to fetch), and capturing it live needs a
real browser that would also fire real events into the client's pixel.

What shipped instead:

- The per-platform defaults are unchanged — they're what actually matched on a
  real high-volume Shopify account (99%+ pixel-to-catalogue match, using the
  raw variant ID as `id` and the product ID as `item_group_id`).
- After a build the user sees every candidate ID side by side (to compare with
  Meta Pixel Helper) and picks the source. Because each row carries all its
  candidate IDs (`_ids`, never written to the CSV), switching is a local
  re-key — no re-crawl, and it works the same for background builds.
- Shopify offers Default and SKU only. "Product ID" would give a product's
  variants one shared id, which Meta treats as duplicates; the product ID is
  already in the feed as `item_group_id`.
- Rows missing the chosen field are **flagged, not filled** from another
  source: a feed mixing ID sources can't match a pixel sending only one of
  them, so a visible hole beats a plausible wrong id.
- Generic stores with no SKU / `productID` fall back to the page URL as id,
  which can never match a pixel — that's counted and warned about.
- A daily feed saves the choice (`id_field`) on its registry entry, and the
  nightly job applies it. Changing it later re-keys the catalogue, so it goes
  through the contact flow rather than self-service.

## A bad night must not replace a good live feed

Meta treats the latest file as the source of truth, and `write_feed` always
overwrites — so a night where the site was down, blocked us mid-run, or came
back thin used to replace a complete feed (an empty run replaced it with a
header-only CSV). The nightly job now builds into a temp file and only
replaces the live feed when the result is trustworthy
(`decide_replacement` in `scripts/regenerate_feeds.py`):

- 0 products: keep the previous feed; counts toward auto-pruning as before.
- The site pushed back mid-run: keep the previous feed, but do **not** count
  it toward pruning — pruning deletes the feed outright, and a stale feed is
  better for the client than a missing one while a site is only temporarily
  refusing us.
- Under 70% of the live feed's size: keep it, but accept the smaller feed
  after 3 such nights running (a real clearance or range cut is possible).
- A feed's first run is accepted whatever it contains (nothing to protect).

Registering an incomplete feed as a daily feed also needs an explicit
confirmation in the app.

## Tests live in the repo now

The earlier regression scripts were throwaway files in a temp folder and got
wiped. `tests/` (standard-library `unittest`, no network, no secrets) now
covers discovery, the CLI behaviours (robots messaging, hint fallback, caps,
bot pushback), ID sources, the nightly guard, the job plumbing, and drives the
real `streamlit_app.py` headlessly with Streamlit's `AppTest` — from a clean
temp copy, so a local `.streamlit/secrets.toml` can't leak in. Run:
`python -m unittest discover -s tests -t .`

Two things the app tests caught that are worth remembering:

- `st.table` / `st.dataframe` need `pyarrow`, which a Streamlit install can
  lack. The sample-ID table is plain markdown (values in code spans, since
  they're scraped text) so nothing in the app depends on it.
- Streamlit has marked `st.components.v1.html` (used by the Turnstile widget)
  for removal. It still works today; replacing it with `st.iframe` needs a
  real Cloudflare challenge to verify the token callback survives, so it's
  left as is — a follow-up before a Streamlit upgrade removes it.
