"use strict";
const $ = (id) => document.getElementById(id);
const origins = {
  mega: {lat:51.0898,lon:71.4078,label:"MEGA Silk Way / EXPO"},
  baiterek: {lat:51.1283,lon:71.4305,label:"Байтерек"},
  river: {lat:51.1546,lon:71.4252,label:"Набережная Есиля"}
};
const scenarios = {
  walk: {query:"Хочу прогуляться на свежем воздухе и посмотреть город",minutes:"60",origin:"mega"},
  coffee: {query:"Хочу тихое кафе с кофе и розеткой, чтобы поработать",minutes:"60",origin:"baiterek"},
  family: {query:"Гуляю с ребёнком, ищу детскую площадку или парк",minutes:"60",origin:"river"},
  tourist: {query:"Я турист, хочу посмотреть достопримечательности и архитектуру",minutes:"120",origin:"baiterek"},
  night: {query:"Хочу ночную прогулку рядом, только места с явно указанным режимом 24/7",minutes:"60",origin:"baiterek",time:"night"}
};
let places = [];
let lastRecommendations = [];
let requestCounter = 0;
let activeController = null;
let datasetInfo = {};
let catalogPromise = null;
let checkingDataset = false;
let twoGisBusy = false;
let twoGisAvailable = false;
const categoryNames = {park:"Парк",museum:"Музей",cafe:"Кафе",attraction:"Достопримечательность",playground:"Детская площадка",viewpoint:"Смотровая площадка",walk:"Прогулка",monument:"Монумент",promenade:"Прогулочная зона",mall:"Торговый центр",theatre:"Театр",bar:"Бар",restaurant:"Ресторан"};
function originValue() {
  if ($("origin").value !== "custom") return origins[$("origin").value];
  const lat = Number($("latitude").value), lon = Number($("longitude").value);
  if (!$("latitude").value.trim() || !$("longitude").value.trim() || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat)>90 || Math.abs(lon)>180) throw new Error("Укажите корректные координаты.");
  return {lat,lon,label:"Выбранная точка"};
}
function updateCoordinateFields() {
  const custom=$("origin").value==="custom";
  $("coordinates").hidden=!custom;
  for(const id of ["latitude","longitude"])$(id).disabled=!custom;
}
function updateDurationFields() {
  const custom=$("minutes").value==="custom";
  $("custom-duration").hidden=!custom;
  $("custom-minutes").disabled=!custom;
  $("custom-minutes").required=custom;
}
function durationValue() {
  const raw=$("minutes").value==="custom"?$("custom-minutes").value:$("minutes").value;
  const minutes=Number(raw);
  if(!raw.trim()||!Number.isInteger(minutes)||minutes<10||minutes>480)throw new Error("Укажите целое число минут от 10 до 480 (8 часов).");
  return minutes;
}
function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function safeLink(url) {
  try { const parsed=new URL(url); return parsed.protocol==="https:" ? parsed.href : null; } catch { return null; }
}
function sourceLink(url, label) {
  const href = safeLink(url);
  if (!href) return el("span","source-link","Ссылка на источник недоступна");
  const link=el("a","source-link",label); link.href=href;link.target="_blank";link.rel="noopener noreferrer";return link;
}
function drawMap() {
  let origin;
  try{origin=originValue();}catch{
    if(window.AstanaMap)window.AstanaMap.update({origin:null,places,recommendations:[],focusAllowed:false});
    return;
  }
  if(window.AstanaMap){
    window.AstanaMap.update({origin,places,recommendations:lastRecommendations,focusAllowed:$("origin").value!=="custom"});
  }else{
    $("city-map").textContent="Карта не загрузилась. Список рекомендаций и ссылки на места доступны ниже.";
  }
}
function renderRecommendations(data) {
  const list=$("results");list.replaceChildren();
  const usedDataset=data.dataset||datasetInfo;
  if(usedDataset.collected_at&&usedDataset.collected_at!==datasetInfo.collected_at)loadPlaces(false);
  lastRecommendations=(data.recommendations||[]).map(p=>({...places.find(x=>x.id===p.id),...p}));
  lastRecommendations.forEach((p,index)=>{
    const card=el("article","place-card"),body=el("div");
    card.append(el("span","place-number",index+1),body);
    body.append(el("h3",null,p.name));
    const meta=el("div","place-meta");
    const distance=Number(p.distance_m); const distanceText=Number.isFinite(distance)?(distance>=1000?(distance/1000).toFixed(1)+" км":Math.round(distance)+" м"):"Расстояние неизвестно";
    meta.append(el("span",null,categoryNames[p.category]||p.category||"Место"),el("span",null,"≈ "+distanceText+" от предыдущей точки"),el("span",null,(p.visit_minutes||"—")+" мин на посещение"));body.append(meta);
    if(p.why)body.append(el("p",null,p.why));
    if(p.name_generated)body.append(el("p","evidence-label","Название в источнике не указано: показана категория и идентификатор OSM."));
    if(p.action)body.append(el("p","action","Что сделать: "+p.action));
    const generated=String(p.explanation_source||data.mode||"").includes("llm");
    body.append(el("p","evidence-label",generated?"Объяснение и идея — LLM · сведения о месте — POI":"Объяснение по базе POI · идея действия — шаблон"));
    if(p.opening_note)body.append(el("p","opening",p.opening_note));
    body.append(sourceLink(p.source_url,"Источник места ↗"));list.append(card);
  });
  const warnings=[data.clarification,...(data.warnings||[])].filter(Boolean);
  $("status").classList.remove("error");
  $("status").textContent=warnings.join(" ")||(!lastRecommendations.length?"Подходящих мест пока нет. Попробуйте другую локацию или больше времени.":"");
  const plan=data.plan;$("plan-summary").hidden=!plan||!lastRecommendations.length;
  if(plan)$("plan-summary").textContent="≈ "+plan.total_minutes+" мин всего · "+plan.walk_minutes+" мин пешком · "+plan.visit_minutes+" мин в местах · возврат к старту учтён";
  const llmMode=String(data.mode).startsWith("llm-grounded");
  $("result-mode").textContent=llmMode?"RAG + LLM":"ПОИСК + ШАБЛОННЫЙ ОТВЕТ";
  $("generation-note").hidden=!lastRecommendations.length;
  $("generation-note").textContent=llmMode?"LLM сформулировала объяснения по найденным источникам. Факты, расстояние и время рассчитаны приложением; проверка текста снижает риск ошибок, но не исключает их.":"Работает локальный ответ по базе POI. Свободная генерация LLM в этом ответе не использовалась.";
  const sources=$("sources");sources.replaceChildren();
  sources.append(el("p",null,"Использованы: введённые координаты, запрос, время прогулки и выбранное время суток. Часы и свойства мест требуют проверки. Сгенерированная идея действия не подтверждает наличие услуги."));
  for(const s of data.sources||[])if(s&&typeof s==="object")sources.append(sourceLink(s.source_url||s.url,s.name||s.title||s.id||"Источник"));
  if(usedDataset.collected_at)sources.append(el("p",null,"Сбор данных этого ответа: "+new Date(usedDataset.collected_at).toLocaleString("ru-RU")+". "+(usedDataset.attribution||"")));
  const diagnostics=$("diagnostics");diagnostics.replaceChildren();
  if(data.diagnostics){
    const d=data.diagnostics;
    const count=typeof d.candidates_count==="number"?d.candidates_count:data.retrieved_count;
    if(Number.isFinite(count))diagnostics.append(el("p",null,"Найдено кандидатов перед планированием: "+count+". Пустой ответ означает нехватку подходящих подтверждённых данных в выбранном бюджете."));
  }
  $("source-details").hidden=false;drawMap();
}
function invalidateResults(){
  requestCounter++;if(activeController)activeController.abort();
  lastRecommendations=[];$("results").replaceChildren();$("plan-summary").hidden=true;$("generation-note").hidden=true;$("source-details").hidden=true;
  $("result-mode").textContent="ПОИСК ПО POI";
  $("status").classList.remove("error");$("status").textContent="Параметры изменены. Нажмите «Найти мою прогулку», чтобы обновить рекомендации.";drawMap();
}
function revealResult(requestId){
  if(requestId!==requestCounter||!window.matchMedia("(max-width: 760px)").matches)return;
  const target=$("route-results"),heading=target.querySelector("h2");
  heading.setAttribute("tabindex","-1");heading.focus({preventScroll:true});
  // Moving focus can commit a field edit and invalidate the completed request.
  if(requestId!==requestCounter)return;
  target.scrollIntoView({block:"start",behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"});
}
$("origin").addEventListener("change",()=>{updateCoordinateFields();invalidateResults();});
$("use-location").addEventListener("click",()=>{
  if(!navigator.geolocation){$("status").textContent="Геолокация недоступна в этом браузере. Выберите ориентир или укажите координаты вручную.";return;}
  const button=$("use-location");button.disabled=true;button.textContent="Определяем локацию…";
  navigator.geolocation.getCurrentPosition(position=>{
    $("latitude").value=position.coords.latitude.toFixed(6);$("longitude").value=position.coords.longitude.toFixed(6);$("origin").value="custom";updateCoordinateFields();invalidateResults();
    $("status").textContent="Локация определена и используется только для текущего запроса. Выберите интересы и нажмите «Найти мою прогулку».";
    button.disabled=false;button.textContent="⌖ Обновить мою геолокацию";
  },()=>{
    $("status").textContent="Не удалось получить локацию. Разрешите доступ в браузере или выберите ориентир вручную.";button.disabled=false;button.textContent="⌖ Использовать мою геолокацию";
  },{enableHighAccuracy:false,timeout:10000,maximumAge:0});
});
$("minutes").addEventListener("change",()=>{updateDurationFields();invalidateResults();});
for(const id of ["query","custom-minutes","time-of-day","latitude","longitude"])$(id).addEventListener("change",invalidateResults);
for(const button of document.querySelectorAll("[data-scenario]"))button.addEventListener("click",()=>{
  const preset=scenarios[button.dataset.scenario];$("query").value=preset.query;$("minutes").value=preset.minutes;$("origin").value=preset.origin;$("time-of-day").value=preset.time||"day";updateCoordinateFields();updateDurationFields();
  for(const other of document.querySelectorAll("[data-scenario]"))other.classList.toggle("active",other===button);
  invalidateResults();
});
$("planner-form").addEventListener("submit",async(event)=>{
  event.preventDefault();const requestId=++requestCounter;const button=$("submit-button");button.disabled=true;button.firstElementChild.textContent="Подбираем места…";
  $("status").textContent="Сопоставляем интересы, расстояния и ваш запас времени…";$("status").classList.remove("error");
  $("results").replaceChildren();$("plan-summary").hidden=true;$("source-details").hidden=true;$("generation-note").hidden=true;
  lastRecommendations=[];$("result-mode").textContent="ПОДБОР МЕСТ";drawMap();
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),125000);activeController=controller;
  try{
    const response=await fetch("/api/recommend",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:$("query").value,minutes:durationValue(),origin:originValue(),time_of_day:$("time-of-day").value}),signal:controller.signal});
    if(!(response.headers.get("Content-Type")||"").includes("application/json"))throw new Error("Сервис временно недоступен. Попробуйте ещё раз позже.");
    const data=await response.json();if(!response.ok)throw new Error(typeof data.error==="string"?data.error:"Не удалось обработать запрос.");
    if(requestId===requestCounter){renderRecommendations(data);revealResult(requestId);}
  }catch(error){if(requestId===requestCounter){$("result-mode").textContent="ОШИБКА ПОДБОРА";$("status").classList.add("error");$("status").textContent=error.name==="AbortError"?"Запрос занял слишком много времени. Попробуйте снова.":error.message;lastRecommendations=[];drawMap();revealResult(requestId);}}
  finally{clearTimeout(timer);if(activeController===controller)activeController=null;button.disabled=false;button.firstElementChild.textContent="Найти мою прогулку";}
});
function showTwoGisService(service){
  if(!service)return;
  twoGisAvailable=service.available===true;
  if(twoGisBusy)return;
  $("two-gis-submit").disabled=!twoGisAvailable;
  $("two-gis-quota").textContent=!service.configured?"Живой поиск 2ГИС не подключён на этом сервере. Подбор прогулки по OpenStreetMap доступен выше.":
    service.quota?"Осталось "+service.quota.remaining+" из "+service.quota.limit+" запросов за последние 30 дней. Лимит общий для этого демо.":
    "Живой поиск 2ГИС временно недоступен. Повторите позже.";
}
$("two-gis-category").addEventListener("change",()=>{$("two-gis-results").replaceChildren();});
$("two-gis-form").addEventListener("submit",async(event)=>{
  event.preventDefault();if(twoGisBusy||!twoGisAvailable)return;
  twoGisBusy=true;const button=$("two-gis-submit"),category=$("two-gis-category");
  button.disabled=true;category.disabled=true;button.textContent="Ищем в 2ГИС…";
  $("two-gis-results").replaceChildren();$("two-gis-status").textContent="Запрашиваем свежие карточки выбранной категории в Астане…";
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),35000);
  try{
    const response=await fetch("/api/2gis/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({category:category.value}),signal:controller.signal});
    if(!(response.headers.get("Content-Type")||"").includes("application/json"))throw new Error("Живой поиск 2ГИС временно недоступен. Попробуйте позже.");
    const data=await response.json();
    if(data.quota)twoGisAvailable=data.quota.remaining>0;
    if(!response.ok)throw new Error(data.error||"Не удалось выполнить поиск в 2ГИС.");
    const list=$("two-gis-results"),found=Array.isArray(data.places)?data.places:[];
    for(const place of found){
      const card=el("article","live-place");card.append(el("h3",null,place.name));
      card.append(el("p",null,categoryNames[place.category]||"Место"));
      if(place.address)card.append(el("p",null,place.address));
      card.append(el("p",null,place.opening_hours?"Расписание из 2ГИС: "+place.opening_hours+". Проверьте перед визитом.":"Часы и доступ сейчас не подтверждены."));
      card.append(sourceLink(place.source_url,"Открыть карточку в 2ГИС ↗"));list.append(card);
    }
    const remaining=data.quota?" Осталось запросов за 30 дней: "+data.quota.remaining+".":"";
    $("two-gis-status").textContent=(found.length?"Получено карточек: "+found.length+". Это одна страница поиска, а не полный каталог.":"В этой выборке нет подходящих карточек. Попробуйте другую категорию.")+remaining;
  }catch(error){
    $("two-gis-status").textContent=error.name==="AbortError"?"Поиск занял слишком много времени. Попытка уже могла быть учтена в лимите.":error.message;
  }finally{
    clearTimeout(timer);twoGisBusy=false;category.disabled=false;button.disabled=!twoGisAvailable;button.textContent="Найти в 2ГИС";
    checkDataset();
  }
});
async function loadPlaces(initial){
  if(catalogPromise)return catalogPromise;
  catalogPromise=(async()=>{try{
    const response=await fetch("/api/places");if(!response.ok)throw new Error("Данные недоступны");const data=await response.json();places=data.places||[];
    const dataset=data.dataset||{};datasetInfo=dataset;const isDemo=dataset.is_demo!==false;
    const provider=/2gis|2гис/i.test(dataset.name||"")?"2ГИС":/openstreetmap|osm/i.test(dataset.name||"")?"OpenStreetMap":"Реальные POI";
    $("dataset-badge").textContent=isDemo?"Учебные POI · "+places.length+" мест":provider+" · "+places.length+" мест";
    $("dataset-badge").title=dataset.collected_at?"Собрано "+new Date(dataset.collected_at).toLocaleDateString("ru-RU")+" · "+(dataset.attribution||""):"Учебные и непроверенные сведения";drawMap();
    $("dataset-freshness").textContent=dataset.collected_at?"Получено из источника: "+new Date(dataset.collected_at).toLocaleString("ru-RU")+". Сведения не проверены на месте.":"Дата получения неизвестна.";
    if(dataset.attribution)$("data-attribution").textContent=dataset.attribution+" · "+(dataset.license||"Условия источника");
    const licenseLink=safeLink(dataset.license_url);if(licenseLink)$("data-attribution").href=licenseLink;
  }catch{
    if(initial){$("dataset-badge").textContent="Нужно соединение";$("status").classList.add("error");$("status").textContent="Нет связи с сервисом. Подключитесь к интернету и обновите страницу.";}
    else $("dataset-freshness").textContent="Нет связи для обновления списка. На карте остаётся ранее загруженный набор.";
  }})().finally(()=>{catalogPromise=null;});
  return catalogPromise;
}
async function checkDataset(){
  if(checkingDataset||document.hidden)return;
  checkingDataset=true;
  try{
    const response=await fetch("/api/health");if(!response.ok)return;
    const health=await response.json();
    showTwoGisService(health.two_gis);
    if(health.dataset?.collected_at&&health.dataset.collected_at!==datasetInfo.collected_at)await loadPlaces(false);
    const refresh=health.refresh;
    if(refresh){
      $("dataset-refresh").textContent=!refresh.enabled?"Используется сохранённый набор; автообновление на сервере выключено.":
        refresh.state==="updating"?"Сервер получает свежие места из OpenStreetMap. Текущий набор остаётся доступен.":
        refresh.last_error?"Не удалось обновить источник. Используется последний проверенный набор; сервер повторит попытку позже.":
        "Автообновление из OpenStreetMap включено: проверка раз в "+(Number(refresh.ttl_hours)||24)+" ч.";
    }
  }catch{/* Keep the last loaded catalog and the user's current answer. */}
  finally{checkingDataset=false;}
}
async function initialize(){
  updateCoordinateFields();updateDurationFields();drawMap();await loadPlaces(true);
  await checkDataset();
  // This checks our backend snapshot, never triggers a remote Overpass request.
  setInterval(checkDataset,300000);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)checkDataset();});
}
initialize();
