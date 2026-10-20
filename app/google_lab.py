"""Isolated Google-embed lab backed only by the shipped OSM snapshot.

Run python -m app.google_lab and open http://127.0.0.1:8003.
No model/provider client, refresh worker or main server is imported.
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

ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_BYTES = 25_000_000
MAX_STATIC_BYTES = 8_000_000
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


class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path):
        self.root = Path(root).resolve()
        self.catalog = load_catalog(self.root)
        self.catalog_json = _json_bytes(self.catalog)
        self.health_json = _json_bytes({
            "status": "ok", "service": "google-maps-lab", "lab": True,
            "free": True, "mode": "catalog-only", "provider_calls": 0,
            "backend_outbound_calls": False,
            "source_count": self.catalog["summary"]["source_count"],
            "places_count": self.catalog["summary"]["visible_count"],
            "data_source": "Shipped OSM snapshot", "google_embed": "browser-only",
        })
        super().__init__(address, LabHandler)


class LabHandler(BaseHTTPRequestHandler):
    server: LabServer
    server_version = "AstanaWalksLab/1.0"
    sys_version = ""

    def log_message(self, _format, *args):
        # No request URLs, raw user input, origin or client addresses are logged.
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", *, head: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
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
            self._send(403, _json_bytes({"error": "Local lab requests only"}), head=head)
            return
        if self.path == "/api/catalog":
            self._send(200, self.server.catalog_json, head=head)
        elif self.path == "/api/health":
            self._send(200, self.server.health_json, head=head)
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
        self._send(405, _json_bytes({"error": "Lab has no provider or mutation endpoints"}))

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_OPTIONS = do_POST


def create_server(host: str = "127.0.0.1", port: int = 8003, *, root: Path = ROOT) -> LabServer:
    """Create an owned context-managed server; the caller owns its lifecycle."""
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Lab binds only to IPv4 loopback") from error
    if address.version != 4 or not address.is_loopback or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Lab binds only to IPv4 loopback and a valid port")
    return LabServer((host, port), Path(root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args(argv)
    try:
        with create_server(args.host, args.port) as server:
            print(f"Google Maps lab: http://127.0.0.1:{server.server_port} — {server.catalog['summary']['visible_count']} OSM places", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
    except (ValueError, OSError) as error:
        parser.exit(1, f"Lab startup failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
