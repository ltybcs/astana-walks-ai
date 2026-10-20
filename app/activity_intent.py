"""Bounded, shared leisure intent parser; no raw request leaves this module.

The five-field result is the contract for search consumers and a future semantic
parser. Unknown explicit goals fail closed instead of becoming a park search.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal, TypedDict

from .category_catalog import CATEGORY_CATALOG


class SearchIntent(TypedDict):
    categories: list[str]
    excluded_categories: list[str]
    preferences: list[str]
    mode: Literal["walk", "single_venue"]
    supported: bool


COMPUTER_CLUB_PATTERN = "|".join(CATEGORY_CATALOG["computer_club"]["aliases"])
_CATEGORY_PATTERNS = {
    identifier: re.compile(r"(?<!\w)(?:" + "|".join(spec["aliases"]) + r")(?!\w)")
    for identifier, spec in CATEGORY_CATALOG.items()
}
_PREFERENCES = {
    "quiet": r"тих\w*|тишин\w*|спокойн\w*|quiet|тыныш",
    "socket": r"розет\w*|заряд\w*|sockets?|power\s+outlets?",
    "accessible": r"коляс\w*|безбарьер\w*|wheelchair|accessible|арбамен",
}
_TARGET = re.compile(
    r"(?<!\w)(?:хочу|хотел\w*|пойду|сходить|ищу|найти|посетить|нужен|нужна|нужно|нужны|"
    r"want|need|find|visit|looking\s+for|would\s+like)(?!\w)\s+"
    r"(?:(?:бы|to|a|an)\s+)?(?:(?:в|на|to)\s+)?"
)
# These are generic requests, not evidence that any source has a new category.
_GENERIC = re.compile(
    r"(?:где\s+)?(?:погуля\w*|гуля\w*|пройтись)|"
    r"(?:(?:что[\s-]*нибудь|что[\s-]*то|что|нечто)\s+)?"
    r"(?:необычн\w*|интересн\w*)(?:\s+мест[оа])?|"
    r"мест[оа]|куда\s+сходить|где\s+провести\s+время|"
    r"(?:(?:необычн\w*|интересн\w*)\s+)?(?:(?:ночн\w*|вечерн\w*|дневн\w*)\s+)?активност\w*|"
    r"(?:an?\s+)?(?:interesting\s+)?places?|"
    r"something(?:\s+(?:interesting|unusual))?"
)
_PRIORITY = re.compile(r"\b(?:главное|главн\w*\s+цел\w*|основн\w*\s+цел\w*|main\s+goal|primary\s+goal)\b")
_PUBLIC_ANCHOR = re.compile(
    r"(?:мег[аи]|mega(?:\s+silk\s+way)?|байтерек\w*|бәйтерек\w*|"
    r"baiterek|bayterek|экспо(?:\s+2017)?|expo(?:\s+2017)?)"
)


def _normalize(query):
    return unicodedata.normalize("NFKC", query).casefold().replace("ё", "е").replace("’", "'")


def _clause_prefix(query, start):
    # Contrast and punctuation terminate the scope of a previous rejection.
    prefix = query[max(0, start - 90):start]
    return re.split(r"[,;.!?]|\b(?:но|зато|but|rather|instead)\b|\s+а\s+", prefix)[-1]


def _negated(query, start, end):
    prefix = _clause_prefix(query, start)
    suffix = query[end:end + 40]
    if re.search(r"(?:\bне\s+только|\bnot\s+only)\s+(?:\w+\s+){0,3}$", prefix):
        return False
    negative = re.search(
        r"(?:\bне|\bни|\bбез|\bне\s+(?:хочу|нуж\w*|надо|интерес\w*|обязательн\w*)|"
        r"\b(?:do\s+not|don't)\s+want|\bno|\bnot|\bwithout)"
        r"\s+(?:(?:в|на|a|an|the)\s+)?(?:\w+\s+){0,5}$", prefix
    )
    return bool(negative or re.match(
        r"\s+(?:не\s+(?:нуж\w*|хочу|надо|интерес\w*|требу\w*|обязательн\w*)|"
        r"(?:is|are)\s+not\s+(?:wanted|needed|required))\b", suffix
    ))


def _location_mention(query, start):
    prefix = _clause_prefix(query, start)
    goal_markers = list(_TARGET.finditer(prefix))
    if goal_markers:
        prefix = prefix[goal_markers[-1].end():]
    # A venue named as a landmark is context, not a second requested activity.
    return bool(re.search(
        r"(?:\bрядом\s+(?:с|со)|\bвозле|\bоколо|\bнеподалеку\s+от|\bу|"
        r"\bnear|\bnearby|\bnext\s+to|\bclose\s+to|\bя\s+(?:в|у))"
        r"\s+(?:\w+\s+){0,2}$", prefix
    ) or re.search(r"\b(?:еду|нахожусь|стою|сейчас)\s+(?:\w+\s+){0,4}(?:в|на|у)\s+(?:\w+\s+){0,2}$", prefix))


def _matches(query):
    candidates = [(match.start(), match.end(), identifier)
                  for identifier, pattern in _CATEGORY_PATTERNS.items()
                  for match in pattern.finditer(query)]
    # A compound activity owns its complete span: movie theater is not theatre,
    # pool halls are not pools, anti-cafe is not cafe, amusement park is not park.
    chosen = []
    for candidate in sorted(candidates, key=lambda item: (-(item[1]-item[0]), item[0], item[2])):
        if not any(candidate[0] < prior[1] and prior[0] < candidate[1] for prior in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda item: (item[0], item[1]))


def _known_goal_lead(prefix):
    # Permit ordinary modifiers/actions before an explicit known venue, but do
    # not let "archery in a cafe" turn the cafe into the primary activity.
    return bool(re.fullmatch(
        r"(?:(?:найти|посетить|сходить|пойти|провести|время|посидеть|поиграть|сыграть|играть|"
        r"познакомиться|посмотреть|увидеть|побывать|попробовать|зайти|бы|только|обязательно|only|must|"
        r"в|на|с|со|для|место|to|a|an|the|find|visit|see|try|go|in|at|"
        r"quiet|nice|good|interesting|unusual|short|nearby|family|for|"
        r"час\w*|минут\w*|полчаса|полтора|один|два|три|пару|"
        r"hours?|minutes?|one|two|three|\d+(?:[-–—]\d+)?|"
        r"(?:хорош|интересн|необычн|тих|спокойн|уютн|ближайш|близк|"
        r"недорог|дешев|дорог|нов|коротк|небольш|красив|семейн|детск|"
        r"вечерн|ночн|дневн)\w*)\s+)*", prefix
    ))


def _unsupported_primary(query, matches):
    for target in _TARGET.finditer(query):
        # "don't want X" and "не хочу X" reject X; they are not new goals.
        if (_negated(query, target.start(), target.end())
                or re.search(r"\b(?:don\x27t|do\s+not)\s+$", _clause_prefix(query, target.start()))):
            continue
        end = re.search(r"[,;.!?]", query[target.end():])
        limit = target.end() + end.start() if end else len(query)
        body = query[target.end():limit]
        if re.match(r"(?:не|not)\b", body):
            continue
        # A secondary known venue cannot rescue an unknown primary goal.
        cut = re.search(r"\s+(?:и|или|а|но|and|or|рядом|возле|около|near|with)\b", body)
        primary_end = target.end() + (cut.start() if cut else len(body))
        primary = query[target.end():primary_end].strip()
        if not primary:
            continue
        if any(start >= target.end() and end <= primary_end and not _negated(query, start, end)
               and not _location_mention(query, start)
               and _known_goal_lead(query[target.end():start])
               for start, end, _ in matches):
            continue
        # Nested generic "хочу найти активность" and contextual time tails are
        # accepted only when their complete core is known, never by substring.
        primary = re.sub(r"^(?:(?:найти|посмотреть|увидеть|to\s+find|to\s+see)\s+)", "", primary)
        primary = re.split(r"\s+(?:на\s+(?:(?:\d+(?:\s*[-–—]\s*\d+)?|один|два|три|пару)\s+)?(?:минут\w*|час\w*)|"
                           r"(?:for\s+)?\d+\s+(?:minutes?|hours?))\b", primary, maxsplit=1)[0].strip()
        primary = re.sub(r"\s+(?:пожалуйста|please)$", "", primary)
        if (_GENERIC.fullmatch(primary) or _PUBLIC_ANCHOR.fullmatch(primary)
                or any(re.fullmatch(pattern, primary) for pattern in _PREFERENCES.values())
                or re.fullmatch(r"(?:место|заведение)\s+(?:24\s*/\s*7|круглосуточн\w*)", primary)):
            continue
        return True
    return False


def parse_search_intent(query: str) -> SearchIntent:
    """Return only canonical categories/preferences; never echo a request."""
    if not isinstance(query, str):
        return {"categories": [], "excluded_categories": [], "preferences": [], "mode": "walk", "supported": False}
    text = _normalize(query)
    matches = _matches(text)
    positives, excluded = [], []
    for start, end, identifier in matches:
        if _location_mention(text, start):
            continue
        destination = excluded if _negated(text, start, end) else positives
        if identifier not in destination:
            destination.append(identifier)
    preferences = []
    for name, pattern in _PREFERENCES.items():
        if any(not _negated(text, match.start(), match.end())
               for match in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", text)):
            preferences.append(name)
    contradiction = bool(set(positives) & set(excluded))
    supported = bool(text.strip()) and not contradiction and not _unsupported_primary(text, matches)
    if not supported:
        positives = []
    # Generic walking does not mask an explicit venue or park request.
    if len(positives) > 1 and "walk" in positives:
        positives.remove("walk")
    mode = CATEGORY_CATALOG[positives[0]]["mode"] if positives else "walk"
    primary_explicit = bool(_TARGET.search(text) or _PRIORITY.search(text))
    if len(positives) > 1 and not primary_explicit:
        # A bare list retains the existing multi-interest walk behavior. An
        # explicit "want bowling and coffee" still has one primary venue.
        mode = "walk"
    positive_spans = [(start, end, identifier) for start, end, identifier in matches
                      if identifier in positives and not _negated(text, start, end)
                      and not _location_mention(text, start)]
    if not _PRIORITY.search(text) and any(first[2] != second[2] and re.search(r"\b(?:или|либо|or)\b", text[first[1]:second[0]])
           for first, second in zip(positive_spans, positive_spans[1:])):
        # An explicit OR is a choice among types, not a multi-service venue.
        mode = "walk"
    return {"categories": positives, "excluded_categories": excluded,
            "preferences": preferences, "mode": mode, "supported": supported}


def _positive(query, pattern):
    """Compatibility helper with the same negation scope as every category."""
    return any(not _negated(query, match.start(), match.end())
               for match in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", _normalize(query)))


def activity_intent(query, recognized_categories=()):
    """Compatibility wrapper; source recognition cannot prove user intent."""
    parsed = parse_search_intent(query)
    return {"categories": parsed["categories"], "mode": parsed["mode"],
            "unsupported": not parsed["supported"]}
