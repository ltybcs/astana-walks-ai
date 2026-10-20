"""A single explicitly initialized, fail-closed 500-attempt Google pilot ledger.

Only operation counters and UTC timestamps are persisted, never Google content,
queries, coordinates, keys or identifiers. Reservation is durable before HTTP.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from time import monotonic, sleep

LIMIT = 500
OPERATIONS = frozenset({
    "mcp.initialize", "mcp.initialized", "mcp.tools_list", "mcp.search_places",
    "mcp.compute_routes", "mcp.close", "places.hours", "places.reviews", "places.resolve",
})


class GoogleError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def as_dict(self):
        return {"error": self.message, "code": self.code}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(value) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Google ledger requires one shared absolute path")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Google runtime path must not contain symlinks")
    if not path.parent.is_dir():
        raise ValueError("Google runtime directory must already exist")
    return path


def _decode(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate ledger key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite ledger")))
    if not isinstance(value, dict) or set(value) != {"version", "limit", "used", "operations", "initialized_at", "updated_at"}:
        raise ValueError("Invalid ledger schema")
    if type(value["version"]) is not int or value["version"] != 1 or type(value["limit"]) is not int or value["limit"] != LIMIT:
        raise ValueError("Invalid ledger grant")
    if type(value["used"]) is not int or not 0 <= value["used"] <= LIMIT:
        raise ValueError("Invalid ledger usage")
    counts = value["operations"]
    if not isinstance(counts, dict) or not set(counts) <= OPERATIONS or any(type(n) is not int or n < 0 for n in counts.values()) or sum(counts.values()) != value["used"]:
        raise ValueError("Invalid ledger counters")
    for key in ("initialized_at", "updated_at"):
        if not isinstance(value[key], str) or len(value[key]) > 64 or datetime.fromisoformat(value[key]).tzinfo is None:
            raise ValueError("Invalid ledger timestamp")
    return value


@contextmanager
def _locked(path: Path, timeout: float = 5):
    # OS locks are shared across threads, Budget instances and processes.
    lock_path = _path(path.with_name(path.name + ".lock"))
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if monotonic() >= deadline:
                    raise GoogleError("quota_unavailable", "Не удалось заблокировать общий лимит Google.", 503)
                sleep(0.01)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _replace_reserved_snapshot(source, target):
    """Retry only transient Windows sharing collisions on this same snapshot.

    The caller retains the writer lock and the already fsynced temporary file;
    neither the counter reservation nor any HTTP operation is repeated.
    """
    deadline = monotonic() + 0.2
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33}:
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            sleep(min(0.01, remaining))
            if monotonic() >= deadline:
                raise


class GoogleBudget:
    def __init__(self, path):
        self.path = _path(path)

    @staticmethod
    def initialize(path) -> None:
        """Explicit operator action only. Never reset or overwrite a ledger."""
        path = _path(path)
        with _locked(path):
            stamp = utc_now()
            value = {"version": 1, "limit": LIMIT, "used": 0, "operations": {},
                     "initialized_at": stamp, "updated_at": stamp}
            raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
            # O_EXCL is essential: an exhausted/corrupt prior grant is not reset.
            with path.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())

    def _read(self) -> dict:
        try:
            _path(self.path)
            with self.path.open("rb") as source:
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError("Oversized ledger")
            return _decode(raw)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise GoogleError("quota_unavailable", "Общий лимит Google отсутствует или повреждён; требуется проверка оператора.", 503) from None

    @staticmethod
    def _status(value) -> dict:
        return {"used": value["used"], "limit": LIMIT, "remaining": LIMIT - value["used"],
                "state": "exhausted" if value["used"] == LIMIT else "ready"}

    def status(self) -> dict:
        try:
            # Atomic replacement gives readers a complete old or new snapshot.
            return self._status(self._read())
        except GoogleError:
            return {"used": None, "limit": LIMIT, "remaining": 0, "state": "unavailable"}

    def reserve(self, operation: str) -> dict:
        if operation not in OPERATIONS:
            raise ValueError("Unknown Google operation")
        try:
            with _locked(self.path):
                value = self._read()
                if value["used"] >= LIMIT:
                    raise GoogleError("quota_exhausted", "Общий тестовый лимит Google 500 запросов исчерпан.", 429)
                value["used"] += 1
                value["operations"][operation] = value["operations"].get(operation, 0) + 1
                value["updated_at"] = utc_now()
                raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent, delete=False) as output:
                        temporary = Path(output.name)
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    _path(self.path)
                    _replace_reserved_snapshot(temporary, self.path)
                    temporary = None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return self._status(value)
        except GoogleError:
            raise
        except (OSError, ValueError):
            raise GoogleError("quota_unavailable", "Не удалось надёжно учесть запрос Google; запрос не отправлен.", 503) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", metavar="ABSOLUTE_LEDGER_PATH", required=True)
    args = parser.parse_args(argv)
    try:
        GoogleBudget.initialize(args.init)
    except (OSError, ValueError, GoogleError):
        parser.exit(1, "Ledger initialization refused; existing state is never reset.\n")
    print("Initialized one 500-attempt Google pilot ledger; keep its absolute path shared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
