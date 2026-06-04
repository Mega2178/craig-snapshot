"""
AI enrichment via Gemini Flash-Lite.

Strategy:
- Batch items per API call and use structured output (response_schema) so we
  get reliable JSON back.
- For each item the model estimates current retail value, resale %, sales
  velocity, condition, and confidence. Items with confidence=unknown sink to
  the bottom of the CSV.
- Classifieds descriptions are sparse, so (when SEND_IMAGES_TO_AI is on) we
  attach the listing's first photo to the SAME batched request, interleaved
  right after that item's text. A text+image request still counts as ONE
  request against the RPM/RPD quota — photos only add tokens.

Error handling:
- 429 (quota): parse the server's retryDelay; if short, sleep and retry; if
  long (> GEMINI_GIVEUP_AFTER_SECONDS), raise QuotaExhausted so the
  orchestrator stops cleanly. Already-enriched items are already saved.
- 503 (overloaded): exponential backoff retry.
- media_resolution rejected by the model/API version: disable that hint for
  the worker and retry the same call without it (photos are still sent).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Iterable

import requests
from pydantic import BaseModel, Field

import config


class QuotaExhausted(Exception):
    """Raised when we've clearly hit the daily quota wall and should stop."""


# google-genai is the current official SDK (replaces deprecated google-generativeai)
try:
    from google import genai
    from google.genai import types as genai_types
    from google.genai import errors as genai_errors
except ImportError as e:
    raise SystemExit(
        "Missing dependency: install with `pip install google-genai`\n"
        f"(import error: {e})"
    )


# Resolution-hint enum lookup, built defensively: older SDKs may not expose
# MediaResolution at all, in which case we just never send the hint.
try:
    _MEDIA_RES_MAP = {
        "low": genai_types.MediaResolution.MEDIA_RESOLUTION_LOW,
        "medium": genai_types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
        "high": genai_types.MediaResolution.MEDIA_RESOLUTION_HIGH,
    }
except Exception:  # pragma: no cover - depends on installed SDK version
    _MEDIA_RES_MAP = {}

# Headers for image downloads. A realistic UA keeps image CDNs from 403'ing.
_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
# Skip any single image larger than this (keeps one oversized photo from
# dominating a request). Real listing photos are well under this.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
# Total image-bytes budget per request. Inline image data must keep the whole
# request under the API's ~20 MB ceiling, so once a batch's photos reach this
# we stop attaching more and send the remaining items text-only. 25 typical
# listing photos total only a few MB, so this is a safety net, not a usual cap.
_MAX_REQUEST_IMAGE_BYTES = 16 * 1024 * 1024


# ────────────────────────────── schema ──────────────────────────────────────

class ItemValuation(BaseModel):
    """One row of Gemini's batch response.

    Field order matters: the model generates fields in schema order, so it
    identifies the product, classifies the listing, and grounds the price
    BEFORE it estimates value — and writes notes last so they can reconcile
    with everything above.
    """
    item_id: str = Field(description="The item_id we sent in the request, echo it back exactly")
    product_identified: str = Field(description="Brief identification of the specific item you are valuing, e.g. 'Sony WH-CH520 wireless headphones'. For a multi-item listing, this is the ONE item you chose to value.")
    listing_kind: str = Field(description="One of: single_item, multi_item, not_for_sale. single_item = one item for sale. multi_item = one ad covering several different items / a lot / a bundle / a liquidation or 'everything must go' sale (value only the SINGLE most valuable item). not_for_sale = a wanted/ISO ad, a GARAGE/ESTATE/MOVING-SALE announcement, found/lost property, a service, a labor/haul-away request, housing, or anything that is not one specific item being sold. See system instructions.")
    price_status: str = Field(description="One of: priced, free, unknown. 'priced' = you found a real cash price (in the price field OR anywhere in the description) for the item you valued. 'free' = the listing EXPLICITLY gives it away (says 'free', 'curb alert', 'free to good home'). 'unknown' = no determinable price (pure 'make offer' with no number, or a placeholder with nothing in the text). A $0 or missing price field does NOT mean free — read the description. See system instructions.")
    effective_price_usd: float = Field(description="The real cash price to BUY the one item you valued, in USD, when price_status is 'priced'. READ THE DESCRIPTION: sellers often leave the price field at $0 or a placeholder ($1, $5, 1234) and put the real price in the text ('I'm wanting 250 for it', '$150 obo', '$500 cash', 'Queen $90 and up'). Extract that number. For a per-item or 'and up' price, use the single item's price. Set 0 when price_status is 'free' or 'unknown'. Never echo a placeholder as if it were the real price.")
    current_retail_usd: float = Field(description="Realistic CURRENT retail price NEW in USD (Amazon/Walmart 2026) of the item you valued. 0 if unknown.")
    resale_pct: float = Field(description="Estimated resale value of the item you valued as a fraction of its retail in the local secondhand market (Facebook Marketplace / OfferUp). E.g. 0.55 means it typically sells used for 55% of retail. Use 0 if unknown.")
    sales_velocity: str = Field(description="Estimated speed of selling the item you valued on Facebook Marketplace in the local metro. One of: hot, normal, slow, very_slow, unknown. See system instructions for criteria.")
    confidence: str = Field(description="One of: high, medium, low, unknown")
    condition: str = Field(description="One of: new, open_box, damaged_easy_fix, damaged_hard_fix. DEFAULT to 'open_box' unless damage is explicitly stated or clearly visible in the photo — see system instructions for the full rules.")
    notes: str = Field(description="Brief caveat or reasoning (1-2 sentences max). For a multi_item listing, say which item you valued. Mention any damage / missing parts that drove the condition assessment.")


