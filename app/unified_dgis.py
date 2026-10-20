"""Transient nearby 2GIS search sharing the primary app's existing 20/day ledger.

No provider call is made by construction/status. The operator must inject the
absolute PRIMARY workspace root; missing usage state never creates a new quota.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .dgis_live import (_LiveClient, _NoRedirect, DGISLiveError, ASTANA_BBOX,
                        ATTRIBUTION, CATEGORIES, ENDPOINT, FIELDS, PAGE_SIZE, MAX_BYTES,
                        _state_error)
from .retrieval import detect_intent, distance_m
from scripts.collect_2gis import CollectionError, normalize_items, response_items

CATEGORY_QUERIES = {**CATEGORIES, "restaurant": "Рестораны", "bar": "Бары"}


def _invalid():
    return DGISLiveError("Некорректные параметры живого поиска 2ГИС.", code="invalid_request", http_status=400)


def _strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate key")
            value[key] = item
        return value
    def invalid(_):
        raise ValueError("Nonfinite number")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    json.dumps(value, allow_nan=False)  # Reject exponent overflow in metadata too.
    return value


def _category(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise _invalid()
    intent = detect_intent(query)
    requested = [name for name in intent["preferred_categories"] if name in CATEGORY_QUERIES]
    if requested:
        return requested[0]
    if intent["child"]:
        return "playground"
    return "attraction"


def _nearby_box(origin, radius):
    if not isinstance(origin, dict) or any(type(origin.get(key)) not in {int, float} or not math.isfinite(origin[key])
                                          for key in ("lat", "lon")):
        raise _invalid()
    south, west, north, east = ASTANA_BBOX
    if not south <= origin["lat"] <= north or not west <= origin["lon"] <= east or type(radius) is not int or not 100 <= radius <= 2000:
        raise _invalid()
    angular = radius / 6371000
    delta_lat = math.degrees(angular)
    delta_lon = math.degrees(math.asin(math.sin(angular) / math.cos(math.radians(origin["lat"]))))
    return (max(south, origin["lat"] - delta_lat), max(west, origin["lon"] - delta_lon),
            min(north, origin["lat"] + delta_lat), min(east, origin["lon"] + delta_lon))


class UnifiedDGIS:
    def __init__(self, root, *, http_open=None, clock=time.time, monotonic_clock=time.monotonic, timeout=25):
        path = Path(root)
        if not path.is_absolute() or type(timeout) not in {int, float} or not 1 <= timeout <= 25:
            raise ValueError("An absolute primary root and bounded timeout are required")
        self._client = _LiveClient(path, clock)
        self._http_open = http_open
        self.clock, self.monotonic, self.timeout = clock, monotonic_clock, timeout

    def _existing_state(self):
        # Called before lock creation, and again under the same stable lock used
        # by dgis_live. Never let _attempts seed a second worktree-local budget.
        try:
            path = self._client._path("usage.json")
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64000:
                raise _state_error()
            state = _strict_json(path.read_bytes())
            if not isinstance(state, dict) or set(state) != {"schema_version", "attempts"} or type(state["schema_version"]) is not int or state["schema_version"] != 1 or not isinstance(state["attempts"], list):
                raise _state_error()
            if any(type(value) not in {int, float} or not math.isfinite(value) or value < 0 for value in state["attempts"]):
                raise _state_error()
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            raise _state_error() from None

    def status(self):
        try:
            self._existing_state()
            configured = bool(self._client._key())
            if not configured:
                return {"configured": False, "available": False, "quota": None, "code": "not_configured"}
            with self._client._locked():
                self._existing_state()
                now = self.clock()
                quota = self._client._quota(self._client._attempts(now), now)
            return {"configured": True, "available": quota["remaining"] > 0, "quota": quota,
                    "code": "ready" if quota["remaining"] else "quota_exhausted"}
        except DGISLiveError as error:
            return {"configured": False, "available": False, "quota": None, "code": error.code}

    def _remaining(self, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise DGISLiveError("Истекло общее время ожидания 2ГИС.", code="provider_timeout", http_status=504)
        return remaining

    def _read(self, response, deadline):
        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if mime != "application/json":
            raise ValueError("Unexpected media type")
        chunks, size = [], 0
        while True:
            remaining = self._remaining(deadline)
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            read = getattr(response, "read1", response.read)
            chunk = read(min(65536, MAX_BYTES + 1 - size))
            self._remaining(deadline)
            size += len(chunk)
            if size > MAX_BYTES:
                raise DGISLiveError("Ответ 2ГИС превышает допустимый размер.", code="provider_response_too_large", http_status=502)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def search(self, query, origin, radius):
        category = _category(query)
        bbox = _nearby_box(origin, radius)
        self._existing_state()
        key = self._client._key()
        if not key:
            raise DGISLiveError("Живой поиск 2ГИС не настроен на сервере.", code="not_configured", http_status=503)
        with self._client._locked():
            self._existing_state()
            now = self.clock()
            attempts = self._client._attempts(now)
            quota = self._client._quota(attempts, now)
            if quota["remaining"] == 0:
                raise DGISLiveError("Лимит 20 попыток 2ГИС за последние 24 часа исчерпан.", code="quota_exhausted", http_status=429, quota=quota)
            attempts.append(now)
            self._client._write_attempts(attempts)
            quota = self._client._quota(attempts, now)
            south, west, north, east = bbox
            params = {"key": key, "q": CATEGORY_QUERIES[category], "point1": f"{west},{north}", "point2": f"{east},{south}",
                      "page": 1, "page_size": PAGE_SIZE, "fields": FIELDS}
            request = Request(ENDPOINT + "?" + urlencode(params), headers={"Accept": "application/json", "User-Agent": "AstanaWalks/1.0 (unified live search)"})
            deadline = self.monotonic() + self.timeout
            try:
                opener = self._http_open or build_opener(ProxyHandler({}), _NoRedirect()).open
                with opener(request, timeout=self._remaining(deadline)) as response:
                    payload = _strict_json(self._read(response, deadline))
                items = response_items(payload)
                stamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
                normalized = normalize_items(items[:PAGE_SIZE], stamp, bbox)
                places = [place for place in normalized if distance_m(origin, place) <= radius]
            except DGISLiveError as error:
                error.quota = quota
                raise
            except HTTPError as error:
                status = error.code if type(error.code) is int else None
                error.close()
                raise DGISLiveError("2ГИС отклонил запрос; проверьте доступ и лимит сервиса.", code="provider_http_error", http_status=502,
                                    quota=quota, provider_code=status) from None
            except (TimeoutError, URLError, OSError) as error:
                timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
                raise DGISLiveError("Истекло время ожидания 2ГИС." if timed_out else "2ГИС недоступен: сетевая ошибка.",
                                    code="provider_timeout" if timed_out else "provider_unavailable", http_status=504 if timed_out else 502, quota=quota) from None
            except (CollectionError, ValueError, UnicodeError, TypeError, OverflowError, RecursionError, AttributeError):
                raise DGISLiveError("2ГИС не подтвердил корректный ответ поиска.", code="provider_invalid_response", http_status=502, quota=quota) from None
            return {"places": places, "attribution": dict(ATTRIBUTION), "quota": quota, "category": category, "fetched_at": stamp,
                    "search_scope": {"radius_meters": radius, "returned_count": len(items[:PAGE_SIZE]), "filtered_count": len(items[:PAGE_SIZE]) - len(places)},
                    "warnings": ["Живой поиск 2ГИС: одна страница до 10 объектов в ограниченной области, не полный каталог.",
                                 "Сведения не проверены на месте; неизвестные часы и свойства не являются подтверждением."]}
