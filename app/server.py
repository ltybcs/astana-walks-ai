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
from .llm import capabilities
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
        if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
            raise ValueError(f"origin.{name}: некорректная координата.")
    label = origin.get("label", "Отправная точка")
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError("origin.label: текст до 200 символов.")
    time_of_day = payload.get("time_of_day", "day")
    if time_of_day not in ("day", "evening", "night"):
        raise ValueError("time_of_day: day, evening или night.")
    return {"query": query.strip(), "minutes": minutes, "origin": {"lat": origin["lat"], "lon": origin["lon"], "label": label}, "time_of_day": time_of_day}


class Handler(SimpleHTTPRequestHandler):
    server_version = "AstanaWalks/1.0"
    sys_version = ""

    def __init__(self, *args, retriever: Retriever, **kwargs):
        self.retriever = retriever
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Do not retain coordinates, queries, Referer or URLs in request logs.
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Permissions-Policy", "camera=(), microphone=()")
        super().end_headers()

    def _json(self, value: dict, status: int = 200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        # Reject browser cross-origin requests without enabling CORS.
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            llm = capabilities()
            self._json({"status": "ok", "api_version": 1, "mode": "extractive/demo" if self.retriever.dataset.get("is_demo") else "extractive", "places_count": len(self.retriever.places), "llm_configured": llm["configured"], "llm": llm, "dataset": self.retriever.dataset})
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
        path = self.path.split("?", 1)[0]
        target = static_target(path)
        if target is None:
            self.send_error(404)
            return
        self.path = target
        super().do_HEAD()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/recommend":
            self._json({"error": "Маршрут API не найден."}, 404)
            return
        if not self._same_origin():
            self._json({"error": "Запрос из другого источника отклонён."}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "Нужен Content-Type: application/json."}, 415)
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            self._json({"error": "Нужен один Content-Length; Transfer-Encoding не поддерживается."}, 400)
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
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Тело запроса получено не полностью.")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Повторяющийся ключ JSON.")
                    result[key] = value
                return result
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Нечисловая константа.")))
            validated = validate_request(payload)
        except TimeoutError:
            self._json({"error": "Превышено время получения запроса."}, 408)
            return
        except (ValueError, UnicodeError, RecursionError) as error:
            self._json({"error": str(error)}, 400)
            return
        try:
            response = recommend(self.retriever, **validated)
        except Exception:
            # A corrupted optional provider or unexpected runtime failure must
            # not expose prompts, coordinates, keys or internal file paths.
            self._json({"error": "Не удалось составить рекомендации. Повторите запрос позже.", "code": "recommendation_failed"}, 500)
            return
        self._json(response)


def create_server(host: str = "127.0.0.1", port: int = 8000, *, retriever: Retriever | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), partial(Handler, retriever=retriever or Retriever()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Local-only by default")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", type=Path, default=os.getenv("POI_DATA_PATH"))
    args = parser.parse_args()
    try:
        retriever = Retriever(args.data)
        server = create_server(args.host, args.port, retriever=retriever)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Не удалось запустить сервер: {error}\n")
    print(f"Astana Walks: http://{args.host}:{args.port} — {len(retriever.places)} POI; Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
