"""Bounded explicit POI-name exclusions for this request only.

These helpers never remember visits or borrow another source's identity.
Matching uses the candidate's complete name, with closed grammatical case forms
only for a named Russian park/garden/museum/promenade title.
"""
from __future__ import annotations

import re
import unicodedata

from .category_catalog import CATEGORY_CATALOG

_MAX_QUERY = 2000
_MAX_NAMES = 10
_MAX_NAME = 120
_TRANSLATE = str.maketrans({"ё": "е", "Ё": "Е", "’": "'", "‘": "'", "ʼ": "'",
                           "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
                           "–": "-", "—": "-", "−": "-"})
_BOUNDARY = r"(?:^|[.!?;,\n]|(?<=\s)-(?=\s))\s*"
_POLITE = r"(?:(?:пожалуйста|please)\s*[,]?\s*)?"
_DONT = r"не\s+(?:предлагай(?:те)?|советуй(?:те)?|рекомендуй(?:те)?)"
_COMMAND = re.compile(_BOUNDARY + _POLITE + r"(?P<head>" + _DONT
    + r"|исключи(?:те)?|exclude|omit|(?:do\s+not|don't)\s+(?:suggest|recommend))\b\s*:?\s*", re.I)
_PRONOUN = r"(?:их|это|эти\s+места)(?!\w)"
_VISIT = r"(?:был|была|были|бывал|бывала|бывали)"
_REVERSE_VISITED = re.compile(_BOUNDARY + r"(?:я\s+)?(?:в|на)\s+(?P<names>[^\n!?;]{1,1000}?)\s+"
    + r"(?:я\s+)?(?:уже\s+)?" + _VISIT + r"\s*[,\-\s]+" + _POLITE + _DONT + r"\s+" + _PRONOUN, re.I)
_FORWARD_VISITED = re.compile(_BOUNDARY + r"(?:я\s+)?(?:уже\s+)?" + _VISIT
    + r"\s+(?:в|на)\s+(?P<names>[^\n!?;]{1,1000}?)\s*[,\-\s]+" + _POLITE + _DONT + r"\s+" + _PRONOUN, re.I)
_EN_VISITED = re.compile(_BOUNDARY + r"(?P<head>i(?:'ve|\s+have)\s+(?:already\s+)?visited|i\s+(?:already\s+)?visited)\b\s+", re.I)
_SEPARATORS = re.compile(r",|\s+(?:и|and)\s+", re.I)
_TAIL_CLAUSE = re.compile(r"(?:но|а|but|because|then|instead|хочу|хочется|вместо|затем|потом)(?!\w)", re.I)
_COMMA_CLAUSE = re.compile(r"\s*(?:я|мы|мне|нам|у\s+меня|потому|так\s+как|i|we|because|they)(?!\w)", re.I)
_REVISIT = re.compile(r"(?<!\w)(?:again|revisit|снова|еще\s+раз|повторно)(?!\w)", re.I)
_KEEP_VISITED = re.compile(r"(?<!\w)(?:(?:don't|do\s+not|never)\s+(?:exclude|omit)|не\s+исключай(?:те)?)(?!\w)", re.I)
_NON_NAME = frozenset({"их", "это", "эти места", "там", "туда", "они", "их все", "ничего", "все", "всё",
    "другие места", "места", "место", "кафе", "кофейня", "кофейни", "парк", "парки", "музей", "музеи",
    "аптека", "аптеки", "рестораны", "площадки", "them", "it", "these", "these places", "places", "everything",
    "all", "others", "parks", "park", "cafe", "cafes", "museum", "museums", "pharmacy", "pharmacies"})
_NOT_A_NAME = re.compile(r"(?<!\w)(?:я|мы|мне|нам|не|not|never|кроме|except|был|была|были|уже|предлагай|исключи|хочу|нужно|"
                        r"want|visited|recommend|suggest|already|please|любые|все|всё|all|any)(?!\w)", re.I)


