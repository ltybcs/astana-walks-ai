"use strict";

const $ = id => document.getElementById(id);
const state = {
  requestId: 0, controller: null, signature: "", result: null, optionId: null,
  mapId: 0, mapController: null, mapTimer: null, mapLoadTimer: null, geoCancel: null
};
let contextPreparationCapability = null;
const PROVIDERS = {osm: "OpenStreetMap", google: "Google Maps", dgis: "2ГИС"};
const ORIGIN_SOURCES = {
  query_landmark: "из сообщения", query_catalog: "из сообщения",
  google_places_text_search: "найдено в Google Maps",
  browser_geolocation: "геопозиция браузера", explicit_landmark: "из сообщения",
  explicit_coordinates: "из сообщения"
};
function node(tag, text, className) {
  const value = document.createElement(tag);
  if (text !== undefined && text !== null) value.textContent = String(text);
  if (className) value.className = className;
  return value;
}
function show(id, visible) { $(id).hidden = !visible; }
function finite(value) { return typeof value === "number" && Number.isFinite(value); }
function coordinate(lat, lon) {
  return finite(lat) && finite(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
}
function signature() { return $("walk-query").value; }
function current(id, sig) { return state.requestId === id && signature() === sig; }
function safeUrl(value, provider) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.username || url.password || url.port) return null;
    if (provider === "dgis-attribution" && url.protocol === "http:" && url.hostname === "dev.2gis.ru" && url.pathname === "/") return url.href;
    if (url.protocol !== "https:") return null;
    if (provider === "origin-attribution") return url.href;
    const hosts = {
      google: ["www.google.com", "maps.google.com", "maps.app.goo.gl"],
      osm: ["www.openstreetmap.org", "openstreetmap.org"],
      dgis: ["2gis.kz", "2gis.ru", "2gis.com", "www.2gis.kz", "www.2gis.ru", "www.2gis.com", "dev.2gis.ru"]
    };
    if (!(hosts[provider] || []).includes(url.hostname)) return null;
    if (provider === "google" && url.hostname === "www.google.com" && !url.pathname.startsWith("/maps")) return null;
    return url.href;
  } catch (_) { return null; }
}
function link(text, url, provider) {
  const safe = safeUrl(url, provider);
  if (!safe) return node("span", text);
  const element = node("a", text);
  element.href = safe;
  element.target = "_blank";
  element.rel = "noopener noreferrer";
  return element;
}
function clearMap() {
  state.mapId += 1;
  if (state.mapController) state.mapController.abort();
  state.mapController = null;
  clearTimeout(state.mapTimer);
  clearTimeout(state.mapLoadTimer);
  state.mapTimer = null;
  state.mapLoadTimer = null;
  $("route-map-host").replaceChildren();
  $("route-map-status").textContent = "";
  $("route-external").removeAttribute("href");
  show("route-external", false);
  show("route-map-section", false);
}
function clearResult() {
  clearMap();
  state.result = null;
  state.optionId = null;
  show("walk-result", false);
  for (const id of ["source-receipts", "google-sources", "google-original-sources", "walk-warnings", "walk-variants", "walk-stops", "other-sources", "route-attributions"]) $(id).replaceChildren();
  $("walk-answer").textContent = "";
  $("google-original-text").textContent = "";
  $("walk-context").textContent = "";
  $("walk-budget").textContent = "";
  show("walk-warnings", false);
  $("walk-explanation").open = false;
  $("amenity-fallback-note").textContent = "";
  show("amenity-fallback-note", false);
}
function cancelFlow(message = "Сообщение изменилось. Подбор можно запустить снова.") {
  state.requestId += 1;
  if (state.controller) state.controller.abort();
  state.controller = null;
  if (state.geoCancel) state.geoCancel();
  state.geoCancel = null;
  clearResult();
  $("walk-submit").disabled = false;
  $("walk-submit").textContent = "Найти прогулку →";
  show("walk-error", false);
  $("walk-status").textContent = message;
}
function onInputChange() {
  const next = signature();
  if (next === state.signature) return;
  state.signature = next;
  cancelFlow();
}
function errorMessage(error) {
  if (error && error.code === "known_start_required") {
    const reason = typeof error.message === "string" && error.message.trim() ? error.message : "Старт не определён.";
    return reason + " Подбор мест и расчёт маршрута не выполнены.";
  }
  if (error && error.code === "timeout") return "Ожидание завершено. Удалённый запрос мог выполниться и учесться в лимите. Автоматического повтора нет.";
  if (error && error.name === "AbortError") return "Подбор отменён. Удалённый запрос мог выполниться и учесться в лимите.";
  if (error && typeof error.message === "string") return error.message;
  return "Подбор не выполнен. Попробуйте позже.";
}
async function post(path, body, signal) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    credentials: "same-origin", cache: "no-store", redirect: "error",
    signal, body: JSON.stringify(body)
  });
  let data;
  try { data = await response.json(); } catch (_) {
    throw new Error("Сервис вернул нечитаемый ответ. Подбор не выполнен.");
  }
  if (!response.ok) {
    const error = new Error(typeof data.error === "string" ? data.error : "Сервис временно недоступен.");
    error.code = data.code;
    throw error;
  }
  return data;
}
async function contextPreparationEnabled(signal) {
  if (contextPreparationCapability !== null) return contextPreparationCapability;
  const response = await fetch("/api/health", {
    method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error", signal
  });
  let health;
  try { health = await response.json(); } catch (_) {
    throw new Error("Не удалось проверить версию сервиса. Подбор не запущен.");
  }
  if (!response.ok || !health || typeof health !== "object" || Array.isArray(health)) {
    throw new Error("Не удалось проверить версию сервиса. Подбор не запущен.");
  }
  if (signal.aborted) { const error = new Error("Подбор отменён."); error.name = "AbortError"; throw error; }
  // Select once from a successful capability response, never from a token error.
  contextPreparationCapability = health.context_preparation === true;
  return contextPreparationCapability;
}
function startKnown(context) {
  return Boolean(context && context.origin && coordinate(context.origin.lat, context.origin.lon)
    && context.origin.lat >= 51.00 && context.origin.lat <= 51.32
    && context.origin.lon >= 71.20 && context.origin.lon <= 71.70
    && typeof context.origin.label === "string" && context.origin.label.trim()
    && Object.prototype.hasOwnProperty.call(ORIGIN_SOURCES, context.origin_source)
    && !context.is_city_sample && context.origin_source !== "city_sample"
    && context.origin_source !== "pending_geolocation");
}
function preparationToken(probe) {
  if (!probe || typeof probe.preparation_token !== "string" || !probe.preparation_token.trim()) {
    throw new Error("Подготовка запроса не подтверждена. Поиск не запущен.");
  }
  return probe.preparation_token; // Opaque: never parse, normalize, display or persist.
}
function startError(reason) {
  const messages = {
    denied: "Браузер не разрешил доступ к геолокации.",
    timeout: "Браузер не успел определить местоположение за 8 секунд.",
    unsupported: "Этот браузер не поддерживает геолокацию.",
    unavailable: "Браузер не смог определить местоположение."
  };
  const error = new Error(messages[reason] || "Старт не определён.");
  error.code = "known_start_required";
  return error;
}
function requestLocation(id, sig) {
  return new Promise(resolve => {
    let settled = false;
    let timer;
    const finish = value => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (state.geoCancel === cancel) state.geoCancel = null;
      resolve(current(id, sig) ? value : {status: "cancelled"});
    };
    const cancel = () => finish({status: "cancelled"});
    state.geoCancel = cancel;
    if (!navigator.geolocation || typeof navigator.geolocation.getCurrentPosition !== "function") {
      finish({status: "unsupported"});
      return;
    }
    timer = setTimeout(() => finish({status: "timeout"}), 8000);
    try {
      navigator.geolocation.getCurrentPosition(position => {
        const coords = position && position.coords;
        const accuracy = coords && coords.accuracy;
        if (!coords || !coordinate(coords.latitude, coords.longitude) || !finite(accuracy) || accuracy < 0 || accuracy > 100000) {
          finish({status: "unavailable"});
          return;
        }
        finish({status: "granted", geolocation: {lat: coords.latitude, lon: coords.longitude, accuracy}});
      }, error => finish({status: error && error.code === 1 ? "denied" : error && error.code === 3 ? "timeout" : "unavailable"}),
      {enableHighAccuracy: false, timeout: 8000, maximumAge: 0});
    } catch (_) { finish({status: "unavailable"}); }
  });
}
function unconfirmedAmenities(value, allowEmpty = false) {
  return Array.isArray(value) && (allowEmpty || value.length > 0) && value.length <= 3
    && new Set(value).size === value.length && value.every(key => ["socket", "wifi", "quiet"].includes(key));
}
function amenityText(value) {
  return typeof value === "string" && value.trim() && value.length <= 600;
}
function validateAmenityFallback(option) {
  const fallback = option.amenity_fallback;
  if (fallback !== undefined && (!fallback || fallback.applied !== true || !option.route_built
      || !unconfirmedAmenities(fallback.unconfirmed) || !amenityText(fallback.notice))) {
    throw new Error("Не подтверждено примечание о дополнительных пожеланиях.");
  }
  const observed = new Set();
  for (const stop of option.stops) {
    const keys = stop && stop.unconfirmed_amenities;
    const requested = stop && stop.requested_features;
    if (requested !== undefined && (!Array.isArray(requested) || requested.length > 4
        || new Set(requested).size !== requested.length
        || requested.some(key => !["socket", "wifi", "quiet", "wheelchair"].includes(key)))) throw new Error("Некорректные запрошенные свойства места.");
    if (keys !== undefined && !unconfirmedAmenities(keys, true)) throw new Error("Некорректные неподтверждённые свойства места.");
    if (stop && stop.amenity_note !== undefined && !amenityText(stop.amenity_note)) throw new Error("Некорректное примечание о свойствах места.");
    if (keys && keys.length) {
      if (!fallback || !amenityText(stop.amenity_note) || keys.some(key => !fallback.unconfirmed.includes(key))) throw new Error("Не подтверждён выбор без дополнительного условия.");
      for (const key of keys) {
        const feature = stop.features && stop.features[key];
        const evidence = Array.isArray(stop.feature_evidence) && stop.feature_evidence.find(item => item
          && item.property === key && item.provider === stop.provider && item.place_id === stop.id
          && ["yes", "no"].includes(item.value));
        if (["yes", "no", true, false].includes(feature) || evidence) throw new Error("Свойство места не является неизвестным.");
        observed.add(key);
      }
    } else if (stop && stop.amenity_note !== undefined) throw new Error("Примечание не связано с неподтверждённым свойством.");
  }
  if (fallback && fallback.unconfirmed.some(key => !observed.has(key))) throw new Error("Примечание не связано с выбранными местами.");
}
function validateResult(data) {
  if (!data || !["llm-grounded-unified", "grounding-only"].includes(data.mode) || !startKnown(data.resolved_context)) throw new Error("Ответ не соответствует протоколу подбора.");
  if (!data.grounding || typeof data.grounding.summary !== "string" || !Array.isArray(data.grounding.sources)) throw new Error("В ответе отсутствуют источники Google.");
  if (data.answer !== null && (!data.answer || typeof data.answer.text !== "string" || !data.answer.text.trim())) throw new Error("В ответе отсутствует полный текст.");
  if (data.mode === "llm-grounded-unified" && !data.answer) throw new Error("Режим ответа не подтверждён.");
  if (!data.sources || !Array.isArray(data.sources.osm) || !Array.isArray(data.sources.dgis)) throw new Error("В ответе отсутствует происхождение мест.");
  for (const provider of ["osm", "google", "dgis"]) {
    const receipt = data.source_receipt && data.source_receipt[provider];
    const unavailable = receipt && receipt.status === "unavailable";
    const validFailure = unavailable && provider !== "google" && receipt.candidate_count === null
      && receipt.selected_count === 0 && typeof receipt.attempted === "boolean"
      && ["not_configured", "quota_exhausted", "provider_unavailable", "provider_invalid_response", "source_timeout"].includes(receipt.error_code)
      && data.sources[provider].length === 0;
    const validCompleted = receipt && ["searched", "no_data"].includes(receipt.status)
      && Number.isInteger(receipt.candidate_count) && receipt.candidate_count >= 0
      && Number.isInteger(receipt.selected_count) && receipt.selected_count >= 0
      && receipt.selected_count <= receipt.candidate_count
      && (receipt.status !== "no_data" || (receipt.candidate_count === 0 && receipt.selected_count === 0));
    if (!validFailure && !validCompleted) throw new Error("Не подтверждён результат проверки всех трёх источников.");
  }
  const incomplete = ["osm", "dgis"].some(provider => data.source_receipt[provider].status === "unavailable");
  if (data.search_complete !== undefined && (typeof data.search_complete !== "boolean" || data.search_complete === incomplete)) throw new Error("Некорректный статус полноты поиска.");
  if (!Array.isArray(data.alternatives) || !data.alternatives.length || data.alternatives.length > 3) throw new Error("Отсутствуют готовые варианты прогулки.");
  const ids = new Set();
  for (const option of data.alternatives) {
    if (!option || typeof option.id !== "string" || !option.id || ids.has(option.id) || !Number.isInteger(option.budget_minutes) || option.budget_minutes < 10 || option.budget_minutes > 480 || !Array.isArray(option.stops) || option.stops.length > 3 || typeof option.route_built !== "boolean") throw new Error("Некорректный вариант прогулки.");
    ids.add(option.id);
    validateAmenityFallback(option);
    if (option.stops.some(stop => stop && data.source_receipt[stop.provider] && data.source_receipt[stop.provider].status === "unavailable")) throw new Error("Место получено из недоступного источника.");
    if (option.route_built) {
      const plan = option.plan;
      if (option.budget_status !== "fits" || !plan || plan.verified !== true || plan.returns_to_origin !== true || !finite(plan.total_minutes) || plan.total_minutes < 0 || plan.total_minutes > option.budget_minutes || !option.stops.length) throw new Error("Маршрут не подтверждён в пределах времени.");
      for (const stop of option.stops) if (!stop || !["osm", "google", "dgis"].includes(stop.provider) || typeof stop.name !== "string" || !coordinate(stop.lat, stop.lon)) throw new Error("Некорректная остановка маршрута.");
    }
  }
  if (!ids.has(data.default_option_id)) throw new Error("Не определён основной вариант.");
  return data;
}
function contextText(context) {
  const time = {day: "день", evening: "вечер", night: "ночь"}[context.time_of_day] || "текущее время";
  const source = ORIGIN_SOURCES[context.origin_source] || "подтверждённый старт";
  return "Старт: " + context.origin.label + " · " + source + " · " + time;
}
function renderGoogleSources(hostId, sources) {
  const host = $(hostId);
  host.replaceChildren();
  const brand = node("span", "Google Maps", "google-brand");
  brand.setAttribute("translate", "no");
  host.append(brand);
  sources.forEach((source, index) => {
    const item = node("div", null, "google-source");
    item.append(node("span", "[" + index + "]" + (typeof source.source_id === "string" ? " / " + source.source_id : ""), "source-index"));
    const title = typeof source.title === "string" && source.title ? source.title
      : source.attribution && typeof source.attribution.title === "string" ? source.attribution.title : "Google Maps";
    const url = source.url || (source.attribution && source.attribution.url);
    item.append(link(title, url, "google"));
    const attribution = source.attribution;
    if (attribution && typeof attribution.title === "string" && attribution.title !== title) item.append(node("p", attribution.title));
    if (attribution && attribution.url && attribution.url !== url) item.append(link(attribution.title || "Источник Google", attribution.url, "google"));
    host.append(item);
  });
}
function renderReceipts(receipts) {
  const host = $("source-receipts");
  host.replaceChildren();
  for (const provider of ["osm", "google", "dgis"]) {
    const receipt = receipts[provider];
    const item = node("div", null, "source-receipt");
    item.append(node("strong", PROVIDERS[provider]));
    const hasSplit = receipt.status !== "unavailable"
      && Number.isInteger(receipt.strict_candidate_count) && receipt.strict_candidate_count >= 0
      && Number.isInteger(receipt.alternative_candidate_count) && receipt.alternative_candidate_count >= 0
      && receipt.strict_candidate_count + receipt.alternative_candidate_count === receipt.candidate_count;
    item.append(node("span", receipt.status === "unavailable" ? (receipt.attempted ? "Источник не ответил" : "Источник недоступен")
      : receipt.status === "no_data" ? "Подходящих данных нет"
      : hasSplit ? "С подтверждением: " + receipt.strict_candidate_count + " · без подтверждения: " + receipt.alternative_candidate_count + " · в плане: " + receipt.selected_count
      : "Подходит: " + receipt.candidate_count + " · в плане: " + receipt.selected_count));
    host.append(item);
  }
}
function renderOtherSources(sources, receipts) {
  const host = $("other-sources");
  host.replaceChildren();
  for (const provider of ["osm", "dgis"]) {
    host.append(node("h3", PROVIDERS[provider]));
    const list = node("ul", null, "other-source-list");
    sources[provider].forEach(source => {
      const item = node("li");
      if (typeof source.source_id === "string") item.append(node("span", "[" + source.source_id + "] "));
      item.append(link(source.name || source.id || "Место", source.source_url, provider));
      if (source.category) item.append(node("span", " · " + source.category));
      list.append(item);
    });
    if (!sources[provider].length) list.append(node("li", receipts[provider].status === "unavailable" ? "Источник недоступен; его данные не использованы." : "Подходящих данных нет."));
    host.append(list);
  }
}
function renderResult(data) {
  state.result = data;
  const incomplete = ["osm", "dgis"].some(provider => data.source_receipt[provider].status === "unavailable");
  $("walk-mode").textContent = data.mode === "llm-grounded-unified"
    ? (incomplete ? "ИИ · неполный поиск" : "ИИ · три источника проверены")
    : (incomplete ? "Неполный поиск · без ответа модели" : "Без ответа модели");
  $("walk-context").textContent = contextText(data.resolved_context);
  if (data.resolved_context.origin_source === "google_places_text_search"
      && safeUrl(data.resolved_context.origin_reference, "google")) {
    $("walk-context").append(node("span", " · "), link("Источник старта ↗", data.resolved_context.origin_reference, "google"));
  }
  const attributions = data.resolved_context.origin_attributions;
  if (Array.isArray(attributions)) attributions.slice(0, 8).forEach(value => {
    if (!value || typeof value.provider !== "string" || !value.provider.trim() || value.provider.length > 300) return;
    $("walk-context").append(node("span", " · "), link(value.provider, value.providerUri, "origin-attribution"));
  });
  renderReceipts(data.source_receipt);
  $("answer-label").textContent = data.answer ? "Ответ помощника" : "Полный ответ Google · без генерации модели";
  $("walk-answer").textContent = data.answer ? data.answer.text : data.grounding.summary;
  renderGoogleSources("google-sources", data.grounding.sources);
  show("google-original", Boolean(data.answer));
  $("google-original-text").textContent = data.grounding.summary;
  renderGoogleSources("google-original-sources", data.grounding.sources);
  const warnings = $("walk-warnings");
  warnings.replaceChildren();
  const notes = [...(Array.isArray(data.warnings) ? data.warnings : []),
                 ...(Array.isArray(data.resolved_context.assumptions) ? data.resolved_context.assumptions : [])];
  [...new Set(notes.filter(value => typeof value === "string"))].forEach(value => warnings.append(node("li", value)));
  show("walk-warnings", warnings.children.length > 0);
  renderOtherSources(data.sources, data.source_receipt);
  const variants = $("walk-variants");
  variants.replaceChildren();
  data.alternatives.forEach(option => {
    const button = node("button", option.budget_minutes + " мин");
    button.type = "button";
    button.dataset.optionId = option.id;
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => selectOption(option.id));
    variants.append(button);
  });
  show("walk-variants", data.alternatives.length > 1);
  show("walk-result", true);
  selectOption(data.default_option_id);
}
function renderRouteAttributions(attributions) {
  const host = $("route-attributions");
  host.replaceChildren();
  if (!Array.isArray(attributions)) return;
  attributions.forEach(value => {
    if (typeof value === "string") host.append(node("span", value));
    else if (value && typeof value.title === "string") host.append(link(value.title, value.url, "google"));
  });
}
function directionsUrl(context, stops) {
  if (!startKnown(context) || !stops.length || stops.length > 3) return null;
  const origin = context.origin.lat + "," + context.origin.lon;
  const url = new URL("https://www.google.com/maps/dir/");
  url.searchParams.set("api", "1");
  url.searchParams.set("origin", origin);
  url.searchParams.set("destination", origin);
  url.searchParams.set("travelmode", "walking");
  url.searchParams.set("waypoints", stops.map(stop => stop.lat + "," + stop.lon).join("|"));
  return url.href;
}
function validateMapUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.hostname !== "www.google.com" || url.port || url.username || url.password || url.pathname !== "/maps/embed/v1/directions" || url.hash) return null;
    const allowed = new Set(["key", "origin", "destination", "waypoints", "mode", "language", "region", "avoid", "units"]);
    for (const key of url.searchParams.keys()) if (!allowed.has(key) || url.searchParams.getAll(key).length !== 1) return null;
    if (!url.searchParams.get("key") || !url.searchParams.get("origin") || url.searchParams.get("origin") !== url.searchParams.get("destination") || url.searchParams.get("mode") !== "walking") return null;
    const stops = (url.searchParams.get("waypoints") || "").split("|").filter(Boolean);
    return stops.length >= 1 && stops.length <= 3 ? url.href : null;
  } catch (_) { return null; }
}
async function loadRouteMap(option, context) {
  clearMap();
  if (!option.route_built) return;
  show("route-map-section", true);
  const external = directionsUrl(context, option.stops);
  if (external) { $("route-external").href = external; show("route-external", true); }
  if (typeof option.map_ticket !== "string" || !option.map_ticket) {
    $("route-map-status").textContent = "Карта маршрута пока недоступна. План и ссылка на Google Maps остаются доступны.";
    return;
  }
  const mapId = state.mapId;
  const requestId = state.requestId;
  const controller = new AbortController();
  state.mapController = controller;
  $("route-map-status").textContent = "Загружаем единственную карту выбранного маршрута…";
  const timer = setTimeout(() => controller.abort(), 20000);
  const active = () => mapId === state.mapId && requestId === state.requestId && state.optionId === option.id;
  try {
    const data = await post("/api/unified/map", {ticket: option.map_ticket}, controller.signal);
    if (!active()) return;
    const url = validateMapUrl(data.url);
    if (!url || !finite(data.expires_in) || data.expires_in <= 0 || data.expires_in > 600) throw new Error("Неподтверждённый адрес карты.");
    const iframe = node("iframe");
    iframe.title = "Пеший маршрут Google Maps с возвращением";
    iframe.setAttribute("referrerpolicy", "origin");
    iframe.setAttribute("allowfullscreen", "");
    iframe.addEventListener("load", () => {
      if (!active()) return;
      clearTimeout(state.mapLoadTimer);
      $("route-map-status").textContent = "Карта Google Maps загружена. Если маршрут не отображается, откройте ссылку ниже.";
    });
    iframe.addEventListener("error", () => {
      if (!active()) return;
      clearTimeout(state.mapLoadTimer);
      $("route-map-status").textContent = "Карта не загрузилась. Откройте маршрут в Google Maps по ссылке ниже.";
    });
    iframe.src = url;
    $("route-map-host").replaceChildren(iframe);
    state.mapLoadTimer = setTimeout(() => {
      if (active()) $("route-map-status").textContent = "Карта отвечает медленно. План доступен; маршрут можно открыть по ссылке ниже.";
    }, 12000);
    state.mapTimer = setTimeout(() => {
      if (!active()) return;
      $("route-map-host").replaceChildren();
      $("route-map-status").textContent = "Срок показа карты завершён. Откройте маршрут по ссылке ниже.";
    }, data.expires_in * 1000);
  } catch (_) {
    if (active()) $("route-map-status").textContent = "Карта маршрута недоступна. План и ссылка на Google Maps остаются доступны.";
  } finally {
    clearTimeout(timer);
    if (active()) state.mapController = null;
  }
}
function selectOption(id) {
  if (!state.result) return;
  const option = state.result.alternatives.find(value => value.id === id);
  if (!option || state.optionId === id) return;
  state.optionId = id;
  for (const button of $("walk-variants").children) button.setAttribute("aria-pressed", String(button.dataset.optionId === id));
  const plan = option.plan || {};
  $("walk-budget").textContent = option.route_built
    ? plan.total_minutes + " из " + option.budget_minutes + " мин · пешком " + plan.walk_minutes + " · остановки " + plan.visit_minutes + " · запас " + plan.buffer_minutes
    : "На " + option.budget_minutes + " мин подтверждённый маршрут не построен.";
  $("walk-route-note").textContent = option.route_built
    ? "Возвращение к старту включено: " + plan.return_walk_minutes + " мин. Пеший путь рассчитан Google; время посещения и запас предложены приложением."
    : "Подходящих остановок или подтверждённого времени недостаточно. Это результат проверки источников, а не готовая рекомендация.";
  const fallback = option.route_built && option.amenity_fallback && option.amenity_fallback.applied === true
    ? option.amenity_fallback : null;
  $("amenity-fallback-note").textContent = fallback ? fallback.notice : "";
  show("amenity-fallback-note", Boolean(fallback));
  renderRouteAttributions(plan.route_attributions);
  const host = $("walk-stops");
  host.replaceChildren();
  if (option.route_built) option.stops.forEach(stop => {
    const item = node("li");
    const content = node("div", null, "stop-content");
    content.append(node("strong", stop.name));
    const availabilityNote = typeof stop.availability_note === "string" && stop.availability_note.trim()
      && stop.availability_note.length <= 600 ? stop.availability_note : null;
    let displayDescription = typeof stop.user_description === "string" && stop.user_description.length <= 600
      ? stop.user_description : null;
    let separatedAvailabilityNote = false;
    if (displayDescription && availabilityNote) {
      // Older clients need the compatibility suffix in the response. Only this
      // exact suffix is split for display; the original response stays intact.
      const suffix = " Примечание приложения: " + availabilityNote;
      if (displayDescription.endsWith(suffix)) {
        displayDescription = displayDescription.slice(0, -suffix.length);
        separatedAvailabilityNote = true;
      }
    }
    if (displayDescription && displayDescription.trim()) {
      const originLabel = stop.description_origin === "source-template" ? "Идея приложения"
        : (stop.description_origin === "local-model"
          || stop.description_origin === "local-model-with-note" && separatedAvailabilityNote) ? "Описание ИИ" : null;
      if (originLabel) content.append(node("p", originLabel, "stop-description-origin"));
      content.append(node("p", displayDescription, "stop-description"));
    }
    if (availabilityNote) content.append(node("p", availabilityNote, "stop-availability-note"));
    if (Array.isArray(stop.unconfirmed_amenities) && stop.unconfirmed_amenities.length && amenityText(stop.amenity_note)) {
      content.append(node("p", stop.amenity_note, "stop-amenity-note"));
    }
    const details = node("details", null, "stop-details");
    details.append(node("summary", "Почему выбрано и данные места"));
    if (typeof stop.why_text === "string" && stop.why_text.trim()) details.append(node("p", stop.why_text, "stop-why"));
    const facts = node("div", null, "stop-source-facts");
    const propertyLabels = {socket:"Розетки", wifi:"Wi-Fi", quiet:"Тишина", wheelchair:"Доступный вход"};
    const values = {yes:"указано в источнике", no:"в источнике указано отсутствие", unknown:"неизвестно"};
    const requested = new Set([...(Array.isArray(stop.requested_features) ? stop.requested_features : []),
      ...(Array.isArray(stop.unconfirmed_amenities) ? stop.unconfirmed_amenities : [])]);
    for (const [key, label] of Object.entries(propertyLabels)) {
      const evidence = Array.isArray(stop.feature_evidence) && stop.feature_evidence.find(item => item
        && item.property === key && item.provider === stop.provider && item.place_id === stop.id
        && ["yes", "no", "unknown"].includes(item.value));
      const value = evidence ? evidence.value : "unknown";
      if (value !== "unknown" || requested.has(key)) facts.append(node("p", label + ": " + values[value]));
    }
    if (typeof stop.opening_hours === "string" && stop.opening_hours.length <= 512 && stop.opening_hours.trim()) facts.append(node("p", "Расписание источника: " + stop.opening_hours));
    if (stop.opening_status && stop.opening_status.source === "source-reported") {
      const notes = {open:"Указанное расписание покрывает предложенное посещение. Фактический доступ не проверен.",
        unknown:"Расписание на время посещения не подтверждено."};
      if (notes[stop.opening_status.status]) facts.append(node("p", notes[stop.opening_status.status]));
    }
    if (typeof stop.source_observed_at === "string" && /^\d{4}-\d{2}-\d{2}T/.test(stop.source_observed_at)) facts.append(node("p", "Данные получены: " + stop.source_observed_at.slice(0,10) + ". Актуальность на месте не проверена."));
    if (stop.features && stop.features.wheelchair === "yes") facts.append(node("p", "Сведения о входе не подтверждают доступность всего маршрута."));
    if (facts.children.length) details.append(facts);
    if (details.children.length > 1) content.append(details);
    content.append(node("p", PROVIDERS[stop.provider] + (finite(stop.visit_minutes) ? " · посещение " + stop.visit_minutes + " мин" : "")));
    content.append(link("Источник места ↗", stop.source_url, stop.provider));
    item.append(content);
    host.append(item);
  });
  loadRouteMap(option, option.resolved_context || state.result.resolved_context);
}
async function submitWalk(event) {
  if (event && event.preventDefault) event.preventDefault();
  const query = $("walk-query").value.trim();
  if (!query || query.length > 2000) {
    $("walk-error").textContent = "Сообщение пустое или слишком длинное. Подбор не запущен.";
    show("walk-error", true);
    return;
  }
  cancelFlow("");
  const id = state.requestId;
  const sig = signature();
  state.signature = sig;
  const controller = new AbortController();
  state.controller = controller;
  const clock = new Date();
  const payload = {query, client_context: {instant: clock.toISOString(), utc_offset_minutes: -clock.getTimezoneOffset()}};
  let timedOut = false;
  let probe = null;
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); if (state.geoCancel) state.geoCancel(); }, 210000);
  $("walk-submit").disabled = true;
  $("walk-submit").textContent = "Подбираем прогулку…";
  $("walk-status").textContent = "Определяем старт и время из сообщения…";
  try {
    const preparationEnabled = await contextPreparationEnabled(controller.signal);
    if (!current(id, sig)) return;
    if (controller.signal.aborted) { const error = new Error("Подбор отменён."); error.name = "AbortError"; throw error; }
    if (preparationEnabled) payload.consent = true;
    probe = await post("/api/context", payload, controller.signal);
    if (!current(id, sig)) return;
    if (controller.signal.aborted) { const error = new Error("Подбор отменён."); error.name = "AbortError"; throw error; }
    if (preparationEnabled) payload.preparation_token = preparationToken(probe);
    if (probe.needs_geolocation === true) {
      $("walk-status").textContent = "Ожидаем разрешённую геолокацию, не более 8 секунд…";
      const location = await requestLocation(id, sig);
      if (!current(id, sig)) return;
      if (controller.signal.aborted) { const error = new Error("Подбор отменён."); error.name = "AbortError"; throw error; }
      if (location.status !== "granted") throw startError(location.status);
      payload.location_status = "granted";
      payload.geolocation = location.geolocation;
      probe = await post("/api/context", payload, controller.signal);
      if (!current(id, sig)) return;
      if (controller.signal.aborted) { const error = new Error("Подбор отменён."); error.name = "AbortError"; throw error; }
      if (preparationEnabled) payload.preparation_token = preparationToken(probe);
    }
    if (probe.needs_geolocation || !startKnown(probe.resolved_context)) throw startError();
    $("walk-status").textContent = "Проверяем OSM, Google и 2ГИС. Затем рассчитаем возвращение и подготовим один ответ…";
    const data = await post("/api/unified/recommend", {...payload, consent: true}, controller.signal);
    if (!current(id, sig)) return;
    if (controller.signal.aborted) { const error = new Error("Подбор отменён."); error.name = "AbortError"; throw error; }
    renderResult(validateResult(data));
    $("walk-status").textContent = "Подбор завершён. Варианты времени переключаются без нового поиска.";
    $("walk-result-title").focus({preventScroll: true});
    $("walk-result-title").scrollIntoView({block: "start", behavior: "auto"});
  } catch (error) {
    if (!current(id, sig)) return;
    clearResult();
    if (timedOut) { error = new Error(); error.code = "timeout"; }
    $("walk-error").textContent = errorMessage(error);
    show("walk-error", true);
    $("walk-status").textContent = "Подбор не выполнен.";
  } finally {
    delete payload.preparation_token;
    delete payload.query;
    delete payload.client_context;
    probe = null;
    delete payload.geolocation;
    delete payload.location_status;
    clearTimeout(deadline);
    if (current(id, sig)) {
      state.controller = null;
      $("walk-submit").disabled = false;
      $("walk-submit").textContent = "Найти прогулку →";
    }
  }
}
function boot() {
  state.signature = signature();
  $("walk-form").addEventListener("submit", submitWalk);
  $("walk-query").addEventListener("input", onInputChange);
  $("walk-query").addEventListener("change", onInputChange);
  window.addEventListener("pagehide", () => cancelFlow("Подбор остановлен. Можно отправить новое сообщение."));
}
document.addEventListener("DOMContentLoaded", boot);
