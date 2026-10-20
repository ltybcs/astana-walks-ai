"use strict";
/* Offline DOM/fetch/geolocation fixtures only. No browser, network, GPS or model calls. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/google-lab/index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "web/google-lab/lab.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web/google-lab/lab.css"), "utf8");
const copy = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag="div") { this.tagName=tag.toUpperCase(); this.children=[];this.attributes={};this.listeners={};this.dataset={};this.hidden=false;this.disabled=false;this.value="";this._text="";this.parentNode=null;this.className=""; }
  set textContent(value) { this._text=String(value);this.children=[]; }
  get textContent() { return this._text+this.children.map(child=>child.textContent).join(""); }
  append(...children) { for(const child of children){child.parentNode=this;this.children.push(child);} }
  replaceChildren(...children) { this.children=[];this._text="";this.append(...children); }
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key] ?? null;}
  removeAttribute(key){delete this.attributes[key];delete this[key];}
  addEventListener(name,handler){(this.listeners[name] ||= []).push(handler);}
  async emit(name,event={}){await Promise.all((this.listeners[name]||[]).map(fn=>fn({preventDefault(){},...event})));}
  focus(options){this.ownerDocument.activeElement=this;this.focusOptions=options;}
  scrollIntoView(options){this.scrollCalls=(this.scrollCalls||0)+1;this.scrollOptions=options;}
}
const context = (extra={}) => ({
  origin:{lat:51.0892,lon:71.4074,label:"MEGA Silk Way"},origin_id:"mega",
  origin_source:"query_landmark",minutes:45,budget_options:[45],
  minutes_source:"query",time_of_day:"day",time_of_day_source:"client_current_time",
  assumptions:[],sensitive_origin:false,...extra
});
const stop = (provider="osm", index=0) => ({
  stop_id:provider+":"+index,source_id:provider==="osm"?"o0":provider==="dgis"?"d0":"g0",
  provider,name:"Парк "+provider,category:"park",lat:51.09+index*.001,lon:71.41,
  source_url:provider==="osm"?"https://www.openstreetmap.org/way/1":provider==="dgis"?"https://2gis.kz/astana/firm/1":"https://www.google.com/maps/place/test",
  visit_minutes:15
});
const option = (minutes=45, extra={}) => ({
  id:"t"+minutes,budget_minutes:minutes,resolved_context:context({minutes}),route_built:true,budget_status:"fits",
  plan:{budget_minutes:minutes,total_minutes:minutes-3,walk_minutes:22,visit_minutes:15,buffer_minutes:5,return_walk_minutes:11,verified:true,returns_to_origin:true,
    route_attributions:[{title:"Google Maps routes",url:"https://www.google.com/maps/"}],legs:[]},
  stops:[stop()],map_ticket:"ticket-"+minutes,...extra
});
const result = (extra={}) => ({
  mode:"llm-grounded-unified",resolved_context:context(),
  answer:{text:"Целый ответ модели.\nВсе предложения остаются вместе.",source_ids:["g0","o0","d0"],stop_ids:["osm:0"]},
  grounding:{summary:"Полный исходный ответ Google. Ничего не сокращено.",sources:[
    {source_id:"g0",id:"places/one",title:"Точное название Google",url:"https://www.google.com/maps/place/one",attribution:{title:"Google Maps",url:"https://www.google.com/maps/"}},
    {source_id:"g1",id:"places/two",title:"Второй источник",url:"https://maps.google.com/?cid=2",attribution:{title:"Google Maps"}}
  ]},
  sources:{osm:[{...stop(),id:"1"}],dgis:[{...stop("dgis"),id:"2"}]},
  source_receipt:{osm:{status:"searched",candidate_count:3,selected_count:1},google:{status:"searched",candidate_count:2,selected_count:0},dgis:{status:"searched",candidate_count:1,selected_count:0}},
  alternatives:[option()],default_option_id:"t45",warnings:["Часы не подтверждены."],diagnostics:{llm_attempts:1,llm_status:"accepted"},...extra
});
function mapReply() {
  return {url:"https://www.google.com/maps/embed/v1/directions?key=PUBLIC_RESTRICTED_FIXTURE&origin=51.0892%2C71.4074&destination=51.0892%2C71.4074&mode=walking&waypoints=51.09%2C71.41",expires_in:600};
}
function harness(config={}) {
  const elements={};
  for(const match of html.matchAll(/<([a-z0-9-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)){
    const element=new Element(match[1]);element.hidden=/\bhidden\b/.test(match[2]);elements[match[3]]=element;
  }
  const document={activeElement:null,createElement:tag=>{const el=new Element(tag);el.ownerDocument=document;return el;},getElementById:id=>{if(!elements[id])throw Error("Missing element "+id);return elements[id];},addEventListener(){}};
  for(const element of Object.values(elements)) element.ownerDocument=document;
  document.body=new Element("body");document.activeElement=document.body;
  const windowEvents={};const timers=new Map();let timerId=0;const requests=[];const healthRequests=[];const geo=[];let clock="2026-10-20T12:00:00.000Z";
  class FixtureDate extends Date {constructor(...args){super(...(args.length?args:[clock]));}getTimezoneOffset(){return -300;}}
  const navigator=config.noGeo?{}:{geolocation:{getCurrentPosition(success,error,options){geo.push({success,error,options});if(config.geo)config.geo(success,error,options);}}};
  const respond=(data,ok=true,status=200)=>({ok,status,json:async()=>data});
  const fetch=async(url,options)=>{
    const request={url,options,body:options.body===undefined?undefined:JSON.parse(options.body)};
    // Capability reads are independently recorded; original fixtures below still
    // count context/search/map operations without silently ignoring the GET.
    if(url==="/api/health"){
      healthRequests.push(request);
      return typeof config.health==="function"?config.health(request,respond)
        : respond(config.health===undefined?{context_preparation:true}:config.health);
    }
    requests.push(request);
    let response;
    if(config.fetch) response=await config.fetch(request,respond);
    else if(url==="/api/context")response=respond({needs_geolocation:false,resolved_context:context()});
    else if(url==="/api/unified/recommend")response=respond(result());
    else if(url==="/api/unified/map")response=respond({code:"route_map_unconfigured",error:"Карта не настроена."},false,503);
    else throw Error("Unexpected offline endpoint "+url);
    // Keep old context fixtures on the current API contract. Explicit malformed
    // values and rawContext fixtures below exercise missing/invalid tokens.
    if(url==="/api/context" && response.ok && !config.rawContext){
      const data=await response.json();
      if(data && typeof data==="object" && !("preparation_token" in data))return respond({...data,preparation_token:"opaque-fixture-token"},response.ok,response.status);
    }
    return response;
  };
  const sandbox={document,window:{addEventListener:(name,fn)=>{(windowEvents[name] ||= []).push(fn);}},navigator,fetch,URL,AbortController,Date:FixtureDate,
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id),console};
  const vmContext=vm.createContext(sandbox);vm.runInContext(source,vmContext,{filename:"lab.js"});vm.runInContext("boot()",vmContext);
  return {elements,requests,healthRequests,geo,timers,document,run:code=>vm.runInContext(code,vmContext),respond,
    clock:value=>clock=value,
    async submit(query="У меня 45 минут возле Mega, хочу прогулку"){elements["walk-query"].value=query;return elements["walk-form"].emit("submit");},
    async edit(query){elements["walk-query"].value=query;await elements["walk-query"].emit("input");},
    async pagehide(){for(const fn of windowEvents.pagehide||[])fn();await flush();},
    fire(ms){for(const [id,timer]of [...timers])if(timer.ms===ms){timers.delete(id);timer.fn();}},
    calls:endpoint=>[...healthRequests,...requests].filter(request=>request.url===endpoint),
    frame:()=>elements["route-map-host"].children[0]
  };
}
const cases=[];
function test(name,fn){cases.push({name,fn});}
test("initial page has no external fetch or iframe",()=>{const h=harness();assert.equal(h.requests.length,0);assert.equal(h.frame(),undefined);});
test("only one textarea and one submit, no manual controls",()=>{assert.equal((html.match(/<textarea\b/g)||[]).length,1);assert.equal((html.match(/type="submit"/g)||[]).length,1);assert(!/<select\b|type="number"|walk-provider/.test(html));});
test("initial textarea is empty and example is only placeholder",()=>{assert(/<textarea[^>]*placeholder="[^"]*45[^"]*"[^>]*><\/textarea>/.test(html));});
test("no catalog, Leaflet, old embed config or initial map",()=>{assert(!/leaflet|embeds\.json|catalog-browser|tab-osm|tab-google/.test(html+source));assert.equal((html.match(/id="route-map-host"/g)||[]).length,1);});
test("complete provider consent lives in initially available footer terms",()=>{const terms=html.match(/<footer\b[^>]*>([\s\S]*?)<\/footer>/);assert(terms);assert(terms[1].includes('id="usage-terms"'));assert(terms[1].includes('id="walk-consent"'));assert(terms[1].includes("Google и 2ГИС"));assert(!/<details[^>]*id="usage-terms"[^>]*\bopen\b/.test(html));});
test("footer terms explain bounded GPS and strict start before any request",()=>{const terms=html.slice(html.indexOf('id="usage-terms"'));assert(terms.includes("до 8 секунд"));assert(terms.includes("Подбор мест и расчёт маршрута начинаются после определения старта"));assert(terms.includes("Исходное сообщение"));assert(!html.includes("подписанный пример"));const h=harness();assert.equal(h.requests.length,0);assert.equal(h.geo.length,0);});
test("no storage, history mutation or raw HTML sinks",()=>{assert(!/localStorage|sessionStorage|indexedDB|innerHTML|outerHTML|pushState|replaceState|document\.cookie/.test(source));});
test("mobile input is 16px and all buttons have 44px targets",()=>{assert(css.includes("min-height:44px"));assert(css.includes("textarea{font-size:16px}"));});
test("empty query performs zero context or provider calls",async()=>{const h=harness();await h.submit("  ");assert.equal(h.requests.length,0);assert(!h.elements["walk-error"].hidden);});
test("query-only context resolves before one unified search",async()=>{const h=harness();await h.submit();assert.deepEqual(h.requests.map(v=>v.url),["/api/context","/api/unified/recommend","/api/unified/map"]);assert.equal(h.calls("/api/unified/recommend").length,1);});
test("request contains explicit combined consent",async()=>{const h=harness();await h.submit();assert.equal(h.calls("/api/unified/recommend")[0].body.consent,true);assert.equal(h.calls("/api/context")[0].body.consent,true);});
test("one frozen clock and correct UTC offset are reused",async()=>{const h=harness();await h.submit();const a=h.requests[0].body.client_context,b=h.requests[1].body.client_context;assert.deepEqual(a,b);assert.equal(a.instant,"2026-10-20T12:00:00.000Z");assert.equal(a.utc_offset_minutes,300);});
test("ordinary text origin never requests browser GPS",async()=>{const h=harness();await h.submit();assert.equal(h.geo.length,0);assert(!("geolocation" in h.calls("/api/unified/recommend")[0].body));});
test("whole model answer remains verbatim",async()=>{const h=harness();await h.submit();assert.equal(h.elements["walk-answer"].textContent,result().answer.text);});
test("every Google source preview stays adjacent to whole answer inside one disclosure",async()=>{const h=harness();await h.submit();assert.equal(h.elements["google-sources"].children.length,3);assert(h.elements["google-sources"].textContent.includes("Второй источник"));const explanation=html.match(/<details[^>]*id="walk-explanation"[^>]*>([\s\S]*?)<\/details>/);assert(explanation);assert(/<p[^>]*id="walk-answer"[^>]*><\/p>\s*<div[^>]*id="google-sources"/.test(explanation[1]));});
test("Google source references preserve zero index and exact source ID",async()=>{const data=result();data.grounding.sources=Array.from({length:5},(_,index)=>({source_id:"g"+index,id:"places/"+index,title:"Google title "+index,url:"https://www.google.com/maps/place/"+index}));data.answer.text="Смотрите источник [g2], исходная ссылка [2].";const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(h.elements["google-sources"].children[3].textContent.includes("[2] / g2"));assert.equal(h.elements["walk-answer"].textContent,data.answer.text);});
test("one disclosure exposes whole original Google summary and all original previews",async()=>{const h=harness();await h.submit();assert.equal(h.elements["google-original-text"].textContent,result().grounding.summary);assert.equal(h.elements["google-original"].hidden,false);assert.equal(h.elements["google-original-sources"].children.length,3);const explanation=html.match(/<details[^>]*id="walk-explanation"[^>]*>([\s\S]*?)<\/details>/);assert(explanation);assert(!/<details\b/.test(explanation[1]));assert(/<section[^>]*id="google-original"/.test(explanation[1]));});
test("OSM and 2GIS evidence preserves exact citation IDs",async()=>{const h=harness();await h.submit();assert(h.elements["other-sources"].textContent.includes("[o0]"));assert(h.elements["other-sources"].textContent.includes("[d0]"));});
test("all three source receipts expose actual counts",async()=>{const h=harness();await h.submit();const text=h.elements["source-receipts"].textContent;assert(text.includes("OpenStreetMap"));assert(text.includes("2ГИС"));assert(text.includes("Найдено: 3"));});
test("successful empty source is labelled no data",async()=>{const data=result();data.source_receipt.dgis={status:"no_data",candidate_count:0,selected_count:0};const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(h.elements["source-receipts"].textContent.includes("Подходящих данных нет"));});
test("missing provider receipt rejects apparent combined success",async()=>{const data=result();delete data.source_receipt.dgis;const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:data)});await h.submit();assert(h.elements["walk-result"].hidden);assert(h.elements["walk-error"].textContent.includes("трёх источников"));assert.equal(h.calls("/api/unified/map").length,0);});
test("provider error has no synthesized or hidden fallback result",async()=>{const h=harness({fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:context()}):r({error:"2ГИС недоступен.",code:"dgis_unavailable"},false,503)});await h.submit();assert(h.elements["walk-result"].hidden);assert(h.elements["walk-error"].textContent.includes("2ГИС"));assert.equal(h.requests.length,2);});
test("grounding-only mode shows full summary without fake AI label",async()=>{const data=result({mode:"grounding-only",answer:null});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert.equal(h.elements["walk-answer"].textContent,data.grounding.summary);assert.equal(h.elements["walk-mode"].textContent,"Без ответа модели");assert(h.elements["google-original"].hidden);});
test("unverified or unfit option does not render a route map",async()=>{const data=result({alternatives:[option(45,{route_built:false,budget_status:"unverified",map_ticket:null,stops:[]})]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:data)});await h.submit();assert.equal(h.calls("/api/unified/map").length,0);assert(h.elements["route-map-section"].hidden);assert(h.elements["walk-budget"].textContent.includes("не построен"));});
test("route must fit and include verified return",()=>{const h=harness();for(const patch of [{budget_status:"does_not_fit"},{plan:{...option().plan,verified:false}},{plan:{...option().plan,returns_to_origin:false}},{plan:{...option().plan,total_minutes:46}}])assert.throws(()=>h.run("validateResult("+JSON.stringify(result({alternatives:[option(45,patch)]}))+")"));});
test("source text is rendered without HTML interpretation",async()=>{const data=result();data.answer.text="<img src=x onerror=alert(1)>";data.grounding.sources[0].title="<script>bad</script>";const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert.equal(h.elements["walk-answer"].textContent,data.answer.text);assert.equal(h.elements["walk-answer"].children.length,0);});
test("known start check rejects null and sample origins",()=>{const h=harness();for(const ctx of [null,context({origin:null}),context({is_city_sample:true}),context({origin_source:"city_sample"}),context({origin_source:"pending_geolocation"})])assert.equal(h.run("startKnown("+JSON.stringify(ctx)+")"),false);});
test("unknown address context error causes zero provider search",async()=>{const h=harness({fetch:(req,r)=>r({code:"known_start_required",error:"Неизвестный старт."},false,422)});await h.submit("Гулять у неизвестного адреса");assert.equal(h.calls("/api/unified/recommend").length,0);assert.equal(h.geo.length,0);assert(h.elements["walk-error"].textContent.includes("Подбор мест и расчёт маршрута"));});
test("legacy city-sample response is rejected before search",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:false,resolved_context:context({is_city_sample:true})})});await h.submit();assert.equal(h.requests.length,1);assert(h.elements["walk-result"].hidden);});
test("GPS denied has zero unified search and no sample fallback",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true,resolved_context:context({origin:null})}),geo:(ok,bad)=>bad({code:1})});await h.submit("Хочу прогулку");assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Браузер не разрешил доступ к геолокации."));assert(h.elements["walk-error"].textContent.includes("Подбор мест и расчёт маршрута не выполнены."));});
test("GPS timeout is bounded at eight seconds and never searches",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true})});const pending=h.submit("Хочу гулять");await flush();assert.equal(h.geo[0].options.timeout,8000);h.fire(8000);await pending;assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Браузер не успел определить местоположение за 8 секунд."));assert(!h.elements["walk-submit"].disabled);});
test("unsupported GPS fails closed without search",async()=>{const h=harness({noGeo:true,fetch:(req,r)=>r({needs_geolocation:true})});await h.submit("Хочу гулять");assert.equal(h.requests.length,1);assert(!h.elements["walk-error"].hidden);assert(h.elements["walk-error"].textContent.includes("Этот браузер не поддерживает геолокацию."));assert.equal(h.calls("/api/unified/recommend").length,0);});
test("invalid coarse accuracy is not sent to backend",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true}),geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:200000}})});await h.submit("Хочу гулять");assert.equal(h.requests.length,1);});
test("valid GPS is gated again and uses granted status",async()=>{let probes=0;const h=harness({geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:20}}),fetch:(req,r)=>req.url==="/api/context"?r(++probes===1?{needs_geolocation:true}:{resolved_context:context({origin_source:"browser_geolocation"})}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});await h.submit("Хочу гулять");assert.equal(probes,2);const req=h.calls("/api/unified/recommend")[0];assert.equal(req.body.location_status,"granted");assert.deepEqual(req.body.geolocation,{lat:51.1,lon:71.4,accuracy:20});assert.equal(h.geo[0].options.maximumAge,0);});
test("GPS callback after clock change retains original submit snapshot",async()=>{let probes=0;const h=harness({fetch:(req,r)=>req.url==="/api/context"?r(++probes===1?{needs_geolocation:true}:{resolved_context:context({origin_source:"browser_geolocation"})}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});const pending=h.submit("Хочу гулять");await flush();h.clock("2026-10-20T13:00:00.000Z");h.geo[0].success({coords:{latitude:51.1,longitude:71.4,accuracy:20}});await pending;for(const req of h.requests.filter(value=>value.url!=="/api/unified/map"))assert.equal(req.body.client_context.instant,"2026-10-20T12:00:00.000Z");});
test("outside-area second context fails before all providers",async()=>{let probes=0;const h=harness({geo:ok=>ok({coords:{latitude:40,longitude:70,accuracy:20}}),fetch:(req,r)=>++probes===1?r({needs_geolocation:true}):r({code:"known_start_required",error:"За пределами области."},false,422)});await h.submit("Хочу гулять");assert.equal(h.requests.length,2);assert.equal(h.calls("/api/unified/recommend").length,0);});
test("edited input ignores late geolocation callback",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true})});const pending=h.submit("Хочу гулять");await flush();await h.edit("Другое сообщение");h.geo[0].success({coords:{latitude:51.1,longitude:71.4,accuracy:1}});await pending;assert.equal(h.requests.length,1);assert(h.elements["walk-result"].hidden);});
test("GPS is not retained in application state",async()=>{const h=harness();await h.submit();assert(!Object.keys(h.run("state")).some(key=>/geo(location)?$|payload|query/i.test(key)));});
test("edited query aborts in-flight provider and ignores late result",async()=>{const d=deferred();const h=harness({fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:context()}):d.promise});const pending=h.submit();await flush();const request=h.calls("/api/unified/recommend")[0];await h.edit("Новый запрос");assert(request.options.signal.aborted);d.resolve(h.respond(result()));await pending;assert(h.elements["walk-result"].hidden);assert.equal(h.calls("/api/unified/map").length,0);assert.equal(h.elements["walk-result-title"].scrollCalls||0,0);});
test("old context result cannot start provider after new query",async()=>{const d=deferred();const h=harness({fetch:()=>d.promise});const pending=h.submit();await flush();await h.edit("Другой старт");d.resolve(h.respond({resolved_context:context()}));await pending;assert.equal(h.requests.length,1);});
test("duplicate change event preserves current ready result",async()=>{const h=harness();await h.submit();await h.elements["walk-query"].emit("change");assert.equal(h.elements["walk-result"].hidden,false);});
test("pagehide cancels pending flow and unlocks BFCache form",async()=>{const d=deferred();const h=harness({fetch:()=>d.promise});const pending=h.submit();await flush();assert(h.elements["walk-submit"].disabled);await h.pagehide();assert(!h.elements["walk-submit"].disabled);d.resolve(h.respond({resolved_context:context()}));await pending;assert.equal(h.requests.length,1);assert(h.elements["walk-result"].hidden);});
test("overall deadline is 210 seconds and resets UI on abort",async()=>{const d=deferred();const h=harness({fetch:()=>d.promise});const pending=h.submit();await flush();h.fire(210000);assert(h.requests[0].options.signal.aborted);d.resolve(h.respond({resolved_context:context()}));await pending;assert(h.elements["walk-error"].textContent.includes("мог выполниться"));assert(!h.elements["walk-submit"].disabled);});
test("malformed JSON is a safe visible failure",async()=>{const h=harness({fetch:()=>({ok:false,json:async()=>{throw Error("raw private error");}})});await h.submit();assert(h.elements["walk-error"].textContent.includes("нечитаемый"));assert(!h.elements["walk-error"].textContent.includes("raw private"));});
test("ready variants switch without another search or model call",async()=>{const data=result({alternatives:[option(60),option(120),option(240)],default_option_id:"t120"});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();const count=h.calls("/api/unified/recommend").length;h.run("selectOption('t240')");await flush();assert.equal(h.calls("/api/unified/recommend").length,count);assert.equal(h.calls("/api/unified/map").length,2);assert(h.elements["walk-budget"].textContent.includes("240"));});
test("selected variant does not rewrite whole answer or sources",async()=>{const data=result({alternatives:[option(60),option(120)],default_option_id:"t60"});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();const text=h.elements["walk-answer"].textContent;h.run("selectOption('t120')");assert.equal(h.elements["walk-answer"].textContent,text);assert.equal(h.elements["google-sources"].children.length,3);});
test("same selected variant does not resolve map twice",async()=>{const h=harness();await h.submit();h.run("selectOption('t45')");await flush();assert.equal(h.calls("/api/unified/map").length,1);});
test("map unavailable remains honest and external walking link is ready",async()=>{const h=harness();await h.submit();await flush();assert.equal(h.frame(),undefined);assert(h.elements["route-map-status"].textContent.includes("недоступна"));const url=new URL(h.elements["route-external"].href);assert.equal(url.searchParams.get("origin"),url.searchParams.get("destination"));assert.equal(url.searchParams.get("travelmode"),"walking");});
test("valid map creates exactly one Google directions iframe after plan",async()=>{const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?result():mapReply())});await h.submit();await flush();assert.equal(h.elements["route-map-host"].children.length,1);assert.equal(h.frame().tagName,"IFRAME");assert.equal(h.frame().getAttribute("referrerpolicy"),"origin");assert(h.frame().src.startsWith("https://www.google.com/maps/embed/v1/directions?"));});
test("map resolves only RAM ticket, not query or coordinates",async()=>{const h=harness();await h.submit();assert.deepEqual(h.calls("/api/unified/map")[0].body,{ticket:"ticket-45"});});
test("edit removes existing iframe and external precise route",async()=>{const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?result():mapReply())});await h.submit();await flush();assert(h.frame());await h.edit("Новое сообщение");assert.equal(h.frame(),undefined);assert(!h.elements["route-external"].href);assert.equal(h.run("state.result"),null);});
test("late map after edit cannot recreate iframe",async()=>{const d=deferred();const h=harness({fetch:(req,r)=>req.url==="/api/unified/map"?d.promise:r(req.url==="/api/context"?{resolved_context:context()}:result())});await h.submit();await h.edit("Новый запрос");d.resolve(h.respond(mapReply()));await flush();assert.equal(h.frame(),undefined);});
test("variant switch ignores old map and keeps one new iframe",async()=>{const first=deferred();let maps=0;const data=result({alternatives:[option(60),option(120)],default_option_id:"t60"});const h=harness({fetch:(req,r)=>req.url==="/api/unified/map"?(++maps===1?first.promise:r(mapReply())):r(req.url==="/api/context"?{resolved_context:context()}:data)});await h.submit();h.run("selectOption('t120')");await flush();const frame=h.frame();assert(frame);first.resolve(h.respond(mapReply()));await flush();assert.equal(h.frame(),frame);assert.equal(h.elements["route-map-host"].children.length,1);});
test("pagehide removes existing map and clears accepted answer",async()=>{const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?result():mapReply())});await h.submit();await flush();await h.pagehide();assert.equal(h.frame(),undefined);assert.equal(h.elements["walk-answer"].textContent,"");assert.equal(h.run("state.result"),null);});
test("map display expires without automatic new network request",async()=>{const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?result():mapReply())});await h.submit();await flush();const count=h.requests.length;h.fire(600000);assert.equal(h.frame(),undefined);assert.equal(h.requests.length,count);assert(h.elements["route-map-status"].textContent.includes("завершён"));});
test("slow iframe receives bounded twelve second status without retry",async()=>{const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?result():mapReply())});await h.submit();await flush();h.fire(12000);assert(h.elements["route-map-status"].textContent.includes("медленно"));assert.equal(h.requests.length,3);});
test("old map load event cannot overwrite new request status",async()=>{const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?result():mapReply())});await h.submit();await flush();const old=h.frame();await h.edit("Новый");await old.emit("load");assert.equal(h.elements["route-map-status"].textContent,"");});
test("arbitrary, Share-pb and credential map URLs are rejected",()=>{const h=harness();for(const url of ["javascript:alert(1)","https://evil.example/maps/embed/v1/directions","https://www.google.com/maps/embed?pb=123","https://x@www.google.com/maps/embed/v1/directions?key=x","https://www.google.com:443/maps/embed/v1/directions?key=x#secret"])assert.equal(h.run("validateMapUrl("+JSON.stringify(url)+")"),null);});
test("map validation requires walking, return and ordered stops",()=>{const h=harness();const good=mapReply().url;assert(h.run("validateMapUrl("+JSON.stringify(good)+")"));for(const url of [good.replace("mode=walking","mode=driving"),good.replace("destination=51.0892%2C71.4074","destination=other"),good.replace("&waypoints=51.09%2C71.41",""),good+"&origin=duplicate"])assert.equal(h.run("validateMapUrl("+JSON.stringify(url)+")"),null);});
test("source URLs reject scripts, credentials and unrelated Google paths",()=>{const h=harness();for(const [url,provider]of [["javascript:alert(1)","osm"],["https://evil.example","dgis"],["https://user@2gis.kz/astana","dgis"],["https://www.google.com/search?q=x","google"],["http://www.openstreetmap.org/way/1","osm"]])assert.equal(h.run("safeUrl("+JSON.stringify(url)+","+JSON.stringify(provider)+")"),null);});
test("official attribution HTTP exception is only dev.2gis.ru",()=>{const h=harness();assert.equal(h.run("safeUrl('http://dev.2gis.ru','dgis-attribution')"),"http://dev.2gis.ru/");assert.equal(h.run("safeUrl('http://dev.2gis.ru.evil/','dgis-attribution')"),null);});
test("ordered external route includes every stop and roundtrip",()=>{const h=harness();const value=h.run("directionsUrl("+JSON.stringify(context())+","+JSON.stringify([stop("osm",0),stop("dgis",1),stop("google",2)])+")");const url=new URL(value);assert.equal(url.searchParams.get("api"),"1");assert.equal(url.searchParams.get("waypoints"),[stop("osm",0),stop("dgis",1),stop("google",2)].map(value=>value.lat+","+value.lon).join("|"));assert.equal(url.searchParams.get("origin"),url.searchParams.get("destination"));});
test("map for missing ticket never performs network request",async()=>{const data=result({alternatives:[option(45,{map_ticket:null})]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:data)});await h.submit();assert.equal(h.calls("/api/unified/map").length,0);assert(h.elements["route-map-status"].textContent.includes("пока недоступна"));});
test("invalid default variant rejects result instead of guessed choice",()=>{const h=harness();assert.throws(()=>h.run("validateResult("+JSON.stringify(result({default_option_id:"missing"}))+")"));});
test("oversized or duplicate variants are rejected",()=>{const h=harness();for(const alternatives of [[option(),option()],[option(30),option(40),option(50),option(60)]])assert.throws(()=>h.run("validateResult("+JSON.stringify(result({alternatives}))+")"));});
test("source absence cannot be reported with fabricated no_data count",()=>{const h=harness();const data=result();data.source_receipt.dgis={status:"no_data",candidate_count:4,selected_count:0};assert.throws(()=>h.run("validateResult("+JSON.stringify(data)+")"));});
test("fetch policy is no-store, same-origin and rejects redirects",async()=>{const h=harness();await h.submit();for(const request of [...h.healthRequests,...h.requests]){assert.equal(request.options.cache,"no-store");assert.equal(request.options.credentials,"same-origin");assert.equal(request.options.redirect,"error");}});
test("accepted current result focuses then scrolls to answer without animation",async()=>{const h=harness();await h.submit();const heading=h.elements["walk-result-title"];assert.equal(h.document.activeElement,heading);assert.equal(heading.focusOptions.preventScroll,true);assert.equal(heading.scrollCalls,1);assert.deepEqual(copy(heading.scrollOptions),{block:"start",behavior:"auto"});});
test("user editing clears all source previews and raw response from UI state",async()=>{const h=harness();await h.submit();await h.edit("Изменено");assert.equal(h.elements["google-sources"].children.length,0);assert.equal(h.elements["other-sources"].children.length,0);assert.equal(h.elements["google-original-text"].textContent,"");assert.equal(h.run("state.result"),null);});
test("obsolete late response never scrolls again after a newer accepted answer",async()=>{const old=deferred();let recommends=0;const fresh=result({answer:{text:"Новый принятый ответ.",source_ids:["o0"],stop_ids:["osm:0"]}});const h=harness({fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:context()}):req.url==="/api/unified/recommend"?(++recommends===1?old.promise:r(fresh)):r({},false,503)});const pending=h.submit("Первое сообщение у Mega на 45 минут");await flush();await h.edit("Новое сообщение у Mega на 45 минут");await h.submit("Новое сообщение у Mega на 45 минут");assert.equal(h.elements["walk-result-title"].scrollCalls,1);old.resolve(h.respond(result()));await pending;assert.equal(h.elements["walk-result-title"].scrollCalls,1);assert.equal(h.elements["walk-answer"].textContent,fresh.answer.text);});
test("strict start error never focuses or scrolls to hidden answer",async()=>{const h=harness({fetch:(req,r)=>r({code:"known_start_required",error:"Старт не определён."},false,422)});await h.submit("Неизвестный адрес");assert.equal(h.elements["walk-result-title"].scrollCalls||0,0);assert.notEqual(h.document.activeElement,h.elements["walk-result-title"]);assert(h.elements["walk-result"].hidden);});

test("main form stays compact while detailed rules are available in the footer",()=>{const form=html.match(/<form[^>]*id="walk-form"[^>]*>([\s\S]*?)<\/form>/);assert(form);assert(!form[1].includes("Без указанного времени"));assert(!form[1].includes("до 8 секунд"));assert(!form[1].includes("Исходное сообщение"));const terms=html.slice(html.indexOf('id="usage-terms"'));assert(terms.includes("Без указанного времени"));assert(terms.includes("геопозици"));});
test("route and map precede one directly expandable explanation",()=>{const details=html.indexOf('id="walk-explanation"');assert(html.indexOf('id="walk-stops"')<details);assert(html.indexOf('id="route-map-section"')<details);assert.equal((html.match(/id="walk-explanation"/g)||[]).length,1);const content=html.match(/<details[^>]*id="walk-explanation"[^>]*>([\s\S]*?)<\/details>/)[1];assert(content.indexOf('id="walk-answer"')<content.indexOf('id="google-sources"'));assert(content.indexOf('id="google-sources"')<content.indexOf('id="google-original-text"'));assert(content.indexOf('id="google-original-text"')<content.indexOf('id="google-original-sources"'));assert(!/<details\b/.test(content));});
test("source assumptions and warnings are preserved in terms without crowding context",async()=>{const assumption="Текущее время определено автоматически.";const data=result({resolved_context:context({assumptions:[assumption]}),warnings:[assumption,"Часы не подтверждены."]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(!h.elements["walk-context"].textContent.includes(assumption));assert(h.elements["walk-warnings"].textContent.includes(assumption));assert(h.elements["walk-warnings"].textContent.includes("Часы не подтверждены."));assert.equal(h.elements["walk-warnings"].children.filter(item=>item.textContent===assumption).length,1);assert(html.slice(html.indexOf('id="usage-terms"')).includes('id="walk-warnings"'));});
test("stop why text is displayed exactly as plain text rather than interpreted markup",async()=>{const why="<img src=x onerror=alert(1)> Категория подтверждена источником.";const data=result({alternatives:[option(45,{stops:[{...stop(),why_text:why}]})]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();const descend=el=>[el,...el.children.flatMap(descend)];const reasons=descend(h.elements["walk-stops"]).filter(el=>el.className==="stop-why");assert.equal(reasons.length,1);assert.equal(reasons[0].textContent,why);assert.equal(reasons[0].children.length,0);assert(!descend(h.elements["walk-stops"]).some(el=>el.tagName==="IMG"));});
test("missing or invalid stop why text does not invent an explanation",async()=>{for(const why of [undefined,null,{},42,""]){const data=result({alternatives:[option(45,{stops:[{...stop(),why_text:why}]})]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(!h.elements["walk-stops"].textContent.includes("Почему выбрано"));assert(!h.elements["walk-stops"].textContent.includes("[object Object]"));assert(!h.elements["walk-result"].hidden);}});
test("ready variant changes its reason without repeating search or model request",async()=>{const first="Первая проверенная причина.",second="Причина другого готового варианта.";const data=result({alternatives:[option(45,{stops:[{...stop(),why_text:first}]}),option(60,{stops:[{...stop("dgis"),why_text:second}]})]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(h.elements["walk-stops"].textContent.includes(first));await h.elements["walk-variants"].children[1].emit("click");await flush();assert(h.elements["walk-stops"].textContent.includes(second));assert(!h.elements["walk-stops"].textContent.includes(first));assert.equal(h.calls("/api/unified/recommend").length,1);assert.equal(h.calls("/api/context").length,1);});
test("editing clears reasons, closes response disclosures and hides old warnings",async()=>{const data=result({alternatives:[option(45,{stops:[{...stop(),why_text:"Причина прошлого результата."}]})]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();h.elements["walk-explanation"].open=true;h.elements["source-receipt-details"].open=true;await h.edit("Новый запрос");assert.equal(h.elements["walk-explanation"].open,false);assert.equal(h.elements["source-receipt-details"].open,false);assert.equal(h.elements["walk-stops"].textContent,"");assert.equal(h.elements["walk-warnings"].textContent,"");assert(h.elements["walk-warnings"].hidden);assert.equal(h.elements["google-original-text"].textContent,"");});

test("native GPS unavailable has a distinct reason and performs no source search",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true}),geo:(ok,bad)=>bad({code:2})});await h.submit("Хочу прогулку");assert(h.elements["walk-error"].textContent.includes("Браузер не смог определить местоположение."));assert(h.elements["walk-error"].textContent.includes("Подбор мест и расчёт маршрута не выполнены."));assert.equal(h.requests.length,1);assert.equal(h.calls("/api/unified/recommend").length,0);assert.equal(h.calls("/api/unified/map").length,0);assert(!h.elements["walk-submit"].disabled);});
test("specific static server start errors remain plain text and never start provider search",async()=>{for(const message of ["Геопозиция находится вне Астаны.","Адрес старта не найден.","<b>Неизвестный адрес</b>"]){const h=harness({fetch:(req,r)=>r({code:"known_start_required",error:message},false,422)});await h.submit("Синтетический неизвестный старт");assert.equal(h.elements["walk-error"].textContent,message+" Подбор мест и расчёт маршрута не выполнены.");assert.equal(h.elements["walk-error"].children.length,0);assert.equal(h.requests.length,1);assert.equal(h.calls("/api/unified/recommend").length,0);assert.equal(h.geo.length,0);}});
test("known-start error without reason keeps a stable static fallback",()=>{const h=harness();assert.equal(h.run("errorMessage({code:'known_start_required'})"),"Старт не определён. Подбор мест и расчёт маршрута не выполнены.");assert.equal(h.requests.length,0);assert.equal(h.geo.length,0);});
test("valid browser coordinates outside Astana are rejected by the second gate without search",async()=>{let probes=0;const message="Геопозиция находится вне Астаны.";const h=harness({geo:ok=>ok({coords:{latitude:55.7,longitude:37.6,accuracy:20}}),fetch:(req,r)=>req.url==="/api/context"?r(++probes===1?{needs_geolocation:true}:{code:"known_start_required",error:message},probes===1,probes===1?200:422):r({},false,500)});await h.submit("Хочу кофе");assert.equal(probes,2);assert.equal(h.requests[1].body.location_status,"granted");assert.equal(h.requests[1].body.geolocation.lat,55.7);assert.equal(h.calls("/api/unified/recommend").length,0);assert.equal(h.calls("/api/unified/map").length,0);assert(h.elements["walk-error"].textContent.includes(message));assert(h.elements["walk-result"].hidden);});

test("opaque preparation token is forwarded unchanged only after consented context",async()=>{
  const token="opaque:keep $ punctuation / and spaces";
  const h=harness({rawContext:true,fetch:(req,r)=>req.url==="/api/context"?r({preparation_token:token,resolved_context:context()}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});
  await h.submit();const prepare=h.calls("/api/context")[0].body,search=h.calls("/api/unified/recommend")[0].body;
  assert.equal(prepare.consent,true);assert(!("preparation_token" in prepare));assert.equal(search.preparation_token,token);
  assert.equal(search.query,prepare.query);assert.deepEqual(search.client_context,prepare.client_context);
  assert(!h.elements["walk-result"].textContent.includes(token));assert(!Object.keys(h.run("state")).some(key=>/token/i.test(key)));
});
test("GPS resume reuses preparation token and exactly the frozen message and clock",async()=>{
  const token="prepared-before-browser-location";let contexts=0;
  const h=harness({rawContext:true,geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:20}}),fetch:(req,r)=>req.url==="/api/context"?r(++contexts===1?{needs_geolocation:true,preparation_token:token}:{resolved_context:context({origin_source:"browser_geolocation"}),preparation_token:token}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});
  await h.submit("Хочу прогулку на пару часов");const first=h.calls("/api/context")[0].body,second=h.calls("/api/context")[1].body,last=h.calls("/api/unified/recommend")[0].body;
  assert.equal(first.consent,true);assert(!("geolocation" in first));assert(!("preparation_token" in first));
  for(const body of [second,last]){assert.equal(body.preparation_token,token);assert.equal(body.consent,true);assert.equal(body.query,first.query);assert.deepEqual(body.client_context,first.client_context);assert.deepEqual(body.geolocation,{lat:51.1,lon:71.4,accuracy:20});assert.equal(body.location_status,"granted");}
  assert.equal(contexts,2);assert.equal(h.geo.length,1);
});
test("missing or malformed preparation token stops before GPS or provider search",async()=>{
  for(const token of [undefined,null,17,"","   "]){const data={needs_geolocation:true};if(token!==undefined)data.preparation_token=token;
    const h=harness({rawContext:true,fetch:(req,r)=>r(data)});await h.submit("Хочу прогулку");
    assert.equal(h.requests.length,1);assert.equal(h.geo.length,0);assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Подготовка запроса не подтверждена"));assert(!h.elements["walk-submit"].disabled);
  }
});
test("GPS resume must itself return a preparation token before recommendation",async()=>{
  let contexts=0;const h=harness({rawContext:true,geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:20}}),fetch:(req,r)=>r(++contexts===1?{needs_geolocation:true,preparation_token:"first"}:{resolved_context:context()})});
  await h.submit("Хочу прогулку");assert.equal(contexts,2);assert.equal(h.geo.length,1);assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Подготовка запроса"));
});
test("invalid preparation token error is displayed without automatic inference retry",async()=>{
  const h=harness({rawContext:true,fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:context(),preparation_token:"expired-on-server"}):r({code:"invalid_preparation_token",error:"Подготовка запроса истекла."},false,409)});
  await h.submit();assert.equal(h.calls("/api/context").length,1);assert.equal(h.calls("/api/unified/recommend").length,1);assert.equal(h.calls("/api/unified/map").length,0);assert.equal(h.elements["walk-error"].textContent,"Подготовка запроса истекла.");assert(h.elements["walk-result"].hidden);
});
test("new explicit submission gets a new preparation instead of reusing the prior token",async()=>{
  let prepared=0;const h=harness({rawContext:true,fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:context(),preparation_token:"one-use-"+(++prepared)}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});
  await h.submit();await h.submit();const calls=h.calls("/api/unified/recommend");assert.equal(calls.length,2);assert.equal(calls[0].body.preparation_token,"one-use-1");assert.equal(calls[1].body.preparation_token,"one-use-2");for(const call of h.calls("/api/context"))assert(!("preparation_token" in call.body));
});
test("late prepared context after editing cannot request GPS or pass its token forward",async()=>{
  const pendingReply=deferred();const h=harness({rawContext:true,fetch:()=>pendingReply.promise});const pending=h.submit();await flush();await h.edit("Другое сообщение");pendingReply.resolve(h.respond({needs_geolocation:true,preparation_token:"obsolete-token"}));await pending;
  assert.equal(h.requests.length,1);assert.equal(h.geo.length,0);assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-result"].hidden);
});
test("Google-resolved typed start has exact source label and safe reference without GPS override",async()=>{
  const resolved=context({origin_source:"google_places_text_search",origin_reference:"https://www.google.com/maps/place/known-start"});
  const h=harness({rawContext:true,fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:resolved,preparation_token:"google-origin"}):r(req.url==="/api/unified/recommend"?result({resolved_context:resolved}):{},req.url!=="/api/unified/map")});
  await h.submit("Прогулка возле известного нового здания на час");assert.equal(h.geo.length,0);assert.equal(h.calls("/api/unified/recommend").length,1);assert(h.elements["walk-context"].textContent.includes("найдено в Google Maps"));const ref=h.elements["walk-context"].children.find(el=>el.tagName==="A");assert(ref);assert.equal(ref.href,resolved.origin_reference);assert.equal(ref.rel,"noopener noreferrer");
});
test("out-of-city Google-resolved coordinates cannot reach activity search",async()=>{
  const h=harness({rawContext:true,fetch:(req,r)=>r({resolved_context:context({origin_source:"google_places_text_search",origin:{lat:55.7,lon:37.6,label:"Другой город"}}),preparation_token:"outside-city"})});
  await h.submit();assert.equal(h.requests.length,1);assert.equal(h.geo.length,0);assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Подбор мест и расчёт маршрута не выполнены"));
});
test("unknown typed place server error never falls back to GPS or another preparation",async()=>{
  const h=harness({rawContext:true,fetch:(req,r)=>r({code:"known_start_required",error:"Указанное место не удалось подтвердить."},false,422)});
  await h.submit("Хочу прогулку возле неподтверждённого места");assert.equal(h.requests.length,1);assert.equal(h.geo.length,0);assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Указанное место не удалось подтвердить."));
});


test("Google origin preserves up to eight exact provider attributions as safe plain text",async()=>{
  const providers=Array.from({length:9},(_,i)=>({provider:i===0?"<b>Exact third-party attribution</b>":"Provider "+i,providerUri:"https://credits.example/provider/"+i}));
  const data=result({resolved_context:context({origin_source:"google_places_text_search",origin_attributions:providers})});
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
  const links=h.elements["walk-context"].children.filter(el=>el.tagName==="A");assert.equal(links.length,8);assert.equal(links[0].textContent,providers[0].provider);assert.equal(links[0].children.length,0);assert.equal(links[0].href,providers[0].providerUri);assert(!h.elements["walk-context"].textContent.includes("Provider 8"));
});
test("unsafe origin attribution URLs keep their text without becoming links",async()=>{
  const bad=["javascript:alert(1)","http://credits.example/","https://user:secret@credits.example/","https://credits.example:8443/"];
  const resolved=context({origin_source:"google_places_text_search",origin_reference:"https://evil.example/maps",origin_attributions:bad.map((url,i)=>({provider:"Exact provider "+i,providerUri:url}))});
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:resolved}:req.url==="/api/unified/recommend"?result({resolved_context:resolved}):{},req.url!=="/api/unified/map")});await h.submit();
  assert.equal(h.elements["walk-context"].children.filter(el=>el.tagName==="A").length,0);for(let i=0;i<bad.length;i++)assert(h.elements["walk-context"].textContent.includes("Exact provider "+i));assert(!h.elements["walk-context"].textContent.includes("Источник старта"));
});
test("empty attribution array adds no placeholders or separators",async()=>{
  const resolved=context({origin_attributions:[]});const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:resolved}:req.url==="/api/unified/recommend"?result({resolved_context:resolved}):{},req.url!=="/api/unified/map")});await h.submit();assert.equal(h.elements["walk-context"].children.length,0);assert.equal(h.elements["walk-context"].textContent,h.run("contextText(state.result.resolved_context)"));
});
test("unagreed geocoded or guessed origin source cannot reach activity search",async()=>{
  for(const origin_source of ["query_geocoded","model_guessed"]){const h=harness({fetch:(req,r)=>r({resolved_context:context({origin_source})})});await h.submit();assert.equal(h.requests.length,1);assert.equal(h.geo.length,0);assert.equal(h.calls("/api/unified/recommend").length,0);}
});
test("selected stop description is prominent plain text and technical why is closed below it",async()=>{
  const description="<img src=x onerror=alert(1)> Игровая пауза после проверки условий.";const why="Подходит по типу; пеший бюджет проверен.";const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:description,why_text:why}]})]});
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();const content=h.elements["walk-stops"].children[0].children[0];const p=content.children.find(el=>el.className==="stop-description"),details=content.children.find(el=>el.tagName==="DETAILS");assert(p);assert.equal(p.textContent,description);assert.equal(p.children.length,0);assert(details);assert.notEqual(details.open,true);assert.equal(details.children[0].tagName,"SUMMARY");assert.equal(details.children[0].textContent,"Почему выбрано");assert.equal(details.children[1].textContent,why);assert(content.children.indexOf(p)<content.children.indexOf(details));assert.equal(h.elements["walk-answer"].textContent,data.answer.text);
});
test("missing invalid or oversized selected description never falls back to other-place Google prose",async()=>{
  for(const user_description of [undefined,null,{},42,""," ","x".repeat(601)]){const data=result({answer:{text:"Global text about another venue.",source_ids:["g0"],stop_ids:["osm:0"]},alternatives:[option(45,{stops:[{...stop(),description:"Source text must not be an automatic fallback.",user_description}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(!h.elements["walk-stops"].textContent.includes("Global text"));assert(!h.elements["walk-stops"].textContent.includes("Source text"));assert(!h.elements["walk-stops"].children[0].children[0].children.some(el=>el.className==="stop-description"));assert(!h.elements["walk-result"].hidden);
  }
});

test("capability GET has no personal payload and selects the new protocol only once",async()=>{
  const h=harness();assert.equal(h.healthRequests.length,0);await h.submit();await h.submit();
  assert.equal(h.healthRequests.length,1);const request=h.healthRequests[0];assert.equal(request.url,"/api/health");assert.equal(request.options.method,"GET");assert.equal(request.body,undefined);assert(!("body" in request.options));
  for(const request of h.calls("/api/context"))assert.equal(request.body.consent,true);
  for(const request of h.calls("/api/unified/recommend"))assert.equal(request.body.preparation_token,"opaque-fixture-token");
});
test("old server absent or false capability preserves legacy context without consent or tokens",async()=>{
  for(const health of [{},{context_preparation:false},{context_preparation:"true"}]){
    const h=harness({health,rawContext:true});await h.submit();assert(!h.elements["walk-result"].hidden);
    const probe=h.calls("/api/context")[0].body,search=h.calls("/api/unified/recommend")[0].body;
    assert(!("consent" in probe));assert(!("preparation_token" in probe));assert(!("preparation_token" in search));assert.equal(search.consent,true);assert.deepEqual(search.client_context,probe.client_context);assert.equal(search.query,probe.query);
  }
});
test("legacy GPS resume retains frozen clock and granted coordinates with no preparation fields",async()=>{
  let probes=0;const h=harness({health:{},rawContext:true,geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:20}}),fetch:(req,r)=>req.url==="/api/context"?r(++probes===1?{needs_geolocation:true}:{resolved_context:context({origin_source:"browser_geolocation"})}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});
  await h.submit("Хочу прогулку на час");assert.equal(h.geo.length,1);assert.equal(probes,2);
  const first=h.calls("/api/context")[0].body,second=h.calls("/api/context")[1].body,last=h.calls("/api/unified/recommend")[0].body;
  for(const body of [first,second]){assert(!("consent" in body));assert(!("preparation_token" in body));}
  assert(!("preparation_token" in last));assert.equal(last.consent,true);for(const body of [second,last]){assert.equal(body.query,first.query);assert.deepEqual(body.client_context,first.client_context);assert.deepEqual(body.geolocation,{lat:51.1,lon:71.4,accuracy:20});assert.equal(body.location_status,"granted");}
});
test("capability true never falls back to legacy after malformed or rejected preparation",async()=>{
  for(const response of [{resolved_context:context()},{error:"Подготовка устарела.",code:"invalid_preparation"}]){
    const h=harness({rawContext:true,fetch:(req,r)=>r(response,!response.error,response.error?400:200)});await h.submit();
    assert.equal(h.healthRequests.length,1);assert.equal(h.calls("/api/context").length,1);assert.equal(h.calls("/api/context")[0].body.consent,true);assert.equal(h.calls("/api/unified/recommend").length,0);assert.equal(h.geo.length,0);assert(h.elements["walk-result"].hidden);
  }
});
test("failed unreadable or unavailable capability never guesses legacy or sends query",async()=>{
  for(const health of [(_req,r)=>r({},false,503),()=>({ok:true,json:async()=>{throw Error("private diagnostic");}}),()=>{throw Error("Offline");}]){
    const h=harness({health});await h.submit("Синтетический запрос у Mega");assert.equal(h.healthRequests.length,1);assert.equal(h.requests.length,0);assert.equal(h.geo.length,0);assert(!h.elements["walk-error"].hidden);assert(!h.elements["walk-error"].textContent.includes("private diagnostic"));
  }
});
test("editing or leaving during capability lookup cannot start context or cache a stale protocol",async()=>{
  for(const leave of [false,true]){const delayed=deferred();const h=harness({health:()=>delayed.promise});const pending=h.submit();await flush();if(leave)await h.pagehide();else await h.edit("Изменённое сообщение");delayed.resolve(h.respond({context_preparation:true}));await pending;assert.equal(h.requests.length,0);assert.equal(h.geo.length,0);assert.equal(h.run("contextPreparationCapability"),null);assert(!h.elements["walk-submit"].disabled);}
});
test("protocol selection remains stable across later submissions and does not probe again",async()=>{
  let healthCalls=0;const h=harness({health:(_req,r)=>r({context_preparation:++healthCalls>1}),rawContext:true});await h.submit();await h.submit();assert.equal(healthCalls,1);assert.equal(h.calls("/api/context").length,2);for(const request of h.calls("/api/context"))assert(!("consent" in request.body));for(const request of h.calls("/api/unified/recommend"))assert(!("preparation_token" in request.body));
});

test("selected description origin is labelled honestly without changing whole answer or sources",async()=>{
  for(const [description_origin,label] of [["source-template","Идея приложения"],["local-model","Описание ИИ"]]){
    const description="<b>Предложенная пауза</b> после проверки условий.";const why="Проверенный собственный тип.";
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:description,description_origin,why_text:why}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
    const content=h.elements["walk-stops"].children[0].children[0],origin=content.children.find(el=>el.className==="stop-description-origin"),text=content.children.find(el=>el.className==="stop-description"),whyDetails=content.children.find(el=>el.className==="stop-why-details");
    assert.equal(origin.tagName,"P");assert.equal(origin.textContent,label);assert.equal(origin.children.length,0);assert.equal(text.textContent,description);assert.equal(text.children.length,0);assert.notEqual(whyDetails.open,true);
    assert.equal(h.elements["walk-answer"].textContent,data.answer.text);assert.equal(h.elements["google-original-text"].textContent,data.grounding.summary);assert.equal(h.elements["google-sources"].children.length,data.grounding.sources.length+1);
  }
});
test("legacy or unrecognized description origin never invents a model or template label",async()=>{
  for(const description_origin of [undefined,null,"",{},"remote-model","<img src=x>","__proto__"]){
    const description="Описание старого ответа остаётся доступно без выдуманной атрибуции.";
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:description,description_origin}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
    const content=h.elements["walk-stops"].children[0].children[0];assert(!content.children.some(el=>el.className==="stop-description-origin"));assert(content.children.some(el=>el.className==="stop-description"&&el.textContent===description));
  }
});
test("invalid descriptions cannot leave an orphan origin label",async()=>{
  for(const user_description of [null," ",{},"x".repeat(601)]){
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description,description_origin:"source-template"}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
    assert(!h.elements["walk-stops"].children[0].children[0].children.some(el=>el.className==="stop-description-origin"));
  }
});
test("ready variant switches description attribution locally and editing clears it",async()=>{
  const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:"Принятое описание выбранного места.",description_origin:"local-model"}]}),option(60,{stops:[{...stop("dgis"),user_description:"Предложенная идея для другой остановки.",description_origin:"source-template"}]})]});
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(h.elements["walk-stops"].textContent.includes("Описание ИИ"));await h.elements["walk-variants"].children[1].emit("click");await flush();assert(h.elements["walk-stops"].textContent.includes("Идея приложения"));assert(!h.elements["walk-stops"].textContent.includes("Описание ИИ"));assert.equal(h.calls("/api/context").length,1);assert.equal(h.calls("/api/unified/recommend").length,1);await h.edit("Новое сообщение");assert.equal(h.elements["walk-stops"].textContent,"");
});

test("night availability note stays visible beside accepted model or template description",async()=>{
  const note="Ночной доступ и освещение не подтверждены; проверьте перед выходом.";
  for(const description_origin of ["local-model","source-template",undefined]){
    const user_description=description_origin?"Предложенная прогулка у выбранного места.":undefined;
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description,description_origin,availability_note:note,why_text:"Техническая причина выбора."}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
    const content=h.elements["walk-stops"].children[0].children[0],p=content.children.find(el=>el.className==="stop-availability-note"),why=content.children.find(el=>el.className==="stop-why-details");
    assert(p);assert.equal(p.tagName,"P");assert.equal(p.textContent,note);assert.equal(p.hidden,false);assert.equal(p.parentNode,content);assert.equal(p.children.length,0);assert.notEqual(why.open,true);assert(content.children.indexOf(p)<content.children.indexOf(why));
    assert.equal(h.elements["walk-mode"].textContent,"ИИ · три источника проверены");assert.equal(h.elements["walk-answer"].textContent,data.answer.text);assert.equal(h.elements["google-sources"].children.length,data.grounding.sources.length+1);
  }
});
test("missing empty or invalid availability note adds no fabricated access statement",async()=>{
  for(const availability_note of [undefined,null,""," ",{},42,"x".repeat(601)]){
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:"Принятое описание без сведений о ночном доступе.",description_origin:"local-model",availability_note}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(!h.elements["walk-stops"].children[0].children[0].children.some(el=>el.className==="stop-availability-note"));assert(!h.elements["walk-stops"].textContent.includes("[object Object]"));assert(!h.elements["walk-result"].hidden);
  }
});
test("availability note is exact plain text and never injects elements or changes Google content",async()=>{
  const note='<img src=x onerror=alert(1)> Ночной доступ не подтверждён.';
  const data=result({alternatives:[option(45,{stops:[{...stop(),availability_note:note}]})]});
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
  const p=h.elements["walk-stops"].children[0].children[0].children.find(el=>el.className==="stop-availability-note");assert.equal(p.textContent,note);assert.equal(p.children.length,0);assert.equal(h.elements["walk-answer"].textContent,data.answer.text);assert.equal(h.elements["google-original-text"].textContent,data.grounding.summary);assert.equal(h.elements["google-original-sources"].children.length,data.grounding.sources.length+1);
});
test("ready variant without a night note clears old note without another source search",async()=>{
  const note="Ночной доступ и освещение не подтверждены; проверьте перед выходом.";
  const data=result({alternatives:[option(45,{stops:[{...stop(),availability_note:note}]}),option(60)]});
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(h.elements["walk-stops"].textContent.includes(note));await h.elements["walk-variants"].children[1].emit("click");await flush();assert(!h.elements["walk-stops"].textContent.includes(note));assert.equal(h.calls("/api/context").length,1);assert.equal(h.calls("/api/unified/recommend").length,1);await h.edit("Другой запрос");assert.equal(h.elements["walk-stops"].textContent,"");
});

test("compatibility note appears once for accepted model or template without mutating response",async()=>{
  const note="Ночной доступ и освещение не подтверждены; проверьте перед выходом.";
  const description="Предложенная прогулка у выбранного места после проверки условий.";
  const composite=description+" Примечание приложения: "+note;
  for(const description_origin of ["local-model","source-template"]){
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:composite,description_origin,availability_note:note}]})]});
    data.answer.venue_descriptions=[{stop_id:"osm:0",text:description}];
    if(description_origin==="source-template"){data.mode="grounding-only";data.answer=null;}
    const original=copy(data);
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
    const content=h.elements["walk-stops"].children[0].children[0];assert.equal(content.children.find(el=>el.className==="stop-description").textContent,description);assert.equal(content.children.find(el=>el.className==="stop-availability-note").textContent,note);assert.equal(content.textContent.split(note).length-1,1);
    assert.equal(content.children.find(el=>el.className==="stop-description-origin").textContent,description_origin==="local-model"?"Описание ИИ":"Идея приложения");
    assert.deepEqual(copy(h.run("state.result")),original);assert.deepEqual(data,original);assert.equal(h.elements["walk-answer"].textContent,data.answer?data.answer.text:data.grounding.summary);assert.equal(h.elements["google-sources"].children.length,data.grounding.sources.length+1);
  }
});
test("only exact matching final application suffix is split from display description",async()=>{
  const note="Ночной доступ и освещение не подтверждены; проверьте перед выходом.";
  for(const user_description of ["Примечание приложения: "+note,"Своя идея. "+note,"Своя идея. Примечание приложения: "+note+" ","Своя идея. Примечание приложения: Другая проверка."]){
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description,availability_note:note}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();const content=h.elements["walk-stops"].children[0].children[0];assert.equal(content.children.find(el=>el.className==="stop-description").textContent,user_description);assert.equal(content.children.find(el=>el.className==="stop-availability-note").textContent,note);
  }
});

test("accepted model composite is labelled AI only after exact note separation",async()=>{
  const note="Ночной доступ и освещение не подтверждены; проверьте перед выходом.";
  const pure="Модель предложила прогулку у выбранного места после проверки условий.";
  const composite=pure+" Примечание приложения: "+note;
  const data=result({alternatives:[option(45,{stops:[{...stop(),user_description:composite,description_origin:"local-model-with-note",availability_note:note}]})]});
  data.answer.venue_descriptions=[{stop_id:"osm:0",text:pure}];const original=copy(data);
  const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();
  const content=h.elements["walk-stops"].children[0].children[0];assert.equal(content.children.find(el=>el.className==="stop-description").textContent,pure);assert.equal(content.children.find(el=>el.className==="stop-description-origin").textContent,"Описание ИИ");assert.equal(content.children.find(el=>el.className==="stop-availability-note").textContent,note);assert.equal(content.textContent.split(note).length-1,1);assert.deepEqual(copy(h.run("state.result")),original);assert.deepEqual(data,original);assert.equal(data.answer.venue_descriptions[0].text,pure);assert.equal(h.elements["walk-answer"].textContent,data.answer.text);
});
test("malformed or missing composite suffix never receives an AI description label",async()=>{
  const note="Ночной доступ и освещение не подтверждены; проверьте перед выходом.";
  for(const [user_description,availability_note] of [["Описание без суффикса приложения.",note],["Идея. Примечание приложения: "+note+" ",note],["Идея. Примечание приложения: Другое примечание.",note],["Идея. Примечание приложения: "+note,undefined],["Идея. Примечание приложения: "+note,{}]]){
    const data=result({alternatives:[option(45,{stops:[{...stop(),user_description,availability_note,description_origin:"local-model-with-note"}]})]});
    const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();const content=h.elements["walk-stops"].children[0].children[0];assert(!content.children.some(el=>el.className==="stop-description-origin"));assert.equal(content.children.find(el=>el.className==="stop-description").textContent,user_description);assert(!h.elements["walk-result"].hidden);
  }
});

(async()=>{
  let passed=0;
  for(const item of cases){try{await item.fn();passed++;}catch(error){console.error("FAIL "+item.name+"\n"+error.stack);process.exitCode=1;}}
  console.log(JSON.stringify({scope:"offline unified frontend DOM/fetch/geolocation fixtures; not browser/provider/model QA",total:cases.length,passed,network_calls:0}));
})();
