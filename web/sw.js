"use strict";
const SHELL_CACHE = "astana-walks-shell-v3";
const SHELL_PATHS = new Set([
  "/", "/index.html", "/styles.css", "/app.js", "/pwa.js",
  "/manifest.webmanifest", "/favicon.svg",
  "/icons/icon-192.png", "/icons/icon-512.png",
  "/icons/maskable-512.png", "/icons/apple-touch-icon.png",
  "/map.js", "/map.css",
  "/vendor/leaflet/1.9.4/leaflet.js", "/vendor/leaflet/1.9.4/leaflet.css",
  "/vendor/leaflet/1.9.4/images/marker-icon.png",
  "/vendor/leaflet/1.9.4/images/marker-icon-2x.png",
  "/vendor/leaflet/1.9.4/images/marker-shadow.png",
  "/vendor/leaflet/1.9.4/images/layers.png",
  "/vendor/leaflet/1.9.4/images/layers-2x.png"
]);

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache =>
    cache.addAll([...SHELL_PATHS].map(path => new Request(path, {cache: "reload"})))
  ));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith("astana-walks-shell-") && key !== SHELL_CACHE)
      .map(key => caches.delete(key))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // API requests always use the network. Never retain queries, coordinates or responses.
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request).catch(() => new Response(JSON.stringify({
      error: "Нет связи с сервисом. Подключитесь к интернету и попробуйте ещё раз."
    }), {status: 503, headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store"
    }})));
    return;
  }
  // Do not cache arbitrary paths, query strings or non-GET requests.
  if (request.method !== "GET" || url.search || !SHELL_PATHS.has(url.pathname)) return;
  event.respondWith((async () => {
    let cache;
    try { cache = await caches.open(SHELL_CACHE); } catch { /* Network still works without storage. */ }
    let response;
    try {
      response = await fetch(request);
    } catch {
      return cache && await cache.match(url.pathname) || Response.error();
    }
    if (cache && response.ok && response.type !== "opaque") {
      // Quota/storage failures must never replace a successful network response.
      try { await cache.put(url.pathname, response.clone()); } catch { /* Best effort. */ }
    }
    return response;
  })());
});
