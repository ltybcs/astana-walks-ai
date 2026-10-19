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
    return [
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
        f'RAG начинается с данных. Автоматический сборщик получает публичные точки {d["source_name"]}, '
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
        "публичные POI с расчётными временами. Исходный пользовательский текст и координаты старта "
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


def assemble(args):
    dataset_path = args.data.resolve()
    evaluation_path = args.evaluation.resolve()
    raw = read_json(dataset_path)
    evaluation = read_json(evaluation_path)
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
        raise ValueError("A real local product PNG screenshot is required.")
    config = read_json(args.config) if args.config else {}
    llm = config.get("llm", {})
    llm.setdefault("provider", "Не подключён")
    llm.setdefault("model", "Не подключена")
    llm["live_verified"] = bool(llm.get("live_verified") and evaluation.get("live_llm_used"))
    llm["privacy_note"] = "Внешней LLM передаются intent и найденные публичные POI, расчётные времена. Исходный запрос и origin не передаются."
    from app.retrieval import Retriever
    from app.generation import recommend
    first = scenario_rows[0]
    response = recommend(Retriever(dataset_path), first["query"], first["minutes"], first["start"], use_llm=False)
    recs = response["recommendations"]
    if not recs:
        raise ValueError("Featured EXPO scenario has no recommendations. Select verified evidence first.")
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
        "dataset":{"count":len(raw["places"]),"missing_hours":sum(not p.get("opening_hours") for p in raw["places"]),"date_label":collected.strftime("%d.%m.%Y"),"path":relative(dataset_path),"sha256":sha256(dataset_path),"source_name":meta.get("source_provider") or meta.get("name","Источник POI"),"source_url":meta.get("license_url") or raw["places"][0]["source_url"],"license_url":meta.get("license_url"),"attribution":meta.get("attribution","Атрибуция не указана"),"license":meta.get("license","Условия в метаданных не указаны"),"metadata":meta},
        "evaluation":{"source":relative(evaluation_path),"sha256":sha256(evaluation_path),"scenarios":rows},
        "tests":{**tests,"source":relative(evaluation_path)},
        "llm":llm,
        "featured":{"query":first["query"],"minutes":first["minutes"],"total_minutes":response["plan"]["total_minutes"],"names":[r["name"] for r in recs],"result_count":len(recs),"response":response},
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
    story += [p("Ссылки и происхождение","h2"),p(f'<link href="{f["repository"]}" color="#167C6B">Репозиторий ltybcs/astana-walks-ai</link>'),p(safe(f["dataset"]["attribution"])+". "+safe(f["dataset"]["license"]))]
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
        "Только явно допустимые ночью объекты. Неизвестные часы не означают «открыто сейчас»."
    ]
    for i,r in enumerate(f["evaluation"]["scenarios"]):
        if i==3:
            story += [PageBreak(),p("Сценарии и границы проверки","h1")]
        ctx=f'{r["start"]["label"]}, {r["minutes"]} мин'
        story += [p(f'{i+1}. {safe(r["short_name"])}',"h2"),p(f'<b>Запрос:</b> «{safe(r["query"])}»<br/><b>Контекст:</b> {safe(ctx)}'),p(f'<b>Ожидается:</b> {safe(expected[i])}'),p(f'<b>Результат:</b> {safe(r["result_label"])}. Режим {safe(r["mode"])}. Проверка пройдена.',"small")]
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
    story += [table,p("Внешняя модель","h2"),p(f["llm"]["privacy_note"]+" Ключ доступен только backend через окружение. Перед реальным размещением нужно проверить условия провайдера и политику хранения на стороне хостинга.")]
    story += [p("Коммерческие рекомендации","h2"),p("Спрос и бизнес-модель пока не проверены. Предполагаемый пилот: отели и туристические стойки. Любая будущая платная позиция должна иметь явную маркировку, не подменяя релевантность скрытым продвижением.")]
    story += [PageBreak(),p("Повторение проверки","h1"),p("Команды выполняются из корня проекта. Для запуска прототипа сторонние Python-пакеты не нужны. Финальная сборка материалов требует ReportLab, Pillow, pypdf и @oai/artifact-tool.")]
    llm_flag=" --llm" if f["llm"]["live_verified"] else ""
    commands=["python -m unittest discover -s tests -v",f'python -m app.evaluate --data {f["dataset"]["path"]} --output output/evaluation.json --run-tests{llm_flag}',f'python -m app.server --data {f["dataset"]["path"]}',"python scripts/collect_osm.py --help"]
    for command in commands:story.append(p(safe(command),"small"))
    story += [p("Источник фактических метрик","h2"),p(safe(f["evaluation"]["source"])),p("SHA-256 отчёта: "+f["evaluation"]["sha256"],"small"),p("Источник набора данных","h2"),p(safe(f["dataset"]["path"])),p("SHA-256 набора: "+f["dataset"]["sha256"],"small")]
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
    screenshot_path=(args.output/"demo-ui.png").resolve()
    if args.screenshot.resolve()!=screenshot_path:
        shutil.copyfile(args.screenshot,screenshot_path)
    f["screenshot"]=str(screenshot_path)
    f["screenshot_relative"]=relative(screenshot_path)
    facts_path=build/"facts.json"
    facts_path.write_text(json.dumps(f,ensure_ascii=False,indent=2),encoding="utf-8")
    document_pdf(f,pdfs/"astana-walks-scenarios.pdf")
    speech_lines=["# Astana Walks: речь на 6 минут 30 секунд","","Темп и длительность ориентировочны. Проведите репетицию перед выступлением.",""]
    timings=["0:00–0:35","0:35–1:15","1:15–2:00","2:00–2:50","2:50–3:30","3:30–4:20","4:20–5:05","5:05–5:55","5:55–6:30"]
    for i,t in enumerate(f["speech"]):speech_lines += [f"## Слайд {i+1}, {timings[i]}","",t,""]
    (args.output/"speaker-script.md").write_text("\n".join(speech_lines),encoding="utf-8")
    public_facts={k:v for k,v in f.items() if k not in {"screenshot","speech"}}
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
