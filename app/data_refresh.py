"""Opt-in, daily, local POI cache refresh. Never runs from a public HTTP route."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import hashlib
import math
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import time
from urllib.error import HTTPError, URLError

from scripts.collect_osm import ASTANA_BBOX, DEFAULT_ENDPOINT, QUERY_PROFILE, atomic_write, build_query, collect_response, make_document, parse_json, write_collection
from .retrieval import ROOT, Retriever

TTL_SECONDS = 24 * 60 * 60
COOLDOWN_SECONDS = 60 * 60


def timestamp(value) -> float:
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0.0
    except (ValueError, OverflowError, OSError):
        return 0.0


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value else None


class DatasetStore:
    """A completed immutable index is captured once by each HTTP handler."""
    def __init__(self, retriever: Retriever):
        self._lock = Lock()
        self._current = retriever
        self._source = "shipped"

    def snapshot(self) -> Retriever:
        with self._lock:
            return self._current

    def source(self) -> str:
        with self._lock:
            return self._source

    def publish(self, retriever: Retriever, source="cache") -> None:
        with self._lock:
            self._current, self._source = retriever, source


@contextmanager
def process_lock(path):
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class POIRefresher:
    def __init__(self, store: DatasetStore, cache_dir=None, *, fetcher=collect_response, clock=time):
        self.store = store
        self.cache_dir = Path(cache_dir or ROOT / ".local/poi").resolve()
        self.fetcher, self.clock = fetcher, clock
        self._flight, self._status_lock = Lock(), Lock()
        self._stop = Event()
        self._thread = None
        self._state = {}
        self._phase = "fresh"
        self._last_error = None
        self._cache_mtime = None

    def path(self, name):
        target = self.cache_dir / name
        if target.is_symlink() or not target.resolve().is_relative_to(self.cache_dir):
            raise ValueError("Unsafe refresh cache path")
        return target

    def _read_state(self):
        path = self.path("refresh-state.json")
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_bytes())
            if not isinstance(data, dict):
                raise ValueError()
            for key in ("last_attempt", "last_success"):
                if key in data and (timestamp(data[key]) == 0 or timestamp(data[key]) > self.clock() + 300):
                    raise ValueError()
            error = data.get("last_error")
            allowed = {"invalid_refresh_state", "invalid_cache", "network_timeout", "network_error", "invalid_response", "storage_or_network_error"}
            if error is not None and error not in allowed and not (isinstance(error, str) and len(error) == 8 and error.startswith("http_") and error[5:].isdigit()):
                raise ValueError()
            return data
        except (ValueError, OSError):
            # A broken cooldown file must not cause a network burst on restart.
            self._last_error = "invalid_refresh_state"
            repaired = {"last_attempt": self.clock(), "last_error": "invalid_refresh_state"}
            # Called under process_lock: persist once so subsequent polls do
            # not move the retry deadline forward forever.
            self._write_state(repaired)
            return repaired

    def _write_state(self, state):
        atomic_write(self.path("refresh-state.json"), json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        with self._status_lock:
            self._state = state

    def load_cache(self):
        try:
            path = self.path("poi.astana.json")
            if not path.exists():
                return False
            modified = path.stat().st_mtime_ns
            if modified == self._cache_mtime:
                return False
            self._cache_mtime = modified
            if path.stat().st_size > 75_000_000:
                raise ValueError("Cache file exceeds bounded size")
            raw = path.read_bytes()
            cached = Retriever(data=parse_json(raw))
            cached.data_path = path.resolve()
            cached.data_sha256 = hashlib.sha256(raw).hexdigest()
            cache_time = timestamp(cached.dataset.get("collected_at"))
            current_time = timestamp(self.store.snapshot().dataset.get("collected_at"))
            if not cache_time or cache_time > self.clock() + 300 or cache_time <= current_time:
                return False
            if cached.dataset.get("is_demo") is not False or cached.dataset.get("bbox") != list(ASTANA_BBOX) or cached.dataset.get("query_profile") != QUERY_PROFILE or cached.dataset.get("license") != "ODbL 1.0":
                raise ValueError("Unexpected cache scope")
            if any(not (ASTANA_BBOX[0] <= p["lat"] <= ASTANA_BBOX[2] and ASTANA_BBOX[1] <= p["lon"] <= ASTANA_BBOX[3]) for p in cached.places):
                raise ValueError("Cache geometry escaped scope")
            if any(not p["id"].startswith("osm:") or p["source_url"] != "https://www.openstreetmap.org/" + p["id"].removeprefix("osm:") for p in cached.places):
                raise ValueError("Cache source provenance differs from OSM")
            self.store.publish(cached)
            return True
        except (ValueError, OSError, TypeError):
            self._last_error = "invalid_cache"
            return False

    def _schedule(self, state, snapshot=None):
        # Cooldown state cannot make a corrupt/unloaded cache look like the
        # currently served last-good snapshot is newer than it actually is.
        current = snapshot if snapshot is not None else self.store.snapshot()
        good = timestamp(current.dataset.get("collected_at"))
        attempted = timestamp(state.get("last_attempt"))
        return good, attempted, max(good + TTL_SECONDS if good else 0, attempted + COOLDOWN_SECONDS if attempted else 0)

    def status(self, *, snapshot=None):
        with self._status_lock:
            state = dict(self._state)
            phase = self._phase
        good, attempted, due = self._schedule(state, snapshot)
        source = self.store.source()
        if snapshot is not None:
            source = "cache" if snapshot.data_path is not None and snapshot.data_path.is_relative_to(self.cache_dir) else "shipped"
        return {"enabled": True, "state": phase, "ttl_hours": 24, "active_source": source, "last_good_at": utc(good), "last_attempt_at": utc(attempted), "next_check_at": utc(due), "stale": not good or self.clock() - good >= TTL_SECONDS, "last_error": self._last_error or state.get("last_error")}

    def refresh_once(self, *, force=False):
        if not self._flight.acquire(blocking=False):
            return False
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with process_lock(self.path("refresh.lock")) as acquired:
                if not acquired:
                    self._phase = "cooldown"
                    return False
                self.load_cache()
                state = self._read_state()
                self._state = state
                good, attempted, due = self._schedule(state)
                now = self.clock()
                # Force bypasses the 24h TTL, never the persistent 1h cooldown.
                if now < max(attempted, good) + COOLDOWN_SECONDS or (not force and now < due):
                    self._phase = "fresh" if good and now < good + TTL_SECONDS else "cooldown"
                    return False
                self._phase = "updating"
                state.update(last_attempt=now)
                self._write_state(state)
                payload, raw = self.fetcher(DEFAULT_ENDPOINT, ASTANA_BBOX, profile=QUERY_PROFILE, stop_event=self._stop)
                if self._stop.is_set():
                    raise TimeoutError("Refresh stopped")
                collected = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
                document = make_document(payload, collected)
                Retriever(data=document)  # Full geometry/schema/index validation.
                output = self.path("poi.astana.json")
                snapshots = self.path("snapshots")
                write_collection(document, raw, build_query(ASTANA_BBOX, profile=QUERY_PROFILE), output, snapshots)
                self.store.publish(Retriever(output))
                self._cache_mtime = output.stat().st_mtime_ns
                self._last_error = None
                self._phase = "updated"
                state.update(last_success=self.clock(), last_error=None)
                self._write_state(state)
                return True
        except Exception as error:
            code = "http_" + str(error.code) if isinstance(error, HTTPError) else "network_timeout" if isinstance(error, TimeoutError) else "network_error" if isinstance(error, URLError) else "invalid_response" if isinstance(error, (ValueError, TypeError)) else "storage_or_network_error"
            self._last_error, self._phase = code, "error"
            state = dict(self._state)
            state.update(last_attempt=self.clock(), last_error=code)
            try:
                self._write_state(state)
            except OSError:
                pass
            return False
        finally:
            self._flight.release()

    def start(self):
        if self._thread is not None:
            return
        self.load_cache()  # Disk only, no network on the main/startup thread.
        def worker():
            while not self._stop.is_set():
                self.refresh_once()
                self._stop.wait(60)
        self._thread = Thread(target=worker, name="poi-refresh", daemon=True)
        self._thread.start()

    def stop(self, timeout=2):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self._thread is None or not self._thread.is_alive()
