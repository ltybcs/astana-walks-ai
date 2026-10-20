"""Ephemeral preparation: one intent, one verified origin, one-use RAM ticket.

Nothing here writes a query, coordinate, provider record or ticket to disk.
The public token is a random handle, not an encoded copy of personal context.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import re
import secrets
from threading import Lock
from time import monotonic
import unicodedata

from .google_budget import GoogleError
from .google_client import json_bytes
from .lab_context import ContextError
from .semantic_intent import deterministic_plan, explicit_start_quote, IntentError
from .route_request import validate_walking_request


@dataclass(frozen=True)
class PreparedRequest:
    form: dict
    resolved: dict
    intent_plan: object
    deadline: float
    intent_status: str
    intent_attempts: int
    place_attempts: int


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity(form):
    return _digest({key: value for key, value in form.items() if key not in {"geolocation", "location_status"}})


def _place_terms(value):
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    stop = {"я", "сейчас", "нахожусь", "стою", "около", "возле", "рядом", "с", "у", "в", "на", "от",
            "здание", "здания", "здании", "улица", "улице", "улицы", "ул", "проспект", "проспекте",
            "астана", "астане", "казахстан", "astana", "kazakhstan", "street", "avenue", "near", "at", "the",
            "отель", "отеля", "отеле", "гостиница", "гостиницы", "гостинице", "тц", "трц",
            "торговый", "торгового", "торговом", "центр", "центра", "центре"}
    result = []
    for word in re.findall(r"[\w]+", text):
        if word in stop or (len(word) < 2 and not word.isdecimal()):
            continue
        if len(word) > 5:
            word = re.sub(r"(иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ая|яя|ое|ее|ые|ие|ой|ей|ов|ев|ах|ях|ом|ем|ам|ям|ы|и|а|я|у|ю|е)$", "", word)
        result.append(word)
    return result


def _latin_name(word):
    # Systematic spelling normalization, not a table of venue aliases or fuzzy rank.
    alphabet = dict(zip("абвгдеёзийклмнопрстуфыэ", "abvgdeezijklmnoprstufye"))
    alphabet.update({"ж":"zh", "х":"kh", "ц":"ts", "ч":"ch", "ш":"sh", "щ":"shch",
                     "ю":"yu", "я":"ya", "ь":"", "ъ":"", "й":"y", "ә":"a", "і":"i",
                     "қ":"q", "ғ":"gh", "ұ":"u", "ү":"u", "ө":"o", "ң":"ng"})
    return "".join(alphabet.get(character, character) for character in word)


def unique_place(candidates, quote):
    """Select by actual name/address agreement, never by the first ranked hit."""
    needed = _place_terms(quote)
    if not needed or not any(not term.isdecimal() and len(term) >= 3 for term in needed) or not isinstance(candidates, list):
        return None
    numbers = set(re.findall(r"(?<!\w)\d+[a-zа-яё]?\d*(?:[/\-][\da-zа-яё]+)*", quote.casefold()))
    matched = {}
    for place in candidates:
        if not isinstance(place, dict):
            continue
        name_address = place.get("name", "") + " " + place.get("address", "")
        actual_numbers = set(re.findall(r"(?<!\w)\d+[a-zа-яё]?\d*(?:[/\-][\da-zа-яё]+)*", name_address.casefold()))
        if not numbers.issubset(actual_numbers):
            continue
        actual = _place_terms(name_address)
        def agrees(term):
            if term.isdecimal():
                return term in actual
            term = _latin_name(term)
            return any(term == _latin_name(word) for word in actual)
        if all(agrees(term) for term in needed):
            matched[place.get("id")] = place
    return next(iter(matched.values())) if len(matched) == 1 else None


class RequestPreparation:
    def __init__(self, resolver, stager, google, gateway, *, clock=monotonic, lifetime=200, capacity=128):
        self.resolver, self.stager, self.google, self.gateway = resolver, stager, google, gateway
        self.clock, self.lifetime, self.capacity = clock, lifetime, capacity
        self._lock, self._entries = Lock(), {}

    @staticmethod
    def _form(payload):
        if not isinstance(payload, dict) or payload.get("consent") is not True:
            raise GoogleError("consent_required", "Нужно согласие на поиск мест рядом.", 400)
        return {key: value for key, value in payload.items() if key not in {"consent", "preparation_token"}}

    def _purge(self):
        now = self.clock()
        self._entries = {token: entry for token, entry in self._entries.items() if entry["prepared"].deadline > now}

    @staticmethod
    def _invalid():
        return GoogleError("invalid_preparation", "Подготовленный запрос изменился или устарел. Запустите подбор снова.", 400)

    def _origin(self, form, plan, deadline, previous=None):
        quote = plan.start_quote if plan.extraction_mode == "semantic" else explicit_start_quote(form["query"])
        options = {"start_quote": quote} if plan.extraction_mode == "semantic" or quote is not None else {}
        if previous and previous.resolved.get("origin") is not None:
            return deepcopy(previous.resolved), previous.place_attempts
        draft = self.stager(form, **options)
        if not draft.get("needs_place_resolution"):
            return draft, previous.place_attempts if previous else 0
        quote = draft.get("unresolved_start_quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ContextError("Не удалось определить указанное место старта.", code="location_unresolved", status=422)
        if not _place_terms(quote):
            # Mentioning only Astana specifies the supported city, not a start point.
            return self.stager(form, start_quote=None), 0
        if self.google is None or not hasattr(self.google, "resolve_place"):
            raise GoogleError("place_resolution_unavailable", "Поиск названного места временно недоступен.", 503)
        candidates = self.google.resolve_place(quote, deadline=deadline)
        place = unique_place(candidates, quote)
        if place is None:
            raise ContextError("По указанному названию нет однозначного места в Астане. Подбор мест не выполнен.",
                               code="location_unresolved", status=422, fields=("origin",))
        verified = {"origin": {"lat": place["lat"], "lon": place["lon"], "label": place["name"]},
                    "origin_source": "google_places_text_search", "origin_reference": place["source_url"], "origin_id": None}
        resolved = self.stager(form, verified_start=verified, **options)
        resolved["resolved_context"]["origin_attributions"] = deepcopy(place.get("attributions", []))
        return resolved, 1

    def _new(self, form):
        started = self.clock()
        # Validate structure, time, location fields and city scope before model I/O.
        draft = self.stager(form)
        query = form["query"].strip()
        validate_walking_request(query, draft["resolved_context"]["budget_options"])
        plan, status, attempts = self.gateway.extract(query, started + self.lifetime) if self.gateway else (None, "unconfigured", 0)
        if plan is None:
            try:
                plan = deterministic_plan(query)
            except IntentError:
                if status == "deadline":
                    raise GoogleError("intent_timeout", "Разбор пожелания занял слишком много времени. Попробуйте снова.", 504) from None
                if status in {"invalid_intent", "invalid_response", "provider_json", "content_json", "truncated_response"}:
                    raise GoogleError("intent_not_understood", "Не удалось надёжно разобрать пожелание. Поиск по источникам не запускался.", 422) from None
                raise GoogleError("intent_temporarily_unavailable", "Не удалось обработать пожелание: локальный ИИ временно недоступен. Попробуйте позже.", 503) from None
        if self.clock() >= started + self.lifetime:
            raise GoogleError("context_timeout", "Истекло время подготовки запроса.", 504)
        resolved, place_attempts = self._origin(form, plan, started + self.lifetime)
        return PreparedRequest(deepcopy(form), resolved, plan, started + self.lifetime, status, attempts, place_attempts)

    def prepare(self, payload):
        form = self._form(payload)
        token = payload.get("preparation_token")
        if "preparation_token" in payload:
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token) is None:
                raise self._invalid()
            with self._lock:
                self._purge()
                entry = self._entries.pop(token, None)
            if entry is None or entry["identity"] != _identity(form) or not entry["prepared"].resolved.get("needs_geolocation"):
                raise self._invalid()
            previous = entry["prepared"]
            resolved, count = self._origin(form, previous.intent_plan, previous.deadline, previous)
            prepared = replace(previous, form=deepcopy(form), resolved=resolved, place_attempts=count)
        else:
            prepared = self._new(form)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            if len(self._entries) >= self.capacity:
                raise GoogleError("context_busy", "Подготовка запросов занята. Попробуйте немного позже.", 429)
            self._entries[token] = {"identity": _identity(form), "prepared": prepared}
        return {"preparation_token": token, "needs_geolocation": prepared.resolved["needs_geolocation"],
                "resolved_context": deepcopy(prepared.resolved["resolved_context"])}

    def consume(self, payload):
        form = self._form(payload)
        if "preparation_token" not in payload:
            prepared = self._new(form)
        else:
            token = payload["preparation_token"]
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token) is None:
                raise self._invalid()
            with self._lock:
                self._purge()
                entry = self._entries.pop(token, None)
            if entry is None or _digest(form) != _digest(entry["prepared"].form):
                raise self._invalid()
            prepared = entry["prepared"]
        if prepared.resolved.get("needs_geolocation") or not prepared.resolved.get("origin"):
            raise ContextError("Для подбора нужен старт в сообщении или разрешённая геолокация.", code="known_start_required", status=422)
        if self.clock() >= prepared.deadline:
            raise GoogleError("context_timeout", "Истекло время подготовки запроса.", 504)
        return prepared
