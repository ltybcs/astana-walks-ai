"""Local, deterministic context for the text-only lab; no providers or storage."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import re
import unicodedata

PUBLIC_ORIGINS = {
    "airport": {"lat": 51.027825, "lon": 71.463770, "label": "Аэропорт Астаны — ориентировочная точка терминала T1"},
    "central_park": {"lat": 51.1548152, "lon": 71.4195693, "label": "Центральный парк (Парк Астана) — ориентировочная публичная точка"},
    "abu_dhabi": {"lat": 51.1219718, "lon": 71.4279322, "label": "Abu Dhabi Plaza — публичная точка"},
    "mega": {"lat": 51.0892, "lon": 71.4074, "label": "MEGA Silk Way — публичная точка"},
    "baiterek": {"lat": 51.1283, "lon": 71.4305, "label": "Байтерек — публичная точка"},
    "river": {"lat": 51.1546, "lon": 71.4252, "label": "Набережная Есиля — публичная точка"},
    "expo": {"lat": 51.09125, "lon": 71.412806, "label": "EXPO — ориентировочная публичная точка района"},
}
EXPO_REFERENCE = "https://www.wikidata.org/w/index.php?title=Q1074528&oldid=2425505851#P625"
ORIGIN_REFERENCES = {
    "expo": EXPO_REFERENCE,
    "airport": "https://www.openstreetmap.org/way/166500591",
    "central_park": "https://www.openstreetmap.org/relation/15957444",
    "abu_dhabi": "https://www.openstreetmap.org/way/762780007",
}
ORIGIN_PATTERNS = {
    "airport": r"(?<!\w)(?:аэропорт\w*|airport|nqz)(?!\w)",
    "central_park": r"(?<!\w)(?:центральн\w*\s+парк\w*|central\s+park|парк\s+астана)(?!\w)",
    "abu_dhabi": r"(?<!\w)(?:абу[\s-]+даби(?:[\s-]+плаза)?|abu[\s-]+dhabi(?:[\s-]+plaza)?)(?!\w)",
    "mega": r"(?<!\w)(?:mega(?:\s*silk\s*way)?|мег[аи](?:\s*силк\s*(?:уэй|вэй|вей))?)(?!\w)",
    "baiterek": r"(?<!\w)(?:байтерек\w*|бәйтерек\w*|baiterek|bayterek)(?!\w)",
    "river": r"(?<!\w)(?:набережн\w*(?:\s+(?:реки\s+)?(?:есил\w*|есіл\w*))?|есиль|есиля|есіл\w*)(?!\w)",
    "expo": r"(?<!\w)(?:expo|экспо)(?:\s*[-–—]?\s*2017)?(?!\w)",
}
TIME_UNIT = r"минут(?:а|ы)?|мин\.?|час(?:а|ов)?|ч\.?|minutes?|mins?|hours?|hrs?|h"
RANGE_TIME = re.compile(r"(?<![\w.,])(?:на\s*)?(\d+(?:[.,]\d+)?)\s*(?:[-–—]|до)\s*(\d+(?:[.,]\d+)?)\s*(" + TIME_UNIT + r")\b")
SINGLE_TIME = re.compile(r"(?<![\w.,])(?:на\s*)?([+-]?\d+(?:[.,]\d+)?)\s*(" + TIME_UNIT + r")\b")
COMPOSITE_TIME = re.compile(r"(?<![\w.,])(?:на\s*)?(\d+(?:[.,]\d+)?)\s*(?:час(?:а|ов)?|ч\.?|hours?|hrs?|h)\s*(?:и\s*)?(\d+)\s*(?:минут(?:а|ы)?|мин\.?|minutes?|mins?)\b")
DEFAULT_BUDGETS = [60, 120, 240]
LOCATION_STATUSES = {"not_requested", "granted", "denied", "timeout", "unavailable", "unsupported", "error", "pending"}


class ContextError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request", status: int = 400, fields=()):
        super().__init__(message)
        self.code, self.status, self.fields = code, status, list(fields)

    def as_dict(self) -> dict:
        return {"error": str(self), "code": self.code, "fields": self.fields}


def _normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    text = re.sub("[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _minutes(number, unit):
    value = float(number.replace(",", ".")) * (60 if unit.startswith(("ч", "h")) else 1)
    if not math.isfinite(value) or not value.is_integer() or not 10 <= value <= 480:
        raise ContextError("Бюджет должен составлять целое число от 10 до 480 минут.", fields=("minutes",))
    return int(value)


def _time_options(query, explicit):
    covered, exact, intervals = [], set(), []
    def free(match):
        return not any(match.start() < end and match.end() > start for start, end in covered)
    for match in RANGE_TIME.finditer(query):
        low, high = _minutes(match[1], match[3]), _minutes(match[2], match[3])
        if low > high:
            raise ContextError("Диапазон времени указан в обратном порядке.", fields=("minutes",))
        intervals.append((low, high))
        covered.append(match.span())
    for match in COMPOSITE_TIME.finditer(query):
        if free(match):
            exact.add(_minutes(str(float(match[1].replace(",", ".")) * 60 + int(match[2])), "мин"))
            covered.append(match.span())
    phrases = [(r"\bполтора\s*часа\b|\bчас\s+с\s+половиной\b", 90), (r"\bпол\s*часа\b", 30)]
    for pattern, value in phrases:
        for match in re.finditer(pattern, query):
            if free(match):
                exact.add(value)
                covered.append(match.span())
    words = {"один": 1, "одного": 1, "пара": 2, "пару": 2, "пары": 2, "два": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8}
    for match in re.finditer(r"\b(" + "|".join(words) + r")\s+час(?:а|ов)?\b", query):
        if free(match):
            exact.add(words[match[1]] * 60)
            covered.append(match.span())
    for match in SINGLE_TIME.finditer(query):
        if free(match):
            exact.add(_minutes(match[1], match[2]))
            covered.append(match.span())
    for match in re.finditer(r"\bчас(?:ик|ок)?\b", query):
        if free(match) and not re.search(r"\b(?:в|к|через|после)\s*$", query[max(0, match.start()-15):match.start()]):
            exact.add(60)
    notices = []
    if len(exact) > 1 or len(intervals) > 1 or exact and intervals:
        return list(DEFAULT_BUDGETS), "ambiguous_text_options", None, ["В тексте несколько длительностей; показаны отдельные варианты на 1, 2 и 4 часа."]
    if intervals:
        low, high = intervals[0]
        options = sorted({low, (low + high) // 2, high})
        source, requested_range = "query_range", [low, high]
    elif exact:
        options, source, requested_range = [next(iter(exact))], "query", None
    elif explicit is not None:
        options, source, requested_range = [explicit], "explicit", None
    else:
        return list(DEFAULT_BUDGETS), "default_options", None, ["Время не указано; показаны варианты на 1, 2 и 4 часа."]
    if explicit is not None and explicit != options[0] and source != "explicit":
        notices.append("Время из текста имеет приоритет над прежним значением поля.")
    return options, source, requested_range, notices


def _point(value, *, legacy=False):
    allowed = {"lat", "lon", "label"} if legacy else {"lat", "lon", "accuracy"}
    required = {"lat", "lon", "label"} if legacy else {"lat", "lon"}
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise ContextError("Некорректная структура точки старта.", fields=("origin",))
    for key, low, high in (("lat", -90, 90), ("lon", -180, 180)):
        if type(value[key]) not in {int, float} or not math.isfinite(value[key]) or not low <= value[key] <= high:
            raise ContextError("Координаты старта некорректны.", fields=("origin",))
    if "accuracy" in value and (type(value["accuracy"]) not in {int, float} or not math.isfinite(value["accuracy"]) or not 0 <= value["accuracy"] <= 100000):
        raise ContextError("Точность геолокации некорректна.", fields=("geolocation",))
    if legacy and (not isinstance(value["label"], str) or not value["label"].strip() or len(value["label"]) > 200):
        raise ContextError("У собственной точки отсутствует корректная подпись.", fields=("origin",))
    return {"lat": value["lat"], "lon": value["lon"], "label": value["label"].strip() if legacy else "Геолокация устройства — точность не проверена"}


def _in_city(point):
    return 51.00 <= point["lat"] <= 51.32 and 71.20 <= point["lon"] <= 71.70


def _positioned(query, start):
    return bool(re.search(r"\b(?:возле|около|у|от|рядом\s+с|на|в|старт(?:ую)?(?:\s*[:—-])?)\s*$", query[max(0, start-40):start]))


def _negated_origin(query, start):
    return bool(re.search(r"\bне\s+(?:возле|около|у|от|рядом\s+с|на|в)\s*$", query[max(0, start-40):start]))


def _text_origin(query, catalog):
    landmarks = []
    for identifier, pattern in ORIGIN_PATTERNS.items():
        for match in re.finditer(pattern, query):
            if not _negated_origin(query, match.start()):
                landmarks.append((not _positioned(query, match.start()), match.start(), identifier))
    if landmarks:
        landmarks.sort()
        identifier = landmarks[0][2]
        notices = ["В тексте несколько ориентиров; старт выбран по первому явному указанию места."] if len({item[2] for item in landmarks}) > 1 else []
        if identifier == "airport":
            notices.append("Терминал не указан: использован ориентир T1, не точное местоположение устройства. Доступ из зоны вылета наружу не подтверждён.")
        return deepcopy(PUBLIC_ORIGINS[identifier]), identifier, "query_landmark", False, ORIGIN_REFERENCES.get(identifier), notices
    matches = []
    generic = {"кафе", "парк", "ресторан", "театр", "музей", "детская площадка", "сквер"}
    places = catalog.get("places", []) if isinstance(catalog, dict) else catalog or []
    for place in places:
        if not isinstance(place, dict):
            continue
        name = place.get("source_name") or place.get("name")
        if isinstance(name, str) and not place.get("name_generated"):
            normalized = _normalize(name)
            if len(normalized) >= 4 and normalized not in generic:
                match = re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", query)
                if match and _positioned(query, match.start()) and not _negated_origin(query, match.start()):
                    matches.append((match.start(), place, "name"))
        tags = place.get("source_tags", {})
        street, house = tags.get("addr:street"), tags.get("addr:housenumber")
        if isinstance(street, str) and isinstance(house, str):
            address = _normalize(street + " " + house)
            match = re.search(r"(?<!\w)" + re.escape(address) + r"(?!\w)", query)
            if match and not _negated_origin(query, match.start()):
                matches.append((match.start(), place, "address"))
    if matches:
        matches.sort(key=lambda item: item[0])
        place = matches[0][1]
        point = {"lat": place["lat"], "lon": place["lon"], "label": place.get("name") or "Точка из локального OSM"}
        if _in_city(point):
            notices = ["В справочнике несколько совпадений; выбран первый упомянутый объект."] if len({item[1]["id"] for item in matches}) > 1 else []
            return point, None, "query_catalog", True, place.get("source_url"), notices
    return None


def _has_unresolved_address(query):
    # Generic goals (water, a park, home) are not a concrete address. A street,
    # named venue or otherwise specific unknown place suppresses GPS fallback.
    generic = r"вод[аыеу]|водой|парк\w*|дом\w*|рек\w*|берег\w*|центр\w*|кафе|музе\w*|театр\w*|отел\w*"
    stop = {"меня", "нас", "него", "нее", "них", "вас", "тебя", "сил", "часа", "час", "минут", "минуты"}
    following = {"хочу", "хочется", "гулять", "погулять", "есть", "на", "с", "и", "или", "без", "около", "возле", "полчаса", "час", "часа"}
    pattern = r"\b(?:возле|около|рядом\s+с|у|от|старт(?:ую)?|адрес|улиц\w*|ул\.|просп\w*)\s*[:—-]?\s+([\w-]+)(?:\s+([\w-]+))?"
    for match in re.finditer(pattern, query):
        if re.search(r"\bне\s*$", query[max(0, match.start()-10):match.start()]):
            continue
        first, second = match[1], match[2]
        if first in stop or first.isdigit():
            continue
        if re.fullmatch(generic, first) and (not second or second in following or second.isdigit()):
            continue
        return True
    return False


def _clock(payload, now):
    current = now() if callable(now) else now
    current = datetime.now(timezone.utc) if current is None else current
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("An aware clock is required")
    client = payload.get("client_context")
    if client is None and "client_context" not in payload:
        return current.astimezone(timezone.utc), {"instant": None, "utc_offset_minutes": 300}, "astana_current_time"
    if not isinstance(client, dict) or set(client) != {"instant", "utc_offset_minutes"} or not isinstance(client["instant"], str) or len(client["instant"]) > 64:
        raise ContextError("Некорректный снимок локального времени.", fields=("client_context",))
    offset = client["utc_offset_minutes"]
    if type(offset) is not int or not -720 <= offset <= 840:
        raise ContextError("Некорректное смещение часового пояса.", fields=("client_context",))
    try:
        instant = datetime.fromisoformat(client["instant"].replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() != timedelta(0) or abs((current - instant).total_seconds()) > 600:
            raise ValueError("Stale or non-UTC snapshot")
    except (ValueError, OverflowError):
        raise ContextError("Снимок времени должен быть UTC и не старше 10 минут.", fields=("client_context",)) from None
    return instant, {"instant": client["instant"], "utc_offset_minutes": offset}, "client_current_time"


def resolve_request(payload, *, catalog=None, now=None, probe=False):
    allowed = {"query", "minutes", "origin_id", "origin", "time_of_day", "geolocation", "location_status", "client_context"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ContextError("Некорректные поля запроса прогулки.", fields=("query",))
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ContextError("query: непустой текст до 2000 символов.", fields=("query",))
    query, normalized = query.strip(), _normalize(query)
    explicit = payload.get("minutes")
    if "minutes" in payload and (type(explicit) is not int or not 10 <= explicit <= 480):
        raise ContextError("minutes: целое число от 10 до 480.", fields=("minutes",))
    if "origin" in payload and "origin_id" in payload:
        raise ContextError("Указаны два несовместимых старых поля старта.", fields=("origin",))
    identifier = payload.get("origin_id")
    if "origin_id" in payload and (not isinstance(identifier, str) or identifier not in PUBLIC_ORIGINS):
        raise ContextError("Неизвестный публичный ориентир.", fields=("origin",))
    legacy = _point(payload["origin"], legacy=True) if "origin" in payload else None
    geo = _point(payload["geolocation"]) if "geolocation" in payload else None
    if "location_status" in payload and (not isinstance(payload["location_status"], str) or payload["location_status"] not in LOCATION_STATUSES):
        raise ContextError("Некорректный статус геолокации.", fields=("location_status",))
    explicit_day = payload.get("time_of_day")
    if "time_of_day" in payload and (not isinstance(explicit_day, str) or explicit_day not in {"day", "evening", "night"}):
        raise ContextError("time_of_day: day, evening или night.", fields=("time_of_day",))
    instant, clock_context, clock_source = _clock(payload, now)
    options, budget_source, requested_range, assumptions = _time_options(normalized, explicit)
    match = _text_origin(normalized, catalog)
    unresolved = match is None and _has_unresolved_address(normalized)
    sensitive, reference, needs_geo = False, None, False
    if match:
        origin, origin_id, origin_source, sensitive, reference, notices = match
        assumptions.extend(notices)
        if legacy is not None or identifier and identifier != origin_id:
            assumptions.append("Место из текста имеет приоритет над прежним полем старта.")
    elif unresolved:
        raise ContextError("Старт из сообщения не найден в локальном справочнике; поиск не выполнялся.",
                           code="known_start_required", status=422, fields=("origin",))
    elif legacy is not None or identifier:
        origin = legacy if legacy is not None else deepcopy(PUBLIC_ORIGINS[identifier])
        origin_id, origin_source, sensitive = (None, "explicit_coordinates", True) if legacy is not None else (identifier, "explicit_landmark", False)
        reference = ORIGIN_REFERENCES.get(origin_id)
    elif geo is not None:
        origin, origin_id, origin_source, sensitive = geo, None, "browser_geolocation", True
    elif probe and payload.get("location_status", "not_requested") in {"not_requested", "pending"}:
        origin = origin_id = None
        origin_source, needs_geo = "pending_geolocation", True
    else:
        raise ContextError("Для поиска требуется известный старт или разрешённая геолокация; поиск не выполнялся.",
                           code="known_start_required", status=422, fields=("origin",))
    if origin is not None and not _in_city(origin):
        raise ContextError("Старт вне области Астаны, поддерживаемой приложением; поиск не выполнялся.",
                           code="known_start_required", status=422, fields=("origin",))
    day_matches = []
    for period, pattern in (("night", r"\b(?:ноч\w*|полноч\w*|night|түн\w*)\b"), ("evening", r"\b(?:вечер\w*|evening)\b"), ("day", r"\b(?:днем|дневн\w*|утр\w*|day)\b")):
        for found in re.finditer(pattern, normalized):
            # A venue type is not a requested time of day.
            if period == "night" and re.match(r"\s+(?:клуб\w*|clubs?)\b", normalized[found.end():]):
                continue
            if not re.search(r"\b(?:не|без)(?:\s+\w+){0,2}\s*$", normalized[max(0, found.start()-25):found.start()]):
                day_matches.append((found.start(), period))
    if day_matches:
        day_matches.sort()
        period, day_source = day_matches[0][1], "query"
        if len({value for _, value in day_matches}) > 1:
            assumptions.append("Упомянуто несколько периодов суток; выбран первый.")
        if explicit_day and explicit_day != period:
            assumptions.append("Время суток из текста имеет приоритет над прежним полем.")
    elif explicit_day:
        period, day_source = explicit_day, "explicit"
    else:
        hour = (instant + timedelta(minutes=clock_context["utc_offset_minutes"])).hour
        period, day_source = ("day" if 6 <= hour < 18 else "evening" if 18 <= hour < 22 else "night"), clock_source
    context = {"origin": origin, "origin_id": origin_id, "origin_source": origin_source,
               "origin_reference": reference, "minutes": options[0], "budget_options": options,
               "minutes_source": budget_source, "budget_source": budget_source, "requested_range": requested_range,
               "time_of_day": period, "time_of_day_source": day_source, "client_context": clock_context,
               "assumptions": assumptions, "sensitive_origin": sensitive, "is_city_sample": False}
    return {"query": query, "minutes": options[0], "origin": origin, "time_of_day": period,
            "resolved_context": context, "needs_geolocation": needs_geo}
