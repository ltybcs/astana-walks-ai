"use strict";
const SHELL_CACHE = "astana-walks-shell-v6-categories";
const SHELL_PATHS = new Set([
  "/", "/lab.js", "/lab.css", "/pwa.js", "/manifest.webmanifest", "/favicon.svg",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/maskable-512.png", "/icons/apple-touch-icon.png"
]);
self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache =>
    cache.addAll([...SHELL_PATHS].map(path => new Request(path, {cache: "reload"})))
  ).then(() => self.skipWaiting()));
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
  if (request.method !== "GET" || url.origin !== self.location.origin || url.search || !SHELL_PATHS.has(url.pathname)) return;
  event.respondWith((async () => {
    let cache;
    try { cache = await caches.open(SHELL_CACHE); } catch (_) { /* Network works without storage. */ }
    let response;
    try { response = await fetch(request); }
    catch (_) { return cache && await cache.match(url.pathname) || Response.error(); }
    if (cache && response.ok && response.type !== "opaque" && !response.redirected) {
      try { await cache.put(url.pathname, response.clone()); } catch (_) { /* Keep a valid network response. */ }
    }
    return response;
  })());
});
