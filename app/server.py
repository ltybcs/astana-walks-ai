"""Local-only HTTP server. Run from the project root: python -m app.server."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
from urllib.parse import urlparse

from .generation import recommend
from .retrieval import ROOT, Retriever


MAX_REQUEST_BYTES = 16_384
STATIC_FILES = {"/": "/index.html", "/index.html": "/index.html", "/app.js": "/app.js", "/styles.css": "/styles.css", "/favicon.svg": "/favicon.svg"}


def static_target(path: str) -> str | None:
    target = STATIC_FILES.get(path)
    if target is None:
        return None
    web_root = (ROOT / "web").resolve()
    resolved = (web_root / target.lstrip("/")).resolve()
    if not resolved.is_relative_to(web_root):
        return None
    return target


def validate_request(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ожидается JSON-объект.")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("query: непустой текст до 2000 символов.")
    minutes = payload.get("minutes")
    if type(minutes) is not int or not 10 <= minutes <= 240:
        raise ValueError("minutes: целое число от 10 до 240.")
    origin = payload.get("origin")
    if not isinstance(origin, dict):
        raise ValueError("origin: нужны числовые lat и lon.")
    for name, low, high in (("lat", -90, 90), ("lon", -180, 180)):
        value = origin.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"origin.{name}: некорректная координата.")
    label = origin.get("label", "Отправная точка")
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError("origin.label: текст до 200 символов.")
    time_of_day = payload.get("time_of_day", "day")
    if time_of_day not in ("day", "evening", "night"):
        raise ValueError("time_of_day: day, evening или night.")
    return {"query": query.strip(), "minutes": minutes, "origin": {"lat": origin["lat"], "lon": origin["lon"], "label": label}, "time_of_day": time_of_day}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, retriever: Retriever, **kwargs):
        self.retriever = retriever
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, fmt, *args):
        # Do not retain coordinates, queries, Referer or URLs in request logs.
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, value: dict, status: int = 200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        # Reject browser cross-origin requests without enabling CORS.
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({"status": "ok", "mode": "extractive/demo" if self.retriever.dataset.get("is_demo") else "extractive", "places_count": len(self.retriever.places), "llm_configured": bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL")), "dataset": self.retriever.dataset})
        elif path == "/api/places":
            self._json({"places": self.retriever.places, "dataset": self.retriever.dataset})
        elif path.startswith("/api/"):
            self._json({"error": "Маршрут API не найден."}, 404)
        else:
            # Never expose the repository, datasets, .env or Python sources.
            target = static_target(path)
            if target is None:
                self._json({"error": "Файл не найден."}, 404)
                return
            self.path = target
            super().do_GET()

    def do_HEAD(self):
        # Use the same static allowlist as GET; do not permit directory listings.
        path = urlparse(self.path).path
        target = static_target(path)
        if target is None:
            self.send_error(404)
            return
        self.path = target
        super().do_HEAD()

    def do_POST(self):
        if urlparse(self.path).path != "/api/recommend":
            self._json({"error": "Маршрут API не найден."}, 404)
            return
        if not self._same_origin():
            self._json({"error": "Запрос из другого источника отклонён."}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "Нужен Content-Type: application/json."}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "Некорректный Content-Length."}, 400)
            return
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._json({"error": "Пустой или слишком большой запрос."}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Нечисловая константа.")))
            validated = validate_request(payload)
        except (ValueError, UnicodeError, RecursionError) as error:
            self._json({"error": str(error)}, 400)
            return
        self._json(recommend(self.retriever, **validated))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Local-only by default")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", type=Path, default=os.getenv("POI_DATA_PATH"))
    args = parser.parse_args()
    retriever = Retriever(args.data)
    handler = partial(Handler, retriever=retriever)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Astana Walks: http://{args.host}:{args.port} — {len(retriever.places)} POI; Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
