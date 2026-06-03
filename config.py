"""
Configuration. Edit these values, then run scrape.py.

The only thing you MUST set is GEMINI_API_KEY.
Everything else has reasonable defaults.
"""

# ─── REQUIRED ────────────────────────────────────────────────────────────────
# Get a free key from https://aistudio.google.com/apikey
# (login with your Google account, "Create API key", copy/paste)
#
# Keys are read from env vars, NOT hardcoded here.
#   - Locally: put them in a .env file next to this one (gitignored).
#     python-dotenv loads it automatically below.
#   - In CI:   the GitHub Actions workflow injects them from repo Secrets.
#
# FALLBACK KEY: GEMINI_API_KEY_2 is optional. If set, the enricher will
# dispatch batches concurrently across both keys, roughly DOUBLING
# throughput.
#
# CRITICAL: the two keys must come from DIFFERENT Google Cloud projects
# (i.e. different Google accounts, or at minimum a second project under
# the same account with its own quota allocation). Google enforces
# rate-limit quotas at the project level, not the key level — two keys
# inside the same project share one daily/per-minute bucket and the
# fallback gains you nothing. Two keys in separate projects = combined
# quota and combined RPM.
#
# Leave GEMINI_API_KEY_2 unset (empty) to operate single-key.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv only needed locally; in CI the env var comes from Actions
import os
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2", "")  # optional fallback


# ─── SOURCE / SEARCH ─────────────────────────────────────────────────────────
# Which regional subdomain to pull from. Lee's Summit / Kansas City metro =
# "kansascity". Find yours by visiting the site and reading it out of the
# address bar (e.g. "denver", "seattle", "chicago").
SITE_SUBDOMAIN = "kansascity"

# Which search result pages to crawl. Each entry is a path on the source site.
# The default pulls the newest items across ALL for-sale categories, which is
# what you want for a broad flip hunt.
#
# To cut the noise and spend less Gemini quota, narrow to specific categories
# by replacing the list, e.g.:
#     SEARCH_PATHS = ["/search/ela", "/search/tla", "/search/sga"]
# Common category codes:
#     sss = all for sale        ela = electronics      tla = tools
#     sga = garage sales        ata = antiques         ppa = appliances
#     hsa = household           ata = arts+crafts      sna = sporting goods
#     vga = video gaming        msa = musical instr    bia = bikes
#     foa = furniture           hva = heavy equipment  mca = motorcycle parts
SEARCH_PATHS = ["/search/sss"]

# How many result pages to fetch PER search path. One page already returns the
# newest few hundred listings, which is plenty for a flip hunt and keeps the
# request count (and the chance of getting blocked) low. Bump to 2-3 only if
# you want deeper coverage and accept more requests. Pagination uses the site's
# "?s=<offset>" query param under the hood.
MAX_PAGES_PER_SEARCH = 1


# ─── BEHAVIOR ────────────────────────────────────────────────────────────────
# Seconds between HTTP requests to the source. Be polite — this site is much
# more aggressive about blocking scrapers than the auction site this tool was
# adapted from, so a slower, gentler pace is safer. 2s is a reasonable floor.
SCRAPE_DELAY_SECONDS = 2.0

# Fetch the per-item detail page for newly-discovered listings, so the AI can
# read the full freeform description (the search-results cards only carry the
# title + price). The detail page is also where we pick up the listing photos.
# Trade-off: ~1 extra HTTP request per *new* listing. Already-cached items
# aren't re-fetched (unless RECHECK_EXISTING_DETAILS is on).
SCRAPE_ITEM_DETAIL_PAGES = True

# Re-fetch the detail page for items we've already seen on a later run? Off by
# default — detail content rarely changes once a listing is posted, and
# re-fetching every cached item every run multiplies request volume (and block
# risk). When False, cached items only get their price/location refreshed from
# the cheap search-results pass.
RECHECK_EXISTING_DETAILS = False

# Max seconds to spend fetching detail pages in one run. Once exceeded, the
# scraper stops detail-page fetches and lets the rest enrich on title-only
# data. Prevents a huge day from blowing past the GitHub Actions timeout. The
# leftover listings get detail-fetched on a subsequent run (they're cached
# without the detail flag).
SCRAPE_DETAIL_PAGE_TIME_BUDGET_SECONDS = 3000  # 50 min


# ─── GEMINI ENRICHMENT ───────────────────────────────────────────────────────
# Items per Gemini batch call — used for BOTH text-only and with-photos runs.
# 25 is well within limits: the model's input window is ~1M tokens and its
# output ceiling is ~65K, while one 25-item batch is only ~17K input (text +
# 25 low-res photos) and ~2.5K output. The binding free-tier limit is requests
# per DAY, which counts requests not items, so a LARGER batch stretches your
# daily budget further. Photo-to-item matching at this size is handled by the
# strict per-image labeling in enricher.py (each photo is captioned with its
# item_id + ordinal — see SYSTEM_PROMPT). Only lower this if you ever see
# "could not parse" (truncated-response) lines in the log.
BATCH_SIZE = 25

# Which Gemini model to use for enrichment.
# Check your actual quotas at https://ai.dev/rate-limit (they vary by account!).
# Current free-tier defaults observed in production (May 2026):
#   gemini-3.1-flash-lite  → ~1,500 RPD, ~30 RPM on many accounts (RECOMMENDED)
#   gemini-3-flash         → smaller daily quota, smarter
#   gemini-2.5-flash-lite  → ~1,000 RPD on some accounts
GEMINI_MODEL = "gemini-3.1-flash-lite"

