"""A shared, deadline-bounded queue for the operator's loopback model.

The optional OS lock contains no requests or responses. It is shared by main
and preview; this module never starts a model or retries inference.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from threading import Lock, local
from time import monotonic
from urllib.request import Request
from urllib.error import URLError

from .google_budget import _locked, GoogleError
from .google_client import json_bytes, strict_json
from .google_grounded import _local_config, open_local_model

_PROCESS_LOCK = Lock()


class ModelUnavailable(Exception):
    def __init__(self, message, *, code="transport"):
        super().__init__(message)
        self.code = code if code in {"unconfigured", "busy", "deadline", "transport", "invalid_response"} else "transport"


class _BoundedResponse:
    def __init__(self, response, deadline, clock):
        self.response, self.deadline, self.clock = response, deadline, clock

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.response.close()

    def read(self, size=100001):
        if type(size) is not int or not 0 < size <= 100001:
            raise ModelUnavailable("Invalid local response bound", code="invalid_response")
        chunks = bytearray()
        while len(chunks) < size:
            left = self.deadline - self.clock()
            if left <= 0:
                raise ModelUnavailable("Local response deadline", code="deadline")
            sock = getattr(getattr(getattr(self.response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(left)
            reader = getattr(self.response, "read1", self.response.read)
            chunk = reader(min(8192, size - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if self.clock() >= self.deadline:
            raise ModelUnavailable("Local response deadline", code="deadline")
        return bytes(chunks)


class ModelGateway:
    def __init__(self, config, *, lock_path=None, http_open=None, clock=monotonic):
        self.config, self.lock_path = config, lock_path
        self.http_open, self.clock = http_open or open_local_model, clock
        self._state = local()

    @contextmanager
    def session(self, deadline, *, seconds=40):
        if not _local_config(self.config):
            raise ModelUnavailable("No loopback model configured", code="unconfigured")
        end = min(deadline, self.clock() + seconds, self.clock() + self.config.timeout)
        remaining = end - self.clock()
        if remaining <= 0:
            raise ModelUnavailable("Local model deadline", code="deadline")
        if not _PROCESS_LOCK.acquire(timeout=remaining):
            raise ModelUnavailable("Local model busy", code="busy")
        previous = getattr(self._state, "deadline", None)
        try:
            remaining = end - self.clock()
            if remaining <= 0:
                raise ModelUnavailable("Local model deadline", code="deadline")
            # A separate lock file, never the provider ledger itself.
            lock = _locked(self.lock_path, timeout=remaining) if self.lock_path else nullcontext()
            with lock:
                self._state.deadline = end
                yield
        except ModelUnavailable:
            raise
        except GoogleError:
            # The model session uses only its OS lock, never the provider ledger.
            raise ModelUnavailable("Local model busy", code="busy") from None
        except TimeoutError:
            raise ModelUnavailable("Local model deadline", code="deadline") from None
        except URLError as error:
            code = "deadline" if isinstance(error.reason, TimeoutError) else "transport"
            raise ModelUnavailable("Local model unavailable", code=code) from None
        except Exception:
            raise ModelUnavailable("Local model unavailable", code="transport") from None
        finally:
            self._state.deadline = previous
            _PROCESS_LOCK.release()

    def open(self, request, timeout):
        deadline = getattr(self._state, "deadline", None)
        if deadline is None or request.full_url != self.config.base_url + "/chat/completions":
            raise ModelUnavailable("Model gateway requires an active local session")
        remaining = min(timeout, deadline - self.clock())
        if remaining <= 0:
            raise ModelUnavailable("Local model deadline", code="deadline")
        response = self.http_open(request, timeout=remaining)
        return _BoundedResponse(response, deadline, self.clock)

    def extract(self, query, deadline):
        if not _local_config(self.config):
            return None, "unconfigured", 0
        from .semantic_intent import extraction_request, validate_intent, IntentError
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
        except ModelUnavailable as error:
            return None, error.code, attempts
        except Exception:
            return None, "transport", attempts
        # Parsing is outside session(): malformed content is not an outage and
        # must not be converted by its transport/lock exception boundary.
        if len(raw) > 100000:
            return None, "invalid_response", attempts
        try:
            provider = strict_json(raw)
        except Exception:
            return None, "provider_json", attempts
        try:
            choice = provider["choices"][0]
            finish = choice.get("finish_reason")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Invalid model content")
            if finish == "length":
                return None, "truncated_response", attempts
            if finish not in {None, "stop"}:
                return None, "invalid_response", attempts
        except Exception:
            return None, "invalid_response", attempts
        try:
            value = strict_json(content)
        except Exception:
            return None, "content_json", attempts
        try:
            return validate_intent(value, query), "semantic", attempts
        except IntentError:
            return None, "invalid_intent", attempts
        except Exception:
            return None, "invalid_intent", attempts
