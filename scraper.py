"""
Scraper for a public classifieds source. No browser needed — just requests + bs4.

Two phases:
  1. crawl_all(session, existing) → enumerates the metro's live for-sale
                                   listings through the site's results API
                                   (complete price slices + a newest pass) and
                                   yields the in-scope, in-window ones as Items
                                   (title, price, URL, category, post ids).
  2. fetch_item_detail(session, item) → fetches one listing's own page and
                                   fills in the full description, photo URLs,
                                   location, and posted/updated timestamps.

A legacy crawler that parses the no-JS search-results pages
(<ol class="cl-static-search-results">) is kept behind
config.LEGACY_FEED_DISCOVERY for manual use; it only ever sees the newest few
hundred rows per feed. If the site changes markup/format, the spots to adjust
are _decode_discovery_item(), parse_search_results(), and fetch_item_detail().
"""
from __future__ import annotations

import bisect
import json
import re
import statistics
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urljoin, urlencode, urlparse

import requests
from bs4 import BeautifulSoup, Tag

import config

BASE_URL = f"https://{config.SITE_SUBDOMAIN}.craigslist.org"


class DiscoveryError(Exception):
    """Discovery could not enumerate the source (endpoint down, response shape
    changed, or an implausibly small result set). Raised so the caller aborts the
    run BEFORE any purge/write — never silently falling back to a partial set."""


# Numeric section id → short human label, for the `category` display field. Only
# the for-sale sections appear via discovery; unknown ids fall through to the id.
_CATEGORY_LABELS = {
    5: "general for sale", 7: "computers", 20: "wanted", 42: "barter",
    44: "tickets", 68: "bicycles", 69: "motorcycles", 73: "garage sale",
    92: "books", 93: "sporting goods", 94: "clothing", 95: "collectibles",
    96: "electronics", 97: "household", 98: "musical instruments",
    101: "free stuff", 107: "baby & kid", 117: "cds / dvds", 118: "tools",
    119: "boats", 120: "jewelry", 122: "auto parts", 124: "rvs",
    132: "toys & games", 133: "farm & garden", 134: "business/commercial",
    135: "arts & crafts", 136: "materials", 137: "photo/video", 141: "furniture",
    145: "cars & trucks", 149: "appliances", 150: "antiques", 151: "video gaming",
    152: "health & beauty", 153: "cell phones", 191: "atvs/utvs/snowmobiles",
    193: "heavy equipment", 195: "motorcycle parts", 197: "bicycle parts",
    199: "computer parts", 201: "boat parts", 203: "wheels & tires",
    205: "trailers", 208: "aviation",
}


def _category_label(category_id: int) -> str:
    return _CATEGORY_LABELS.get(category_id, str(category_id))

# How many "sample" scraped items to echo to the log across the ENTIRE run, so
# you can eyeball that the scraper is pulling real titles/prices and not empty
# cards. Kept deliberately small so it never floods the GitHub Actions log.
# Set to 0 to disable sample prints entirely.
SAMPLE_ITEM_PRINT_LIMIT = 3

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ────────────────────────────── data class ──────────────────────────────────

@dataclass
class Item:
    post_id: str = ""          # dedup key: listing token (current URLs) or numeric id (legacy)
    numeric_post_id: str = ""  # numeric "post id:" read off the detail page, when present
    title: str = ""
    price: str = ""            # raw, e.g. "$1,600"
    price_value: float = 0.0   # parsed numeric (0 = free / unparsed)
    image_url: str = ""        # primary photo (filled from the detail page)
    image_urls: list = field(default_factory=list)  # up to MAX_IMAGES_PER_ITEM
    item_url: str = ""
    category: str = ""         # human-ish label (from the section code the listing sits in)
    category_id: str = ""      # numeric section id from discovery (stable, for filtering)
    location: str = ""         # neighborhood / city text the seller entered
    description: str = ""      # full freeform body (from the detail page)
    posted_at: str = ""        # ISO, when the seller posted (detail page)
    updated_at: str = ""       # ISO, last edited by the seller (detail page)
    # When WE first saw this listing. Set once, used for retention/purge and
    # for the "seen N days ago" footer fallback.
    first_seen_at: str = ""
    # True once we've fetched the per-item detail page and pulled its full
    # description / photos. Cached so we don't re-fetch every run.
    description_enriched: bool = False
    # AI enrichment fields (filled later by the enricher)
    ai_retail_estimate: str = ""
    ai_resale_pct: str = ""
    ai_estimated_resale: str = ""
    ai_confidence: str = ""
    ai_condition: str = ""        # new/open_box/damaged_easy_fix/damaged_hard_fix
    ai_sales_velocity: str = ""    # hot/normal/slow/very_slow/unknown
    ai_product: str = ""           # what the model decided this (one) item is
    ai_listing_kind: str = ""      # single_item/multi_item/not_for_sale
    ai_price_status: str = ""      # priced / free / unknown (how to read the price)
    ai_effective_price: str = ""   # model's realistic cash price for the valued item ("" = unknown)
    value_overridden: str = ""     # "yes" if we forced resale to $0
    ai_notes: str = ""
    cost_basis: str = ""           # where the scored cost came from: ai_effective/listed/free/unknown/not_for_sale
    flip_score: str = ""  # (effective_resale - purchase_price - hassle) / purchase_price
    gross_profit: str = ""  # effective_resale - purchase_price - hassle (in dollars)
    scraped_at: str = ""   # last time this run touched the item
    enriched_at: str = ""

    def key(self) -> str:
        return self.post_id


