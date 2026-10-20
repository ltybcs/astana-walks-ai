"""Isolated AI walks lab: shipped OSM plus an explicitly enabled Google pilot.

Run python -m app.google_lab and open http://127.0.0.1:8003.
The existing RAG core is reused; inference is opt-in on an existing local model.
Google pilot is separately opt-in; main HTTP server and OSM core stay unchanged.
Google frames and optional map tiles are browser-only frontend choices.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
from threading import Lock
from time import monotonic
from urllib.parse import urlparse

from .google_budget import GoogleError
from .google_client import GoogleClient
from .google_grounded import GoogleGrounded, disabled_status
from .generation import recommend as core_recommend
from .llm import LLMConfig
from .retrieval import Retriever, detect_intent
from .lab_context import PUBLIC_ORIGINS, ContextError, resolve_request

ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_BYTES = 25_000_000
MAX_STATIC_BYTES = 8_000_000
MAX_REQUEST_BYTES = 16_384
CATEGORY_LABELS = {
    "park": "Парк или сад", "playground": "Детская площадка", "cafe": "Кафе",
    "restaurant": "Ресторан", "bar": "Бар", "mall": "Торговый центр",
    "monument": "Памятник", "attraction": "Достопримечательность",
    "museum": "Музей", "theatre": "Театр", "viewpoint": "Видовая площадка",
    "promenade": "Прогулочная территория",
}
STATIC_FILES = {
    "/": ("web/google-lab/index.html", "text/html; charset=utf-8"),
    "/lab.js": ("web/google-lab/lab.js", "application/javascript; charset=utf-8"),
    "/lab.css": ("web/google-lab/lab.css", "text/css; charset=utf-8"),
    "/embeds.json": ("web/google-lab/embeds.json", "application/json; charset=utf-8"),
    "/vendor/leaflet/1.9.4/leaflet.js": ("web/vendor/leaflet/1.9.4/leaflet.js", "application/javascript; charset=utf-8"),
    "/vendor/leaflet/1.9.4/leaflet.css": ("web/vendor/leaflet/1.9.4/leaflet.css", "text/css; charset=utf-8"),
}
for _image in ("marker-icon.png", "marker-icon-2x.png", "marker-shadow.png", "layers.png", "layers-2x.png"):
    STATIC_FILES["/vendor/leaflet/1.9.4/images/" + _image] = (
        "web/vendor/leaflet/1.9.4/images/" + _image, "image/png"
    )

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; "
    "frame-src https://www.google.com; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _safe_file(root: Path, relative: str) -> Path:
    """Resolve one operator-selected file, rejecting traversal and symlinks."""
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or "\\" in relative or "\0" in relative or any(part in {".", ".."} or ":" in part for part in path.parts):
        raise ValueError("Unsafe local source path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink is not a lab source")
    result = current.resolve()
    if not result.is_relative_to(root) or not result.is_file():
        raise ValueError("Local source is missing or outside the lab")
    return result


def _read_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Local source exceeds size bound")
    return raw


def _read_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate source key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("Non-finite source number")

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a source object")
    # Also rejects numeric literal overflow, e.g. 1e999 in arbitrary metadata.
    _json_bytes(value)
    return value


def load_catalog(root: Path = ROOT) -> dict:
    """Join immutable OSM records to their exact raw source; never use a network."""
    root = Path(root).resolve()
    dataset_path = _safe_file(root, "data/poi.astana.json")
    dataset_raw = _read_bytes(dataset_path, MAX_SOURCE_BYTES)
    document = _read_json(dataset_raw)
    metadata, original_places = document.get("dataset"), document.get("places")
    if not isinstance(metadata, dict) or metadata.get("is_demo") is not False or not isinstance(original_places, list) or not original_places:
        raise ValueError("The lab requires a nonempty real OSM snapshot")
    raw_relative = metadata.get("raw_snapshot")
    if not isinstance(raw_relative, str) or not raw_relative.startswith("data/snapshots/"):
        raise ValueError("Missing confined OSM raw snapshot")
    raw_path = _safe_file(root, raw_relative)
    raw_bytes = _read_bytes(raw_path, MAX_SOURCE_BYTES)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    if raw_sha != metadata.get("raw_snapshot_sha256"):
        raise ValueError("OSM raw snapshot checksum mismatch")
    raw_document = _read_json(raw_bytes)
    elements = raw_document.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Raw OSM elements are missing")
    by_id = {}
    for element in elements:
        if not isinstance(element, dict) or element.get("type") not in {"node", "way", "relation"} or type(element.get("id")) is not int:
            raise ValueError("Invalid raw OSM identity")
        identifier = "osm:" + element["type"] + "/" + str(element["id"])
        tags = element.get("tags")
        if identifier in by_id or not isinstance(tags, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in tags.items()):
            raise ValueError("Duplicate identity or invalid raw OSM tags")
        by_id[identifier] = element
    places, seen, excluded = [], set(), 0
    for original in original_places:
        if not isinstance(original, dict):
            raise ValueError("Invalid normalized POI")
        identifier = original.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"osm:(?:node|way|relation)/[0-9]+", identifier) or identifier in seen or identifier not in by_id:
            raise ValueError("Normalized POI has no unique raw OSM source")
        seen.add(identifier)
        if not isinstance(original.get("name"), str) or not original["name"].strip() or not isinstance(original.get("category"), str) or not original["category"]:
            raise ValueError("Normalized POI name/category missing")
        if any(type(original.get(key)) not in (int, float) or not math.isfinite(original[key]) or abs(original[key]) > bound for key, bound in (("lat", 90), ("lon", 180))):
            raise ValueError("Invalid POI coordinates")
        element = by_id[identifier]
        tags = element["tags"]
        access = tags.get("access", "unknown")
        if access.strip().lower() in {"private", "no"}:
            excluded += 1
            continue
        place = deepcopy(original)
        if not isinstance(place.get("source_tags", {}), dict):
            raise ValueError("Invalid normalized source tags")
        place["source_tags"] = {**place.get("source_tags", {}), **tags}
        place.update(
            source="OSM",
            source_url="https://www.openstreetmap.org/" + identifier.removeprefix("osm:"),
            access_evidence=access,
            cuisine=tags.get("cuisine"),
            operator=tags.get("operator"),
            source_description=tags.get("description"),
            raw_provenance={
                "snapshot_path": raw_relative,
                "snapshot_sha256": raw_sha,
                "osm_id": identifier,
                "source_updated_at": element.get("timestamp"),
            },
        )
        places.append(place)
    dataset = deepcopy(metadata)
    dataset.update(
        source="OSM",
        dataset_sha256=hashlib.sha256(dataset_raw).hexdigest(),
        lab_enrichment="Exact raw OSM tag join; no provider requests or invented names",
        raw_snapshot_sha256=raw_sha,
    )
    catalog = {
        "places": places,
        "dataset": dataset,
        "summary": {
            "source_count": len(original_places),
            "visible_count": len(places),
            "excluded_restricted_count": excluded,
            "categories": dict(sorted(Counter(place["category"] for place in places).items())),
            "generated_names_count": sum(place.get("name_generated") is True for place in places),
        },
        "category_labels": dict(CATEGORY_LABELS),
    }
    _json_bytes(catalog)
    return catalog


def rag_document(catalog: dict) -> dict:
    """Enrich only the in-memory RAG copy, preserving names and source aliases."""
    document = deepcopy({"dataset": catalog["dataset"], "places": catalog["places"]})
    for place in document["places"]:
        facts = [
            ("Кухня в OSM", place.get("cuisine")),
            ("Оператор в OSM", place.get("operator")),
            ("Описание в OSM", place.get("source_description")),
        ]
        if place.get("access_evidence") not in {None, "", "unknown"}:
            facts.append(("Тег доступа в OSM, не проверка входа сейчас", place["access_evidence"]))
        suffix = " ".join(label + ": " + value + "." for label, value in facts if value)
        if suffix:
            place["description"] = place["description"].rstrip() + " " + suffix
    return document


def _local_llm_config() -> LLMConfig | None:
    config = LLMConfig.from_env()
    if config is None:
        return None
    parsed = urlparse(config.base_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Локальная модель: неверный порт.") from error
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or config.api_key or port == 0:
        raise ValueError("Лаборатория использует только локальную модель без API-ключа и облачных endpoint.")
    return config


class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path, *, llm_enabled: bool = False, recommender=None, google=None):
        self.root = Path(root).resolve()
        self.catalog = load_catalog(self.root)
        self.catalog_json = _json_bytes(self.catalog)
        document = rag_document(self.catalog)
        self.retriever = Retriever(data=document)
        self.index_sha256 = hashlib.sha256(_json_bytes(document)).hexdigest()
        self.by_id = {place["id"]: place for place in self.catalog["places"]}
        self.recommender = recommender if recommender is not None else core_recommend
        self.llm_requested = bool(llm_enabled)
        self.llm_config = _local_llm_config() if llm_enabled else None
        self.llm_enabled = self.llm_config is not None
        self.google = google
        if isinstance(self.google, GoogleGrounded):
            self.google.resolver = self.resolve
        self._stats_lock = Lock()
        self._provider_attempts = 0
        self._validated_answers = 0
        super().__init__(address, LabHandler)
        if self.google is not None:
            self.google.start_cleanup()

    def server_close(self):
        try:
            super().server_close()
        finally:
            if self.google is not None:
                self.google.close()

    def llm_available(self) -> bool:
        if not self.llm_enabled:
            return False
        try:
            return _local_llm_config() == self.llm_config
        except ValueError:
            return False

    @property
    def health_json(self) -> bytes:
        with self._stats_lock:
            attempts, validated = self._provider_attempts, self._validated_answers
        google_status = self.google.status() if self.google else disabled_status()
        google_attempts = google_status["quota"]["used"]
        return _json_bytes({
            "status": "ok", "service": "google-maps-lab", "lab": True,
            "free": self.google is None, "mode": "rag-assistant", "provider_calls": attempts,
            "provider_call_count_scope": "Core-reported local LLM attempts in this lab process only",
            "backend_outbound_calls": attempts > 0 or type(google_attempts) is int and google_attempts > 0,
            "backend_outbound_calls_scope": "Local LLM attempts in this lab process OR Google HTTP attempts across the shared pilot ledger",
            "local_llm_outbound_calls": attempts > 0, "google_http_attempts_pilot": google_attempts,
            "llm_opt_in": self.llm_requested, "llm_configured": self.llm_available(),
            "llm_validated_answers": validated, "live_verified": False,
            "generation": "local-llm-with-extractive-fallback" if self.llm_available() else "extractive",
            "source_count": self.catalog["summary"]["source_count"],
            "places_count": self.catalog["summary"]["visible_count"],
            "rag_index_sha256": self.index_sha256,
            "data_source": "Shipped OSM snapshot", "google_embed": "browser-only",
            "google": google_status,
        })

    def resolve(self, payload: object, *, probe=False) -> dict:
        return resolve_request(payload, catalog=self.catalog, probe=probe)

    def recommend(self, payload: object) -> dict:
        resolved = self.resolve(payload)
        context = resolved["resolved_context"]
        available = self.llm_available()
        alternatives = []
        for index, minutes in enumerate(context["budget_options"]):
            variant_context = deepcopy(context)
            variant_context["minutes"] = minutes
            request = {"query": resolved["query"], "minutes": minutes, "origin": resolved["origin"],
                       "time_of_day": resolved["time_of_day"]}
            proposed = max(10, min(60, minutes // 4)) if len(context["budget_options"]) > 1 else None
            alternatives.append(self._recommend_one(request, variant_context, use_llm=available and index == 0,
                                                    proposed_visit_minutes=proposed))
        response = deepcopy(alternatives[0])
        response["alternatives"] = alternatives
        return response

    def _recommend_one(self, request: dict, context: dict, *, use_llm: bool, proposed_visit_minutes: int | None = None) -> dict:
        retrieval_request = dict(request)
        intent = detect_intent(request["query"], request["time_of_day"])
        generic = bool(re.search(r"необычн\w*|пешком", request["query"].casefold())) and not any(
            intent[key] for key in ("coffee", "socket", "quiet", "child", "tourist", "accessible", "outdoor", "preferred_categories", "excluded_categories"))
        if generic:
            retrieval_request["query"] += " Прогулка."
        retriever, index_sha = self.retriever, self.index_sha256
        allocation = {"policy": "base_visit_profile", "proposed_visit_minutes_per_stop": None,
                      "is_source_fact": False}
        if proposed_visit_minutes is not None:
            if type(proposed_visit_minutes) is not int or not 10 <= proposed_visit_minutes <= 60:
                raise ValueError("Invalid app visit proposal")
            document = rag_document(self.catalog)
            for place in document["places"]:
                place["visit_minutes"] = proposed_visit_minutes
            index_sha = hashlib.sha256(_json_bytes(document)).hexdigest()
            retriever = Retriever(data=document)
            allocation = {"policy": "app_budget_proposal", "proposed_visit_minutes_per_stop": proposed_visit_minutes,
                          "is_source_fact": False}
        response = self.recommender(retriever, **retrieval_request, use_llm=use_llm)
        context = {**context, "time_allocation": deepcopy(allocation)}
        response["diagnostics"]["time_allocation"] = deepcopy(allocation)
        if proposed_visit_minutes is not None:
            response["warnings"].append(f"До {proposed_visit_minutes} минут на остановку предложено приложением для этого бюджета; OSM не подтверждает длительность посещения.")
        response.pop("clarification", None)
        if not response["recommendations"]:
            response["empty_message"] = "В этой базе нет мест, соответствующих всем условиям и бюджету с возвращением. Неизвестные свойства не считаются подтверждёнными."
        if generic:
            response["warnings"].append("Для общего запроса показаны места для пешего осмотра; специальных занятий база не подтверждает.")
        response["diagnostics"]["lab_generic_walk_fallback"] = generic
        for item in response["recommendations"]:
            source = self.by_id[item["id"]]
            item.update({key: deepcopy(source[key]) for key in ("source", "access_evidence", "cuisine", "operator", "raw_provenance")})
        if any(item["access_evidence"].strip().lower() == "customers" for item in response["recommendations"]):
            response["warnings"].append("Для выбранного места в OSM указан доступ только для клиентов; условия посещения нужно проверить.")
        if self.llm_requested and not self.llm_available():
            response["warnings"].append("Подключение локальной модели не настроено или изменилось; использован ответ по базе POI без свободной генерации.")
        if re.search(r"вылет\w*|аэропорт\w*|\bрейс\w*|\bflight\w*", request["query"].casefold()):
            response["warnings"].append("Дорога в аэропорт, регистрация и контроль не входят в план прогулки; оставьте отдельный запас времени перед вылетом.")
        response["warnings"].extend(context["assumptions"])
        response["budget_minutes"] = request["minutes"]
        response["resolved_context"] = context
        response["diagnostics"]["lab_index_sha256"] = index_sha
        response["diagnostics"]["lab_base_index_sha256"] = self.index_sha256
        attempts = response["diagnostics"].get("llm_attempts", 0)
        with self._stats_lock:
            if type(attempts) is int and 0 <= attempts <= 2:
                self._provider_attempts += attempts
            self._validated_answers += int(response["mode"].startswith("llm-grounded"))
        return response


class LabHandler(BaseHTTPRequestHandler):
    server: LabServer
    server_version = "AstanaWalksLab/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, _format, *args):
        # Never retain query, origin, client addresses or URLs in logs.
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _discard_small_rejected_body(self):
        """Avoid a TCP reset hiding early errors from ordinary small clients.

        Never interpret chunked/ambiguous framing, never process another request.
        A single bounded body is discarded under one 200ms wall-clock deadline.
        """
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or re.fullmatch(r"[0-9]{1,6}", lengths[0]) is None:
            return 0
        length = int(lengths[0])
        if not 0 < length <= MAX_REQUEST_BYTES:
            return 0
        timeout = self.connection.gettimeout()
        deadline, discarded = monotonic() + 0.2, 0
        try:
            while discarded < length:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(4096, length - discarded))
                if not chunk:
                    break
                discarded += len(chunk)
        except (OSError, TimeoutError):
            pass
        finally:
            try:
                self.connection.settimeout(timeout)
            except OSError:
                pass
        return discarded

    def _reject(self, status, body, *, head=False):
        self.close_connection = True
        self._discard_small_rejected_body()
        self._send(status, body, head=head)

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", *, head: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                pass

    def _local_host(self) -> bool:
        values = self.headers.get_all("Host", [])
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"{self.server.server_address[0]}:{port}"}
        if len(values) != 1 or values[0].lower() not in allowed:
            return False
        origins = self.headers.get_all("Origin", [])
        return not origins or len(origins) == 1 and origins[0] in {"http://" + value for value in allowed}

    def _get(self, *, head: bool = False):
        if not self._local_host():
            self._reject(403, _json_bytes({"error": "Local lab requests only", "code": "forbidden"}), head=head)
            return
        if self.path == "/api/catalog":
            self._send(200, self.server.catalog_json, head=head)
        elif self.path == "/api/health":
            self._send(200, self.server.health_json, head=head)
        elif self.path == "/api/google/status":
            self._send(200, _json_bytes(self.server.google.status() if self.server.google else disabled_status()), head=head)
        elif self.path in STATIC_FILES:
            relative, content_type = STATIC_FILES[self.path]
            try:
                body = _read_bytes(_safe_file(self.server.root, relative), MAX_STATIC_BYTES)
                if self.path == "/embeds.json":
                    _read_json(body)
            except (OSError, ValueError, UnicodeError, RecursionError):
                self._send(404, _json_bytes({"error": "Lab asset unavailable"}), head=head)
                return
            self._send(200, body, content_type, head=head)
        else:
            self._send(404, _json_bytes({"error": "Lab route not found"}), head=head)

    def do_GET(self):
        self._get()

    def do_HEAD(self):
        self._get(head=True)

    def do_POST(self):
        if not self._local_host():
            self._reject(403, _json_bytes({"error": "Local lab requests only", "code": "forbidden"}))
            return
        if self.path not in {"/api/context", "/api/recommend", "/api/google/preview", "/api/google/recommend", "/api/google/details"}:
            self._reject(404, _json_bytes({"error": "Lab route not found", "code": "not_found"}))
            return
        if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
            self._reject(415, _json_bytes({"error": "Нужен Content-Type: application/json.", "code": "unsupported_media_type"}))
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
            self._reject(400, _json_bytes({"error": "Нужен один Content-Length, без Transfer-Encoding.", "code": "invalid_request"}))
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self._reject(400, _json_bytes({"error": "Некорректный Content-Length.", "code": "invalid_request"}))
            return
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._reject(413, _json_bytes({"error": "JSON должен занимать не более 16384 байт.", "code": "request_too_large"}))
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            payload = _read_json(raw)
        except TimeoutError:
            self._send(408, _json_bytes({"error": "Истекло время получения запроса.", "code": "request_timeout"}))
            return
        except (ValueError, UnicodeError, RecursionError):
            self._send(400, _json_bytes({"error": "Некорректный JSON запроса.", "code": "invalid_json"}))
            return
        try:
            if self.path == "/api/context":
                resolved = self.server.resolve(payload, probe=True)
                response = {"needs_geolocation": resolved["needs_geolocation"], "resolved_context": resolved["resolved_context"]}
            elif self.path == "/api/recommend":
                response = self.server.recommend(payload)
            else:
                if self.server.google is None:
                    raise GoogleError("google_disabled", "Google-пилот не включён; OSM-подбор работает отдельно.", 503)
                method = {"/api/google/preview": "preview", "/api/google/recommend": "recommend", "/api/google/details": "details"}[self.path]
                response = getattr(self.server.google, method)(payload)
            serialized = _json_bytes(response)
        except GoogleError as error:
            response = error.as_dict()
            response["quota"] = self.server.google.status()["quota"] if self.server.google else disabled_status()["quota"]
            self._send(error.status, _json_bytes(response))
            return
        except ContextError as error:
            self._send(error.status, _json_bytes(error.as_dict()))
            return
        except Exception:
            self._send(500, _json_bytes({"error": "Не удалось составить план прогулки. Повторите запрос.", "code": "recommendation_failed"}))
            return
        self._send(200, serialized)

    def _method_not_allowed(self):
        self._reject(405, _json_bytes({"error": "Метод не поддерживается.", "code": "method_not_allowed"}))

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def create_server(host: str = "127.0.0.1", port: int = 8003, *, root: Path = ROOT, llm_enabled: bool = False, recommender=None, google=None, google_config=None) -> LabServer:
    """Create one context-managed lab server; startup never calls inference."""
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Lab binds only to IPv4 loopback") from error
    if address.version != 4 or not address.is_loopback or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Lab binds only to IPv4 loopback and a valid port")
    if google is not None and google_config is not None:
        raise ValueError("Choose injected Google service or explicit runtime config")
    if google_config is not None:
        client = GoogleClient.from_runtime(google_config)
        cache, cache_init_error = None, False
        if client.cache_dir is not None:
            try:
                from .google_cache import GoogleCache
                cache = GoogleCache(client.cache_dir)
            except Exception:
                cache_init_error = True
        google = GoogleGrounded(client, resolve_request, llm_config=_local_llm_config() if llm_enabled else None,
                                cache=cache, cache_init_error=cache_init_error)
    return LabServer((host, port), Path(root), llm_enabled=llm_enabled, recommender=recommender, google=google)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--llm", action="store_true", help="Use an already configured loopback LLM; never start or download a model")
    parser.add_argument("--google", action="store_true", help="Opt in to the separately budgeted official Google pilot")
    parser.add_argument("--google-config", help="Absolute ignored JSON with key_file, shared ledger_file and optional cache_dir; never printed")
    args = parser.parse_args(argv)
    if bool(args.google) != bool(args.google_config):
        parser.error("--google and --google-config must be supplied together")
    try:
        with create_server(args.host, args.port, llm_enabled=args.llm, google_config=args.google_config if args.google else None) as server:
            print(f"AI walks lab: http://127.0.0.1:{server.server_port} — {server.catalog['summary']['visible_count']} OSM places", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
    except (ValueError, OSError) as error:
        parser.exit(1, f"Lab startup failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
