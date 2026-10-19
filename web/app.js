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
  tourist: {query:"Я турист, хочу посмотреть достопримечательности и архитектуру",minutes:"120",origin:"baiterek"}
};
let places = [];
let lastRecommendations = [];
let requestCounter = 0;
const categoryNames = {park:"Парк",museum:"Музей",cafe:"Кафе",attraction:"Достопримечательность",playground:"Детская площадка",viewpoint:"Смотровая площадка",walk:"Прогулка",monument:"Монумент",promenade:"Прогулочная зона",mall:"Торговый центр",theatre:"Театр",bar:"Бар",restaurant:"Ресторан"};
function originValue() {
  if ($("origin").value !== "custom") return origins[$("origin").value];
  const lat = Number($("latitude").value), lon = Number($("longitude").value);
  if (!$("latitude").value.trim() || !$("longitude").value.trim() || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat)>90 || Math.abs(lon)>180) throw new Error("Укажите корректные координаты.");
  return {lat,lon,label:"Выбранная точка"};
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
  if (!href) return el("span","source-link","Учебные данные · источник не проверен");
  const link=el("a","source-link",label); link.href=href;link.target="_blank";link.rel="noopener noreferrer";return link;
}
function svgNode(tag, attrs, content) {
  const node=document.createElementNS("http://www.w3.org/2000/svg",tag);
  for(const [key,value] of Object.entries(attrs))node.setAttribute(key,String(value));
  if(content!==undefined)node.textContent=content;
  return node;
}
function drawMap() {
  const svg=$("city-map"); svg.replaceChildren();
  let origin; try{origin=originValue();}catch{return;}
  const shown=lastRecommendations.length?lastRecommendations:places;
  const points=shown.filter(p=>Number.isFinite(p.lat)&&Number.isFinite(p.lon));
  const bounds=[origin,...points];
  let minLat=Math.min(...bounds.map(p=>p.lat))-.004,maxLat=Math.max(...bounds.map(p=>p.lat))+.004;
  let minLon=Math.min(...bounds.map(p=>p.lon))-.007,maxLon=Math.max(...bounds.map(p=>p.lon))+.007;
  const pos=p=>[55+(p.lon-minLon)/(maxLon-minLon)*610,265-(p.lat-minLat)/(maxLat-minLat)*210];
  for(let x=0;x<740;x+=40)svg.append(svgNode("line",{x1:x,y1:0,x2:x,y2:320,stroke:"#dee6d8","stroke-width":1}));
  for(let y=0;y<340;y+=40)svg.append(svgNode("line",{x1:0,y1:y,x2:720,y2:y,stroke:"#dee6d8","stroke-width":1}));
  svg.append(svgNode("text",{x:28,y:35,fill:"#8b9c86","font-size":10,"letter-spacing":3},"ASTANA / EXPLORE NEARBY"));
  if(lastRecommendations.length){
    const path=[origin,...lastRecommendations.filter(p=>Number.isFinite(p.lat)&&Number.isFinite(p.lon)),origin].map(p=>pos(p).join(",")).join(" ");
    svg.append(svgNode("polyline",{points:path,fill:"none",stroke:"#729878","stroke-width":2,"stroke-dasharray":"6 6"}));
  }
  points.forEach((p,i)=>{
    const [x,y]=pos(p);const g=svgNode("g",{});
    g.append(svgNode("circle",{cx:x,cy:y,r:lastRecommendations.length?13:7,fill:lastRecommendations.length?"#23675a":"#b0c2a5",stroke:"#fff","stroke-width":3}));
    if(lastRecommendations.length)g.append(svgNode("text",{x,y:y+4,fill:"#fff","font-size":11,"text-anchor":"middle"},i+1));
    g.append(svgNode("title",{},p.name));
    if(lastRecommendations.length)g.append(svgNode("text",{x:Math.min(Math.max(x,90),630),y:y-23,fill:"#335142","font-size":11,"text-anchor":"middle"},p.name.length>30?p.name.slice(0,29)+"…":p.name));
    svg.append(g);
  });
  const [x,y]=pos(origin);
  svg.append(svgNode("circle",{cx:x,cy:y,r:20,fill:"#23675a",opacity:.12}),svgNode("circle",{cx:x,cy:y,r:9,fill:"#23675a",stroke:"white","stroke-width":3}),svgNode("text",{x,y:y+31,fill:"#23675a","font-size":11,"font-weight":600,"text-anchor":"middle"},"Вы здесь"));
}
function renderRecommendations(data) {
  const list=$("results");list.replaceChildren();
  lastRecommendations=(data.recommendations||[]).map(p=>({...places.find(x=>x.id===p.id),...p}));
  lastRecommendations.forEach((p,index)=>{
    const card=el("article","place-card"),body=el("div");
    card.append(el("span","place-number",index+1),body);
    body.append(el("h3",null,p.name));
    const meta=el("div","place-meta");
    const distance=Number(p.distance_m); const distanceText=Number.isFinite(distance)?(distance>=1000?(distance/1000).toFixed(1)+" км":Math.round(distance)+" м"):"Расстояние неизвестно";
    meta.append(el("span",null,categoryNames[p.category]||p.category||"Место"),el("span",null,"≈ "+distanceText+" от предыдущей точки"),el("span",null,(p.visit_minutes||"—")+" мин на посещение"));body.append(meta);
    if(p.why)body.append(el("p",null,p.why));
    if(p.action)body.append(el("p","action","Что сделать: "+p.action));
    if(p.opening_note)body.append(el("p","opening",p.opening_note));
    body.append(sourceLink(p.source_url,"Источник места ↗"));list.append(card);
  });
  const warnings=[data.clarification,...(data.warnings||[])].filter(Boolean);
  $("status").classList.remove("error");
  $("status").textContent=warnings.join(" ")||(!lastRecommendations.length?"Подходящих мест пока нет. Попробуйте другую локацию или больше времени.":"");
  const plan=data.plan;$("plan-summary").hidden=!plan||!lastRecommendations.length;
  if(plan)$("plan-summary").textContent="≈ "+plan.total_minutes+" мин всего · "+plan.walk_minutes+" мин пешком · "+plan.visit_minutes+" мин в местах · возврат к старту учтён";
  $("result-mode").textContent=String(data.mode).startsWith("llm-constrained")?"RAG + LLM · ВЫБОР ФОРМУЛИРОВОК":"ПОИСК + ШАБЛОННЫЙ ОТВЕТ";
  const sources=$("sources");sources.replaceChildren();
  sources.append(el("p",null,"Использованы: введённые координаты, запрос, время прогулки и выбранное время суток. Часы и свойства мест требуют проверки. Сгенерированная идея действия не подтверждает наличие услуги."));
  for(const s of data.sources||[])if(s&&typeof s==="object")sources.append(sourceLink(s.source_url||s.url,s.name||s.title||s.id||"Источник"));
  $("source-details").hidden=false;drawMap();
}
$("origin").addEventListener("change",()=>{$("coordinates").hidden=$("origin").value!=="custom";lastRecommendations=[];drawMap();});
for(const button of document.querySelectorAll("[data-scenario]"))button.addEventListener("click",()=>{
  const preset=scenarios[button.dataset.scenario];$("query").value=preset.query;$("minutes").value=preset.minutes;$("origin").value=preset.origin;$("coordinates").hidden=true;
  for(const other of document.querySelectorAll("[data-scenario]"))other.classList.toggle("active",other===button);
  lastRecommendations=[];drawMap();
});
$("planner-form").addEventListener("submit",async(event)=>{
  event.preventDefault();const requestId=++requestCounter;const button=$("submit-button");button.disabled=true;button.firstElementChild.textContent="Подбираем места…";
  $("status").textContent="Сопоставляем интересы, расстояния и ваш запас времени…";$("status").classList.remove("error");
  $("results").replaceChildren();$("plan-summary").hidden=true;$("source-details").hidden=true;
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),45000);
  try{
    const response=await fetch("/api/recommend",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:$("query").value,minutes:Number($("minutes").value),origin:originValue(),time_of_day:$("time-of-day").value}),signal:controller.signal});
    const data=await response.json();if(!response.ok)throw new Error(typeof data.error==="string"?data.error:"Не удалось обработать запрос.");
    if(requestId===requestCounter)renderRecommendations(data);
  }catch(error){$("status").classList.add("error");$("status").textContent=error.name==="AbortError"?"Запрос занял слишком много времени. Попробуйте снова.":error.message;lastRecommendations=[];drawMap();}
  finally{clearTimeout(timer);button.disabled=false;button.firstElementChild.textContent="Найти мою прогулку";}
});
async function initialize(){
  drawMap();try{
    const response=await fetch("/api/places");if(!response.ok)throw new Error("Данные недоступны");const data=await response.json();places=data.places||[];
    const dataset=data.dataset||{};const isDemo=dataset.is_demo!==false;
    $("dataset-badge").textContent=isDemo?"Учебные POI · "+places.length+" мест":"OpenStreetMap · "+places.length+" мест";
    $("dataset-badge").title=JSON.stringify(dataset);drawMap();
  }catch{$("dataset-badge").textContent="Нет связи с сервером";$("status").classList.add("error");$("status").textContent="Запустите сервер: python -m app.server. Затем обновите страницу.";}
}
initialize();
