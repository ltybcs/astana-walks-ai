"""Build the jury package from a real dataset and a recorded evaluation.

This script never substitutes sample metrics. It refuses demo data, missing
evidence, failed scenarios and an absent product screenshot.
Runtime: reportlab, pypdf, Pillow and Node + @oai/artifact-tool.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEAL = "#0A2D2E"
CREAM = "#F4F0E5"
ACCENT = "#167C6B"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def relative(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def place_count(value):
    return f"{value} "+("место" if value==1 else "места" if 2<=value<=4 else "мест")


def protocol_note(protocol):
    if protocol.get("not_a_new_sequential_full_generation_run"):
        reused=protocol.get("main_responses_revalidated_offline",0)
        fresh=len(protocol.get("new_family_provider_calls",[]))
        accepted=protocol.get("final_accepted_provider_calls",0)
        total=protocol.get("unique_observed_provider_calls_including_rejected_history",0)
        return (f'Свидетельства получены в разное время: {reused} основных ответа повторно проверены '
                f'новым guard без генерации, семейный пресет сгенерирован отдельно ({fresh} вызов). '
                f'Принято {accepted} ответов; всего наблюдалось {total} уникальных вызовов, включая исторический отказ. '
                'Это не новый последовательный прогон всех генераций.')
    return "Результаты относятся к записанной проверке и не гарантируют качество будущих ответов."


def speech(f):
    """Ten-slide speech; all run metrics come from the recorded evidence."""
    d, featured = f["dataset"], f["featured"]
    summary = f["evaluation"].get("llm_summary") or {}
    if f["llm"]["live_verified"]:
        generation = (f'В записанных основных сценариях LLM принята в {summary.get("validated_scenarios", 0)} из '
                      f'{summary.get("eligible_scenarios", 0)} непустых основных сценариев. '
                      f'Локальный fallback использован в {summary.get("fallback_scenarios", 0)} случаях. ')
        if f["evaluation"].get("validation_protocol",{}).get("not_a_new_sequential_full_generation_run"):
            generation += "Основные ответы повторно проверены новым guard без генерации; семейный пресет сгенерирован отдельно. Это не новый полный последовательный прогон. "
    else:
        generation = "В этом прогоне работает обозначенный локальный extractive-режим. Живой вызов LLM не подтверждён. "
    phone = f.get("pwa", {}).get("phone_installation", {})
    phone_text = (" Установку предыдущей версии на Android с Chrome и запуск по иконке пользователь подтвердил ранее. "
                  "Новые функции и работу без сети на физическом телефоне отдельно не проверяли.") if phone.get("user_reported") else ""
    return [
        "Здравствуйте. Astana Walks помогает жителю или гостю Астаны выбрать короткую прогулку. "
        "Мы решаем первую задачу НИЦ Smart City: учитываем старт, интересы и свободное время, "
        "находим до трёх городских мест и объясняем выбор. Вместо длинного списка человек получает "
        "небольшой план с возвращением, источниками и понятными ограничениями. Начнём с ситуации возле EXPO.",
        f'Пользователь говорит: «{featured["query"]}». Старт в этом примере смоделирован. '
        f'При бюджете {featured["minutes"]} минут найдены {", ".join(featured["names"])}. '
        f'Расчётный план занимает {featured["total_minutes"]} минут вместе с возвращением. '
        "Посещение и обратная дорога входят в бюджет. Расстояние пока геометрическое: мы прямо "
        "предупреждаем, что переходы, ограды и состояние тротуаров могут изменить реальный путь.",
        f'На фактическом снимке UI план занимает {f["ui_example"]["total_minutes"]} минуты: здесь выбрана другая публичная точка старта, чем в проверке на {featured["total_minutes"]} минуты. '
        "В карточке видны объяснение, идея действия, "
        "расчётное время, источник и режим генерации. Сведения POI отделены от предложенного действия. "
        "Мобильный интерфейс поддерживает установку PWA. Оболочка доступна без сети, но новый подбор "
        "требует backend, а ответы API не кэшируются." + phone_text,
        "Теперь у приложения есть настоящая уличная карта Leaflet с подложкой OpenStreetMap. "
        "На ней видны места каталога, начало и пронумерованные остановки. Карту можно перемещать "
        "и масштабировать, а источник открыть из маркера. Линии соединяют остановки по прямой: "
        "это схема плана, а не пешеходная навигация. При недоступности подложки остаются точки "
        "и список. Внешний сервис тайлов получает IP и область просмотра.",
        "RAG начинается с автоматически собранных публичных POI. BM25 и категории сопоставляют "
        "интересы с описаниями, затем планировщик учитывает расстояние, возврат и обязательные "
        "ограничения. Модель получает только найденные публичные факты и признаки интересов. "
        "После генерации проверяются идентификаторы, структура и известные рискованные утверждения. "
        + generation + "Пустая выдача при отсутствии подтверждённой розетки не требует вызова модели. "
        "Проверка структуры не гарантирует смысловую достоверность каждой будущей фразы.",
        f'В поставляемом снимке {d["count"]} реальных OSM POI, собранных {d["date_label"]}. '
        f'Для {d["missing_hours"]} мест часы не указаны. Мы сохраняем источник, дату, исходный снимок '
        "и лицензию. Обновление OSM включается по согласию: срок свежести составляет сутки, "
        "а при сетевой ошибке сохраняется последний целый набор. Отдельный живой поиск 2ГИС "
        "возвращает одну страницу до десяти карточек по выбранной категории. Лимит — двадцать "
        "попыток за скользящие сутки, включая ошибки. Эти карточки не сохраняются и не смешиваются с OSM или RAG.",
        f'Отчёт содержит пять основных сценариев и {f["tests"]["passed"]} пройденных автоматических тестов. '
        "Проверяются бюджет с возвратом, источники, число рекомендаций и явные ограничения. "
        "Тишина не считается доказанной по категории кафе. Розетка требует источника. Детская "
        "категория не доказывает безопасность, а ночной запрос не делает неизвестные часы "
        "круглосуточными. Дополнительный семейный UI-пресет записан отдельно. Формальный pass "
        "и ручное чтение объяснений отвечают на разные вопросы.",
        "Приложение не создаёт аккаунт или историю прогулок и не сохраняет запросы и точные "
        "координаты в логах или localStorage. Исходный запрос и старт не передаются модели. "
        "2ГИС получает только фиксированную категорию и границы Астаны; на диске остаются времена "
        "попыток для лимита. Уличные тайлы раскрывают область просмотра. Временное HTTPS-демо "
        "передаёт трафик через Cloudflare к локальному backend. Поэтому локальная модель не означает, "
        "что весь сетевой путь ограничен устройством пользователя.",
        "Бизнес-гипотеза — помощник для отелей и туристических стоек, где гостю нужен план на "
        "короткое свободное время. Спрос пока не подтверждён. В пилоте измерим долю выбранных "
        "планов и полезность объяснений. Следующие шаги — маршрутизация по пешеходным дорогам, "
        "проверка свежести часов, оценка с жителями и казахский с английским. Будущие платные "
        "позиции потребуют явной маркировки.",
        "Для передачи подготовлены исходники, данные с происхождением, сценарии, ограничения "
        "и материалы запуска. Прежний снимок сохранён отдельно от расширенной версии. "
        "YouTube-публикацию пользователь решил выполнить самостоятельно позже, поэтому ссылка "
        "не подменяется вымышленным адресом. Временный сайт зависит от включённого компьютера "
        "и туннеля. Проект остаётся прототипом: путь приблизителен, полевой проверки и гарантии "
        "безопасности нет. Спасибо, готовы показать фактический сценарий."
    ]


def assemble(args):
    dataset_path = args.data.resolve()
    evaluation_path = args.evaluation.resolve()
    raw = read_json(dataset_path)
    evaluation = read_json(evaluation_path)
    if evaluation.get("manual_review",{}).get("quality_pass") is False:
        raise ValueError("Recorded evaluation failed manual prose review. Resolve the issue and record fresh evidence before final build.")
    meta = raw.get("dataset", {})
    if meta.get("is_demo", True):
        raise ValueError("Final submission requires a real collected dataset (is_demo=false).")
    if not raw.get("places") or not meta.get("collected_at"):
        raise ValueError("Dataset must contain real POI and collected_at.")
    if evaluation.get("dataset", {}).get("is_demo", True):
        raise ValueError("Evaluation used demo data.")
    if evaluation.get("dataset", {}).get("places_count") != len(raw["places"]):
        raise ValueError("Evaluation and dataset POI count differ. Repeat evaluation.")
    if evaluation.get("dataset", {}).get("sha256") != sha256(dataset_path):
        raise ValueError("Evaluation SHA-256 differs from dataset. Repeat evaluation.")
    scenario_rows = evaluation.get("scenarios", [])
    if len(scenario_rows) != 5 or not all(r.get("pass") for r in scenario_rows):
        raise ValueError("Expected five passed scenario records, including honest empty results.")
    tests = evaluation.get("summary", {}).get("tests") or {}
    if not tests.get("success") or not tests.get("passed"):
        raise ValueError("Evaluation must include successful --run-tests evidence.")
    if not args.screenshot.is_file():
        raise ValueError("A real local product screenshot (JPEG or PNG) is required.")
    config = read_json(args.config) if args.config else {}
    pwa=dict(config.get("pwa",{"enabled":False}))
    if pwa.get("https_evidence"):
        https_path=Path(pwa["https_evidence"])
        if not https_path.is_absolute():https_path=ROOT/https_path
        https_check=read_json(https_path)
        if not https_check.get("pass") or not https_check.get("checks") or not all(c.get("pass") for c in https_check["checks"]):
            raise ValueError("HTTPS resource evidence is incomplete or failed.")
        if str(https_check["url"]).rstrip("/")!=str(pwa.get("demo_url","")).rstrip("/"):
            raise ValueError("Temporary demo URL differs from HTTPS resource evidence.")
        pwa["https_resource_check"]={"passed":sum(bool(c.get("pass")) for c in https_check["checks"]),"total":len(https_check["checks"]),"generated_at":https_check["generated_at"],"source":relative(https_path),"sha256":sha256(https_path)}
    if pwa.get("phone_evidence"):
        phone_path=Path(pwa["phone_evidence"])
        if not phone_path.is_absolute():phone_path=ROOT/phone_path
        phone_check=read_json(phone_path)
        if phone_check.get("method")!="user_reported" or phone_check.get("installed_on_physical_phone") is not True or phone_check.get("launched_from_home_screen_icon") is not True:
            raise ValueError("Phone evidence must record the user's installation and icon-launch report.")
        if str(phone_check["url"]).rstrip("/")!=str(pwa.get("demo_url","")).rstrip("/"):
            raise ValueError("Temporary demo URL differs from phone report.")
        reported=datetime.fromisoformat(phone_check["recorded_at"].replace("Z","+00:00")).astimezone(timezone(timedelta(hours=5)))
        pwa["phone_installation"]={"user_reported":True,"reported_on":reported.date().isoformat(),"installed":True,"opened_from_icon":True,"os":phone_check.get("device_os"),"browser":phone_check.get("browser"),"device_model":phone_check.get("device_model"),"offline_tested":phone_check.get("offline_on_physical_phone") in (True,"passed","failed"),"source":relative(phone_path),"sha256":sha256(phone_path)}
    crop=config.get("screenshot_crop",{"left":0,"top":0,"right":0,"bottom":0})
    if set(crop)!={"left","top","right","bottom"} or any(type(v) not in (int,float) or not 0<=v<1 for v in crop.values()) or crop["left"]+crop["right"]>=1 or crop["top"]+crop["bottom"]>=1:
        raise ValueError("screenshot_crop must give four valid normalized edge fractions.")
    from PIL import Image
    with Image.open(args.screenshot) as screenshot:
        shot_w,shot_h=screenshot.size
        shot_format=screenshot.format
    if shot_format not in {"JPEG","PNG"}:
        raise ValueError("Product screenshot must contain JPEG or PNG image bytes.")
    shot_mime={"JPEG":"image/jpeg","PNG":"image/png"}[shot_format]
    crop_w=shot_w*(1-crop["left"]-crop["right"])
    crop_h=shot_h*(1-crop["top"]-crop["bottom"])
    screenshot_layout=config.get("screenshot_layout","full")
    if screenshot_layout not in {"full","detail"}:raise ValueError("Unknown screenshot_layout")
    area_left,area_width=(600,608) if screenshot_layout=="detail" else (72,1136)
    shot_scale=min(area_width/crop_w,493/crop_h)
    shot_frame={"left":area_left+(area_width-crop_w*shot_scale)/2,"top":148+(493-crop_h*shot_scale)/2,"width":crop_w*shot_scale,"height":crop_h*shot_scale}
    mobile_path=Path(config.get("mobile_screenshot","output/demo-mobile.jpg"))
    if not mobile_path.is_absolute():mobile_path=ROOT/mobile_path
    mobile_frame=None
    mobile_mime=None
    if config.get("pwa",{}).get("enabled") and mobile_path.is_file():
        with Image.open(mobile_path) as mobile:
            mobile_w,mobile_h=mobile.size
            mobile_format=mobile.format
        if mobile_format not in {"JPEG","PNG"}:
            raise ValueError("Mobile screenshot must contain JPEG or PNG image bytes.")
        mobile_mime={"JPEG":"image/jpeg","PNG":"image/png"}[mobile_format]
        mobile_scale=min(222/mobile_w,455/mobile_h)
        mobile_frame={"left":72+(222-mobile_w*mobile_scale)/2,"top":170,"width":mobile_w*mobile_scale,"height":mobile_h*mobile_scale}
    llm = config.get("llm", {})
    llm.setdefault("provider", "Не подключён")
    llm.setdefault("model", "Не подключена")
    llm["live_verified"] = bool(llm.get("live_verified") and evaluation.get("live_llm_used"))
    narration_source=config.get("narration_evidence","output/live-explanations.json")
    narration_path=Path(narration_source)
    if not narration_path.is_absolute():narration_path=ROOT/narration_path
    narration=None
    if llm["live_verified"]:
        if evaluation.get("manual_review",{}).get("quality_pass") is not True:
            raise ValueError("Live LLM build requires evaluation manual_review.quality_pass=true.")
        if not narration_path.is_file():
            raise ValueError("Live LLM build requires the recorded narration file.")
        narration=read_json(narration_path)
        if narration.get("manual_review",{}).get("quality_pass") is not True:
            raise ValueError("Live LLM build requires narration manual_review.quality_pass=true.")
        if narration.get("dataset",{}).get("sha256")!=sha256(dataset_path):
            raise ValueError("Narration evidence used a different POI snapshot.")
    llm_run=evaluation.get("summary",{}).get("llm")
    validation_protocol=evaluation.get("evidence",{}).get("validation_protocol",{})
    llm["evidence"]=(f'Actual report: {relative(evaluation_path)}. '
        f'{evaluation["summary"]["scenarios_passed"]}/{evaluation["summary"]["scenarios_total"]} main scenarios, '
        f'{tests["passed"]}/{tests["run"]} tests. '
        +(f'{llm_run["validated_scenarios"]}/{llm_run["eligible_scenarios"]} eligible scenarios validated, {llm_run["total_attempts"]} recorded provider calls in main scenarios. ' if llm_run else "Live LLM not recorded. ")
        +protocol_note(validation_protocol))
    llm["privacy_note"] = "LLM получает признаки интересов и найденные публичные POI: name, category, description, features. Исходный запрос, origin, координаты, расчётные времена, расстояния и opening_hours не передаются. Числа и расписания отображаются отдельно из backend, а не генерируются в свободном тексте."
    from app.retrieval import Retriever
    from app.generation import recommend
    first = scenario_rows[0]
    response = recommend(Retriever(dataset_path), first["query"], first["minutes"], first["start"], use_llm=False)
    recs = response["recommendations"]
    if not recs:
        raise ValueError("Featured EXPO scenario has no recommendations. Select verified evidence first.")
    generated_why=None
    if narration is not None:
        narr_recs=narration["scenarios"][0]["recommendations"]
        if [r["id"] for r in narr_recs]!=[r["id"] for r in recs]:
            raise ValueError("Narration evidence and featured plan differ.")
        generated_why=narr_recs[0]["why"]
    if screenshot_layout=="detail" and not generated_why:
        raise ValueError("Detail layout requires the actual reviewed explanation from the recorded run.")
    ui_path=Path(config.get("ui_evidence","output/ui-check.json"))
    if not ui_path.is_absolute():ui_path=ROOT/ui_path
    ui_check=read_json(ui_path)
    if ui_check.get("dataset_sha256")!=sha256(dataset_path):
        raise ValueError("UI evidence used a different POI snapshot.")
    if ui_check.get("query")!=first["query"] or ui_check.get("minutes")!=first["minutes"]:
        raise ValueError("UI screenshot evidence must use the featured query and budget.")
    ui_minutes=re.search(r"≈\s*(\d+)\s*мин",ui_check.get("plan",""))
    if not ui_minutes:
        raise ValueError("UI evidence must record the actual total plan minutes.")
    ui_shots={shot["path"]:shot for shot in ui_check.get("screenshots",[])}
    if ui_shots.get(relative(args.screenshot),{}).get("sha256")!=sha256(args.screenshot):
        raise ValueError("UI evidence screenshot hash is absent or differs.")
    if mobile_frame and ui_shots.get(relative(mobile_path),{}).get("sha256")!=sha256(mobile_path):
        raise ValueError("Mobile screenshot hash is absent from UI evidence or differs.")
    ui_example={"source":relative(ui_path),"sha256":sha256(ui_path),"recorded_at":ui_check["recorded_at"],
                "query":ui_check["query"],"minutes":ui_check["minutes"],"total_minutes":int(ui_minutes.group(1)),
                "mode":ui_check.get("rendered_mode"),"origin":ui_check.get("origin"),
                "scope":ui_check.get("scope"),"limitations":ui_check.get("limitations",[])}
    features={key:dict(value) for key,value in config.get("features",{}).items()}
    street=features.get("street_map",{})
    map_path=None
    if street.get("available"):
        if street.get("screenshot_dataset_sha256")!=sha256(dataset_path):
            raise ValueError("Map screenshot must be confirmed for the current dataset SHA-256.")
        map_path=Path(street.get("screenshot","output/map-desktop.jpg"))
        if not map_path.is_absolute():map_path=ROOT/map_path
        if not map_path.is_file():raise ValueError("A current actual street-map screenshot is required.")
        if ui_shots.get(relative(map_path),{}).get("sha256")!=sha256(map_path):
            raise ValueError("Map screenshot hash is absent from UI evidence or differs.")
        map_crop=street.get("crop",{"left":0,"top":0,"right":0,"bottom":0})
        if set(map_crop)!={"left","top","right","bottom"} or any(type(v) not in (int,float) or not 0<=v<1 for v in map_crop.values()) or map_crop["left"]+map_crop["right"]>=1 or map_crop["top"]+map_crop["bottom"]>=1:
            raise ValueError("Street-map screenshot crop is invalid.")
        with Image.open(map_path) as map_image:
            mw,mh=map_image.size
            mf=map_image.format
        if mf not in {"JPEG","PNG"}:raise ValueError("Map screenshot must contain JPEG or PNG bytes.")
        cw=mw*(1-map_crop["left"]-map_crop["right"])
        ch=mh*(1-map_crop["top"]-map_crop["bottom"])
        scale=min(1136/cw,446/ch)
        street.update(screenshot_relative=relative(map_path),screenshot_sha256=sha256(map_path),content_type={"JPEG":"image/jpeg","PNG":"image/png"}[mf],crop=map_crop,frame={"left":72+(1136-cw*scale)/2,"top":158+(446-ch*scale)/2,"width":cw*scale,"height":ch*scale})
        street.pop("screenshot",None)
        features["street_map"]=street
    if features.get("osm_refresh",{}).get("available"):
        from app.data_refresh import TTL_SECONDS, COOLDOWN_SECONDS
        features["osm_refresh"].update(ttl_hours=TTL_SECONDS//3600,retry_cooldown_hours=COOLDOWN_SECONDS//3600,opt_in=True,public_http_trigger=False)
    if features.get("two_gis",{}).get("available"):
        from app.dgis_live import PAGE_SIZE, LIMIT, WINDOW_SECONDS, ATTRIBUTION
        features["two_gis"].update(page_size=PAGE_SIZE,attempt_limit=LIMIT,window_hours=WINDOW_SECONDS//3600,attribution=dict(ATTRIBUTION),poi_persistence=False,separate_from_osm_and_rag=True)
    video=dict(config.get("video",{}))
    if video.get("current_mp4"):
        video_path=Path(video["current_mp4"])
        if not video_path.is_absolute():video_path=ROOT/video_path
        if not video_path.is_file() or video.get("current_dataset_sha256")!=sha256(dataset_path):
            raise ValueError("Current video must exist and be approved for the current dataset SHA-256.")
        video.update(current_mp4=relative(video_path),sha256=sha256(video_path))
    local_tz = timezone(timedelta(hours=5))
    collected = datetime.fromisoformat(meta["collected_at"].replace("Z", "+00:00")).astimezone(local_tz)
    rows = []
    labels = ["Прогулка у EXPO", "Кофе, тишина, розетка", "Прогулка с ребёнком", "Турист, два часа", "Ночная активность"]
    for i, row in enumerate(scenario_rows):
        count=row["result_count"]
        label = f'{place_count(count)}, {row["total_minutes"]} мин' if count else "Нет подтверждённых мест"
        rows.append({**row, "passed": row["pass"], "short_name":labels[i],"result_label":label})
    facts = {
        "generated_at":datetime.now(local_tz).isoformat(timespec="seconds"),
        "repository":"https://github.com/ltybcs/astana-walks-ai",
        "video_url":config.get("video_url"),
        "screenshot":str(args.screenshot.resolve()),"screenshot_relative":relative(args.screenshot),
        "screenshot_content_type":shot_mime,"screenshot_sha256":sha256(args.screenshot),
        "screenshot_crop":crop,
        "screenshot_frame":shot_frame,
        "screenshot_layout":screenshot_layout,
        "mobile_screenshot":str(mobile_path) if mobile_frame else None,
        "mobile_screenshot_relative":relative(mobile_path) if mobile_frame else None,
        "mobile_screenshot_content_type":mobile_mime,"mobile_screenshot_sha256":sha256(mobile_path) if mobile_frame else None,
        "mobile_frame":mobile_frame,
        "dataset":{"count":len(raw["places"]),"missing_hours":sum(not p.get("opening_hours") for p in raw["places"]),"date_label":collected.strftime("%d.%m.%Y"),"path":relative(dataset_path),"sha256":sha256(dataset_path),"source_name":meta.get("source_provider") or meta.get("name","Источник POI"),"source_url":meta.get("license_url") or raw["places"][0]["source_url"],"license_url":meta.get("license_url"),"attribution":meta.get("attribution","Атрибуция не указана"),"license":meta.get("license","Условия в метаданных не указаны"),"metadata":meta},
        "evaluation":{"source":relative(evaluation_path),"sha256":sha256(evaluation_path),"scenarios":rows,"llm_summary":evaluation.get("summary",{}).get("llm"),"additional_checks":evaluation.get("additional_checks",[]),"validation_protocol":validation_protocol},
        "tests":{**tests,"source":relative(evaluation_path)},
        "llm":llm,
        "pwa":pwa,
        "features":features,
        "map_screenshot":str(map_path) if map_path else None,
        "video":video,
        "ui_example":ui_example,
        "featured":{"query":first["query"],"minutes":first["minutes"],"total_minutes":response["plan"]["total_minutes"],"names":[r["name"] for r in recs],"result_count":len(recs),"mode":first["mode"],"generated_why":generated_why,"narration_source":relative(narration_path) if generated_why else None},
    }
    facts["speech"] = speech(facts)
    return facts


def document_pdf(f, out):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, KeepTogether
    fonts = Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts"
    font_path = Path(os.environ.get("ASTANA_FONT", str(fonts/"arial.ttf")))
    bold_path = Path(os.environ.get("ASTANA_BOLD_FONT", str(fonts/"arialbd.ttf")))
    pdfmetrics.registerFont(TTFont("Astana",str(font_path)))
    pdfmetrics.registerFont(TTFont("AstanaBold",str(bold_path)))
    pdfmetrics.registerFontFamily("Astana",normal="Astana",bold="AstanaBold")
    styles = {
        "title":ParagraphStyle("title",fontName="AstanaBold",fontSize=29,leading=34,textColor=HexColor(TEAL),spaceAfter=20),
        "h1":ParagraphStyle("h1",fontName="AstanaBold",fontSize=21,leading=26,textColor=HexColor(TEAL),spaceAfter=15),
        "h2":ParagraphStyle("h2",fontName="AstanaBold",fontSize=13,leading=17,textColor=HexColor(TEAL),spaceBefore=13,spaceAfter=7),
        "body":ParagraphStyle("body",fontName="Astana",fontSize=10.5,leading=15.5,textColor=HexColor(TEAL),spaceAfter=9),
        "small":ParagraphStyle("small",fontName="Astana",fontSize=8.4,leading=12,textColor=HexColor("#55716A"),spaceAfter=5),
        "table":ParagraphStyle("table",fontName="Astana",fontSize=9.5,leading=14,textColor=HexColor(TEAL)),
        "thead":ParagraphStyle("thead",fontName="AstanaBold",fontSize=10,leading=14,textColor=HexColor(CREAM)),
    }
    def p(t,style="body"):
        t=str(t).replace("\u2011","-").replace("\u2013","-").replace("\u2014","-")
        return Paragraph(t,styles[style])
    def safe(t):return escape(str(t))
    story=[]
    story += [p("Astana Walks","title"),p("Тестовые сценарии,<br/>ограничения и воспроизводимость","h1"),Spacer(1,14)]
    story += [p("Задача №1 НИЦ Smart City. Build with AI / GDG Astana.")]
    story += [p("Назначение","h2"),p("Прототип помогает жителю или гостю Астаны выбрать до трёх мест рядом со стартом. Он учитывает интересы и бюджет времени с возвращением, объясняет выбор и предлагает конкретное действие. Все старты в проверочных сценариях смоделированы.")]
    story += [p("Снимок проверенной версии","h2")]
    mode=(f'LLM: {safe(f["llm"]["provider"])} / {safe(f["llm"]["model"])}. Живой вызов подтверждён отчётом.' if f["llm"]["live_verified"] else "Локальный extractive-режим. Живой вызов LLM не подтверждён, шаблонный ответ не выдаётся за LLM.")
    for t in [f'Данные: {f["dataset"]["count"]} реальных POI. Источник: {safe(f["dataset"]["source_name"])}. Сбор {f["dataset"]["date_label"]}.',f'Проверка: 5/5 сценариев, {f["tests"]["passed"]} автоматических тестов пройдено.',mode,f'Материалы собраны: {safe(f["generated_at"])} (UTC+05:00).']:
        story.append(p(t))
    llm_summary=f["evaluation"].get("llm_summary")
    if f["llm"]["live_verified"] and llm_summary:
        story.append(p(f'В пяти основных сценариях LLM принята в {llm_summary["validated_scenarios"]} из {llm_summary["eligible_scenarios"]} непустых случаев, записано {llm_summary["total_attempts"]} вызова модели. При отклонении ответа допускается одна корректирующая попытка, затем fallback.',"small"))
        story.append(p(protocol_note(f["evaluation"].get("validation_protocol",{})),"small"))
    story += [p("Ссылки и происхождение","h2"),p(f'<link href="{f["repository"]}" color="#167C6B">Репозиторий ltybcs/astana-walks-ai</link>'),p(safe(f["dataset"]["attribution"])+". "+safe(f["dataset"]["license"]))]
    if f.get("pwa",{}).get("demo_url"):
        story.append(p(f'<link href="{escape(f["pwa"]["demo_url"])}" color="#167C6B">Временное HTTPS-демо</link> (при включённых ПК и туннеле).'))
    if f["dataset"].get("license_url"):
        story.append(p(f'<link href="{escape(f["dataset"]["license_url"])}" color="#167C6B">Условия использования данных</link>'))
    if f.get("video_url"):story.append(p(f'<link href="{escape(f["video_url"])}" color="#167C6B">Демонстрация на YouTube</link>'))
    elif f.get("video",{}).get("publication")=="user_upload_later":
        story.append(p("Пользователь решил загрузить видео на YouTube самостоятельно позже. Ссылка пока отсутствует.","small"))
    story += [p("Сведения POI получены из источника и не являются полевой проверкой каждого объекта. Прототип оценивает время геометрически и не гарантирует проходимость или безопасность маршрута.","small"),PageBreak()]
    story += [p("Пять сценариев из условия","h1"),p("Критерии: 0–3 рекомендации, подтверждённые источники, соблюдение явных ограничений и времени с возвратом. Пустая выдача проходит проверку, если подходящих подтверждённых мест нет.")]
    expected=[
        "1–3 места для прогулки. Время перемещений, посещений и возврата укладывается в бюджет.",
        "Розетка только по подтверждению источника. Неизвестная тишина явно отмечается. При требовании «только тихое» неизвестные исключаются.",
        "Парк, площадка или подходящий музей. Не обещать интерактивы, безопасность и доступность без данных.",
        "Достопримечательности в пределах времени. Не считать дорогу до аэропорта и не гарантировать успеть на рейс.",
        "Только POI с часами 24/7 в источнике. Это не доказывает доступность или безопасность сейчас. Неизвестные часы не означают «открыто сейчас»."
    ]
    for i,r in enumerate(f["evaluation"]["scenarios"]):
        if i==3:
            story += [PageBreak(),p("Сценарии и границы проверки","h1")]
        ctx=f'{r["start"]["label"]}, {r["minutes"]} мин'
        story += [p(f'{i+1}. {safe(r["short_name"])}',"h2"),p(f'<b>Запрос:</b> «{safe(r["query"])}»<br/><b>Контекст:</b> {safe(ctx)}'),p(f'<b>Ожидается:</b> {safe(expected[i])}'),p(f'<b>Результат:</b> {safe(r["result_label"])}. Режим {safe(r["mode"])}. Проверка пройдена.',"small")]
    story += [p("Что означает пройденная проверка","h2"),p("Pass фиксирует соблюдение формальных условий конкретного запроса: число пунктов, бюджет, источники и обязательные ограничения. Он не означает, что жители оценили ответ как полезный, что каталог полон или что каждую фразу модели проверил человек."),p("Пустая выдача и генерация","h2"),p("Строгий запрос может не иметь подтверждённых кандидатов. В таком случае приложение объясняет причину и не вызывает модель для выдумывания альтернатив. При наличии кандидатов LLM может подготовить объяснения, но не меняет найденные места и бюджет плана.")]
    for extra in f["evaluation"].get("additional_checks",[]):
        story += [p("Дополнительная проверка UI-пресета","h2"),p(f'«{safe(extra["query"])}»: {place_count(extra["result_count"])}, {extra["total_minutes"]} мин, режим {safe(extra["mode"])}. Обращений к модели: {extra.get("llm_attempts",0)}. Эта отдельная проверка не входит в пять основных сценариев и их число обращений к LLM.',"small")]
    story.append(p(f'Снимок MEGA в интерфейсе: {f["ui_example"]["total_minutes"]} мин; формальная проверка: {f["featured"]["total_minutes"]} мин. Публичные точки старта различаются. Снимок — отдельный UI-ответ, не часть смешанного протокола оценки.',"small"))
    features=f.get("features",{})
    if any(item.get("available") for item in features.values()):
        story += [PageBreak(),p("Карта и источники онлайн","h1")]
    if features.get("street_map",{}).get("available"):
        story += [p("Уличная карта Leaflet / OpenStreetMap","h2"),p("Подложка показывает улицы, маркеры — POI текущего OSM-набора, старт и остановки плана. Связи проведены по прямой и не являются пешеходной маршрутизацией. Внешний сервис тайлов получает IP и область просмотра; приближение к выбранному старту выполняется отдельной кнопкой после уведомления. При сбое подложки остаются точки и список.")]
    refresh=features.get("osm_refresh",{})
    if refresh.get("available"):
        story += [p("Обновление OSM по согласию","h2"),p(f'Фоновое обновление включается локально через --refresh-poi. TTL — {refresh["ttl_hours"]} ч, пауза повторной попытки после ошибки — {refresh["retry_cooldown_hours"]} ч. Проверенный набор заменяется атомарно, при сбое сохраняется последний целый снимок. Кэш публичных OSM POI находится в игнорируемой .local/poi. Публичного HTTP-запуска обновления нет.')]
    live=features.get("two_gis",{})
    if live.get("available"):
        story += [p("Отдельный живой поиск 2ГИС","h2"),p(f'Одна кнопка отправляет один запрос по фиксированной категории и границам Астаны: первая страница, до {live["page_size"]} объектов. Общий лимит — {live["attempt_limit"]} попыток за скользящие {live["window_hours"]} ч, включая ошибки. Нужен серверный ключ. Исходный запрос и личная локация не отправляются. Карточки живут только в памяти ответа, HTTP private/no-store; в файлы, RAG и слой OSM они не попадают. На диске остаётся только счётчик попыток.'),p('<link href="http://dev.2gis.ru" color="#167C6B">2ГИС</link> — самостоятельный источник; его данные не распространяются по ODbL.',"small")]
    if f.get("pwa",{}).get("enabled"):
        story += [p("Телефон, PWA и офлайн","h2"),p("PWA сохраняет оболочку. Подбор и живой поиск требуют backend; API не кэшируется. Тайлы карты требуют сети. Для телефона нужен HTTPS, а localhost подходит для разработки на том же устройстве.","small")]
        if f["pwa"].get("phone_installation",{}).get("user_reported"):
            phone=f["pwa"]["phone_installation"]
            platform=", ".join(value for value in [phone.get("os"),phone.get("browser")] if value)
            story.append(p("Пользователь ранее подтвердил установку предыдущей версии по HTTPS и запуск по иконке на телефоне"+safe((" ("+platform+")") if platform else "")+". Модель устройства не указана; новые функции и работа без сети на физическом телефоне отдельно не проверены.","small"))
        else:
            story.append(p("Установка на физический телефон не подтверждена.","small"))
        if f["pwa"].get("demo_url"):
            check=f["pwa"].get("https_resource_check",{})
            text="Временный адрес зависит от включённого ПК и процесса туннеля. Endpoint локальной модели отдельно не публикуется."
            if check:text+=f' Проверки ресурсов: {check["passed"]}/{check["total"]}. Это отдельное подтверждение доступности ресурсов, а не доказательство установки.'
            story.append(p(text,"small"))
        story.append(p('<link href="https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable" color="#167C6B">Требования браузера к установке PWA: MDN</link>',"small"))
    story += [PageBreak(),p("Риски, этика и приватность","h1")]
    risk_rows=[
        ("Неполные или устаревшие POI","Показываем источник, дату и неизвестные поля. Не добавляем рейтинг, часы, текущие события, розетки и доступность по догадке."),
        ("Геометрический путь","Расстояние и время приблизительны. Реальные переходы, ограды, стройки и зимние условия не проверяются. Это не пошаговая навигация."),
        ("Ночная прогулка и дети","Категория места не доказывает безопасность. Неизвестные часы и неподтверждённая доступность требуют самостоятельной проверки."),
        ("Ошибка или выдумка LLM","Проверяем принадлежность POI к retrieval, структуру и известные рискованные утверждения. Проверка не доказывает семантическую обоснованность каждой фразы. При сбое используем обозначенный локальный fallback."),
        ("Передача локации","Запрос и координаты используются в памяти. Приложение не сохраняет историю и не пишет тела запросов в логи. Третья сторона видит переход по ссылке только после выбора пользователя."),
    ]
    data=[[p("Риск","thead"),p("Обработка в прототипе","thead")]]+[[p(a,"table"),p(b,"table")]for a,b in risk_rows]
    table=Table(data,colWidths=[139,346],hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),HexColor(TEAL)),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),10),("TOPPADDING",(0,0),(-1,-1),10),("BOTTOMPADDING",(0,0),(-1,-1),10),("LINEBELOW",(0,1),(-1,-1),0.4,HexColor("#CCD5CC"))]))
    story += [table,p("Модель и данные запроса","h2"),p(f["llm"]["privacy_note"]+" При необходимости API-ключ доступен только backend через окружение. Для внешнего провайдера и хостинга нужно отдельно проверить условия обработки и хранения.")]
    if f.get("pwa",{}).get("demo_url"):
        story.append(p("Во временном HTTPS-демо сетевые запросы проходят через Cloudflare Quick Tunnel к локальному backend; локальная модель остаётся на ПК.","small"))
    if features.get("street_map",{}).get("available"):
        story.append(p("Тайлы OpenStreetMap раскрывают внешнему сервису IP и область просмотра. Текст запроса и координаты маркеров отдельными параметрами этому сервису не отправляются.","small"))
    story += [p("Коммерческие рекомендации","h2"),p("Спрос и бизнес-модель пока не проверены. Предполагаемый пилот: отели и туристические стойки. Любая будущая платная позиция должна иметь явную маркировку, не подменяя релевантность скрытым продвижением.")]
    story += [PageBreak(),p("Повторение проверки","h1"),p("Команды выполняются из корня проекта. Для запуска прототипа сторонние Python-пакеты не нужны. Финальная сборка материалов требует ReportLab, Pillow, pypdf и @oai/artifact-tool.")]
    llm_flag=" --llm" if f["llm"]["live_verified"] else ""
    commands=["python -m unittest discover -s tests -v",f'python -m app.evaluate --data {f["dataset"]["path"]} --output tmp/evaluation-check.json --run-tests{llm_flag}',f'python -m app.server --data {f["dataset"]["path"]}',"python scripts/collect_osm.py --help"]
    for command in commands:story.append(p(safe(command),"small"))
    story += [p("Источник фактических метрик","h2"),p(safe(f["evaluation"]["source"])),p("SHA-256 отчёта: "+f["evaluation"]["sha256"],"small")]
    https_evidence=f.get("pwa",{}).get("https_resource_check")
    if https_evidence:
        recorded=datetime.fromisoformat(https_evidence["generated_at"].replace("Z","+00:00")).astimezone(timezone(timedelta(hours=5)))
        story += [p("Проверка HTTPS-ресурсов","h2"),p(safe(https_evidence["source"])),p(f'{https_evidence["passed"]}/{https_evidence["total"]} проверок, {recorded.strftime("%d.%m.%Y %H:%M")} (UTC+05:00).',"small"),p("SHA-256 проверки: "+https_evidence["sha256"],"small")]
    phone_evidence=f.get("pwa",{}).get("phone_installation",{})
    if phone_evidence.get("source"):
        story.append(p("Установка со слов пользователя: "+safe(phone_evidence["source"])+". SHA-256: "+phone_evidence["sha256"],"small"))
    story += [p("Источник набора данных","h2"),p(safe(f["dataset"]["path"])),p("SHA-256 набора: "+f["dataset"]["sha256"],"small")]
    story += [p("Дополнительные автоматические проверки","h2"),p("Валидация JSON и координат, строгие фильтры, неизвестные свойства, пустая выдача, ошибки LLM и защитная проверка статических путей. Итоговые числа относятся к приложенному отчёту, а не к предполагаемой городской полноте данных.")]
    story += [p("Перед внешним размещением","h2"),p("Нужны производственный HTTP-сервер, ограничения нагрузки, HTTPS, эксплуатационные проверки и проверка свежести данных. Текущий сервер предназначен для локального демо. Не публикуйте .env, ключи и журналы с запросами или точными координатами.")]
    story += [p("Материалы для жюри","h2"),p("Редактируемая презентация PowerPoint, PDF слайдов, этот документ, сценарий выступления и видеодемонстрация. Ссылки на итоговую публикацию должны проверяться отдельно с правами получателя.")]
    if f.get("video",{}).get("current_mp4"):
        story.append(p("Актуальный локальный ролик: "+safe(f["video"]["current_mp4"])+". Пользователь загрузит его на YouTube самостоятельно; локальный файл не заменяет публикацию.","small"))
    elif f.get("video",{}).get("previous_snapshot_video"):
        story.append(p("Ролик версии с 738 POI сохранён как история. Актуальный тейк в конфиге этой сборки не указан; YouTube-ссылка остаётся пустой.","small"))
    def page(c,doc):
        w,h=A4
        c.setFillColor(HexColor(CREAM));c.rect(0,0,w,h,fill=1,stroke=0)
        c.setFillColor(HexColor(TEAL));c.setFont("Astana",8)
        c.drawString(55,31,"ASTANA WALKS  /  SMART CITY")
        c.drawRightString(w-55,31,str(doc.page))
    doc=SimpleDocTemplate(str(out),pagesize=A4,rightMargin=55,leftMargin=55,topMargin=48,bottomMargin=55,title="Astana Walks: сценарии и ограничения",author="Astana Walks team")
    doc.build(story,onFirstPage=page,onLaterPages=page)


def deck_pdf(slides, out):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    c=canvas.Canvas(str(out),pagesize=(960,540))
    c.setTitle("Astana Walks: презентация")
    c.setAuthor("Astana Walks team")
    for image in sorted(slides.glob("slide-*.png")):
        c.drawImage(ImageReader(str(image)),0,0,width=960,height=540)
        c.showPage()
    c.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data",type=Path,default=ROOT/"data/poi.astana.json")
    parser.add_argument("--evaluation",type=Path,default=ROOT/"output/evaluation.json")
    parser.add_argument("--screenshot",type=Path,required=True)
    parser.add_argument("--config",type=Path)
    parser.add_argument("--output",type=Path,default=ROOT/"output")
    parser.add_argument("--skip-deck",action="store_true",help="Rebuild scenarios PDF and speech only")
    args=parser.parse_args()
    f=assemble(args)
    build=ROOT/"tmp/submission";build.mkdir(parents=True,exist_ok=True)
    pdfs=args.output/"pdf";pdfs.mkdir(parents=True,exist_ok=True)
    args.output.mkdir(parents=True,exist_ok=True)
    evidence_path=(args.output/"evaluation.json").resolve()
    if args.evaluation.resolve()!=evidence_path:
        shutil.copyfile(args.evaluation,evidence_path)
    f["evaluation"]["source"]=relative(evidence_path)
    f["tests"]["source"]=relative(evidence_path)
    screenshot_suffix=".jpg" if f["screenshot_content_type"]=="image/jpeg" else ".png"
    screenshot_path=(args.output/("demo-ui"+screenshot_suffix)).resolve()
    if args.screenshot.resolve()!=screenshot_path:
        shutil.copyfile(args.screenshot,screenshot_path)
    f["screenshot"]=str(screenshot_path)
    f["screenshot_relative"]=relative(screenshot_path)
    facts_path=build/"facts.json"
    facts_path.write_text(json.dumps(f,ensure_ascii=False,indent=2),encoding="utf-8")
    document_pdf(f,pdfs/"astana-walks-scenarios.pdf")
    speech_lines=["# Astana Walks: речь на 6 минут 45 секунд","","Темп и длительность ориентировочны. Проведите репетицию перед выступлением.",""]
    timings=["0:00–0:30","0:30–1:05","1:05–1:55","1:55–2:35","2:35–3:20","3:20–4:10","4:10–4:55","4:55–5:40","5:40–6:10","6:10–6:45"]
    for i,t in enumerate(f["speech"]):speech_lines += [f"## Слайд {i+1}, {timings[i]}","",t,""]
    (args.output/"speaker-script.md").write_text("\n".join(speech_lines),encoding="utf-8")
    public_facts={k:v for k,v in f.items() if k not in {"screenshot","mobile_screenshot","map_screenshot","speech"}}
    (args.output/"submission-facts.json").write_text(json.dumps(public_facts,ensure_ascii=False,indent=2),encoding="utf-8")
    if not args.skip_deck:
        runtime=Path(os.environ.get("CODEX_ARTIFACT_RUNTIME",str(Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies")))
        node=os.environ.get("CODEX_RUNTIME_NODE") or str(runtime/"node/bin/node.exe")
        if not Path(node).is_file():node=shutil.which("node")
        if not node:raise RuntimeError("Node.js is required for the editable PPTX build.")
        subprocess.run([node,str(ROOT/"scripts/build_submission.mjs"),str(ROOT),str(facts_path),str(args.output.resolve())],check=True)
        deck_pdf(build/"slides",pdfs/"astana-walks-presentation.pdf")
    from pypdf import PdfReader
    for pdf in pdfs.glob("astana-walks*.pdf"):
        print(f"{relative(pdf)}: {len(PdfReader(pdf).pages)} pages")
    print(f"Evidence: {relative(args.output/'submission-facts.json')}")


if __name__=="__main__":
    main()
