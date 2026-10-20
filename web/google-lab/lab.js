"use strict";

const GROUPS = [
  {id:"all",label:"Все",categories:null,color:"#536b6b"},
  {id:"food",label:"Еда",categories:["cafe","restaurant","bar"],color:"#b24b35"},
  {id:"walk",label:"Прогулка",categories:["park"],color:"#267947"},
  {id:"culture",label:"Культура",categories:["museum","theatre","attraction","monument"],color:"#6251a0"},
  {id:"kids",label:"Дети",categories:["playground"],color:"#ab6b05"},
  {id:"mall",label:"ТЦ",categories:["mall"],color:"#176f96"}
];
const FALLBACK_LABELS = {cafe:"Кафе",restaurant:"Ресторан",bar:"Бар",park:"Парк",museum:"Музей",theatre:"Театр",attraction:"Достопримечательность",monument:"Памятник",playground:"Детская площадка",mall:"Торговый центр"};
const CATEGORY_COLORS = {cafe:"#b24b35",restaurant:"#aa6530",bar:"#88534b",park:"#267947",museum:"#6251a0",theatre:"#975197",attraction:"#496bb2",monument:"#795b93",playground:"#ab6b05",mall:"#176f96"};
function categoryColor(category) {return CATEGORY_COLORS[category] || "#536b6b";}
function restrictedLabel(count) {
  const n=Number.isInteger(count) && count>=0 ? count : 0;
  const tail=n%100;
  if(tail<11 || tail>14) {
    if(n%10===1) return n+" место с ограниченным доступом скрыто";
    if(n%10>=2 && n%10<=4) return n+" места с ограниченным доступом скрыты";
  }
  return n+" мест с ограниченным доступом скрыто";
}
const PAGE_SIZE = 60;
const state = {places:[],filtered:[],dataset:{},summary:{},labels:FALLBACK_LABELS,group:"culture",query:"",showUnnamed:false,limit:PAGE_SIZE,selected:null,map:null,layer:null,markers:new Map(),activeTab:"osm",areas:[],googleLoaded:false,tilesLoaded:0,tilesFailed:0};
let ui;

function normalized(value) {return String(value || "").normalize("NFKC").toLocaleLowerCase("ru").replace(/ё/g,"е");}
function isUnnamed(place) {return place.name_generated === true;}
function groupFor(category) {return GROUPS.find(group => group.categories?.includes(category)) || GROUPS[0];}
function filterPlaces(places,groupId,query,showUnnamed) {
  const group=GROUPS.find(item=>item.id===groupId) || GROUPS[0];
  const words=normalized(query).trim().split(/\s+/).filter(Boolean);
  return places.filter(place=>{
    if(!showUnnamed && isUnnamed(place)) return false;
    if(group.categories && !group.categories.includes(place.category)) return false;
    const haystack=normalized([place.name,FALLBACK_LABELS[place.category],place.category,place.description,place.cuisine,place.operator].filter(Boolean).join(" "));
    return words.every(word=>haystack.includes(word));
  });
}
function googleURLForPlace(place) {
  const query=isUnnamed(place) ? place.lat+","+place.lon : place.name+", Астана";
  return "https://www.google.com/maps/search/?api=1&query="+encodeURIComponent(query);
}
function googleURLForArea(area) {return "https://www.google.com/maps/search/?api=1&query="+encodeURIComponent((area?.name || "Астана")+", Казахстан");}
function safeEmbedURL(value) {
  try {const url=new URL(value);return url.origin==="https://www.google.com" && url.pathname==="/maps/embed" && !url.username && !url.password && Boolean(url.searchParams.get("pb")) && [...url.searchParams.keys()].every(key=>key==="pb") && !url.hash ? url.href : null;} catch {return null;}
}
function sourceURL(place) {
  try {const url=new URL(place.source_url);return url.protocol==="https:" && ["www.openstreetmap.org","openstreetmap.org"].includes(url.hostname) ? url.href : null;} catch {return null;}
}
function node(tag,className,text) {const element=document.createElement(tag);if(className) element.className=className;if(text!==undefined) element.textContent=text;return element;}
function externalLink(text,href,className) {const link=node("a",className,text);link.href=href;link.target="_blank";link.rel="noopener noreferrer";return link;}
function categoryLabel(place) {return state.labels[place.category] || FALLBACK_LABELS[place.category] || place.category;}
function dateLabel(value) {if(!value) return "не указана";const date=new Date(value);return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString("ru-RU",{timeZone:"Asia/Almaty",year:"numeric",month:"long",day:"numeric"});}
function factText(place) {
  if(place.cuisine) return "Кухня по OSM: "+place.cuisine;
  if(place.operator) return "Оператор по OSM: "+place.operator;
  return place.opening_hours ? "Часы по OSM: "+place.opening_hours : "Часы и возможность посещения не подтверждены.";
}
function addFact(list,label,value) {if(value===null || value===undefined || value==="") return;list.append(node("dt","",label),node("dd","",String(value)));}

