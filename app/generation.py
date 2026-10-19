"""Generate explanations only from retrieved facts and auditable choices."""

from __future__ import annotations

from .llm import narrate
from .retrieval import Retriever, feature, walk_distance_m


def _why(poi: dict, intent: dict) -> str:
    reasons = {
        "park": "Подходит для прогулки на воздухе",
        "playground": "Категория источника — детская площадка",
        "cafe": "Категория источника — кафе; можно рассмотреть для короткой паузы",
        "restaurant": "Категория источника — ресторан; посещение требует проверки работы и условий входа",
        "museum": "Позволяет включить в маршрут музей",
        "attraction": "Достопримечательность для знакомства с городом",
        "monument": "Архитектурная или историческая точка для знакомства с городом",
        "viewpoint": "Точка с категорией «видовая площадка»",
        "theatre": "Архитектурная и культурная остановка",
        "promenade": "Подходит для прогулки по городской среде",
        "mall": "Точка с категорией «торговый центр»",
        "bar": "Заведение с категорией «бар»",
    }
    reason = reasons.get(poi["category"], "Точка найдена в локальной базе мест")
    if intent["child"] and poi["category"] == "park":
        reason += "; можно рассмотреть для прогулки с ребёнком, условия нужно проверить"
    if intent["socket"] and feature(poi, "socket") == "yes":
        reason += "; источник отмечает наличие розетки, подтвердите перед визитом"
    return reason + ". Переход и посещение укладываются в общий бюджет."


def _action(poi: dict, visit_minutes: int) -> str:
    ideas = {
        "park": f"Прогуляться по парку около {visit_minutes} минут и сделать паузу на свежем воздухе",
        "playground": f"Предложить ребёнку поиграть около {visit_minutes} минут, если площадка доступна и подходит по возрасту",
        "cafe": f"Выпить кофе и отдохнуть около {visit_minutes} минут, если заведение открыто и есть места",
        "restaurant": f"Запланировать перекус примерно на {visit_minutes} минут после проверки работы заведения",
        "museum": f"Посмотреть экспозицию около {visit_minutes} минут, только если доступ, часы и билеты подтверждены",
        "attraction": f"Осмотреть место снаружи и сделать несколько фотографий за {visit_minutes} минут",
        "monument": f"Осмотреть монумент снаружи и сделать фото, выделив около {visit_minutes} минут",
        "viewpoint": f"Осмотреть панораму и сделать фото, выделив около {visit_minutes} минут",
        "theatre": f"Осмотреть здание театра снаружи и сделать фото, выделив около {visit_minutes} минут",
        "promenade": f"Прогуляться по доступному участку и осмотреть городскую среду около {visit_minutes} минут",
        "mall": f"Сделать короткую прогулку по доступным пространствам торгового центра около {visit_minutes} минут",
        "bar": f"Запланировать остановку примерно на {visit_minutes} минут после проверки работы и условий входа",
    }
    idea = ideas.get(poi["category"], f"Осмотреть место, выделив около {visit_minutes} минут")
    return "Идея: " + idea + ". Перед выходом откройте источник, проверьте доступ, часы и пешеходный путь."


def _opening_note(poi: dict) -> str:
    value = poi.get("opening_hours")
    if value == "24/7":
        return "В источнике указано 24/7. Фактический доступ сейчас не проверен."
    if value:
        return f"Часы в источнике: {value}. Открытие сейчас не проверено."
    return "Часы и доступ сейчас не подтверждены; проверьте перед посещением."


