"""One user-initiated, transient official 2GIS search; never a local POI index.

The public seam accepts a category only. It sends neither a user's free text nor
their coordinates. HTTP handlers must return all responses with private/no-store.
Only a rolling attempt counter is persisted, under ignored .local/2gis/.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.collect_2gis import CollectionError, normalize_items, response_items

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://catalog.api.2gis.com/3.0/items"
ASTANA_BBOX = (51.00, 71.20, 51.32, 71.70)
CATEGORIES = {"cafe": "Кофейни", "park": "Парки", "museum": "Музеи",
              "attraction": "Достопримечательности", "playground": "Детские площадки",
              "mall": "Торговые центры", "theatre": "Театры"}
FIELDS = "items.point,items.rubrics"
PAGE_SIZE = 10  # Demo-key maximum; never silently request the paid-key limit.
ATTRIBUTION = {"text": "2ГИС", "url": "http://dev.2gis.ru"}
LIMIT = 500
WINDOW_SECONDS = 30 * 86_400
TIMEOUT_SECONDS = 25
MAX_BYTES = 5_000_000
_PROCESS_LOCK = threading.Lock()


class DGISLiveError(ValueError):
    """Only static, safe text and numeric quota metadata may leave this module."""
    def __init__(self, message, *, code, http_status, quota=None, provider_code=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.quota = quota
        self.provider_code = provider_code

    def as_dict(self):
        return {"error": str(self), "code": self.code, "quota": self.quota}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DGISLiveError("2ГИС вернул перенаправление; запрос остановлен.",
                            code="provider_redirect", http_status=502)


def _state_error():
    return DGISLiveError("Не удалось безопасно проверить лимит 2ГИС.",
                         code="quota_state_unavailable", http_status=503)


def _busy_error():
    return DGISLiveError("Поиск 2ГИС уже выполняется; дождитесь завершения.",
                         code="busy", http_status=409)


class _LiveClient:
    """The root/clock parameters are an internal offline-test seam, never HTTP input."""
    def __init__(self, root=ROOT, clock=time.time):
        self.root = Path(root).resolve()
        self.directory = self.root / ".local" / "2gis"
        self.clock = clock

    def _path(self, name):
        directory = self.directory.resolve()
        candidate = directory / name
        if not directory.is_relative_to(self.root) or candidate.resolve().parent != directory:
            raise _state_error()
        return candidate

    def _key(self):
        value = os.environ.get("DGIS_API_KEY", "").strip()
        if not value:
            try:
                path = self._path("api-key")
                if not path.is_file() or path.stat().st_size > 2048:
                    return None
                value = path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeError, DGISLiveError):
                return None
        if not value or len(value) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            return None
        return value

    @contextmanager
    def _locked(self):
        # The OS lock uses a stable separate inode, never the replaced JSON file.
        if not _PROCESS_LOCK.acquire(blocking=False):
            raise _busy_error()
        handle = None
        locked = False
        try:
            try:
                lock_path = self._path("usage.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+b")
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
            except OSError:
                raise _state_error() from None
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise _busy_error() from None
            yield
        finally:
            if handle:
                try:
                    if locked:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass  # Closing the descriptor also releases its OS lock.
                finally:
                    try:
                        handle.close()
                    except OSError:
                        pass
            _PROCESS_LOCK.release()

    def _write_attempts(self, attempts):
        temporary = None
        try:
            path = self._path("usage.json")
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                              prefix=".usage-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                raw = {"schema_version": 1, "attempts": attempts}
                stream.write((json.dumps(raw, allow_nan=False) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except (OSError, ValueError, TypeError):
            raise _state_error() from None
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _attempts(self, now):
        try:
            path = self._path("usage.json")
            if not path.exists():
                attempts = []
                self._write_attempts(attempts)
                return attempts
            if path.stat().st_size > 64_000:
                raise _state_error()
            raw = json.loads(path.read_bytes())
            attempts = raw.get("attempts") if isinstance(raw, dict) else None
            if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1 or not isinstance(attempts, list):
                raise _state_error()
            if any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in attempts):
                raise _state_error()
            # Future timestamps remain charged if the clock moves backwards.
            return [t for t in attempts if t > now - WINDOW_SECONDS]
        except (OSError, ValueError, TypeError, AttributeError):
            raise _state_error() from None

    @staticmethod
    def _quota(attempts, now):
        used = len(attempts)
        retry = max(1, math.ceil(min(attempts) + WINDOW_SECONDS - now)) if used >= LIMIT else 0
        return {"limit": LIMIT, "used": used, "remaining": max(0, LIMIT - used),
                "window_seconds": WINDOW_SECONDS, "retry_after_seconds": retry}

    def status(self):
        configured = bool(self._key())
        if not configured:
            return {"configured": False, "available": False, "quota": None, "code": "not_configured"}
        try:
            with self._locked():
                now = self.clock()
                quota = self._quota(self._attempts(now), now)
            return {"configured": True, "available": quota["remaining"] > 0, "quota": quota,
                    "code": "ready" if quota["remaining"] else "quota_exhausted"}
        except DGISLiveError as error:
            return {"configured": True, "available": False, "quota": None, "code": error.code}

    def fetch(self, category):
        if not isinstance(category, str) or category not in CATEGORIES:
            raise DGISLiveError("Выберите одну из разрешённых категорий 2ГИС.",
                                code="invalid_category", http_status=400)
        key = self._key()
        if not key:
            raise DGISLiveError("Живой поиск 2ГИС не настроен на сервере.",
                                code="not_configured", http_status=503)
        with self._locked():
            now = self.clock()
            attempts = self._attempts(now)
            quota = self._quota(attempts, now)
            if quota["remaining"] == 0:
                raise DGISLiveError("Лимит 500 попыток 2ГИС за последние 30 дней исчерпан.",
                                    code="quota_exhausted", http_status=429, quota=quota)
            attempts.append(now)
            self._write_attempts(attempts)  # Reserve durably before any network call.
            quota = self._quota(attempts, now)
            south, west, north, east = ASTANA_BBOX
            params = {"key": key, "q": CATEGORIES[category], "point1": f"{west},{north}",
                      "point2": f"{east},{south}", "page": 1, "page_size": PAGE_SIZE, "fields": FIELDS}
            request = Request(ENDPOINT + "?" + urlencode(params),
                              headers={"Accept": "application/json", "User-Agent": "AstanaWalks/1.0 (live user search)"})
            try:
                with build_opener(_NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise DGISLiveError("Ответ 2ГИС превышает допустимый размер.",
                                        code="provider_response_too_large", http_status=502)
                payload = json.loads(raw)
                items = response_items(payload)
                stamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
                places = normalize_items(items[:PAGE_SIZE], stamp, ASTANA_BBOX)
            except DGISLiveError as error:
                error.quota = quota
                raise
            except HTTPError as error:
                code = error.code if type(error.code) is int else None
                error.close()
                raise DGISLiveError("2ГИС отклонил запрос; проверьте доступ и лимит сервиса.",
                                    code="provider_http_error", http_status=502, quota=quota,
                                    provider_code=code) from None
            except TimeoutError:
                raise DGISLiveError("Истекло время ожидания 2ГИС.", code="provider_timeout",
                                    http_status=504, quota=quota) from None
            except URLError as error:
                timeout = isinstance(error.reason, TimeoutError)
                raise DGISLiveError("Истекло время ожидания 2ГИС." if timeout else "2ГИС недоступен: сетевая ошибка.",
                                    code="provider_timeout" if timeout else "provider_unavailable",
                                    http_status=504 if timeout else 502, quota=quota) from None
            except OSError:
                raise DGISLiveError("2ГИС недоступен: сетевая ошибка.", code="provider_unavailable",
                                    http_status=502, quota=quota) from None
            except (CollectionError, ValueError, UnicodeError, TypeError, OverflowError, RecursionError):
                code = None
                if isinstance(locals().get("payload"), dict):
                    meta = payload.get("meta")
                    value = meta.get("code") if isinstance(meta, dict) else None
                    code = value if type(value) is int else None
                raise DGISLiveError("2ГИС не подтвердил корректный ответ поиска.",
                                    code="provider_invalid_response", http_status=502,
                                    quota=quota, provider_code=code) from None
            return {"places": places, "attribution": dict(ATTRIBUTION), "quota": quota,
                    "category": category, "fetched_at": stamp,
                    "warnings": ["Живой поиск 2ГИС: одна страница до 10 объектов, не полный каталог.",
                                 "Сведения не проверены на месте; неизвестные часы и свойства не являются подтверждением."]}


def fetch_live(category):
    """Perform one fixed-category search. Never call this automatically or in a loop."""
    return _LiveClient().fetch(category)


def service_status():
    """Safe local-only configuration/quota status; no provider calls or new attempts."""
    return _LiveClient().status()