function createCard(place) {
  const card=node("article","place-card"+(state.selected===place.id?" selected":""));
  card.dataset.placeId=place.id;card.style.setProperty("--category-color",categoryColor(place.category));
  const heading=node("div","card-heading");heading.append(node("h3","",place.name));card.append(heading,node("p","category-label",categoryLabel(place)));
  if(place.access_evidence==="customers") card.append(node("span","card-badge","По OSM: для клиентов"));
  if(isUnnamed(place)) card.append(node("span","card-badge generated-badge","Название по категории; в OSM не указано"));
  card.append(node("p","card-fact",factText(place)));
  const actions=node("div","card-actions");
  const locate=node("button","quiet-button","На карте OSM");locate.type="button";locate.setAttribute("aria-label","Показать "+place.name+" на карте OSM");locate.addEventListener("click",()=>focusPlace(place));
  const google=externalLink("Открыть в Google ↗",googleURLForPlace(place));google.setAttribute("aria-label","Открыть "+place.name+" во внешнем Google Maps");
  actions.append(locate,google);card.append(actions);
  const source=sourceURL(place);if(source) card.append(externalLink("Источник OSM ↗",source,"source-link"));
  const details=node("details");details.append(node("summary","","Факты из источника"));
  const facts=node("dl");addFact(facts,"Доступ по OSM",place.access_evidence || "unknown");addFact(facts,"Часы по OSM",place.opening_hours || "не указаны");addFact(facts,"Кухня",place.cuisine);addFact(facts,"Оператор",place.operator);addFact(facts,"Последнее изменение OSM",place.source_updated_at || place.raw_provenance?.source_updated_at);addFact(facts,"Получено из источника",place.collected_at || state.dataset.collected_at);addFact(facts,"OSM ID",place.raw_provenance?.osm_id || place.id);
  details.append(facts,node("p","card-fact","Сведения источника не заменяют проверку доступа, часов или безопасности на месте."));card.append(details);
  return card;
}
function renderFilters() {
  const basis=filterPlaces(state.places,"all",state.query,state.showUnnamed);
  ui.filters.replaceChildren(...GROUPS.map(group=>{
    const count=group.categories ? basis.filter(place=>group.categories.includes(place.category)).length : basis.length;
    const button=node("button");button.type="button";button.dataset.group=group.id;button.style.setProperty("--group-color",group.color);button.setAttribute("aria-pressed",String(state.group===group.id));button.append(node("span","",group.label),node("span","group-count",String(count)));button.addEventListener("click",()=>{state.group=group.id;state.limit=PAGE_SIZE;applyFilters(true);ui.filters.querySelector('[data-group="'+group.id+'"]')?.focus();});return button;
  }));
}
function renderList() {
  const shown=state.filtered.slice(0,state.limit);
  if(!state.filtered.length) {
    const empty=node("div","empty-state");empty.append(node("h3","","Подходящих мест нет"),node("p","","Попробуйте другой запрос, категорию или включите безымянные места."));
    const reset=node("button","quiet-button","Сбросить фильтры");reset.type="button";reset.addEventListener("click",()=>{state.group="all";state.query="";state.showUnnamed=false;state.limit=PAGE_SIZE;ui.search.value="";ui.unnamed.checked=false;applyFilters(true);});empty.append(reset);ui.list.replaceChildren(empty);
  } else ui.list.replaceChildren(...shown.map(createCard));
  ui.listCount.textContent="В списке "+shown.length+" из "+state.filtered.length+" мест по фильтрам";
  ui.more.hidden=shown.length>=state.filtered.length;
  ui.more.textContent="Ещё "+Math.min(PAGE_SIZE,state.filtered.length-shown.length)+" мест";
}
function applyFilters(fit) {
  state.filtered=filterPlaces(state.places,state.group,state.query,state.showUnnamed);
  state.filtered.sort((a,b)=>Number(isUnnamed(a))-Number(isUnnamed(b)) || a.name.localeCompare(b.name,"ru"));
  if(!state.filtered.some(place=>place.id===state.selected)) state.selected=null;
  renderFilters();renderList();renderMarkers(fit);
  ui.filterSummary.textContent="Найдено "+state.filtered.length+" из "+state.places.length+" объектов каталога. "+(state.showUnnamed?"Безымянные включены.":"Безымянные скрыты.");
  updateMapCount();
}
function createPopup(place) {
  const popup=node("div");popup.append(node("p","popup-title",place.name),node("p","popup-category",categoryLabel(place)),node("p","popup-fact",factText(place)));
  if(place.access_evidence==="customers") popup.append(node("p","popup-fact","По данным OSM: доступ для клиентов."));
  popup.append(node("p","popup-fact","Доступ, часы и условия сейчас не проверены."));
  const links=node("div","popup-links");const source=sourceURL(place);if(source) links.append(externalLink("Источник OSM ↗",source));links.append(externalLink("Открыть в Google ↗",googleURLForPlace(place)));popup.append(links);return popup;
}
function initMap() {
  if(!window.L) {ui.tileStatus.textContent="Библиотека карты недоступна. Каталог и ссылки на источники работают.";return;}
  state.map=L.map(ui.map,{preferCanvas:true,scrollWheelZoom:false}).setView([51.14,71.43],12);
  const tiles=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,referrerPolicy:"strict-origin-when-cross-origin",attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>'});
  let tileTimer;
  const privacyNote="Сервис тайлов получает IP и область карты. Личная геолокация не запрашивается.";
  const unavailable=()=>state.tilesLoaded>0 ? "Подложка загрузилась частично. Каталог и точки работают." : "Подложка недоступна. Каталог и точки работают.";
  const startLoading=()=>{
    clearTimeout(tileTimer);state.tilesLoaded=0;state.tilesFailed=0;
    ui.tileStatus.textContent="Загружаем подложку OSM… "+privacyNote;
    tileTimer=setTimeout(()=>{ui.tileStatus.textContent=unavailable()+" "+privacyNote;},12000);
  };
  tiles.on("loading",startLoading);
  tiles.on("tileload",()=>{state.tilesLoaded++;});
  tiles.on("tileerror",()=>{state.tilesFailed++;ui.tileStatus.textContent=unavailable()+" "+privacyNote;});
  tiles.on("load",()=>{
    clearTimeout(tileTimer);
    ui.tileStatus.textContent=(state.tilesFailed || !state.tilesLoaded ? unavailable()+" " : "")+privacyNote;
  });
  startLoading();tiles.addTo(state.map);state.layer=L.layerGroup().addTo(state.map);state.routeLayer=L.layerGroup().addTo(state.map);
  const zoom=ui.map.querySelectorAll(".leaflet-control-zoom a");if(zoom[0]){zoom[0].title="Приблизить";zoom[0].setAttribute("aria-label","Приблизить карту");}if(zoom[1]){zoom[1].title="Отдалить";zoom[1].setAttribute("aria-label","Отдалить карту");}
}
function renderMarkers(fit) {
  if(!state.map || !state.layer) return;
  state.layer.clearLayers();state.markers.clear();
  if(ui.explorer && !ui.explorer.open) return;
  for(const place of state.filtered) {
    const color=categoryColor(place.category);
    const marker=L.circleMarker([place.lat,place.lon],{radius:6,weight:1.5,color:"#fffef9",fillColor:color,fillOpacity:.9}).bindPopup(()=>createPopup(place),{maxWidth:300});
    marker.on("click",()=>{state.selected=place.id;for(const card of ui.list.querySelectorAll(".place-card")) card.classList.toggle("selected",card.dataset.placeId===place.id);});
    marker.addTo(state.layer);state.markers.set(place.id,marker);
  }
  if(fit) fitResults();
}
function fitResults() {if(state.map && state.filtered.length) state.map.fitBounds(state.filtered.map(place=>[place.lat,place.lon]),{padding:[25,25],maxZoom:14,animate:false});}
function focusPlace(place) {
  setTab("osm");state.selected=place.id;renderList();state.map?.setView([place.lat,place.lon],16,{animate:false});state.markers.get(place.id)?.openPopup();ui.map.tabIndex=0;ui.map.focus({preventScroll:true});ui.mapPanel.scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth",block:"start"});
}
function unloadGoogle(message="Карта выгружена. Новое подключение — только по кнопке.") {
  if(!state.googleLoaded) return;
  ui.googleHost.replaceChildren(googlePlaceholder());state.googleLoaded=false;ui.unload.disabled=true;ui.load.disabled=!selectedArea();ui.googleStatus.textContent=message;
}
function googlePlaceholder() {
  const wrapper=node("div","google-placeholder");wrapper.append(node("span","placeholder-label","Google Maps"),node("h2","","Подключение по вашему выбору"),node("p","","Нажмите «Загрузить Google-карту». Google получит IP и выбранную публичную область. Маркеры каталога OSM сюда не передаются."));return wrapper;
}
function selectedArea() {return state.areas.find(area=>area.id===ui.area.value);}
function setTab(tab,focus=false) {
  state.activeTab=tab;
  for(const id of ["osm","google"]) {const active=id===tab;ui.tabs[id].setAttribute("aria-selected",String(active));ui.tabs[id].tabIndex=active?0:-1;ui.panels[id].hidden=!active;}
  if(tab==="osm") {unloadGoogle("Google-карта выгружена при переходе к OSM. Повторное подключение — по кнопке.");requestAnimationFrame(()=>state.map?.invalidateSize());}
  if(focus) ui.tabs[tab].focus();
}
function loadGoogle() {
  const area=selectedArea();if(!area) return;
  const url=safeEmbedURL(area.embed_url);if(!url) return;
  const frame=document.createElement("iframe");frame.title="Google Maps: "+area.name;frame.referrerPolicy="no-referrer";frame.loading="eager";frame.src=url;
  ui.googleHost.replaceChildren(frame);state.googleLoaded=true;ui.unload.disabled=false;ui.load.disabled=true;ui.googleStatus.textContent="Google-карта загружена. Если она не отображается, откройте её по ссылке ниже.";
}
async function loadEmbeds() {
  try {
    const response=await fetch("/embeds.json",{cache:"no-store"});if(!response.ok) throw new Error("config");const config=await response.json();
    state.areas=Array.isArray(config.maps) ? config.maps.filter(area=>typeof area.id==="string" && typeof area.name==="string" && safeEmbedURL(area.embed_url)).slice(0,12) : [];
    if(!state.areas.length) throw new Error("maps");
    ui.area.replaceChildren(...state.areas.map(area=>{const option=node("option","",area.name);option.value=area.id;return option;}));
    ui.area.value=state.areas.some(area=>area.id===config.default_id)?config.default_id:state.areas[0].id;ui.area.disabled=false;ui.load.disabled=false;ui.googleExternal.href=googleURLForArea(selectedArea());syncGoogleArea();
  } catch {ui.area.replaceChildren(node("option","","Нет настроенных областей"));ui.area.disabled=true;ui.load.disabled=true;ui.googleStatus.textContent="Google-карта недоступна. Можно открыть Астану по ссылке ниже.";}
}
async function loadCatalog() {
  ui.error.hidden=true;
  try {
    const response=await fetch("/api/catalog",{cache:"no-store"});if(!response.ok) throw new Error("catalog");const catalog=await response.json();
    if(!Array.isArray(catalog.places)) throw new Error("schema");
    state.places=catalog.places.filter(place=>typeof place.id==="string" && typeof place.name==="string" && Number.isFinite(place.lat) && Number.isFinite(place.lon) && Math.abs(place.lat)<=90 && Math.abs(place.lon)<=180);
    state.dataset=catalog.dataset || {};state.summary=catalog.summary || {};state.labels={...FALLBACK_LABELS,...catalog.category_labels};
    const generated=state.places.filter(isUnnamed).length;ui.unnamedCount.textContent="("+generated+")";
    ui.summary.replaceChildren(node("span","summary-item",state.places.length+" объектов OSM"),node("span","summary-item",restrictedLabel(state.summary.excluded_restricted_count)),node("span","summary-item","Google — просмотр по кнопке"));
    ui.datasetNote.textContent="Источник каталога: "+(state.dataset.name || "OpenStreetMap")+". Снимок получен "+dateLabel(state.dataset.collected_at)+"; свойства не проверены на месте.";
    applyFilters(true);
  } catch {
    ui.error.hidden=false;ui.errorText.textContent="Не удалось получить локальный каталог. Проверьте, что сервер Maps Lab запущен, и повторите загрузку.";ui.summary.textContent="Каталог временно недоступен";ui.listCount.textContent="";ui.fit.disabled=true;
  }
}
function boot() {
  const get=id=>document.getElementById(id);
  ui={summary:get("catalog-summary"),filters:get("group-filters"),search:get("place-search"),unnamed:get("show-unnamed"),unnamedCount:get("unnamed-count"),filterSummary:get("filter-summary"),list:get("place-list"),listCount:get("list-count"),more:get("load-more"),error:get("catalog-error"),errorText:get("catalog-error-text"),map:get("osm-map"),mapPanel:document.querySelector(".map-panel"),fit:get("fit-map"),mapCount:get("map-result-count"),tileStatus:get("tile-status"),area:get("google-area"),load:get("load-google"),unload:get("unload-google"),googleHost:get("google-frame-host"),googleStatus:get("google-status"),googleExternal:get("google-external"),datasetNote:get("dataset-note"),tabs:{osm:get("tab-osm"),google:get("tab-google")},panels:{osm:get("panel-osm"),google:get("panel-google")}};
  for(const tab of ["osm","google"]) ui.tabs[tab].addEventListener("click",()=>setTab(tab));
  document.querySelector(".map-tabs").addEventListener("keydown",event=>{if(["ArrowLeft","ArrowRight","Home","End"].includes(event.key)){event.preventDefault();setTab(event.key==="Home"?"osm":event.key==="End"?"google":state.activeTab==="osm"?"google":"osm",true);}});
  let timer;ui.search.addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(()=>{state.query=ui.search.value;state.limit=PAGE_SIZE;applyFilters(true);},150);});
  ui.unnamed.addEventListener("change",()=>{state.showUnnamed=ui.unnamed.checked;state.limit=PAGE_SIZE;applyFilters(true);});
  ui.more.addEventListener("click",()=>{state.limit+=PAGE_SIZE;renderList();});ui.fit.addEventListener("click",fitCurrentMap);get("retry-catalog").addEventListener("click",loadCatalog);
  ui.load.addEventListener("click",loadGoogle);ui.unload.addEventListener("click",()=>unloadGoogle());
  ui.area.addEventListener("change",()=>{unloadGoogle("Область изменена. Нажмите кнопку для нового подключения к Google.");ui.load.disabled=!selectedArea();ui.googleExternal.href=googleURLForArea(selectedArea());});
  window.addEventListener("pagehide",()=>unloadGoogle());
  initWalk();initMap();renderFilters();loadCatalog();loadEmbeds();
}
document.addEventListener("DOMContentLoaded",boot);

