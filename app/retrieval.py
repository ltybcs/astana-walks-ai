"""Grounded POI retrieval and a conservative, time-bounded walking plan.

This is lexical RAG (BM25), not an embedding or live-routing service. All
distance and duration estimates are derived locally, never stated as live facts.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any
from itertools import permutations
from urllib.parse import urlparse

from .activity_intent import parse_search_intent, _negated, WIFI_PATTERN
from .category_catalog import CATEGORY_CATALOG


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "poi.astana.json"
WALK_METRES_PER_MINUTE = 75
PATH_FACTOR = 1.3
CATEGORY_TEXT = {
    "computer_club": "компьютерный комп клуб киберклуб интернет-клуб gaming computer pc esports club",
    "park": "парк сад природа зелень прогулка гулять воздух ребёнок дети семья park garden nature саябақ серуен отбасы",
    "playground": "детская площадка ребёнок дети семья играть playground бала балалар",
    "cafe": "кафе кофе чай перекус посидеть coffee cafe",
    "restaurant": "ресторан еда поесть обед ужин",
    "museum": "музей выставка история культура турист достопримечательность",
    "attraction": "достопримечательность турист архитектура посмотреть фото",
    "monument": "памятник турист история архитектура фото",
    "viewpoint": "вид панорама фото турист посмотреть",
    "theatre": "театр культура архитектура посмотреть",
    "mall": "торговый центр магазин шопинг mega silk way мега",
    "promenade": "набережная бульвар прогулка гулять воздух архитектура",
    "bar": "бар напитки ночь ночная активность",
}
# Keep the existing corpus wording stable; only new source types need labels.
for _category_id, _category_spec in CATEGORY_CATALOG.items():
    for _source_category in _category_spec["source_categories"]:
        CATEGORY_TEXT.setdefault(_source_category, _category_spec["ru_label"] + " " + _category_spec["google_query"])


DEFAULT_VISIT = {
    "park": 20, "playground": 20, "cafe": 25, "restaurant": 35,
    "museum": 45, "attraction": 20, "monument": 15, "viewpoint": 15,
    "theatre": 15, "mall": 20, "promenade": 20, "bar": 30,
}
MIN_VISIT = {"park": 10, "playground": 15, "cafe": 15, "restaurant": 25, "museum": 30, "attraction": 10, "monument": 10, "viewpoint": 10, "theatre": 10, "mall": 15, "promenade": 10, "bar": 20}
STOPWORDS = set("а и в на у по из со с к для мне меня мой моя есть хочу нужно возле около рядом минут мин час часа часов провести времени время давай где можно the a an to near my".split())


def tokens(text: str) -> list[str]:
    """Light stemming keeps Russian inflections useful without dependencies."""
    result = []
    for word in re.findall(r"[\w]+", text.lower().replace("ё", "е"), re.UNICODE):
        if word in STOPWORDS or word.isdecimal():
            continue
        # Deliberately conservative: a lexical index remains inspectable.
        if len(word) > 5:
            word = re.sub(r"(иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ая|яя|ое|ее|ые|ие|ой|ей|ов|ев|ах|ях|ом|ем|ам|ям|ы|и|а|я|у|ю|е)$", "", word)
        result.append(word)
    return result


def distance_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(h)))


def walk_distance_m(a: dict[str, Any], b: dict[str, Any]) -> int:
    return math.ceil(distance_m(a, b) * PATH_FACTOR)


def walk_minutes(a: dict[str, Any], b: dict[str, Any]) -> int:
    return math.ceil(walk_distance_m(a, b) / WALK_METRES_PER_MINUTE)


def _wanted(query: str, pattern: str) -> bool:
    """Recognize local, explicit negation instead of demanding 'без розетки'."""
    for match in re.finditer(pattern, query):
        prefix = query[max(0, match.start() - 35):match.start()]
        suffix = query[match.end():match.end() + 30]
        negated_prefix = re.search(r"(?:\bбез|\bне\s+(?:нуж\w*|хочу|надо|интерес\w*|обязательн\w*)|\bнеобязательн\w*|\bno|\bwithout)\s+(?:\w+\s+){0,2}$", prefix)
        negated_suffix = re.search(r"^\s+(?:не\s+(?:нуж\w*|надо|требу\w*|обязательн\w*)|необязательн\w*)", suffix)
        if not negated_prefix and not negated_suffix:
            return True
    return False



_CONFIRMED_HOURS_PATTERN = (
    r"(?<!\w)(?:24\s*/\s*7|круглосуточн\w*|open\s+now|24[\s-]*hours?|"
    r"открыт(?:о|а|ы)?\s+сейчас|(?:должен|должна|должно|должны)\s+быть\s+открыт\w*|"
    r"обязательно\s+открыт\w*)(?!\w)"
)


def requires_confirmed_hours(query: str) -> bool:
    """A stated opening requirement is stricter than the current time of day."""
    if not isinstance(query, str):
        return False
    normalized = query.casefold().replace("ё", "е")
    return (_wanted(normalized, _CONFIRMED_HOURS_PATTERN)
            and any(not _negated(normalized, match.start(), match.end())
                    for match in re.finditer(_CONFIRMED_HOURS_PATTERN, normalized)))


def night_hours_eligible(place: dict, *, night: bool, allow_unknown_outdoor_hours: bool = False,
                         require_confirmed_hours: bool = False, schedule_when=None) -> bool:
    """Return availability eligibility, never an assertion of live access.

    Unknown park/promenade hours can be an explicitly opted-in conditional idea.
    An explicit opening requirement always needs the exact own-source 24/7 fact.
    Other types, specified non-24/7 schedules and restricted access stay strict.
    """
    if (not isinstance(place, dict) or type(night) is not bool
            or type(allow_unknown_outdoor_hours) is not bool or type(require_confirmed_hours) is not bool):
        return False
    tags = place.get("source_tags", {})
    if not isinstance(tags, dict):
        return False
    for access in (tags.get("access"), place.get("access_evidence")):
        if access is not None and (not isinstance(access, str) or access.strip().casefold() in {"private", "no"}):
            return False
    hours = place.get("opening_hours")
    if require_confirmed_hours:
        return hours == "24/7"
    if schedule_when is not None:
        from .opening_hours import evaluate_schedule
        status = evaluate_schedule(hours, schedule_when)["status"]
        if status == "closed":
            return False
        if status == "open":
            return True
    if not night or hours == "24/7":
        return True
    return (allow_unknown_outdoor_hours and place.get("category") in {"park", "promenade"}
            and (hours is None or hours == ""))

def detect_intent(query: str, time_of_day: str = "day", *, intent_plan=None) -> dict[str, Any]:
    if intent_plan is not None:
        result = _planned_intent(intent_plan, time_of_day)
        result["require_confirmed_hours"] = requires_confirmed_hours(query)
        return result
    query = query.lower().replace("ё", "е")
    search = parse_search_intent(query)
    positives = list(search["categories"])
    requested = positives[:1] if search["mode"] == "single_venue" else positives
    excluded = list(search["excluded_categories"])
    night_query = query
    for pattern in CATEGORY_CATALOG["nightclub"]["aliases"]:
        night_query = re.sub(r"(?<!\w)(?:" + pattern + r")(?!\w)", " ", night_query)
    return {
        "coffee": "cafe" in requested,
        "socket": _wanted(query, r"розет\w*|заряд\w*|socket|power outlet"),
        "wifi": any(not _negated(query, match.start(), match.end())
                    for match in re.finditer(r"(?<!\w)(?:" + WIFI_PATTERN + r")(?!\w)", query)),
        "quiet": _wanted(query, r"тих\w*|спокой\w*|quiet|тыныш"),
        "strict_quiet": _wanted(query, r"тих\w*|quiet|тыныш") and bool(re.search(r"(?:только|обязательно|only|must).{0,20}(?:тих|quiet|тыныш)", query)),
        "child": _wanted(query, r"ребен\w*|деть\w*|дети|детск\w*|семь[яеюи]\w*|семейн\w*|family|child\w*|kids|бала\w*|отбасы"),
        "tourist": _wanted(query, r"турист\w*|достопримеч\w*|архитект\w*|tourist|sightsee\w*"),
        "architecture": _wanted(query, r"архитект\w*|architecture|сәулет\w*"),
        "night": time_of_day == "night" or _wanted(night_query, r"ноч\w*|night|түн\w*"),
        "require_confirmed_hours": requires_confirmed_hours(query),
        "accessible": _wanted(query, r"коляс\w*|безбарьер\w*|wheelchair|accessible|арбамен"),
        "outdoor": _wanted(query, r"прогул\w*|погуля\w*|гуля\w*|walk\w*|серуен\w*|воздух"),
        "preferred_categories": requested,
        "activity_mode": search["mode"],
        "unsupported_activity": not search["supported"],
        "primary_category": positives[0] if positives else None,
        "requested_categories": positives,
        "search_intent": search,
        "excluded_categories": excluded,
        "time_of_day": time_of_day,
        "general_suggestion": bool(re.search(r"куда|где|посовету\w*|рядом|near\w*|where|suggest\w*", query)),
    }


def _planned_intent(plan, time_of_day="day"):
    """Project one already validated plan; never reparse the original message."""
    # Lazy import keeps deterministic plan construction independent of adapters.
    from .semantic_intent import IntentPlan
    if not isinstance(plan, IntentPlan):
        raise ValueError("Expected one validated IntentPlan")
    intent = plan.retrieval_intent(time_of_day)
    intent.update({"open_goal": plan.open_goal, "source_phrase": plan.search_text,
                   "required_keys": list(plan.global_required_keys),
                   "semantic_goal": deepcopy(plan.sanitized_narration)})
    return intent


def _source_label(value):
    """Literal type-label identity with small grammatical inflections only.

    This is not a semantic similarity score. Whole token sequences must agree;
    a venue name, description, provider query or Google prose is never evidence.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        return ()
    return tuple(re.findall(r"[^\W_]+", value.casefold().replace("ё", "е")))


