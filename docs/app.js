// Market Snapshot — vanilla JS, no framework, no build step.
// Reads data/items.json, renders cards with sort/filter/search controls.
//
// Pagination:    only render PAGE_SIZE cards at a time so thousands of items
//                don't choke the browser.
// State persist: filter/sort/search/scroll/renderedCount survive a page reload
//                via sessionStorage, so "Load more" + scroll position aren't lost.
// Refresh:       there is intentionally no auto-refresh banner. To get a fresh
//                snapshot, hard-reload the page (Ctrl+Shift+R).
// Service Worker (sw.js) caches the static files + items.json so repeat
// visits are instant; a fresh copy is fetched in the background.

(function () {
  "use strict";

  const PAGE_SIZE = 60;                  // cards rendered per "page"
  const STATE_KEY = "market-snapshot:ui-state";
  const STATE_VERSION = 1;

  // ─── Smart-score weights ────────────────────────────────────────
  // Smart score = w_roi*ROI_norm + w_profit*profit_norm + w_velocity*velocity_norm
  // Tweak weights here if you want a different emphasis.
  //
  // Tuned toward absolute profit dollars (45%) over ROI (30%): a $150
  // gross-profit item that's 3× ROI is usually more useful than a $15-profit
  // item that's 10× ROI, because real-world flip economics care about dollars
  // per pickup-trip, not ratios.
  const SMART_WEIGHTS = {
    roi: 0.30,
    profit: 0.45,
    velocity: 0.25,
  };
  // Log-scale normalization: we DO NOT cap ROI or profit. Capping would shove
  // a rare big find down to the rank of a merely-good one. Log compresses the
  // range gracefully:
  //     ROI  1×  → 0.41      Profit  $25  → 0.47
  //     ROI  3×  → 0.79      Profit  $50  → 0.57
  //     ROI 10×  → 1.0+      Profit $200  → 0.77
  //                          Profit $1000 → 1.00 (full weight)
  // The log denominator sets where "1.0" lands; values above still contribute
  // proportionally more.
  const LOG_ROI_DENOM = log10p(10);      // ROI of 10 maps to ~1.0
  const LOG_PROFIT_DENOM = log10p(1000); // Profit of $1000 maps to 1.0
  function log10p(x) { return Math.log10(1 + Math.max(0, x)); }

  // Sales-velocity tier → numeric score. Mirrors SALES_VELOCITY_SCORES in config.py.
  const VELOCITY_SCORES = {
    hot: 1.0,
    normal: 0.65,
    slow: 0.35,
    very_slow: 0.10,
    unknown: 0.0,
    "": 0.0,
  };

  // Purchase-price model mirrors config.py: cost to acquire ≈ asking price ×
  // NEGOTIATION_FACTOR (you talk them down ~10%). HASSLE mirrors
  // config.PICKUP_HASSLE_DOLLARS. KEEP THESE IN SYNC WITH config.py.
  const NEGOTIATION_FACTOR = 0.9;
  const HASSLE = 10.0;

  // ─── Condition tiers ────────────────────────────────────────────
  // Mirrors _condition_resale_factor() in scrape.py. The factor multiplies
  // estimated_resale: 1.0 for new / open_box (default — no penalty when
  // condition is unknown), 0.85 for damaged_easy_fix, 0.0 for damaged_hard_fix.
  const CONDITION_FACTORS = {
    new:               1.00,
    open_box:          1.00,
    damaged_easy_fix:  0.85,
    damaged_hard_fix:  0.00,
  };
  const CONDITION_LABELS = {
    new:               "new",
    open_box:          "open box",
    damaged_easy_fix:  "easy fix",
    damaged_hard_fix:  "hard fix",
  };

  // Mirrors _PLACEHOLDER_PRICES in scrape.py: obvious teaser/sequential prices
  // we won't trust as a real cost when the model gave no effective price.
  const PLACEHOLDER_PRICES = new Set([
    1, 11, 111, 1111, 11111, 111111,
    12, 123, 1234, 12345, 123456, 1234567,
    321, 4321, 54321,
    1212, 1010,
    9999, 99999, 999999,
  ]);

  // ---- DOM refs ---------------------------------------------------
  const grid           = document.getElementById("grid");
  const sortSel        = document.getElementById("sort");
  const velocitySel    = document.getElementById("velocity");
  const conditionSel   = document.getElementById("condition");
  const minFlipInput   = document.getElementById("min-flip");
  const minFlipValue   = document.getElementById("min-flip-value");
  const searchInput    = document.getElementById("search");
  const searchClear    = document.getElementById("search-clear");
  const freshnessEl    = document.getElementById("freshness");
  const freshnessText  = document.getElementById("freshness-text");
  const resultCount    = document.getElementById("result-count");
  const cardTpl        = document.getElementById("card-template");
  const loadMoreWrap   = document.getElementById("load-more-wrap");
  const loadMoreBtn    = document.getElementById("load-more");
  const loadMoreInfo   = document.getElementById("load-more-info");

  // ---- State ------------------------------------------------------
  let allItems        = [];
  let filteredItems   = [];   // current filtered+sorted view
  let renderedCount   = 0;    // how many of filteredItems are drawn
  let generatedAt     = null;
  let nowMs           = Date.now();
  let pendingRestore  = null; // { renderedCount, scrollY } from sessionStorage
  let saveStateTimer  = null;
  let searchDebounce  = null;
  let searchQueryNorm = "";   // lowercased + trimmed; used by filter

  // ---- Boot -------------------------------------------------------
  pendingRestore = restoreUiState();   // read saved state BEFORE first render
  registerServiceWorker();
  loadData();
  bindControls();
  bindUnloadSave();

  // Re-tick "now" each minute so the freshness pill ("Refreshed N minutes
  // ago") stays current. We deliberately don't re-render or re-paginate so the
  // user doesn't lose their scroll position.
  setInterval(() => {
    nowMs = Date.now();
    renderFreshness();
  }, 60_000);

  // ---- Service Worker registration -------------------------------
  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("sw.js").catch((err) => {
        console.warn("service worker registration failed:", err);
      });
    });
  }

  // ---- Data loading ----------------------------------------------
  async function loadData() {
    try {
      const res = await fetch("data/items.json", { cache: "no-cache" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      allItems    = Array.isArray(data.items) ? data.items : [];
      generatedAt = data.generated_at ? new Date(data.generated_at) : null;
      grid.removeAttribute("aria-busy");
      renderFreshness();
      render();
      applyPendingRestore();
    } catch (err) {
      grid.classList.add("grid--empty");
      grid.removeAttribute("aria-busy");
      grid.textContent =
        "Could not load data/items.json. If you're running locally, make " +
        "sure docs/data/items.json exists and that you started the server " +
        "from the docs/ directory.";
      freshnessEl.className = "freshness freshness--ancient";
      freshnessText.textContent = "no data";
      console.error(err);
    }
  }

  // ---- Controls ---------------------------------------------------
  function bindControls() {
    // Restore values from saved state before we wire change handlers (so we
    // don't fire a render mid-restore). Falls back to defaults from HTML.
    if (pendingRestore) {
      if (typeof pendingRestore.sort === "string") {
        const opt = sortSel.querySelector(`option[value="${pendingRestore.sort}"]`);
        if (opt) sortSel.value = pendingRestore.sort;
      }
      if (typeof pendingRestore.velocity === "string") {
        const opt = velocitySel.querySelector(`option[value="${pendingRestore.velocity}"]`);
        if (opt) velocitySel.value = pendingRestore.velocity;
      }
      if (typeof pendingRestore.condition === "string") {
        const opt = conditionSel.querySelector(`option[value="${pendingRestore.condition}"]`);
        if (opt) conditionSel.value = pendingRestore.condition;
      }
      if (typeof pendingRestore.minFlip === "number") {
        minFlipInput.value = String(pendingRestore.minFlip);
      }
      if (typeof pendingRestore.search === "string") {
        searchInput.value = pendingRestore.search;
        searchQueryNorm = pendingRestore.search.trim().toLowerCase();
        searchClear.hidden = !pendingRestore.search;
      }
    }

    sortSel.addEventListener("change", () => { render(); saveStateSoon(); });
    velocitySel.addEventListener("change", () => { render(); saveStateSoon(); });
    conditionSel.addEventListener("change", () => { render(); saveStateSoon(); });
    minFlipInput.addEventListener("input", () => {
      minFlipValue.textContent = parseFloat(minFlipInput.value).toFixed(1);
      render();
      saveStateSoon();
    });
    minFlipValue.textContent = parseFloat(minFlipInput.value).toFixed(1);

    // Search: debounce so we don't re-render on every keystroke.
    searchInput.addEventListener("input", () => {
      searchClear.hidden = !searchInput.value;
      if (searchDebounce) clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        searchQueryNorm = searchInput.value.trim().toLowerCase();
        render();
        saveStateSoon();
      }, 150);
    });
    searchClear.addEventListener("click", () => {
      searchInput.value = "";
      searchQueryNorm = "";
      searchClear.hidden = true;
      render();
      saveStateSoon();
      searchInput.focus();
    });

    loadMoreBtn.addEventListener("click", () => { renderMore(); saveStateSoon(); });

    // Save scroll position as the user scrolls, debounced.
    window.addEventListener("scroll", () => { saveStateSoon(); }, { passive: true });
  }

  // ---- State persistence -----------------------------------------
  function saveStateSoon() {
    if (saveStateTimer) clearTimeout(saveStateTimer);
    saveStateTimer = setTimeout(saveUiStateNow, 250);
  }

  function saveUiStateNow() {
    try {
      const payload = {
        v: STATE_VERSION,
        sort: sortSel.value,
        velocity: velocitySel.value,
        condition: conditionSel.value,
        minFlip: parseFloat(minFlipInput.value),
        search: searchInput.value,
        renderedCount: renderedCount,
        scrollY: window.scrollY || window.pageYOffset || 0,
      };
      sessionStorage.setItem(STATE_KEY, JSON.stringify(payload));
    } catch (err) {
      // sessionStorage can throw in private browsing or when full. Non-fatal.
    }
  }

  function restoreUiState() {
    try {
      const raw = sessionStorage.getItem(STATE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.v !== STATE_VERSION) return null;
      return parsed;
    } catch (err) {
      return null;
    }
  }

  function bindUnloadSave() {
    window.addEventListener("pagehide", saveUiStateNow);
    window.addEventListener("beforeunload", saveUiStateNow);
  }

  function applyPendingRestore() {
    if (!pendingRestore) return;
    const target = pendingRestore.renderedCount || 0;
    while (renderedCount < target && renderedCount < filteredItems.length) {
      renderMore();
    }
    const scrollY = pendingRestore.scrollY || 0;
    if (scrollY > 0) {
      requestAnimationFrame(() => requestAnimationFrame(() => {
        window.scrollTo(0, scrollY);
      }));
    }
    pendingRestore = null;
  }

  // ---- Freshness banner ------------------------------------------
  function renderFreshness() {
    if (!generatedAt || isNaN(generatedAt.getTime())) {
      freshnessEl.className = "freshness freshness--unknown";
      freshnessText.textContent = "freshness unknown";
      return;
    }
    const ageMs = nowMs - generatedAt.getTime();
    const ageH  = ageMs / 3_600_000;

    let tier, label;
    if (ageH < 12)      { tier = "fresh";   }
    else if (ageH < 24) { tier = "stale";   }
    else                { tier = "ancient"; }

    if (ageH < 1) {
      const m = Math.max(0, Math.round(ageMs / 60_000));
      label = m <= 1 ? "Refreshed just now" : `Refreshed ${m} minutes ago`;
    } else if (ageH < 48) {
      const h = Math.round(ageH);
      label = `Refreshed ${h} hour${h === 1 ? "" : "s"} ago`;
    } else {
      const d = Math.round(ageH / 24);
      label = `Refreshed ${d} days ago`;
    }
    freshnessEl.className = "freshness freshness--" + tier;
    freshnessText.textContent = label;
  }

  // ---- Number / field helpers ------------------------------------
  function num(s) {
    if (s === "" || s === null || s === undefined) return NaN;
    const n = parseFloat(s);
    return isNaN(n) ? NaN : n;
  }
  function dollarsToNum(s) {
    if (s === "" || s === null || s === undefined) return NaN;
    return num(String(s).replace(/[^0-9.]/g, ""));
  }
  // Seller's headline asking price. Prefer the parsed numeric, fall back to raw.
  function priceNum(item) {
    if (typeof item.price_value === "number" && item.price_value > 0) {
      return item.price_value;
    }
    return dollarsToNum(item.price);
  }

  // Cost BASIS — mirrors _cost_basis() in scrape.py. Returns the realistic cash
  // cost to buy the one item that was valued, or NaN when the price can't be
  // trusted (bundle without a per-item price, "make offer", placeholder, or a
  // not-for-sale listing) — those become unscoreable and sink, exactly as on
  // the backend. KEEP THIS IN LOCKSTEP WITH scrape.py.
  function costBasisNum(item) {
    if (item._costBasis !== undefined) return item._costBasis;
    let cost = NaN;
    const kind = (item.ai_listing_kind || "single_item").trim().toLowerCase();
    if (kind === "not_for_sale") {
      item._costBasis = NaN;
      return NaN;
    }
    const eff = num(item.ai_effective_price);
    if (!isNaN(eff) && eff > 0) {
      cost = eff;
    } else {
      const placeholder =
        String(item.ai_price_is_placeholder || "").trim().toLowerCase() === "yes";
      if (kind === "single_item" && !placeholder) {
        const listed = priceNum(item);
        if (!isNaN(listed) && listed > 0 && !PLACEHOLDER_PRICES.has(Math.trunc(listed))) {
          cost = listed;
        } else {
          const raw = String(item.price || "").trim();
          if (raw === "$0" || raw === "$0.00" || raw === "0") cost = 0;
        }
      }
    }
    item._costBasis = cost;
    return cost;
  }

  // Realistic out-of-pocket cost: cost basis × NEGOTIATION_FACTOR.
  function purchasePriceNum(item) {
    const c = costBasisNum(item);
    return isNaN(c) ? NaN : c * NEGOTIATION_FACTOR;
  }

  // ─── Posted-time helpers ────────────────────────────────────────
  // Prefer the seller's posted_at; fall back to when we first saw the listing.
  function postedMs(item) {
    if (item._postedMs === undefined) {
      let t = item.posted_at ? Date.parse(item.posted_at) : NaN;
      if (isNaN(t) && item.first_seen_at) t = Date.parse(item.first_seen_at);
      item._postedMs = isNaN(t) ? NaN : t;
    }
    return item._postedMs;
  }

  // ─── Condition helpers ──────────────────────────────────────────
  function conditionOf(item) {
    if (item._cond === undefined) {
      const c = (item.ai_condition || "").trim().toLowerCase();
      item._cond = (c in CONDITION_FACTORS) ? c : "open_box";
    }
    return item._cond;
  }
  function conditionFactorOf(item) {
    return CONDITION_FACTORS[conditionOf(item)];
  }

  // Recompute flip score (ROI) from raw fields. Stays in sync with
  // compute_flip_score() in scrape.py — same purchase-price model and same
  // condition-as-resale-multiplier treatment.
  function computeFlipScore(item) {
    const resale = num(item.ai_estimated_resale);
    const purchase = purchasePriceNum(item);
    if (isNaN(resale) || isNaN(purchase) || resale <= 0) return NaN;
    const effectiveResale = resale * conditionFactorOf(item);
    const denom = Math.max(purchase, 1.0);
    return (effectiveResale - purchase - HASSLE) / denom;
  }
  function flipScoreOf(item) {
    if (item._fs === undefined) item._fs = computeFlipScore(item);
    return item._fs;
  }

  // Gross profit in dollars: effective_resale - purchase - hassle.
  function computeGrossProfit(item) {
    const resale = num(item.ai_estimated_resale);
    const purchase = purchasePriceNum(item);
    if (isNaN(resale) || isNaN(purchase) || resale <= 0) return NaN;
    const effectiveResale = resale * conditionFactorOf(item);
    return effectiveResale - purchase - HASSLE;
  }
  function grossProfitOf(item) {
    if (item._gp === undefined) item._gp = computeGrossProfit(item);
    return item._gp;
  }

  // Sales velocity score: numeric value derived from ai_sales_velocity tier.
  function velocityScoreOf(item) {
    if (item._vs === undefined) {
      const v = (item.ai_sales_velocity || "").toLowerCase();
      item._vs = VELOCITY_SCORES[v] !== undefined ? VELOCITY_SCORES[v] : 0;
    }
    return item._vs;
  }

  // Smart score blends ROI, gross profit, and sales velocity into one rank.
  // Returns NaN if we can't compute ROI/profit (AI confidence unknown), so
  // unknowns sink to the bottom of any sort. Negative ROI / profit contribute
  // 0 (those items are losing money — don't reward them).
  function smartScoreOf(item) {
    if (item._ss === undefined) {
      const roi    = flipScoreOf(item);
      const profit = grossProfitOf(item);
      if (isNaN(roi) || isNaN(profit)) {
        item._ss = NaN;
      } else {
        const roiNorm    = log10p(roi)    / LOG_ROI_DENOM;
        const profitNorm = log10p(profit) / LOG_PROFIT_DENOM;
        const velNorm    = velocityScoreOf(item);
        item._ss = SMART_WEIGHTS.roi    * roiNorm
                 + SMART_WEIGHTS.profit * profitNorm
                 + SMART_WEIGHTS.velocity * velNorm;
      }
    }
    return item._ss;
  }

  // Split haystacks: we score title hits higher than body hits so a search for
  // "drill" surfaces "DeWalt Drill" before an item whose notes mention
  // "drilled holes". Built lazily and cached on the item.
  function titleHayOf(item) {
    if (item._titleHay === undefined) {
      item._titleHay = (item.title || "").toLowerCase();
    }
    return item._titleHay;
  }
  function bodyHayOf(item) {
    if (item._bodyHay === undefined) {
      const parts = [
        item.ai_notes,
        item.category,
        item.description,
        item.location,
      ].filter(Boolean);
      item._bodyHay = parts.join(" \n ").toLowerCase();
    }
    return item._bodyHay;
  }

  // Build search-term regexes from the user's query. ALL terms must match
  // (AND). For each term we precompile a whole-word regex and a prefix regex.
  function buildSearchTerms(query) {
    if (!query) return null;
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length === 0) return null;
    return terms.map((t) => {
      const esc = escapeRegex(t);
      return {
        raw: t,
        reWord:   new RegExp("\\b" + esc + "\\b"),
        rePrefix: new RegExp("\\b" + esc),
      };
    });
  }
  function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // Filter test: every term must match SOMEWHERE — title or body — at least at
  // the prefix level. The relevance tier (below) decides ranking among survivors.
  function matchesSearch(item, terms) {
    if (!terms) return true;
    const titleHay = titleHayOf(item);
    const bodyHay = bodyHayOf(item);
    for (let i = 0; i < terms.length; i++) {
      const t = terms[i];
      if (!t.rePrefix.test(titleHay) && !t.rePrefix.test(bodyHay)) return false;
    }
    return true;
  }

  // Per-term tier (lower = better match):
  //   1: title whole-word   2: title prefix   3: body whole-word   4: body prefix
  function termTier(term, titleHay, bodyHay) {
    if (term.reWord.test(titleHay))   return 1;
    if (term.rePrefix.test(titleHay)) return 2;
    if (term.reWord.test(bodyHay))    return 3;
    if (term.rePrefix.test(bodyHay))  return 4;
    return 99;
  }

  // Item-level tier = the WORST per-term tier ("weakest link"), so results
  // form clean bands. Cached per render-pass via the terms object identity.
  function searchTierOf(item, terms) {
    if (item._tierTerms === terms) return item._tier;
    const titleHay = titleHayOf(item);
    const bodyHay = bodyHayOf(item);
    let worst = 0;
    for (let i = 0; i < terms.length; i++) {
      const t = termTier(terms[i], titleHay, bodyHay);
      if (t > worst) worst = t;
    }
    item._tier = worst;
    item._tierTerms = terms;
    return worst;
  }

  // ---- Sort comparators ------------------------------------------
  function descBy(getter) {
    return (a, b) => {
      const av = getter(a), bv = getter(b);
      const ag = isNaN(av) ? 1 : 0, bg = isNaN(bv) ? 1 : 0;
      if (ag !== bg) return ag - bg;
      if (ag === 1) return 0;
      return bv - av;
    };
  }
  function ascBy(getter) {
    return (a, b) => {
      const av = getter(a), bv = getter(b);
      const ag = isNaN(av) ? 1 : 0, bg = isNaN(bv) ? 1 : 0;
      if (ag !== bg) return ag - bg;
      if (ag === 1) return 0;
      return av - bv;
    };
  }
  const COMPARATORS = {
    smart:        descBy(smartScoreOf),
    flip_score:   descBy(flipScoreOf),
    gross_profit: descBy(grossProfitOf),
    price:        ascBy(priceNum),
    newest:       descBy(postedMs),
    title: (a, b) =>
      (a.title || "").localeCompare(b.title || "", undefined, { sensitivity: "base" }),
  };

  // ---- Velocity filter -------------------------------------------
  function passesVelocityFilter(item, mode) {
    if (mode === "any") return true;
    const v = (item.ai_sales_velocity || "").toLowerCase();
    if (mode === "hot")              return v === "hot";
    if (mode === "hot_or_normal")    return v === "hot" || v === "normal";
    if (mode === "exclude_very_slow") return v !== "very_slow";
    return true;
  }

  // ---- Condition filter ------------------------------------------
  function passesConditionFilter(item, mode) {
    if (mode === "any") return true;
    const c = conditionOf(item);
    if (mode === "new")              return c === "new";
    if (mode === "new_or_open_box")  return c === "new" || c === "open_box";
    if (mode === "exclude_hard_fix") return c !== "damaged_hard_fix";
    return true;
  }

  // ---- Render -----------------------------------------------------
  function render() {
    const minFlip       = parseFloat(minFlipInput.value);
    const sortBy        = sortSel.value;
    const velocityMode  = velocitySel.value;
    const conditionMode = conditionSel.value;
    const terms         = buildSearchTerms(searchQueryNorm);

    filteredItems = allItems.filter((it) => {
      if (!passesVelocityFilter(it, velocityMode)) return false;
      if (!passesConditionFilter(it, conditionMode)) return false;
      if (!matchesSearch(it, terms)) return false;
      const f = flipScoreOf(it);
      if (isNaN(f)) return minFlip === 0;
      return f >= minFlip;
    });

    // With a search active, GROUP by relevance tier first, then apply the
    // chosen sort within each group. Clean bands rather than a gradient.
    const baseCmp = COMPARATORS[sortBy] || COMPARATORS.smart;
    if (terms) {
      filteredItems.sort((a, b) => {
        const ta = searchTierOf(a, terms);
        const tb = searchTierOf(b, terms);
        if (ta !== tb) return ta - tb;
        return baseCmp(a, b);
      });
    } else {
      filteredItems.sort(baseCmp);
    }

    const total = allItems.length;
    resultCount.textContent =
      `${filteredItems.length} of ${total} item${total === 1 ? "" : "s"}`;

    // Reset paging and clear the grid
    renderedCount = 0;
    grid.replaceChildren();

    if (filteredItems.length === 0) {
      grid.classList.add("grid--empty");
      const reasons = [];
      if (searchQueryNorm) reasons.push("clear the search");
      if (minFlip > 0) reasons.push("lower the min ROI");
      if (velocityMode !== "any") reasons.push("change Velocity to Any");
      if (conditionMode !== "any") reasons.push("change Condition to Any");
      const hint = reasons.length
        ? `Try ${reasons.join(" or ")} to see more.`
        : "There are no items in the data file yet.";
      grid.innerHTML = `<p>No items match the current filters.<br><small>${hint}</small></p>`;
      loadMoreWrap.hidden = true;
      return;
    }

    grid.classList.remove("grid--empty");
    renderMore();
  }

  // Append the next PAGE_SIZE cards.
  function renderMore() {
    const end = Math.min(renderedCount + PAGE_SIZE, filteredItems.length);
    const frag = document.createDocumentFragment();
    for (let i = renderedCount; i < end; i++) {
      frag.appendChild(buildCard(filteredItems[i]));
    }
    grid.appendChild(frag);
    renderedCount = end;

    if (renderedCount >= filteredItems.length) {
      loadMoreWrap.hidden = true;
    } else {
      loadMoreWrap.hidden = false;
      const remaining = filteredItems.length - renderedCount;
      loadMoreInfo.textContent =
        `Showing ${renderedCount} of ${filteredItems.length} — ${remaining} more`;
    }
  }

  // ---- Card builder ----------------------------------------------
  function buildCard(item) {
    const node = cardTpl.content.firstElementChild.cloneNode(true);

    // Image
    const img = node.querySelector("img");
    if (item.image_url) {
      img.src = item.image_url;
      img.alt = item.title || "listing";
      img.addEventListener("error", () => img.classList.add("broken"), { once: true });
    } else {
      img.classList.add("broken");
      img.alt = "";
    }

    // Flip score (ROI) badge
    const scoreEl = node.querySelector('[data-role="flip-score"]');
    const f = flipScoreOf(item);
    if (isNaN(f)) {
      scoreEl.textContent = "—";
      scoreEl.classList.add("empty");
      scoreEl.title = "No flip score (AI confidence: unknown)";
    } else {
      scoreEl.textContent = f.toFixed(2) + "×";
      scoreEl.title =
        `ROI: ${f.toFixed(2)}× — ` +
        `(effective resale − cost − $${HASSLE.toFixed(0)} hassle) ÷ cost. ` +
        `Cost = effective item price × ${NEGOTIATION_FACTOR.toFixed(2)}. ` +
        `Effective resale = est. resale × condition factor.`;
    }

    // Gross profit badge ($)
    const profitEl = node.querySelector('[data-role="gross-profit"]');
    if (profitEl) {
      const gp = grossProfitOf(item);
      if (isNaN(gp)) {
        profitEl.textContent = "";
      } else {
        profitEl.textContent = "$" + Math.round(gp);
        profitEl.title = `Estimated gross profit: $${gp.toFixed(2)}`;
      }
    }

    // Sales velocity badge
    const velEl = node.querySelector('[data-role="velocity"]');
    if (velEl) {
      const v = (item.ai_sales_velocity || "").toLowerCase();
      if (v && v !== "unknown") {
        velEl.textContent = v.replace("_", " ");
        velEl.setAttribute("data-velocity", v);
        velEl.title = `Estimated FB Marketplace velocity: ${v.replace("_", " ")}`;
      } else {
        velEl.textContent = "";
      }
    }

    // Confidence badge
    const confEl = node.querySelector('[data-role="confidence"]');
    if (item.ai_confidence && item.ai_confidence !== "unknown") {
      confEl.textContent = item.ai_confidence;
    }

    // Condition badge — renders only when the AI tagged a recognized condition.
    const condEl = node.querySelector('[data-role="condition"]');
    if (condEl) {
      const rawCond = (item.ai_condition || "").trim().toLowerCase();
      if (rawCond && rawCond in CONDITION_LABELS) {
        condEl.textContent = CONDITION_LABELS[rawCond];
        condEl.setAttribute("data-condition", rawCond);
        condEl.title = `AI-assessed condition: ${CONDITION_LABELS[rawCond]}`;
      } else {
        condEl.textContent = "";
      }
    }

    // Listing-kind / price-clarity flag badge.
    const kind = (item.ai_listing_kind || "single_item").trim().toLowerCase();
    const costUnknown = isNaN(costBasisNum(item));
    const flagEl = node.querySelector('[data-role="listing-flag"]');
    if (flagEl) {
      if (kind === "multi_item") {
        flagEl.textContent = "bundle";
        flagEl.setAttribute("data-flag", "bundle");
        flagEl.title = "Multi-item listing — the figures describe one item from it"
          + (item.ai_product ? `: ${item.ai_product}` : "");
      } else if (kind === "not_for_sale") {
        flagEl.textContent = "not for sale";
        flagEl.setAttribute("data-flag", "not-for-sale");
        flagEl.title = "Not a single item for sale (wanted ad, garage sale, found item, etc.)";
      } else if (costUnknown) {
        flagEl.textContent = "price?";
        flagEl.setAttribute("data-flag", "price");
        flagEl.title = "Asking price looks like a placeholder or make-offer — open the listing for the real price";
      } else {
        flagEl.textContent = "";
      }
    }

    // Title
    node.querySelector('[data-role="title"]').textContent = item.title || "(untitled)";

    // Valued-item subtitle: for bundles, say which single item the numbers describe.
    const subEl = node.querySelector('[data-role="valued-item"]');
    if (subEl) {
      if (kind === "multi_item" && item.ai_product) {
        subEl.textContent = `figures for: ${item.ai_product}`;
        subEl.hidden = false;
      } else {
        subEl.hidden = true;
      }
    }

    // Stats: asking price, purchase cost (×0.9), resale, retail
    node.querySelector('[data-role="price"]').textContent = item.price || "—";
    const costEl = node.querySelector('[data-role="purchase-price"]');
    if (costEl) {
      const cost = purchasePriceNum(item);
      if (isNaN(cost)) {
        // Price can't be trusted (placeholder / make-offer / bundle / not for sale).
        costEl.textContent = kind === "not_for_sale" ? "—" : "see listing";
        costEl.title = "No reliable acquisition price — check the listing";
      } else {
        costEl.textContent = "$" + cost.toFixed(2);
        costEl.title = "Realistic out-of-pocket: effective item price × 0.9 (light haggling)";
      }
    }
    const resale = num(item.ai_estimated_resale);
    node.querySelector('[data-role="resale"]').textContent =
      isNaN(resale) ? "—" : "$" + resale.toFixed(2);
    const retail = num(item.ai_retail_estimate);
    node.querySelector('[data-role="retail"]').textContent =
      isNaN(retail) ? "—" : "$" + retail.toFixed(2);

    // Footer
    const postedEl   = node.querySelector('[data-role="posted"]');
    const categoryEl = node.querySelector('[data-role="category"]');
    if (postedEl) postedEl.textContent = formatPosted(item);
    categoryEl.textContent = shortCategory(item.category);

    // Location
    const locationEl = node.querySelector('[data-role="location"]');
    if (locationEl) {
      locationEl.textContent = item.location || "";
    }

    // Click anywhere → open in new tab
    if (item.item_url) {
      node.addEventListener("click", () => {
        saveUiStateNow();
        window.open(item.item_url, "_blank", "noopener");
      });
      node.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          saveUiStateNow();
          window.open(item.item_url, "_blank", "noopener");
        }
      });
      node.setAttribute("aria-label", `Open ${item.title || "listing"}`);
    }

    return node;
  }

  // ---- Display formatters ----------------------------------------
  function formatPosted(item) {
    const t = postedMs(item);
    if (isNaN(t)) return "";
    const prefix = item.posted_at && !isNaN(Date.parse(item.posted_at))
      ? "posted" : "seen";
    const diffMs = nowMs - t;
    if (diffMs < 0) return `${prefix} just now`;
    const minutes = Math.round(diffMs / 60_000);
    if (minutes < 60)  return `${prefix} ${Math.max(1, minutes)}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24)    return `${prefix} ${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${prefix} ${days}d ago`;
  }
  function shortCategory(cat) {
    if (!cat) return "";
    const parts = cat.split(">").map((s) => s.trim()).filter(Boolean);
    return parts[parts.length - 1] || "";
  }
})();
