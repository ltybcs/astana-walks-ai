/* No tile requests: exercise privacy and degraded-map behavior with Leaflet doubles. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/map.js"), "utf8");

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attributes = {}; this.handlers = {};
    this.textContent = ""; this.className = ""; this.hidden = false;
    this.classList = {
      add: value => { this.className += " " + value; },
      toggle: (value, enabled) => { this.toggles ||= {}; this.toggles[value] = enabled; }
    };
  }
  append(...children) { this.children.push(...children); }
  after(element) { this.afterElement = element; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  click() { if (!this.disabled) this.handlers.click?.(); }
}

function environment(withLeaflet = true) {
  const container = new Element("div");
  const groups = [], maps = [], tiles = [];
  function shape(type, points, options) {
    return {type, points, options,
      addTo(layer) { layer.items.push(this); return this; },
      bindPopup(content, options) { this.popup = content; this.popupOptions = options; return this; },
      bindTooltip(content) { this.tooltip = content; return this; }
    };
  }
  const L = {
    map(element, options) {
      const map = {element, options, fits: [], views: [], events: {},
        attributionControl: {setPrefix(value) { this.prefix = value; }},
        setView(center, zoom) { this.center = {lat:center[0],lng:center[1]}; this.views.push({center,zoom}); return this; },
        getCenter() { return this.center; },
        getBounds() { return {pad() { return this; }, contains(point) { return Math.abs(point[0] - map.center.lat) < 0.3 && Math.abs(point[1] - map.center.lng) < 0.6; }}; },
        fitBounds(bounds, options) { this.fits.push({bounds,options}); },
        on(name, callback) { this.events[name] = callback; },
        invalidateSize() {}
      };
      maps.push(map); return map;
    },
    layerGroup() {
      const group = {items: [], clearLayers() { this.items = []; }, addTo() { return this; }};
      groups.push(group); return group;
    },
    tileLayer(url, options) {
      const layer = {url, options, events: {}, redraws: 0,
        on(name, callback) { this.events[name] = callback; return this; },
        addTo() { this.events.loading?.(); return this; },
        redraw() { this.redraws++; },
        emit(name, data) { this.events[name]?.(data); }
      };
      tiles.push(layer); return layer;
    },
    latLngBounds: points => points,
    circleMarker: (point, options) => shape("candidate", point, options),
    marker: (point, options) => shape("marker", point, options),
    polyline: (points, options) => shape("route", points, options),
    divIcon: options => options
  };
  const window = {addEventListener() {}};
  if (withLeaflet) window.L = L;
  const context = {window, document: {createElement: tag => new Element(tag), getElementById: () => container},
    navigator: {onLine:true}, URL, setTimeout: () => 1, clearTimeout() {}, L: withLeaflet ? L : undefined};
  vm.runInNewContext(source, context);
  return {api:window.AstanaMap, container, groups, maps, tiles, context,
    control: name => container.afterElement.children.find(child => child.className === name)};
}

const env = environment();
const origin = {lat:51.24,lon:71.8,label:"Личный старт"};
const points = [
  {id:"one",lat:51.14,lon:71.45,name:"<img src=x onerror=alert(1)>",source_url:"javascript:alert(1)"},
  {id:"two",lat:51.12,lon:71.41,name:"Парк",source_url:"https://www.openstreetmap.org/node/2"},
  {id:"three",lat:51.15,lon:71.44,name:"Музей"}
];
env.api.update({origin,places:points,recommendations:[points[1],points[0]]});
const map = env.maps[0], route = env.groups[1], tile = env.tiles[0];
assert.deepEqual(JSON.parse(JSON.stringify(map.views)), [{center:[51.13,71.43],zoom:12}]);
assert.equal(map.fits.length, 0, "Private origin must not move external tile viewport before consent");
assert.equal(map.options.preferCanvas, true);
assert.equal(tile.url, "https://tile.openstreetmap.org/{z}/{x}/{y}.png");
assert.equal(tile.options.referrerPolicy, "strict-origin-when-cross-origin");
assert.equal(tile.options.detectRetina, false);
assert.equal(tile.options.updateWhenIdle, true);
assert.equal(tile.options.updateWhenZooming, false);
assert.match(tile.options.attribution, /openstreetmap.org\/copyright/);
const legs = route.items.find(item => item.type === "route");
assert.deepEqual(JSON.parse(JSON.stringify(legs.points)), [[51.24,71.8],[51.12,71.41],[51.14,71.45],[51.24,71.8]], "Preserve stop order and return leg");
const selected = route.items.filter(item => item.type === "marker");
assert.deepEqual(selected.map(item => item.options.icon.html.textContent), ["1","2","С"]);
for (const marker of selected) {
  assert.equal(marker.options.autoPanOnFocus, false, "Keyboard focus must not move private viewport");
  assert.equal(marker.popupOptions.autoPan, false, "Opening marker popup must not move private viewport");
}
assert.equal(env.groups[0].items[0].popupOptions.autoPan, false);
assert.equal(selected[1].popup.children[1].textContent, points[0].name, "Untrusted name remains text");
assert.equal(selected[1].popup.children.some(child => child.tagName === "a"), false, "Unsafe source URL must not become a link");

const failed = {};
tile.emit("tileerror", {tile:failed});
assert.match(env.control("street-map-status").textContent, /подложка недоступна/);
assert.equal(env.container.toggles["street-map--unavailable"], true);
assert.equal(route.items.length, 4, "Tile failure must preserve route/start markers");
const successful = {};
tile.emit("tileload", {tile:successful});
assert.match(env.control("street-map-status").textContent, /Часть/);
tile.emit("tileunload", {tile:failed}); tile.emit("load");
assert.match(env.control("street-map-status").textContent, /не показывают реальные проходы/);
env.context.navigator.onLine = false; tile.emit("load");
assert.match(env.control("street-map-status").textContent, /Нет сети/);
env.context.navigator.onLine = true;
env.control("street-map-retry").click();
assert.equal(tile.redraws, 1, "Only explicit retry redraws tiles");

env.control("street-map-focus").click();
assert.equal(map.fits.length, 1, "Explicit informed click can focus personal origin");
env.api.update({origin,places:points,recommendations:[points[0]]});
assert.equal(map.fits.length, 2, "Current origin consent covers updated route");
env.api.update({origin:{...origin,lat:51.25},places:points,recommendations:points});
assert.equal(map.fits.length, 2, "Changed private origin needs a new click");
env.control("street-map-focus").click(); assert.equal(map.fits.length, 3);
env.api.update({origin:{lat:51.1283,lon:71.4305},places:points,focusAllowed:true});
assert.equal(map.fits.length, 4, "Caller can focus public preset");
env.api.update({origin,places:points}); assert.equal(map.fits.length, 4);

const many = Array.from({length:1510}, (_, i) => ({id:String(i),lat:51.13,lon:71.43,name:"Место " + i}));
env.api.update({origin:{lat:NaN,lon:71},places:many});
assert.equal(env.control("street-map-focus").disabled, true);
assert.equal(env.groups[0].items.length, 1500, "Visible candidate rendering is bounded");
assert.match(env.control("street-map-count").textContent, /1500 из 1510/);
const missing = environment(false);
missing.api.update({origin});
assert.match(missing.container.textContent, /Карта недоступна/);
assert.equal(missing.tiles.length, 0);
console.log("Map checks passed: consent isolation, ordered route, safe POI content, tile failure, bounded rendering.");
