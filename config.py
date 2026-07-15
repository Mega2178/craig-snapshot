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


# ─── DISCOVERY (primary) ─────────────────────────────────────────────────────
# Discovery no longer relies on the shallow no-JS result pages above (which only
# ever serve the newest few hundred rows per path). Instead we enumerate the
# metro's live for-sale inventory through the site's own results API, which
# returns the full set in a handful of requests. The SEARCH_PATHS/MAX_PAGES knobs
# above are retained only for the legacy path in scraper.py (not used by default).
#
# AREA ID: the API addresses a metro by a numeric area id, NOT the subdomain
# string. kansascity = 30. If you point SITE_SUBDOMAIN at another metro, set the
# matching id here (read it off the "batch=<id>-..." request the site issues in a
# browser's Network tab).
DISCOVERY_AREA_ID = 30
DISCOVERY_ENDPOINT = "https://sapi.craigslist.org/web/v8/postings/search/full"
DISCOVERY_SEARCH_PATH = "sss"   # the whole for-sale section (every for-sale category)

# The API serves at most 10,000 rows per query, so a single "everything" pull
# would top out at 10k of ~25k. We defeat that by pulling complete price slices
# (each slice is well under 10k, so every priced listing in it comes back) plus
# one newest-first pull to pick up listings that carry no price (they fall in no
# slice). Each (min, max) is inclusive; max=None means "no upper bound". If a
# slice ever approaches 10k (logged as a warning), split it further.
DISCOVERY_PRICE_SLICES = [(0, 25), (26, 100), (101, 500), (501, 2500), (2501, None)]
DISCOVERY_NEWEST_PASS = True
# The API ignores an oldest-first sort and won't page past row 10,000, so a
# listing with NO price whose last renewal is older than the newest-10k window is
# not reachable (a small, unscoreable residual — priced listings are unaffected,
# the slices catch them all regardless of renewal age).

# Category ids to EXCLUDE from discovery. Everything else in the for-sale section
# stays in scope (including free/no-price listings, parts, motorcycles). Ids are
# the site's own category codes — tweak freely.
#   • Every "by dealer" category (dealer listings are excluded entirely)
#   • Boats: 119 (by owner); 164 (by dealer) is already in the dealer set
#   • Cars & trucks: 145 (by owner); 146 (by dealer) is already in the dealer set
DISCOVERY_EXCLUDED_CATEGORY_IDS = {
    # "by dealer" categories
    142, 146, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172,
    173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187,
    188, 189, 190, 192, 194, 196, 198, 200, 202, 204, 206, 209,
    # boats (by owner) and cars & trucks (by owner)
    119, 145,
}

# Abort the run (before any purge/write) if the section reports fewer than this
# many total listings — a floor that catches the API/section breaking or a soft
# block, rather than quietly discovering almost nothing. Normal is ~25,000.
MIN_CENSUS_TOTAL = 5000

# Safety margin (days) added to the retention window when pre-filtering discovery
# candidates by their estimated original post date. Candidates estimated older
# than RETENTION_DAYS + this are skipped (not detail-fetched); the exact post date
# from the detail page still governs the final keep/drop, so the margin only
# guards against interpolation error at the boundary.
DISCOVERY_POST_DATE_MARGIN_DAYS = 2

# Escape hatch: set True to use the legacy no-JS feed crawl (SEARCH_PATHS above)
# instead of the results-API enumeration. Off by default and never selected
# automatically — a discovery failure aborts the run rather than silently
# reverting to the shallower crawl.
LEGACY_FEED_DISCOVERY = False


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
# At ~2s/page this allows roughly 5,700 detail fetches in one run — enough to
# cover a full re-discovery after a long gap while still leaving time for the
# enrichment pass to finish inside the workflow timeout. This is the practical
# governor: on a very large backlog it (not the count cap below) is what bounds
# the run, so a blocked/slow run can't blow past the job's timeout — detail
# fetching just stops and enrichment proceeds on whatever was fetched, with the
# remainder draining over later runs.
SCRAPE_DETAIL_PAGE_TIME_BUDGET_SECONDS = 12000  # ~200 min


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
# How many days to keep a listing, measured from its ORIGINAL post date (the
# exact date read off the detail page). A listing renewed/bumped to look fresh
# but originally posted longer ago than this is dropped — the window tracks the
# real post date, not the renewal. Listings with no readable post date yet (e.g.
# recorded but not detail-fetched) fall back to when WE first saw them, so they
# survive to be detail-fetched on a later run rather than being dropped blind.
# Classifieds listings run ~30-45 days, so 30 keeps the set to the fresh window.
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


