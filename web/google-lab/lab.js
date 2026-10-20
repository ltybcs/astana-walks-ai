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
  if(currentProvider()==="google") {state.layer.clearLayers();return;}
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
function fitResults() {if(currentProvider()!=="google" && state.map && state.filtered.length) state.map.fitBounds(state.filtered.map(place=>[place.lat,place.lon]),{padding:[25,25],maxZoom:14,animate:false});}
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
  if(tab==="osm" && currentProvider()==="google") tab="google";
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
function walkValues() {const w=ui.walk;return {provider:w.provider?.value || "osm",query:w.query.value,origin:w.origin.value,minutes:w.minutes.value,day:w.day.value,lat:w.lat.value,lon:w.lon.value};}
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
  const custom=currentProvider()!=="google" && ui.walk.origin.value==="custom";ui.walk.coordinates.hidden=!custom;
  for(const field of [ui.walk.lat,ui.walk.lon]) {field.disabled=!custom;field.required=custom;}
}
function invalidateWalk() {
  clearGoogleProvider();
  state.walkFormSignature=walkSignature();
  state.walkRequestId++;state.walkController?.abort();state.walkController=null;state.walkData=null;state.routeMarkers=[];
  state.routeLayer?.clearLayers();ui.walk.cards.replaceChildren();ui.walk.placeholder.hidden=false;
  ui.walk.context.replaceChildren();ui.walk.context.hidden=true;ui.walk.budget.hidden=true;ui.walk.generation.hidden=true;ui.walk.dataNote.hidden=true;ui.walk.actions.hidden=true;ui.walk.google.removeAttribute("href");
  ui.walk.mode.textContent="Ожидает запроса";ui.walk.mode.removeAttribute("data-mode");
  ui.walk.status.classList.remove("error");ui.walk.status.textContent="Параметры изменены. Нажмите «Найти мою прогулку», чтобы обновить план.";
  ui.walk.submit.disabled=false;ui.walk.submit.textContent="Найти мою прогулку →";clearWalkFieldErrors();
  unloadGoogle("Параметры прогулки изменены. Google-карта выгружена; подключите её снова по кнопке.");updateMapCount();
  if(ui.google)applyProviderSurface();
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
  if(!state.areas.length) return;
  const data=currentProvider()==="google"?state.googleData:state.walkData;
  if(!data) return;
  const originID=data.resolved_context.origin_id;
  const wanted=originID==="mega"?"mega":originID==="baiterek"?"baiterek":"astana";
  if(!state.areas.some(area=>area.id===wanted)) return;
  if(ui.area.value!==wanted) {unloadGoogle("Район прогулки изменён. Подключите Google-карту по кнопке.");ui.area.value=wanted;}
  ui.googleExternal.href=googleURLForArea(selectedArea());
}
async function submitWalk(event) {
  if(currentProvider()==="google")return previewGoogle(event);
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
  ui.walk={provider:get("walk-provider"),providerNote:get("provider-note"),customOption:get("walk-custom-option"),form:get("walk-form"),query:get("walk-query"),origin:get("walk-origin"),minutes:get("walk-minutes"),day:get("walk-day"),lat:get("walk-lat"),lon:get("walk-lon"),coordinates:get("walk-coordinates"),options:get("walk-options"),submit:get("walk-submit"),section:get("walk-result"),heading:get("walk-result-title"),mode:get("walk-mode"),status:get("walk-status"),context:get("walk-context"),budget:get("walk-budget"),generation:get("walk-generation"),dataNote:get("walk-data-note"),placeholder:get("walk-placeholder"),cards:get("walk-cards"),actions:get("walk-route-actions"),google:get("walk-google-route")};
  state.walkRequestId=0;state.walkController=null;state.walkData=null;state.routeMarkers=[];
  initGoogleProvider();state.walkFormSignature=walkSignature();
  updateWalkCustomFields();ui.walk.form.addEventListener("submit",submitWalk);
  const changed=()=>{updateWalkCustomFields();if(walkSignature()!==state.walkFormSignature)invalidateWalk();};
  ui.walk.form.addEventListener("input",changed);ui.walk.form.addEventListener("change",changed);
  document.querySelectorAll("[data-example]").forEach(button=>button.addEventListener("click",()=>{const example=WALK_EXAMPLES[button.dataset.example];if(!example) return;ui.walk.query.value=example.query;ui.walk.origin.value=example.origin;ui.walk.minutes.value=example.minutes;ui.walk.day.value=example.day;ui.walk.options.open=example.origin!=="auto";updateWalkCustomFields();invalidateWalk();ui.walk.query.focus({preventScroll:true});}));
  get("walk-show-map").addEventListener("click",focusWalkMap);
  ui.explorer.addEventListener("toggle",()=>{renderMarkers(false);updateMapCount();});
}

