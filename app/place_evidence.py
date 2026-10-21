"""Pure, own-record property projection; no I/O, cache, or POI merging.

Only existing normalized OSM/2GIS features and the documented Places-New
wheelchair-accessible entrance boolean are accepted. ``wheelchair`` describes
that entrance, never a barrier-free route. Unsupported properties remain
unknown; venue categories, prose, ratings and Google summaries prove nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

_PROPERTIES = ("socket", "quiet", "wheelchair")
_PROVIDERS = frozenset({"osm", "dgis", "google"})
_HOSTS = {
    "osm": frozenset({"openstreetmap.org", "www.openstreetmap.org"}),
    "dgis": frozenset({"2gis.kz", "2gis.ru", "www.2gis.kz", "www.2gis.ru"}),
    "google": frozenset({"www.google.com", "google.com", "maps.google.com", "maps.google.kz"}),
}
# No socket/quiet attribute IDs or tags have verified semantics yet. Do not add
# guesses based on names, categories, provider queries or human-readable prose.
VERIFIED_DGIS_ATTRIBUTES = MappingProxyType({})
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_CONTROLS = re.compile(r"[\x00-\x20\x7f]")
_SECRET_PARAMETERS = frozenset({"key", "api_key", "apikey", "token", "access_token"})


class PlaceEvidenceError(ValueError):
    """Invalid evidence metadata; message deliberately contains no input data."""

    def __init__(self):
        super().__init__("Некорректные структурированные сведения о месте.")


def _identity(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or value != value.strip() or _CONTROLS.search(value)):
        raise PlaceEvidenceError()
    return value


def _source_url(value, provider):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or _CONTROLS.search(value):
        raise PlaceEvidenceError()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS[provider]
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or any(key.casefold() in _SECRET_PARAMETERS for key, _ in parse_qsl(parsed.query))):
            raise PlaceEvidenceError()
    except (ValueError, TypeError):
        raise PlaceEvidenceError() from None
    return value


def _observed_at(value):
    if not isinstance(value, str) or len(value) > 40 or not _ISO_INSTANT.fullmatch(value):
        raise PlaceEvidenceError()
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = instant.utcoffset()
        if offset is None or abs(offset) > timedelta(hours=14):
            raise PlaceEvidenceError()
        return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise PlaceEvidenceError() from None


def _feature(value):
    if type(value) is bool:
        return "yes" if value else "no"
    if isinstance(value, str) and value in {"yes", "no", "unknown"}:
        return value
    raise PlaceEvidenceError()


def _validate_unmapped_dgis_attributes(place):
    """Bound shape checking only: no verified attribute mapping exists yet."""
    if "attribute_groups" not in place:
        return
    groups = place["attribute_groups"]
    if not isinstance(groups, list) or len(groups) > 16:
        raise PlaceEvidenceError()
    for group in groups:
        if not isinstance(group, dict):
            raise PlaceEvidenceError()
        attributes = group.get("attributes", [])
        if not isinstance(attributes, list) or len(attributes) > 64:
            raise PlaceEvidenceError()
        for attribute in attributes:
            if not isinstance(attribute, dict):
                raise PlaceEvidenceError()
            identifier, tag = attribute.get("id"), attribute.get("tag")
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100:
                raise PlaceEvidenceError()
            if tag is not None and (not isinstance(tag, str) or not 1 <= len(tag) <= 120):
                raise PlaceEvidenceError()
            # Unverified IDs/tags/names never become a property, including tags
            # literally spelling 'socket', 'quiet' or 'wheelchair'.


def normalize_place_evidence(place, *, provider, place_id, source_url, observed_at):
    """Return fresh ``features`` and minimal provenance for one exact record.

    ``place_id`` is the expected identity from the caller's own source record;
    input ``id`` must match it literally. Provider is osm/dgis/google. OSM/2GIS
    ``features`` are PRE-EXISTING NORMALIZED values, not raw attribute proof.
    Google accepts only raw Places-New accessibilityOptions entrance boolean;
    nonstandard Google ``features`` and all other content are ignored.

    Caller supplies a bounded HTTPS provider URL and timezone-aware ISO instant.
    Observation time is normalized to UTC, without claiming source freshness.
    No provider body, names, query, location, hours, reviews or group capacity
    are returned or retained. No activity/service proof is manufactured.
    """
    if (not isinstance(place, dict) or len(place) > 256
            or not isinstance(provider, str) or provider not in _PROVIDERS):
        raise PlaceEvidenceError()
    expected = _identity(place_id)
    if _identity(place.get("id")) != expected:
        raise PlaceEvidenceError()
    if "provider" in place and place["provider"] != provider:
        raise PlaceEvidenceError()
    source_url = _source_url(source_url, provider)
    stamp = _observed_at(observed_at)
    features = dict.fromkeys(_PROPERTIES, "unknown")
    evidence = []

    def record(property_name, value, path):
        features[property_name] = value
        evidence.append({"property": property_name, "value": value,
                         "provider": provider, "place_id": expected,
                         "field_path": path, "source_url": source_url,
                         "observed_at": stamp})

    if provider in {"osm", "dgis"}:
        values = place.get("features", {})
        if not isinstance(values, dict) or len(values) > 32:
            raise PlaceEvidenceError()
        for property_name in _PROPERTIES:
            if property_name in values:
                record(property_name, _feature(values[property_name]), "normalized.features." + property_name)
        if provider == "dgis":
            _validate_unmapped_dgis_attributes(place)
    else:
        if "accessibilityOptions" in place:
            options = place["accessibilityOptions"]
            if not isinstance(options, dict) or len(options) > 16:
                raise PlaceEvidenceError()
            field = "wheelchairAccessibleEntrance"
            if field in options:
                if type(options[field]) is not bool:
                    raise PlaceEvidenceError()
                record("wheelchair", _feature(options[field]), "accessibilityOptions." + field)
    return {"features": features, "evidence": evidence}
