// Market Snapshot service worker.
// ---------------------------------------------------------------
// Goal: make repeat visits feel instant, AND always show fresh data.
//
// Strategy:
//   - HTML / CSS / JS:   cache-first.   Fastest possible paint.
//   - data/items.json:   network-first. Always try for the latest copy on
//                        every load; if the network is down, serve the
//                        last-known cached copy as an offline fallback.
//   - Everything else (CDN images): just hit the network normally.
//
// Bump CACHE_VERSION whenever you change app.js, index.html, or style.css so
// old clients pick up the new code on next reload.

const CACHE_VERSION = "v1";
const STATIC_CACHE  = `static-${CACHE_VERSION}`;
const DATA_CACHE    = `data-${CACHE_VERSION}`;

// Files we want available offline / instantly on repeat load.
// Keep this list to OUR static assets — not third-party CDN images.
const STATIC_ASSETS = [
  "./",
  "./index.html",
  "./style.css",
  "./app.js",
];

// ---- install: pre-cache the static shell ----------------------
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => {
      // CRITICAL: use {cache: "reload"} so the precache fetch BYPASSES the
      // browser's HTTP cache and goes to the network. Without this, when a new
      // SW version installs, it can pull stale copies of index.html / app.js /
      // style.css from the HTTP cache and store those into the new SW cache,
      // so the user sees old code on the next reload even though the SW
      // version was bumped. cache:"reload" forces a fresh GET for each asset.
      return Promise.all(
        STATIC_ASSETS.map((url) =>
          fetch(new Request(url, { cache: "reload" }))
            .then((res) => {
              if (!res || !res.ok) {
                throw new Error(`precache failed for ${url}: HTTP ${res && res.status}`);
              }
              return cache.put(url, res);
            })
        )
      );
    })
  );
  self.skipWaiting();
});

// ---- activate: drop old caches when CACHE_VERSION bumps -------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE && k !== DATA_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ---- message: no-op SKIP_WAITING safety net -------------------
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// ---- fetch: route by URL ---------------------------------------
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Only handle GETs from our own origin. Everything else goes to network.
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // The data file: network-first so we always pick up fresh scrape results.
  if (url.pathname.endsWith("/data/items.json")) {
    event.respondWith(networkFirst(req, DATA_CACHE));
    return;
  }

  // Static shell: cache-first.
  if (
    url.pathname.endsWith("/") ||
    url.pathname.endsWith("/index.html") ||
    url.pathname.endsWith("/app.js") ||
    url.pathname.endsWith("/style.css")
  ) {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // Anything else same-origin: just network.
});

// Cache-first: try cache, fall back to network, store on the way back.
async function cacheFirst(request, cacheName) {
  const cache  = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    throw err;
  }
}

// Network-first: try the network; if it works, cache for offline fallback.
// If the network fails, serve last cached copy.
async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}
