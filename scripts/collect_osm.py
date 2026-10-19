"""Collect named Astana POI from Overpass; keep provenance and unknowns intact.

Examples:
  python scripts/collect_osm.py --output data/poi.osm.json
  python scripts/collect_osm.py --input raw-overpass.json --output data/poi.osm.json

Collection needs network access. It is a batch operation, never called per user
query. Refresh intentionally; do not hit the public API on every application run.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_BBOX = (51.02, 71.30, 51.22, 71.55)  # south, west, north, east
MAX_RESPONSE_BYTES = 25_000_000
CATEGORY_RULES = (
    ("amenity", "cafe", "cafe"), ("amenity", "restaurant", "restaurant"),
    ("amenity", "bar", "bar"), ("amenity", "pub", "bar"),
    ("amenity", "theatre", "theatre"),
    ("leisure", "park", "park"), ("leisure", "garden", "park"),
    ("leisure", "playground", "playground"),
    ("tourism", "museum", "museum"), ("tourism", "attraction", "attraction"),
    ("tourism", "viewpoint", "viewpoint"),
    ("historic", "monument", "monument"), ("historic", "memorial", "monument"),
    ("shop", "mall", "mall"),
)
CATEGORY_RU = {
    "cafe": "кафе", "restaurant": "ресторан", "bar": "бар", "theatre": "театр",
    "park": "парк или сад", "playground": "детская площадка", "museum": "музей",
    "attraction": "достопримечательность", "viewpoint": "видовая площадка",
    "monument": "памятник", "mall": "торговый центр",
}


def build_query(bbox: tuple[float, float, float, float]) -> str:
    box = ",".join(str(value) for value in bbox)
    selectors = "\n".join(f'  nwr["{key}"="{value}"]["name"]({box});' for key, value, _ in CATEGORY_RULES)
    return f"[out:json][timeout:45];\n(\n{selectors}\n);\nout center tags meta;\n"


def _affirmative(value: object) -> str:
    if value in {"yes", "designated", "permissive"}:
        return "yes"
    if value in {"no", "private"}:
        return "no"
    return "unknown"


def normalize_elements(payload: dict, collected_at: str) -> list[dict]:
    places = []
    seen = set()
    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            continue
        name = tags.get("name:ru") or tags.get("name") or tags.get("name:en")
        if not isinstance(name, str) or not name.strip():
            continue
        category = next((category for key, value, category in CATEGORY_RULES if tags.get(key) == value), None)
        if not category:
            continue
        location = element if "lat" in element and "lon" in element else element.get("center", {})
        lat, lon = location.get("lat"), location.get("lon")
        if type(lat) not in (int, float) or type(lon) not in (int, float) or not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            continue
        kind, identifier = element.get("type"), element.get("id")
        if kind not in {"node", "way", "relation"} or type(identifier) is not int:
            continue
        source_id = f"osm:{kind}/{identifier}"
        if source_id in seen:
            continue
        seen.add(source_id)
        socket = _affirmative(tags.get("socket"))
        # A tagged socket type is explicit evidence; internet_access is not.
        if socket == "unknown" and any(key.startswith("socket:") and str(value).lower() not in {"no", "0", "unknown"} for key, value in tags.items()):
            socket = "yes"
        address = ", ".join(str(tags[key]) for key in ("addr:street", "addr:housenumber") if tags.get(key))
        description = f"По данным OpenStreetMap: {CATEGORY_RU[category]}."
        if address:
            description += f" Адрес в источнике: {address}."
        description += " Актуальное состояние и возможность посещения не проверены."
        places.append({
            "id": source_id, "name": name.strip(), "lat": lat, "lon": lon,
            "category": category, "description": description,
            "aliases": list(dict.fromkeys(value for key, value in tags.items() if key in {"name", "name:ru", "name:kk", "name:en", "alt_name", "brand"} and isinstance(value, str))),
            "features": {"socket": socket, "wheelchair": _affirmative(tags.get("wheelchair")), "quiet": "unknown"},
            "opening_hours": tags.get("opening_hours"),
            "source_url": f"https://www.openstreetmap.org/{kind}/{identifier}",
            "collected_at": collected_at, "source_updated_at": element.get("timestamp"),
            "verification": "source_reported_not_field_verified",
            "source_tags": {key: value for key, value in tags.items() if key in {"name", "name:ru", "name:kk", "name:en", "amenity", "tourism", "leisure", "historic", "shop", "opening_hours", "wheelchair", "socket", "addr:street", "addr:housenumber", "website", "contact:website"} or key.startswith("socket:")},
        })
    return sorted(places, key=lambda item: item["id"])


def collect(endpoint: str, bbox: tuple) -> dict:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Overpass endpoint должен быть HTTPS URL.")
    data = urlencode({"data": build_query(bbox)}).encode()
    request = Request(endpoint, data=data, headers={"User-Agent": "AstanaWalkHackathon/0.1 (batch educational POI collector)", "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    with urlopen(request, timeout=55) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ответ Overpass превышает 25 MB; сузьте bbox.")
    payload = json.loads(raw)
    if payload.get("remark"):
        raise ValueError("Overpass вернул предупреждение/частичный ответ: " + str(payload["remark"]))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--bbox", nargs=4, type=float, default=DEFAULT_BBOX, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--output", type=Path, default=Path("data/poi.osm.json"))
    parser.add_argument("--input", type=Path, help="Normalize a previously saved Overpass JSON, without network")
    args = parser.parse_args()
    south, west, north, east = args.bbox
    if not all(math.isfinite(value) for value in args.bbox) or not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        parser.error("bbox: ожидаются корректные SOUTH WEST NORTH EAST")
    try:
        if args.input:
            with args.input.open(encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        else:
            payload = collect(args.endpoint, tuple(args.bbox))
        if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
            raise ValueError("Ожидается Overpass JSON с массивом elements.")
        if payload.get("remark"):
            raise ValueError("Overpass вернул предупреждение/частичный ответ: " + str(payload["remark"]))
        # For replay files the normalization time is explicit; it does not claim
        # to be the original remote observation time.
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        places = normalize_elements(payload, collected_at)
        if not places:
            raise ValueError("Подходящих именованных POI нет; существующий файл не изменён.")
        document = {"dataset": {
            "name": "Astana POI / OpenStreetMap", "version": 1, "is_demo": False,
            "collected_at": collected_at,
            "collection_method": "replay_normalization" if args.input else "overpass_batch",
            "source_file": args.input.name if args.input else None,
            "endpoint": args.endpoint, "bbox": list(args.bbox),
            "source_snapshot_at": payload.get("osm3s", {}).get("timestamp_osm_base"),
            "attribution": "© OpenStreetMap contributors", "license": "ODbL 1.0",
            "license_url": "https://www.openstreetmap.org/copyright",
            "limitations": ["Не проверено на месте", "Не является информацией об открытии сейчас", "Отсутствующие теги означают неизвестность", "Геометрия way/relation представлена центром объекта"],
        }, "places": places}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.output)
        print(f"Saved {len(places)} POI to {args.output}; © OpenStreetMap contributors / ODbL.")
        return 0
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as error:
        print(f"Сбор не выполнен: {error}. Существующий набор не заменён. Используйте --input для сохранённого Overpass JSON.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