def _labels_match(left, right):
    if not left or len(left) != len(right):
        return False
    # Small grammatical endings preserve label identity. This does not match
    # abbreviations, translations, different token counts or semantic neighbours.
    endings = {"", "ы", "и", "а", "я", "у", "ю", "е", "ий", "ии", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ей", "ов", "ев", "ах", "ях", "ом", "ем", "ам", "ям", "ами", "ями", "ыми", "ими", "ого", "ему", "ому", "иями"}
    for first, second in zip(left, right):
        if first == second:
            continue
        if first.isascii() and second.isascii():
            if min(len(first), len(second)) >= 5 and (first.endswith("s") and first[:-1] == second or second.endswith("s") and second[:-1] == first):
                continue
            return False
        if not any(first[:cut] == second[:cut] and first[cut:] in endings and second[cut:] in endings
                   for cut in range(3, min(len(first), len(second)) + 1)):
            return False
    return True


def _planned_architecture_evidence(place, actual):
    """A planned architectural branch needs own structure/type, not a museum's mere existence."""
    if actual.intersection({"monument", "architecture"}):
        return True
    tags = place.get("source_tags", {})
    if not isinstance(tags, dict):
        return False
    if tags.get("historic") in {"monument", "memorial", "castle", "building"}:
        return True
    return any(isinstance(tags.get(key), str) and tags[key].strip().casefold() not in
               {"", "no", "unknown", "none"} for key in ("building:architecture", "architectural_style"))


def matched_goal_indices(place, plan_or_intent):
    """Return original goal indices whose own type AND effective conditions match.

    Use before route/model calls. Unknown mandatory activity, equipment, stock
    or price is not established by a venue category, even when that type fits.
    No raw provider body is retained and no source field is rewritten here.
    """
    if not isinstance(place, dict):
        return ()
    intent = plan_or_intent if isinstance(plan_or_intent, dict) else _planned_intent(plan_or_intent)
    global_required = set(intent.get("required_keys", ()))
    for key, flag in (("socket", "socket"), ("wifi", "wifi"), ("accessible", "accessible"), ("quiet", "strict_quiet")):
        if intent.get(flag):
            global_required.add(key)

    def conditions(keys):
        if keys & {"activity", "equipment", "inventory", "price"}:
            return ()  # No affirmative structured service proof is imported.
        return all(feature(place, field) == "yes" for key, field in
                   (("socket", "socket"), ("wifi", "wifi"), ("quiet", "quiet"), ("accessible", "wheelchair")) if key in keys)

    if not conditions(global_required):
        return ()
    semantic = intent.get("semantic_goal")
    if not isinstance(semantic, dict):
        return (0,)  # Legacy eligibility remains owned by its existing filters.
    goals = semantic.get("goals", [])
    if not isinstance(goals, list) or not 1 <= len(goals) <= 3:
        return ()
    policy = semantic.get("goal_policy")
    if policy not in {"primary", "any", "walk_mix"}:
        return ()
    branch_required = [set(global_required) for _ in goals]
    records = semantic.get("requirements", [])
    if not isinstance(records, list):
        return ()
    for item in records:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str) or item.get("key") not in
                {"socket", "wifi", "quiet", "accessible", "activity", "equipment", "inventory", "price"}
                or type(item.get("required")) is not bool):
            return ()
        index = item.get("goal_index")
        if index is not None and (type(index) is not int or not 0 <= index < len(goals)):
            return ()
        if item["required"]:
            for position, required in enumerate(branch_required):
                if index is None or position == index:
                    required.add(item["key"])
    active = range(1) if policy == "primary" else range(len(goals))
    actual = {place.get("category")}
    for field in ("source_categories", "source_types"):
        values = place.get(field, [])
        if isinstance(values, (list, tuple)):
            actual.update(value for value in values if isinstance(value, str))
    labels = set()
    for field in ("source_rubrics", "source_types"):
        values = place.get(field, [])
        if isinstance(values, (list, tuple)):
            labels.update(_source_label(value) for value in values if isinstance(value, str))
    labels.update(_source_label(value) for value in actual if isinstance(value, str) and value not in {"unknown", "branch", "adm_div.place"})
    source_type = place.get("source_type")
    if isinstance(source_type, str) and source_type not in {"branch", "adm_div.place", "unknown"}:
        labels.add(_source_label(source_type))
    tags = place.get("source_tags", {})
    if isinstance(tags, dict):
        labels.update(_source_label(tags.get(key)) for key in ("amenity", "leisure", "tourism", "shop", "sport"))
    labels.discard(())
    matched = []
    for index in active:
        goal = goals[index]
        if not isinstance(goal, dict):
            continue
        if goal.get("fulfilment") == "specific_activity" or not conditions(branch_required[index]):
            continue
        category = goal.get("category_hint")
        if category is not None and (not isinstance(category, str) or category not in CATEGORY_CATALOG):
            continue
        matches = False
        if category == "walk" or goal.get("fulfilment") == "generic_walk" and category is None:
            allowed = set(CATEGORY_CATALOG["walk"]["source_categories"])
            if intent.get("tourist"):
                allowed = allowed - {"playground"} | {"museum"}
            matches = bool(actual.intersection(allowed))
            # This untyped Google preview already passed the upstream broad
            # source path. It cannot establish a requested type or amenity.
            matches = matches or bool(place.get("provider") == "google" and place.get("category") == "unknown"
                    and isinstance(place.get("id"), str) and place.get("stop_id") == "google:" + place["id"]
                    and isinstance(place.get("source_id"), str) and re.fullmatch(r"g\d+", place["source_id"])
                    and not intent.get("child") and not intent.get("night")
                    and not intent.get("require_confirmed_hours") and not intent.get("excluded_categories"))
        elif category == "architecture":
            matches = bool(actual.intersection(CATEGORY_CATALOG[category]["source_categories"])
                           and _planned_architecture_evidence(place, actual))
        elif category in CATEGORY_CATALOG:
            matches = bool(actual.intersection(CATEGORY_CATALOG[category]["source_categories"]))
        else:
            matches = any(_labels_match(_source_label(goal.get("text")), label) for label in labels)
        if matches:
            matched.append(index)
    return tuple(matched)