# Sleep between Gemini calls (seconds) PER KEY to stay under the RPM limit.
# 4.5s = ~13 RPM, comfortably under common Flash-Lite ceilings. This applies to
# each worker independently — with two keys the actual request rate is 2× this
# (still well-spaced per Google's per-key books).
GEMINI_DELAY_SECONDS = 4.5

# How many times to retry a single batch when Gemini returns 429/503.
# Each retry honors the server's suggested retryDelay before trying again.
GEMINI_MAX_RETRIES = 3

# If the server says "retry in N seconds" and N is bigger than this, we treat
# it as a daily-quota wall and either swap to the fallback key or stop.
GEMINI_GIVEUP_AFTER_SECONDS = 90


# ─── SEND LISTING PHOTOS TO THE AI ───────────────────────────────────────────
# Classifieds titles + descriptions are often sparse ("dresser, $40, good
# shape"), so a photo carries most of the signal about what the item actually
# is and what condition it's in. When this is on, the enricher fetches the
# listing's first photo and includes it in the SAME batched valuation request,
# interleaved next to that item's text.
#
# IMPORTANT, so you know what this does and doesn't cost:
#   • A text+image request still counts as ONE request against your RPM/RPD
#     quota. Photos do NOT use extra requests — they only add tokens (which is
#     not the binding free-tier constraint here). So enabling this does NOT
#     reduce how many listings you can process per day.
#   • It DOES add one extra image download per new listing at enrichment time,
#     and multimodal calls are a bit slower. We fetch the image bytes, send
#     them inline, and discard them — photos are never saved to the repo.
#   • Photos are matched to the right item by strict per-image labeling in the
#     prompt (each photo is captioned with its item_id and ordinal), so the
#     full BATCH_SIZE works even with a photo on every item.
#
# Set to False to go back to text-only valuation (faster, fewer downloads).
SEND_IMAGES_TO_AI = True

# How many photos per listing to send. 1 (the first/primary photo) is almost
# always enough and keeps requests small. The scraper stores up to this many
# image URLs per item.
MAX_IMAGES_PER_ITEM = 1

# Resolution hint for image inputs: "low", "medium", or "high". "low" uses the
# fewest tokens per image and is plenty for "what is this and what condition".
# Applied defensively — if your model/API version rejects the setting, the
# enricher automatically retries the call without it rather than failing.
IMAGE_MEDIA_RESOLUTION = "low"


# ─── PURCHASE PRICE MODEL ────────────────────────────────────────────────────
# Unlike an auction (where the listed number is a starting bid), a classifieds
# asking price IS roughly the cost — but sellers expect a little haggling. We
# model the realistic out-of-pocket cost as asking_price * NEGOTIATION_FACTOR.
# 0.9 assumes you talk them down ~10% on average. There is no buyer's premium
# or sales tax on a private-party sale, so there's no markup multiplier here.
# Set to 1.0 to score against full asking price.
NEGOTIATION_FACTOR = 1.0

# Pickup hassle fudge factor (dollars subtracted when computing flip score).
# Set a little higher than an auction tool would: classifieds pickups mean
# driving to a stranger's house, coordinating a meetup, sometimes hauling
# furniture. $10 is a reasonable per-pickup friction cost. MUST match the
# HASSLE constant in docs/app.js so the dashboard's numbers agree with the CSV.
PICKUP_HASSLE_DOLLARS = 10.0


# ─── SALES VELOCITY MODEL ────────────────────────────────────────────────────
# Gemini also estimates how quickly an item will sell on Facebook Marketplace
# in the local metro. Tiers map to a numeric score so we can blend it into a
# weighted "smart score" alongside ROI and gross profit.
#
# Don't read these as "days to sell" — Gemini doesn't have real velocity data.
# Treat them as a rank: hot brand-name electronics rank high, generic junk
# ranks low. Useful as ONE input among several, not a precise prediction.
SALES_VELOCITY_SCORES = {
    "hot": 1.0,        # name-brand electronics, tools, popular toys
    "normal": 0.65,    # most household goods, name-brand kitchen items
    "slow": 0.35,      # niche/specialty items, generic clothing, decor
    "very_slow": 0.10, # generic Amazon-brand items, dated fashion, oddities
    "unknown": 0.0,
}


# ─── DATA RETENTION ──────────────────────────────────────────────────────────
# How many days to keep a listing in the dataset after WE FIRST SAW IT.
# Classifieds listings run ~30-45 days, so 30 is a sensible default. Drop it to
# keep the dashboard focused on fresh listings; raise it to keep a longer
# history. Setting this very high grows the repo over time (GitHub's hard limit
# is 100 MB per file).
RETENTION_DAYS = 30

# Pretty-print docs/data/items.json? False = single-line JSON (~30% smaller).
# Set True for human-readable file in git diffs at the cost of size.
PRETTY_PRINT_JSON = False


# ─── TEST MODE ───────────────────────────────────────────────────────────────
# How many items to process when --test is passed. Set to BATCH_SIZE so a test
# run does exactly ONE Gemini batch end-to-end: scrape ~25 newest listings,
# fetch their detail pages + photos, and run a single enrichment call — writing
# to the *_test output files, never the production ones. The GitHub Action's
# "test" run mode runs exactly this. Raise it to exercise more batches.
TEST_MODE_ITEM_LIMIT = 25
