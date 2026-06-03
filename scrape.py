"""
Main entry point.

Run from your IDE or terminal:
    python scrape.py            # scrape + enrich new items + write outputs
    python scrape.py --scrape   # only scrape (refresh prices), don't call AI
    python scrape.py --enrich   # only enrich items missing AI data
    python scrape.py --no-enrich  # scrape but skip AI step
    python scrape.py --test     # process only the first N items (see config.py)

Output:
    items.csv             - sorted by flip_score, best deals on top
    raw_items.json        - raw scraped data (for debugging / re-enrichment)
    docs/data/items.json  - what the dashboard reads
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from scraper import Item, Session, crawl_all


def _fmt_duration(seconds: float) -> str:
    """Format an elapsed-seconds float as a compact 'Xm YYs' (or 'YYs')."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


SCRIPT_DIR = Path(__file__).parent.resolve()
CSV_PATH = SCRIPT_DIR / "items.csv"
RAW_PATH = SCRIPT_DIR / "raw_items.json"
JSON_PATH = SCRIPT_DIR / "docs" / "data" / "items.json"

# When --test is passed, we redirect persistence to separate files so test
# runs can't contaminate the production dataset.
CSV_PATH_TEST = SCRIPT_DIR / "items_test.csv"
RAW_PATH_TEST = SCRIPT_DIR / "raw_items_test.json"
JSON_PATH_TEST = SCRIPT_DIR / "docs" / "data" / "items_test.json"


def _set_test_paths() -> None:
    """Swap the module-level paths to their test-mode variants."""
    global CSV_PATH, RAW_PATH, JSON_PATH
    CSV_PATH = CSV_PATH_TEST
    RAW_PATH = RAW_PATH_TEST
    JSON_PATH = JSON_PATH_TEST


CSV_FIELDS = [
    "flip_score",            # ROI: most important — sorted by this
    "gross_profit",          # absolute $ profit
    "price",                 # seller's asking price (raw, e.g. "$1,600")
    "ai_estimated_resale",
    "ai_retail_estimate",
    "ai_resale_pct",
    "ai_confidence",
    "ai_sales_velocity",     # hot / normal / slow / very_slow / unknown
    "ai_condition",          # new / open_box / damaged_easy_fix / damaged_hard_fix
    "value_overridden",      # "yes" if we forced resale to $0 (damaged_hard_fix)
    "title",
    "location",              # neighborhood / city the seller entered
    "ai_notes",
    "category",
    "description",
    "posted_at",
    "updated_at",
    "first_seen_at",
    "image_url",
    "item_url",
    "post_id",
    "price_value",
    "scraped_at",
    "enriched_at",
]


# ────────────────────────────── persistence ─────────────────────────────────

def load_existing() -> dict[str, Item]:
    """Load previously-saved items keyed by post_id."""
    if not RAW_PATH.exists():
        return {}
    try:
        with RAW_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        items = {}
        for d in data:
            it = Item(**{k: v for k, v in d.items() if k in Item.__dataclass_fields__})
            if it.key():
                items[it.key()] = it
        return items
    except Exception as e:
        print(f"warning: could not load existing data ({e}); starting fresh")
        return {}


def save_raw(items: dict[str, Item]) -> None:
    with RAW_PATH.open("w", encoding="utf-8") as f:
        if config.PRETTY_PRINT_JSON:
            json.dump([asdict(it) for it in items.values()], f, indent=2)
        else:
            json.dump([asdict(it) for it in items.values()], f,
                      separators=(",", ":"))


def _sort_key(it: Item):
    """Items with a numeric flip_score sort first (descending); unknowns last."""
    try:
        score = float(it.flip_score)
        return (0, -score)
    except (ValueError, TypeError):
        return (1, 0)


def write_csv(items: dict[str, Item]) -> None:
    """Sort by flip_score desc (unknowns at bottom), write CSV."""
    rows = sorted(items.values(), key=_sort_key)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for it in rows:
            writer.writerow(asdict(it))