class BatchResponse(BaseModel):
    valuations: list[ItemValuation]


# ────────────────────────────── prompt ──────────────────────────────────────

SYSTEM_PROMPT = """You are an expert resale-value estimator for a person who
buys items from a local classifieds marketplace (private-party listings) and
re-sells them on Facebook Marketplace in the same metro.

FIRST, before valuing anything, classify the listing and find the REAL price.
Classifieds listings are messy: the structured asking price is frequently
missing, $0, or a placeholder, with the true price written in the description.
Bundles, liquidations, "make offer" ads, and ads that aren't selling one item
are all common. Getting this right matters MORE than the valuation, because a
wrong price produces a garbage deal score.

A. LISTING_KIND — pick exactly one:
   • "single_item"  = one specific item (or a matched set sold as one unit, like
                      a set of 4 hubcaps) for sale.
   • "multi_item"   = ONE ad covering several DIFFERENT items, a lot, a bundle,
                      OR a liquidation / "everything must go" / "$X and up"
                      sale — e.g. "Dressers, tables, couches, chairs all for
                      sale", a list of separately-priced tools, or "Queen
                      mattresses $90 and up, King $125 and up". For these,
                      choose the SINGLE most valuable item you can clearly
                      identify and value ONLY that one item. Every field below
                      (product_identified, retail, resale, velocity, condition)
                      must describe that one item — NOT the whole pile.
   • "not_for_sale" = NOT one item being sold. Includes: wanted/ISO ("looking
                      for", "need", "ISO"), GARAGE / ESTATE / MOVING / YARD SALE
                      announcements (a title like "Garage sale" is ALWAYS
                      not_for_sale), found/lost property, labor or haul-away
                      requests, services, jobs, housing/rooms, personal ads.
                      Set price_status = "unknown", current_retail_usd = 0,
                      confidence = "unknown", and say so in notes.

B. PRICE_STATUS — pick exactly one, AFTER reading the whole description:
   • "priced"  = you found a real cash price for the item you valued, EITHER in
                 the price field OR anywhere in the description. Put it in
                 effective_price_usd.
   • "free"    = the listing EXPLICITLY gives the item away — it says "free",
                 "curb alert", "free to good home", "$0 just come get it", etc.
                 Set effective_price_usd = 0. (A genuinely free item is a great
                 flip, so this is allowed to score.)
   • "unknown" = there is genuinely no determinable price anywhere: a pure "make
                 offer" with no number, or a placeholder ($1/$5/1234) with no
                 real price in the text. Set effective_price_usd = 0. Do NOT
                 guess a number.

   CRITICAL: A $0 or missing PRICE FIELD does NOT mean the item is free. It
   almost always means the seller put the price in the description, or wants an
   offer. Read the description and HUNT for the price. Only choose "free" when
   the listing actually says it's being given away.

C. EFFECTIVE_PRICE_USD — when price_status is "priced", the real cash price to
   BUY the one item you valued. Extract it from wherever it appears:
     - price written in prose: "I'm wanting 250 for it" → 250
     - "$150 or best offer" → 150
     - "SELLING ALL COMPLETE $500.00 CASH OBO" → 500
     - a per-item / "and up" price for the item you chose: "Queen $90 and up" →
       90 (for a queen);  "doors $50 each" → 50
     - a normal price field with no contradiction in the text → use it
   Set 0 when price_status is "free" or "unknown". NEVER echo a $1 / $5 / 1234
   placeholder as the price, and never invent a number.

THEN value the item. Fill in ALL of these fields for the ONE item you valued:

1. CURRENT_RETAIL_USD — what this product sells for NEW today on Amazon,
   Walmart, Target, or the manufacturer's site. Estimate the product's real
   retail value from your own knowledge. The seller's asking price is NOT the
   retail value — do not echo it. (A seller asking $40 for a dresser tells you
   little about what the dresser is worth new.)

2. RESALE_PCT — what fraction of that retail price a used copy of this category
   typically fetches on Facebook Marketplace in a midwest metro.
   Rough guidance (use your judgment, not these exactly):
     • Major-brand consumer electronics (Nintendo, Sony, Apple, Bose): 0.50–0.70
     • Small kitchen appliances, name-brand: 0.40–0.55
     • Generic/no-name items: 0.20–0.35
     • Power tools, name-brand (DeWalt, Milwaukee): 0.55–0.75
     • Generic clothing/beauty: 0.15–0.30
     • Furniture: 0.25–0.45
     • Specialty/hobby (musical instruments, exercise equipment): 0.30–0.50

3. SALES_VELOCITY — how quickly this item is likely to sell on Facebook
   Marketplace in the local metro at a fair price. This is NOT a precise
   prediction, just a rank. Pick exactly one:
     • "hot"       = high demand, name recognition, broadly useful. Sells in
                     under a week. Examples: name-brand power tools (DeWalt,
                     Milwaukee, Ryobi), gaming consoles, Apple/Sony
                     electronics, baby gear, popular sneakers, generators,
                     snow blowers in season.
     • "normal"    = steady demand. Sells in 1-3 weeks. Examples: most
                     name-brand kitchen appliances, mid-tier electronics,
                     bicycles, sporting goods, common furniture.
     • "slow"      = niche, specialty, or commodity. Sells in 1-2 months.
                     Examples: decor items, lamps, generic small appliances,
                     office furniture, exercise equipment (large/heavy),
                     unusual collectibles, kids' toys (non-trending).
     • "very_slow" = generic, unbranded, or oversupplied. Often sits 2+ months
                     or never sells. Examples: generic unbranded junk,
                     used-clothing single items, dated fashion, novelty items,
                     niche hobby gear without a clear buyer.
     • "unknown"   = you genuinely cannot tell what category this is.

   Adjust DOWN one tier if condition is "damaged_easy_fix". Adjust DOWN two
   tiers if condition is "damaged_hard_fix". Seasonal items (Christmas decor
   in May, AC units in November) should be one tier slower.

4. CONFIDENCE — be honest:
     • "high"   = you know exactly what this product is and its real price
     • "medium" = you can identify the category and estimate within ±25%
     • "low"    = you have a rough guess but real value could be 2x off
     • "unknown" = you genuinely cannot identify the product or value it
   Use "unknown" liberally rather than fabricating. We'd rather have a missing
   row than a wrong row.

5. CONDITION — what is the physical state of THIS specific item, judged from
   the title + description AND the photo when one is provided. Pick exactly one:

     • "new"               = explicitly described as new, sealed, unopened,
                             "in original packaging", "brand new", or clearly
                             unused/sealed in the photo.

     • "open_box"          = THE DEFAULT. Use this whenever the listing does
                             NOT explicitly describe damage and the photo does
                             not clearly show damage. This includes:
                               - "used", "good condition", "gently used",
                                 "works great", "lightly used"
                               - listings that say little about condition
                               - normal used wear visible in the photo
                             Private sellers list working used goods; assume
                             it works unless told or shown otherwise.

     • "damaged_easy_fix"  = the listing EXPLICITLY mentions, or the photo
                             clearly shows, cosmetic damage, a missing standard
                             part, or a simple problem fixable for under ~$30
                             in cheap aftermarket / used / hardware-store parts.
                             The buyer has a competent handyman who can do basic
                             mechanical, cosmetic, and non-risky electrical work.
                             Examples that BELONG here:
                               - missing standard cable (USB-C, HDMI, AC)
                               - missing universal power adapter / generic remote
                               - missing screws, knobs, hardware-store parts
                               - cracked glass, dented panel (cheap replacement)
                               - worn cord on a lamp or appliance
                               - light cosmetic wear, scratches, dings
                                 (still functional)

     • "damaged_hard_fix"  = the listing EXPLICITLY indicates, or the photo
                             clearly shows, the item is broken, non-functional,
                             or unsellable AND the fix requires specialist work
                             or expensive proprietary parts. Examples:
                               - missing PROPRIETARY battery (e-bike, drone,
                                 modern power tool) with no cheap option
                               - cracked LCD/OLED on phones, tablets, laptops
                               - internal electronics failure on a sealed unit
                               - engine, transmission, or drivetrain failure
                               - refrigerant / sealed-system appliance work
                               - soldering or board-level repair required
                             ALSO use this for items fundamentally unsellable
                             regardless of repair: used hygiene/personal-care
                             items, expired food, prescription items,
                             custom-engraved items worthless to others.

   CRITICAL DEFAULTING RULE: When in doubt, choose "open_box". Do NOT guess
   that an item is damaged. Only downgrade from "open_box" when there is
   explicit damage/missing-part/non-functional language OR clearly visible
   damage in the photo. If your confidence on the condition specifically is
   low, default to "open_box".

6. NOTES — 1–2 short sentences: identify what you think the item is, mention
   any damage / missing parts that drove the condition assessment, or why
   you're uncertain.

IMAGE–ITEM MATCHING (read carefully):
Items are presented in order. Each item begins with a header line
"----- ITEM n of N -----", then "item_id: <id>", then that item's fields.
When an item has a photo, the LAST line of its text block reads
"PHOTO FOR item_id <id> (item n of N) ..." and the very next image you receive
is THAT item's photo — it belongs to that item_id and to no other item. Use the
photo as your primary signal for identifying the product and judging condition.
Follow these rules exactly:
  • Attribute each image to the item_id named on the "PHOTO FOR item_id ..."
    line immediately before it. NEVER describe one item using another item's
    photo, and never shift photos by one position.
  • Items whose block ends with "(no photo provided for this item)" have NO
    image — judge them from text alone; do not borrow a neighbor's photo.
  • Return EXACTLY one valuation per item_id, echoing the item_id exactly as
    given. Do not invent item_ids, skip items, or merge two items into one row.

CONSISTENCY CHECK: The notes must agree with the structured fields. Do not
write "this is broken" in notes while marking it open_box, and do not write
"appears new" while marking it damaged_hard_fix. The structured fields are
what get used by downstream code."""