const WALK_EXAMPLES = {
  mega:{query:"У меня 45 минут возле Mega, хочу короткую прогулку",origin:"auto",minutes:"",day:"auto"},
  family:{query:"Гуляю с ребёнком, ищу детскую площадку или парк",origin:"river",minutes:"60",day:"auto"},
  tourist:{query:"Я турист, хочу посмотреть достопримечательности и архитектуру",origin:"baiterek",minutes:"120",day:"auto"},
  coffee:{query:"Хочу тихое кафе с кофе и розеткой, чтобы поработать",origin:"baiterek",minutes:"60",day:"auto"},
  night:{query:"Хочу ночную активность рядом, только места с явно указанным режимом 24/7",origin:"baiterek",minutes:"60",day:"night"}
};
function validPoint(point) {return point && Number.isFinite(point.lat) && Number.isFinite(point.lon) && Math.abs(point.lat)<=90 && Math.abs(point.lon)<=180;}
function buildWalkPayload(values) {
  const query=String(values.query || "").trim();
  if(!query || query.length>2000) throw new Error("Напишите запрос длиной от 1 до 2000 символов.");
  const payload={query};
  const minutes=String(values.minutes ?? "").trim();
  if(minutes) {
    const amount=Number(minutes);
    if(!Number.isInteger(amount) || amount<10 || amount>480) throw new Error("Укажите целое число минут от 10 до 480.");
    payload.minutes=amount;
  }
  const origin=values.origin || "auto";
  if(["mega","baiterek","river"].includes(origin)) payload.origin_id=origin;
  else if(origin==="custom") {
    const latText=String(values.lat ?? "").trim(),lonText=String(values.lon ?? "").trim();
    const point={lat:Number(latText),lon:Number(lonText),label:"Выбранная точка"};
    if(!latText || !lonText || !validPoint(point)) throw new Error("Укажите корректные широту и долготу.");
    payload.origin=point;
  } else if(origin!=="auto") throw new Error("Выберите известный ориентир или укажите координаты.");
  if(values.day && values.day!=="auto") {
    if(!["day","evening","night"].includes(values.day)) throw new Error("Выберите время суток.");
    payload.time_of_day=values.day;
  }
  return payload;
}
function googleDirectionsURL(context,recommendations) {
  if(!validPoint(context?.origin) || !Array.isArray(recommendations) || recommendations.length<1 || recommendations.length>3 || !recommendations.every(validPoint)) return null;
  const start=context.origin.lat+","+context.origin.lon;
  const params=new URLSearchParams({api:"1",origin:start,destination:start,travelmode:"walking",waypoints:recommendations.map(place=>place.lat+","+place.lon).join("|")});
  return "https://www.google.com/maps/dir/?"+params.toString();
}
function validateWalkResponse(data) {
  const context=data?.resolved_context;
  if(!context || !validPoint(context.origin) || !Number.isInteger(context.minutes) || context.minutes<10 || context.minutes>480 || !["day","evening","night"].includes(context.time_of_day) || typeof data.mode!=="string" || !Array.isArray(data.recommendations) || data.recommendations.length>3) throw new Error("Сервер вернул неполный ответ. Попробуйте подбор ещё раз.");
  for(const place of data.recommendations) {
    if(!validPoint(place) || typeof place.name!=="string" || typeof place.why!=="string" || typeof place.action!=="string" || !sourceURL(place)) throw new Error("В ответе не хватает данных о месте. Попробуйте ещё раз.");
  }
  if(data.recommendations.length) {
    const plan=data.plan;
    if(!plan || plan.returns_to_origin!==true || plan.budget_minutes!==context.minutes || !["total_minutes","walk_minutes","visit_minutes","return_walk_minutes"].every(key=>Number.isFinite(plan[key]) && plan[key]>=0) || plan.total_minutes>context.minutes) throw new Error("Не удалось подтвердить время и возвращение в плане. Повторите подбор.");
  }
  return data;
}
function walkValues() {const w=ui.walk;return {query:w.query.value,origin:w.origin.value,minutes:w.minutes.value,day:w.day.value,lat:w.lat.value,lon:w.lon.value};}
function walkSignature() {return JSON.stringify(walkValues());}
function clearWalkFieldErrors() {for(const element of [ui.walk.query,ui.walk.origin,ui.walk.minutes,ui.walk.day,ui.walk.lat,ui.walk.lon]) element.removeAttribute("aria-invalid");}
function markWalkFieldErrors(fields) {
  for(const field of Array.isArray(fields)?fields:[]) {
    const key=String(field);
    const element=key==="query"?ui.walk.query:key==="minutes"?ui.walk.minutes:key==="time_of_day"?ui.walk.day:key.startsWith("origin")?ui.walk.origin:null;
    if(element) element.setAttribute("aria-invalid","true");
    if(key!=="query") ui.walk.options.open=true;
    if(key==="origin.lat") ui.walk.lat.setAttribute("aria-invalid","true");
    if(key==="origin.lon") ui.walk.lon.setAttribute("aria-invalid","true");
  }
}
function updateWalkCustomFields() {
  const custom=ui.walk.origin.value==="custom";ui.walk.coordinates.hidden=!custom;
  for(const field of [ui.walk.lat,ui.walk.lon]) {field.disabled=!custom;field.required=custom;}
}
function invalidateWalk() {
  state.walkFormSignature=walkSignature();
  state.walkRequestId++;state.walkController?.abort();state.walkController=null;state.walkData=null;state.routeMarkers=[];
  state.routeLayer?.clearLayers();ui.walk.cards.replaceChildren();ui.walk.placeholder.hidden=false;
  ui.walk.context.replaceChildren();ui.walk.context.hidden=true;ui.walk.budget.hidden=true;ui.walk.generation.hidden=true;ui.walk.dataNote.hidden=true;ui.walk.actions.hidden=true;ui.walk.google.removeAttribute("href");
  ui.walk.mode.textContent="Ожидает запроса";ui.walk.mode.removeAttribute("data-mode");
  ui.walk.status.classList.remove("error");ui.walk.status.textContent="Параметры изменены. Нажмите «Найти мою прогулку», чтобы обновить план.";
  ui.walk.submit.disabled=false;ui.walk.submit.textContent="Найти мою прогулку →";clearWalkFieldErrors();
  unloadGoogle("Параметры прогулки изменены. Google-карта выгружена; подключите её снова по кнопке.");updateMapCount();
}
function contextSource(value) {return {query_landmark:"из запроса",query:"из запроса",explicit_landmark:"выбрано вами",explicit_coordinates:"ваша точка",explicit:"выбрано вами",default_day:"по умолчанию"}[value] || "";}
function createWalkCard(place,index,mode) {
  const card=node("article","walk-card"),body=node("div");card.append(node("span","walk-number",String(index+1)),body);body.append(node("h3","",place.name));
  const meta=node("div","walk-meta");meta.append(node("span","",categoryLabel(place)));
  if(Number.isFinite(place.distance_m)) meta.append(node("span","","≈ "+Math.round(place.distance_m)+" м от предыдущей точки"));
  if(Number.isFinite(place.visit_minutes)) meta.append(node("span","",place.visit_minutes+" мин на посещение"));
  body.append(meta,node("p","why",place.why),node("p","action-label","Что сделать"),node("p","action-text",place.action));
  const generated=String(place.explanation_source || mode).includes("llm");
  body.append(node("p","evidence-note",generated?"Объяснение и идея — LLM · сведения о месте — OSM":"Пояснение по базе мест · идея действия — шаблон"));
  if(place.name_generated) body.append(node("p","evidence-note","Название в источнике не указано: показана категория и идентификатор OSM."));
  if(place.access_evidence==="customers") body.append(node("p","opening-note","По OSM: доступ для клиентов."));
  if(place.opening_note) body.append(node("p","opening-note",place.opening_note));
  const actions=node("div","card-actions"),source=sourceURL(place);
  if(source) actions.append(externalLink("Источник места ↗",source));
  const onMap=node("button","quiet-button","Место "+(index+1)+" на OSM");onMap.type="button";onMap.addEventListener("click",()=>focusRouteStop(index));actions.append(onMap);body.append(actions);
  return card;
}
function renderWalk(data) {
  state.walkData=data;const context=data.resolved_context,recommendations=data.recommendations;
  const llm=String(data.mode).startsWith("llm-grounded");
  ui.walk.mode.textContent=llm?"LLM + найденные места":"Поиск + шаблон";ui.walk.mode.dataset.mode=llm?"llm":"extractive";
  ui.walk.context.replaceChildren(node("span","context-chip",(context.origin.label || "Выбранный старт")+" · "+contextSource(context.origin_source)),node("span","context-chip",context.minutes+" мин · "+contextSource(context.minutes_source)),node("span","context-chip",({day:"Днём",evening:"Вечером",night:"Ночью"}[context.time_of_day])+" · "+contextSource(context.time_of_day_source)));ui.walk.context.hidden=false;
  const collected=data.dataset?.collected_at || state.dataset.collected_at;
  ui.walk.dataNote.textContent=collected?"Подбор по снимку OpenStreetMap от "+dateLabel(collected)+". Google-карта служит для просмотра.":"Подбор по снимку OpenStreetMap; дата не указана. Google-карта служит для просмотра.";ui.walk.dataNote.hidden=false;
  const warnings=[data.clarification,...(Array.isArray(data.warnings)?data.warnings:[])].filter(value=>typeof value==="string" && value.trim());
  ui.walk.status.classList.remove("error");ui.walk.status.textContent=warnings.join(" ") || (!recommendations.length?"Подтверждённых мест в выбранных условиях не найдено. Уточните интересы или время.":"");
  ui.walk.cards.replaceChildren(...recommendations.map((place,index)=>createWalkCard({...state.places.find(item=>item.id===place.id),...place},index,data.mode)));
  ui.walk.placeholder.hidden=true;ui.walk.budget.hidden=!recommendations.length;ui.walk.generation.hidden=!recommendations.length;
  if(recommendations.length) {
    const plan=data.plan;ui.walk.budget.textContent="≈ "+plan.total_minutes+" мин всего из "+context.minutes+" · "+plan.walk_minutes+" мин пешком · "+plan.visit_minutes+" мин в местах. Возвращение к старту учтено ("+plan.return_walk_minutes+" мин).";
    ui.walk.generation.textContent=llm?"Объяснения подготовлены LLM по найденным местам. Факты и время рассчитаны отдельно; часы и доступность требуют проверки.":"В этом ответе используется поиск по базе мест и шаблонные пояснения. Свободная генерация LLM не использовалась.";
  }
  const routeURL=googleDirectionsURL(context,recommendations);ui.walk.actions.hidden=!routeURL;if(routeURL) ui.walk.google.href=routeURL;else ui.walk.google.removeAttribute("href");
  syncGoogleArea();drawWalkRoute(Boolean(context.origin_id));updateMapCount();
}
function walkRoutePoints() {const data=state.walkData;if(!data?.recommendations.length) return [];const start=data.resolved_context.origin;return [[start.lat,start.lon],...data.recommendations.map(place=>[place.lat,place.lon]),[start.lat,start.lon]];}
function drawWalkRoute(fit) {
  if(!state.routeLayer || !state.map) return;state.routeLayer.clearLayers();state.routeMarkers=[];
  const data=state.walkData;if(!data?.recommendations.length) return;
  const context=data.resolved_context,points=walkRoutePoints();
  L.polyline(points,{color:"#b04428",weight:4,opacity:.85,dashArray:"8,7"}).addTo(state.routeLayer);
  const start=L.marker([context.origin.lat,context.origin.lon],{icon:L.divIcon({className:"route-pin start-pin",html:"<span>С</span>",iconSize:[44,44],iconAnchor:[22,22]}),autoPanOnFocus:false});
  const startPopup=node("div");startPopup.append(node("p","popup-title","Старт и возвращение"),node("p","popup-fact",context.origin.label || "Выбранная точка"));start.bindPopup(startPopup,{autoPan:false}).addTo(state.routeLayer);
  data.recommendations.forEach((place,index)=>{const marker=L.marker([place.lat,place.lon],{icon:L.divIcon({className:"route-pin",html:"<span>"+(index+1)+"</span>",iconSize:[44,44],iconAnchor:[22,22]}),autoPanOnFocus:false}).bindPopup(()=>createPopup(place),{autoPan:false});marker.addTo(state.routeLayer);state.routeMarkers.push(marker);});
  if(fit) state.map.fitBounds(points,{padding:[35,35],maxZoom:15,animate:false});
}
function updateMapCount() {
  if(!ui?.fit) return;
  const count=state.walkData?.recommendations.length || 0,exploring=Boolean(ui.explorer?.open);
  ui.mapCount.textContent=count?count+" остановки · старт и возвращение"+(exploring?" · "+state.filtered.length+" мест каталога":""):exploring?state.filtered.length+" точек каталога по фильтрам":"Сначала подберите прогулку или откройте каталог ниже";
  ui.fit.textContent=count?"Показать план":"Найденные точки";ui.fit.disabled=!state.map || (!count && (!exploring || !state.filtered.length));
}
function fitCurrentMap() {const points=walkRoutePoints();if(state.map && points.length) state.map.fitBounds(points,{padding:[35,35],maxZoom:15,animate:false});else fitResults();}
function focusWalkMap() {setTab("osm");fitCurrentMap();ui.map.focus({preventScroll:true});ui.mapPanel.scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});}
function focusRouteStop(index) {
  const place=state.walkData?.recommendations[index];if(!place) return;setTab("osm");state.map?.setView([place.lat,place.lon],16,{animate:false});state.routeMarkers[index]?.openPopup();ui.map.focus({preventScroll:true});ui.mapPanel.scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});
}
function syncGoogleArea() {
  if(!state.areas.length || !state.walkData) return;
  const originID=state.walkData.resolved_context.origin_id;
  const wanted=originID==="mega"?"mega":originID==="baiterek"?"baiterek":"astana";
  if(!state.areas.some(area=>area.id===wanted)) return;
  if(ui.area.value!==wanted) {unloadGoogle("Район прогулки изменён. Подключите Google-карту по кнопке.");ui.area.value=wanted;}
  ui.googleExternal.href=googleURLForArea(selectedArea());
}
async function submitWalk(event) {
  event.preventDefault();let payload;
  try {payload=buildWalkPayload(walkValues());} catch(error) {ui.walk.status.classList.add("error");ui.walk.status.textContent=error.message;return;}
  invalidateWalk();const requestId=++state.walkRequestId,signature=walkSignature(),controller=new AbortController();state.walkController=controller;
  ui.walk.submit.disabled=true;ui.walk.submit.textContent="Подбираем места…";ui.walk.placeholder.hidden=true;ui.walk.mode.textContent="Подбор мест";ui.walk.status.textContent="Подбираем прогулку по вашим условиям. Объяснения могут занять некоторое время…";
  const timer=setTimeout(()=>controller.abort(),125000);
  try {
    const response=await fetch("/api/recommend",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),signal:controller.signal});
    let data;try {data=await response.json();} catch {throw new Error("Сервер не смог вернуть результат подбора. Попробуйте ещё раз.");}
    if(requestId!==state.walkRequestId || signature!==walkSignature()) return;
    if(!response.ok) {
      markWalkFieldErrors(data.fields);
      const error=new Error(typeof data.error==="string"?data.error:"Не удалось подобрать прогулку. Проверьте параметры.");
      error.contextCode=data.code;throw error;
    }
    renderWalk(validateWalkResponse(data));
    if(requestId===state.walkRequestId) {ui.walk.heading.focus({preventScroll:true});if(window.matchMedia("(max-width: 760px)").matches) ui.walk.section.scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});}
  } catch(error) {
    if(requestId!==state.walkRequestId || signature!==walkSignature()) return;
    ui.walk.mode.textContent="Нужно уточнение";if(error.contextCode!=="needs_context" && error.contextCode!=="context_conflict") ui.walk.mode.textContent="Ошибка подбора";
    ui.walk.status.classList.add("error");ui.walk.status.textContent=error.name==="AbortError"?"Запрос занял слишком много времени. Повторите подбор.":error.message;
    state.walkData=null;state.routeLayer?.clearLayers();state.routeMarkers=[];ui.walk.actions.hidden=true;ui.walk.google.removeAttribute("href");updateMapCount();
  } finally {
    clearTimeout(timer);if(state.walkController===controller) state.walkController=null;
    if(requestId===state.walkRequestId) {ui.walk.submit.disabled=false;ui.walk.submit.textContent="Найти мою прогулку →";}
  }
}
function initWalk() {
  const get=id=>document.getElementById(id);
  ui.explorer=get("catalog-browser");
  ui.walk={form:get("walk-form"),query:get("walk-query"),origin:get("walk-origin"),minutes:get("walk-minutes"),day:get("walk-day"),lat:get("walk-lat"),lon:get("walk-lon"),coordinates:get("walk-coordinates"),options:get("walk-options"),submit:get("walk-submit"),section:get("walk-result"),heading:get("walk-result-title"),mode:get("walk-mode"),status:get("walk-status"),context:get("walk-context"),budget:get("walk-budget"),generation:get("walk-generation"),dataNote:get("walk-data-note"),placeholder:get("walk-placeholder"),cards:get("walk-cards"),actions:get("walk-route-actions"),google:get("walk-google-route")};
  state.walkRequestId=0;state.walkController=null;state.walkData=null;state.routeMarkers=[];
  state.walkFormSignature=walkSignature();
  updateWalkCustomFields();ui.walk.form.addEventListener("submit",submitWalk);
  const changed=()=>{updateWalkCustomFields();if(walkSignature()!==state.walkFormSignature)invalidateWalk();};
  ui.walk.form.addEventListener("input",changed);ui.walk.form.addEventListener("change",changed);
  document.querySelectorAll("[data-example]").forEach(button=>button.addEventListener("click",()=>{const example=WALK_EXAMPLES[button.dataset.example];if(!example) return;ui.walk.query.value=example.query;ui.walk.origin.value=example.origin;ui.walk.minutes.value=example.minutes;ui.walk.day.value=example.day;ui.walk.options.open=example.origin!=="auto";updateWalkCustomFields();invalidateWalk();ui.walk.query.focus({preventScroll:true});}));
  get("walk-show-map").addEventListener("click",focusWalkMap);
  ui.explorer.addEventListener("toggle",()=>{renderMarkers(false);updateMapCount();});
}
