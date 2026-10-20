"""Bounded stdlib clients for the two official Google pilot APIs.

No calls at construction/status. No retry, redirects, content logging or cache.
MCP tool metadata is retained in memory; place content is not retained here.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
from threading import RLock, local
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .google_budget import GoogleBudget, GoogleError, _path, utc_now

MCP_URL = "https://mapstools.googleapis.com/mcp"
PLACES_URL = "https://places.googleapis.com/v1/places/"
MAX_RESPONSE = 1_048_576
MAX_LINE = 65_536
VERSIONS = {"2025-06-18", "2025-03-26"}
DETAIL_MASKS = {
    "hours": "id,displayName,formattedAddress,location,businessStatus,currentOpeningHours,regularOpeningHours,utcOffsetMinutes,googleMapsUri,attributions",
    "reviews": "id,displayName,formattedAddress,rating,userRatingCount,reviews,googleMapsUri,attributions",
}


def strict_json(raw: bytes | str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Nonfinite number")
        return number
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, object_pairs_hook=unique, parse_float=finite,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_google(request, timeout):
    # Ignore ambient proxy configuration: a key must go only to official TLS.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def valid_place_id(value) -> bool:
    # IDs are opaque, not assumed to start with ChIJ. No URL/path accepted.
    return isinstance(value, str) and 1 <= len(value) <= 512 and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def maps_url(value) -> bool:
    from urllib.parse import urlsplit
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.hostname in {"www.google.com", "maps.google.com", "maps.app.goo.gl"} and not parsed.username and not parsed.password and parsed.port in {None, 443}
    except ValueError:
        return False


class GoogleClient:
    def __init__(self, api_key: str, budget: GoogleBudget, *, http_open=None, timeout: float = 30):
        if not isinstance(api_key, str) or not 1 <= len(api_key) <= 4096 or any(char.isspace() for char in api_key):
            raise ValueError("Invalid Google runtime key")
        if not 1 <= timeout <= 60:
            raise ValueError("Invalid Google timeout")
        self._key = api_key
        self.budget, self.http_open, self.timeout = budget, http_open or open_google, timeout
        self._lock = RLock()
        self._version = None
        self._session = None
        self._schemas = None
        self.cache_dir = None
        self._rpc_id = 0
        self._workflow = local()

    @classmethod
    def from_runtime(cls, config_path):
        # This is invoked only by explicit --google --google-config operator input.
        try:
            path = _path(config_path)
            raw = path.read_bytes()
            if len(raw) > 16384:
                raise ValueError("Oversized runtime config")
            config = strict_json(raw)
            if not isinstance(config, dict) or not {"key_file", "ledger_file"} <= set(config) or set(config) - {"key_file", "ledger_file", "cache_dir"}:
                raise ValueError("Invalid runtime config")
            key_path = _path(config["key_file"])
            with key_path.open("rb") as source:
                key_raw = source.read(4097)
            if len(key_raw) > 4096:
                raise ValueError("Oversized key file")
            key = key_raw.decode("utf-8").strip()
            result = cls(key, GoogleBudget(config["ledger_file"]))
            if "cache_dir" in config:
                result.cache_dir = _path(config["cache_dir"])
            return result
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ValueError("Google runtime configuration unavailable; check the explicitly selected local files") from None

    def status(self):
        quota = self.budget.status()
        return {"enabled": True, "configured": True, "quota": quota,
                "ready": quota["state"] == "ready", "live_verified": False,
                "counter_scope": "All HTTP attempts by clients sharing this pilot ledger"}

    @contextmanager
    def workflow(self, seconds=60):
        previous = getattr(self._workflow, "deadline", None)
        self._workflow.deadline = monotonic() + seconds
        try:
            yield
        finally:
            self._workflow.deadline = previous

    def _remaining(self):
        remaining = min(self.timeout, (getattr(self._workflow, "deadline", None) or (monotonic() + 60)) - monotonic())
        if remaining <= 0:
            raise GoogleError("google_timeout", "Истекло общее время ожидания Google.", 504)
        return remaining

    def _read_timeout(self, response):
        remaining = self._remaining()
        # urllib HTTPResponse uses this socket; test streams need no socket.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)

    def _http(self, method, url, operation, payload=None, extra=None):
        headers = {"X-Goog-Api-Key": self._key, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        request = Request(url, data=json_bytes(payload) if payload is not None else None, headers=headers, method=method)
        timeout = self._remaining()
        self.budget.reserve(operation)  # Durable before any opening/DNS attempt.
        try:
            return self.http_open(request, timeout=timeout)
        except HTTPError as error:
            # Never return request URLs, headers, provider bodies or key-bearing errors.
            status = error.code
            error.close()
            if status == 429:
                raise GoogleError("google_rate_limited", "Google временно ограничил частоту запросов.", 429) from None
            raise GoogleError("google_unavailable", "Google отклонил запрос; проверьте доступ и настройку API.", 502) from None
        except (URLError, OSError, TimeoutError):
            raise GoogleError("google_unavailable", "Не удалось получить ответ Google.", 502) from None

    def _bounded_json(self, response):
        deadline, raw = monotonic() + 60, bytearray()
        while True:
            if monotonic() > deadline:
                raise ValueError("Google response deadline")
            self._read_timeout(response)
            chunk = response.read1(min(8192, MAX_RESPONSE + 1 - len(raw)))
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("Oversized Google response")
            if not chunk:
                break
        return strict_json(bytes(raw))

    def _rpc_message(self, response, request_id):
        mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if mime == "application/json":
            result = self._bounded_json(response)
        elif mime == "text/event-stream":
            deadline, total, events, data = monotonic() + 60, 0, 0, []
            result = None
            while True:
                if monotonic() > deadline:
                    raise ValueError("MCP stream deadline")
                self._read_timeout(response)
                line = response.readline(MAX_LINE + 1)
                total += len(line)
                if total > MAX_RESPONSE or len(line) > MAX_LINE:
                    raise ValueError("Oversized MCP stream")
                if not line:
                    raise ValueError("MCP stream ended without response")
                line = line.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif not line and data:
                    events += 1
                    if events > 64:
                        raise ValueError("Too many MCP events")
                    event = strict_json("\n".join(data))
                    data = []
                    if isinstance(event, dict) and event.get("id") == request_id:
                        result = event
                        break
                    if not isinstance(event, dict) or "id" in event or "method" not in event:
                        raise ValueError("Unexpected MCP event")
        else:
            raise ValueError("Unsupported MCP content type")
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or type(result.get("id")) is not int or result["id"] != request_id:
            raise ValueError("MCP response ID mismatch")
        if "error" in result:
            raise GoogleError("google_tool_failed", "Google MCP не смог выполнить операцию.", 502)
        if set(result) != {"jsonrpc", "id", "result"} or not isinstance(result["result"], dict):
            raise ValueError("Malformed MCP result")
        return result["result"]

    def _rpc(self, method, params, operation, *, notification=False):
        self._rpc_id += 1
        request_id = self._rpc_id
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            message["id"] = request_id
        headers = {}
        if self._version:
            headers["MCP-Protocol-Version"] = self._version
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        try:
            with self._http("POST", MCP_URL, operation, message, headers) as response:
                if notification:
                    if response.status != 202:
                        raise ValueError("MCP notification not accepted")
                    return {}
                result = self._rpc_message(response, request_id)
                if method == "initialize":
                    session = response.headers.get("Mcp-Session-Id")
                    if session is not None and (not 1 <= len(session) <= 2048 or any(not 0x21 <= ord(char) <= 0x7e for char in session)):
                        raise ValueError("Invalid MCP session")
                    self._session = session
                return result
        except GoogleError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, TimeoutError):
            raise GoogleError("google_protocol_error", "Ответ Google MCP не прошёл проверку протокола.", 502) from None

    def _initialize(self):
        if self._schemas is not None:
            return
        try:
            result = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "astana-walks-google-pilot", "version": "1.0"}}, "mcp.initialize")
            version = result.get("protocolVersion")
            if version not in VERSIONS or not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
                raise ValueError("Unsupported MCP capabilities/version")
            self._version = version
            self._rpc("notifications/initialized", None, "mcp.initialized", notification=True)
            listing = self._rpc("tools/list", {}, "mcp.tools_list")
            tools = listing.get("tools")
            if listing.get("nextCursor") or not isinstance(tools, list) or len(tools) > 32:
                raise ValueError("Unsupported MCP tool listing")
            schemas = {}
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise ValueError("Invalid MCP tool")
                if tool["name"] in {"search_places", "compute_routes"}:
                    if tool["name"] in schemas:
                        raise ValueError("Duplicate MCP tool")
                    schema = tool.get("inputSchema")
                    if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
                        raise ValueError("Invalid MCP schema")
                    schemas[tool["name"]] = schema
            if set(schemas) != {"search_places", "compute_routes"}:
                raise ValueError("Required tools missing")
            self._schemas = schemas
        except GoogleError:
            self._schemas, self._session, self._version = None, None, None
            raise
        except (ValueError, TypeError):
            self._schemas, self._session, self._version = None, None, None
            raise GoogleError("google_protocol_error", "Не удалось согласовать инструменты Google MCP.", 502) from None

    def _arguments(self, tool, aliases, values):
        self._initialize()
        schema = self._schemas[tool]
        properties, result = schema["properties"], {}
        for names, value in zip(aliases, values):
            matches = [name for name in names if name in properties]
            if len(matches) != 1:
                raise GoogleError("google_protocol_error", "Схема Google MCP несовместима с клиентом.", 502)
            result[matches[0]] = value
        if not isinstance(schema.get("required", []), list) or not set(schema.get("required", [])) <= set(result):
            raise GoogleError("google_protocol_error", "Схема Google MCP требует неподдержанные поля.", 502)
        return result

    def _tool(self, name, arguments):
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}, "mcp." + name)
        if result.get("isError") is True:
            raise GoogleError("google_tool_failed", "Инструмент Google не вернул пригодные данные.", 502)
        value = result.get("structuredContent")
        if value is None:
            content = result.get("content")
            if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get("type") != "text":
                raise GoogleError("google_protocol_error", "Нет структурированного результата Google.", 502)
            try:
                value = strict_json(content[0]["text"])
            except (KeyError, ValueError, TypeError, RecursionError):
                raise GoogleError("google_protocol_error", "Некорректный результат инструмента Google.", 502) from None
        if not isinstance(value, dict):
            raise GoogleError("google_protocol_error", "Некорректный результат инструмента Google.", 502)
        return value

    def search_places(self, parameters: dict):
        # These exact canonical parameters are shown and signed at preview.
        expected = {"textQuery", "languageCode", "regionCode", "locationBias", "pageSize", "includeExtendedDetails"}
        try:
            if not isinstance(parameters, dict) or set(parameters) != expected:
                raise ValueError("Missing agreed search parameters")
            if not isinstance(parameters["textQuery"], str) or not 1 <= len(parameters["textQuery"]) <= 2000 or parameters["languageCode"] != "ru" or parameters["regionCode"] != "KZ" or type(parameters["pageSize"]) is not int or parameters["pageSize"] != 5 or parameters["includeExtendedDetails"] is not False:
                raise ValueError("Invalid search parameters")
            bias = parameters["locationBias"]
            if not isinstance(bias, dict) or set(bias) != {"circle"} or not isinstance(bias["circle"], dict) or set(bias["circle"]) != {"center", "radiusMeters"}:
                raise ValueError("Invalid public bias")
            circle = bias["circle"]
            point = circle["center"]
            if not isinstance(point, dict) or set(point) != {"latitude", "longitude"} or any(type(point[key]) not in {int, float} or not math.isfinite(point[key]) or not low <= point[key] <= high for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180))) or type(circle["radiusMeters"]) is not int or not 100 <= circle["radiusMeters"] <= 2000:
                raise ValueError("Invalid public circle")
        except (KeyError, TypeError, ValueError, OverflowError):
            raise GoogleError("invalid_request", "Поисковые параметры Google не прошли проверку.", 400) from None
        with self._lock:
            # If a future schema changes field names, fail before tools/call
            # instead of sending parameters different from the consent preview.
            arguments = self._arguments("search_places", [(key,) for key in parameters], list(parameters.values()))
            return self._tool("search_places", arguments)

    def compute_route(self, origin: dict, destination: dict):
        with self._lock:
            self._initialize()
            schema = self._schemas["compute_routes"]["properties"]
            # Nested waypoint fields follow the declared schema, never paid probes.
            def waypoint(field, value):
                properties = schema.get(field, {}).get("properties", {})
                if not properties and "$ref" in schema.get(field, {}):
                    ref = schema[field]["$ref"]
                    definitions = self._schemas["compute_routes"].get("$defs", self._schemas["compute_routes"].get("definitions", {}))
                    properties = definitions.get(ref.rsplit("/", 1)[-1], {}).get("properties", {})
                if "address" in value and "address" in properties:
                    return {"address": value["address"]}
                if "id" in value:
                    names = [key for key in ("placeId", "place_id") if key in properties]
                    if len(names) == 1:
                        return {names[0]: value["id"]}
                raise GoogleError("google_protocol_error", "Неподдержанная схема Google waypoint.", 502)
            arguments = self._arguments("compute_routes", [("origin",), ("destination",), ("travelMode", "travel_mode")],
                [waypoint("origin", origin), waypoint("destination", destination), "WALK"])
            return self._tool("compute_routes", arguments)

    def details(self, place_id: str, profile: str):
        if not valid_place_id(place_id) or profile not in DETAIL_MASKS:
            raise GoogleError("invalid_request", "Некорректное место или профиль Google.", 400)
        try:
            with self._http("GET", PLACES_URL + quote(place_id, safe="") + "?languageCode=ru", "places." + profile,
                            extra={"X-Goog-FieldMask": DETAIL_MASKS[profile], "Accept": "application/json"}) as response:
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("Invalid Places content type")
                data = self._bounded_json(response)
            allowed = set(DETAIL_MASKS[profile].split(","))
            if not isinstance(data, dict) or data.get("id") != place_id or not set(data) <= allowed:
                raise ValueError("Invalid Places identity/fields")
            if "reviews" in data and (not isinstance(data["reviews"], list) or len(data["reviews"]) > 5):
                raise ValueError("Invalid Places reviews")
            return {"source": "google_places", "place_id": place_id, "profile": profile,
                    "fetched_at": utc_now(), "data": data, "quota": self.budget.status(),
                    "warnings": ["Отзывы отражают мнения авторов; розетки, тишина и доступность не подтверждаются одним отзывом."]}
        except GoogleError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise GoogleError("google_protocol_error", "Ответ Places API не прошёл проверку.", 502) from None