/* Google live provider: explicit preview/consent, isolated text and details. */
function currentProvider() {return ui?.walk?.provider?.value==="google"?"google":"osm";}
function safeHTTPS(value) {
  try {const url=new URL(value);return url.protocol==="https:" && !url.username && !url.password ? url.href : null;} catch {return null;}
}
function safeGoogleDirections(value) {
  try {
    const url=new URL(value);
    return url.origin==="https://www.google.com" && url.pathname==="/maps/dir/" && url.searchParams.get("api")==="1" && url.searchParams.get("travelmode")==="walking" && url.searchParams.get("origin")===url.searchParams.get("destination") && Boolean(url.searchParams.get("origin")) ? url.href : null;
  } catch {return null;}
}
function quotaText(quota) {
  if(!quota || !Number.isInteger(quota.used) || !Number.isInteger(quota.limit) || !Number.isInteger(quota.remaining)) return "Тестовый лимит временно неизвестен.";
  return "Тестовый лимит: "+quota.used+" из "+quota.limit+" внешних попыток; осталось "+quota.remaining+".";
}
function googleWarnings(value) {return (Array.isArray(value)?value:[]).filter(item=>typeof item==="string" && item.trim()).join(" ");}
function googleContextText(context) {
  return (context.origin.label || "Публичный старт")+" · "+context.minutes+" мин · "+({day:"днём",evening:"вечером",night:"ночью"}[context.time_of_day] || "");
}
function validatePublicContext(context) {
  if(!context || !["mega","baiterek","river"].includes(context.origin_id) || !validPoint(context.origin) || !Number.isInteger(context.minutes) || context.minutes<10 || context.minutes>480 || !["day","evening","night"].includes(context.time_of_day)) throw new Error("Google-поиск требует известного публичного старта и времени. Уточните запрос.");
  return context;
}
function validateGoogleSearchParameters(parameters,text,context) {
  const expected=["includeExtendedDetails","languageCode","locationBias","pageSize","regionCode","textQuery"];
  const circle=parameters?.locationBias?.circle,center=circle?.center;
  if(!parameters || typeof parameters!=="object" || Array.isArray(parameters) || JSON.stringify(Object.keys(parameters).sort())!==JSON.stringify(expected) || parameters.textQuery!==text || parameters.languageCode!=="ru" || parameters.regionCode!=="KZ" || parameters.pageSize!==5 || parameters.includeExtendedDetails!==false || !center || !validPoint({lat:center.latitude,lon:center.longitude}) || Math.abs(center.latitude-context.origin.lat)>0.000001 || Math.abs(center.longitude-context.origin.lon)>0.000001 || !Number.isInteger(circle.radiusMeters) || circle.radiusMeters<100 || circle.radiusMeters>2000) throw new Error("Не удалось показать полные параметры передачи Google. Внешний поиск не выполнен.");
}function buildGooglePayload() {
  const payload=buildWalkPayload(walkValues());
  if(payload.origin || ui.walk.origin.value==="custom") throw new Error("В Google-режиме доступны только публичные старты: MEGA, Байтерек и набережная. Свои координаты не отправляются.");
  return payload;
}
function clearGoogleProvider() {
  state.googlePreview=null;state.googleData=null;state.googleDetailsId=(state.googleDetailsId || 0)+1;
  state.googleDetailsController?.abort();state.googleDetailsController=null;
  const g=ui.google;if(!g) return;
  g.preview.hidden=true;g.send.disabled=true;g.outbound.textContent="";g.parameters.textContent="";g.previewContext.textContent="";g.previewWarnings.textContent="";g.previewQuota.textContent="";
  g.content.hidden=true;g.placeholder.hidden=false;g.answer.textContent="";g.sources.replaceChildren();g.context.replaceChildren();g.budget.textContent="";g.routeAttributions.replaceChildren();g.warnings.textContent="";
  g.original.hidden=true;g.original.open=false;g.originalText.textContent="";g.originalSources.replaceChildren();g.routes.hidden=true;g.directions.removeAttribute("href");
  g.details.hidden=true;g.detailsContent.replaceChildren();g.detailsStatus.textContent="";g.hours.disabled=false;g.reviews.disabled=false;
}
function applyProviderSurface() {
  const google=currentProvider()==="google",g=ui.google;
  ui.walk.section.classList.toggle("provider-google",google);
  ui.walk.heading.textContent=google?"Ваша прогулка · Google":"Ваша прогулка";
  g.consent.hidden=!google;g.result.hidden=!google;ui.walk.placeholder.hidden=google;
  ui.walk.providerNote.textContent=google?"Live-поиск по Google: сначала покажем точные условия передачи. Данные OSM в этот ответ не подмешиваются.":"Снимок OSM и локальная модель. Поиск по Google включается отдельно.";
  ui.walk.submit.textContent=google?"Проверить условия для Google →":"Найти мою прогулку →";
  ui.walk.customOption.disabled=google;
  if(google && ui.walk.origin.value==="custom") {ui.walk.origin.value="auto";ui.walk.lat.value="";ui.walk.lon.value="";}
  updateWalkCustomFields();
  ui.explorer.hidden=google;if(google) ui.explorer.open=false;
  ui.datasetNote.hidden=google;ui.providerTerms.textContent=google?"Google Maps работает по условиям Google. Часы, доступность и безопасность требуют самостоятельной проверки.":"© OpenStreetMap contributors · ODbL 1.0. Google Maps работает по условиям Google. Часы, доступность и безопасность требуют самостоятельной проверки.";
  ui.tabs.osm.hidden=google;ui.panels.osm.hidden=google;
  ui.mapsDescription.textContent=google?"Здесь только Google-карта. Ответ Google не переносится на OSM-карту. Подключение карты — по отдельной кнопке.":"Рекомендации основаны на OSM. Google помогает рассмотреть район и открыть пеший маршрут по ссылке.";
  ui.googleMapDescription.textContent=google?"Карта показывает публичный район старта. Маршрут выбранной прогулки откроется отдельной ссылкой в ответе.":"Google-карта показывает район. Пеший маршрут откроется отдельной ссылкой в результате подбора; рекомендации и каталог — из OSM.";
  if(google) {state.layer?.clearLayers();state.routeLayer?.clearLayers();setTab("google");}
  else {setTab("osm");renderMarkers(false);}
  state.walkFormSignature=walkSignature();
}
async function loadGoogleStatus() {
  try {
    const response=await fetch("/api/google/status",{cache:"no-store"});const data=await response.json();
    if(!response.ok) throw new Error("status");
    state.googleService=data;
    ui.google.service.textContent=(data.available===false || data.enabled===false || data.configured===false || data.ready===false ? "Google-пилот пока недоступен. Режим OSM работает отдельно. " : "Google-пилот требует вашего подтверждения перед каждым поиском. ")+quotaText(data.quota);
  } catch {ui.google.service.textContent="Не удалось проверить доступность Google-пилота. Подбор по OSM доступен отдельно.";}
}
async function jsonGoogleRequest(path,payload,controller) {
  const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),signal:controller.signal,cache:"no-store"});
  let data;try {data=await response.json();} catch {throw new Error("Сервер Google-пилота вернул неполный ответ. Повторите действие позже.");}
  return {response,data};
}
function googleRequestError(data) {
  const message=typeof data?.error==="string"?data.error:"Google-поиск недоступен. Попробуйте позже или явно выберите OSM.";
  const error=new Error(message+(data?.quota?" "+quotaText(data.quota):""));
  error.contextCode=data?.code;return error;
}
function googleErrorMode(error) {return ["needs_context","context_conflict","preview_mismatch","consent_required"].includes(error.contextCode)?"Нужно уточнение":"Google: поиск недоступен";}
async function previewGoogle(event) {
  event?.preventDefault();let payload;
  try {payload=buildGooglePayload();} catch(error) {ui.walk.status.classList.add("error");ui.walk.status.textContent=error.message;return;}
  invalidateWalk();const requestId=++state.walkRequestId,signature=walkSignature(),controller=new AbortController();state.walkController=controller;
  ui.walk.submit.disabled=true;ui.walk.submit.textContent="Проверяем условия…";ui.walk.mode.textContent="Локальная проверка";
  ui.walk.status.textContent="Готовим условия передачи. Внешний поиск пока не выполняется.";ui.google.placeholder.hidden=false;
  const timer=setTimeout(()=>controller.abort(),15000);
  try {
    const {response,data}=await jsonGoogleRequest("/api/google/preview",payload,controller);
    if(requestId!==state.walkRequestId || signature!==walkSignature() || currentProvider()!=="google") return;
    if(!response.ok) {markWalkFieldErrors(data.fields);throw googleRequestError(data);}
    validatePublicContext(data.resolved_context);
    if(typeof data.outbound_search_text!=="string" || !data.outbound_search_text.trim() || typeof data.preview_token!=="string" || !data.preview_token) throw new Error("Не удалось подготовить точные условия передачи. Внешний поиск не выполнен.");
    validateGoogleSearchParameters(data.outbound_search_parameters,data.outbound_search_text,data.resolved_context);
    state.googlePreview={payload,signature,token:data.preview_token};
    const g=ui.google;g.outbound.textContent=data.outbound_search_text;g.parameters.textContent=JSON.stringify(data.outbound_search_parameters,null,2);g.previewContext.textContent=googleContextText(data.resolved_context);
    g.previewWarnings.textContent=googleWarnings(data.warnings);g.previewQuota.textContent=quotaText(data.quota);g.preview.hidden=false;g.send.disabled=data.quota?.state!=="ready";
    ui.walk.mode.textContent=g.send.disabled?"Лимит недоступен":"Ждёт согласия";ui.walk.status.textContent=g.send.disabled?"Условия подготовлены локально, но тестовый лимит недоступен. Google не запрашивался.":"Проверьте условия ниже и подтвердите отправку. Google ещё не запрашивался.";
    if(!g.send.disabled)g.send.focus({preventScroll:true});
  } catch(error) {
    if(requestId!==state.walkRequestId || signature!==walkSignature()) return;
    ui.walk.mode.textContent=googleErrorMode(error);ui.walk.status.classList.add("error");ui.walk.status.textContent=error.name==="AbortError"?"Локальная проверка заняла слишком много времени. Повторите действие.":error.message;
  } finally {clearTimeout(timer);if(state.walkController===controller)state.walkController=null;if(requestId===state.walkRequestId){ui.walk.submit.disabled=false;ui.walk.submit.textContent="Проверить условия для Google →";}}
}
function validateGoogleResponse(data) {
  validatePublicContext(data?.resolved_context);
  if(!["google-grounded-llm","google-grounding-summary"].includes(data.mode) || !data.grounding || typeof data.grounding.summary!=="string" || !Array.isArray(data.grounding.sources) || !["fits","does_not_fit","unverified"].includes(data.budget_status)) throw new Error("Ответ Google неполный. Условия и время не подтверждены.");
  if(data.mode==="google-grounded-llm" && data.budget_status!=="fits") throw new Error("План Google не подтверждён как укладывающийся в бюджет. Рекомендация ИИ не показана.");
  if(data.mode==="google-grounded-llm" && (!data.answer || typeof data.answer.text!=="string" || !data.answer.text.trim())) throw new Error("Не удалось получить цельный ответ ИИ по Google.");
  if(data.grounding.sources.some(source=>!source || typeof source!=="object")) throw new Error("Google вернул неполные источники ответа.");
  if(!data.plan || data.plan.budget_minutes!==data.resolved_context.minutes || data.plan.returns_to_origin!==true || typeof data.plan.verified!=="boolean") throw new Error("Не удалось подтвердить расчёт времени Google.");
  for(const key of ["total_minutes","walk_minutes","outbound_minutes","return_minutes","visit_minutes","buffer_minutes"]) if(data.plan[key]!==null && (!Number.isFinite(data.plan[key]) || data.plan[key]<0)) throw new Error("Google вернул некорректный расчёт времени.");
  if(data.budget_status==="fits" && (!data.plan.verified || !Number.isFinite(data.plan.total_minutes) || data.plan.total_minutes>data.plan.budget_minutes)) throw new Error("Укладывание в бюджет не подтверждено.");
  return data;
}
function googleSourceCard(source,index) {
  const card=node("article","google-source"),brand=node("span","google-brand","Google Maps");brand.setAttribute("translate","no");card.append(brand,node("p","","["+index+"]"+(typeof source.source_id==="string"?" · "+source.source_id:"")));
  const title=typeof source.title==="string" && source.title ? source.title : source.attribution?.title;
  card.append(node("h3","",typeof title==="string" && title ? title : "Источник "+(index+1)));
  const placeURL=safeHTTPS(source.url || source.googleMapsLinks?.placeUrl),attributionURL=safeHTTPS(source.attribution?.url);
  if(placeURL) card.append(externalLink("Открыть источник Google ↗",placeURL));
  if(attributionURL && attributionURL!==placeURL) card.append(externalLink("Атрибуция источника ↗",attributionURL));
  if(!placeURL && !attributionURL) card.append(node("p","","Ссылка источника не предоставлена Google."));
  return card;
}
function renderGoogleSources(host,sources) {host.replaceChildren(...sources.map(googleSourceCard));}
function googleBudgetText(data) {
  const p=data.plan,minutes=value=>Number.isFinite(value)?value+" мин":"неизвестно";
  const status=data.budget_status==="fits"?"План укладывается в "+p.budget_minutes+" мин":data.budget_status==="does_not_fit"?"План не укладывается в "+p.budget_minutes+" мин":"Укладывание в "+p.budget_minutes+" мин не подтверждено";
  return status+". Всего: "+minutes(p.total_minutes)+"; туда: "+minutes(p.outbound_minutes)+"; обратно: "+minutes(p.return_minutes)+"; посещение: "+minutes(p.visit_minutes)+"; запас: "+minutes(p.buffer_minutes)+".";
}
function renderGoogleWalk(data) {
  const g=ui.google;state.googleData=data;state.googlePreview=null;g.preview.hidden=true;g.send.disabled=true;
  state.walkData=null;state.routeLayer?.clearLayers();state.layer?.clearLayers();state.routeMarkers=[];
  ui.walk.mode.textContent=data.mode==="google-grounded-llm"?"Google + локальный ИИ":"Исходный ответ Google";ui.walk.mode.dataset.mode=data.mode==="google-grounded-llm"?"llm":"extractive";
  ui.walk.status.classList.remove("error");ui.walk.status.textContent=quotaText(data.quota);g.placeholder.hidden=true;g.content.hidden=false;
  g.context.replaceChildren(node("span","context-chip",googleContextText(data.resolved_context)));
  g.budget.textContent=googleBudgetText(data);g.routeAttributions.replaceChildren();
  for(const attribution of Array.isArray(data.plan.route_attributions)?data.plan.route_attributions:[]) {const url=safeHTTPS(attribution?.url);if(url && typeof attribution.title==="string")g.routeAttributions.append(externalLink(attribution.title+" ↗",url));}
  g.warnings.textContent=googleWarnings(data.warnings);
  g.answerLabel.textContent=data.mode==="google-grounded-llm"?"Цельный ответ локального ИИ по Google Grounding Lite":data.budget_status!=="fits"?"Google Grounding Lite · подходящий план не подтверждён; рекомендация ИИ не создавалась":"Google Grounding Lite · исходный ответ без принятой локальной генерации";
  g.answer.textContent=data.mode==="google-grounded-llm"?data.answer.text:data.grounding.summary;
  renderGoogleSources(g.sources,data.grounding.sources);
  g.original.hidden=data.mode!=="google-grounded-llm";g.originalText.textContent=data.grounding.summary;renderGoogleSources(g.originalSources,data.grounding.sources);
  const directions=safeGoogleDirections(data.directions_url);g.routes.hidden=!directions;if(directions)g.directions.href=directions;else g.directions.removeAttribute("href");
  g.details.hidden=!(typeof data.selected_place_id==="string" && data.selected_place_id && typeof data.selection_token==="string" && data.selection_token);
  g.detailsContent.replaceChildren();g.detailsStatus.textContent="Часы и отзывы не загружены. Каждая кнопка делает отдельный запрос к Google.";
  syncGoogleArea();setTab("google");
}
async function recommendGoogle() {
  const preview=state.googlePreview;
  if(currentProvider()!=="google" || !preview || preview.signature!==walkSignature()) {clearGoogleProvider();ui.walk.status.textContent="Условия изменились. Сначала проверьте их ещё раз.";return;}
  const requestId=++state.walkRequestId,signature=walkSignature(),controller=new AbortController();state.walkController?.abort();state.walkController=controller;
  state.googlePreview=null;ui.google.send.disabled=true;ui.walk.submit.disabled=true;ui.walk.mode.textContent="Поиск Google";ui.walk.status.textContent="Google подбирает место и проверяет путь туда и обратно. Затем локальный ИИ подготовит объяснение…";
  const timer=setTimeout(()=>controller.abort(),200000);
  try {
    const {response,data}=await jsonGoogleRequest("/api/google/recommend",{...preview.payload,consent:true,preview_token:preview.token},controller);
    if(requestId!==state.walkRequestId || signature!==walkSignature() || currentProvider()!=="google") return;
    if(!response.ok) {markWalkFieldErrors(data.fields);if(data.quota)ui.google.previewQuota.textContent=quotaText(data.quota);throw googleRequestError(data);}
    renderGoogleWalk(validateGoogleResponse(data));ui.walk.heading.focus({preventScroll:true});
    if(window.matchMedia("(max-width: 760px)").matches)ui.walk.section.scrollIntoView({behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});
  } catch(error) {
    if(requestId!==state.walkRequestId || signature!==walkSignature()) return;
    ui.walk.mode.textContent=googleErrorMode(error);ui.walk.status.classList.add("error");ui.walk.status.textContent=error.name==="AbortError"?"Google-поиск занял слишком много времени. Его внешние попытки могли быть учтены. Для повторения сначала проверьте условия.":error.message;
    ui.google.preview.hidden=true;ui.google.content.hidden=true;
  } finally {clearTimeout(timer);if(state.walkController===controller)state.walkController=null;if(requestId===state.walkRequestId){ui.walk.submit.disabled=false;ui.walk.submit.textContent="Проверить условия для Google →";}}
}
function detailText(value) {return typeof value==="string"?value:typeof value?.text==="string"?value.text:"";}
function renderPlaceAttributions(host,data) {
  const label=node("p","google-details-attribution","Google Maps");label.setAttribute("translate","no");host.append(label);
  for(const item of Array.isArray(data.attributions)?data.attributions:[]) {
    const name=typeof item.provider==="string"?item.provider:typeof item.displayName==="string"?item.displayName:"Атрибуция источника",link=safeHTTPS(item.providerUri || item.uri);
    host.append(link?externalLink(name+" ↗",link,"google-details-attribution"):node("p","google-details-attribution",name));
  }
}
function renderGoogleDetails(data) {
  const host=ui.google.detailsContent,p=data.data || {};host.replaceChildren();
  const name=detailText(p.displayName);if(name)host.append(node("h3","",name));
  if(typeof p.formattedAddress==="string")host.append(node("p","",p.formattedAddress));
  if(data.profile==="hours") {
    const current=p.currentOpeningHours,regular=p.regularOpeningHours;
    if(typeof current?.openNow==="boolean")host.append(node("p","",current.openNow?"По текущим сведениям Google: открыто.":"По текущим сведениям Google: закрыто."));
    let shown=false;
    for(const [label,hours] of [["Текущий график",current],["Обычный график",regular]]) {
      const lines=Array.isArray(hours?.weekdayDescriptions)?hours.weekdayDescriptions.filter(line=>typeof line==="string"):[];
      if(lines.length) {host.append(node("h4","",label));const list=node("ul","google-hours-list");lines.forEach(line=>list.append(node("li","",line)));host.append(list);shown=true;}
    }
    if(!shown)host.append(node("p","","Google не предоставил полный график. Уточните часы у места перед посещением."));
    if(typeof p.businessStatus==="string")host.append(node("p","review-meta","Статус Google: "+p.businessStatus));
  } else {
    if(Number.isFinite(p.rating))host.append(node("p","","Рейтинг Google: "+p.rating+(Number.isInteger(p.userRatingCount)?" · оценок: "+p.userRatingCount:"")));
    const reviews=(Array.isArray(p.reviews)?p.reviews:[]).slice(0,5);
    if(!reviews.length)host.append(node("p","","Google не вернул отзывы для этой выборки. Это не подтверждает наличие или отсутствие розеток."));
    for(const review of reviews) {
      const card=node("article","google-review"),author=review.authorAttribution || {},authorName=typeof author.displayName==="string"?author.displayName:"Автор не указан";
      const heading=node("h4","",authorName),authorURL=safeHTTPS(author.uri);if(authorURL){heading.replaceChildren(externalLink(authorName+" ↗",authorURL));}card.append(heading);
      const meta=[Number.isFinite(review.rating)?"Оценка: "+review.rating:"",typeof review.publishTime==="string"?"Опубликовано: "+dateLabel(review.publishTime):""].filter(Boolean).join(" · ");if(meta)card.append(node("p","review-meta",meta));
      const text=detailText(review.text),original=detailText(review.originalText);card.append(node("p","",text || original || "Текст отзыва не предоставлен."));
      if(original && original!==text) {const originalBlock=node("details");originalBlock.append(node("summary","","Оригинал отзыва"),node("p","",original));card.append(originalBlock);}
      const reviewURL=safeHTTPS(review.googleMapsUri),flagURL=safeHTTPS(review.flagContentUri);if(reviewURL)card.append(externalLink("Отзыв в Google ↗",reviewURL));if(flagURL)card.append(externalLink("Пожаловаться в Google ↗",flagURL));host.append(card);
    }
    host.append(node("p","google-details-attribution","Отзывы выражают мнение авторов; условия посещения и наличие розеток не подтверждены приложением."));
  }
  const placeURL=safeHTTPS(p.googleMapsUri);if(placeURL)host.append(externalLink("Место в Google Maps ↗",placeURL));
  renderPlaceAttributions(host,p);
}
async function loadGoogleDetails(profile) {
  const selected=state.googleData;
  if(currentProvider()!=="google" || !selected || !["hours","reviews"].includes(profile) || !selected.selection_token || !selected.selected_place_id) return;
  state.googleDetailsController?.abort();const controller=new AbortController(),detailsId=++state.googleDetailsId,requestId=state.walkRequestId,signature=walkSignature();state.googleDetailsController=controller;
  ui.google.hours.disabled=true;ui.google.reviews.disabled=true;ui.google.detailsContent.replaceChildren();ui.google.detailsStatus.textContent=profile==="hours"?"Загружаем часы из Google…":"Загружаем отзывы из Google…";
  const timer=setTimeout(()=>controller.abort(),45000);
  try {
    const {response,data}=await jsonGoogleRequest("/api/google/details",{selection_token:selected.selection_token,place_id:selected.selected_place_id,profile},controller);
    if(detailsId!==state.googleDetailsId || requestId!==state.walkRequestId || signature!==walkSignature() || state.googleData!==selected || currentProvider()!=="google")return;
    if(!response.ok)throw googleRequestError(data);
    if(data.source!=="google_places" || data.place_id!==selected.selected_place_id || data.profile!==profile || !data.data || typeof data.data!=="object")throw new Error("Google вернул сведения о другом месте. Повторите действие.");
    renderGoogleDetails(data);ui.google.detailsStatus.textContent="Получено: "+dateLabel(data.fetched_at)+". "+quotaText(data.quota)+" "+googleWarnings(data.warnings);
  } catch(error) {
    if(detailsId!==state.googleDetailsId || requestId!==state.walkRequestId || signature!==walkSignature() || currentProvider()!=="google")return;
    ui.google.detailsStatus.textContent=error.name==="AbortError"?"Загрузка заняла слишком много времени. Попытка могла быть учтена в лимите.":error.message;
  } finally {clearTimeout(timer);if(state.googleDetailsController===controller)state.googleDetailsController=null;if(detailsId===state.googleDetailsId && requestId===state.walkRequestId){ui.google.hours.disabled=false;ui.google.reviews.disabled=false;}}
}
function initGoogleProvider() {
  const get=id=>document.getElementById(id);
  ui.providerTerms=get("provider-terms");ui.mapsDescription=get("maps-description");ui.googleMapDescription=get("google-map-description");
  ui.google={consent:get("google-consent"),service:get("google-service-status"),preview:get("google-preview"),outbound:get("google-outbound"),parameters:get("google-outbound-parameters"),previewContext:get("google-preview-context"),previewWarnings:get("google-preview-warnings"),previewQuota:get("google-preview-quota"),send:get("google-consent-submit"),result:get("google-result"),placeholder:get("google-result-placeholder"),content:get("google-result-content"),context:get("google-result-context"),budget:get("google-result-budget"),routeAttributions:get("google-route-attributions"),warnings:get("google-result-warnings"),answerLabel:get("google-answer-label"),answer:get("google-answer"),sources:get("google-sources"),original:get("google-original"),originalText:get("google-original-text"),originalSources:get("google-original-sources"),routes:get("google-route-actions"),directions:get("google-directions"),details:get("google-place-details"),hours:get("google-hours"),reviews:get("google-reviews"),detailsStatus:get("google-details-status"),detailsContent:get("google-details-content")};
  state.googleDetailsId=0;state.googleData=null;state.googlePreview=null;
  ui.google.send.addEventListener("click",recommendGoogle);ui.google.hours.addEventListener("click",()=>loadGoogleDetails("hours"));ui.google.reviews.addEventListener("click",()=>loadGoogleDetails("reviews"));
  ui.walk.provider.addEventListener("change",()=>{invalidateWalk();applyProviderSurface();});
  window.addEventListener("pagehide",()=>{const pending=Boolean(state.walkController);state.walkRequestId++;state.walkController?.abort();state.walkController=null;state.googleDetailsController?.abort();clearGoogleProvider();ui.walk.submit.disabled=false;ui.walk.submit.textContent=currentProvider()==="google"?"Проверить условия для Google →":"Найти мою прогулку →";if(pending || currentProvider()==="google"){ui.walk.mode.textContent="Ожидает запроса";ui.walk.status.textContent="Предыдущий запрос отменён при уходе со страницы. Для нового поиска проверьте условия ещё раз.";}});
  applyProviderSurface();loadGoogleStatus();
}
