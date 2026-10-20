"""Shared bounded leisure vocabulary, search phrases and exact source rubric aliases.

These matching rules are operator-authored, not proof of live provider coverage.
A source category is established only by a returned, exact rubric label.
"""
from __future__ import annotations

from types import MappingProxyType
import re
import unicodedata


def _spec(label, query, mode, aliases, rubrics=(), source_categories=(), action="Предложить посещение после проверки условий."):
    return MappingProxyType({
        "ru_label": label, "google_query": query, "dgis_query": query,
        "mode": mode, "aliases": tuple(aliases), "rubric_aliases": tuple(rubrics),
        "source_categories": tuple(source_categories),
        "proposed_activity": action,
    })


CATEGORY_CATALOG = MappingProxyType({
    "walk": _spec("Прогулка", "достопримечательности, площади и бульвары для пешей прогулки", "walk",
        (r"прогул\w*|погуля\w*|гуля\w*|пройтись|пешком|walk(?:ing|s)?|stroll\w*",),
        source_categories=("park", "playground", "promenade", "monument", "attraction", "viewpoint", "theatre"),
        action="Предложить пешую прогулку и внешний осмотр без обещания доступа."),
    "park": _spec("Парк", "Парки и сады", "walk",
        (r"парк(?:а|е|ом|и|ов|ах|ам)?|сад(?:а|у|ы|ом|ов|е|ах|ам)?|parks?|gardens?|саябақ\w*|на\s+природ\w*|in\s+nature|nature\s+walks?",),
        ("Парки", "Парки культуры и отдыха", "Сады"), ("park",),
        "Предложить прогулку в парке после проверки доступа."),
    "promenade": _spec("Набережная", "Набережные и бульвары", "walk",
        (r"набережн\w*|бульвар\w*|promenades?|riverside|embankments?",),
        ("Набережные", "Бульвары"), ("promenade",),
        "Предложить прогулку по набережной без обещания безопасности."),
    "playground": _spec("Детская площадка", "Детские площадки", "single_venue",
        (r"детск\w*\s+площад\w*|playgrounds?|ойын\s+алаң\w*",),
        ("Детские площадки",), ("playground",),
        "Предложить посещение площадки после проверки условий; оборудование неизвестно."),
    "cafe": _spec("Кафе и кофе", "Кафе и кофейни", "single_venue",
        (r"кофе|кофейн\w*|кофеен|кафе|кафешк\w*|coffee|caf[eé]s?",),
        ("Кофейни", "Кафе"), ("cafe",), "Предложить паузу на кофе после проверки условий."),
    "restaurant": _spec("Ресторан", "Рестораны", "single_venue",
        (r"ресторан\w*|поесть|обед\w*|ужин\w*|restaurants?|food",),
        ("Рестораны",), ("restaurant",), "Предложить остановку для еды после проверки условий."),
    "bar": _spec("Бар или паб", "Бары и пабы", "single_venue",
        (r"бар(?:а|е|ом|ы|ов|ах|ам)?|паб(?:а|е|ом|ы|ов|ах|ам)?|pubs?|bars?",),
        ("Бары", "Пабы"), ("bar",)),
    "museum": _spec("Музей", "Музеи", "single_venue",
        (r"музе\w*|museums?|мұражай\w*",),
        ("Музеи",), ("museum",), "Предложить посещение музея после проверки билетов и доступа."),
    "theatre": _spec("Театр", "Театры", "single_venue",
        (r"театр\w*|балет\w*|theatres?|theaters?",),
        ("Театры",), ("theatre",), "Предложить проверить афишу и возможность посещения; спектакль не подтверждён."),
    "architecture": _spec("Архитектура и достопримечательности", "Достопримечательности и архитектура для внешнего осмотра", "walk",
        (r"архитектур\w*|достопримечательност\w*|памятник\w*|architecture|sightsee\w*|landmarks?",),
        source_categories=("attraction", "monument", "viewpoint", "museum", "theatre", "promenade"),
        action="Предложить внешний осмотр без обещания входа."),
    "mall": _spec("Торговый центр", "Торговые центры", "single_venue",
        (r"торгов\w*\s+(?:развлекательн\w*\s+)?центр\w*|шопинг|шоппинг|покуп\w*|shopping|malls?",),
        ("Торговые центры", "Торгово-развлекательные центры"), ("mall",)),
    "computer_club": _spec("Компьютерный клуб", "Компьютерные клубы", "single_venue",
        (r"комп(?:ьютер(?:н\w*)?)?\s*[-–—]?\s*клуб\w*|кибер\s*[-–—]?\s*клуб\w*|киберспорт\w*\s+клуб\w*|интернет\s*[-–—]?\s*клуб\w*|(?:пк|pc)\s*[-–—]?\s*клуб\w*|(?:computer|gaming|esports?|e-sports|pc)\s+(?:gaming\s+)?(?:club|clubs|lounge|lounges)",),
        ("Компьютерные клубы", "Интернет-клубы", "Киберспортивные клубы"), ("computer_club",),
        "Предложить игровую паузу после проверки условий; техника и свободные места неизвестны."),
    "cinema": _spec("Кинотеатр", "Кинотеатры", "single_venue",
        (r"кинотеатр\w*|кино|cinemas?|movie\s+theat(?:er|re)s?|movies?",),
        ("Кинотеатры",), ("cinema",), "Предложить проверить сеансы и билеты; конкретный сеанс не подтверждён."),
    "bowling": _spec("Боулинг", "Боулинг", "single_venue",
        (r"боулинг\w*|bowling(?:\s+alleys?)?|(?:покидать|побросать|бросать|кидать)\s+шар\w*\s+в\s+кегл\w*",),
        ("Боулинг", "Боулинг-клубы"), ("bowling",),
        "Предложить боулинг после проверки дорожек и условий."),
    "billiards": _spec("Бильярд", "Бильярдные", "single_venue",
        (r"бильярд\w*|билльярд\w*|billiards?|pool\s+(?:tables?|halls?)|(?:позабивать|забивать)\s+шар\w*\s+в\s+луз\w*",),
        ("Бильярдные", "Бильярдные клубы"), ("billiards",),
        "Предложить бильярд после проверки столов и условий."),
    "karaoke": _spec("Караоке", "Караоке", "single_venue",
        (r"караоке|karaoke|(?:попеть|петь)\s+под\s+минусовк\w*",), ("Караоке", "Караоке-клубы"), ("karaoke",),
        "Предложить караоке после проверки условий."),
    "escape_room": _spec("Квест-комната", "Квесты в реальности", "single_venue",
        (r"квест\w*|escape\s+rooms?",),
        ("Квесты в реальности", "Квест-комнаты"), ("escape_room",),
        "Предложить проверить доступность квеста; сюжет, возраст и места неизвестны."),
    "board_games": _spec("Настольные игры", "Клубы настольных игр", "single_venue",
        (r"настолк\w*|настольн\w*\s+игр\w*|board\s+games?",),
        ("Клубы настольных игр",), ("board_games",),
        "Предложить настольные игры после проверки условий и доступности."),
    "anti_cafe": _spec("Антикафе", "Антикафе", "single_venue",
        (r"анти\s*[-–—]?\s*кафе|anti\s*[-–—]?\s*caf[eé]s?|time\s+caf[eé]s?",),
        ("Антикафе", "Тайм-кафе"), ("anti_cafe",),
        "Предложить паузу в антикафе; набор услуг неизвестен."),
    "trampoline": _spec("Батутный центр", "Батутные центры", "single_venue",
        (r"батут\w*|trampolin\w*(?:\s+(?:parks?|cent(?:er|re)s?))?",), ("Батутные центры", "Батутные парки"), ("trampoline",),
        "Предложить проверить условия посещения батутного центра; безопасность не подтверждена."),
    "climbing": _spec("Скалодром", "Скалодромы", "single_venue",
        (r"скалодром\w*|скалолазан\w*|climbing(?:\s+(?:gyms?|walls?))?|bouldering",),
        ("Скалодромы", "Центры скалолазания"), ("climbing",),
        "Предложить проверить условия скалодрома; снаряжение и допуск неизвестны."),
    "ice_rink": _spec("Каток", "Катки", "single_venue",
        (r"каток|катк\w*|ice\s+(?:rinks?|skating)|skating\s+rinks?",),
        ("Катки", "Ледовые катки"), ("ice_rink",),
        "Предложить проверить работу катка и условия; прокат не подтверждён."),
    "pool": _spec("Бассейн", "Бассейны", "single_venue",
        (r"бассейн\w*|поплава\w*|плаван\w*|swimming(?:\s+pools?)?|pools?",),
        ("Бассейны", "Плавательные бассейны"), ("pool",),
        "Предложить проверить условия бассейна; доступ и оборудование неизвестны."),
    "spa": _spec("СПА", "SPA-процедуры", "single_venue",
        (r"спа(?:\s*[-–—]?\s*(?:центр\w*|салон\w*))?|spa(?:\s+cent(?:er|re)s?)?",),
        ("SPA-процедуры", "SPA-салоны", "Спа-центры"), ("spa",),
        "Предложить проверить доступность СПА; процедуры и показания не подтверждены."),
    "amusement_park": _spec("Парк аттракционов", "Парки аттракционов", "single_venue",
        (r"парк\w*\s+(?:развлечен\w*|(?:с\s+)?аттракцион\w*)|аттракцион\w*|amusement\s+parks?|theme\s+parks?",),
        ("Парки аттракционов", "Аттракционы"), ("amusement_park",),
        "Предложить проверить работу аттракционов; доступ и безопасность неизвестны."),
    "fitness": _spec("Фитнес", "Фитнес-клубы", "single_venue",
        (r"фитнес\w*|спортзал\w*|тренажерн\w*\s+зал\w*|fitness|gyms?",),
        ("Фитнес-клубы", "Тренажёрные залы", "Тренажерные залы"), ("fitness",),
        "Предложить проверить разовое посещение; оборудование и допуск неизвестны."),
    "bookshop": _spec("Книжный магазин", "Книжные магазины", "single_venue",
        (r"книжн\w*\s+магазин\w*|купить\s+книг\w*|book\s*shops?|book\s*stores?",),
        ("Книжные магазины",), ("bookshop",),
        "Предложить посетить книжный магазин; наличие конкретной книги неизвестно."),
    "library": _spec("Библиотека", "Библиотеки", "single_venue",
        (r"библиотек\w*|librar(?:y|ies)",), ("Библиотеки",), ("library",),
        "Предложить проверить условия библиотеки; доступ и фонд неизвестны."),
    "coworking": _spec("Коворкинг", "Коворкинги", "single_venue",
        (r"коворкинг\w*|co\s*[-–—]?\s*working(?:\s+spaces?)?|(?:поработать|работать)\s+(?:с|за)\s+ноутбук\w*",),
        ("Коворкинги", "Коворкинг-центры"), ("coworking",),
        "Предложить проверить рабочее место; розетки, тишина и свободные места неизвестны."),
    "nightclub": _spec("Ночной клуб", "Ночные клубы", "single_venue",
        (r"ночн\w*\s+клуб\w*|дискотек\w*|night\s*clubs?",),
        ("Ночные клубы", "Дискотеки"), ("nightclub",),
        "Предложить проверить условия ночного клуба; программа и вход неизвестны."),
})
CATEGORY_IDS = frozenset(CATEGORY_CATALOG)

# These are existing source taxonomy IDs, not additional requested activity IDs.
LEGACY_RUBRIC_CATEGORIES = MappingProxyType({
    "достопримечательности": "attraction", "памятники": "monument",
    "памятники / мемориалы": "monument", "смотровые площадки": "viewpoint",
})


def _rubric_key(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")).strip()


def categories_from_rubrics(names):
    """Return exact evidenced source types in source order; never inspect brands."""
    if not isinstance(names, (list, tuple)):
        return ()
    result = []
    for name in names:
        if not isinstance(name, str):
            continue
        key = _rubric_key(name)
        category = LEGACY_RUBRIC_CATEGORIES.get(key)
        if category and category not in result:
            result.append(category)
        for identifier, spec in CATEGORY_CATALOG.items():
            if any(key == _rubric_key(alias) for alias in spec["rubric_aliases"]) and identifier not in result:
                result.append(identifier)
    return tuple(result)


def category_from_rubrics(names):
    return next(iter(categories_from_rubrics(names)), None)