def _text(value):
    return unicodedata.normalize("NFKC", value).translate(_TRANSLATE)


def _quote_mask(text):
    """Mark quoted content, without treating a word's apostrophe as a quote."""
    quoted = None
    mask = []
    for index, char in enumerate(text):
        mask.append(quoted is not None)
        if char not in {'"', "'"}:
            continue
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if char == "'" and before.isalnum() and after.isalnum():
            continue
        if quoted == char:
            quoted = None
        elif quoted is None and (char == '"' or after and not after.isspace()):
            quoted = char
    mask.append(quoted is not None)
    return mask


def _normalize_name(value):
    if not isinstance(value, str) or len(value) > _MAX_NAME + 16:
        return ""
    text = re.sub(r"\s+", " ", _text(value)).strip().rstrip(".!?").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if not text or len(text) > _MAX_NAME or not any(char.isalnum() for char in text):
        return ""
    if _quote_mask(text)[-1] or any(unicodedata.category(char).startswith("C") for char in text):
        return ""
    return text.casefold()


def _list_names(payload, *, prepositions=False):
    mask = _quote_mask(payload)
    if mask[-1]:
        return ()
    boundaries = [(match.start(), match.end()) for match in _SEPARATORS.finditer(payload) if not mask[match.start()]]
    pieces, beginning = [], 0
    for start, end in boundaries:
        pieces.append(payload[beginning:start])
        beginning = end
    pieces.append(payload[beginning:])
    result = []
    for piece in pieces:
        if prepositions:
            piece = re.sub(r"^\s*(?:в|на)\s+", "", piece, flags=re.I)
        name = _normalize_name(piece)
        generic = any(re.fullmatch(pattern, name, re.I) for spec in CATEGORY_CATALOG.values() for pattern in spec["aliases"])
        if not name or name in _NON_NAME or generic or _NOT_A_NAME.search(name) or "://" in name:
            continue
        result.append(name)
    return tuple(result)


def _payload(text, start):
    """Read one list, stopping before an unrelated unquoted clause."""
    tail = text[start:]
    mask = _quote_mask(tail)
    for index, char in enumerate(tail):
        if mask[index]:
            continue
        if char in "\n;!?" or char == "-" and index and tail[index - 1].isspace() and tail[index + 1:index + 2].isspace():
            return tail[:index]
        if char == "." and (index + 1 == len(tail) or tail[index + 1].isspace()):
            prefix = re.search(r"(\w+)$", tail[:index])
            if not prefix or prefix[1].casefold() not in {"st", "mr", "mrs", "dr", "ул", "пр", "им"}:
                return tail[:index]
        if char == "," and _COMMA_CLAUSE.match(tail, index + 1):
            return tail[:index]
        if (index == 0 or not tail[index - 1].isalnum()) and _TAIL_CLAUSE.match(tail, index):
            return tail[:index]
    return tail


