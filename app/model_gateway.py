"""A shared, deadline-bounded queue for the operator's loopback model.

The optional OS lock contains no requests or responses. It is shared by main
and preview; this module never starts a model or retries inference.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from threading import Lock, local
from time import monotonic
from urllib.request import Request

from .google_budget import _locked
from .google_client import json_bytes, strict_json
from .google_grounded import _local_config, open_local_model

_PROCESS_LOCK = Lock()


class ModelUnavailable(Exception):
    pass


class _BoundedResponse:
    def __init__(self, response, deadline, clock):
        self.response, self.deadline, self.clock = response, deadline, clock

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.response.close()

    def read(self, size=100001):
        if type(size) is not int or not 0 < size <= 100001:
            raise ModelUnavailable("Invalid local response bound")
        chunks = bytearray()
        while len(chunks) < size:
            left = self.deadline - self.clock()
            if left <= 0:
                raise ModelUnavailable("Local response deadline")
            sock = getattr(getattr(getattr(self.response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(left)
            reader = getattr(self.response, "read1", self.response.read)
            chunk = reader(min(8192, size - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if self.clock() >= self.deadline:
            raise ModelUnavailable("Local response deadline")
        return bytes(chunks)


class ModelGateway:
    def __init__(self, config, *, lock_path=None, http_open=None, clock=monotonic):
        self.config, self.lock_path = config, lock_path
        self.http_open, self.clock = http_open or open_local_model, clock
        self._state = local()

    @contextmanager
    def session(self, deadline, *, seconds=40):
        if not _local_config(self.config):
            raise ModelUnavailable("No loopback model configured")
        end = min(deadline, self.clock() + seconds, self.clock() + self.config.timeout)
        remaining = end - self.clock()
        if remaining <= 0 or not _PROCESS_LOCK.acquire(timeout=max(0, remaining)):
            raise ModelUnavailable("Local model busy")
        previous = getattr(self._state, "deadline", None)
        try:
            remaining = end - self.clock()
            if remaining <= 0:
                raise ModelUnavailable("Local model deadline")
            # A separate lock file, never the provider ledger itself.
            lock = _locked(self.lock_path, timeout=remaining) if self.lock_path else nullcontext()
            with lock:
                self._state.deadline = end
                yield
        except ModelUnavailable:
            raise
        except Exception:
            raise ModelUnavailable("Local model unavailable") from None
        finally:
            self._state.deadline = previous
            _PROCESS_LOCK.release()

    def open(self, request, timeout):
        deadline = getattr(self._state, "deadline", None)
        if deadline is None or request.full_url != self.config.base_url + "/chat/completions":
            raise ModelUnavailable("Model gateway requires an active local session")
        remaining = min(timeout, deadline - self.clock())
        if remaining <= 0:
            raise ModelUnavailable("Local model deadline")
        response = self.http_open(request, timeout=remaining)
        return _BoundedResponse(response, deadline, self.clock)

    def extract(self, query, deadline):
        if not _local_config(self.config):
            return None, "unconfigured", 0
        from .semantic_intent import extraction_request, validate_intent
        attempts = 0
        try:
            with self.session(deadline, seconds=40):
                payload = extraction_request(query, self.config.model)
                payload.pop("max_completion_tokens", None)
                payload.pop("max_tokens", None)
                payload[self.config.token_parameter] = 512
                payload["cache_prompt"] = False
                request = Request(self.config.base_url + "/chat/completions", data=json_bytes(payload),
                                  headers={"Content-Type": "application/json"}, method="POST")
                attempts = 1
                with self.open(request, timeout=40) as response:
                    raw = response.read(100001)
                if len(raw) > 100000:
                    raise ValueError("Oversized local response")
                provider = strict_json(raw)
                value = strict_json(provider["choices"][0]["message"]["content"])
                return validate_intent(value, query), "semantic", attempts
        except Exception:
            return None, "unavailable_or_invalid", attempts
