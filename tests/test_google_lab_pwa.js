"use strict";
/* Offline service-worker/install fixtures. No browser/network/provider calls. */
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const root=path.resolve(__dirname,".."),dir=path.join(root,"web/google-lab");
const sw=fs.readFileSync(path.join(dir,"sw.js"),"utf8"),pwa=fs.readFileSync(path.join(dir,"pwa.js"),"utf8");
const html=fs.readFileSync(path.join(dir,"index.html"),"utf8");
const manifest=JSON.parse(fs.readFileSync(path.join(root,"web/manifest.webmanifest"),"utf8"));
const tests=[];const test=(name,fn)=>tests.push({name,fn});const tick=()=>new Promise(r=>setImmediate(r));
function worker(options={}){
 const handlers={},added=[],deleted=[],puts=[],matches=[],network=[],counters={skip:0,claim:0};
 const cache={addAll:async requests=>{added.push(...requests.map(v=>v.url));if(options.addFailure)throw Error("quota");},put:async(key,value)=>{puts.push(key);if(options.putFailure)throw Error("quota");},match:async key=>{matches.push(key);return options.cached;}};
 const ctx=vm.createContext({self:{location:{origin:"https://app.example"},clients:{claim:async()=>{counters.claim++;}},skipWaiting:async()=>{counters.skip++;},addEventListener:(name,fn)=>handlers[name]=fn},
  caches:{open:async()=>{if(options.openFailure)throw Error("disk");return cache;},keys:async()=>options.keys||[],delete:async key=>{deleted.push(key);}},
  Request:class{constructor(url,opts){this.url=url;this.cache=opts.cache;}},Response:{error:()=>({error:true})},URL,
  fetch:async request=>{network.push(request);if(options.fetchFailure)throw Error("offline");return options.response||{ok:true,type:"basic",redirected:false,clone(){return {clone:true};}};}});
 vm.runInContext(sw,ctx);
 return {added,deleted,puts,matches,network,counters,paths:()=>Array.from(vm.runInContext("SHELL_PATHS",ctx)),
 async event(name){let done;handlers[name]({waitUntil:p=>done=p});if(done)await done;},
 async fetch(url="/",method="GET"){let response;handlers.fetch({request:{url:new URL(url,"https://app.example").href,method},respondWith:p=>response=p});return response===undefined?{intercepted:false}:{intercepted:true,response:await response};}};
}
function install(options={}){
 const button={hidden:true,listeners:{},addEventListener:(name,fn)=>button.listeners[name]=fn};
 const status={textContent:""};const handlers={},calls=[];
 const media={matches:Boolean(options.installed),addEventListener:(name,fn)=>media.change=fn};
 const nav={standalone:Boolean(options.iosInstalled)};
 if(!options.noSW)nav.serviceWorker={register:async(url,args)=>{calls.push({url,args});if(options.failure)throw Error("disk");},ready:Promise.resolve()};
 vm.runInNewContext(pwa,{document:{getElementById:id=>id==="install-app"?button:status},window:{matchMedia:()=>media,isSecureContext:options.secure!==false,addEventListener:(name,fn)=>handlers[name]=fn},navigator:nav});
 return {button,status,handlers,calls,media};
}
test("manifest preserves installed app identity",()=>{assert.equal(manifest.id,"/");assert.equal(manifest.start_url,"/");assert.equal(manifest.scope,"/");assert.equal(manifest.display,"standalone");});
test("head links manifest icons and new PWA only",()=>{assert(html.includes('href="/manifest.webmanifest"'));assert(html.includes('src="/pwa.js"'));assert(html.includes('href="/icons/apple-touch-icon.png"'));assert(!html.includes("Экспериментальный"));});
test("install control has safe manual help",()=>{assert(html.includes('id="install-app"'));assert(html.includes("Как установить приложение"));assert(html.includes("iPhone Safari"));assert(!html.includes("Основное демо ↗"));});
test("shell install caches only exact intended local resources",async()=>{const h=worker();await h.event("install");assert.deepEqual(h.added,["/","/lab.js","/lab.css","/pwa.js","/manifest.webmanifest","/favicon.svg","/icons/icon-192.png","/icons/icon-512.png","/icons/maskable-512.png","/icons/apple-touch-icon.png"]);assert.equal(h.counters.skip,1);});
test("shell install failure does not activate incomplete cache",async()=>{const h=worker({addFailure:true});await assert.rejects(()=>h.event("install"));assert.equal(h.counters.skip,0);});
test("activation removes only known shell-prefix caches",async()=>{const h=worker({keys:["astana-walks-shell-v4","astana-walks-shell-v5-unified","astana-walks-shell-v6-categories","astana-walks-shell-v7-semantic","astana-walks-shell-v8-audit-development","astana-walks-shell-v9-amenity-alternatives","unrelated-cache","provider-data"]});await h.event("activate");assert.deepEqual(h.deleted,["astana-walks-shell-v4","astana-walks-shell-v5-unified","astana-walks-shell-v6-categories","astana-walks-shell-v7-semantic","astana-walks-shell-v8-audit-development"]);assert.equal(h.counters.claim,1);});
test("no API methods or query strings intercepted",async()=>{const h=worker();for(const [url,method]of [["/api/context","POST"],["/api/unified/recommend","POST"],["/api/unified/map","POST"],["/api/health","GET"],["/?query=private","GET"],["/lab.js?gps=1","GET"],["/","POST"]])assert.equal((await h.fetch(url,method)).intercepted,false);assert.equal(h.network.length,0);assert.equal(h.puts.length,0);});
test("cross-origin Google 2GIS tiles and iframe bypass cache",async()=>{const h=worker();for(const url of ["https://www.google.com/maps/embed/v1/directions?key=fixture","https://catalog.api.2gis.com/3.0/items","https://tile.openstreetmap.org/1/1/1.png"])assert.equal((await h.fetch(url)).intercepted,false);assert.equal(h.puts.length,0);});
test("arbitrary same-origin records bypass cache",async()=>{const h=worker();for(const url of ["/output/report.json","/.local/key","/unknown","/app.js","/styles.css","/sw.js"])assert.equal((await h.fetch(url)).intercepted,false);});
test("shell is network-first and caches successful response",async()=>{const h=worker();const data=await h.fetch("/lab.js");assert(data.intercepted);assert(data.response.ok);assert.equal(h.network.length,1);assert.deepEqual(h.puts,["/lab.js"]);assert.equal(h.matches.length,0);});
test("offline shell uses only matching saved shell entry",async()=>{const cached={shell:true};const h=worker({fetchFailure:true,cached});const data=await h.fetch("/");assert.equal(data.response,cached);assert.deepEqual(h.matches,["/"]);});
test("uncached offline resource is a network failure",async()=>{const h=worker({fetchFailure:true});const data=await h.fetch("/lab.js");assert.equal(data.response.error,true);});
test("cache write failure never masks valid network response",async()=>{const response={ok:true,type:"basic",clone(){return {};}};const h=worker({putFailure:true,response});assert.equal((await h.fetch("/lab.css")).response,response);});
test("cache open failure never masks network response",async()=>{const h=worker({openFailure:true});assert((await h.fetch("/lab.js")).response.ok);});
test("HTTP error opaque and redirected payloads never cached",async()=>{for(const response of [{ok:false,type:"basic"},{ok:true,type:"opaque"},{ok:true,type:"basic",redirected:true}]){const h=worker({response});await h.fetch("/");assert.equal(h.puts.length,0);}});
test("no reload GPS data stores or message channel in PWA",()=>{assert(!/reload\(|geolocation|localStorage|sessionStorage|indexedDB|postMessage|querySelector.*form/.test(pwa+sw));});
test("secure PWA registers root worker without cache reuse",async()=>{const h=install();await tick();assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,"/sw.js");assert.equal(h.calls[0].args.scope,"/");assert.equal(h.calls[0].args.updateViaCache,"none");assert(h.status.textContent.includes("нужно подключение"));});
test("insecure or unsupported browser never registers",async()=>{for(const options of [{secure:false},{noSW:true}]){const h=install(options);await tick();assert.equal(h.calls.length,0);assert(h.status.textContent.includes("HTTPS"));}});
test("native prompt appears only after beforeinstallprompt",async()=>{const h=install();assert(h.button.hidden);let prevented=false;let prompted=0;h.handlers.beforeinstallprompt({preventDefault(){prevented=true;},prompt:async()=>{prompted++;},userChoice:Promise.resolve({outcome:"dismissed"})});assert(prevented);assert(!h.button.hidden);await h.button.listeners.click();assert.equal(prompted,1);assert(h.button.hidden);});
test("install prompt rejection keeps manual instructions usable",async()=>{const h=install();h.handlers.beforeinstallprompt({preventDefault(){},prompt:async()=>{throw Error("dismissed");}});await h.button.listeners.click();assert(h.status.textContent.includes("меню браузера"));assert(h.button.hidden);});
test("installed standalone app keeps native install button hidden",()=>{for(const options of [{installed:true},{iosInstalled:true}]){const h=install(options);h.handlers.beforeinstallprompt({preventDefault(){}});assert(h.button.hidden);}});
test("appinstalled clears pending install prompt",()=>{const h=install();h.handlers.beforeinstallprompt({preventDefault(){}});assert(!h.button.hidden);h.handlers.appinstalled();assert(h.button.hidden);assert(h.status.textContent.includes("установлено"));});
test("worker registration error is honest nonblocking copy",async()=>{const h=install({failure:true});await tick();assert(h.status.textContent.includes("не удалось"));});
(async()=>{let passed=0;for(const item of tests){try{await item.fn();passed++;}catch(error){console.error("FAIL "+item.name+"\n"+error.stack);process.exitCode=1;}}console.log(JSON.stringify({scope:"offline SW/install fixtures only, not physical-phone or provider QA",total:tests.length,passed,network_calls:0}));})();
