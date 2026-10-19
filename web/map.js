/* Street tiles are external; POI, start markers and route overlays stay local. */
"use strict";
(() => {
  const CITY_CENTER = [51.13, 71.43];
  const MAX_VISIBLE_PLACES = 1500;
  const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  let state;

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function valid(point) {
    return point && Number.isFinite(point.lat) && Number.isFinite(point.lon) &&
      Math.abs(point.lat) <= 90 && Math.abs(point.lon) <= 180;
  }
  const latLng = point => [point.lat, point.lon];
  const originKey = point => valid(point) ? `${point.lat},${point.lon}` : "";
  function popup(point, heading) {
    const content = node("div", "street-map-popup");
    content.append(node("strong", "", heading || point.name || "Место"));
    if (point.name && heading) content.append(node("span", "", point.name));
    if (point.category) content.append(node("span", "", "Категория POI: " + point.category));
    try {
      const url = new URL(point.source_url);
      if (url.protocol === "https:") {
        const link = node("a", "", "Источник места ↗");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        content.append(link);
      }
    } catch { /* A missing or unsafe source is not made clickable. */ }
    return content;
  }
  function numberedIcon(label, kind) {
    return L.divIcon({
      className: "street-map-marker street-map-marker--" + kind,
      html: node("span", "", label), iconSize: [30, 30], iconAnchor: [15, 15],
      popupAnchor: [0, -17]
    });
  }
  function refreshTileStatus() {
    const {status, container, loadedTiles, failedTiles} = state;
    const offline = navigator.onLine === false;
    const unavailable = offline || (!loadedTiles.size && (failedTiles.size || state.timedOut));
    container.classList.toggle("street-map--unavailable", Boolean(unavailable));
    if (offline) status.textContent = "Нет сети: уличная подложка может быть недоступна. Точки мест и список рекомендаций остаются доступны.";
    else if (unavailable) status.textContent = "Уличная подложка недоступна. На пустом фоне показаны только точки мест и прямые отрезки маршрута; список рекомендаций доступен ниже.";
    else if (failedTiles.size) status.textContent = "Часть уличной подложки не загрузилась. Точки мест и список рекомендаций остаются доступны.";
    else if (loadedTiles.size) status.textContent = "Уличная карта OpenStreetMap. Линии соединяют остановки по прямой и не показывают реальные проходы.";
    else status.textContent = "Загружаем уличную подложку OpenStreetMap…";
    state.retry.hidden = !unavailable && !failedTiles.size;
  }
  function renderCandidates() {
    if (!state) return;
    const {map, candidates, places, recommendations} = state;
    candidates.clearLayers();
    const bounds = map.getBounds().pad(0.1);
    const center = map.getCenter();
    const selected = new Set(recommendations.map(point => point.id));
    const visible = places.filter(point => valid(point) && !selected.has(point.id) && bounds.contains(latLng(point)));
    const shown = visible.sort((a, b) =>
      Math.hypot(a.lat - center.lat, (a.lon - center.lng) * 0.63) -
      Math.hypot(b.lat - center.lat, (b.lon - center.lng) * 0.63)
    ).slice(0, MAX_VISIBLE_PLACES);
    for (const point of shown) {
      const marker = L.circleMarker(latLng(point), {
        radius: 5, color: "#ffffff", weight: 1, fillColor: "#526f68", fillOpacity: 0.85
      });
      marker.bindPopup(popup(point), {maxWidth: 260, autoPan: false});
      marker.bindTooltip(node("span", "", point.name || "Место"), {direction: "top"});
      marker.addTo(candidates);
    }
    state.count.textContent = shown.length < visible.length ?
      `В этой области показано ${shown.length} из ${visible.length} мест. Приблизьте карту, чтобы увидеть остальные.` :
      `Мест в видимой области: ${shown.length}. Номера остановок совпадают с карточками маршрута.`;
  }
  function focusRoute() {
    const points = [state.origin, ...state.recommendations].filter(valid);
    if (!points.length) return;
    state.map.fitBounds(L.latLngBounds(points.map(latLng)), {padding: [42, 42], maxZoom: 16, animate: false});
  }
  function initialize() {
    const container = document.getElementById("city-map");
    if (!container) return false;
    container.setAttribute("role", "region");
    container.setAttribute("aria-label", "Уличная карта Астаны, места и приблизительный маршрут");
    if (!window.L) {
      container.classList.add("street-map-unavailable");
      container.textContent = "Карта недоступна: не загрузилась локальная библиотека. Список рекомендаций находится ниже.";
      return false;
    }
    const tools = node("div", "street-map-tools");
    const focus = node("button", "street-map-focus", "Показать начало и маршрут");
    focus.type = "button";
    focus.setAttribute("aria-describedby", "map-privacy");
    const privacy = node("p", "street-map-privacy", "Карта: OpenStreetMap. Внешний сервис получает IP и область просмотра. Кнопка выше приблизит карту к выбранному началу, включая геолокацию, если вы её указали.");
    privacy.id = "map-privacy";
    const status = node("p", "street-map-status");
    status.setAttribute("role", "status");
    const count = node("p", "street-map-count");
    const retry = node("button", "street-map-retry", "Повторить загрузку подложки");
    retry.type = "button";
    retry.hidden = true;
    tools.append(focus, privacy, status, retry, count);
    container.after(tools);
    if (L.Control?.Zoom) L.Control.Zoom.mergeOptions({zoomInTitle: "Приблизить карту", zoomOutTitle: "Отдалить карту"});
    const map = L.map(container, {preferCanvas: true, scrollWheelZoom: false, minZoom: 3, maxZoom: 19});
    map.setView(CITY_CENTER, 12);
    map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener noreferrer">Leaflet</a>');
    const candidates = L.layerGroup().addTo(map);
    const route = L.layerGroup().addTo(map);
    state = {container, map, candidates, route, focus, privacy, status, count, retry,
      places: [], recommendations: [], origin: null, originKey: "", consentOriginKey: "",
      focusSignature: "", loadedTiles: new Set(), failedTiles: new Set(), timedOut: false};
    const tiles = L.tileLayer(TILE_URL, {
      maxZoom: 19, noWrap: true, updateWhenIdle: true, updateWhenZooming: false,
      keepBuffer: 1, detectRetina: false, referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>'
    });
    state.tiles = tiles;
    tiles.on("tileload", event => {
      state.loadedTiles.add(event.tile); state.failedTiles.delete(event.tile); refreshTileStatus();
    });
    tiles.on("tileerror", event => {
      state.failedTiles.add(event.tile); state.loadedTiles.delete(event.tile); refreshTileStatus();
    });
    tiles.on("tileunload", event => {
      state.loadedTiles.delete(event.tile); state.failedTiles.delete(event.tile);
    });
    tiles.on("loading", () => {
      state.timedOut = false; refreshTileStatus();
      clearTimeout(state.loadingTimer);
      state.loadingTimer = setTimeout(() => {state.timedOut = true; refreshTileStatus();}, 12000);
    });
    tiles.on("load", () => {clearTimeout(state.loadingTimer); refreshTileStatus();});
    tiles.addTo(map);
    focus.addEventListener("click", () => {
      if (!valid(state.origin)) return;
      state.consentOriginKey = state.originKey;
      focusRoute();
    });
    retry.addEventListener("click", () => {
      state.loadedTiles.clear(); state.failedTiles.clear(); state.timedOut = false;
      tiles.redraw(); refreshTileStatus();
    });
    map.on("moveend", renderCandidates);
    window.addEventListener("offline", refreshTileStatus);
    window.addEventListener("online", refreshTileStatus);
    if (window.ResizeObserver) {
      state.observer = new ResizeObserver(() => map.invalidateSize({pan: false}));
      state.observer.observe(container);
    }
    refreshTileStatus();
    return true;
  }
  function update({origin, places = [], recommendations = [], focusAllowed = false} = {}) {
    if (!state && !initialize()) return;
    const key = originKey(origin);
    if (key !== state.originKey) state.consentOriginKey = "";
    Object.assign(state, {origin, originKey: key, places, recommendations});
    state.focus.disabled = !valid(origin);
    state.route.clearLayers();
    const selected = recommendations.map((point, index) => ({point, index})).filter(({point}) => valid(point));
    if (valid(origin) && selected.length) {
      L.polyline([latLng(origin), ...selected.map(({point}) => latLng(point)), latLng(origin)], {
        color: "#b74429", weight: 3, opacity: 0.85, dashArray: "7 7", interactive: false
      }).addTo(state.route);
    }
    selected.forEach(({point, index}) => {
      L.marker(latLng(point), {
        icon: numberedIcon(String(index + 1), "selected"), title: `${index + 1}. ${point.name || "Остановка"}`,
        alt: `Остановка ${index + 1}`, zIndexOffset: 500, autoPanOnFocus: false
      }).bindPopup(popup(point, `Остановка ${index + 1}`), {maxWidth: 260, autoPan: false}).addTo(state.route);
    });
    if (valid(origin)) {
      L.marker(latLng(origin), {
        icon: numberedIcon("С", "origin"), title: "Начало и конец прогулки", alt: "Начало и конец прогулки", zIndexOffset: 1000, autoPanOnFocus: false
      }).bindPopup(popup({name: origin.label}, "Начало и конец прогулки"), {maxWidth: 260, autoPan: false}).addTo(state.route);
    }
    const signature = JSON.stringify([key, ...selected.map(({point}) => [point.id, point.lat, point.lon])]);
    if ((focusAllowed || (key && state.consentOriginKey === key)) && signature !== state.focusSignature) {
      focusRoute(); state.focusSignature = signature;
    }
    renderCandidates();
  }
  window.AstanaMap = Object.freeze({update});
})();
