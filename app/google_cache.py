"""Permitted, local-only Google pilot storage; never a retrieval/response cache.

The operator supplies an existing absolute ignored runtime directory. Only
Places New IDs and time-limited Places coordinates, plus immutable accepted
whole Qwen grounded output with its displayed source previews, are retained.
Grounded records are solely for performance/display evaluation, never for
training, model/AI development or improvement, production reuse, POI extraction,
or future answers. There is deliberately no content-reading/retrieval API.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic, sleep

from .google_client import json_bytes, maps_url, strict_json, valid_place_id

TTL = timedelta(days=30)
RETENTION = timedelta(days=29)  # One-day headroom for hourly runtime cleanup.
PURPOSE = "performance_and_display_evaluation_only"
_MAX_BYTES = 32 * 1024 * 1024
_DETAIL_FIELDS = frozenset({
    "id", "location", "displayName", "formattedAddress", "businessStatus",
    "currentOpeningHours", "regularOpeningHours", "utcOffsetMinutes",
    "googleMapsUri", "attributions", "rating", "userRatingCount", "reviews",
})
_THREAD_GUARD = Lock()
_THREAD_LOCKS = {}


class GoogleCacheError(Exception):
    """Sanitized optional-storage failure; callers retain their live answer."""
    code = "google_cache_unavailable"

    def __init__(self):
        super().__init__("Разрешённый архив Google недоступен; живой ответ не изменён.")


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return result.astimezone(timezone.utc)


def _safe(path, *, directory=False, missing=False):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item == path and missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Unsafe runtime path")
    if path.exists():
        info = path.stat()
        if directory:
            if not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o022):
                raise ValueError("Directory required")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("Private regular file required")


def _checked_open(path, flags, mode=0o600):
    _safe(path, missing=bool(flags & os.O_CREAT))
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("Unsafe file")
        _safe(path)
        current = path.stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("File changed")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _sources(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("Invalid source previews")
    result, ids, source_ids = [], set(), set()
    for value in values:
        if not isinstance(value, dict) or not valid_place_id(value.get("id")):
            raise ValueError("Invalid source ID")
        source_id = value.get("source_id")
        if not isinstance(source_id, str) or not re.fullmatch(r"g\d{1,3}", source_id) or source_id in source_ids or value["id"] in ids:
            raise ValueError("Invalid preview ID")
        title = value.get("title")
        attribution = value.get("attribution")
        if title is None and isinstance(attribution, dict):
            title = attribution.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 8192):
            raise ValueError("Invalid preview title")
        url = value.get("url")
        if url is None and isinstance(value.get("googleMapsLinks"), dict):
            url = value["googleMapsLinks"].get("placeUrl")
        if not maps_url(url):
            raise ValueError("Invalid preview URL")
        # These are the exact title, link and attribution actually displayed.
        # location/place/resource URLs, summary and other metadata are dropped.
        preview = {"id": value["id"], "source_id": source_id, "title": title, "url": url}
        if attribution is not None:
            if not isinstance(attribution, dict) or not isinstance(attribution.get("title"), str) or len(attribution["title"]) > 8192 or not maps_url(attribution.get("url")):
                raise ValueError("Invalid preview attribution")
            preview["attribution"] = {"title": attribution["title"], "url": attribution["url"]}
        result.append(preview)
        ids.add(value["id"])
        source_ids.add(source_id)
    return result


def _grounded(answer, sources):
    if not isinstance(answer, dict) or set(answer) != {"text", "selected_place_ids", "source_ids"} or not isinstance(answer["text"], str) or not answer["text"].strip() or len(answer["text"]) > 24000:
        raise ValueError("Only an accepted whole grounded answer is permitted")
    previews = _sources(sources)
    place_ids, source_ids = {item["id"] for item in previews}, {item["source_id"] for item in previews}
    for key, allowed in (("selected_place_ids", place_ids), ("source_ids", source_ids)):
        values = answer[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 32 or any(not isinstance(value, str) or value not in allowed for value in values) or len(set(values)) != len(values):
            raise ValueError("Unassociated grounded output")
    for selected in answer["selected_place_ids"]:
        if not any(source["id"] == selected and source["source_id"] in answer["source_ids"] for source in previews):
            raise ValueError("Missing selected source citation")
    # Preserve text and array order verbatim; do not strip or regenerate prose.
    payload = {"answer": {"text": answer["text"], "selected_place_ids": list(answer["selected_place_ids"]),
                          "source_ids": list(answer["source_ids"])}, "sources": previews}
    return hashlib.sha256(json_bytes(payload)).hexdigest(), payload


class GoogleCache:
    def __init__(self, directory, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._worker_guard = Lock()
        self._stop = Event()
        self._worker = None
        self.cleanup_error = None
        try:
            self.directory = Path(directory)
            if not self.directory.is_absolute():
                raise ValueError("Absolute runtime directory required")
            _safe(self.directory, directory=True)
            if not self.directory.is_dir():
                raise ValueError("Existing runtime directory required")
            info = self.directory.stat()
            self.identity = (info.st_dev, info.st_ino)
            self.path = self.directory / "cache.json"
            self.lock_path = self.directory / "cache.lock"
            with _THREAD_GUARD:
                self._thread_lock = _THREAD_LOCKS.setdefault(str(self.directory.resolve()), RLock())
            self.prune()
        except GoogleCacheError:
            raise
        except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
            raise GoogleCacheError() from None

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Aware clock required")
        return now.astimezone(timezone.utc)

    def _directory(self):
        _safe(self.directory, directory=True)
        info = self.directory.stat()
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError("Runtime directory changed")

    @contextmanager
    def _locked(self):
        self._directory()
        if not self._thread_lock.acquire(timeout=5):
            raise ValueError("Storage busy")
        handle = None
        acquired = False
        try:
            fd = _checked_open(self.lock_path, os.O_CREAT | os.O_RDWR)
            handle = os.fdopen(fd, "r+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = monotonic() + 5
            while True:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if monotonic() >= deadline:
                        raise ValueError("Storage busy")
                    sleep(0.01)
            self._directory()
            yield
        finally:
            try:
                if handle is not None and acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                if handle is not None:
                    handle.close()
                self._thread_lock.release()

    def _read(self, now):
        if not self.path.exists():
            # A dangling symlink must not be treated as a new cache.
            _safe(self.path, missing=True)
            return {"version": 1, "places": {}, "grounded": {}}
        with os.fdopen(_checked_open(self.path, os.O_RDONLY), "rb") as source:
            raw = source.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("Oversized storage")
        value = strict_json(raw)
        if not isinstance(value, dict) or set(value) != {"version", "places", "grounded"} or type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("Invalid storage schema")
        if not isinstance(value["places"], dict) or len(value["places"]) > 10000 or not isinstance(value["grounded"], dict) or len(value["grounded"]) > 512:
            raise ValueError("Invalid records")
        for place_id, record in value["places"].items():
            if not valid_place_id(place_id) or not isinstance(record, dict) or set(record) not in ({"first_seen", "last_seen"}, {"first_seen", "last_seen", "location"}):
                raise ValueError("Invalid ID registry")
            if not _time(record["first_seen"]) <= _time(record["last_seen"]) <= now:
                raise ValueError("Invalid ID timestamps")
            if "location" in record:
                location = record["location"]
                if not isinstance(location, dict) or set(location) != {"latitude", "longitude", "obtained_at", "expires_at", "provenance"} or location["provenance"] != "places_api_new":
                    raise ValueError("Invalid location provenance")
                self._location(location)
                created, expires = _time(location["obtained_at"]), _time(location["expires_at"])
                if created > now or not timedelta(0) < expires - created <= TTL:
                    raise ValueError("Invalid location TTL")
        for record_id, record in value["grounded"].items():
            if not isinstance(record, dict) or set(record) != {"created_at", "expires_at", "purpose", "answer", "sources"} or record["purpose"] != PURPOSE:
                raise ValueError("Invalid grounded record")
            digest, payload = _grounded(record["answer"], record["sources"])
            if digest != record_id or payload["sources"] != record["sources"]:
                raise ValueError("Grounded content changed")
            created, expires = _time(record["created_at"]), _time(record["expires_at"])
            if created > now or not timedelta(0) < expires - created <= TTL:
                raise ValueError("Invalid grounded TTL")
        return value

    @staticmethod
    def _location(value):
        for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
            if type(value.get(key)) not in {int, float} or not math.isfinite(value[key]) or not low <= value[key] <= high:
                raise ValueError("Invalid Places coordinate")

    def _write(self, value):
        self._directory()
        raw = json_bytes(value)
        if len(raw) > _MAX_BYTES:
            raise ValueError("Oversized storage")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".cache-", suffix=".tmp", dir=self.directory, delete=False) as output:
                temporary = Path(output.name)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            self._directory()
            _safe(self.path, missing=True)
            os.replace(temporary, self.path)
            temporary = None
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _cleanup_temps(self):
        # Same cross-process lock as writers: no live writer owns one of these.
        # A hard-crash orphan can contain grounded text, so remove it eagerly.
        self._directory()
        removed = 0
        for index, candidate in enumerate(self.directory.iterdir()):
            if index >= 2048:
                raise ValueError("Unexpected runtime directory size")
            if re.fullmatch(r"\.cache-[a-z0-9_]{8}\.tmp", candidate.name):
                _safe(candidate)  # Never follow/delete links or unrelated files.
                candidate.unlink()
                removed += 1
        return removed
    def _operation(self, mutate=None):
        try:
            with self._locked():
                now = self._now()
                removed_orphans = self._cleanup_temps()
                value = self._read(now)
                removed = 0
                for record in value["places"].values():
                    if "location" in record and min(_time(record["location"]["expires_at"]), _time(record["location"]["obtained_at"]) + RETENTION) <= now:
                        del record["location"]
                        removed += 1
                for record_id in list(value["grounded"]):
                    if min(_time(value["grounded"][record_id]["expires_at"]), _time(value["grounded"][record_id]["created_at"]) + RETENTION) <= now:
                        del value["grounded"][record_id]
                        removed += 1
                # Physically prune even if the incoming record later fails.
                if removed:
                    self._write(value)
                stored = mutate(value, now) if mutate else False
                if stored:
                    if len(value["places"]) > 10000 or len(value["grounded"]) > 512:
                        raise ValueError("Storage full")
                    self._write(value)
                return {"state": "ready", "places_count": len(value["places"]),
                        "grounded_count": len(value["grounded"]), "removed": removed,
                        "removed_orphans": removed_orphans,
                        "stored": bool(stored)}
        except GoogleCacheError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError, RuntimeError, KeyError):
            raise GoogleCacheError() from None

    def prune(self):
        """Physically remove expired coordinates/outputs, keeping Places IDs."""
        return self._operation()

    def record_places(self, data):
        """Only validated Places New details enter this provenance-specific path."""
        def mutate(value, now):
            if not isinstance(data, dict) or not set(data) <= _DETAIL_FIELDS or not valid_place_id(data.get("id")):
                raise ValueError("Only validated Places details allowed")
            location = data.get("location")
            if location is not None:
                if not isinstance(location, dict) or set(location) != {"latitude", "longitude"}:
                    raise ValueError("Invalid Places location")
                self._location(location)
            stamp = _stamp(now)
            record = value["places"].setdefault(data["id"], {"first_seen": stamp, "last_seen": stamp})
            record["last_seen"] = stamp
            if location is not None:
                record["location"] = {"latitude": location["latitude"], "longitude": location["longitude"],
                                      "obtained_at": stamp, "expires_at": _stamp(now + RETENTION),
                                      "provenance": "places_api_new"}
            return True
        return self._operation(mutate)

    def record_grounded(self, answer, sources):
        """Store a whole accepted output once; repeats never renew its TTL."""
        def mutate(value, now):
            record_id, payload = _grounded(answer, sources)
            if record_id in value["grounded"]:
                return False
            value["grounded"][record_id] = {"created_at": _stamp(now), "expires_at": _stamp(now + RETENTION),
                                          "purpose": PURPOSE, **payload}
            return True
        return self._operation(mutate)

    def start_cleanup(self, *, interval_seconds=3600):
        """Operator-wired local housekeeping; no network or content retrieval.

        29-day expiry plus at most one hour of cleanup latency stays below the
        30-day retention ceiling while the app is running. Startup always prunes.
        """
        if type(interval_seconds) not in {int, float} or not 0 < interval_seconds <= 3600:
            raise GoogleCacheError()
        with self._worker_guard:
            if self._worker is not None and self._worker.is_alive():
                return {"cleanup_running": True}
            self._stop.clear()
            def clean():
                while not self._stop.wait(interval_seconds):
                    try:
                        self.prune()
                        self.cleanup_error = None
                    except GoogleCacheError:
                        self.cleanup_error = GoogleCacheError.code
            self._worker = Thread(target=clean, name="google-permitted-cache-cleanup", daemon=True)
            self._worker.start()
        return {"cleanup_running": True}

    def close(self):
        """Stop runtime housekeeping without any final content use or API call."""
        self._stop.set()
        worker = self._worker
        if worker is not None and worker is not current_thread():
            worker.join(timeout=6)
    def status(self):
        """Only availability metadata; never returns stored Google content."""
        try:
            self.prune()
            self.cleanup_error = None
            return {"enabled": True, "error": None}
        except GoogleCacheError:
            return {"enabled": True, "error": GoogleCacheError.code}