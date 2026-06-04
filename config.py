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
#
# WHY A LIST OF CATEGORIES (not just "/search/sss"):
# The no-JS results page we parse (to stay block-resistant) only serves the
# newest ~400 listings PER search path, total — and the "?s=<offset>" param
# re-serves that same newest set rather than paging deeper. So "/search/sss"
# alone (= ALL for-sale categories) tops out around ~400 of the newest items
# across everything combined, no matter how many pages you ask for.
#
# The fix for both VOLUME and a PROFIT focus is breadth: each category has its
# OWN newest ~few-hundred, so crawling the profitable categories multiplies the
# distinct listings AND concentrates the (limited) Gemini quota on the stuff
# worth flipping. Listings cross-posted to two feeds are de-duped automatically.
#
# The codes below are the REAL Kansas City section codes (pulled from live
# listing URLs), ordered roughly by flip value. "/search/sss" is kept as a
# catch-all backstop so coverage never drops to zero even if a code is off.
#
# TO ADD/REMOVE A CATEGORY: on the site, click a category and copy the code that
# appears after "/search/" in the address bar. After a run, check the log: any
# path that prints "parsed 0 rows" isn't valid for your city — comment it out.
# To go leaner (less quota), trim this to just the few categories you flip most.
SEARCH_PATHS = [
    "/search/sss",   # all for sale — broad backstop (newest ~400 overall)
    "/search/tls",   # tools
    "/search/ele",   # electronics
    "/search/fuo",   # furniture (by owner — where the deals are)
    "/search/app",   # appliances
    "/search/spo",   # sporting goods
    "/search/pho",   # photo + video
    "/search/jwl",   # jewelry
    "/search/atq",   # antiques
    "/search/bik",   # bikes
    "/search/sys",   # computers
    "/search/vgm",   # video gaming
    "/search/msg",   # musical instruments
    "/search/hvo",   # heavy equipment
    "/search/pts",   # auto parts
    "/search/mpo",   # motorcycle parts
    "/search/mcy",   # motorcycles
    "/search/wto",   # wheels + tires
    "/search/tro",   # trailers
    "/search/grd",   # farm + garden
    "/search/mat",   # materials
]

# How many result pages to fetch PER search path. Set to 1 on purpose: as noted
# above, the no-JS feed re-serves the same newest set on "?s=" pages instead of
# paging deeper, so page 2+ is almost entirely duplicates (wasted requests and
# block risk for ~nothing). Volume comes from the category breadth above, not
# from depth on any one feed. Each NEW listing still triggers one detail-page
# fetch; coverage of each category fills in over subsequent runs.
#
# If you ever do want to try deeper paging, raise this AND know that offsets now
# step by PAGE_OFFSET_STEP (the site's true 120-per-page unit), so a higher value
# at least lands on real page boundaries. The crawler still stops a feed early
# the moment a page yields no new listings.
MAX_PAGES_PER_SEARCH = 1

# Pagination offset unit. The site paginates in fixed 120-item steps, so page N
# is "?s=<120*N>". (Earlier this stepped by the parsed row count, which landed
# between real pages and forced duplicate results.) Only matters if you raise
# MAX_PAGES_PER_SEARCH above 1.
PAGE_OFFSET_STEP = 120


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
# scraper stops detail-page fetches; the leftover listings get fetched on a
# subsequent run (they're cached without the detail flag) and enriched then.
# At ~2s/page this allows roughly 3,000 detail fetches, enough to cover a full
# ~3,000-item first run when the source isn't throttling. It's a hard ceiling
# so a blocked/slow run can't blow past the workflow's 240-minute timeout —
# detail fetching just stops and enrichment proceeds on whatever was fetched.
SCRAPE_DETAIL_PAGE_TIME_BUDGET_SECONDS = 6500  # ~108 min


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
# The cost to acquire an item is its effective price as-is — the model's
# per-item effective price (or the headline price for a trustworthy single
# item). No negotiation discount is applied: scoring is against the price you
# actually see, so only listings priced BELOW their resale value float up as
# real deals. (There's also no buyer's premium or sales tax on a private sale.)

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

# Skip listings POSTED more than this many days ago: don't enrich them and don't
# score them, so the (limited) Gemini quota and the dashboard both stay on fresh
# deals. This is distinct from RETENTION_DAYS above — that counts from when WE
# first saw a listing; this counts from the listing's own POST date, so a months-
# old post we only just discovered gets skipped too. Set to 0 to disable.
#
# Caveat: this keys off the original post date. A stale listing the seller keeps
# "renewing" shows a fresh position in the feed but an old post date, so it gets
# filtered here — usually the right call (long-unsold = picked over). Listings
# whose date can't be parsed are kept (we don't filter on missing data).
MAX_LISTING_AGE_DAYS = 60

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
