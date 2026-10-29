const CACHE = "svwap-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const u = new URL(e.request.url), key = u.origin + u.pathname;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const r = await fetch(e.request);
      if (r.ok) cache.put(key, r.clone());
      return r;
    } catch (err) {
      const hit = await cache.match(key);
      if (!hit) throw err;
      const h = new Headers(hit.headers); h.set("X-SW-Cache", "1");
      return new Response(await hit.blob(), {status: 200, headers: h});
    }
  })());
});
