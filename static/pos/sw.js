// Service worker of the cash register: keeps the register page, its scripts and product
// photos on this computer, so the register opens and works without internet.
// Served as /pos-sw.js (scope /pos). The data itself is kept by offline.js.
const CACHE = "paprika-pos-v1";
const SHELL = [
  "/pos",
  "/static/pos/pos.css",
  "/static/pos/pos.js",
  "/static/pos/offline.js",
  "/static/device.js",
  "/static/favicon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

// The register asks to keep the photos of its menu (they load lazily on screen).
self.addEventListener("message", (event) => {
  if (event.data?.type !== "cache-images") return;
  event.waitUntil(caches.open(CACHE).then(async (cache) => {
    for (const url of event.data.urls) {
      if (await cache.match(url)) continue;
      try {
        const response = await fetch(url);
        if (response.ok) await cache.put(url, response);
      } catch { /* offline: try again next time */ }
    }
  }));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (url.pathname.startsWith("/static/images/")) {
    // Photos do not change under the same name: the saved copy is enough.
    event.respondWith(caches.open(CACHE).then(async (cache) => {
      const saved = await cache.match(request);
      if (saved) return saved;
      const response = await fetch(request);
      if (response.ok) cache.put(request, response.clone());
      return response;
    }));
    return;
  }

  // Page and scripts: the server's version when online (updates), the saved one offline.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request.mode === "navigate" ? "/pos" : request, copy));
        }
        return response;
      })
      .catch(async () => {
        const saved = await caches.match(request.mode === "navigate" ? "/pos" : request, { ignoreSearch: true });
        return saved || Response.error();
      }),
  );
});
