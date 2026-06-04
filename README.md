# market-snapshot

A small Python pipeline that fetches public listing data from a single public
classifieds source on a schedule, enriches each record with an LLM-based
valuation estimate, and renders the result as a static dashboard.

GitHub Actions runs the pipeline on a schedule. The output is a JSON file
consumed by a static frontend in `docs/`, served via GitHub Pages.

## Components

- `scrape.py` — orchestrator. Loads cached data, refreshes, optionally
  enriches, writes outputs.
- `scraper.py` — fetches the source pages and parses listings (title, price,
  location, description, photos, timestamps).
- `enricher.py` — sends batched records to the Gemini API for valuation
  estimates with structured-output JSON. Optionally attaches each listing's
  primary photo to the same request so the model can identify the item and
  judge condition from the image.
- `config.py` — source/region, which searches to run, batch size, model
  selection, rate-limit settings, scoring knobs, and the image toggle.
- `docs/` — static HTML/CSS/JS dashboard. No build step. Reads
  `docs/data/items.json`.

## Outputs

- `raw_items.json` — persistent cache of every record seen, with cached
  enrichment results so we don't re-spend quota on items already valued.
- `items.csv` — flat tabular export sorted by score.
- `docs/data/items.json` — what the dashboard reads.

## How scoring works

