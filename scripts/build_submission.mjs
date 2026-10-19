/**
 * Editable 16:9 jury deck. Reads only the facts assembled by build_submission.py.
 * Requires the bundled @oai/artifact-tool runtime (or an installed package).
 */
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspaceDir = path.resolve(process.argv[2] || ".");
const factsPath = path.resolve(process.argv[3] || "tmp/submission/facts.json");
const outDir = path.resolve(process.argv[4] || "output");
const buildDir = path.join(workspaceDir, "tmp", "submission");
const facts = JSON.parse(await fs.readFile(factsPath, "utf8"));
const runtimeRoot = process.env.CODEX_ARTIFACT_RUNTIME || path.join(process.env.USERPROFILE || "", ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies");
process.env.RUNTIME_NODE_MODULES ||= path.join(runtimeRoot,"node","node_modules");
const modulePath = process.env.ARTIFACT_TOOL_MODULE || path.join(runtimeRoot, "node", "node_modules", "@oai", "artifact-tool", "dist", "artifact_tool.mjs");
let toolkit;
try { toolkit = await import("@oai/artifact-tool"); }
catch { toolkit = await import(pathToFileURL(modulePath).href); }
const { Presentation, PresentationFile } = toolkit;
const W = 1280, H = 720;
const C = { dark: "#0A2D2E", cream: "#F4F0E5", teal: "#63D3BE", muted: "#AEC8C0", ink: "#123C3B", gray: "#55716A" };
const deck = Presentation.create({ slideSize: { width: W, height: H } });
await fs.mkdir(buildDir, { recursive: true });
await fs.mkdir(path.join(outDir, "presentations"), { recursive: true });
await fs.mkdir(path.join(buildDir, "slides"), { recursive: true });

function text(slide, str, x, y, w, h, size=30, color=C.cream, bold=false, align="left") {
  const s = slide.shapes.add({ geometry: "textbox", position: { left:x, top:y, width:w, height:h }, fill:"none", line:{fill:"none",width:0} });
  s.text = str;
  s.text.style = { typeface:"Arial", fontSize:size, color, bold, alignment:align, verticalAlignment:"top", insets:0, wrap:"square" };
  return s;
}
function slide(title, light=false) {
  const s = deck.slides.add();
  s.background.fill = light ? C.cream : C.dark;
  if (title) text(s,title,72,53,1136,90,48,light?C.ink:C.cream,true);
  text(s,String(deck.slides.items.length).padStart(2,"0"),1162,658,46,30,17,light?C.gray:C.muted);
  return s;
}
function note(s, content, sources=[]) {
  s.speakerNotes.text = content + (sources.length ? "\n\nИсточники и доказательства:\n"+sources.join("\n") : "");
}
function imageMime(bytes) {
  if(bytes.length>=3 && bytes[0]===0xff && bytes[1]===0xd8 && bytes[2]===0xff) return "image/jpeg";
  const png=[0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a];
  if(bytes.length>=8 && png.every((value,i)=>bytes[i]===value)) return "image/png";
  throw new Error("Screenshot must contain actual JPEG or PNG bytes; its extension is not trusted.");
}
const n=facts.dataset.count;
const date=facts.dataset.date_label;
const scenarioCount=facts.evaluation.scenarios.length;
const passCount=facts.evaluation.scenarios.filter(x=>x.passed).length;
const demo=facts.featured;
const llm=facts.llm.live_verified ? `${facts.llm.provider} / ${facts.llm.model}` : "Локальный extractive-режим";

{
 const s=slide("");
 text(s,"Astana Walks",72,92,1100,118,88,C.cream,true);
 text(s,"Помощник для коротких\nпрогулок в Астане",76,251,1040,166,57,C.teal);
 text(s,"Задача №1 НИЦ Smart City\nBuild with AI / GDG Astana",77,528,1040,82,27,C.muted);
 note(s,facts.speech[0], ["Условие хакатона, задача №1, страницы 1–3.", facts.repository]);
}
{
 const s=slide("Прогулка рядом с EXPO",true);
 text(s,String(demo.minutes),72,183,320,185,144,C.ink,true);
 text(s,"минут на прогулку\nс возвращением",79,375,345,100,31,C.gray);
 text(s,`«${demo.query}»`,486,185,700,126,35,C.ink,true);
 text(s,demo.names.join("\n"),486,347,700,138,32,C.ink);
 text(s,`${demo.total_minutes} мин по расчёту\n${demo.result_count} ${demo.result_count===1?"место":"места"}, объяснения и идеи действий`,486,527,700,95,27,C.gray);
 note(s,facts.speech[1],[facts.evaluation.source,"Оценка пути геометрическая, с запасом на обход и возвратом. Это не пешеходная навигация."]);
}
{
 const s=slide("Ответ в интерфейсе");
 const bytes=await fs.readFile(facts.screenshot);
 s.images.add({blob:bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),contentType:imageMime(bytes),alt:"Фактический ответ локального Astana Walks",fit:"cover",crop:facts.screenshot_crop,position:facts.screenshot_frame});
 if(facts.screenshot_layout==="detail") {
   if(facts.mobile_screenshot) {
     const mobileBytes=await fs.readFile(facts.mobile_screenshot);
     s.images.add({blob:mobileBytes.buffer.slice(mobileBytes.byteOffset,mobileBytes.byteOffset+mobileBytes.byteLength),contentType:imageMime(mobileBytes),alt:"Фактический мобильный веб-интерфейс. Физическая установка подтверждается отдельно со слов пользователя.",fit:"contain",position:facts.mobile_frame});
     text(s,"Из проверочного\nпрогона",326,192,238,64,24,C.teal,true);
     text(s,`«${facts.featured.generated_why}»`,326,274,238,250,26,C.cream);
     text(s,facts.featured.names[0],326,545,238,42,28,C.cream,true);
     text(s,`${facts.featured.total_minutes} мин, с возвратом`,326,603,238,35,23,C.muted);
   } else {
     text(s,"Из проверочного прогона",72,192,460,56,27,C.teal,true);
     text(s,`«${facts.featured.generated_why}»`,72,288,460,200,32,C.cream);
     text(s,facts.featured.names[0],72,522,460,55,35,C.cream,true);
     text(s,`${facts.featured.total_minutes} мин, с возвратом`,72,590,460,46,27,C.muted);
   }
 }
 text(s,facts.pwa?.phone_installation?.user_reported?"Установка и запуск по иконке подтверждены пользователем":facts.pwa?.enabled?"Мобильный интерфейс с поддержкой PWA":"У каждого места: почему, что сделать, время и ссылка на источник",72,651,1070,32,22,C.muted);
 note(s,facts.speech[2],[`Фактический скриншот: ${facts.screenshot_relative}`,facts.mobile_screenshot_relative||"",`Режим на момент сборки: ${llm}`,facts.featured.narration_source||"",...(facts.pwa?.enabled?["https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable",facts.pwa.evidence||""]:[])]);
}
{
 const s=slide("Как работает RAG");
 const cols=[
  ["01","Данные POI", "Автоматический сбор\nПубличные POI\nИсточник и дата"],
  ["02","Поиск", "BM25 и категории\nЛокация и время\nСтрогие ограничения"],
  ["03","Рекомендация", "Найденные места\nКраткое объяснение\nИдея действия"],
  ["04","Проверка", "Только известные ID\nБюджет с возвратом\nЧестный fallback"]
 ];
 cols.forEach((a,i)=>{const x=72+i*288;text(s,a[0],x,182,235,65,48,C.teal,true);text(s,a[1],x,279,255,65,31,C.cream,true);text(s,a[2],x,374,255,168,26,C.muted);});
 text(s,`Генерация: ${facts.llm.live_verified?facts.llm.model.split(" / ")[0]:"Локальный extractive-режим"}`,72,602,1130,48,25,C.teal);
 note(s,facts.speech[3],["app/retrieval.py","app/generation.py","app/llm.py",facts.llm.evidence||"Живой вызов LLM не подтверждён. Режим указан явно."]);
}
{
 const s=slide("Данные и происхождение",true);
 text(s,String(n),72,181,530,170,128,C.ink,true);
 text(s,"реальных POI",79,370,520,60,37,C.gray);
 text(s,`Сбор: ${date}`,79,485,520,64,27,C.gray);
 text(s,`${facts.dataset.attribution}\n${facts.dataset.license}`,79,578,530,68,23,C.gray);
 text(s,facts.dataset.source_name,656,195,550,110,36,C.ink,true);
 text(s,"Название, координаты, категория\nОписание и доступные теги\nID источника, ссылка и дата",656,329,550,174,29,C.ink);
 text(s,`${facts.dataset.missing_hours} POI без часов работы\nНеизвестное не становится обещанием`,656,532,550,86,27,C.gray);
 note(s,facts.speech[4],[facts.dataset.path,facts.dataset.source_url,facts.dataset.attribution,facts.dataset.license,`SHA-256 набора: ${facts.dataset.sha256}`]);
}
{
 const s=slide("Проверка пользовательских сценариев");
 text(s,`${passCount}/${scenarioCount}`,72,161,430,115,92,C.teal,true);
 text(s,`${facts.tests.passed} автоматических тестов пройдено`,557,207,640,65,32,C.cream,true);
 const values=facts.evaluation.scenarios.slice(0,5).map(r=>[r.short_name||r.name,r.result_label]);
 const table=s.tables.add({rows:5,columns:2,left:72,top:315,width:1136,height:295,columnWidths:[674,462],values});
 table.styleOptions={headerRow:false,bandedRows:false};
 table.borders.assign({style:"solid",fill:C.dark,width:0});
 for(let r=0;r<5;r++)for(let c=0;c<2;c++) {
   const cell=table.getCell(r,c);cell.fill=C.dark;
   cell.text.style={typeface:"Arial",fontSize:26,color:c?C.muted:C.cream,verticalAlignment:"middle",insets:0};
 }
 text(s,"Формальные ограничения плана; пустой ответ допустим.\nКачество текста проверяем отдельно.",72,632,1050,55,20,C.muted);
 note(s,facts.speech[5],[facts.evaluation.source,facts.tests.source,"Проверки фиксируют поведение прототипа. Они не измеряют удовлетворённость жителей и безопасность городских маршрутов."]);
}
{
 const s=slide("Приватность и доверие",true);
 text(s,"Локация для одного ответа",72,195,1110,64,42,C.ink,true);
 text(s,"Без аккаунта, истории прогулок и аналитики\nЗапросы и координаты не сохраняются приложением",72,300,1090,112,31,C.ink);
 text(s,"POI-факт отдельно от идеи действия",72,466,1100,62,36,C.ink,true);
 text(s,"Нет обещания «открыто сейчас», тишины или розетки\nбез подтверждения в источнике",72,558,1110,94,28,C.gray);
 note(s,facts.speech[6],["docs/PRIVACY.md", "app/server.py", "app/llm.py", facts.llm.privacy_note]);
}
{
 const s=slide("Бизнес-гипотеза и развитие");
 text(s,"Пилот для отелей\nи туристических стоек",72,189,640,145,43,C.cream,true);
 text(s,"В пилоте: доля выбранных планов\nи полезность объяснений",72,389,620,111,29,C.muted);
 text(s,"Следующие шаги",795,196,400,70,34,C.teal,true);
 text(s,"Пешеходные маршруты\nПроверка часов и свежести\nОценка с жителями\nКазахский и английский",795,310,400,249,27,C.cream);
 text(s,"Коммерческая модель и спрос пока не проверены",72,622,1050,44,25,C.muted);
 note(s,facts.speech[7],["docs/CONCEPT.md","Бизнес-модель и пилот — гипотезы команды. Выручка и спрос не измерялись."]);
}
{
 const s=slide("Материалы проекта",true);
 text(s,`${facts.pwa?.enabled?"Мобильный прототип и PWA":"Рабочий прототип"}\nВоспроизводимый сбор POI\nСценарии и ограничения\nПрезентация и демо`,72,192,1110,253,43,C.ink,true);
 const temporary=Boolean(facts.pwa?.demo_url);
 text(s,"Ограничения: приблизительный путь, неполные POI,\nнет текущих событий и проверки безопасности"+(facts.pwa?.enabled?"\nОфлайн: оболочка PWA. Подбор требует сервера":"")+(temporary?"\nДемо зависит от включённых ПК и туннеля":""),72,temporary?472:facts.pwa?.enabled?488:499,1110,temporary?144:facts.pwa?.enabled?126:90,27,C.gray);
 text(s,temporary?`Временно: ${facts.pwa.demo_url.replace(/^https:\/\//,"").replace(/\/$/,"")}`:"github.com/ltybcs/astana-walks-ai",72,630,1060,44,27,C.ink);
 note(s,facts.speech[8],[facts.repository,facts.pwa?.demo_url||"",facts.pwa?.https_evidence||"",facts.video_url||"Публикация YouTube не подтверждена.",`Снимок материалов: ${facts.generated_at}`]);
}