def excluded_place_names(query):
    """Return at most ten exact normalized names from explicit RU/EN clauses.

    Russian visited lists require a linked exclusion ("не предлагай их").
    An explicit English "I've visited X" is also supported, unless the same
    sentence asks to visit again. Unknown names never expand to a category.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY:
        return ()
    text = _text(query)
    mask = _quote_mask(text)
    found = []
    for match in _COMMAND.finditer(text):
        if not mask[match.start("head")]:
            found.append((match.start("head"), _list_names(_payload(text, match.end()))))
    for pattern in (_REVERSE_VISITED, _FORWARD_VISITED):
        for match in pattern.finditer(text):
            if not mask[match.start("names")]:
                found.append((match.start("names"), _list_names(match["names"], prepositions=True)))
    for match in _EN_VISITED.finditer(text):
        if mask[match.start("head")]:
            continue
        sentence_start = max((text.rfind(char, 0, match.start("head")) for char in ".!?\n"), default=-1) + 1
        sentence = re.split(r"[.!?\n]", text[sentence_start:], maxsplit=1)[0]
        if not _REVISIT.search(sentence) and not _KEEP_VISITED.search(sentence):
            found.append((match.start("head"), _list_names(_payload(text, match.end()))))
    result = []
    for _, names in sorted(found, key=lambda item: item[0]):
        for name in names:
            if name not in result:
                result.append(name)
            if len(result) == _MAX_NAMES:
                return tuple(result)
    return tuple(result)



# Nominative/genitive/dative/accusative/instrumental/prepositional. A full
# source title, not the user's suffix or a guessed stem, generates these forms.
_VENUE_CASES = {
    "парк": ("парк", "парка", "парку", "парк", "парком", "парке"),
    "сад": ("сад", "сада", "саду", "сад", "садом", "саду"),
    "музей": ("музей", "музея", "музею", "музей", "музеем", "музее"),
    "набережная": ("набережная", "набережной", "набережной", "набережную", "набережной", "набережной"),
}


def _adjective_cases(word, feminine):
    if feminine:
        if word.endswith("яя") and len(word) > 4:
            stem = word[:-2]
            return tuple(stem + ending for ending in ("яя", "ей", "ей", "юю", "ей", "ей"))
        # The common hard feminine agreement is unambiguous here; do not guess
        # stress-dependent -шая/-жая forms from an arbitrary proper name.
        if word.endswith("ая") and len(word) > 4 and word[-3] not in "жшчщ":
            stem = word[:-2]
            return tuple(stem + ending for ending in ("ая", "ой", "ой", "ую", "ой", "ой"))
        return None
    if len(word) <= 4 or not word.endswith(("ый", "ий", "ой")):
        return None
    stem, ending = word[:-2], word[-2:]
    soft = word.endswith("ний") or ending == "ий" and stem[-1] in "жшчщ"
    suffixes = (ending, "его", "ему", ending, "им", "ем") if soft else (
        ending, "ого", "ому", ending, "им" if ending == "ий" or stem[-1] in "гкхжшчщ" else "ым", "ом")
    return tuple(stem + suffix for suffix in suffixes)


def _own_name_forms(name):
    forms = {name}
    words = name.split()
    heads = [index for index, word in enumerate(words) if word in _VENUE_CASES]
    # A bare category is never a named identity; two possible heads are
    # ambiguous. The remainder (proper name or adjective) must stay present.
    if len(heads) != 1 or len(words) < 2 or len(words) > 12:
        return forms
    head = heads[0]
    changing = {head: _VENUE_CASES[words[head]]}
    for index in range(head - 1, max(-1, head - 4), -1):
        cases = _adjective_cases(words[index], words[head] == "набережная")
        if cases is None:
            break
        changing[index] = cases
    for case in range(6):
        forms.add(" ".join(changing[index][case] if index in changing else word for index, word in enumerate(words)))
    return forms


def is_place_excluded(place, names):
    """Match an own full title/case form; never inspect tags, type or address."""
    if (not isinstance(place, dict) or not isinstance(names, (tuple, list))
            or len(names) > _MAX_NAMES):
        return False
    own_name = _normalize_name(place.get("name"))
    if not own_name:
        return False
    own_forms = _own_name_forms(own_name)
    return any(_normalize_name(name) in own_forms for name in names)


def _maskable_list(payload, *, prepositions=False):
    """Interpretation masking needs stronger evidence than exact-name filtering."""
    mask = _quote_mask(payload)
    boundaries = [(match.start(), match.end()) for match in _SEPARATORS.finditer(payload) if not mask[match.start()]]
    pieces, beginning = [], 0
    for start, end in boundaries:
        pieces.append(payload[beginning:start])
        beginning = end
    pieces.append(payload[beginning:])
    if not pieces or len(pieces) > _MAX_NAMES:
        return False
    for piece in pieces:
        if prepositions:
            piece = re.sub(r"^\s*(?:в|на)\s+", "", piece, flags=re.I)
        piece = piece.strip()
        names = _list_names(piece)
        if len(names) != 1:
            return False
        quoted = len(piece) > 2 and piece[0] == piece[-1] and piece[0] in {'"', "'"}
        if quoted:
            continue
        name = names[0]
        # A category/property prohibition must remain available to the activity
        # parser. Quoted literal names may contain otherwise ambiguous words.
        if re.search(r"(?<!\w)(?:в|на|с|со|без|для|with|without|for|near|около|"
                     r"розет\w*|wi[ -]?fi|wifi|socket\w*|тих\w*|quiet|доступн\w*|"
                     r"коляс\w*|ребен\w*|ребён\w*|дет\w*|бесплатн\w*|дешев\w*|"
                     r"дорог\w*|цен\w*|price|cheap|accessible|красив\w*|интересн\w*|"
                     r"нов\w*|необычн\w*|любые|все|всё)(?!\w)", name, re.I):
            return False
        if re.search(r"(?<!\w)(?:парки|парков|сады|садов|музеи|музеев|кафешки|кофейни|"
                     r"площадки|рестораны|аптеки|parks|cafes|museums|pharmacies)(?!\w)", name):
            return False
        # No arbitrary lowercase Russian descriptor becomes a named identity.
        known_title = re.fullmatch(r"(?:ботаническ(?:ий|ом|ого|ому|им)\s+сад(?:у|а|ом)?|"
            r"(?:президентск|центральн)(?:ий|ый|ом|ого|ому|им)\s+парк(?:е|а|у|ом)?|парк\s+астана)", name)
        if not (any(char.isupper() for char in piece) or re.search(r"[a-z0-9]", name) or known_title):
            return False
    return True


def _name_exclusion_clauses(query):
    if not isinstance(query, str) or len(query) > _MAX_QUERY:
        return query, []
    text = _text(query)
    mask, clauses = _quote_mask(text), []
    for match in _COMMAND.finditer(text):
        payload = _payload(text, match.end())
        if not mask[match.start("head")] and _maskable_list(payload):
            clauses.append((match.start("head"), match.end() + len(payload), _list_names(payload)))
    for pattern in (_REVERSE_VISITED, _FORWARD_VISITED):
        for match in pattern.finditer(text):
            if not mask[match.start("names")] and _maskable_list(match["names"], prepositions=True):
                start = match.start() + re.match(r"[.!?;,\n\s-]*", match[0]).end()
                clauses.append((start, match.end(), _list_names(match["names"], prepositions=True)))
    for match in _EN_VISITED.finditer(text):
        payload = _payload(text, match.end())
        sentence = re.split(r"[.!?\n]", text[match.end():], maxsplit=1)[0]
        if not mask[match.start("head")] and not _REVISIT.search(sentence) and _maskable_list(payload):
            clauses.append((match.start("head"), match.end() + len(payload), _list_names(payload)))
    return text, clauses


def proven_place_exclusions(query):
    """Only strongly witnessed literal identities may replace semantic exclusions.

    This is stricter than the exact candidate-name filter: an ambiguous unknown
    category remains available to semantic validation and fail-closed fallback.
    """
    _, clauses = _name_exclusion_clauses(query)
    result = []
    for _, _, names in clauses:
        for name in names:
            if name not in result:
                result.append(name)
    return tuple(result[:_MAX_NAMES])


def mask_place_exclusions(query):
    """Blank strongly witnessed named clauses for activity interpretation only.

    Original text remains the quote/endpoint evidence. Spaces preserve offsets
    within normalized text. Category/property/ambiguous clauses stay untouched.
    """
    text, clauses = _name_exclusion_clauses(query)
    if not clauses:
        return query
    result = list(text)
    for beginning, end, _ in clauses:
        result[beginning:end] = ' ' * (end - beginning)
    return ''.join(result)