# ─── RUN SAFETY GUARDS ───────────────────────────────────────────────────────
# The run refuses to overwrite the committed dataset when it looks broken or
# partial, so a parser break or a mid-run network failure can never silently
# replace good data with an empty/collapsed snapshot.

# Minimum listings a run must parse across ALL feeds before it may purge stale
# items or write any output. A healthy run discovers hundreds to thousands (one
# feed alone serves a few hundred). A run that parses fewer than this is treated
# as broken/partial: it exits non-zero and writes nothing, leaving the previous
# data intact for the next run.
MIN_HEALTHY_PARSED_ITEMS = 25

# Refuse to overwrite the dataset if it would lose more than this fraction of its
# items for reasons OTHER than normal retention expiry (old listings aging out is
# expected and does not count toward this). Catches a partial/corrupt load or an
# unexpected mass-delete before it is committed. 0.20 = 20%.
MAX_DATASET_SHRINK_FRACTION = 0.20

# Upper bound on how many listing detail pages one run will fetch. The first run
# after a long gap can discover thousands of new listings at once; at the polite
# per-request delay, fetching a detail page for every one in a single run could
# exceed the CI job's time budget. New listings past this cap are still recorded
# (so they are rediscovered and detail-fetched on later runs) — the backlog just
# drains over several runs. The detail budget is spread round-robin across the
# feeds (see crawl_all), so a cap never starves the later categories. In practice
# SCRAPE_DETAIL_PAGE_TIME_BUDGET_SECONDS above is reached first on a very large
# backlog, so this count is a generous safety ceiling rather than the usual
# limiter. Set to 0 (or negative) to disable the count cap and rely on the time
# budget alone.
MAX_NEW_DETAIL_FETCHES_PER_RUN = 7000


# ─── DISCOVERY DRIFT ALARM (warn-only) ───────────────────────────────────────
# A soft, non-aborting check layered on top of the hard guards above. After a
# healthy run it records a small rolling history of per-run counts and, once
# enough history exists, warns loudly (a CI ::warning:: annotation) when a run's
# parsed count collapses far below its recent norm — the degraded-but-nonzero
# result the hard guards (zero-parse / floor / shrink) don't catch. It never
# aborts and never blocks a commit; it only flags a run for a human to eyeball.

# Where the rolling per-run history is stored (committed, small). Kept next to
# the raw cache.
RUN_STATS_FILENAME = "run_stats.json"

# How many recent runs to retain in that history file.
RUN_STATS_MAX_ENTRIES = 50

# Minimum number of prior runs required before the drift check does anything, so
# the first runs on a fresh history never warn.
DISCOVERY_DRIFT_MIN_HISTORY = 3

# How many trailing runs to take the median over when judging "normal".
DISCOVERY_DRIFT_MEDIAN_WINDOW = 5

# Warn when this run's parsed count is below this fraction of that trailing
# median. 0.40 = warn if discovery drops under 40% of the recent norm.
DISCOVERY_DRIFT_WARN_FRACTION = 0.40


# ─── TEST MODE ───────────────────────────────────────────────────────────────
# How many items to process when --test is passed. Set to BATCH_SIZE so a test
# run does exactly ONE Gemini batch end-to-end: scrape ~25 newest listings,
# fetch their detail pages + photos, and run a single enrichment call — writing
# to the *_test output files, never the production ones. The GitHub Action's
# "test" run mode runs exactly this. Raise it to exercise more batches.
TEST_MODE_ITEM_LIMIT = 25
