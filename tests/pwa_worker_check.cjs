"use strict";
// Exercise the service-worker boundary without a browser or external network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const origin = "https://walks.example";
const handlers = {};
const entries = new Map();
const cacheWrites = [];
const networkRequests = [];
let networkFails = false;
let claimed = false;
const cache = {
  async addAll(requests) {
    for (const request of requests) {
      const url = new URL(request.url);
      assert.equal(url.origin, origin);
      assert.equal(url.search, "");
      assert.ok(!url.pathname.startsWith("/api/"));
      entries.set(url.pathname, new Response("saved-shell:" + url.pathname));
    }
  },
  async put(path, response) {
    cacheWrites.push(path);
    entries.set(path, response);
  },
  async match(path) { return entries.get(path)?.clone(); }
};
vm.runInNewContext(fs.readFileSync("web/sw.js", "utf8"), {
  self: {location: {origin}, clients: {claim: async () => { claimed = true; }},
    addEventListener: (type, handler) => { handlers[type] = handler; }},
  caches: {open: async () => cache, keys: async () => ["unrelated-app-cache", "astana-walks-shell-v1"],
    delete: async key => { assert.notEqual(key, "unrelated-app-cache"); }},
  URL, Response,
  Request: class extends Request {
    constructor(path, options) { super(new URL(path, origin), options); }
  },
  fetch: async request => {
    networkRequests.push(request);
    if (networkFails) throw new Error("Offline");
    return new Response("network-only", {headers: {"Cache-Control": "no-store"}});
  }
});
async function lifecycle(type) {
  let pending;
  handlers[type]({waitUntil: promise => { pending = promise; }});
  await pending;
}
function request(path, method = "GET") {
  let pending;
  const value = new Request(new URL(path, origin), {method});
  handlers.fetch({request: value, respondWith: promise => { pending = promise; }});
  return {value, pending};
}
(async () => {
  await lifecycle("install");
  await lifecycle("activate");
  assert.ok(claimed);
  assert.ok(entries.has("/index.html"));
  for (const path of ["/api/places", "/api/health", "/api/recommend"]) {
    const call = request(path, path.endsWith("recommend") ? "POST" : "GET");
    const response = await call.pending;
    assert.equal(await response.text(), "network-only");
    assert.equal(networkRequests.at(-1), call.value);
    assert.ok(!entries.has(path));
  }
  assert.equal(cacheWrites.length, 0);
  const shell = request("/styles.css");
  assert.equal(await (await shell.pending).text(), "network-only");
  assert.deepEqual(cacheWrites, ["/styles.css"]);
  networkFails = true;
  const offline = await request("/").pending;
  assert.equal(await offline.text(), "saved-shell:/");
  const offlineApi = await request("/api/recommend", "POST").pending;
  assert.equal(offlineApi.status, 503);
  assert.equal(offlineApi.headers.get("Cache-Control"), "no-store");
  const error = await offlineApi.json();
  assert.ok(error.error);
  assert.equal(error.recommendations, undefined);
  for (const [path, method] of [["/.env", "GET"], ["/?private=example", "GET"],
    ["/index.html?private=example", "GET"], ["/styles.css", "POST"],
    ["https://other.example/styles.css", "GET"]]) {
    assert.equal(request(path, method).pending, undefined, path);
  }
  assert.ok([...entries.keys()].every(path => !path.includes("?") && !path.startsWith("/api/")));
  console.log("PWA: public shell works offline; API stays network-only; unknown/private paths bypass cache.");
})().catch(error => { console.error(error); process.exitCode = 1; });
