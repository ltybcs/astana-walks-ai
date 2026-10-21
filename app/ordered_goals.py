"""Literal two-stage activity order over an already validated immutable plan.

This is not a general grammar, a new goal extractor or evidence about a place.
Unknown transitions leave the original plan untouched. Retrieval and planning
must still prove every selected stage and all existing request constraints.
"""
from __future__ import annotations

from dataclasses import replace
import re
import unicodedata

from .activity_intent import _negated, _PRIORITY
from .place_exclusions import mask_place_exclusions
from .semantic_intent import IntentPlan, IntentError, _norm, _quote_position, _REQUIREMENT_WITNESSES, _GLOBAL_CONDITION


_SEQUENCE = re.compile(r"(?<!\w)(?:потом|затем|then)(?!\w)")
_JOIN = re.compile(r"[\s,;:—–-]*(?:(?:и|а|and)\s+)?")
_AMENITY_JOIN = re.compile(r"(?:[\s,;:—–-]|(?<!\w)(?:с|со|with|и|and)(?!\w))*")
_WITH = re.compile(r"(?<!\w)(?:с|со|with)(?!\w)")
# Only ordinary visits/walking connect a sequence marker to a literal goal.
# In particular, equipment, games, services and unfamiliar actions are absent.
_LEAD = re.compile(
    r"(?:(?:пойти|сходить|зайти|посетить|погулять|прогуляться|гулять|пройтись|"
    r"немного|просто|ещ[её]|в|во|на|по|к|вокруг|"
    r"go|visit|walk|walking|stroll|a|an|the|to|in|at|around)\s+)*"
)
_CHOICE = re.compile(
    r"[\s,]*(?:или|либо|or)\s+"
    r"(?:(?:какого|какой|каком|какому|какую|какое)\s*[-–—]?\s*либо\s+|"
    r"(?:какого|какой|каком|какому|какую|какое)\s*[-–—]?\s*нибудь\s+|"
    r"(?:любого|любой|любом|любую|другого|другой|a|an|the|some|another|any)\s+)*"
)
_TAIL = re.compile(
    r"[\s,;:.!?—–-]*(?:"
    r"(?:пожалуйста|please|пешком|walking|on\s+foot)"
    r"|(?:(?:на|за|for|within)\s+)?(?:\d+(?:[.,]\d+)?(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?|"
    r"час|полчаса|полтора|полторы|пару|один|два|две|три|четыре)\s*(?:час\w*|минут\w*|hours?|minutes?)?"
    r")[\s,;:.!?—–-]*"
)


def _tail_known(value):
    value = value.strip(" \t\r\n,;:.!?—–-")
    return not value or _TAIL.fullmatch(value) is not None


def _structural_text(query):
    # Strong name masking needs original capitalization. First normalize only
    # spacing/NFKC, then casefold each character. A masked ß must become TWO
    # spaces, matching the two characters in _quote_position's normalized text.
    original = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query)).strip()
    masked = mask_place_exclusions(original)
    if len(masked) != len(original):
        return _norm(query)  # Fail closed if the masking contract ever changes.
    return "".join(" " * len(before.casefold()) if after == " " and before != " "
                   else after.casefold().replace("ё", "е") for before, after in zip(original, masked))