# ────────────────────────────── HTTP session ────────────────────────────────

class Session:
    """Polite HTTP session with throttling + retries."""

    def __init__(self, delay: float = 1.0):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.delay = delay
        self.last_request = 0.0

    def get(self, url: str, retries: int = 3,
            headers: dict | None = None, timeout: int = 30) -> str:
        elapsed = time.time() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=timeout, headers=headers)
                self.last_request = time.time()
                if r.status_code == 200:
                    return r.text
                if r.status_code in (429, 503):
                    wait = (attempt + 1) * 5
                    print(f"  [{r.status_code}] backing off {wait}s...")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
            except requests.RequestException as e:
                last_err = e
                wait = (attempt + 1) * 3
                print(f"  request error ({e}); retry in {wait}s")
                time.sleep(wait)

        raise RuntimeError(f"GET {url} failed after {retries} retries: {last_err}")


# ────────────────────────────── URL building ────────────────────────────────

def build_search_url(path: str, offset: int = 0) -> str:
    """Build a search-results URL for one category path + page offset."""
    url = urljoin(BASE_URL, path)
    if offset:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'s': offset})}"
    return url


# ────────────────────────────── helpers ─────────────────────────────────────

# Current scheme: ".../view/d/<slug>/<token>". The trailing token (base62-ish,
# may include '-' and '_') is the stable per-listing key.
_POST_TOKEN_RE = re.compile(
    r"/view/[a-z]/[^/]+/([A-Za-z0-9_-]+)/?(?:[?#]|$)", re.IGNORECASE
)
# Legacy scheme, still valid on older cached listings: ".../<digits>.html".
_POST_ID_RE = re.compile(r"/(\d+)\.html(?:[?#]|$)")
# Numeric "post id:" printed on a listing's own detail page.
_DETAIL_POST_ID_RE = re.compile(r"post id:\s*(\d+)", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
# The URL section right after the host on LEGACY urls: ".../spo/d/slug/123.html"
# → "spo". The current "/view/d/..." scheme carries no section code, so category
# is taken from the search feed instead (see _category_from_url).
_URL_SECTION_RE = re.compile(r"^https?://[^/]+/([a-z]{3})/", re.IGNORECASE)

# Friendly-ish labels for the common URL section codes (the 3-letter segment
# right after the domain). Unknown codes pass through as-is. This is only for
# display; scoring doesn't depend on it.
_SECTION_LABELS = {
    "ela": "electronics", "sys": "computers", "tls": "tools", "tld": "tools",
    "ppd": "appliances", "hsd": "household", "hsh": "household",
    "ssd": "general", "spo": "sporting", "spd": "sporting",
    "bik": "bikes", "bid": "bikes", "msg": "musical", "msd": "musical",
    "fud": "furniture", "fuo": "furniture", "atd": "antiques", "atq": "antiques",
    "vgd": "video gaming", "clt": "clothing", "clo": "clothing",
    "jwd": "jewelry", "grd": "garden", "gms": "garage sale", "grg": "garage sale",
    "hvd": "heavy equipment", "mcy": "motorcycle parts", "for": "general",
    "art": "arts+crafts", "bar": "barter", "hab": "household", "tag": "garage sale",
}

# Friendly labels for the search-feed codes in config.SEARCH_PATHS. Used to label
# a listing by the feed it was found under, since the current listing-URL scheme
# no longer carries a per-listing section code.
_SEARCH_CODE_LABELS = {
    "sss": "for sale", "tls": "tools", "ele": "electronics", "fuo": "furniture",
    "app": "appliances", "spo": "sporting", "pho": "photo+video", "jwl": "jewelry",
    "atq": "antiques", "bik": "bikes", "sys": "computers", "vgm": "video gaming",
    "msg": "musical", "hvo": "heavy equipment", "pts": "auto parts",
    "mpo": "motorcycle parts", "mcy": "motorcycles", "wto": "wheels+tires",
    "tro": "trailers", "grd": "farm+garden", "mat": "materials",
}


def _post_id_from_url(url: str) -> str:
    """Stable per-listing key from a listing URL.

    Current scheme  ".../view/d/<slug>/<token>"  → the token.
    Legacy scheme   ".../<digits>.html"           → the digits.
    Returns "" when neither shape is present.
    """
    u = url or ""
    m = _POST_TOKEN_RE.search(u)
    if m:
        return m.group(1)
    m = _POST_ID_RE.search(u)
    return m.group(1) if m else ""


def _parse_price(text: str) -> tuple[str, float]:
    """Return ('$1,600', 1600.0) from a price string. ('', 0.0) if none."""
    if not text:
        return "", 0.0
    m = _PRICE_RE.search(text)
    if not m:
        return "", 0.0
    raw = "$" + m.group(1)
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        val = 0.0
    return raw, val


def _category_from_url(url: str, search_path: str) -> str:
    """Human-ish category for a listing.

    Legacy listing URLs put the section code right after the host
    (".../tls/d/..."), read from there when present. The current
    ".../view/d/..." scheme carries no section code, so we fall back to the feed
    (search_path) the listing was found under — the crawler always knows which
    feed it is fetching — mapped to a friendly label. Either way a listing never
    ends up category-less.
    """
    m = _URL_SECTION_RE.match(url or "")
    if m:
        code = m.group(1).lower()
        return _SECTION_LABELS.get(code, code)
    code = (search_path or "").rstrip("/").split("/")[-1].lower()
    return _SEARCH_CODE_LABELS.get(code, _SECTION_LABELS.get(code, code)) or ""


def _first_text(node: Tag, selectors: list[str]) -> str:
    """Return stripped text of the first matching child selector, else ''."""
    for sel in selectors:
        el = node.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return ""


# ────────────────────────────── search parsing ──────────────────────────────

def parse_search_results(html: str, search_path: str) -> list[Item]:
    """Parse one search-results page into Items (title/price/location/url).

    Targets the no-JS fallback list <ol class="cl-static-search-results"> with
    <li class="cl-static-search-result"> rows. Falls back to older row classes
    and finally to any anchor that links to a "<digits>.html" posting, so a
    markup tweak degrades gracefully instead of returning nothing.
    """
    soup = BeautifulSoup(html, "html.parser")

    rows = soup.select("li.cl-static-search-result")
    if not rows:
        rows = soup.select("li.cl-search-result, li.result-row")

    items: list[Item] = []
    seen_ids: set[str] = set()

    if rows:
        for li in rows:
            a = li.find("a", href=True)
            if not a:
                continue
            url = urljoin(BASE_URL, a["href"])
            post_id = _post_id_from_url(url)
            if not post_id or post_id in seen_ids:
                continue

            title = (
                li.get("title", "").strip()
                or _first_text(li, [".title", ".posting-title .label",
                                    ".result-title", ".titlestring"])
                or a.get_text(" ", strip=True)
            )
            price_text = _first_text(li, [".price", ".result-price"])
            price_raw, price_val = _parse_price(price_text)
            location = _first_text(
                li, [".location", ".result-hood", ".meta .location"]
            ).strip("()")

            # The static list usually has no thumbnail (the gallery loads via
            # JS), but grab one if it's present so the card has an image even
            # before the detail page is fetched.
            img_url = ""
            img = li.find("img")
            if img:
                img_url = (img.get("src") or img.get("data-src") or "").strip()

            seen_ids.add(post_id)
            it = Item(
                post_id=post_id,
                title=title,
                price=price_raw,
                price_value=price_val,
                item_url=url,
                location=location,
                category=_category_from_url(url, search_path),
            )
            if img_url:
                it.image_url = img_url
                it.image_urls = [img_url]
            items.append(it)
        return items

    # Last-resort fallback: scan every posting anchor on the page.
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE_URL, a["href"])
        post_id = _post_id_from_url(url)
        if not post_id or post_id in seen_ids:
            continue
        title = a.get("title", "").strip() or a.get_text(" ", strip=True)
        if not title:
            continue
        seen_ids.add(post_id)
        items.append(Item(
            post_id=post_id,
            title=title,
            item_url=url,
            category=_category_from_url(url, search_path),
        ))
    return items