For each listing the model first classifies it (`single_item`, `multi_item`,
or `not_for_sale`) and finds the real price. A classifieds price field is
frequently missing, `$0`, or a placeholder (`$1`, `$5`, `1234`) with the true
price written in the description ("I'm wanting 250 for it", "$150 obo", "Queen
$90 and up"), so the model **reads the description to extract the price**. It
returns a `price_status` — `priced` (real price found, in `effective_price_usd`),
`free` (explicitly given away), or `unknown` (genuinely no determinable price) —
and for a bundle it values only the single most valuable item. Then it estimates
the item's real retail value (independent of the asking price), a resale
percentage, a sales-velocity tier, a condition tier, and a confidence level.
From those:

- **cost** = the model's `effective_price_usd` when `price_status` is `priced`;
  `$0` when `free`; and the listing is left **unscored** when `unknown` (or
  `not_for_sale`) so a missing/placeholder price can't fabricate a deal. A `$0`
  price field is *not* assumed to be free — only an explicit give-away is. Cost
  is used as-is (no negotiation discount), so only listings priced below their
  resale value surface as deals. Unscored listings still appear under "Newest"
  with a *bundle* / *price?* badge. The `cost_basis` column in the CSV records
  which path was used (`ai_effective` / `free` / `listed` / `unknown` /
  `not_for_sale`).
- **effective resale** = estimated resale × a condition factor (full for
  new/open-box, a small haircut for an easy fix, zero for broken/unsellable or
  not-for-sale).
- **ROI (flip score)** = (effective resale − cost − pickup hassle) ÷ cost.
- **gross profit** = effective resale − cost − pickup hassle.

The dashboard's default "smart score" blends ROI, gross profit, and velocity.

## Local development

Requires Python 3.9 or newer.

    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

Create a `.env` file at the project root containing your Gemini API key:

    GEMINI_API_KEY=your_key_here

Get a free key at https://aistudio.google.com/apikey.

### Running

    python scrape.py              # full run (fetch + enrich)
    python scrape.py --no-enrich  # fetch only, skip the LLM call
    python scrape.py --enrich     # enrich only (no re-fetch)
    python scrape.py --test       # process only the first N items (see config.py)

To preview the dashboard locally, serve the `docs/` directory and open it:

    cd docs && python -m http.server

## Scheduled runs

`.github/workflows/scrape.yml` runs the full pipeline on a schedule and commits
the output back to the default branch. Scheduled runs are always in
**production** mode.

The scraper writes its outputs after every enrichment batch, atomically, and
the production commit step runs even if the run is **cancelled early or fails**
partway — so whatever finished is committed rather than lost. Because each run
loads the already-committed data first and only adds to it, a partial commit
can only extend the dataset, never blank or shrink it. (If you'd rather only
commit on a clean finish, change the commit step's `if:` from
`always() && ...` to `(success() || cancelled()) && ...`.)

The workflow needs a repo secret named `GEMINI_API_KEY`. An optional second
secret `GEMINI_API_KEY_2` (from a **different** Google Cloud project) roughly
doubles enrichment throughput.

### Manual runs: test vs production

Trigger the workflow yourself from the **Actions** tab → **Scheduled snapshot**
→ **Run workflow**. A dropdown lets you pick:

- **test** (the default) — runs `python scrape.py --test`: scrapes ~25 newest
  listings, fetches their detail pages and photos, and runs exactly **one**
  enrichment batch. It writes only the gitignored `*_test` files (so the live
  dashboard is untouched) and uploads them as a downloadable **test-output**
  artifact on the run, so you can verify everything end-to-end before going
  live. Nothing is committed.
- **production** — the full run that commits `docs/data/items.json` and updates
  the dashboard (identical to a scheduled run).

Use **test** first to confirm your key works and the output looks right, then
re-run with **production** selected.

## Sending photos to the model

`SEND_IMAGES_TO_AI` (on by default) attaches each listing's first photo to its
valuation request. Classifieds descriptions are often sparse, so the photo
usually carries most of the signal about what an item is and what condition
it's in.

A text+image request still counts as **one** request against the per-minute /
per-day quota — photos only add tokens — so this does not reduce how many
listings you can process per day. It does add one image download per new
listing at enrichment time, and photos are fetched, sent inline, and discarded
(never written to the repo). Set `SEND_IMAGES_TO_AI = False` for text-only
valuation.

## Free-tier limits

Gemini Flash-Lite free quota varies by account; check yours at
https://ai.dev/rate-limit. The orchestrator preserves cached enrichment across
runs, so it only spends quota on genuinely new records.

If a daily quota wall is hit mid-run, already-enriched records are saved and
the run exits cleanly. Re-running with `--enrich` after the quota resets picks
up the rest.

## A note on reliability and volume

The source actively rate-limits and blocks automated traffic from datacenter IP
ranges, which is what GitHub's hosted runners use. The scraper is deliberately
polite (slow request pacing, a realistic browser User-Agent), but some runs may
still come back short or empty if the source serves a challenge page. That's
expected; the next run generally recovers, and cached data keeps the dashboard
populated in between.

By default it crawls a curated list of profitable for-sale **categories**
(`SEARCH_PATHS`), each contributing its own newest listings, plus `/search/sss`
(everything) as a catch-all. Listings cross-posted to two feeds are de-duped.
A few things worth knowing:

- **Volume comes from category breadth, not page depth.** The no-JS results
  page we parse (to stay block-resistant) only serves the newest ~400 listings
  *per feed*, and the `?s=` offset re-serves that same set instead of paging
  deeper — so `MAX_PAGES_PER_SEARCH` is set to `1` and each *category* is scraped
  separately to multiply the distinct listings. To cover more (or fewer) areas,
  edit `SEARCH_PATHS`: on the site, click a category and copy the code after
  `/search/` in the address bar. After a run, check the log — any feed printing
  `parsed 0 rows` isn't a valid code for your city, so comment it out.
- **Stale listings are skipped.** `MAX_LISTING_AGE_DAYS` (default 60) means a
  listing posted longer ago than that is neither enriched nor scored, so the
  Gemini quota and the dashboard both stay on fresh deals. This is separate from
  `RETENTION_DAYS` (which counts from when *we* first saw a listing); the age
  filter keys off the listing's own post date, so a months-old post we just
  discovered is skipped too. Set it to `0` to disable.
- Only listings whose detail page (and photo) has been fetched are sent for
  enrichment, so every scored item is photo-backed rather than a title-only
  guess. Items still awaiting a detail page show up once a later run fetches them.

The first run after changing `SEARCH_PATHS` re-crawls each category fresh, so it
fetches more detail pages than a steady-state run and is the one most likely to
hit a temporary block; if the detail-fetch budget is reached, the rest are
fetched and enriched next run. To reduce request volume and block risk, trim
`SEARCH_PATHS` to fewer categories and/or raise `SCRAPE_DELAY_SECONDS`. The
cumulative dataset grows with volume × `RETENTION_DAYS`; lower `RETENTION_DAYS`
to keep the dashboard tightly focused on the newest listings.

## Troubleshooting

**Module not found: `google.genai`** — the venv isn't active. Run
`source venv/bin/activate` first.

**`GEMINI_API_KEY is not set`** — create a `.env` file with the key, or in CI
confirm the repo secret is named `GEMINI_API_KEY` exactly.

**Gemini returns empty responses / "could not parse"** — a batch's response
was truncated. Lower `BATCH_SIZE` in `config.py` (the affected batch is skipped
and retried on the next run, so nothing is lost meanwhile).

**A scheduled run scraped zero items** — the source likely served a challenge
page to the runner. Re-run from the Actions tab; raise `SCRAPE_DELAY_SECONDS`
and trim `SEARCH_PATHS` to fewer categories if it persists.

**A category feed printed `parsed 0 rows`** — that code isn't valid for your
city (or has no listings right now). Confirm it by clicking the category on the
site and reading the code after `/search/` in the URL; comment out bad ones.

**Older listings stopped showing up** — that's `MAX_LISTING_AGE_DAYS` (default
60) hiding posts older than the cutoff from scoring. Raise it or set it to `0`
in `config.py` to score everything regardless of age.

**Source page HTML changed** — the selectors live in `parse_search_results()`
and `fetch_item_detail()` in `scraper.py`.
