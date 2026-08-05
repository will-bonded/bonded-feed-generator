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
