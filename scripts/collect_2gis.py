"""Collect Astana POI through the official 2GIS Places API.

Set DGIS_API_KEY in the environment. --input normalizes saved API responses
offline. Public website cookies and the website's own key are never used.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = "https://catalog.api.2gis.com/3.0/items"
DEFAULT_BBOX = (51.02, 71.30, 51.22, 71.55)
MAX_BYTES = 5_000_000
DEFAULT_QUERIES = ("Кофейни", "Музеи", "Парки", "Достопримечательности", "Детские площадки", "Торговые центры")
FIELDS = "items.point,items.rubrics,items.schedule,items.schedule_special,items.flags,items.is_deleted,items.attribute_groups"
RUBRICS = {
    "кофейни": "cafe", "кафе": "cafe", "рестораны": "restaurant",
    "бары": "bar", "пабы": "bar", "музеи": "museum", "театры": "theatre",
    "парки": "park", "парки культуры и отдыха": "park", "сады": "park",
    "детские площадки": "playground", "смотровые площадки": "viewpoint",
    "достопримечательности": "attraction", "памятники": "monument",
    "памятники / мемориалы": "monument", "торговые центры": "mall",
    "торгово-развлекательные центры": "mall", "набережные": "promenade",
}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class CollectionError(ValueError):
    """A safe error message that never contains request URLs or API keys."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The key is in the query string; never forward it to another endpoint.
        raise CollectionError("2ГИС вернул перенаправление; запрос остановлен.")


def valid_bbox(bbox):
    return (len(bbox) == 4 and all(type(x) in (int, float) and math.isfinite(x) for x in bbox)
            and -90 <= bbox[0] < bbox[2] <= 90 and -180 <= bbox[1] < bbox[3] <= 180)


def response_items(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise CollectionError("Ожидается официальный ответ Places API с meta и result.")
    code = payload["meta"].get("code")
    if code != 200 or type(code) is not int:
        raise CollectionError("Places API не подтвердил успешный ответ (meta.code != 200).")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise CollectionError("Places API вернул некорректный список объектов.")
    return result["items"]


def opening_hours(item):
    schedule = item.get("schedule")
    if not isinstance(schedule, dict):
        return None
    values = []
    for day in DAYS:
        entry = schedule.get(day)
        hours = entry.get("working_hours") if isinstance(entry, dict) else None
        if not isinstance(hours, list) or not hours:
            return None
        intervals = []
        for interval in hours:
            if not isinstance(interval, dict):
                return None
            start, end = interval.get("from"), interval.get("to")
            pattern = r"(?:[01]\d|2[0-3]):[0-5]\d|24:00"
            if not all(isinstance(x, str) and re.fullmatch(pattern, x) for x in (start, end)):
                return None
            intervals.append(f"{start}–{end}")
        values.append((day, intervals))
    # Special/holiday schedules cannot establish an unconditional 24/7 claim.
    if all(hours == ["00:00–24:00"] for _, hours in values) and not item.get("schedule_special"):
        return "24/7"
    return "; ".join(f"{day} {', '.join(hours)}" for day, hours in values) + ("; есть особое расписание — проверьте карточку" if item.get("schedule_special") else "")


def normalize_items(items, collected_at, bbox=DEFAULT_BBOX):
    places = {}
    for item in items:
        if not isinstance(item, dict) or item.get("is_deleted"):
            continue
        flags = item.get("flags")
        if isinstance(flags, dict) and flags.get("temporary_closed"):
            continue
        name, point, raw_id = item.get("name"), item.get("point"), item.get("id")
        if not isinstance(name, str) or not name.strip() or not isinstance(point, dict) or not isinstance(raw_id, str):
            continue
        identifier = raw_id.split("_", 1)[0]
        if not re.fullmatch(r"[0-9]{1,32}", identifier):
            continue
        lat, lon = point.get("lat"), point.get("lon")
        if not all(type(x) in (int, float) and math.isfinite(x) for x in (lat, lon)):
            continue
        if not bbox[0] <= lat <= bbox[2] or not bbox[1] <= lon <= bbox[3]:
            continue
        rubrics = item.get("rubrics", [])
        rubrics = [r.get("name") for r in rubrics if isinstance(r, dict) and isinstance(r.get("name"), str)] if isinstance(rubrics, list) else []
        category = next((RUBRICS[r.strip().lower()] for r in rubrics if r.strip().lower() in RUBRICS), None)
        if category is None and item.get("type") == "attraction":
            category = "attraction"
        if category is None or item.get("type") not in {"branch", "attraction", "adm_div.place"}:
            continue
        attributes = []
        groups = item.get("attribute_groups", [])
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict) or not isinstance(group.get("attributes"), list):
                continue
            attributes.extend(a["name"].strip() for a in group["attributes"] if isinstance(a, dict) and isinstance(a.get("name"), str))
        features = {"socket": "unknown", "quiet": "unknown", "wheelchair": "unknown", "wifi": "unknown"}
        evidence = {}
        # Match only explicit source labels. A laptop/Wi-Fi label is not a socket.
        labels = {"wi-fi": "wifi", "бесплатный wi-fi": "wifi", "розетки": "socket", "есть розетки": "socket",
                  "доступный вход для людей с инвалидностью": "wheelchair"}
        for attribute in attributes:
            key = labels.get(attribute.lower())
            if key:
                features[key] = "yes"
                evidence[key] = attribute
        address = item.get("address_name") if isinstance(item.get("address_name"), str) else None
        source_id = "2gis:" + identifier
        description = "По данным 2ГИС: " + (", ".join(rubrics) if rubrics else "достопримечательность") + "."
        if address:
            description += " Адрес в источнике: " + address + "."
        description += " Актуальное состояние и возможность посещения не проверены на месте."
        places.setdefault(source_id, {
            "id": source_id, "name": name.strip(), "lat": lat, "lon": lon,
            "category": category, "description": description, "aliases": [],
            "address": address, "features": features, "feature_evidence": evidence,
            "opening_hours": opening_hours(item),
            "source_url": f"https://2gis.kz/astana/{'firm' if item['type'] == 'branch' else 'geo'}/{identifier}",
            "source_provider": "2gis", "source_rubrics": rubrics,
            "collected_at": collected_at, "verification": "source_reported_not_field_verified",
        })
    return sorted(places.values(), key=lambda p: p["id"])


