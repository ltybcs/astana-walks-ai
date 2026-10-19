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


def speech(f):
    """Speaker text, deliberately tied to the recorded evidence."""
    d, featured = f["dataset"], f["featured"]
    mode = (f'Модель {f["llm"]["model"]} получила только найденные POI и сформировала объяснения. '
            if f["llm"]["live_verified"] else
            "В этой демонстрации работает локальный extractive-режим. Он формирует объяснения по найденным POI. Живой вызов LLM не подтверждён. ")
    result = [
        "Здравствуйте. Мы представляем Astana Walks, помощника для коротких прогулок в Астане. "
        "Он решает первую задачу НИЦ Smart City: учитывает стартовую точку, интересы и время человека, "
        "ищет подходящие городские места и объясняет рекомендации. Наш пользователь может быть жителем, "
        "гостем города или туристом, у которого есть свободный час. Ему нужен небольшой понятный план, "
        "а не ещё один длинный список заведений. Начнём с обычной ситуации возле EXPO.",
        f'В этом проверочном сценарии человек говорит: «{featured["query"]}». '
        f'Он выбирает старт возле EXPO и располагает {featured["minutes"]} минутами. '
        f'Система предлагает {place_count(featured["result_count"])}: {", ".join(featured["names"])}. '
        f'Расчётный план занимает {featured["total_minutes"]} минут вместе с возвращением. '
        "Мы включаем возврат, чтобы короткая прогулка не закончилась далеко от исходной точки. "
        "Расстояние рассчитываем приближённо и прямо об этом сообщаем. Реальные переходы, ограждения "
        "и состояние тротуаров пока требуют отдельной проверки.",
        "Так выглядит фактический ответ работающего приложения. Пользователь задаёт интересы, время "
        "и смоделированный старт, затем получает до трёх рекомендаций. У каждого места видно, почему "
        "оно подходит, сколько займёт посещение и что можно там сделать. Рядом находится ссылка "
        "на источник. Мы различаем факт из каталога и предложенную идею действия. Например, внешний "
        "осмотр архитектуры можно предложить, но нельзя объявить текущую выставку без подтверждения. "
        "Пользователь также видит предупреждения и фактический режим генерации.",
        f'RAG начинается с данных. Автоматический сборщик получает публичные точки из набора {d["source_name"]}, '
        "нормализует их и сохраняет происхождение. Затем поиск BM25 сопоставляет текст интересов "
        "с описаниями и категориями. Мы учитываем расстояние, время суток и обязательные ограничения. "
        "Планировщик выбирает достижимую последовательность мест и рассчитывает возврат. " + mode +
        "После генерации приложение проверяет ссылки на найденные места и ограничения плана. "
        "При отказе провайдера оно показывает локальный ответ и честно меняет название режима.",
        f'В текущем наборе {d["count"]} реальных POI. Дата сбора: {d["date_label"]}. '
        "У объектов есть название, координаты, категория и короткое описание. Если источник содержит "
        "часы работы или подтверждённые свойства, мы сохраняем их вместе с ID источника и ссылкой. "
        "Автоматический сбор воспроизводим через скрипт и сохранённый снимок данных. Это сведения "
        "источника, а не наша полевая проверка каждого места. Поэтому отсутствие "
        f'информации остаётся неизвестностью. Атрибуция: {d["attribution"]}. Условия данных: {d["license"]}.',
        f'Мы проверили {len(f["evaluation"]["scenarios"])} сценариев из условия хакатона. '
        f'Автоматический набор содержит {f["tests"]["passed"]} пройденных тестов. '
        "В сценариях проверяем число рекомендаций, бюджет времени с возвратом, наличие источников "
        "и соблюдение явных ограничений. Прогулка возле EXPO должна давать конкретные достижимые места. "
        "Запрос про розетку требует подтверждения источника. Тишина остаётся неизвестной, "
        "а явное требование только тихого места включает строгий фильтр. Детский сценарий "
        "не должен обещать безопасность без данных. Для туриста учитывается прогулка, но не дорога "
        "в аэропорт. Ночью неизвестные часы не превращаются в обещание открытого заведения.",
        "Локация нужна только для текущего ответа. Приложение не создаёт аккаунт, историю прогулок "
        "или аналитический профиль. Оно не сохраняет запросы и точные координаты в журнале "
        "или localStorage. Во внешнюю модель можно передать только признаки интересов и отобранные "
        "публичные POI. Исходный пользовательский текст, координаты старта, расстояния и расчётные времена "
        "в этот запрос не включаем. Ключ находится только на backend. Ссылки на источники, "
        "неизвестные свойства и честные предупреждения помогают человеку оценить совет самостоятельно.",
        "Бизнес-гипотеза проекта: предложить помощника отелям и туристическим стойкам, где гостю "
        "часто нужен план на небольшой промежуток времени. Это ещё не подтверждённый спрос. "
        "В пилоте мы хотим измерить долю выбранных планов и полезность объяснений. Любые будущие "
        "коммерческие рекомендации потребуют явной маркировки. Развитие начнём с пешеходной "
        "маршрутизации и более надёжной проверки часов работы. После этого проведём оценку "
        "с жителями и добавим казахский и английский языки. Эти возможности пока входят в план развития.",
        "В репозитории находятся работающий прототип, автоматический сборщик, зафиксированный "
        "набор POI, инструкции запуска и проверок. Мы подготовили документ сценариев и ограничений, "
        "презентацию и демонстрацию. Основные ограничения сохраняются: путь приблизительный, "
        "городские данные могут быть неполными, события и безопасность маршрута в реальном времени "
        "не проверяются. Astana Walks уже показывает, как связать интересы человека, доступное время "
        "и объяснимые рекомендации из конкретных источников. Спасибо, готовы показать сценарий и ответить на вопросы."
    ]
    if f.get("pwa",{}).get("enabled"):
        result[2]+=" Интерфейс ориентирован на телефон, его можно установить как PWA. Для установки на реальном телефоне нужен HTTPS. Офлайн доступна только оболочка. Подбор требует сервера, ответы API в кэш не попадают."
        if f["pwa"].get("phone_installation",{}).get("user_reported"):
            result[2]+=" Пользователь подтвердил установку на телефон и запуск по иконке. Работу без сети на этом устройстве мы не проверяли."
        if f["pwa"].get("demo_url"):
            result[8]+=" Временный HTTPS-сайт доступен, пока работают наш компьютер и туннель."
    return result


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
        pwa["phone_installation"]={"user_reported":True,"reported_on":reported.date().isoformat(),"installed":True,"opened_from_icon":True,"os":phone_check.get("device_os"),"device_model":phone_check.get("device_model"),"offline_tested":phone_check.get("offline_on_physical_phone") in (True,"passed","failed"),"source":relative(phone_path),"sha256":sha256(phone_path)}
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
    llm_run=evaluation.get("summary",{}).get("llm")
    llm["evidence"]=(f'Actual report: {relative(evaluation_path)}. '
        f'{evaluation["summary"]["scenarios_passed"]}/{evaluation["summary"]["scenarios_total"]} main scenarios, '
        f'{tests["passed"]}/{tests["run"]} tests. '
        +(f'{llm_run["validated_scenarios"]}/{llm_run["eligible_scenarios"]} eligible scenarios validated, {llm_run["total_attempts"]} provider calls in main scenarios.' if llm_run else "Live LLM not recorded."))
    llm["privacy_note"] = "LLM получает признаки интересов и найденные публичные POI: name, category, description, features. Исходный запрос, origin, координаты, расчётные времена, расстояния и opening_hours не передаются. Числа и расписания отображаются отдельно из backend, а не генерируются в свободном тексте."
    from app.retrieval import Retriever
    from app.generation import recommend
    first = scenario_rows[0]
    response = recommend(Retriever(dataset_path), first["query"], first["minutes"], first["start"], use_llm=False)
    recs = response["recommendations"]
    if not recs:
        raise ValueError("Featured EXPO scenario has no recommendations. Select verified evidence first.")
    generated_why=None
    narration_source=config.get("narration_evidence","output/live-explanations.json")
    narration_path=Path(narration_source)
    if not narration_path.is_absolute():narration_path=ROOT/narration_path
    if llm["live_verified"] and narration_path.is_file():
        narration=read_json(narration_path)
        if narration.get("manual_review",{}).get("quality_pass") is False:
            raise ValueError("Recorded narration failed manual prose review. Resolve the issue and record fresh evidence before final build.")
        if narration.get("dataset",{}).get("sha256")!=sha256(dataset_path):
            raise ValueError("Narration evidence used a different POI snapshot.")
        narr_recs=narration["scenarios"][0]["recommendations"]
        if [r["id"] for r in narr_recs]!=[r["id"] for r in recs]:
            raise ValueError("Narration evidence and featured plan differ.")
        generated_why=narr_recs[0]["why"]
    if screenshot_layout=="detail" and not generated_why:
        raise ValueError("Detail layout requires the actual accepted LLM explanation.")
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
        "evaluation":{"source":relative(evaluation_path),"sha256":sha256(evaluation_path),"scenarios":rows,"llm_summary":evaluation.get("summary",{}).get("llm"),"additional_checks":evaluation.get("additional_checks",[])},
        "tests":{**tests,"source":relative(evaluation_path)},
        "llm":llm,
        "pwa":pwa,
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
        story.append(p(f'В пяти основных сценариях LLM принята в {llm_summary["validated_scenarios"]} из {llm_summary["eligible_scenarios"]} непустых случаев, суммарно {llm_summary["total_attempts"]} запросов к модели. При отклонении ответа допускается одна корректирующая попытка, затем fallback. Это результат конкретного прогона, а не гарантия будущих ответов.',"small"))
    story += [p("Ссылки и происхождение","h2"),p(f'<link href="{f["repository"]}" color="#167C6B">Репозиторий ltybcs/astana-walks-ai</link>'),p(safe(f["dataset"]["attribution"])+". "+safe(f["dataset"]["license"]))]
    if f.get("pwa",{}).get("demo_url"):
        story.append(p(f'<link href="{escape(f["pwa"]["demo_url"])}" color="#167C6B">Временное HTTPS-демо</link> (при включённых ПК и туннеле).'))
    if f["dataset"].get("license_url"):
        story.append(p(f'<link href="{escape(f["dataset"]["license_url"])}" color="#167C6B">Условия использования данных</link>'))
    if f.get("video_url"):story.append(p(f'<link href="{escape(f["video_url"])}" color="#167C6B">Демонстрация на YouTube</link>'))
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
        story += [p("Дополнительная проверка UI-пресета","h2"),p(f'«{safe(extra["query"])}»: {place_count(extra["result_count"])}, {extra["total_minutes"]} мин, режим {safe(extra["mode"])}. {extra.get("llm_attempts",0)} запросов к модели. Эта отдельная проверка не входит в пять основных сценариев и их число обращений к LLM.',"small")]
    if f.get("pwa",{}).get("enabled"):
        story += [p("Телефон, PWA и офлайн","h2"),p("Мобильная PWA сохраняет статическую оболочку. Подбор требует backend; запросы и ответы API не кэшируются. Для телефона нужен HTTPS, а localhost подходит только для разработки на том же устройстве.")]
        if f["pwa"].get("phone_installation",{}).get("user_reported"):
            story.append(p("Пользователь подтвердил установку по HTTPS и запуск по иконке на настоящем телефоне. ОС и модель не указаны. Работа без сети на телефоне не проверена.","small"))
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
    story += [p("Коммерческие рекомендации","h2"),p("Спрос и бизнес-модель пока не проверены. Предполагаемый пилот: отели и туристические стойки. Любая будущая платная позиция должна иметь явную маркировку, не подменяя релевантность скрытым продвижением.")]
    story += [PageBreak(),p("Повторение проверки","h1"),p("Команды выполняются из корня проекта. Для запуска прототипа сторонние Python-пакеты не нужны. Финальная сборка материалов требует ReportLab, Pillow, pypdf и @oai/artifact-tool.")]
    llm_flag=" --llm" if f["llm"]["live_verified"] else ""
    commands=["python -m unittest discover -s tests -v",f'python -m app.evaluate --data {f["dataset"]["path"]} --output output/evaluation.json --run-tests{llm_flag}',f'python -m app.server --data {f["dataset"]["path"]}',"python scripts/collect_osm.py --help"]
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
    speech_lines=["# Astana Walks: речь на 6 минут 30 секунд","","Темп и длительность ориентировочны. Проведите репетицию перед выступлением.",""]
    timings=["0:00–0:35","0:35–1:10","1:10–2:10","2:10–2:55","2:55–3:30","3:30–4:15","4:15–4:55","4:55–5:40","5:40–6:30"]
    for i,t in enumerate(f["speech"]):speech_lines += [f"## Слайд {i+1}, {timings[i]}","",t,""]
    (args.output/"speaker-script.md").write_text("\n".join(speech_lines),encoding="utf-8")
    public_facts={k:v for k,v in f.items() if k not in {"screenshot","mobile_screenshot","speech"}}
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
