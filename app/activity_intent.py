"""Small allowlisted activity vocabulary; no query text leaves this module.

Explicit supported venue requests are not broad walks. Unknown explicit venue
requests fail closed; this is a bounded lexical parser, not universal NLP.
"""
from __future__ import annotations

import re
import unicodedata

COMPUTER_CLUB_PATTERN = (
    r"комп(?:ьютер(?:н\w*)?)?\s*[-–—]?\s*клуб\w*|"
    r"кибер\s*[-–—]?\s*клуб\w*|киберспорт\w*\s+клуб\w*|"
    r"интернет\s*[-–—]?\s*клуб\w*|(?:пк|pc)\s*[-–—]?\s*клуб\w*|"
    r"(?:computer|gaming|esports?|e-sports|pc)\s+(?:gaming\s+)?(?:club|clubs|lounge|lounges)"
)


def _positive(query, pattern):
    for match in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", query):
        prefix, suffix = query[max(0, match.start()-45):match.start()], query[match.end():match.end()+30]
        if (not re.search(r"\bне\s+(?:(?:в|на)\s+)?$", prefix)
                and not re.search(r"(?:\bбез|\bне\s+(?:хочу|нуж\w*|надо|интерес\w*)|\bdon['’]t\s+want|\bno|\bnot|\bwithout)\s+(?:\w+\s+){0,3}$", prefix)
                and not re.match(r"\s+не\s+(?:нуж\w*|надо|интерес\w*)", suffix)):
            return True
    return False


def activity_intent(query, recognized_categories=()):
    normalized = unicodedata.normalize("NFKC", query).casefold().replace("ё", "е")
    # Evaluate the explicit primary activity before allowing a recognized
    # secondary category to influence the search. Adjectives remain in this
    # short clause: 'найти хорошее кафе' is still a cafe, while 'в боулинг и
    # кофе' cannot silently become a cafe request.
    target = re.search(r"\b(?:хочу|хотел\w*|пойду|сходить)\s+(?:бы\s+)?(?:в|на)\s+([^,;.!?]{1,160})|"
                       r"\b(?:ищу|найти|посетить|find|visit)\s+(?:a\s+|an\s+)?([^,;.!?]{1,160})", normalized)
    unsupported = False
    if target:
        clause = next(value for value in target.groups() if value)
        clause = re.split(r"\s+(?:и|или|а|но|and|or|рядом|возле|около|near)\b", clause, maxsplit=1)[0]
        # These are existing supported activities and broad suggestions, not
        # new provider categories or a claim about any returned venue.
        known = (r"парк\w*|сад\w*|кафе|коф\w*|ресторан\w*|музе\w*|театр\w*|бар\w*|паб\w*|"
                 r"детск\w*\s+площад\w*|торгов\w*\s+центр\w*|магазин\w*|шоп\w*|"
                 r"набережн\w*|бульвар\w*|достопримеч\w*|памятник\w*|архитектур\w*|"
                 r"мег[аи]|mega|байтерек\w*|бәйтерек\w*|baiterek|bayterek|экспо|expo|"
                 r"прогул\w*|погуля\w*|гуля\w*|мест[оа]|"
                 r"walk\w*|park\w*|garden\w*|caf[eé]\w*|coffee|restaurant\w*|museum\w*|"
                 r"theat\w*|bar\w*|pub\w*|mall\w*|shop\w*|promenade|place\w*")
        generic_only = re.fullmatch(r"\s*(?:необычн\w*|интересн\w*|interesting|something(?:\s+interesting)?)\s*", clause)
        unsupported = not bool(generic_only or re.search(r"(?<!\w)(?:" + COMPUTER_CLUB_PATTERN + "|" + known + r")(?!\w)", clause))
    if unsupported:
        return {"categories": [], "mode": "walk", "unsupported": True}
    if _positive(normalized, COMPUTER_CLUB_PATTERN):
        return {"categories": ["computer_club"], "mode": "single_venue", "unsupported": False}
    return {"categories": [], "mode": "walk", "unsupported": False}
