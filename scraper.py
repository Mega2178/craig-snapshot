"""
Scraper for a public classifieds source. No browser needed — just requests + bs4.

Two phases:
  1. crawl_all(session)         → yields Item objects parsed from the newest
                                   search-results listings (title, price,
                                   location, URL, post id).
  2. fetch_item_detail(session, item) → fetches one listing's own page and
                                   fills in the full description, photo URLs,
                                   and posted/updated timestamps.

The site serves a plain, no-JavaScript fallback list of results inside
<ol class="cl-static-search-results">. We parse THAT (rather than the gallery
markup that loads via JS), because it carries the one thing we need that the
JSON-LD blob on the same page omits: the per-listing URL. Selectors are written
defensively with fallbacks; if the site changes its markup, the spots to adjust
are parse_search_results() and fetch_item_detail().
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urljoin, urlencode, urlparse

import requests
from bs4 import BeautifulSoup, Tag

import config

BASE_URL = f"https://{config.SITE_SUBDOMAIN}.craigslist.org"

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
    post_id: str = ""
    title: str = ""
    price: str = ""            # raw, e.g. "$1,600"
    price_value: float = 0.0   # parsed numeric (0 = free / unparsed)
    image_url: str = ""        # primary photo (filled from the detail page)
    image_urls: list = field(default_factory=list)  # up to MAX_IMAGES_PER_ITEM
    item_url: str = ""
    category: str = ""         # human-ish label derived from the URL section
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

    def get(self, url: str, retries: int = 3) -> str:
        elapsed = time.time() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=30)
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

_POST_ID_RE = re.compile(r"/(\d+)\.html(?:[?#]|$)")
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
# The URL section right after the host: ".../spo/d/slug/123.html" → "spo".
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


def _post_id_from_url(url: str) -> str:
    m = _POST_ID_RE.search(url or "")
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
    """Best-effort human-ish category from the listing URL section code."""
    m = _URL_SECTION_RE.match(url or "")
    if m:
        code = m.group(1).lower()
        return _SECTION_LABELS.get(code, code)
    # Fall back to the search path we found it under.
    tail = (search_path or "").rstrip("/").split("/")[-1]
    return tail or ""


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


def crawl_all(session: Session) -> Iterator[Item]:
    """Yield Items from every configured search path, newest first.

    Dedupes by post id across paths/pages within this run so the same listing
    that appears in two category feeds is only yielded once.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen: set[str] = set()
    printed = 0

    for path in config.SEARCH_PATHS:
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
                new_on_page += 1
                if printed < SAMPLE_ITEM_PRINT_LIMIT:
                    _print_sample_item(it)
                    printed += 1
                yield it

            print(f"    parsed {len(page_items)} rows ({new_on_page} new)")
            if new_on_page == 0:
                break


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

    # ── Posted / updated timestamps. ──
    times = soup.select(".postinginfos time[datetime], time.timeago[datetime]")
    if times:
        item.posted_at = _normalize_iso(times[0].get("datetime", ""))
        if len(times) > 1:
            item.updated_at = _normalize_iso(times[-1].get("datetime", ""))

    item.description_enriched = True