def source_goal_evidence(place, plan_or_intent):
    """Strict bool compatibility wrapper; alternative relaxation is explicit elsewhere."""
    return bool(matched_goal_indices(place, plan_or_intent))


def _source_categories(identifiers):
    return {source for identifier in identifiers
            for source in CATEGORY_CATALOG[identifier]["source_categories"]}


def feature(poi: dict[str, Any], name: str) -> str:
    """Only explicit source fields count as affirmative evidence."""
    value = poi.get("features", {}).get(name, "unknown")
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value).lower()


def _place_keys(poi: dict) -> set[str]:
    """Use source aliases, never guess cross-language identities by location."""
    keys = set()
    for name in [poi["name"]] + poi.get("aliases", []):
        compact = "".join(re.findall(r"[\w]+", name.lower().replace("ё", "е")))
        keys.add(compact)
        # Geographic suffixes in an alias do not define a different venue.
        if compact.endswith("astana") and len(compact) > 10:
            keys.add(compact[:-6])
        if compact.endswith("астана") and len(compact) > 10:
            keys.add(compact[:-6])
    return keys - {""}


def architecture_evidence(poi: dict) -> bool:
    if poi["category"] in {"monument", "museum", "theatre"}:
        return True
    if poi["category"] != "attraction":
        return False
    tags = poi.get("source_tags", {})
    if tags.get("historic") in {"monument", "memorial", "castle", "building"}:
        return True
    source_names = " ".join([poi["name"]] + poi.get("aliases", [])).lower()
    return bool(re.search(r"\b(?:мост\w*|bridge|арк[а-я]*|arch|дворец\w*|palace|башн\w*|tower|сфер\w*|sphere)\b", source_names))


