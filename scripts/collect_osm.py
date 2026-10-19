"""Collect bounded Astana POI from Overpass; keep provenance and unknowns intact.

Examples:
  python scripts/collect_osm.py --output data/poi.osm.json
  python scripts/collect_osm.py --input raw-overpass.json --output data/poi.osm.json

Collection needs network access. It is a batch operation, never called per user
query. Refresh intentionally; do not hit the public API on every application run.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from threading import Event
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_BBOX = (51.02, 71.30, 51.22, 71.55)  # south, west, north, east
ASTANA_BBOX = (51.00, 71.20, 51.32, 71.70)
QUERY_PROFILE = "astana_walk_v2"
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


def validate_bbox(bbox) -> tuple:
    if len(bbox) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in bbox):
        raise ValueError("bbox must contain four finite coordinates")
    south, west, north, east = bbox
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("bbox coordinates are out of order/range")
    return tuple(bbox)


def build_query(bbox: tuple[float, float, float, float], *, profile: str = "legacy_named") -> str:
    validate_bbox(bbox)
    if profile not in {"legacy_named", QUERY_PROFILE}:
        raise ValueError("Unknown collection profile")
    box = ",".join(str(value) for value in bbox)
    selectors = []
    for key, value, _ in CATEGORY_RULES:
        named = '["name"]' if profile == "legacy_named" else '[~"^name(:ru|:kk|:en)?$"~"."]'
        if profile == QUERY_PROFILE and key == "leisure":
            named = ""
        selectors.append(f'  nwr["{key}"="{value}"]{named}({box});')
    selectors = "\n".join(selectors)
    return f"[out:json][timeout:45];\n(\n{selectors}\n);\nout center tags meta;\n"


def _affirmative(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    if value in {"yes", "designated", "permissive"}:
        return "yes"
    if value in {"no", "private"}:
        return "no"
    return "unknown"


def normalize_elements(payload: dict, collected_at: str, *, bbox=None, profile: str = "legacy_named") -> list[dict]:
    if bbox is not None:
        validate_bbox(bbox)
    places = []
    seen = set()
    for element in payload.get("elements", []):
        if not isinstance(element, dict):
            continue
        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            continue
        category = next((category for key, value, category in CATEGORY_RULES if tags.get(key) == value), None)
        if not category:
            continue
        name_keys = ("name:ru", "name", "name:en", "name:kk") if profile == QUERY_PROFILE else ("name:ru", "name", "name:en")
        name_key = next((key for key in name_keys if isinstance(tags.get(key), str) and tags[key].strip()), None)
        source_name = tags[name_key].strip() if name_key else None
        generated = source_name is None
        if generated and not (profile == QUERY_PROFILE and tags.get("leisure") in {"park", "garden", "playground"}):
            continue
        location = element if "lat" in element and "lon" in element else element.get("center", {})
        if not isinstance(location, dict):
            continue
        lat, lon = location.get("lat"), location.get("lon")
        if type(lat) not in (int, float) or type(lon) not in (int, float) or not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            continue
        if bbox is not None and not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
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
        name = source_name or f"Без названия: {CATEGORY_RU[category]} (OSM {kind}/{identifier})"
        item = {
            "id": source_id, "name": name, "lat": lat, "lon": lon,
            "category": category, "description": description,
            "aliases": list(dict.fromkeys(value for key, value in tags.items() if key in {"name", "name:ru", "name:kk", "name:en", "alt_name", "brand"} and isinstance(value, str))),
            "features": {"socket": socket, "wheelchair": _affirmative(tags.get("wheelchair")), "quiet": "unknown"},
            "opening_hours": tags.get("opening_hours"),
            "source_url": f"https://www.openstreetmap.org/{kind}/{identifier}",
            "collected_at": collected_at, "source_updated_at": element.get("timestamp"),
            "verification": "source_reported_not_field_verified",
            "source_tags": {key: value for key, value in tags.items() if key in {"name", "name:ru", "name:kk", "name:en", "amenity", "tourism", "leisure", "historic", "shop", "opening_hours", "wheelchair", "socket", "addr:street", "addr:housenumber", "website", "contact:website"} or key.startswith("socket:")},
        }
        if profile == QUERY_PROFILE:
            item.update(name_generated=generated, source_name=source_name, source_name_field=name_key)
        places.append(item)
    return sorted(places, key=lambda item: item["id"])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_overpass(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def parse_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate source JSON key")
            result[key] = value
        return result
    def invalid_number(value):
        raise ValueError("Non-finite source JSON number")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Overflowing source JSON number")
        return number
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid_number, parse_float=finite_float)


def collect_response(endpoint: str, bbox: tuple, *, profile="legacy_named", attempts=2, timeout=55, stop_event=None) -> tuple[dict, bytes]:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Overpass endpoint должен быть HTTPS URL.")
    if type(attempts) is not int or not 1 <= attempts <= 3 or not 5 <= timeout <= 55:
        raise ValueError("Collection attempts/timeout are outside bounded limits")
    stopped = stop_event or Event()
    deadline = monotonic() + 180
    data = urlencode({"data": build_query(bbox, profile=profile)}).encode()
    request = Request(endpoint, data=data, headers={"User-Agent": "AstanaWalks/1.0 (daily cached Astana POI refresh; https://github.com/ltybcs/astana-walks-ai)", "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "Accept-Encoding": "identity"}, method="POST")
    for attempt in range(attempts):
        if stopped.is_set() or monotonic() >= deadline:
            raise TimeoutError("Collection stopped or deadline exceeded")
        try:
            with open_overpass(request, min(timeout, max(1, deadline - monotonic()))) as response:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed Overpass response rejected")
                blocks, received = [], 0
                reader = getattr(response, "read1", response.read)
                while True:
                    if stopped.is_set() or monotonic() >= deadline:
                        raise TimeoutError("Collection stopped or deadline exceeded")
                    block = reader(min(1024 * 1024, MAX_RESPONSE_BYTES - received + 1))
                    if not block:
                        break
                    received += len(block)
                    if received > MAX_RESPONSE_BYTES:
                        raise ValueError("Ответ Overpass превышает 25 MB; сузьте bbox.")
                    blocks.append(block)
                raw = b"".join(blocks)
            payload = parse_json(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list) or payload.get("remark"):
                raise ValueError("Overpass response is malformed or partial")
            return payload, raw
        except HTTPError as error:
            retryable = error.code in {406, 429, 500, 502, 503, 504}
            error.close()
            if not retryable or attempt + 1 == attempts:
                raise
        except (URLError, OSError, HTTPException):
            if attempt + 1 == attempts:
                raise
        if stopped.wait(30) or monotonic() >= deadline:
            raise TimeoutError("Collection stopped or deadline exceeded")
    raise RuntimeError("No collection result")


def collect(endpoint: str, bbox: tuple, **kwargs) -> dict:
    return collect_response(endpoint, bbox, **kwargs)[0]


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def make_document(payload: dict, collected_at: str, bbox=ASTANA_BBOX, *, profile=QUERY_PROFILE, endpoint=DEFAULT_ENDPOINT) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list) or payload.get("remark"):
        raise ValueError("Overpass response is malformed or partial")
    metadata = payload.get("osm3s", {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid source snapshot metadata")
    snapshot_at = metadata.get("timestamp_osm_base")
    if snapshot_at is not None:
        if not isinstance(snapshot_at, str):
            raise ValueError("Invalid source snapshot timestamp")
        datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    places = normalize_elements(payload, collected_at, bbox=bbox, profile=profile)
    if not places:
        raise ValueError("Подходящих POI нет; существующий файл не изменён.")
    document = {"dataset": {"name": "Astana POI / OpenStreetMap", "version": 1, "is_demo": False, "collected_at": collected_at, "collection_method": "overpass_batch", "endpoint": endpoint, "bbox": list(bbox), "geographic_scope": "Bounded Astana urban-area rectangle, not an administrative boundary or exhaustive inventory", "query_profile": profile, "unnamed_count": sum(poi.get("name_generated", False) for poi in places), "label_policy": "name_generated=true and source_name=null identify category/OSM-ID display labels, not official names; source_tags retain actual OSM values only", "source_snapshot_at": snapshot_at, "attribution": "© OpenStreetMap contributors", "license": "ODbL 1.0", "license_url": "https://www.openstreetmap.org/copyright", "limitations": ["Не проверено на месте", "Не является информацией об открытии сейчас", "Отсутствующие теги означают неизвестность", "Геометрия way/relation представлена центром объекта", "Названия безымянных площадок/парков — явно отмеченные подписи категории и OSM ID"]}, "places": places}
    json.dumps(document, allow_nan=False)  # The HTTP API uses this same strict JSON policy.
    return document


def write_collection(document: dict, raw: bytes, query: str, output: Path, snapshot_dir: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%fZ")
    raw_path = snapshot_dir / f"astana-{stamp}.overpass.json"
    query_path = snapshot_dir / f"astana-{stamp}.overpassql"
    root = Path(__file__).resolve().parents[1]
    def reference(path):
        resolved = path.resolve()
        return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else resolved.name
    document["dataset"].update(raw_snapshot=reference(raw_path), query_file=reference(query_path), raw_snapshot_sha256=hashlib.sha256(raw).hexdigest())
    encoded = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    atomic_write(raw_path, raw)
    atomic_write(query_path, query.encode("utf-8"))
    # Publish normalized data last, after its immutable source files exist.
    atomic_write(output, encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--bbox", nargs=4, type=float, default=ASTANA_BBOX, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--output", type=Path, default=Path("data/poi.osm.json"))
    parser.add_argument("--input", type=Path, help="Normalize a previously saved Overpass JSON, without network")
    parser.add_argument("--profile", choices=("legacy_named", QUERY_PROFILE), default=QUERY_PROFILE)
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    args = parser.parse_args()
    south, west, north, east = args.bbox
    if not all(math.isfinite(value) for value in args.bbox) or not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        parser.error("bbox: ожидаются корректные SOUTH WEST NORTH EAST")
    try:
        if args.input:
            raw = args.input.read_bytes()
            payload = parse_json(raw)
        else:
            payload, raw = collect_response(args.endpoint, tuple(args.bbox), profile=args.profile)
        if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
            raise ValueError("Ожидается Overpass JSON с массивом elements.")
        if payload.get("remark"):
            raise ValueError("Overpass вернул предупреждение/частичный ответ: " + str(payload["remark"]))
        # For replay files the normalization time is explicit; it does not claim
        # to be the original remote observation time.
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        document = make_document(payload, collected_at, tuple(args.bbox), profile=args.profile, endpoint=args.endpoint)
        document["dataset"].update(collection_method="replay_normalization" if args.input else "overpass_batch", source_file=args.input.name if args.input else None)
        # Validate the same index that the application will use before publish.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.retrieval import Retriever
        Retriever(data=document)
        write_collection(document, raw, build_query(tuple(args.bbox), profile=args.profile), args.output, args.snapshot_dir)
        print(f"Saved {len(document['places'])} POI to {args.output}; © OpenStreetMap contributors / ODbL.")
        return 0
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as error:
        print(f"Сбор не выполнен: {error}. Существующий набор не заменён. Используйте --input для сохранённого Overpass JSON.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