# ── legacy feed crawl (dormant; enable via config.LEGACY_FEED_DISCOVERY) ─────

def _legacy_feed_crawl(session: Session, max_items: int | None = None) -> Iterator[Item]:
    """The previous discovery path: parse the no-JS result pages of each
    configured search path, round-robin interleaved. Superseded by the results-API
    enumeration in crawl_all (which reaches the whole section, not just the newest
    few hundred per path). Retained for manual use only — never used as an
    automatic fallback. Set config.LEGACY_FEED_DISCOVERY = True to select it.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen: set[str] = set()
    printed = 0
    per_feed: list[list[Item]] = []
    collected = 0

    for path in config.SEARCH_PATHS:
        feed_items: list[Item] = []
        for page in range(config.MAX_PAGES_PER_SEARCH):
            offset = config.PAGE_OFFSET_STEP * page
            url = build_search_url(path, offset)
            print(f"  fetching {url}")
            try:
                html = session.get(url)
            except Exception as e:
                print(f"  ! failed to fetch {url}: {e}")
                break

            page_items = parse_search_results(html, path)
            if not page_items:
                break

            new_on_page = 0
            for it in page_items:
                if it.post_id in seen:
                    continue
                seen.add(it.post_id)
                it.scraped_at = now
                feed_items.append(it)
                new_on_page += 1
                collected += 1
                if printed < SAMPLE_ITEM_PRINT_LIMIT:
                    _print_sample_item(it)
                    printed += 1

            print(f"    parsed {len(page_items)} rows ({new_on_page} new)")
            if new_on_page == 0:
                break

        per_feed.append(feed_items)
        if max_items is not None and collected >= max_items:
            break

    from itertools import zip_longest
    _SENTINEL = object()
    for rotation in zip_longest(*per_feed, fillvalue=_SENTINEL):
        for it in rotation:
            if it is not _SENTINEL:
                yield it


# ── original-post-date estimate from the id→date curve ──────────────────────

def _build_post_date_estimator(existing: dict):
    """Build est(numeric_id) → original-post timestamp from cached anchor pairs.

    Listing numeric ids are globally sequential, so a listing's original post
    date interpolates cleanly from the (numeric_id, posted_at) pairs we already
    hold. Used only to PRE-FILTER discovery candidates by age (skip clearly-old
    ones before spending a detail fetch); the exact detail-page date still
    governs the final keep/drop. Returns None when there are too few anchors.
    """
    pairs = []
    for it in existing.values():
        nid = (getattr(it, "numeric_post_id", "") or "")
        pa = (getattr(it, "posted_at", "") or "")
        if nid.isdigit() and pa:
            try:
                pairs.append((int(nid),
                              datetime.fromisoformat(pa.replace("Z", "+00:00")).timestamp()))
            except ValueError:
                continue
    if len(pairs) < 50:
        return None
    pairs.sort()
    ts = [p[1] for p in pairs]
    knot_ids, knot_ts = [], []
    for i in range(0, len(pairs), 10):  # rolling-median knots, robust to outliers
        lo, hi = max(0, i - 25), min(len(pairs), i + 26)
        knot_ids.append(pairs[i][0])
        knot_ts.append(statistics.median(ts[lo:hi]))

    def est(nid: int) -> float:
        i = bisect.bisect_left(knot_ids, nid)
        if i <= 0:
            return knot_ts[0]
        if i >= len(knot_ids):
            return knot_ts[-1]
        x0, x1, y0, y1 = knot_ids[i - 1], knot_ids[i], knot_ts[i - 1], knot_ts[i]
        return y0 if x1 == x0 else y0 + (y1 - y0) * (nid - x0) / (x1 - x0)

    return est


# ── results-API discovery ────────────────────────────────────────────────────

_DISCOVERY_TOKEN_CODE = 13   # sub-element [13, "<token>"] carries the listing token
_DISCOVERY_SLUG_CODE = 6     # sub-element [6, "<slug>"] carries the URL slug


def _discovery_headers() -> dict:
    sp = getattr(config, "DISCOVERY_SEARCH_PATH", "sss")
    return {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.craigslist.org/search/area/{config.SITE_SUBDOMAIN}?cat={sp}",
        "Origin": "https://www.craigslist.org",
    }


def _decode_discovery_item(item: list, decode: dict):
    """Decode one compact-with-detail row → (nid, catid, price, token, slug, title).

    Layout: [id_offset, date_offset, category_id, price, geo, code, …sub-lists…,
    title]; sub-lists [13,token] and [6,slug] carry the listing token and URL
    slug. Returns None if the row can't be decoded (e.g. no token → unreachable).
    """
    try:
        nid = decode["minPostingId"] + item[0]
        catid = item[2]
        price = item[3]
    except (KeyError, IndexError, TypeError):
        return None
    token = slug = None
    for el in item:
        if isinstance(el, list) and el:
            if el[0] == _DISCOVERY_TOKEN_CODE:
                token = el[1]
            elif el[0] == _DISCOVERY_SLUG_CODE:
                slug = el[1]
    title = item[-1] if (item and isinstance(item[-1], str)) else ""
    return nid, catid, price, token, slug, title


def _fetch_discovery_slice(session: Session, area: int, label: str,
                           price_slice: tuple | None):
    """Fetch one results-API slice; return (items, decode, total_reported).

    Raises DiscoveryError on any transport/shape failure so the run aborts before
    writing rather than proceeding on a partial set.
    """
    endpoint = getattr(config, "DISCOVERY_ENDPOINT",
                       "https://sapi.craigslist.org/web/v8/postings/search/full")
    sp = getattr(config, "DISCOVERY_SEARCH_PATH", "sss")
    params = f"batch={area}-0-10000-1-0&cc=US&lang=en&searchPath={sp}"
    if price_slice is not None:
        lo, hi = price_slice
        params += f"&min_price={lo}"
        if hi is not None:
            params += f"&max_price={hi}"
    url = f"{endpoint}?{params}"
    try:
        # Full-format responses run to ~10 MB; a flaky connection can cut one
        # mid-body. Retry a little harder than a page fetch (these are only ~6
        # requests per run and each retry is cheap next to aborting the run).
        text = session.get(url, retries=4, headers=_discovery_headers(), timeout=120)
        payload = json.loads(text)
        data = payload["data"]
        items = data["items"]
        decode = data["decode"]
    except Exception as e:
        raise DiscoveryError(f"{label}: {e}") from e
    return items, decode, data.get("totalResultCount")


def crawl_all(session: Session, existing: dict | None = None,
              max_items: int | None = None) -> Iterator[Item]:
    """Enumerate the metro's live for-sale listings via the results API and yield
    the in-scope, in-window ones as Items (keyed by listing token, exactly like
    the legacy feed crawl so the rest of the pipeline is unchanged).

    Volume comes from enumeration, not feed breadth: complete price slices plus a
    newest-first pass return essentially the whole for-sale section in ~6 requests
    (each row already carries its token, so no extra lookup). Out-of-scope
    categories (see config.DISCOVERY_EXCLUDED_CATEGORY_IDS) are dropped, and
    candidates whose estimated original post date is older than the retention
    window (plus a safety margin) are skipped so a detail fetch isn't spent on
    them — the exact post date from the detail page makes the final call.

    On any source failure — endpoint down, response shape changed, or an
    implausibly small result set — raises DiscoveryError so the caller aborts
    before purge/write. There is deliberately NO fallback to the legacy crawl.

    max_items (test smoke) fetches only the newest pass and yields that many.
    """
    if getattr(config, "LEGACY_FEED_DISCOVERY", False):
        print("  discovery: LEGACY_FEED_DISCOVERY on — using the no-JS feed crawl")
        yield from _legacy_feed_crawl(session, max_items=max_items)
        return

    existing = existing or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    area = getattr(config, "DISCOVERY_AREA_ID", 30)
    excluded = set(getattr(config, "DISCOVERY_EXCLUDED_CATEGORY_IDS", ()))
    test_mode = max_items is not None

    print("\n  discovery: enumerating the for-sale section via the results API")
    slices: list[tuple[str, tuple | None]] = []
    if test_mode:
        slices.append(("newest (test)", None))
    else:
        for lo, hi in getattr(config, "DISCOVERY_PRICE_SLICES", []):
            slices.append((f"price ${lo}-{hi if hi is not None else 'up'}", (lo, hi)))
        if getattr(config, "DISCOVERY_NEWEST_PASS", True):
            slices.append(("newest", None))

    census: dict[int, tuple] = {}   # nid -> (catid, price, token, slug, title)
    section_total = 0
    for label, price_slice in slices:
        items, decode, total = _fetch_discovery_slice(session, area, label, price_slice)
        section_total = max(section_total, total or 0)
        n_new = n_tok = 0
        for item in items:
            row = _decode_discovery_item(item, decode)
            if row is None:
                continue
            nid, catid, price, token, slug, title = row
            if token:
                n_tok += 1
            if nid not in census:
                census[nid] = (catid, price, token, slug, title)
                n_new += 1
        print(f"    {label}: {len(items)} rows (section total={total}), "
              f"{n_new} new → {len(census)} enumerated")
        # Token-presence guard: the full format carries a token on every row. A
        # response missing them means the format changed → abort, don't ingest.
        if items and n_tok < 0.5 * len(items):
            raise DiscoveryError(
                f"{label}: only {n_tok}/{len(items)} rows carried a listing token "
                f"— results format changed")
        if price_slice is not None and (total or 0) >= 10000:
            print(f"    ! WARNING: slice {label!r} reports {total} rows (>=10000 cap); "
                  f"some may be unreachable — split this price slice in config")

    # Format-break / block canary: the section normally reports ~25k listings.
    if not test_mode:
        floor = getattr(config, "MIN_CENSUS_TOTAL", 5000)
        if section_total < floor or len(census) < floor:
            raise DiscoveryError(
                f"section reported {section_total} listings / {len(census)} enumerated "
                f"(< {floor}) — source is broken or blocking; refusing to continue")

    # Window pre-filter: skip candidates estimated older than the retention window.
    est = _build_post_date_estimator(existing)
    known_posted: dict[int, float] = {}
    for it in existing.values():
        nid = (getattr(it, "numeric_post_id", "") or "")
        pa = (getattr(it, "posted_at", "") or "")
        if nid.isdigit() and pa:
            try:
                known_posted[int(nid)] = datetime.fromisoformat(
                    pa.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    retention_days = getattr(config, "RETENTION_DAYS", 30)
    margin = getattr(config, "DISCOVERY_POST_DATE_MARGIN_DAYS", 2)
    cutoff_ts = None
    if est is not None and retention_days >= 0:
        cutoff_ts = datetime.now(timezone.utc).timestamp() - (retention_days + margin) * 86400

    candidates = []
    n_excluded = n_old = 0
    for nid, (catid, price, token, slug, title) in census.items():
        if catid in excluded:
            n_excluded += 1
            continue
        if not token:
            continue
        if cutoff_ts is not None:
            post_ts = known_posted.get(nid) or est(nid)
            if post_ts < cutoff_ts:
                n_old += 1
                continue
        candidates.append((nid, catid, price, token, slug, title))

    # Newest post first, so a capped detail budget always covers the freshest.
    candidates.sort(key=lambda c: c[0], reverse=True)
    print(f"  discovery: {len(census)} enumerated → {len(candidates)} in-scope in-window "
          f"({n_excluded} excluded-category, {n_old} out-of-window skipped)")

    printed = 0
    yielded = 0
    for nid, catid, price, token, slug, title in candidates:
        it = Item(
            post_id=token,
            numeric_post_id=str(nid),
            title=title or "",
            item_url=f"https://www.craigslist.org/view/d/{slug or 'x'}/{token}",
            category=_category_label(catid),
            category_id=str(catid),
        )
        if isinstance(price, (int, float)) and price > 0:
            it.price = f"${int(price):,}"
            it.price_value = float(price)
        it.scraped_at = now
        if printed < SAMPLE_ITEM_PRINT_LIMIT:
            _print_sample_item(it)
            printed += 1
        yield it
        yielded += 1
        if max_items is not None and yielded >= max_items:
            return


def _print_sample_item(it: Item) -> None:
    print(f"    • [{it.post_id}] {it.title[:70]!r} "
          f"{it.price or '(no price)'} — {it.location or '(no location)'}")


# ────────────────────────────── detail page ─────────────────────────────────

_LDJSON_RE = re.compile(
    r'<script[^>]+id=["\']ld_posting_data["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_QR_NOISE_RE = re.compile(r"QR Code Link to This Post\s*", re.IGNORECASE)


def _normalize_iso(value: str) -> str:
    """Normalize a datetime attribute to an ISO-8601 string. '' on failure.

    The site emits values like '2026-05-31T09:14:02-0500' (or with a colon in
    the offset). datetime.fromisoformat handles most shapes once we insert the
    missing colon in a +HHMM offset.
    """
    if not value:
        return ""
    v = value.strip()
    # Insert a colon into a trailing +HHMM / -HHMM offset if needed.
    m = re.search(r"([+-]\d{2})(\d{2})$", v)
    if m:
        v = v[: m.start()] + f"{m.group(1)}:{m.group(2)}"
    try:
        dt = datetime.fromisoformat(v)
        return dt.isoformat(timespec="seconds")
    except ValueError:
        return value  # keep whatever we got rather than dropping it


def _extract_ldjson(html: str) -> dict:
    m = _LDJSON_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1).strip())
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def fetch_item_detail(session: Session, item: Item) -> None:
    """Fetch one listing's page and fill in description, photos, timestamps.

    Sets item.description_enriched=True on success. Never raises — on any
    failure it logs and leaves the item as-is (it'll be retried next run).
    """
    if not item.item_url:
        return
    try:
        html = session.get(item.item_url)
    except Exception as e:
        print(f"  ! detail fetch failed for {item.post_id}: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    ld = _extract_ldjson(html)

    # ── Description: prefer the full freeform body; fall back to JSON-LD. ──
    body = soup.select_one("#postingbody")
    if body:
        # Drop the "QR Code Link to This Post" helper text the site injects.
        for tag in body.select(".print-information, .print-qrcode-container"):
            tag.decompose()
        text = body.get_text("\n", strip=True)
        text = _QR_NOISE_RE.sub("", text).strip()
        if text:
            item.description = text
    if not item.description and ld.get("description"):
        item.description = str(ld["description"]).strip()

    # ── Photos: JSON-LD carries the full-size image list. ──
    images: list[str] = []
    ld_img = ld.get("image")
    if isinstance(ld_img, list):
        images = [str(u) for u in ld_img if u]
    elif isinstance(ld_img, str) and ld_img:
        images = [ld_img]
    if not images:
        # Fall back to the thumbnail anchors / gallery images in the markup.
        for el in soup.select("#thumbs a[href], .gallery img[src], figure img[src]"):
            u = el.get("href") or el.get("src")
            if u:
                images.append(u)
    if images:
        limit = max(1, int(config.MAX_IMAGES_PER_ITEM))
        item.image_urls = images[:limit]
        item.image_url = item.image_urls[0]

    # ── Price / location refinements from JSON-LD, if we didn't get them. ──
    if (not item.price_value) and isinstance(ld.get("offers"), dict):
        price = ld["offers"].get("price")
        if price is not None:
            raw, val = _parse_price(f"${price}")
            if val:
                item.price, item.price_value = raw, val

    # ── Location: the place text shown in parens after the title (results-API
    #    discovery carries only coordinates, not this text). Current markup is a
    #    plain "(City, ST)" span inside .postingtitletext (a .price span may sit
    #    between it and the title); older pages used <small>. Fall back to the
    #    geo.placename meta when the title carries no location. ──
    if not item.location:
        holder = soup.select_one(".postingtitletext")
        if holder:
            for sp in holder.find_all(["span", "small"]):
                t = sp.get_text(" ", strip=True)
                if len(t) >= 3 and t.startswith("(") and t.endswith(")"):
                    item.location = t.strip("()").strip()
                    break
        if not item.location:
            meta = soup.select_one('meta[name="geo.placename"]')
            if meta and meta.get("content"):
                item.location = str(meta["content"]).strip()

    # ── Posted / updated timestamps. ──
    times = soup.select(".postinginfos time[datetime], time.timeago[datetime]")
    if times:
        item.posted_at = _normalize_iso(times[0].get("datetime", ""))
        if len(times) > 1:
            item.updated_at = _normalize_iso(times[-1].get("datetime", ""))

    # ── Numeric "post id:" (the detail page still prints it even though the
    #    current listing URL no longer carries it). Stored as an extra field;
    #    the dedup key stays item.post_id. ──
    if not item.numeric_post_id:
        m = _DETAIL_POST_ID_RE.search(html)
        if m:
            item.numeric_post_id = m.group(1)

    item.description_enriched = True
