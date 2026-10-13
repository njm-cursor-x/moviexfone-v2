"""New York clock, cinema business dates, and spoken times."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_HOUR_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_MINUTE_WORDS = {
    "oh five": 5,
    "o five": 5,
    "five": 5,
    "ten": 10,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "forty five": 45,
    "fifty": 50,
}
_ONES = [
    "twelve",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
]
_TEENS = {
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
}
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def now_ny(frozen: datetime | None = None) -> datetime:
    if frozen is not None:
        if frozen.tzinfo is None:
            return frozen.replace(tzinfo=NY)
        return frozen.astimezone(NY)
    return datetime.now(NY)


def business_date(moment: datetime) -> date:
    """Cinema day runs 03:00 through 02:59 the next morning."""
    local = moment.astimezone(NY)
    if local.time() < time(3, 0):
        return local.date() - timedelta(days=1)
    return local.date()


def parse_clock(value: str | None) -> time | None:
    """Parse HH:MM or H:MM, 24-hour."""
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _strip_daypart(raw: str) -> str:
    return re.sub(r"\s+(night|evening|afternoon|morning)$", "", raw.strip().lower())


def _month_day(raw: str, today: date) -> date | None:
    named = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?", raw)
    numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", raw)
    month = day = year = None
    if named and named.group(1) in _MONTHS:
        month = _MONTHS[named.group(1)]
        day = int(named.group(2))
        year = today.year
    elif numeric:
        month = int(numeric.group(1))
        day = int(numeric.group(2))
        if numeric.group(3):
            year = int(numeric.group(3))
            if year < 100:
                year += 2000
        else:
            year = today.year
    if month is None or day is None or year is None:
        return None
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if numeric and numeric.group(3) is None and found < today - timedelta(days=1):
        try:
            found = date(year + 1, month, day)
        except ValueError:
            return None
    if named and found < today - timedelta(days=1):
        try:
            found = date(year + 1, month, day)
        except ValueError:
            return None
    return found


def resolve_when(value: str | None, now: datetime) -> tuple[list[str], str, bool]:
    """Return ISO dates, a spoken label, and whether the phrase was unclear.

    An empty value means the cinema business day. A phrase we cannot read is
    unclear instead of silently becoming today.
    """
    local = now.astimezone(NY)
    cinema = business_date(now)
    if value is None or not value.strip():
        return [cinema.isoformat()], "today", False
    original = value.strip().lower()
    raw = _strip_daypart(original)
    if raw in {"today", "tonight"}:
        spoken = "tonight" if "tonight" in original or raw == "tonight" else "today"
        return [cinema.isoformat()], spoken, False
    if raw == "tomorrow":
        day = local.date() + timedelta(days=1)
        return [day.isoformat()], "tomorrow", False
    if "weekend" in raw:
        anchor = cinema if local.time() < time(3, 0) else local.date()
        days = upcoming_weekend(anchor)
        return [day.isoformat() for day in days], "this weekend", False
    nxt = re.fullmatch(r"next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", raw)
    if nxt:
        target = _WEEKDAYS[nxt.group(1)]
        delta = (target - local.date().weekday()) % 7
        if delta == 0:
            delta = 7
        day = local.date() + timedelta(days=delta)
        return [day.isoformat()], day.strftime("%A"), False
    if raw in _WEEKDAYS:
        target = _WEEKDAYS[raw]
        delta = (target - local.date().weekday()) % 7
        day = local.date() + timedelta(days=delta)
        return [day.isoformat()], day.strftime("%A"), False
    month_day = _month_day(raw, local.date())
    if month_day is not None:
        return [month_day.isoformat()], month_day.strftime("%A"), False
    token = parse_date_token(raw, local.date())
    if token is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return [token.isoformat()], token.isoformat(), False
    return [], original, True


def parse_date_token(value: str | None, today: date) -> date | None:
    if not value:
        return None
    raw = value.strip().lower()
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    if raw in {"today", "tonight"}:
        return today
    if raw == "tomorrow":
        return today + timedelta(days=1)
    if raw in _WEEKDAYS:
        target = _WEEKDAYS[raw]
        delta = (target - today.weekday()) % 7
        return today + timedelta(days=delta)
    return None


def upcoming_weekend(today: date) -> list[date]:
    """Friday through Sunday, skipping days that already ended."""
    weekday = today.weekday()
    if weekday <= 4:
        friday = today + timedelta(days=(4 - weekday))
    else:
        friday = today - timedelta(days=(weekday - 4))
    days = [friday + timedelta(days=offset) for offset in range(3)]
    upcoming = [day for day in days if day >= today]
    return upcoming or [today]


def show_matches_after(local_t: time, after: time | None) -> bool:
    """Evening cutoffs still include late shows before 03:00."""
    if after is None:
        return True
    if local_t < time(3, 0) and after >= time(12, 0):
        return True
    return local_t >= after


def spoken_time(value: time) -> str:
    hour = _ONES[value.hour % 12]
    minute = value.minute
    if minute == 0:
        clock = hour
    elif minute < 10:
        clock = f"{hour} oh {_ONES[minute]}"
    elif minute < 20:
        clock = f"{hour} {_TEENS[minute]}"
    elif minute % 10 == 0:
        clock = f"{hour} {_TENS[minute // 10]}"
    else:
        clock = f"{hour} {_TENS[minute // 10]}-{_ONES[minute % 10]}"
    if value.hour < 3:
        clock = f"{clock} in the morning"
    elif value.hour < 12:
        clock = f"{clock} a.m."
    return clock


def display_time(value: time) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


def parse_after_phrase(text: str) -> time | None:
    """Pull an 'after seven' / 'after 9:30' cutoff. Bare hours are evening hours."""
    match = re.search(
        r"\bafter\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"(?:\s*[:]\s*(\d{2})|\s+(oh five|o five|fifteen|thirty|forty-five|forty five))?"
        r"\s*(am|pm)?\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    raw_hour = match.group(1).lower()
    hour = int(raw_hour) if raw_hour.isdigit() else _HOUR_WORDS[raw_hour]
    minute = 0
    if match.group(2):
        minute = int(match.group(2))
    elif match.group(3):
        minute = _MINUTE_WORDS[match.group(3).lower()]
    if minute > 59 or hour > 23:
        return None
    meridiem = (match.group(4) or "").lower()
    if meridiem == "am":
        hour = 0 if hour == 12 else hour
    elif meridiem == "pm":
        if hour < 12:
            hour += 12
    elif hour < 12:
        # Movie lines say "after 7" and mean the evening.
        if hour <= 6:
            hour += 12
        elif hour != 12:
            hour += 12
    if hour == 24:
        return None
    return time(hour, minute)


def iso_ny(moment: datetime) -> str:
    return moment.astimezone(NY).isoformat(timespec="seconds")
