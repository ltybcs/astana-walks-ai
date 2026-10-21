"""Pure, bounded weekly source-schedule evaluation for Astana (UTC+05).

Supported OSM subset: weekday lists/ranges, HH:MM-HH:MM interval lists,
semicolon normal rules, full-day off/closed, and standalone 24/7. Normal rules
replace earlier hours on their selected calendar days, including prior-night
spill. Redefining the start day of an earlier overnight rule is deliberately
unsupported rather than approximated. Additional-rule commas, extended hours
beyond 24:00, holidays, dates, comments and specials are not supported.

The collector's separate Mon...Sun form (lowercase accepted) is a complete seven-day
2GIS normalized schedule; its day records are combined, including night spill.
Missing days in the supported OSM weekly schedule are closed. Missing/incomplete
2GIS schedules and any unsupported rule make the ENTIRE evaluation unknown.

An open result means source-reported coverage only, not live availability,
accessibility, safety, a booking, or permission to visit. No clock or I/O access.
References: https://wiki.openstreetmap.org/wiki/Key:opening_hours/specification
and https://wiki.openstreetmap.org/wiki/Key:opening_hours#Closing_after_midnight
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

_ASTANA = timezone(timedelta(hours=5))
_DAY = 1440
_WEEK = 7 * _DAY
_MINUTE_US = 60_000_000
_OSM_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DGIS_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TOKEN = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)"
_SELECTOR = _TOKEN + r"(?:\s*-\s*" + _TOKEN + r")?(?:\s*,\s*" + _TOKEN + r"(?:\s*-\s*" + _TOKEN + r")?)*"
_OSM_RULE = re.compile(r"(?:(?P<days>" + _SELECTOR + r") +)?(?P<body>.+)")
_INTERVAL = re.compile(r"(?P<start>[0-9]{2}:[0-9]{2})\s*[-–]\s*(?P<end>[0-9]{2}:[0-9]{2})")
_BAD_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _result(status, reason):
    return {"status": status, "reason": reason}


def _minute(value, *, closing=False):
    hours, minutes = (int(item) for item in value.split(":"))
    if minutes > 59 or hours > 24 or hours == 24 and (not closing or minutes):
        return None
    return hours * 60 + minutes


def _intervals(body):
    if body in {"off", "closed"}:
        return []
    parts = body.split(",")
    if not 1 <= len(parts) <= 16:
        return None
    intervals = []
    for part in parts:
        match = _INTERVAL.fullmatch(part.strip())
        if match is None:
            return None
        start = _minute(match["start"])
        end = _minute(match["end"], closing=True)
        # Equal endpoints are not guessed to mean 24/7. Explicit 00:00-24:00
        # establishes a full day; malformed/extended syntax remains unknown.
        if start is None or end is None or start == end:
            return None
        if end < start:
            end += _DAY
        intervals.append((start, end))
    return intervals


def _days(selector):
    if selector is None:
        return set(range(7))
    result = set()
    for token in selector.split(","):
        ends = re.split(r"\s*-\s*", token.strip())
        first = _OSM_DAYS.index(ends[0])
        last = _OSM_DAYS.index(ends[-1])
        while True:
            result.add(first)
            if first == last:
                break
            first = (first + 1) % 7
    return result


def _write(grid, day, intervals):
    for start, end in intervals:
        for minute in range(day * _DAY + start, day * _DAY + end):
            grid[minute % _WEEK] = True


def _weekly_grid(hours):
    rules = hours.split(";")
    if not 1 <= len(rules) <= 32 or any(not rule.strip() for rule in rules):
        return None
    grid = [False] * _WEEK
    is_dgis = bool(re.match(r"(?:mon|tue|wed|thu|fri|sat|sun)\b", hours, re.I))
    if is_dgis:
        # This exact normalized form is emitted only after all seven daily
        # records are present. Do not invent closed days for partial source data.
        if len(rules) != 7:
            return None
        seen = set()
        parsed = []
        for rule in rules:
            match = re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun) +(.+)", rule.strip(), re.I)
            if match is None or match[1].casefold() in seen:
                return None
            seen.add(match[1].casefold())
            intervals = _intervals(match[2])
            if intervals is None:
                return None
            parsed.append((_DGIS_DAYS.index(match[1].casefold()), intervals))
        for day, intervals in parsed:
            _write(grid, day, intervals)
        return grid
    overnight_start_days = set()
    for rule in rules:
        match = _OSM_RULE.fullmatch(rule.strip())
        if match is None:
            return None
        intervals = _intervals(match["body"])
        if intervals is None:
            return None
        days = _days(match["days"])
        if days & overnight_start_days:
            return None  # Unsupported overnight start-day redefinition.
        # OSM semicolon rules override the WHOLE selected day, not merely the
        # new intervals. Clearing all selected days precedes writing the rule,
        # so consecutive days within one rule retain their overnight spill.
        for day in days:
            grid[day * _DAY:(day + 1) * _DAY] = [False] * _DAY
        for day in days:
            _write(grid, day, intervals)
        if any(end > _DAY for _, end in intervals):
            overnight_start_days.update(days)
    return grid


def evaluate_schedule(hours, when, *, duration_minutes=0):
    """Return {status: open|closed|unknown, reason: bounded static code}.

    ``when`` must be an aware datetime; its instant is converted to Astana's
    fixed UTC+05. ``duration_minutes`` is a finite nonnegative number <=10080.
    A point at closing is closed; a positive visit may END exactly at closing.
    The ENTIRE visit interval must be covered, including any meal-time break.
    No interpretation is attempted for an imprecise date or future period.
    """
    if not isinstance(when, datetime):
        return _result("unknown", "invalid_instant")
    try:
        if when.tzinfo is None or when.utcoffset() is None:
            return _result("unknown", "invalid_instant")
        local = when.astimezone(_ASTANA)
    except (ValueError, TypeError, OverflowError):
        return _result("unknown", "invalid_instant")
    if (type(duration_minutes) not in (int, float)
            or not 0 <= duration_minutes <= _WEEK):
        return _result("unknown", "invalid_duration")
    if hours is None or hours == "":
        return _result("unknown", "missing_schedule")
    if not isinstance(hours, str) or len(hours) > 2048 or _BAD_CONTROL.search(hours):
        return _result("unknown", "unsupported_schedule")
    hours = hours.strip()
    if not hours:
        return _result("unknown", "missing_schedule")
    if hours == "24/7":
        return _result("open", "reported_schedule_covers_interval")
    grid = _weekly_grid(hours)
    if grid is None:
        return _result("unknown", "unsupported_schedule")
    start = ((local.weekday() * _DAY + local.hour * 60 + local.minute) * _MINUTE_US
             + local.second * 1_000_000 + local.microsecond)
    delta = timedelta(minutes=duration_minutes)
    duration_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    first = start // _MINUTE_US
    last = (start + duration_us - 1) // _MINUTE_US if duration_us else first
    if all(grid[minute % _WEEK] for minute in range(first, last + 1)):
        return _result("open", "reported_schedule_covers_interval")
    return _result("closed", "reported_schedule_does_not_cover_interval")
