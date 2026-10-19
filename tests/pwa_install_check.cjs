"use strict";
// Check installation behavior in both browser capability paths without installing anything.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync("web/index.html", "utf8");
const label = html.match(/<button[^>]*id="install-app"[^>]*>([^<]+)<\/button>/)[1];
assert.equal(label, "Установить");

function fixture(installed = false) {
  const handlers = {};
  const nodes = new Map();
  function element(id) {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: id === "install-app", textContent: id === "install-app" ? label : "",
      open: false, disabled: false, events: {},
      addEventListener(type, handler) { this.events[type] = handler; },
      showModal() { this.open = true; }, close() { this.open = false; },
      scrollIntoView() {}
    });
    return nodes.get(id);
  }
  const media = {matches: installed, addEventListener() {}};
  vm.runInNewContext(fs.readFileSync("web/pwa.js", "utf8"), {
    window: {
      matchMedia: () => media,
      addEventListener: (type, handler) => { handlers[type] = handler; },
      isSecureContext: true
    },
    document: {getElementById: element, querySelectorAll: () => []},
    navigator: {onLine: true, standalone: false},
    MutationObserver: class { observe() {} }
  });
  return {handlers, element};
}

(async () => {
  const manual = fixture();
  assert.equal(manual.element("install-app").hidden, false);
  assert.equal(manual.element("install-app").textContent, label);
  await manual.element("install-app").events.click();
  assert.equal(manual.element("install-dialog").open, true);
  manual.element("close-install").events.click();
  assert.equal(manual.element("install-dialog").open, false);

  const native = fixture();
  let prevented = false, prompted = 0;
  native.handlers.beforeinstallprompt({
    preventDefault() { prevented = true; },
    async prompt() { prompted++; },
    userChoice: Promise.resolve({outcome: "dismissed"})
  });
  assert.ok(prevented);
  assert.equal(native.element("install-app").textContent, label);
  await native.element("install-app").events.click();
  assert.equal(prompted, 1);
  assert.equal(native.element("install-dialog").open, false);
  assert.equal(native.element("install-app").hidden, false, "Dismissal must not report installation");
  await native.element("install-app").events.click();
  assert.equal(native.element("install-dialog").open, true, "Used prompt falls back to manual help");
  native.handlers.appinstalled();
  assert.equal(native.element("install-app").hidden, true);
  assert.equal(native.element("install-dialog").open, false);
  assert.equal(fixture(true).element("install-app").hidden, true);
  console.log("PWA install: one label; native prompt, dismissal, manual instructions and installed state verified.");
})().catch(error => { console.error(error); process.exitCode = 1; });
