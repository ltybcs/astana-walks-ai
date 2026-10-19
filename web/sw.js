"use strict";
const SHELL_CACHE = "astana-walks-shell-v1";
const SHELL_PATHS = new Set([
  "/", "/index.html", "/styles.css", "/app.js", "/pwa.js",
  "/manifest.webmanifest", "/favicon.svg",
  "/icons/icon-192.png", "/icons/icon-512.png",
  "/icons/maskable-512.png", "/icons/apple-touch-icon.png"
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
    const cache = await caches.open(SHELL_CACHE);
    try {
      const response = await fetch(request);
      if (response.ok && response.type !== "opaque") {
        await cache.put(url.pathname, response.clone());
      }
      return response;
    } catch {
      return await cache.match(url.pathname) || Response.error();
    }
  })());
});