# ────────────────────────────── enricher ────────────────────────────────────

class _Worker:
    """One API key + its own client + its own throttle clock.

    Each key has independent per-minute and per-day quotas (when the keys
    belong to different Google Cloud projects), so each worker tracks its own
    last_call time and own exhausted flag. Multiple workers can run
    concurrently; they share nothing but the model name.
    """
    def __init__(self, api_key: str, name: str):
        self.api_key = api_key
        self.name = name
        self.client = genai.Client(api_key=api_key)
        self.model = config.GEMINI_MODEL
        self.last_call = 0.0
        self._lock = threading.Lock()  # serializes the per-key throttle clock
        self.exhausted = False
        # Flip to False the first time this model/API version rejects the
        # media_resolution hint, so we stop sending it (photos still go).
        self._media_res_ok = bool(_MEDIA_RES_MAP)
        # Per-worker session for image downloads (used off the throttle lock).
        self._img_session = requests.Session()
        self._img_session.headers.update(_IMG_HEADERS)

    def _throttle(self):
        # Called inside the lock — at most one in-flight call per key.
        elapsed = time.time() - self.last_call
        if elapsed < config.GEMINI_DELAY_SECONDS:
            time.sleep(config.GEMINI_DELAY_SECONDS - elapsed)

    def _fetch_image_part(self, url: str):
        """Download an image. Returns (Part, num_bytes), or None on failure."""
        if not url:
            return None
        try:
            r = self._img_session.get(url, timeout=20)
            if r.status_code != 200 or not r.content:
                return None
            if len(r.content) > _MAX_IMAGE_BYTES:
                return None
            ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                ctype = _guess_mime(url)
            return (genai_types.Part.from_bytes(data=r.content, mime_type=ctype),
                    len(r.content))
        except Exception:
            return None

    def _gen_config(self, with_media_res: bool):
        kwargs = dict(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=BatchResponse,
            temperature=0.2,
        )
        if with_media_res:
            mr = _MEDIA_RES_MAP.get((config.IMAGE_MEDIA_RESOLUTION or "").lower())
            if mr is not None:
                kwargs["media_resolution"] = mr
        return genai_types.GenerateContentConfig(**kwargs)

    def _generate(self, contents):
        """One generate_content call with defensive media_resolution fallback.

        If the model/API version rejects the media_resolution hint (a 400 about
        'mediaResolution', or a TypeError because the field isn't supported),
        we disable the hint for this worker and retry once WITHOUT it. The
        photos themselves are unaffected — only the resolution hint is dropped.
        """
        use_media_res = self._media_res_ok and config.SEND_IMAGES_TO_AI
        try:
            return self.client.models.generate_content(
                model=self.model, contents=contents,
                config=self._gen_config(with_media_res=use_media_res),
            )
        except Exception as e:
            if use_media_res and _is_media_resolution_error(str(e)):
                self._media_res_ok = False
                print(f"  [{self.name}] media_resolution hint not accepted "
                      f"here; retrying without it (photos still sent)")
                return self.client.models.generate_content(
                    model=self.model, contents=contents,
                    config=self._gen_config(with_media_res=False),
                )
            raise

    def enrich_batch(self, batch: list[dict]) -> list[ItemValuation]:
        """Send one batch through this key. Honors per-key rate limit.

        Returns [] on transient failure, raises QuotaExhausted on daily wall.
        """
        if not batch:
            return []
        if self.exhausted:
            raise QuotaExhausted(f"{self.name} already marked exhausted")

        # Build contents OUTSIDE the throttle lock — when images are on this
        # downloads each item's photo, and we don't want to hold the per-key
        # lock (which paces API calls) during image I/O. Built once so retries
        # don't re-download.
        fetch_part = self._fetch_image_part if config.SEND_IMAGES_TO_AI else None
        contents = _build_contents(batch, fetch_part)

        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            # `caught` survives the except block (Python deletes the `except
            # ... as e` target when the block exits, so we can't reference it
            # in the error-classification code below, which runs outside the
            # lock). We stash the exception here to use with `raise ... from`.
            caught: Exception | None = None
            with self._lock:
                self._throttle()
                try:
                    response = self._generate(contents)
                    self.last_call = time.time()
                    valuations = _parse_response(response)
                    _validate_batch_ids(batch, valuations, self.name)
                    return valuations
                except Exception as e:
                    self.last_call = time.time()
                    caught = e
                    err_str = str(e)
                    code = _extract_status_code(err_str)
                    retry_delay = _extract_retry_delay(err_str)

            # ── handle errors OUTSIDE the lock so the other worker
            # ── isn't blocked while we sleep during retry backoff
            if code == 429:
                if retry_delay is None:
                    retry_delay = 30
                if retry_delay > config.GEMINI_GIVEUP_AFTER_SECONDS:
                    # Daily wall on this key. Mark it dead.
                    self.exhausted = True
                    raise QuotaExhausted(
                        f"{self.name}: daily quota wall "
                        f"(retry in {retry_delay}s)"
                    ) from caught
                if attempt < config.GEMINI_MAX_RETRIES:
                    wait = retry_delay + 1
                    print(f"  [{self.name}][429] throttle, waiting {wait}s "
                          f"(attempt {attempt + 1}/{config.GEMINI_MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                print(f"  [{self.name}][429] giving up on batch after "
                      f"{config.GEMINI_MAX_RETRIES} retries")
                return []

            if code == 503:
                if attempt < config.GEMINI_MAX_RETRIES:
                    wait = (attempt + 1) * 5
                    print(f"  [{self.name}][503] overloaded, retry in {wait}s "
                          f"(attempt {attempt + 1}/{config.GEMINI_MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                print(f"  [{self.name}][503] giving up on this batch")
                return []

            # ── Permanent, non-retryable errors ─────────────────────────
            # 401 = bad/expired key, 403 = key valid but the Google Cloud
            # project is suspended / denied access / API not enabled, 400 =
            # malformed key. Retrying or routing more batches to this key is
            # pointless — it will fail identically every time. Mark the worker
            # dead so enrich_batches_concurrent stops dispatching to it and
            # reroutes everything to the remaining live key(s).
            if code in (400, 401, 403) or _is_permanent_auth_error(err_str):
                self.exhausted = True
                raise QuotaExhausted(
                    f"{self.name}: permanent error "
                    f"(HTTP {code if code else '4xx'}) — key/project rejected. "
                    f"Marking this key dead and routing to other key(s). "
                    f"Detail: {err_str[:160]}"
                ) from caught

            # Anything else: log and skip
            print(f"  ! [{self.name}] Gemini call failed: {err_str[:200]}")
            return []

        return []


class Enricher:
    """Coordinator over one or two API key workers.

    With two keys, batches are dispatched round-robin across worker threads;
    each thread blocks on its own per-key throttle clock so the two keys run in
    genuine parallel against Google's API. Throughput scales with the number of
    keys (modulo Google-side overload).

    Quota walls are handled per worker: when one key gets a long-retry 429 (or
    a permanent auth error), that worker is marked exhausted and the remaining
    batches reroute to the other worker(s). When ALL workers are exhausted, the
    iterator stops and the orchestrator persists already-enriched items.
    """
    def __init__(self):
        if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "PASTE_YOUR_KEY_HERE":
            raise SystemExit(
                "GEMINI_API_KEY is not set.\n"
                "Get a free key at https://aistudio.google.com/apikey"
            )
        # Build the worker list. Each worker = one key + one client.
        self._workers: list[_Worker] = [
            _Worker(config.GEMINI_API_KEY, "key1")
        ]
        if (config.GEMINI_API_KEY_2
                and config.GEMINI_API_KEY_2 != config.GEMINI_API_KEY):
            self._workers.append(_Worker(config.GEMINI_API_KEY_2, "key2"))
        self.model = config.GEMINI_MODEL

        if len(self._workers) > 1:
            print(f"  enricher: {len(self._workers)} API keys → concurrent dispatch")

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def _live_workers(self) -> list[_Worker]:
        return [w for w in self._workers if not w.exhausted]

    def enrich_batch(self, batch: list[dict]) -> list[ItemValuation]:
        """Backwards-compatible single-batch sender.

        Tries the first live worker; on QuotaExhausted, rotates. Raises
        QuotaExhausted only when ALL workers are dead. Used by callers that
        don't want concurrency (e.g. the --test path).
        """
        last_err = None
        for w in self._live_workers():
            try:
                return w.enrich_batch(batch)
            except QuotaExhausted as e:
                print(f"  → {w.name} exhausted, trying next worker")
                last_err = e
                continue
        raise QuotaExhausted(
            f"all {len(self._workers)} configured key(s) are exhausted. "
            f"Stopping; already-enriched items are saved. "
            f"Re-run `python scrape.py --enrich` after midnight Pacific."
        ) from last_err

    def enrich_batches_concurrent(self, batches: list[list[dict]]):
        """Dispatch many batches across all live workers concurrently.

        Yields (index, valuations) tuples in COMPLETION order (not input
        order), so the caller must use the index to map results back to items.
        Each worker thread blocks on its own _Worker._lock (the throttle
        clock), so cross-key parallelism is real.

        If a worker raises QuotaExhausted, its in-flight batch reroutes to live
        workers. If ALL workers go exhausted, the iterator stops yielding —
        remaining batches stay unenriched and pick up on the next run.
        """
        if not batches:
            return

        live = self._live_workers()
        if not live:
            raise QuotaExhausted("no live API keys to dispatch with")

        # Single-worker path: just iterate. Avoids thread-pool overhead and
        # makes test runs deterministic.
        if len(live) == 1:
            w = live[0]
            for idx, batch in enumerate(batches):
                try:
                    yield idx, w.enrich_batch(batch)
                except QuotaExhausted:
                    raise
            return

        # Multi-worker concurrent dispatch.
        import concurrent.futures as _f

        pending: dict = {}
        requeue: list = []  # [(idx, batch), ...] batches to re-dispatch
        executor = _f.ThreadPoolExecutor(max_workers=len(self._workers))
        try:
            batch_iter = iter(enumerate(batches))

            def _submit_next() -> bool:
                """Submit the next batch (preferring any requeued batch) to the
                worker with the least in-flight work. Returns False when there's
                nothing left to submit or no workers remain alive."""
                live_now = self._live_workers()
                if not live_now:
                    return False
                # Drain the requeue first so a batch orphaned by a dead key
                # gets retried before we pull brand-new work.
                if requeue:
                    idx, batch = requeue.pop(0)
                else:
                    try:
                        idx, batch = next(batch_iter)
                    except StopIteration:
                        return False
                # Pick the worker with the fewest currently-pending futures.
                load = {w: 0 for w in live_now}
                for (_idx, _batch, w) in pending.values():
                    if w in load:
                        load[w] += 1
                chosen = min(live_now, key=lambda w: load[w])
                fut = executor.submit(chosen.enrich_batch, batch)
                pending[fut] = (idx, batch, chosen)
                return True

            # Prime: submit up to one batch per worker.
            for _ in range(len(live)):
                if not _submit_next():
                    break

            while pending:
                done, _ = _f.wait(
                    pending.keys(), return_when=_f.FIRST_COMPLETED
                )
                for fut in done:
                    idx, batch, worker = pending.pop(fut)
                    try:
                        result = fut.result()
                    except QuotaExhausted:
                        # This worker is dead (quota wall or permanent auth
                        # error). Its batch wasn't processed — push it back so
                        # a live worker retries it. If no workers remain, the
                        # post-loop check below surfaces the stop cleanly.
                        if not getattr(worker, "_death_announced", False):
                            worker._death_announced = True
                            live_names = [w.name for w in self._live_workers()]
                            print(f"  → {worker.name} is dead; rerouting all "
                                  f"remaining batches to: "
                                  f"{', '.join(live_names) or '(none left)'}")
                        if self._live_workers():
                            requeue.append((idx, batch))
                        result = None  # don't yield an empty result for it
                    except Exception as e:
                        print(f"  ! [{worker.name}] worker exception: {e}")
                        result = []
                    if result is not None:
                        yield idx, result
                    # Top up: keep up to len(live) in flight.
                    _submit_next()
        finally:
            executor.shutdown(wait=True)

        # If we exited because all workers exhausted but batches remain,
        # surface that to the caller so it can stop cleanly.
        leftover = bool(requeue) or any(True for _ in batch_iter)
        if self._live_workers() == [] and leftover:
            raise QuotaExhausted(
                f"all {len(self._workers)} configured key(s) exhausted "
                f"during concurrent dispatch. Already-enriched items "
                f"are saved. Re-run after midnight Pacific."
            )


def chunked(seq: list, n: int) -> Iterable[list]:
    """Yield successive n-sized chunks of a list."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ────────────────────────────── helpers ─────────────────────────────────────

def _item_header(ordinal: int, total: int, entry: dict) -> str:
    """The delimiter + id header that opens one item's block."""
    return (f"----- ITEM {ordinal} of {total} -----\n"
            f"item_id: {entry['item_id']}")


def _item_lines(entry: dict) -> list[str]:
    """The field lines describing one item (after its header)."""
    lines = [f"  title: {entry.get('title', '')}"]
    if entry.get("price"):
        lines.append(f"  asking_price: {entry['price']}")
    if entry.get("category"):
        lines.append(f"  category: {entry['category']}")
    desc = entry.get("description")
    if desc and desc != entry.get("title"):
        # Keep prompts reasonable: cap very long bodies.
        lines.append(f"  description: {desc[:1200]}")
    return lines


def _first_image_url(entry: dict) -> str:
    urls = entry.get("image_urls")
    if isinstance(urls, list) and urls:
        return urls[0]
    return entry.get("image_url") or ""


def _build_contents(batch: list[dict], fetch_part):
    """Build the request body for one batch.

    fetch_part is None for text-only mode (returns a single prompt string), or
    a callable(url) -> (Part, num_bytes) | None for multimodal mode (returns a
    list that interleaves each item's text with its photo).

    Robust photo↔item binding (so batches of 25 don't get scrambled):
      • every item is opened by a "----- ITEM n of N -----" + "item_id: <id>"
        header, giving the model both an ordinal and an explicit id;
      • when a photo is attached, the item block's LAST line is a caption
        naming that item's id, and the image Part is the very next element, so
        nothing ever sits between a photo and its id label;
      • items without a usable photo end with an explicit "(no photo ...)" line
        so the model can't borrow a neighbor's image.
    A per-request byte budget keeps inline images under the API's ~20 MB ceiling.
    """
    total = len(batch)
    intro = [
        "Value each item below. Each item starts with a header line "
        "'----- ITEM n of N -----', then 'item_id: <id>', then its fields.",
        "asking_price is the seller's listed price, NOT the item's retail "
        "value — estimate retail independently.",
        "Return exactly one valuation per item, echoing item_id exactly.",
        "",
    ]

    if fetch_part is None:
        out = ["\n".join(intro)]
        for i, entry in enumerate(batch, 1):
            block = [_item_header(i, total, entry)] + _item_lines(entry)
            block.append("  (no photo provided for this item)")
            out.append("\n".join(block))
        return "\n\n".join(out)

    contents: list = ["\n".join(intro)]
    used_bytes = 0
    budget_hit = False
    for i, entry in enumerate(batch, 1):
        block = [_item_header(i, total, entry)] + _item_lines(entry)
        part = None
        url = _first_image_url(entry)
        if url and not budget_hit:
            res = fetch_part(url)
            if res is not None:
                cand_part, nbytes = res
                if used_bytes + nbytes > _MAX_REQUEST_IMAGE_BYTES:
                    budget_hit = True
                else:
                    part = cand_part
                    used_bytes += nbytes
        if part is not None:
            block.append(
                f"PHOTO FOR item_id {entry['item_id']} (item {i} of {total}) "
                f"— the next image is THIS item's photo and belongs to no "
                f"other item:"
            )
            contents.append("\n".join(block))   # caption is the last line
            contents.append(part)                # image immediately follows
        else:
            block.append("  (no photo provided for this item)")
            contents.append("\n".join(block))

    if budget_hit:
        print("  [images] per-request image-size budget reached; remaining "
              "items in this batch were sent text-only")
    return contents


def _validate_batch_ids(batch: list[dict], valuations: list, worker_name: str) -> None:
    """Log a warning if the echoed item_ids don't line up with what we sent.

    Catches structural problems (a truncated response missing rows, an invented
    id, a duplicated id) and surfaces them in the run log — handy during a test
    run. It mutates nothing: results are applied strictly by echoed item_id
    downstream, so an unknown/duplicate row simply lands on the right item or is
    dropped, and a missing item keeps its empty enrichment and retries next run.
    (A wrong photo described under a correct id is a content error this can't
    see; the per-image id captions above are what prevent that.)
    """
    sent = {e["item_id"] for e in batch}
    got = [v.item_id for v in valuations]
    got_set = set(got)
    missing = sent - got_set
    extra = got_set - sent
    has_dupes = len(got) != len(got_set)
    if missing or extra or has_dupes:
        parts = []
        if missing:
            parts.append(f"{len(missing)} missing")
        if extra:
            parts.append(f"{len(extra)} unknown id(s)")
        if has_dupes:
            parts.append("duplicate id(s)")
        print(f"  ⚠ [{worker_name}] id check: {', '.join(parts)} "
              f"(sent {len(sent)}, got {len(got)}). Unknown/duplicate rows are "
              f"dropped; missing items retry next run.")


def _parse_response(response) -> list[ItemValuation]:
    """Extract valuations from a Gemini response object."""
    try:
        parsed: BatchResponse | None = getattr(response, "parsed", None)
        if parsed is None:
            text = response.text or ""
            data = json.loads(text)
            parsed = BatchResponse(**data)
        return parsed.valuations
    except Exception as e:
        text_preview = (getattr(response, "text", "") or "")[:300]
        print(f"  ! could not parse Gemini response: {e}")
        print(f"    raw: {text_preview}")
        return []


_STATUS_RE = re.compile(r"\b(\d{3})\s+[A-Z_]+", re.MULTILINE)
_RETRY_DELAY_RE = re.compile(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s['\"]?")
_RETRY_PHRASE_RE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_MEDIA_RES_ERR_RE = re.compile(r"media[\s_]*resolution", re.IGNORECASE)

# Phrases Google returns for permanently-broken keys/projects. These mean
# "this key will NEVER work again on its own" — as opposed to 429 (wait and
# retry) or 503 (overloaded, retry soon). Used as a fallback when the numeric
# status code isn't cleanly parseable from the wrapped SDK error string.
_PERMANENT_AUTH_PHRASES = (
    "permission_denied",
    "denied access",
    "api key not valid",
    "api_key_invalid",
    "unauthenticated",
    "permission denied",
    "consumer_suspended",
    "has been suspended",
    "is not enabled",
    "billing",
)


def _guess_mime(url: str) -> str:
    """Guess an image MIME type from the URL extension; default to JPEG."""
    low = (url or "").lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".webp"):
        return "image/webp"
    if low.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _is_media_resolution_error(err_str: str) -> bool:
    """True if the error is specifically about the media_resolution setting.

    Covers both an API 400 ('mediaResolution not enabled for api version ...')
    and a client-side TypeError when the installed SDK's config object doesn't
    accept the field at all. Distinguishing this from a real 400 is important —
    a real 400 marks the key dead, but this one just means 'drop the hint'.
    """
    return bool(_MEDIA_RES_ERR_RE.search(err_str or ""))


def _is_permanent_auth_error(err_str: str) -> bool:
    """True if the error text indicates a permanently-dead key/project."""
    low = (err_str or "").lower()
    return any(p in low for p in _PERMANENT_AUTH_PHRASES)


def _extract_status_code(err_str: str) -> int | None:
    """Pull the HTTP status code out of a Gemini error string."""
    m = _STATUS_RE.search(err_str)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _extract_retry_delay(err_str: str) -> float | None:
    """Pull the suggested retry delay (in seconds) from a 429 error."""
    m = _RETRY_DELAY_RE.search(err_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = _RETRY_PHRASE_RE.search(err_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