for(const [i,s] of deck.slides.items.entries()) {
 const png=await deck.export({slide:s,format:"png",scale:2});
 await fs.writeFile(path.join(buildDir,"slides",`slide-${String(i+1).padStart(2,"0")}.png`),new Uint8Array(await png.arrayBuffer()));
}
const draft=path.join(buildDir,"candidate.pptx");
await (await PresentationFile.exportPptx(deck)).save(draft);
const skillDir=process.env.CODEX_PRESENTATIONS_SKILL || path.join(process.env.USERPROFILE||"", ".codex","plugins","cache","openai-primary-runtime","presentations","26.1017.11052","skills","presentations");
const finalPath=path.join(outDir,"presentations","astana-walks.pptx");
await fs.mkdir(path.join(outDir,"build"),{recursive:true});
const buildStamp=String(Date.now());
const temporaryFinal=path.join(outDir,"build",`validated-${buildStamp}.pptx`);
const receiptPath=path.join(buildDir,`presentation-validation-${buildStamp}.json`);
const {finalizePresentation}=await import(pathToFileURL(path.join(skillDir,"container_tools","artifact_tool_utils.mjs")).href);
await finalizePresentation({workspaceDir,candidatePath:draft,finalPath:temporaryFinal,pythonExecutable:process.env.CODEX_RUNTIME_PYTHON||path.join(runtimeRoot,"python","python.exe"),integrityValidatorPath:path.join(skillDir,"container_tools","inspect_presentation_package_integrity.py"),layoutValidatorPath:path.join(skillDir,"container_tools","inspect_presentation_layout_geometry.py"),layoutArgs:["--expected-slide-size-emu","12192000,6858000","--validate-heading-fit","--require-native-table-slide","6"],explicitTotalSlideCount:9,requiredNativeTableOwnerSlides:[6],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:"design",families:["Arial"]},verifyArtifactToolImport:true,receiptPath});
await fs.copyFile(receiptPath,path.join(buildDir,"presentation-validation.json"));
await fs.copyFile(temporaryFinal,finalPath);
console.log(JSON.stringify({pptx:finalPath,slides:9}));