def _first_join_known(text, query, plan, first, marker):
    begin, end = first[1], marker.start()
    value = text[begin:end]
    if _JOIN.fullmatch(value) is not None:
        return True, ()
    spans = []
    for index, requirement in enumerate(plan.requirements):
        if requirement.key not in {"socket", "wifi", "quiet", "accessible"} or requirement.goal_index not in {None, first[2]}:
            continue
        try:
            _, left, right = _quote_position(requirement.quote, query)
        except (IntentError, ValueError, TypeError):
            continue
        if not begin <= left < right <= end or _negated(text, left, right):
            continue
        # A valid feature witness may have been quoted with unrelated words.
        # Only a closed convenience phrase can disappear from the bridge;
        # quoting "Wi-Fi and pet a dragon" must not erase that extra action.
        literal = text[left:right]
        witness = _REQUIREMENT_WITNESSES[requirement.key]
        if re.fullmatch(r"(?:(?:с|со|with|a|an|the|нужен|нужна|нужны|нужно|обязательно|need)\s+)*(?:"
                        + witness + r")", literal) is None:
            continue
        spans.append((left-begin, right-begin, index))
    if not spans:
        return False, ()
    masked = list(value)
    for left, right, _ in spans:
        masked[left:right] = " " * (right-left)
    if _AMENITY_JOIN.fullmatch("".join(masked)) is None:
        return False, ()
    local = []
    # A closed "cafe WITH property, THEN park" proves local attachment even
    # when the old deterministic schema retained that exact witness globally.
    # An independent positive witness or explicit all-places demand prevents
    # narrowing. Accessibility and service requirements are never narrowed.
    has_with = any(match.end() <= min(right for _, right, _ in spans) for match in _WITH.finditer(value))
    if has_with and _GLOBAL_CONDITION.search(text) is None:
        for _, _, index in spans:
            requirement = plan.requirements[index]
            if requirement.goal_index is not None or requirement.key not in {"socket", "wifi", "quiet"}:
                continue
            outside = any(not first[0] <= match.start() < match.end() <= end
                          and not _negated(text, match.start(), match.end())
                          for match in re.finditer(r"(?<!\w)(?:" + _REQUIREMENT_WITNESSES[requirement.key]
                                                   + r")(?!\w)", text))
            if not outside:
                local.append(index)
    return True, tuple(local)


def ordered_goals(query, plan):
    """Return ``(plan, groups)`` for one witnessed first-then-second request.

    The first stage owns exactly one existing goal. The second owns one goal,
    or two literal alternatives. Indices refer to the unchanged ``plan.goals``.
    Explicit primary, exclusions, requirement values/quotes and hash stay intact.
    Only an existing global convenience in the closed first-stage WITH bridge
    may acquire that first goal index; a separate/global demand never narrows.
    """
    if not isinstance(query, str) or not isinstance(plan, IntentPlan) or not 2 <= len(plan.goals) <= 3:
        return plan, ()
    text = _structural_text(query)
    markers = list(_SEQUENCE.finditer(text))
    if len(markers) != 1 or _PRIORITY.search(text):
        return plan, ()
    marker = markers[0]
    if _negated(text, marker.start(), marker.end()):
        return plan, ()
    try:
        positions = [(*_quote_position(goal.quote, query)[1:], index)
                     for index, goal in enumerate(plan.goals)]
    except (IntentError, ValueError, TypeError):
        return plan, ()
    positions.sort()
    if any(left[1] > right[0] for left, right in zip(positions, positions[1:])):
        return plan, ()
    if any(_negated(text, begin, end) for begin, end, _ in positions):
        return plan, ()
    first = [item for item in positions if item[1] <= marker.start()]
    second = [item for item in positions if item[0] >= marker.end()]
    if len(first) != 1 or len(second) not in (1, 2) or len(first) + len(second) != len(positions):
        return plan, ()
    join_known, local_requirements = _first_join_known(text, query, plan, first[0], marker)
    if (not join_known
            or _LEAD.fullmatch(text[marker.end():second[0][0]].lstrip()) is None
            or not _tail_known(text[second[-1][1]:])):
        return plan, ()
    if len(second) == 2 and _CHOICE.fullmatch(text[second[0][1]:second[1][0]]) is None:
        return plan, ()
    groups = ((first[0][2],), tuple(item[2] for item in second))
    requirements = tuple(replace(item, goal_index=first[0][2]) if index in local_requirements else item
                         for index, item in enumerate(plan.requirements))
    ordered = (plan if plan.goal_policy == "walk_mix" and requirements == plan.requirements
               else replace(plan, goal_policy="walk_mix", requirements=requirements))
    return ordered, groups
