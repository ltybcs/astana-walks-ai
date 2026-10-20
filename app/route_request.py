"""Pure rejection of explicit public A-to-B requests; never a routed ETA."""
from __future__ import annotations

import re
import unicodedata

from .activity_intent import _clause_prefix
from .google_budget import GoogleError
from .lab_context import ORIGIN_PATTERNS, PUBLIC_ORIGINS
from .retrieval import distance_m


# The context aliases contain broad inflection wildcards. For an endpoint,
# accept closed case endings rather than a differently named compound venue.
_CASE_ENDING = r"(?:иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ая|яя|ое|ее|ые|ие|ую|юю|ой|ей|ов|ев|ах|ях|ом|ем|ам|ям|ым|им|а|я|у|ю|е|и|ы)?"
_ANCHORS = {key: re.compile(pattern.replace(r"\w*", _CASE_ENDING))
            for key, pattern in ORIGIN_PATTERNS.items() if key in PUBLIC_ORIGINS}
_FROM = re.compile(r"(?<!\w)(?:от|из|from)(?!\w)\s+(?:the\s+)?")
_TO = re.compile(r"\s+(?:до|to)(?!\w)\s+(?:the\s+)?")


def _at(query, position):
    return [(key, match.end()) for key, pattern in _ANCHORS.items()
            if (match := pattern.match(query, position)) is not None]


def _route_negated(query, start, end):
    # Negate the route itself, not uncertainty or walking without a car.
    prefix = _clause_prefix(query, start)
    return bool(re.search(
        r"(?:\bне|\bnot|\b(?:do\s+not|don't))\s+"
        r"(?:(?:хочу|хотел\w*|нужен|нужна|нужно|надо|пойду|иду|планирую|собираюсь|"
        r"идти|ехать|пройти|маршрут|путь|пеш\w*|want|need|go|walk|walking|route|to|a|the)\s+){0,5}$", prefix
    ) or re.match(
        r"\s+(?:я\s+)?не\s+(?:пойду|планирую|собираюсь|хочу|нужен|нужна|нужно)(?!\w)", query[end:]
    ))


def _unsupported():
    return GoogleError(
        "unsupported_point_to_point",
        "Пока доступны прогулки с возвращением к старту; маршрут между двумя разными точками не построен.",
        422,
    )


def validate_walking_request(query, budget_options):
    """Reject a known non-circular goal before extraction or external search.

    Distance is a straight-line lower bound with a generous 150m/min ceiling,
    not a Google route, walking-time estimate or flight-arrival guarantee.
    """
    if (not isinstance(query, str) or not isinstance(budget_options, (list, tuple))
            or not budget_options or len(budget_options) > 3
            or any(type(value) is not int or not 10 <= value <= 480 for value in budget_options)):
        raise ValueError("Walking preflight requires validated query and minute budgets")
    text = unicodedata.normalize("NFKC", query).casefold().replace("ё", "е").replace("’", "'")
    pairs = []
    for beginning in _FROM.finditer(text):
        starts = _at(text, beginning.end())
        if len(starts) != 1:
            continue
        first, end = starts[0]
        connector = _TO.match(text, end)
        if connector is None:
            continue
        finishes = _at(text, connector.end())
        if len(finishes) != 1:
            continue
        last, end = finishes[0]
        # Rejected route clauses are not the route the person requests.
        if _route_negated(text, beginning.start(), end):
            continue
        pairs.append((first, last))
    if not pairs or (len(pairs) == 1 and pairs[0][0] == pairs[0][1]):
        return None
    if len(pairs) != 1:
        raise _unsupported()
    first, last = pairs[0]
    budget = max(budget_options)
    if distance_m(PUBLIC_ORIGINS[first], PUBLIC_ORIGINS[last]) > budget * 150:
        raise GoogleError(
            "route_does_not_fit",
            f"Пеший маршрут между указанными точками не помещается в {budget} мин; дополнительные остановки не подбирались.",
            422,
        )
    raise _unsupported()
