"""Short-lived route tickets for the official, restricted Google Embed key.

The browser key is intentionally public. Never accept the backend Google key.
Neither precise route coordinates nor ticket content are written to disk.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
import secrets
from threading import Lock, Timer
from time import monotonic
from urllib.parse import urlencode

from .google_budget import GoogleError, _path
from .google_client import valid_place_id
from .google_grounded import PUBLIC_NAMES


class RouteMap:
    def __init__(self, key=None, *, clock=monotonic):
        if key is not None and (not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{20,128}", key) is None):
            raise ValueError("Invalid dedicated embed key")
        self._key, self.clock, self._routes, self._lock = key, clock, {}, Lock()
        self._timers = {}

    @classmethod
    def from_runtime(cls, path):
        try:
            raw = _path(path).read_bytes()
            if len(raw) > 4096:
                raise ValueError()
            config = json.loads(raw)
            if not isinstance(config, dict) or set(config) != {"embed_key_file"}:
                raise ValueError()
            key_raw = _path(config["embed_key_file"]).read_bytes()
            if len(key_raw) > 1024:
                raise ValueError()
            return cls(key_raw.decode("utf-8").strip())
        except (OSError, ValueError, UnicodeError, TypeError):
            raise ValueError("Dedicated Maps Embed configuration unavailable") from None

    def status(self):
        return {"configured": self._key is not None, "provider": "Google Maps Embed", "mode": "walking", "storage": "memory-600-seconds"}

    def _prune(self):
        now = self.clock()
        for key in list(self._routes):
            if self._routes[key][0] <= now:
                self._routes.pop(key, None)
                timer = self._timers.pop(key, None)
                if timer is not None:
                    timer.cancel()

    def _expire(self, ticket):
        with self._lock:
            self._routes.pop(ticket, None)
            self._timers.pop(ticket, None)

    def close(self):
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._routes.clear()

    @staticmethod
    def _point(point):
        if any(type(point.get(key)) not in (int, float) or not math.isfinite(point[key]) or not low <= point[key] <= high for key, low, high in (("lat", -90, 90), ("lon", -180, 180))):
            raise ValueError("Invalid route point")
        return f"{point['lat']:.7f},{point['lon']:.7f}"

    def issue(self, origin, stops, *, context=None):
        if not 1 <= len(stops) <= 3:
            raise ValueError("Route requires one to three stops")
        start = self._point(origin)
        if isinstance(context, dict) and not context.get("sensitive_origin") and context.get("origin_id") in PUBLIC_NAMES:
            start = PUBLIC_NAMES[context["origin_id"]]
        points = []
        for stop in stops:
            coordinate = self._point(stop)
            if stop.get("provider") == "google":
                if not valid_place_id(stop.get("id")):
                    raise ValueError("Invalid Google route identity")
                points.append("place_id:" + stop["id"])
            else:
                points.append(coordinate)
        with self._lock:
            self._prune()
            if len(self._routes) >= 128:
                raise GoogleError("route_map_busy", "Слишком много незавершённых карт; повторите позже.", 429)
            ticket = secrets.token_urlsafe(32)
            self._routes[ticket] = (self.clock() + 600, start, points)
            timer = Timer(600, self._expire, args=(ticket,))
            timer.daemon = True
            self._timers[ticket] = timer
            timer.start()
            return ticket

    def resolve(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"ticket"} or not isinstance(payload["ticket"], str) or len(payload["ticket"]) > 128:
            raise GoogleError("route_map_invalid", "Некорректная карта маршрута.", 400)
        if self._key is None:
            raise GoogleError("route_map_unconfigured", "Для карты маршрута нужен отдельный ограниченный ключ Maps Embed API.", 503)
        with self._lock:
            self._prune()
            value = self._routes.get(payload["ticket"])
            if value is None:
                raise GoogleError("route_map_expired", "Карта маршрута устарела; постройте маршрут заново.", 410)
            expiry, start, points = deepcopy(value)
        params = {"key": self._key, "origin": start, "destination": start,
                  "waypoints": "|".join(points), "mode": "walking", "language": "ru", "region": "KZ"}
        return {"url": "https://www.google.com/maps/embed/v1/directions?" + urlencode(params), "expires_in": max(0, int(expiry - self.clock()))}
