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
  ui.mapCount.textContent=state.filtered.length+" точек OSM по фильтрам";
  ui.fit.disabled=!state.filtered.length || !state.map;
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
  startLoading();tiles.addTo(state.map);state.layer=L.layerGroup().addTo(state.map);
  const zoom=ui.map.querySelectorAll(".leaflet-control-zoom a");if(zoom[0]){zoom[0].title="Приблизить";zoom[0].setAttribute("aria-label","Приблизить карту");}if(zoom[1]){zoom[1].title="Отдалить";zoom[1].setAttribute("aria-label","Отдалить карту");}
}
function renderMarkers(fit) {
  if(!state.map || !state.layer) return;
  state.layer.clearLayers();state.markers.clear();
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
    ui.area.value=state.areas.some(area=>area.id===config.default_id)?config.default_id:state.areas[0].id;ui.area.disabled=false;ui.load.disabled=false;ui.googleExternal.href=googleURLForArea(selectedArea());
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
  ui.more.addEventListener("click",()=>{state.limit+=PAGE_SIZE;renderList();});ui.fit.addEventListener("click",fitResults);get("retry-catalog").addEventListener("click",loadCatalog);
  ui.load.addEventListener("click",loadGoogle);ui.unload.addEventListener("click",()=>unloadGoogle());
  ui.area.addEventListener("change",()=>{unloadGoogle("Область изменена. Нажмите кнопку для нового подключения к Google.");ui.load.disabled=!selectedArea();ui.googleExternal.href=googleURLForArea(selectedArea());});
  window.addEventListener("pagehide",()=>unloadGoogle());
  initMap();renderFilters();loadCatalog();loadEmbeds();
}
document.addEventListener("DOMContentLoaded",boot);