def recommend(retriever: Retriever, query: str, minutes: int, origin: dict, time_of_day: str = "day", *, use_llm: bool = True) -> dict:
    result = retriever.retrieve(query, origin, minutes, time_of_day)
    recommendations = []
    sources = []
    facts_by_id = {}
    intent = result["intent"]
    warnings = ["Расстояние и ходьба приблизительны: прямая дистанция × 1,3 и скорость 75 м/мин. Маршрут включает возврат; переходы по улицам и погода не проверены."]
    if retriever.dataset.get("is_demo", False):
        warnings.append("Учебный набор: названия и координаты демонстрационные или приблизительные, сведения не проверены. Выполните сбор OSM для реальных данных.")
    if intent["quiet"]:
        warnings.append("Тишина меняется в течение дня. Уровень шума не подтверждён, если это явно не указано источником.")
    if intent["child"]:
        warnings.append("Возрастные ограничения, безопасность, туалеты и условия для ребёнка не подтверждены; проверьте их заранее.")
    if intent["night"]:
        warnings.append("Ночной запрос: отобраны только точки, где источник явно указал 24/7; актуальный доступ, освещение и безопасность не проверены.")
    for candidate in result["selected"]:
        poi = candidate["poi"]
        item = {key: poi[key] for key in ("id", "name", "category", "description", "source_url")}
        item.update({
            "lat": poi["lat"], "lon": poi["lon"],
            "distance_m": candidate["leg_distance_m"],
            "origin_distance_m": walk_distance_m(origin, poi),
            "walk_minutes": candidate["leg_walk_minutes"],
            "leg_distance_m": candidate["leg_distance_m"],
            "visit_minutes": candidate["visit_minutes"],
            "why": _why(poi, intent),
            "action": _action(poi, candidate["visit_minutes"]),
            "confidence": "demo_unverified" if retriever.dataset.get("is_demo", False) else "source_reported",
            "opening_note": _opening_note(poi),
            "source_id": poi["id"],
            "citations": [poi["id"]],
            "explanation_source": "extractive",
        })
        recommendations.append(item)
        facts_by_id[poi["id"]] = {key: item[key] for key in ("name", "category", "description", "distance_m", "walk_minutes", "visit_minutes")}
        facts_by_id[poi["id"]].update({"features": {key: feature(poi, key) for key in ("socket", "quiet", "wheelchair")}, "opening_hours": poi.get("opening_hours")})
        sources.append({"id": poi["id"], "title": poi["name"], "url": poi["source_url"], "collected_at": poi.get("collected_at", retriever.dataset.get("collected_at")), "verification": poi.get("verification", "source_reported")})
    mode = "extractive/demo" if retriever.dataset.get("is_demo", False) else "extractive"
    llm, llm_status = narrate(recommendations, intent, facts_by_id=facts_by_id) if use_llm else (None, "disabled")
    if llm:
        by_id = {item["id"]: item for item in llm}
        for item in recommendations:
            item.update(by_id[item["id"]])
            if not item["action"].startswith("Идея:"):
                item["action"] = "Идея: " + item["action"]
            item["action"] += " Перед выходом проверьте доступ и часы по источнику."
            item["explanation_source"] = "llm-grounded"
        mode = "llm-grounded/demo" if retriever.dataset.get("is_demo", False) else "llm-grounded"
    elif llm_status not in {"not_configured", "disabled", "no_candidates"} and recommendations:
        warnings.append("LLM недоступна или её ответ не прошёл проверку; использовано извлечение из базы без генерации новых фактов.")
    response = {"recommendations": recommendations, "warnings": warnings, "mode": mode, "sources": sources, "plan": result["plan"], "retrieved_count": result["retrieved_count"], "dataset": retriever.dataset, "diagnostics": {"llm_status": llm_status, "filtered": result["filtered"], "ranking_policy": result["ranking_policy"], "intent": intent, "validation_note": "Структура, ссылки и защищённые свойства проверены; полная семантическая достоверность свободного текста не гарантируется." if llm else "Фразы сформированы из категории, источника и расчёта маршрута."}}
    if not recommendations:
        reasons = []
        if intent["socket"]:
            reasons.append("Нет подходящего места с подтверждённой в источнике розеткой")
        if intent["accessible"]:
            reasons.append("нет подходящего места с подтверждённой доступностью для коляски")
        if intent["night"]:
            reasons.append("нет подходящей точки с явно указанным режимом 24/7")
        if intent["strict_quiet"]:
            reasons.append("нет подходящего места с явно подтверждённой тишиной")
        if result["filtered"].get("no_text_match"):
            reasons.append("Запрос не совпал с местами и сценариями прогулки; укажите интерес, категорию или название места в Астане")
        if not reasons:
            reasons.append("В этой базе нет подходящих мест, доступных пешком туда и обратно в указанное время")
        response["clarification"] = "; ".join(reasons) + ". Увеличьте время, выберите другую отправную точку или уточните условия; неизвестные свойства не считаются подтверждёнными."
    return response