def fetch_page(key, query, bbox, page):
    south, west, north, east = bbox
    params = {"key": key, "q": query, "locale": "ru_KZ", "page": page, "page_size": 50,
              "point1": f"{west},{north}", "point2": f"{east},{south}",
              "type": "branch,attraction,adm_div.place", "fields": FIELDS}
    request = Request(ENDPOINT + "?" + urlencode(params), headers={"Accept": "application/json", "User-Agent": "AstanaWalks/0.1 (educational batch collector)"})
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CollectionError("Ответ Places API слишком большой; сузьте область поиска.")
        payload = json.loads(raw)
        response_items(payload)
        return payload
    except HTTPError as error:
        code = error.code
        error.close()
        raise CollectionError(f"Places API: HTTP {code}. Проверьте ключ, права и лимит запросов.") from None
    except (URLError, TimeoutError, OSError):
        raise CollectionError("Places API недоступен: сетевая ошибка или таймаут.") from None
    except (ValueError, UnicodeError) as error:
        if isinstance(error, CollectionError):
            raise
        raise CollectionError("Places API вернул некорректный JSON.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Repeat for several queries; defaults to six POI categories")
    parser.add_argument("--pages", type=int, default=1, help="Maximum pages per query, 1–10 (50 results/page)")
    parser.add_argument("--bbox", nargs=4, type=float, default=DEFAULT_BBOX, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--input", type=Path, action="append", help="Replay a saved official API JSON response, without a key or network")
    parser.add_argument("--output", type=Path, default=Path("data/poi.2gis.json"))
    args = parser.parse_args(argv)
    if not valid_bbox(args.bbox) or not 1 <= args.pages <= 10:
        parser.error("Проверьте bbox и pages (от 1 до 10).")
    queries = args.query or list(DEFAULT_QUERIES)
    if any(not q.strip() or len(q) > 500 for q in queries):
        parser.error("query: от 1 до 500 символов.")
    try:
        items, requests = [], 0
        if args.input:
            for path in args.input:
                if path.stat().st_size > MAX_BYTES:
                    raise CollectionError("Входной JSON превышает 5 MB.")
                items.extend(response_items(json.loads(path.read_text(encoding="utf-8-sig"))))
        else:
            key = os.environ.get("DGIS_API_KEY", "").strip()
            if not key:
                raise CollectionError("Задайте свой DGIS_API_KEY из кабинета разработчика 2ГИС или используйте --input.")
            for query in queries:
                for page in range(1, args.pages + 1):
                    if requests:
                        time.sleep(0.25)
                    payload = fetch_page(key, query, tuple(args.bbox), page)
                    requests += 1
                    batch = response_items(payload)
                    items.extend(batch)
                    total = payload["result"].get("total")
                    if len(batch) < 50 or (type(total) is int and page * 50 >= total):
                        break
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        places = normalize_items(items, collected_at, tuple(args.bbox))
        if not places:
            raise CollectionError("Подходящих POI нет; существующий набор не изменён.")
        document = {"dataset": {"name": "Astana POI / 2GIS", "version": 1, "is_demo": False,
                    "source_provider": "2gis", "collected_at": collected_at,
                    "collection_method": "2gis_api_replay" if args.input else "2gis_places_api",
                    "queries": None if args.input else queries, "bbox": list(args.bbox),
                    "pages_per_query_limit": None if args.input else args.pages, "requests": requests,
                    "coverage": "bounded_query_sample_not_complete_city_catalog",
                    "source_files": [p.name for p in args.input] if args.input else [],
                    "attribution": "2ГИС", "license": "2GIS API terms (not ODbL)",
                    "license_url": "https://law.2gis.kz/licensing-agreement/",
                    "limitations": ["Не проверено на месте", "Не является информацией об открытии сейчас",
                                    "Отсутствующие свойства неизвестны", "Неполная выборка: поиск и лимит страниц",
                                    "Рейтинг и отзывы не импортируются"]}, "places": places}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(args.output)
        print(f"Saved {len(places)} POI to {args.output}; source: 2GIS, not field verified.")
        return 0
    except CollectionError as error:
        print(f"Сбор не выполнен: {error} Существующий набор не заменён.", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("Сбор не выполнен: ошибка входного файла или записи. Существующий набор не заменён.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
