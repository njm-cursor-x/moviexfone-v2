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
