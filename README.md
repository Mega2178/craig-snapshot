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

For each listing the model estimates the item's real retail value (independent
of the asking price), a resale percentage, a sales-velocity tier, a condition
tier, and a confidence level. From those:

- **purchase price** = asking price × `NEGOTIATION_FACTOR` (assumes light
  haggling; no buyer's premium or tax on a private sale).
- **effective resale** = estimated resale × a condition factor (full for
  new/open-box, a small haircut for an easy fix, zero for broken/unsellable).
- **ROI (flip score)** = (effective resale − purchase price − pickup hassle) ÷
  purchase price.
- **gross profit** = effective resale − purchase price − pickup hassle.

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

## A note on reliability

The source actively rate-limits and blocks automated traffic from datacenter IP
ranges, which is what GitHub's hosted runners use. The scraper is deliberately
polite (slow request pacing, a realistic browser User-Agent, one results page
per category by default), but some scheduled runs may still come back short or
empty if the source serves a challenge page. That's expected; the next run
generally recovers, and cached data keeps the dashboard populated in between.
Lowering `MAX_PAGES_PER_SEARCH` and raising `SCRAPE_DELAY_SECONDS` reduce the
chance of being blocked.

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
and keep `MAX_PAGES_PER_SEARCH` low if it persists.

**Source page HTML changed** — the selectors live in `parse_search_results()`
and `fetch_item_detail()` in `scraper.py`.
