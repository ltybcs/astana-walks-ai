"""Grounded POI retrieval and a conservative, time-bounded walking plan.

This is lexical RAG (BM25), not an embedding or live-routing service. All
distance and duration estimates are derived locally, never stated as live facts.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "poi.sample.json"
WALK_METRES_PER_MINUTE = 75
PATH_FACTOR = 1.3
CATEGORY_TEXT = {
    "park": "парк сад природа зелень прогулка гулять воздух ребёнок дети семья",
    "playground": "детская площадка ребёнок дети семья играть",
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


def detect_intent(query: str, time_of_day: str = "day") -> dict[str, bool]:
    query = query.lower().replace("ё", "е")
    return {
        "coffee": bool(re.search(r"коф|кафе|coffee|cafe", query)),
        "socket": bool(re.search(r"розет|заряд|socket|power outlet", query)),
        "quiet": bool(re.search(r"тих|спокой|quiet", query)),
        "child": bool(re.search(r"ребен|деть|дети|детск|семь|family|child|kids", query)),
        "tourist": bool(re.search(r"турист|достопримеч|архитект|tourist|sightsee", query)),
        "night": time_of_day == "night" or bool(re.search(r"ноч|night", query)),
        "accessible": bool(re.search(r"коляс|безбарьер|wheelchair", query)),
    }


def feature(poi: dict[str, Any], name: str) -> str:
    """Only explicit source fields count as affirmative evidence."""
    value = poi.get("features", {}).get(name, "unknown")
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value).lower()


class Retriever:
    def __init__(self, data_path: str | Path | None = None, *, data: dict | None = None):
        if data is None:
            with Path(data_path or DEFAULT_DATA).open(encoding="utf-8-sig") as handle:
                data = json.load(handle)
        self.dataset = data.get("dataset", {})
        self.places = data["places"]
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
        for index, poi in enumerate(self.places):
            category = poi["category"]
            if intent["coffee"] and category != "cafe":
                continue
            if intent["socket"] and feature(poi, "socket") != "yes":
                filtered["socket"] += 1
                continue
            if intent["accessible"] and feature(poi, "wheelchair") != "yes":
                filtered["accessible"] += 1
                continue
            if intent["child"] and category == "bar":
                continue
            if intent["night"] and poi.get("opening_hours") != "24/7":
                filtered["night"] += 1
                continue
            if intent["tourist"] and category not in {"attraction", "monument", "viewpoint", "museum", "promenade", "park", "theatre"}:
                continue
            visit = int(poi.get("visit_minutes") or DEFAULT_VISIT.get(category, 20))
            walking = walk_minutes(origin, poi)
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
            score = lexical + preference + 1 / (1 + walking / 15)
            candidates.append({"poi": poi, "score": score, "lexical_score": lexical, "visit_minutes": visit})
        candidates.sort(key=lambda item: (-item["score"], item["poi"]["id"]))
        # Greedy itinerary: each added stop must leave time to return to origin.
        selected = []
        elapsed = 0
        last = origin
        for candidate in candidates:
            poi = candidate["poi"]
            leg = walk_minutes(last, poi)
            if elapsed + leg + candidate["visit_minutes"] + walk_minutes(poi, origin) > minutes:
                continue
            candidate = {**candidate, "leg_walk_minutes": leg, "leg_distance_m": walk_distance_m(last, poi)}
            selected.append(candidate)
            elapsed += leg + candidate["visit_minutes"]
            last = poi
            if len(selected) == 3:
                break
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
        return {"selected": selected, "intent": intent, "filtered": dict(filtered), "plan": plan, "retrieved_count": len(candidates)}