class Retriever:
    def __init__(self, data_path: str | Path | None = None, *, data: dict | None = None):
        self.data_path = None
        self.data_sha256 = None
        if data is None:
            self.data_path = Path(data_path or DEFAULT_DATA).resolve()
            raw = self.data_path.read_bytes()
            self.data_sha256 = hashlib.sha256(raw).hexdigest()
            data = json.loads(raw.decode("utf-8-sig"))
        self._validate_data(data)
        self.dataset = data.get("dataset", {})
        self.places = data["places"]
        self.duplicate_of = self._physical_duplicates()
        self.documents = []
        for poi in self.places:
            text = " ".join([
                poi["name"], poi.get("description", ""),
                " ".join(poi.get("aliases", [])),
                CATEGORY_TEXT.get(poi["category"], poi["category"]),
            ])
            self.documents.append(Counter(tokens(text)))
        self.lengths = [sum(doc.values()) for doc in self.documents]
        self.average_length = sum(self.lengths) / max(1, len(self.lengths))
        self.frequencies = Counter(term for doc in self.documents for term in doc)

    def _physical_duplicates(self) -> dict[str, str]:
        parents = list(range(len(self.places)))
        def find(index):
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index
        buckets = {}
        for index, poi in enumerate(self.places):
            for key in _place_keys(poi):
                bucket = buckets.setdefault((poi["category"], key), [])
                for other in bucket:
                    if distance_m(poi, self.places[other]) < 80:
                        parents[find(index)] = find(other)
                bucket.append(index)
        groups = {}
        for index in range(len(self.places)):
            groups.setdefault(find(index), []).append(index)
        duplicates = {}
        for indices in groups.values():
            def evidence_quality(index):
                poi = self.places[index]
                tags = poi.get("source_tags", {})
                evidence = bool(poi.get("opening_hours")) + sum(feature(poi, key) != "unknown" for key in ("socket", "quiet", "wheelchair")) + sum(bool(tags.get(key)) for key in ("addr:street", "addr:housenumber"))
                return evidence, str(poi.get("source_updated_at") or ""), poi["id"]
            canonical = max(indices, key=evidence_quality)
            for index in indices:
                if index != canonical:
                    duplicates[self.places[index]["id"]] = self.places[canonical]["id"]
        return duplicates

    @staticmethod
    def _validate_data(data: object) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("dataset", {}), dict) or not isinstance(data.get("places"), list):
            raise ValueError("POI dataset: ожидаются dataset object и places array.")
        if len(data["places"]) > 100_000:
            raise ValueError("POI dataset: слишком много объектов.")
        if type(data.get("dataset", {}).get("is_demo", False)) is not bool:
            raise ValueError("POI dataset: is_demo должен быть boolean.")
        seen = set()
        for index, poi in enumerate(data["places"]):
            error = f"POI #{index}: "
            if not isinstance(poi, dict):
                raise ValueError(error + "ожидается object.")
            for field in ("id", "name", "category", "description", "source_url"):
                if not isinstance(poi.get(field), str) or not poi[field].strip() or len(poi[field]) > 4000:
                    raise ValueError(error + field + " должен быть непустым текстом до 4000 символов.")
            if poi["id"] in seen:
                raise ValueError(error + "дублирующий id.")
            seen.add(poi["id"])
            for field, low, high in (("lat", -90, 90), ("lon", -180, 180)):
                value = poi.get(field)
                if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
                    raise ValueError(error + "неверная координата " + field)
            parsed = urlparse(poi["source_url"])
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(error + "source_url должен быть безопасным HTTP(S) URL.")
            if not isinstance(poi.get("features", {}), dict):
                raise ValueError(error + "features должен быть object.")
            if not isinstance(poi.get("source_tags", {}), dict):
                raise ValueError(error + "source_tags должен быть object.")
            aliases = poi.get("aliases", [])
            if not isinstance(aliases, list) or len(aliases) > 100 or any(not isinstance(value, str) for value in aliases):
                raise ValueError(error + "aliases должен быть массивом строк.")
            if poi.get("visit_minutes") is not None and (type(poi["visit_minutes"]) is not int or not 1 <= poi["visit_minutes"] <= 240):
                raise ValueError(error + "visit_minutes должен быть целым числом 1–240.")
            if poi.get("opening_hours") is not None and (not isinstance(poi["opening_hours"], str) or len(poi["opening_hours"]) > 500):
                raise ValueError(error + "неверный opening_hours.")

    def lexical_score(self, index: int, terms: list[str]) -> float:
        doc = self.documents[index]
        score = 0.0
        for term in set(terms):
            frequency = doc.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + (len(self.documents) - self.frequencies[term] + 0.5) / (self.frequencies[term] + 0.5))
            normalizer = frequency + 1.5 * (1 - 0.75 + 0.75 * self.lengths[index] / max(self.average_length, 1))
            score += idf * frequency * 2.5 / normalizer
        return score

    def retrieve(self, query: str, origin: dict, minutes: int, time_of_day: str = "day", *, intent_plan=None, allow_unknown_outdoor_hours: bool = False, schedule_when=None, deadline=None) -> dict:
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError("Invalid retrieval deadline")
        def check_deadline():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Source retrieval deadline exceeded")
        check_deadline()
        intent = detect_intent(query, time_of_day, intent_plan=intent_plan)
        candidates = []
        filtered = Counter()
        terms = tokens(intent.get("source_phrase", query))
        preferred = _source_categories(intent["preferred_categories"])
        explicit_sources = _source_categories([identifier for identifier in intent["preferred_categories"]
                                               if identifier not in {"walk", "architecture"}])
        excluded = _source_categories(intent["excluded_categories"])
        allowed_requested = (explicit_sources or preferred) if intent["activity_mode"] == "single_venue" else (
            preferred | {"monument", "theatre", "museum", "attraction"}
            if preferred and intent["architecture"] else preferred)
        outdoor_categories = {"park", "playground", "promenade", "monument", "attraction", "viewpoint", "theatre"}
        tourist_categories = (outdoor_categories - {"playground"}) | {"museum"}
        check_deadline()
        for index, poi in enumerate(self.places):
            check_deadline()
            category = poi["category"]
            if intent["unsupported_activity"]:
                filtered["unsupported_activity"] += 1
                continue
            if intent_plan is not None and not source_goal_evidence(poi, intent):
                filtered["no_verified_match"] += 1
                continue
            if poi["id"] in self.duplicate_of:
                filtered["duplicate_source"] += 1
                continue
            if category in excluded:
                filtered["excluded_category"] += 1
                continue
            if intent_plan is None and allowed_requested and category not in allowed_requested:
                filtered["category"] += 1
                continue
            if intent_plan is None and not preferred and intent["outdoor"] and not intent["tourist"] and category not in outdoor_categories:
                filtered["outdoor"] += 1
                continue
            if poi.get("source_tags", {}).get("access") in {"no", "private"}:
                filtered["private_access"] += 1
                continue
            if intent["socket"] and feature(poi, "socket") != "yes":
                filtered["socket"] += 1
                continue
            if intent.get("wifi") and feature(poi, "wifi") != "yes":
                filtered["wifi"] += 1
                continue
            if intent["accessible"] and feature(poi, "wheelchair") != "yes":
                filtered["accessible"] += 1
                continue
            if intent["strict_quiet"] and feature(poi, "quiet") != "yes":
                filtered["quiet"] += 1
                continue
            if intent["child"] and category in {"bar", "nightclub"}:
                continue
            if not night_hours_eligible(poi, night=intent["night"],
                    allow_unknown_outdoor_hours=allow_unknown_outdoor_hours,
                    require_confirmed_hours=intent["require_confirmed_hours"], schedule_when=schedule_when):
                filtered["night" if intent["night"] else "required_hours"] += 1
                continue
            if intent_plan is None and intent["tourist"] and not preferred and category not in tourist_categories:
                continue
            if intent_plan is None and intent["architecture"] and category not in explicit_sources and not architecture_evidence(poi):
                filtered["architecture_evidence"] += 1
                continue
            visit = int(poi.get("visit_minutes") or DEFAULT_VISIT.get(category, 20))
            walking = walk_minutes(origin, poi)
            # Visit duration is a proposed activity, not an opening-hours fact.
            # A short exterior stop may shrink, but never below a useful minimum.
            visit = min(visit, max(MIN_VISIT.get(category, 10), minutes - walking * 2))
            if walking * 2 + visit > minutes:
                filtered["budget"] += 1
                continue
            lexical = self.lexical_score(index, terms)
            preference = 0.0
            if intent["child"] and category in {"playground", "park"}:
                preference += 2.5
            if intent["quiet"] and feature(poi, "quiet") == "yes":
                preference += 2.0
            # Distance contributes smoothly; semantics dominate among nearby POI.
            # Category expansion is not source-specific knowledge. Bound BM25
            # so a rare generic category cannot outweigh a much closer match.
            semantic = 2 * lexical / (1 + lexical)
            score = semantic + preference + 2 / (1 + walking / 12)
            candidates.append({"poi": poi, "score": score, "lexical_score": lexical, "visit_minutes": visit})
        relevant_intent = any(intent[key] for key in ("coffee", "socket", "wifi", "quiet", "child", "tourist", "night", "accessible", "outdoor", "general_suggestion")) or bool(preferred)
        if candidates and not relevant_intent and not any(item["lexical_score"] > 0 for item in candidates):
            filtered["no_text_match"] = len(candidates)
            candidates = []
        check_deadline()
        candidates.sort(key=lambda item: (-item["score"], item["poi"]["id"]))
        check_deadline()
        # Fair ranking: no paid/sponsor/rating fields. Diversity prevents a dense
        # chain of identical POI dominating a broad walk. Hard filters stay hard.
        selected = []
        elapsed = 0
        last = origin
        remaining = list(candidates)
        while remaining and len(selected) < 3:
            check_deadline()
            def diversity_score(item):
                check_deadline()
                same_category = sum(other["poi"]["category"] == item["poi"]["category"] for other in selected)
                penalty = 1 if len(allowed_requested) == 1 else 1 + 0.8 * same_category
                return item["score"] / penalty
            remaining.sort(key=lambda item: (-diversity_score(item), item["poi"]["id"]))
            check_deadline()
            candidate = remaining.pop(0)
            poi = candidate["poi"]
            if any(tokens(poi["name"]) == tokens(item["poi"]["name"]) and distance_m(poi, item["poi"]) < 80 for item in selected):
                filtered["duplicate_place"] += 1
                continue
            leg = walk_minutes(last, poi)
            if elapsed + leg + candidate["visit_minutes"] + walk_minutes(poi, origin) > minutes:
                continue
            candidate = {**candidate, "leg_walk_minutes": leg, "leg_distance_m": walk_distance_m(last, poi)}
            selected.append(candidate)
            elapsed += leg + candidate["visit_minutes"]
            last = poi
        # Optimize stop order exactly for this tiny itinerary (at most 3! = 6).
        if len(selected) > 1:
            def route_cost(order):
                check_deadline()
                route = [origin] + [item["poi"] for item in order] + [origin]
                return sum(walk_minutes(a, b) for a, b in zip(route, route[1:]))
            selected = list(min(permutations(selected), key=route_cost))
            last = origin
            elapsed = 0
            for item in selected:
                item["leg_walk_minutes"] = walk_minutes(last, item["poi"])
                item["leg_distance_m"] = walk_distance_m(last, item["poi"])
                elapsed += item["leg_walk_minutes"] + item["visit_minutes"]
                last = item["poi"]
        return_minutes = walk_minutes(last, origin) if selected else 0
        plan = {
            "total_minutes": elapsed + return_minutes,
            "walk_minutes": sum(item["leg_walk_minutes"] for item in selected) + return_minutes,
            "visit_minutes": sum(item["visit_minutes"] for item in selected),
            "return_walk_minutes": return_minutes,
            "budget_minutes": minutes,
            "distance_m": sum(item["leg_distance_m"] for item in selected) + (walk_distance_m(last, origin) if selected else 0),
            "returns_to_origin": True,
            "method": "haversine × 1.3; 75 м/мин; без дорожного графа",
        }
        check_deadline()
        return {"selected": selected, "intent": intent, "filtered": dict(filtered), "plan": plan, "retrieved_count": len(candidates), "ranking_policy": "bounded BM25 + walking distance + request context; diversity; alias/proximity deduplication 80m; no paid boosts"}
