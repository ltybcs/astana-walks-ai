"""Grounded POI retrieval and a conservative, time-bounded walking plan.

This is lexical RAG (BM25), not an embedding or live-routing service. All
distance and duration estimates are derived locally, never stated as live facts.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from itertools import permutations
from urllib.parse import urlparse

from .activity_intent import activity_intent, COMPUTER_CLUB_PATTERN


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


def detect_intent(query: str, time_of_day: str = "day") -> dict[str, Any]:
    query = query.lower().replace("ё", "е")
    category_patterns = {
        "cafe": r"коф\w*|кафе|coffee|caf[eé]|кофе",
        "park": r"парк\w*|\bсад\w*|park|garden|саябақ",
        "playground": r"детск\w*\s+площад\w*|playground|ойын\s+алаң",
        "museum": r"музе\w*|museum|мұражай",
        "restaurant": r"ресторан\w*|поесть|обед\w*|ужин\w*|restaurant|food",
        "promenade": r"набереж\w*|бульвар\w*|promenade|riverside",
        "theatre": r"театр\w*|балет\w*|theatre|theater",
        "bar": r"\bбар\w*|\bпаб\w*|\bpub\b|\bbar\b",
        "mall": r"шопинг|покуп\w*|shopping",
    }
    category_patterns["computer_club"] = COMPUTER_CLUB_PATTERN
    requested = [category for category, pattern in category_patterns.items()
                 if category != "computer_club" and _wanted(query, pattern)]
    activity = activity_intent(query, requested)
    if activity["categories"]:
        requested = activity["categories"]
    excluded = [category for category, pattern in category_patterns.items() if re.search(pattern, query) and not _wanted(query, pattern)]
    if not activity["categories"] and re.search(r"(?<!\w)(?:" + COMPUTER_CLUB_PATTERN + r")(?!\w)", query) and "computer_club" not in excluded:
        excluded.append("computer_club")
    return {
        "coffee": "cafe" in requested,
        "socket": _wanted(query, r"розет\w*|заряд\w*|socket|power outlet"),
        "quiet": _wanted(query, r"тих\w*|спокой\w*|quiet|тыныш"),
        "strict_quiet": _wanted(query, r"тих\w*|quiet|тыныш") and bool(re.search(r"(?:только|обязательно|only|must).{0,20}(?:тих|quiet|тыныш)", query)),
        "child": _wanted(query, r"ребен\w*|деть\w*|дети|детск\w*|семь[яеюи]\w*|семейн\w*|family|child\w*|kids|бала\w*|отбасы"),
        "tourist": _wanted(query, r"турист\w*|достопримеч\w*|архитект\w*|tourist|sightsee\w*"),
        "architecture": _wanted(query, r"архитект\w*|architecture|сәулет\w*"),
        "night": time_of_day == "night" or _wanted(query, r"ноч\w*|night|түн\w*"),
        "accessible": _wanted(query, r"коляс\w*|безбарьер\w*|wheelchair|accessible|арбамен"),
        "outdoor": _wanted(query, r"прогул\w*|погуля\w*|гуля\w*|walk\w*|серуен\w*|воздух"),
        "preferred_categories": requested,
        "activity_mode": activity["mode"],
        "unsupported_activity": activity["unsupported"],
        "excluded_categories": excluded,
        "time_of_day": time_of_day,
        "general_suggestion": bool(re.search(r"куда|где|посовету\w*|рядом|near\w*|where|suggest\w*", query)),
    }


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

    def retrieve(self, query: str, origin: dict, minutes: int, time_of_day: str = "day") -> dict:
        intent = detect_intent(query, time_of_day)
        candidates = []
        filtered = Counter()
        terms = tokens(query)
        preferred = set(intent["preferred_categories"])
        allowed_requested = preferred | {"monument", "theatre", "museum", "attraction"} if preferred and intent["architecture"] else preferred
        outdoor_categories = {"park", "playground", "promenade", "monument", "attraction", "viewpoint", "theatre"}
        tourist_categories = (outdoor_categories - {"playground"}) | {"museum"}
        for index, poi in enumerate(self.places):
            category = poi["category"]
            if intent["unsupported_activity"]:
                filtered["unsupported_activity"] += 1
                continue
            if poi["id"] in self.duplicate_of:
                filtered["duplicate_source"] += 1
                continue
            if category in intent["excluded_categories"]:
                filtered["excluded_category"] += 1
                continue
            if allowed_requested and category not in allowed_requested:
                filtered["category"] += 1
                continue
            if not preferred and intent["outdoor"] and not intent["tourist"] and category not in outdoor_categories:
                filtered["outdoor"] += 1
                continue
            if poi.get("source_tags", {}).get("access") in {"no", "private"}:
                filtered["private_access"] += 1
                continue
            if intent["socket"] and feature(poi, "socket") != "yes":
                filtered["socket"] += 1
                continue
            if intent["accessible"] and feature(poi, "wheelchair") != "yes":
                filtered["accessible"] += 1
                continue
            if intent["strict_quiet"] and feature(poi, "quiet") != "yes":
                filtered["quiet"] += 1
                continue
            if intent["child"] and category == "bar":
                continue
            if intent["night"] and poi.get("opening_hours") != "24/7":
                filtered["night"] += 1
                continue
            if intent["tourist"] and not preferred and category not in tourist_categories:
                continue
            if intent["architecture"] and category not in preferred and not architecture_evidence(poi):
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
        relevant_intent = any(intent[key] for key in ("coffee", "socket", "quiet", "child", "tourist", "night", "accessible", "outdoor", "general_suggestion")) or bool(preferred)
        if candidates and not relevant_intent and not any(item["lexical_score"] > 0 for item in candidates):
            filtered["no_text_match"] = len(candidates)
            candidates = []
        candidates.sort(key=lambda item: (-item["score"], item["poi"]["id"]))
        # Fair ranking: no paid/sponsor/rating fields. Diversity prevents a dense
        # chain of identical POI dominating a broad walk. Hard filters stay hard.
        selected = []
        elapsed = 0
        last = origin
        remaining = list(candidates)
        while remaining and len(selected) < 3:
            def diversity_score(item):
                same_category = sum(other["poi"]["category"] == item["poi"]["category"] for other in selected)
                penalty = 1 if len(allowed_requested) == 1 else 1 + 0.8 * same_category
                return item["score"] / penalty
            remaining.sort(key=lambda item: (-diversity_score(item), item["poi"]["id"]))
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
        return {"selected": selected, "intent": intent, "filtered": dict(filtered), "plan": plan, "retrieved_count": len(candidates), "ranking_policy": "bounded BM25 + walking distance + request context; diversity; alias/proximity deduplication 80m; no paid boosts"}
