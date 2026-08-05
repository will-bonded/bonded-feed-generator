# Bonded Feed Generator

A public, free-to-run web app that builds a Meta Commerce Manager-ready
product feed from any store URL — either as a one-off CSV download, or as
a feed that refreshes itself automatically every day at a stable public
URL, for free, with no software installed anywhere.

The underlying scraping/normalization engine (`feed_tool/`) is also usable
directly as a command-line tool — see **Advanced: command-line usage**
below.

## Using the web app

Open the app (see **Deployment** for the URL once it's live) and:

1. Enter a store URL.
2. Confirm you have permission to pull product data from that site.
3. Pick one:
   - **Get a one-off CSV** — runs immediately, gives you a download link.
     Nothing is saved anywhere; refresh the page and it's gone.
   - **Keep this updated daily** — after a quick human check (Cloudflare
     Turnstile), this commits the feed to this repo at a permanent URL
     (`https://raw.githubusercontent.com/{org}/{repo}/main/feeds/{id}.csv`)
     and registers it for automatic nightly regeneration. Paste that URL
     into Meta Commerce Manager's **Catalog > Data Sources > Scheduled
     Feed** setup, and set Meta's own daily fetch schedule there — Meta
     polls the URL itself from then on.

Guardrails on the daily-refresh flow: a human check (Turnstile) before
anything is written, a cap of 500 total active daily feeds, and a cap on
how many new feeds can be registered per hour (see `DECISIONS.md` for why
this is a global cap rather than per-user tracking).

## How the feed itself is built

1. **Detect** — probes the URL for a Shopify `/products.json` endpoint or a
   WooCommerce Store API endpoint (both public by default, no auth needed).
2. **Extract**
   - **Shopify**: paginates the public JSON endpoint, emits one feed row per
     variant, grouped by `item_group_id` so Meta shows size/colour options
     as one product. Tries `since_id`-based pagination first (Shopify's
     current recommended method); if a storefront ignores that (some do —
     e.g. a CDN caching the endpoint regardless of query string) it detects
     the stall and automatically switches to the legacy `page=` parameter
     instead, rather than looping until the site rate-limits it.
   - **Generic (anything else, including WooCommerce for now)**: reads
     `sitemap.xml` to find candidate product URLs — filtered by common path
     hints (`/product/`, `/shop/`, etc., or `--url-contains` for a site with
     a different structure) when those match something, or every page found
     when they don't, since no fixed hint list can anticipate every site's
     naming scheme. Either way, each candidate page is only actually kept as
     a product if it has real product data: structured `JSON-LD`
     (`schema.org/Product`) first, falling back to Open Graph (requiring
     `og:type="product"` or a price tag, not just any page with a title) if
     no JSON-LD is present. Warns loudly (rather than silently truncating)
     if a site has more matching URLs than `--max-pages` allows.
3. **Normalize** — maps everything to Meta's feed columns and flags rows
   missing a required field (id, title, availability, condition, price,
   link, image_link) instead of silently shipping incomplete data.
4. **Write** — outputs a single CSV.

Every outbound request (the URL you type in, every page the crawler visits
after that, every redirect) is validated against SSRF rules first — plain
`http`/`https` only, no private/reserved/internal IP ranges, redirects
re-checked at each hop. See `DECISIONS.md` for the detail and its one
documented residual limitation.

## Deployment

This is meant to run entirely on free tiers — no paid software anywhere in
the chain.

### 1. Make the repo public

Required for two things: `raw.githubusercontent.com` URLs are only
fetchable without auth on a public repo, and GitHub Actions minutes are
unlimited on public repos (the free-minutes cap only applies to private
ones).

### 2. Push it

```bash
gh repo create YOUR-REPO-NAME --public --source=. --remote=origin --push
```

(Claude Code doesn't have push access to your GitHub account — confirm
this step happens under your own credentials, same as anything else that
publishes to a shared/public place.)

### 3. Set up Cloudflare Turnstile (free) — gates the daily-refresh flow

1. Go to the [Cloudflare dashboard](https://dash.cloudflare.com) (free
   account) > **Turnstile** > **Add a site**.
2. Add your Streamlit app's domain (you can add `localhost` too, for local
   testing).
3. Copy the **Site Key** and **Secret Key** it gives you.

### 4. Set up a GitHub token for the app to write with

The Streamlit app needs write access to this repo to commit
`feeds/{id}.csv` and `registry.json` on registration — it calls the GitHub
Contents API directly using a **fine-grained personal access token**
scoped to just this repo:

1. [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
2. **Repository access**: only select repositories > this one.
3. **Permissions** > Repository permissions > **Contents**: Read and write.
4. Generate, copy the token (starts with `github_pat_...`) — you won't see
   it again.

### 5. Connect the repo on Streamlit Community Cloud

1. [share.streamlit.io](https://share.streamlit.io) (free) > **New app** >
   pick this repo, branch `main`, main file path `streamlit_app.py`.
2. Before (or after) deploying, open **Settings > Secrets** for the app and
   add:
   ```toml
   github_repo = "YOUR-GITHUB-USERNAME/YOUR-REPO-NAME"
   github_token = "github_pat_..."          # from step 4
   turnstile_site_key = "..."               # from step 3
   turnstile_secret_key = "..."              # from step 3
   ```
3. Deploy. Your app URL will be something like
   `https://bonded-feed-generator.streamlit.app` — exact subdomain depends
   on availability at deploy time.

Without the secrets configured, the app still works fine for the "one-off
CSV" flow — it just shows a note that daily refresh isn't set up yet
instead of offering that option.

### 6. Nightly regeneration is already wired up

`.github/workflows/nightly-regenerate.yml` runs daily on GitHub Actions
(free tier), reads `registry.json`, and regenerates every registered
feed's `feeds/{id}.csv` in one batch commit. One store failing (site down,
blocked, whatever) doesn't stop the rest from refreshing. Nothing further
to configure — it works off whatever's already in `registry.json` once
people start using the daily-refresh flow.

### Deferred, not built yet

- **Custom domain** (e.g. a `bonded.co.uk` subdomain) — Streamlit's free
  tier doesn't support this directly; would need a Cloudflare Worker
  reverse proxy if/when this becomes a priority. Ship on the default
  `.streamlit.app` address for now.

## Local development

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

For local testing of the daily-refresh flow, create `.streamlit/secrets.toml`
(gitignored) with the same four keys as step 5 above. Cloudflare publishes
[always-pass test keys](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)
for Turnstile if you want to test that path without a real Cloudflare
account:
```toml
turnstile_site_key = "1x00000000000000000000AA"
turnstile_secret_key = "1x0000000000000000000000000000000AA"
```

## Advanced: command-line usage

The scraping/normalization engine works standalone too, for a quick local
run without the web app:

```bash
python -m feed_tool.cli --url https://examplestore.com --output feed.csv

# For a generic (non-Shopify) site, if the default product-URL hints don't
# match its structure:
python -m feed_tool.cli --url https://examplestore.com --output feed.csv \
    --url-contains /product/ /shop/item/
```

Output: `feed.csv`, plus a terminal QC summary of clean vs flagged rows.

Only five dependencies beyond Streamlit itself, all pure Python —
`requests`, `beautifulsoup4`, `lxml`, `rsa`, `protego` — no compiler or
platform-specific build tools needed on any machine, including Windows on
ARM.

There's also an unused-but-working Google Sheets output
(`feed_tool/sheets.py`, `--google-sheet-id`/`--google-credentials` flags on
the CLI) — not wired into the public web app; see `DECISIONS.md` for why.

## What's been tested vs. what hasn't

- ✅ **Shopify**, live: verified against a real store with 477
  products/804 variants — including catching and fixing a real pagination
  bug where the store's `/products.json` endpoint ignored the `since_id`
  parameter entirely and kept re-serving the same page, which had silently
  inflated one run to 98,000+ duplicate rows before the site's own rate
  limiting cut it off. Fixed with stall detection + a `page=`-based
  fallback strategy.
- ✅ **Generic/WooCommerce fallback path**, live: verified against a public
  WooCommerce test store (755 real products) — including catching and
  fixing a silent-truncation bug where `--max-pages` (previously defaulting
  to 300) cut the feed down with no warning. It now scans the full sitemap,
  warns explicitly if more product URLs exist than the cap allows, and
  defaults to a much higher cap (2000). Also added a small delay between
  page fetches to avoid tripping a site's bot-blocking.
- ✅ **Extraction logic** verified against synthetic fixtures
  (`tests_fixtures/`) covering: Shopify JSON normalization (incl. a
  price/sale_price bug — Shopify's `price` is the current/discounted price,
  but Meta wants `price` = regular price and `sale_price` = the discount,
  so the mapping has to flip), JSON-LD extraction, and Open Graph fallback
  extraction.
- ✅ **SSRF validation** unit-tested against 14 cases (localhost, RFC1918
  ranges, cloud metadata address, IPv6 loopback, bad schemes, unresolvable
  hosts) — all correctly blocked; real store URLs correctly allowed.
- ✅ **Registration guardrails** (hourly cap, total-active cap) unit-tested
  against 5 scenarios including the time-window boundary.
- ✅ **Turnstile server-side verification** confirmed to fail closed on
  invalid input rather than throwing.
- ✅ **The full daily-refresh path** — Turnstile widget render → verify →
  build the feed → attempt the GitHub write — exercised live in-browser
  with Cloudflare's published test keys. Caught and fixed two real bugs in
  the process: `st.components.v1.html`'s sandboxed iframe can't navigate
  the parent page directly (worked around, see `DECISIONS.md`), and a
  failed GitHub write was crashing instead of showing a clean error
  (now wrapped properly in `feed_tool/registry.py`).
- ✅ **Nightly regeneration script** run locally against a small registry —
  caught and fixed a real bug where invoking it as `python
  scripts/regenerate_feeds.py` put `scripts/` rather than the repo root on
  `sys.path`, which would have broken every run of the actual GitHub
  Actions workflow.
- ✅ **Generic path on a genuinely custom-built store**, live: verified
  against a real SAP Commerce Cloud (Hybris) storefront — caught and fixed
  two more real bugs. First, its sitemap index nests sub-sitemaps as
  `Product-en-GBP-....xml?context=...` — a query string trailing the
  `.xml` extension, which the sitemap-vs-page check (`u.endswith(".xml")`)
  missed entirely, so every nested sitemap was silently dropped and 0
  products were found. Fixed by checking the URL's *path* rather than the
  raw string. Second, once discovery worked, every extracted row came back
  missing price and image — the page actually carries *two* JSON-LD
  `Product` blocks (a near-empty stub alongside the full one with
  price/SKU/image), and the code was blindly taking the first one found.
  Fixed by preferring whichever block actually has offer/price data.
- ✅ **robots.txt** — `Disallow` is respected in full; `Crawl-delay`/
  `Request-rate` are read but capped at 1.5s rather than honored in full,
  so a site asking for a 10s delay doesn't turn a several-hundred-product
  catalog into an hours-long run (see `DECISIONS.md`). Live-tested against
  a real production robots.txt, which also surfaced that the stdlib
  `urllib.robotparser` silently drops any rule block using the `*`
  wildcard (nearly all real `Disallow` rules do) and any block with a
  blank line between `User-agent:` and its `Disallow:` lines — switched to
  `protego` (Scrapy's parser, still pure Python) after confirming both
  bugs concretely.
- ✅ **Generic path, second custom-built store**, live: verified against a
  jewellery retailer with no known platform signature. Its product URLs
  (`/jewellery/18ct-gold-...-bracelet-18sb001`) don't contain any of the
  default hints, so this needed `--url-contains /jewellery/` — expected,
  not a bug. That hint also matches 120 category-listing pages ahead of
  the real products in this site's sitemap ordering, which surfaced a
  real gap: the Open Graph fallback was treating *any* page with an
  `og:title` as a product, so every category page came back as a flagged
  row (missing price/image) instead of being excluded. Fixed by requiring
  an actual product signal — `og:type="product"` (the standard convention
  for this) or a price tag — before extracting at all. Also picked up
  `sku`/`id` from `product:retailer_item_id` while in there, which the
  fallback wasn't reading before. Result: 20/20 clean rows, 0 flagged,
  versus 20 clean / 119 noisy-but-safe before the fix. (Separately, this
  site's own HTTPS certificate is misconfigured — missing an intermediate
  certificate — confirmed independently with `openssl` against its
  server; that's on their end, not something to work around here, and it
  would fail the same way on this tool's actual Linux deployment targets,
  not just as a one-off local quirk.)
- ✅ **Content-based fallback when hints match nothing** — the test above
  meant guessing `--url-contains /jewellery/` to get a result at all,
  which doesn't generalize (a furniture site might be `/living-room/`, a
  fashion site `/womens/dresses/`, and so on forever — no fixed hint list
  anticipates every site's naming scheme). Since the Open Graph fallback
  now reliably requires a real product signal, sitemap discovery no longer
  needs to guess right from the URL alone: when the default (or supplied)
  hints match zero pages, it now checks every sitemap page directly by
  content instead of failing outright. Live re-verified against the same
  jewellery retailer site with **no `--url-contains` at all** — the
  fallback engaged automatically and still found real, clean products.
  Confirmed this doesn't affect sites where hints already work: re-ran
  Shopify (unchanged 477/804) and the earlier custom-built store (still
  "found 5 candidate product URLs" via its `/p/` hint match, fallback
  never engages) with no change in behavior.
- ⚠️ **WooCommerce's native Store API path detects but doesn't extract
  yet** — it currently falls through to the generic sitemap/JSON-LD
  scraper (which works, but is slower and gives up on stock/SKU detail the
  Store API would provide directly).
- ⚠️ **JS-rendered storefronts** (React/Vue sites with no server-rendered
  product data) aren't handled — that needs a headless browser, which this
  tool doesn't use. It'll show up as 0 products extracted with a warning,
  not a silent empty feed.
- ⚠️ Cookie/consent walls and Cloudflare-style bot challenges on a target
  site can still block the generic crawler.
- ⚠️ **Not yet tested**: an actual successful GitHub write against a real
  repo/token (only tested against a deliberately invalid one, to confirm
  it fails cleanly) — worth doing once this is deployed for real, before
  relying on it for a client.

## Possible next steps

- **Native WooCommerce Store API extraction** — richer/faster than the
  generic fallback.
- **Headless-browser fallback** (e.g. Playwright) for JS-rendered
  storefronts the current HTML-only fetch can't see into.
- **Custom domain** — see Deployment above.
