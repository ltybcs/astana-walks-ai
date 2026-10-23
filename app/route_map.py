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
from .google_client import valid_place_id, maps_url
from .lab_context import PUBLIC_ORIGINS


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
        if not isinstance(point, dict) or any(type(point.get(key)) not in (int, float) or not math.isfinite(point[key]) or not low <= point[key] <= high for key, low, high in (("lat", -90, 90), ("lon", -180, 180))):
            raise ValueError("Invalid route point")
        return f"{point['lat']:.7f},{point['lon']:.7f}"

    def issue(self, origin, stops, *, context=None, plan=None):
        if not isinstance(stops, (list, tuple)) or len(stops) > 3:
            raise ValueError("Route requires a bounded stop list")
        # Endpoint coordinates are server-confirmed; never route to an ambiguous
        # display name or accept a browser-supplied destination at resolution.
        start = self._point(origin)
        route_kind = context.get("route_kind") if isinstance(context, dict) else None
        if route_kind not in (None, "round_trip", "point_to_point"):
            raise ValueError("Unsupported route kind")
        if route_kind == "point_to_point":
            for role in ("origin", "destination"):
                point = context.get(role)
                self._point(point)
                source, identifier = context.get(role + "_source"), context.get(role + "_id")
                if not 51.00 <= point["lat"] <= 51.32 or not 71.20 <= point["lon"] <= 71.70:
                    raise ValueError("Endpoint outside confirmed city scope")
                if source in {"query_landmark", "explicit_landmark"}:
                    if not isinstance(identifier, str) or identifier not in PUBLIC_ORIGINS or self._point(point) != self._point(PUBLIC_ORIGINS[identifier]):
                        raise ValueError("Public endpoint identity changed")
                elif source == "google_places_text_search":
                    if identifier is not None or not valid_place_id(context.get(role + "_place_id")) or not maps_url(context.get(role + "_reference")):
                        raise ValueError("Endpoint identity is not server-confirmed")
                elif role != "origin" or source not in {"browser_geolocation", "explicit_coordinates"} or identifier is not None:
                    raise ValueError("Unconfirmed endpoint source")
            if len(stops) > 1:
                groups = context.get("ordered_goal_groups")
                if (len(stops) != 2 or not isinstance(groups, list) or len(groups) != 2
                        or any(not isinstance(group, list) or not group or len(group) > 3
                               or any(type(index) is not int or not 0 <= index <= 2 for index in group) for group in groups)
                        or len(set(groups[0] + groups[1])) != len(groups[0] + groups[1])
                        or not isinstance(plan, dict) or plan.get("ordered_goals_covered") is not True
                        or any(not isinstance(stop, dict) or not isinstance(stop.get("matched_goal_indices"), list)
                               or any(type(value) is not int or not 0 <= value <= 2 for value in stop["matched_goal_indices"])
                               or not set(stop["matched_goal_indices"]) & set(groups[index]) for index, stop in enumerate(stops))
                        or stops[0].get("stop_id") == stops[1].get("stop_id")):
                    raise ValueError("Two stops require a verified complete ordered sequence")
            if (not isinstance(plan, dict) or plan.get("route_kind") != route_kind
                    or plan.get("verified") is not True or plan.get("reaches_destination") is not True
                    or plan.get("returns_to_origin") is not False):
                raise ValueError("Point-to-point route must be verified")
            end = self._point(context.get("destination"))
            if (self._point(context.get("origin")) != start or end == start
                    or self._point(plan.get("destination")) != end):
                raise ValueError("Route endpoint mismatch")
            final_minutes = plan.get("final_walk_minutes")
            if type(final_minutes) not in (int, float) or not math.isfinite(final_minutes) or final_minutes < 0:
                raise ValueError("Invalid final walking leg")
            if not stops and plan.get("direct_only") is not True:
                raise ValueError("Empty route requires a verified direct-only plan")
            if stops and plan.get("direct_only") is True:
                raise ValueError("Direct-only plan cannot contain stops")
        else:
            if (isinstance(context, dict) and context.get("destination") is not None
                    or isinstance(plan, dict) and plan.get("route_kind") == "point_to_point"):
                raise ValueError("Destination requires point-to-point context")
            if not stops:
                raise ValueError("Round trip requires one to three stops")
            end = start
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
            self._routes[ticket] = (self.clock() + 600, start, end, points)
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
            expiry, start, end, points = deepcopy(value)
        params = {"key": self._key, "origin": start, "destination": end,
                  "mode": "walking", "language": "ru", "region": "KZ"}
        if points:
            params["waypoints"] = "|".join(points)
        return {"url": "https://www.google.com/maps/embed/v1/directions?" + urlencode(params), "expires_in": max(0, int(expiry - self.clock()))}