def write_json(items: dict[str, Item]) -> None:
    """Write items to JSON for the dashboard to consume.

    Format:
        { "generated_at": "<ISO UTC>", "items": [ ...CSV fields... ] }

    Timestamps stay in raw ISO so the frontend can format them locale-aware.
    """
    rows = sorted(items.values(), key=_sort_key)
    payload_items = []
    for it in rows:
        row = asdict(it)
        payload_items.append({k: row.get(k, "") for k in CSV_FIELDS})

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": payload_items,
    }

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSON_PATH.open("w", encoding="utf-8") as f:
        if config.PRETTY_PRINT_JSON:
            json.dump(payload, f, indent=2)
        else:
            json.dump(payload, f, separators=(",", ":"))


# ────────────────────────────── pipeline steps ──────────────────────────────

def do_scrape(existing: dict[str, Item], limit: int | None = None) -> dict[str, Item]:
    """Scrape current listings and merge into the existing dict.

    Prices/locations on previously-seen listings are refreshed; AI fields and
    first_seen_at are preserved. Newly-discovered listings get a per-item
    detail-page fetch (when config.SCRAPE_ITEM_DETAIL_PAGES is True) to pull the
    full description + photos that feed the AI.
    """
    import time as _time
    from scraper import fetch_item_detail  # lazy import to avoid circular ref

    print("\n=== SCRAPE ===")
    if limit is not None:
        print(f"  (test mode: capped at {limit} items)")
    session = Session(delay=config.SCRAPE_DELAY_SECONDS)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_count = 0
    refresh_count = 0
    processed = 0
    detail_fetched = 0
    detail_skipped_budget = 0
    detail_budget_start = _time.time()
    detail_budget_exceeded = False

    def _detail_ok() -> bool:
        nonlocal detail_budget_exceeded
        if detail_budget_exceeded:
            return False
        elapsed = _time.time() - detail_budget_start
        if elapsed > config.SCRAPE_DETAIL_PAGE_TIME_BUDGET_SECONDS:
            detail_budget_exceeded = True
            print(f"  ! detail-page time budget "
                  f"({config.SCRAPE_DETAIL_PAGE_TIME_BUDGET_SECONDS}s) exceeded; "
                  f"remaining items will enrich on title only")
            return False
        return True

    for fresh in crawl_all(session):
        key = fresh.key()
        if not key:
            continue

        if key in existing:
            old = existing[key]
            # refresh dynamic fields (sellers sometimes drop the price)
            if fresh.price:
                old.price = fresh.price
                old.price_value = fresh.price_value
            if fresh.location and not old.location:
                old.location = fresh.location
            old.scraped_at = now_iso
            # Optionally re-fetch the detail page for already-seen items, or
            # back-fill it for items cached before detail-fetching was on.
            need_detail = (config.SCRAPE_ITEM_DETAIL_PAGES
                           and (not old.description_enriched
                                or config.RECHECK_EXISTING_DETAILS))
            if need_detail:
                if _detail_ok():
                    fetch_item_detail(session, old)
                    detail_fetched += 1
                else:
                    detail_skipped_budget += 1
            refresh_count += 1
        else:
            fresh.first_seen_at = now_iso
            fresh.scraped_at = now_iso
            existing[key] = fresh
            new_count += 1
            if config.SCRAPE_ITEM_DETAIL_PAGES:
                if _detail_ok():
                    fetch_item_detail(session, fresh)
                    detail_fetched += 1
                else:
                    detail_skipped_budget += 1

        processed += 1
        if processed % 100 == 0:
            save_raw(existing)
            print(f"  …checkpoint at {processed} items processed")
        if limit is not None and processed >= limit:
            print(f"  reached test-mode cap ({limit}); stopping crawl early")
            break

    print(f"\n→ {new_count} new items, {refresh_count} refreshed, "
          f"{len(existing)} total in dataset")
    if config.SCRAPE_ITEM_DETAIL_PAGES:
        print(f"  detail pages fetched: {detail_fetched}"
              + (f", skipped (budget): {detail_skipped_budget}"
                 if detail_skipped_budget else ""))
    return existing


