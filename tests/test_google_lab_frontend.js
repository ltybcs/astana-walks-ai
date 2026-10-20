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
  const windowEvents={};const timers=new Map();let timerId=0;const requests=[];const geo=[];let clock="2026-10-20T12:00:00.000Z";
  class FixtureDate extends Date {constructor(...args){super(...(args.length?args:[clock]));}getTimezoneOffset(){return -300;}}
  const navigator=config.noGeo?{}:{geolocation:{getCurrentPosition(success,error,options){geo.push({success,error,options});if(config.geo)config.geo(success,error,options);}}};
  const respond=(data,ok=true,status=200)=>({ok,status,json:async()=>data});
  const fetch=async(url,options)=>{
    const request={url,options,body:JSON.parse(options.body)};
    requests.push(request);
    if(config.fetch) return config.fetch(request,respond);
    if(url==="/api/context")return respond({needs_geolocation:false,resolved_context:context()});
    if(url==="/api/unified/recommend")return respond(result());
    if(url==="/api/unified/map")return respond({code:"route_map_unconfigured",error:"Карта не настроена."},false,503);
    throw Error("Unexpected offline endpoint "+url);
  };
  const sandbox={document,window:{addEventListener:(name,fn)=>{(windowEvents[name] ||= []).push(fn);}},navigator,fetch,URL,AbortController,Date:FixtureDate,
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id),console};
  const vmContext=vm.createContext(sandbox);vm.runInContext(source,vmContext,{filename:"lab.js"});vm.runInContext("boot()",vmContext);
  return {elements,requests,geo,timers,document,run:code=>vm.runInContext(code,vmContext),respond,
    clock:value=>clock=value,
    async submit(query="У меня 45 минут возле Mega, хочу прогулку"){elements["walk-query"].value=query;return elements["walk-form"].emit("submit");},
    async edit(query){elements["walk-query"].value=query;await elements["walk-query"].emit("input");},
    async pagehide(){for(const fn of windowEvents.pagehide||[])fn();await flush();},
    fire(ms){for(const [id,timer]of [...timers])if(timer.ms===ms){timers.delete(id);timer.fn();}},
    calls:endpoint=>requests.filter(request=>request.url===endpoint),
    frame:()=>elements["route-map-host"].children[0]
  };
}
const cases=[];
function test(name,fn){cases.push({name,fn});}
test("initial page has no external fetch or iframe",()=>{const h=harness();assert.equal(h.requests.length,0);assert.equal(h.frame(),undefined);});
test("only one textarea and one submit, no manual controls",()=>{assert.equal((html.match(/<textarea\b/g)||[]).length,1);assert.equal((html.match(/type="submit"/g)||[]).length,1);assert(!/<select\b|type="number"|walk-provider/.test(html));});
test("initial textarea is empty and example is only placeholder",()=>{assert(/<textarea[^>]*placeholder="[^"]*45[^"]*"[^>]*><\/textarea>/.test(html));});
test("no catalog, Leaflet, old embed config or initial map",()=>{assert(!/leaflet|embeds\.json|catalog-browser|tab-osm|tab-google/.test(html+source));assert.equal((html.match(/id="route-map-host"/g)||[]).length,1);});
test("disclosure precedes submit and names both external providers",()=>{const notice=html.indexOf('id="walk-consent"');assert(notice<html.indexOf('id="walk-submit"'));assert(html.slice(notice,html.indexOf('id="walk-submit"')).includes("Google и 2ГИС"));});
test("GPS wait and strict no search copy are visible",()=>{assert(html.includes("до 8 секунд"));assert(html.includes("Без подтверждённого старта поиск не начнётся"));assert(!html.includes("подписанный пример"));});
test("no storage, history mutation or raw HTML sinks",()=>{assert(!/localStorage|sessionStorage|indexedDB|innerHTML|outerHTML|pushState|replaceState|document\.cookie/.test(source));});
test("mobile input is 16px and all buttons have 44px targets",()=>{assert(css.includes("min-height:44px"));assert(css.includes("textarea{font-size:16px}"));});
test("empty query performs zero context or provider calls",async()=>{const h=harness();await h.submit("  ");assert.equal(h.requests.length,0);assert(!h.elements["walk-error"].hidden);});
test("query-only context resolves before one unified search",async()=>{const h=harness();await h.submit();assert.deepEqual(h.requests.map(v=>v.url),["/api/context","/api/unified/recommend","/api/unified/map"]);assert.equal(h.calls("/api/unified/recommend").length,1);});
test("request contains explicit combined consent",async()=>{const h=harness();await h.submit();assert.equal(h.calls("/api/unified/recommend")[0].body.consent,true);assert(!("consent" in h.calls("/api/context")[0].body));});
test("one frozen clock and correct UTC offset are reused",async()=>{const h=harness();await h.submit();const a=h.requests[0].body.client_context,b=h.requests[1].body.client_context;assert.deepEqual(a,b);assert.equal(a.instant,"2026-10-20T12:00:00.000Z");assert.equal(a.utc_offset_minutes,300);});
test("ordinary text origin never requests browser GPS",async()=>{const h=harness();await h.submit();assert.equal(h.geo.length,0);assert(!("geolocation" in h.calls("/api/unified/recommend")[0].body));});
test("whole model answer remains verbatim",async()=>{const h=harness();await h.submit();assert.equal(h.elements["walk-answer"].textContent,result().answer.text);});
test("every Google source preview is adjacent to answer",async()=>{const h=harness();await h.submit();assert.equal(h.elements["google-sources"].children.length,3);assert(h.elements["google-sources"].textContent.includes("Второй источник"));assert(html.indexOf('id="google-sources"')>html.indexOf('id="walk-answer"'));assert(html.indexOf('id="google-sources"')<html.indexOf('id="walk-warnings"'));});
test("Google source references preserve zero index and exact source ID",async()=>{const data=result();data.grounding.sources=Array.from({length:5},(_,index)=>({source_id:"g"+index,id:"places/"+index,title:"Google title "+index,url:"https://www.google.com/maps/place/"+index}));data.answer.text="Смотрите источник [g2], исходная ссылка [2].";const h=harness({fetch:(req,r)=>r(req.url==="/api/context"?{resolved_context:context()}:req.url==="/api/unified/recommend"?data:{},req.url!=="/api/unified/map")});await h.submit();assert(h.elements["google-sources"].children[3].textContent.includes("[2] / g2"));assert.equal(h.elements["walk-answer"].textContent,data.answer.text);});
test("original whole Google summary remains available",async()=>{const h=harness();await h.submit();assert.equal(h.elements["google-original-text"].textContent,result().grounding.summary);assert.equal(h.elements["google-original"].hidden,false);});
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
test("unknown address context error causes zero provider search",async()=>{const h=harness({fetch:(req,r)=>r({code:"known_start_required",error:"Неизвестный старт."},false,422)});await h.submit("Гулять у неизвестного адреса");assert.equal(h.calls("/api/unified/recommend").length,0);assert.equal(h.geo.length,0);assert(h.elements["walk-error"].textContent.includes("Поиск по источникам"));});
test("legacy city-sample response is rejected before search",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:false,resolved_context:context({is_city_sample:true})})});await h.submit();assert.equal(h.requests.length,1);assert(h.elements["walk-result"].hidden);});
test("GPS denied has zero unified search and no sample fallback",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true,resolved_context:context({origin:null})}),geo:(ok,bad)=>bad({code:1})});await h.submit("Хочу прогулку");assert.equal(h.calls("/api/unified/recommend").length,0);assert(h.elements["walk-error"].textContent.includes("Старт не определён"));});
test("GPS timeout is bounded at eight seconds and never searches",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true})});const pending=h.submit("Хочу гулять");await flush();assert.equal(h.geo[0].options.timeout,8000);h.fire(8000);await pending;assert.equal(h.calls("/api/unified/recommend").length,0);assert(!h.elements["walk-submit"].disabled);});
test("unsupported GPS fails closed without search",async()=>{const h=harness({noGeo:true,fetch:(req,r)=>r({needs_geolocation:true})});await h.submit("Хочу гулять");assert.equal(h.requests.length,1);assert(!h.elements["walk-error"].hidden);});
test("invalid coarse accuracy is not sent to backend",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true}),geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:200000}})});await h.submit("Хочу гулять");assert.equal(h.requests.length,1);});
test("valid GPS is gated again and uses granted status",async()=>{let probes=0;const h=harness({geo:ok=>ok({coords:{latitude:51.1,longitude:71.4,accuracy:20}}),fetch:(req,r)=>req.url==="/api/context"?r(++probes===1?{needs_geolocation:true}:{resolved_context:context({origin_source:"browser_geolocation"})}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});await h.submit("Хочу гулять");assert.equal(probes,2);const req=h.calls("/api/unified/recommend")[0];assert.equal(req.body.location_status,"granted");assert.deepEqual(req.body.geolocation,{lat:51.1,lon:71.4,accuracy:20});assert.equal(h.geo[0].options.maximumAge,0);});
test("GPS callback after clock change retains original submit snapshot",async()=>{let probes=0;const h=harness({fetch:(req,r)=>req.url==="/api/context"?r(++probes===1?{needs_geolocation:true}:{resolved_context:context({origin_source:"browser_geolocation"})}):r(req.url==="/api/unified/recommend"?result():{},req.url!=="/api/unified/map")});const pending=h.submit("Хочу гулять");await flush();h.clock("2026-10-20T13:00:00.000Z");h.geo[0].success({coords:{latitude:51.1,longitude:71.4,accuracy:20}});await pending;for(const req of h.requests.filter(value=>value.url!=="/api/unified/map"))assert.equal(req.body.client_context.instant,"2026-10-20T12:00:00.000Z");});
test("outside-area second context fails before all providers",async()=>{let probes=0;const h=harness({geo:ok=>ok({coords:{latitude:40,longitude:70,accuracy:20}}),fetch:(req,r)=>++probes===1?r({needs_geolocation:true}):r({code:"known_start_required",error:"За пределами области."},false,422)});await h.submit("Хочу гулять");assert.equal(h.requests.length,2);assert.equal(h.calls("/api/unified/recommend").length,0);});
test("edited input ignores late geolocation callback",async()=>{const h=harness({fetch:(req,r)=>r({needs_geolocation:true})});const pending=h.submit("Хочу гулять");await flush();await h.edit("Другое сообщение");h.geo[0].success({coords:{latitude:51.1,longitude:71.4,accuracy:1}});await pending;assert.equal(h.requests.length,1);assert(h.elements["walk-result"].hidden);});
test("GPS is not retained in application state",async()=>{const h=harness();await h.submit();assert(!Object.keys(h.run("state")).some(key=>/geo(location)?$|payload|query/i.test(key)));});
test("edited query aborts in-flight provider and ignores late result",async()=>{const d=deferred();const h=harness({fetch:(req,r)=>req.url==="/api/context"?r({resolved_context:context()}):d.promise});const pending=h.submit();await flush();const request=h.calls("/api/unified/recommend")[0];await h.edit("Новый запрос");assert(request.options.signal.aborted);d.resolve(h.respond(result()));await pending;assert(h.elements["walk-result"].hidden);assert.equal(h.calls("/api/unified/map").length,0);});
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
test("fetch policy is no-store, same-origin and rejects redirects",async()=>{const h=harness();await h.submit();for(const request of h.requests){assert.equal(request.options.cache,"no-store");assert.equal(request.options.credentials,"same-origin");assert.equal(request.options.redirect,"error");}});
test("completed result is keyboard focusable without scrolling",async()=>{const h=harness();await h.submit();assert.equal(h.document.activeElement,h.elements["walk-result-title"]);assert.equal(h.document.activeElement.focusOptions.preventScroll,true);});
test("user editing clears all source previews and raw response from UI state",async()=>{const h=harness();await h.submit();await h.edit("Изменено");assert.equal(h.elements["google-sources"].children.length,0);assert.equal(h.elements["other-sources"].children.length,0);assert.equal(h.elements["google-original-text"].textContent,"");assert.equal(h.run("state.result"),null);});
(async()=>{
  let passed=0;
  for(const item of cases){try{await item.fn();passed++;}catch(error){console.error("FAIL "+item.name+"\n"+error.stack);process.exitCode=1;}}
  console.log(JSON.stringify({scope:"offline unified frontend DOM/fetch/geolocation fixtures; not browser/provider/model QA",total:cases.length,passed,network_calls:0}));
})();