def purge_stale_items(items: dict[str, Item]) -> int:
    """Drop listings first seen more than RETENTION_DAYS ago.

    Classifieds listings expire on their own after ~30-45 days; this keeps the
    dataset (and the committed JSON) from growing without bound. Items with no
    parseable first_seen_at are kept (we can't judge their age).

    Returns the number of items removed.
    """
    keep_days = config.RETENTION_DAYS
    if keep_days < 0:
        return 0  # negative = no purging
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)

    to_drop = []
    for key, it in items.items():
        if not it.first_seen_at:
            continue
        try:
            seen = datetime.fromisoformat(it.first_seen_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if seen < cutoff:
            to_drop.append(key)

    for key in to_drop:
        del items[key]
    return len(to_drop)


def do_enrich(items: dict[str, Item], limit: int | None = None) -> dict[str, Item]:
    """Call Gemini in batches for items that don't yet have an AI estimate.

    When config.GEMINI_API_KEY_2 is set (and from a different Google Cloud
    project), this dispatches batches concurrently across both keys. When
    config.SEND_IMAGES_TO_AI is on, each item's first photo rides along in the
    same request (matched to its item via per-image id captions).
    """
    from enricher import Enricher, chunked, QuotaExhausted  # lazy import
    import threading as _threading

    pending: list[Item] = [
        it for it in items.values() if not it.ai_confidence
    ]
    print(f"\n=== ENRICH ===")
    if limit is not None and len(pending) > limit:
        print(f"  (test mode: capping enrichment at {limit} of {len(pending)} pending)")
        pending = pending[:limit]
    print(f"{len(pending)} items need AI enrichment")
    if not pending:
        return items

    batch_size = config.BATCH_SIZE

    enricher = Enricher()
    batches = list(chunked(pending, batch_size))
    print(f"sending {len(batches)} batches of up to {batch_size} items "
          f"to {config.GEMINI_MODEL}"
          + (" (with photos)" if config.SEND_IMAGES_TO_AI else ""))
    if enricher.worker_count > 1:
        print(f"(concurrent: {enricher.worker_count} keys × "
              f"{config.GEMINI_DELAY_SECONDS}s per-key throttle)\n")
    else:
        print(f"(pacing: {config.GEMINI_DELAY_SECONDS}s between calls)\n")

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    quota_hit = False

    # Build batch payloads upfront so the concurrent dispatcher gets plain
    # dicts. We keep the batches list of Items in parallel for result mapping.
    payloads: list[list[dict]] = []
    for batch in batches:
        payloads.append([
            {
                "item_id": it.key(),
                "title": it.title,
                "price": it.price,
                "category": it.category,
                "description": it.description,
                "image_url": it.image_url,
                "image_urls": it.image_urls,
            }
            for it in batch
        ])

    save_lock = _threading.Lock()

    def _apply_valuations(batch_items: list[Item], valuations: list) -> None:
        """Write Gemini results back onto the Item objects, then save."""
        if not valuations:
            return
        by_id = {v.item_id: v for v in valuations}
        for it in batch_items:
            v = by_id.get(it.key())
            if not v:
                continue
            it.ai_retail_estimate = f"{v.current_retail_usd:.2f}"
            it.ai_resale_pct = f"{v.resale_pct:.2f}"
            estimated_resale = v.current_retail_usd * v.resale_pct
            it.ai_confidence = v.confidence
            it.ai_condition = v.condition
            it.ai_sales_velocity = v.sales_velocity

            # Force resale to $0 ONLY when the model says "damaged_hard_fix"
            # (broken/unsellable). Easy-fix items get a small haircut at
            # scoring time instead.
            if v.condition == "damaged_hard_fix":
                estimated_resale = 0.0
                it.value_overridden = "yes"
            else:
                it.value_overridden = ""

            it.ai_estimated_resale = f"{estimated_resale:.2f}"
            it.ai_notes = f"[{v.product_identified}] {v.notes}".strip()
            it.enriched_at = now_iso
            it.flip_score = compute_flip_score(it)
            it.gross_profit = compute_gross_profit(it)

    completed = 0
    try:
        for idx, valuations in enricher.enrich_batches_concurrent(payloads):
            completed += 1
            batch_items = batches[idx]
            if not valuations:
                print(f"  batch {idx + 1}/{len(batches)}: no valuations "
                      f"returned (skipped)")
            else:
                _apply_valuations(batch_items, valuations)
                print(f"  ✓ batch {idx + 1}/{len(batches)} done, "
                      f"{len(valuations)} valuations "
                      f"[{completed}/{len(batches)} complete]")
            with save_lock:
                save_raw(items)
    except QuotaExhausted as e:
        print(f"\n⛔ {e}")
        quota_hit = True

    if quota_hit:
        remaining = sum(1 for it in items.values() if not it.ai_confidence)
        print(f"\n{remaining} items still need enrichment.")
        print(f"Re-run after midnight Pacific (or with --enrich) to continue.")

    return items


# ────────────────────────────── scoring ─────────────────────────────────────

def _purchase_price(it: Item) -> float | None:
    """Realistic out-of-pocket cost to acquire this item.

    asking_price * NEGOTIATION_FACTOR (no buyer's premium / tax on a private
    sale). Free items (price 0) cost ~0 and are allowed through with a $0 cost.
    Returns None only when we can't determine a price at all (e.g. the listing
    hasn't been detail-fetched and had no price on the results card yet).
    """
    val = it.price_value
    if not val:
        # Try to parse from the raw price string as a fallback.
        raw = (it.price or "").replace("$", "").replace(",", "").strip()
        try:
            val = float(raw)
        except ValueError:
            return None
    if val < 0:
        return None
    return val * config.NEGOTIATION_FACTOR


def _condition_resale_factor(it: Item) -> float:
    """Multiplier applied to estimated_resale based on AI-assessed condition.

      • new / open_box   → 1.00  (no haircut — open_box is the default)
      • damaged_easy_fix → 0.85  (small handyman-fix haircut)
      • damaged_hard_fix → 0.00  (already zeroed at enrichment; belt-and-braces)
      • anything else    → 1.00  (never penalize for missing data)
    """
    cond = (it.ai_condition or "").strip().lower()
    if cond == "damaged_hard_fix":
        return 0.0
    if cond == "damaged_easy_fix":
        return 0.85
    return 1.0


def compute_flip_score(it: Item) -> str:
    """flip_score (ROI) =
        (effective_resale - purchase_price - hassle) / purchase_price

    purchase_price = asking_price * NEGOTIATION_FACTOR.
    effective_resale = estimated_resale * condition_resale_factor.

    Returns a string. Empty if unknown / can't compute.
    """
    try:
        if it.ai_confidence in ("", "unknown"):
            return ""
        estimated_resale = float(it.ai_estimated_resale or 0)
        if estimated_resale <= 0:
            return ""
        purchase_price = _purchase_price(it)
        if purchase_price is None:
            return ""
        effective_resale = estimated_resale * _condition_resale_factor(it)
        cost_floor = max(purchase_price, 1.0)
        score = (effective_resale - purchase_price - config.PICKUP_HASSLE_DOLLARS) / cost_floor
        return f"{score:.2f}"
    except (ValueError, TypeError):
        return ""


def compute_gross_profit(it: Item) -> str:
    """Absolute dollar profit: effective_resale - purchase_price - hassle.

    Same numerator as flip_score; differs only in normalization.
    Returns a string. Empty if unknown / can't compute.
    """
    try:
        if it.ai_confidence in ("", "unknown"):
            return ""
        estimated_resale = float(it.ai_estimated_resale or 0)
        if estimated_resale <= 0:
            return ""
        purchase_price = _purchase_price(it)
        if purchase_price is None:
            return ""
        effective_resale = estimated_resale * _condition_resale_factor(it)
        profit = effective_resale - purchase_price - config.PICKUP_HASSLE_DOLLARS
        return f"{profit:.2f}"
    except (ValueError, TypeError):
        return ""


def recompute_all_flip_scores(items: dict[str, Item]) -> None:
    """Recompute flip_score AND gross_profit for every enriched item.

    Prices can change between runs (sellers drop them), so re-score on each run.
    """
    for it in items.values():
        if not it.ai_confidence or it.ai_confidence == "unknown":
            continue
        it.flip_score = compute_flip_score(it)
        it.gross_profit = compute_gross_profit(it)


# ────────────────────────────── main ────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scrape", action="store_true",
                        help="only scrape, don't call AI")
    parser.add_argument("--enrich", action="store_true",
                        help="only enrich items already in the dataset")
    parser.add_argument("--no-enrich", action="store_true",
                        help="scrape but skip AI step")
    parser.add_argument("--test", action="store_true",
                        help=f"test mode: cap work at {config.TEST_MODE_ITEM_LIMIT} "
                             f"items, write to items_test.csv / raw_items_test.json")
    args = parser.parse_args()

    only_scrape = args.scrape or args.no_enrich
    only_enrich = args.enrich
    test_mode = args.test
    limit = config.TEST_MODE_ITEM_LIMIT if test_mode else None

    if test_mode:
        _set_test_paths()
        print("=" * 60)
        print(f"  TEST MODE — capped at {limit} items")
        print(f"  reading/writing {RAW_PATH.name} + {CSV_PATH.name}")
        print(f"  (production data is untouched)")
        print("=" * 60)

    items = load_existing()
    print(f"loaded {len(items)} existing items from {RAW_PATH.name}")

    # Purge stale listings BEFORE scraping so the working set stays small and
    # the committed JSON stays under GitHub's 100 MB cap. Test mode skips it.
    if not test_mode:
        removed = purge_stale_items(items)
        if removed:
            print(f"purged {removed} stale items "
                  f"(>{config.RETENTION_DAYS}d since first seen); "
                  f"{len(items)} remain")

    scrape_secs = None
    enrich_secs = None

    if not only_enrich:
        _t0 = time.time()
        items = do_scrape(items, limit=limit)
        save_raw(items)
        scrape_secs = time.time() - _t0
        print(f"  scrape phase took {_fmt_duration(scrape_secs)}")

    if not only_scrape:
        _t0 = time.time()
        items = do_enrich(items, limit=limit)
        save_raw(items)
        enrich_secs = time.time() - _t0
        print(f"  enrich phase took {_fmt_duration(enrich_secs)}")

    # always recompute flip scores at end (prices may have refreshed)
    recompute_all_flip_scores(items)
    save_raw(items)
    write_csv(items)
    write_json(items)

    # summary
    enriched = sum(1 for it in items.values() if it.ai_confidence)
    high_conf = sum(1 for it in items.values() if it.ai_confidence == "high")
    print(f"\n=== DONE ===")
    if test_mode:
        print(f"** TEST MODE — results are not your production CSV **")
    print(f"total items:    {len(items)}")
    print(f"enriched:       {enriched}")
    print(f"high-conf:      {high_conf}")
    if scrape_secs is not None:
        print(f"scrape phase:   {_fmt_duration(scrape_secs)}")
    if enrich_secs is not None:
        print(f"enrich phase:   {_fmt_duration(enrich_secs)}")
    print(f"\nwrote {CSV_PATH}")
    print(f"wrote {JSON_PATH}")
    print(f"top rows are the best flips")


if __name__ == "__main__":
    main()
