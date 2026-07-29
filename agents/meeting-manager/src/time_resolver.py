"""Natural-language time-range resolution.

resolve_timerange() turns phrases like "this week", "tomorrow", "next
Friday", "last 7 days" into a (time_min, time_max) UTC window. Named
relative ranges ("today", "this week", "last N days", ...) are resolved
by hand since dateparser only returns a single point, not a range;
anything else (weekday names, explicit dates) falls back to dateparser
and is treated as a single day.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dateparser

# ponytail: domain -> IANA timezone is a one-entry heuristic (no per-tenant
# timezone setting exists yet). Add real entries as more domains show up, or
# swap this for a tenant-config lookup once one exists.
_DOMAIN_TIMEZONES: dict[str, str] = {
    "humain.ai": "Asia/Riyadh",
}
_DEFAULT_TIMEZONE = "UTC"

# (EB-0040 follow-up) Hour-of-day windows for day-part phrases ("this
# afternoon", "tonight", "tomorrow morning"). "evening" and "night" share a
# window since the router never disambiguates the two.
_DAY_PART_HOURS: dict[str, tuple[int, int]] = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 24),
    "night": (18, 24),
}

_WORD_NUMBERS: dict[str, int] = {
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
}


def _parse_count(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _resolve_timezone(account_email: str | None) -> ZoneInfo:
    domain = (account_email or "").rsplit("@", 1)[-1].lower()
    return ZoneInfo(_DOMAIN_TIMEZONES.get(domain, _DEFAULT_TIMEZONE))


def _day_bounds(day: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def _week_bounds(day: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    monday = day - timedelta(days=day.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    return start, start + timedelta(days=7)


def _daypart_bounds(day: datetime, tz: ZoneInfo, part: str) -> tuple[datetime, datetime]:
    start_hour, end_hour = _DAY_PART_HOURS[part]
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return day_start + timedelta(hours=start_hour), day_start + timedelta(hours=end_hour)


def _weekend_bounds(day: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Upcoming Saturday through end of Sunday (today counts if it's already
    Saturday). weekday(): Mon=0 .. Sat=5, Sun=6."""
    saturday = day + timedelta(days=(5 - day.weekday()) % 7)
    start, _ = _day_bounds(saturday, tz)
    return start, start + timedelta(days=2)


def _month_bounds(day: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, 1, tzinfo=tz)
    if day.month == 12:
        end = datetime(day.year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(day.year, day.month + 1, 1, tzinfo=tz)
    return start, end


def _match_daypart(text: str) -> tuple[int, str] | None:
    """Returns (day_offset, part) for a day-part phrase ("this afternoon",
    "tonight", "tomorrow morning"), else None. Shared by resolve_timerange
    (a window) and resolve_instant (the window's start, e.g. for
    conflict_check when the router passes through an ambiguous "is my
    afternoon free tomorrow?"-style phrase instead of a specific time)."""
    if text == "tonight":
        return 0, "night"
    if m := re.fullmatch(r"(?:this|today)\s+(morning|afternoon|evening|night)", text):
        return 0, m.group(1)
    if m := re.fullmatch(r"tomorrow\s+(morning|afternoon|evening|night)", text):
        return 1, m.group(1)
    return None


def resolve_timerange(
    natural_language_input: str,
    account_email: str | None = None,
) -> tuple[datetime, datetime]:
    """Resolve `natural_language_input` to a (time_min, time_max) UTC window.

    `account_email` (GOOGLE_ACCOUNT_EMAIL, injected alongside the access
    token) picks the timezone the phrase is interpreted in; unrecognized or
    missing domains default to UTC.
    """
    tz = _resolve_timezone(account_email)
    now = datetime.now(tz)
    text = natural_language_input.strip().lower()

    if text == "today":
        start, end = _day_bounds(now, tz)
    elif text == "tomorrow":
        start, end = _day_bounds(now + timedelta(days=1), tz)
    elif text == "yesterday":
        start, end = _day_bounds(now - timedelta(days=1), tz)
    elif text == "this week":
        start, end = _week_bounds(now, tz)
    elif text == "next week":
        start, end = _week_bounds(now + timedelta(days=7), tz)
    elif text == "last week":
        start, end = _week_bounds(now - timedelta(days=7), tz)
    elif text == "this weekend":
        start, end = _weekend_bounds(now, tz)
    elif text == "this month":
        start, end = _month_bounds(now, tz)
    elif (daypart := _match_daypart(text)) is not None:
        offset, part = daypart
        start, end = _daypart_bounds(now + timedelta(days=offset), tz, part)
    elif m := re.fullmatch(r"last (\d+) days?", text):
        today_start, _ = _day_bounds(now, tz)
        start = today_start - timedelta(days=int(m.group(1)))
        end = today_start + timedelta(days=1)
    elif m := re.fullmatch(r"next (\d+|[a-z]+) days?", text):
        count = _parse_count(m.group(1))
        if count is None:
            raise ValueError(f"Could not understand the time range: {natural_language_input!r}")
        start, _ = _day_bounds(now, tz)
        end = start + timedelta(days=count + 1)
    else:
        # dateparser's "next/this/last <weekday>" phrasing returns None in
        # this version even though bare weekday names ("Friday") parse fine
        # — strip the modifier and drive direction via PREFER_DATES_FROM
        # instead of relying on dateparser to parse the phrase as a whole.
        text_for_parser = text
        prefer = "future"
        weekday_modifier = re.match(r"(next|this|last)\s+(\w+)$", text)
        if weekday_modifier:
            modifier, day_name = weekday_modifier.groups()
            text_for_parser = day_name
            prefer = "past" if modifier == "last" else "future"

        parsed = dateparser.parse(
            text_for_parser,
            settings={"PREFER_DATES_FROM": prefer, "RELATIVE_BASE": now.replace(tzinfo=None)},
        )
        if parsed is None:
            raise ValueError(f"Could not understand the time range: {natural_language_input!r}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        start, end = _day_bounds(parsed, tz)

    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def resolve_instant(
    natural_language_input: str,
    account_email: str | None = None,
) -> datetime:
    """Resolve a point-in-time phrase (e.g. "tomorrow 2pm") to a UTC datetime.

    Uses the same account-email → timezone heuristic as ``resolve_timerange``
    so conflict_check / schedule_meeting send timezone-aware RFC3339 stamps
    to Google Calendar (naive ``isoformat()`` values are rejected with 400).
    """
    tz = _resolve_timezone(account_email)
    now = datetime.now(tz)
    text = natural_language_input.strip().lower()

    # conflict_check's "when" is meant to be a specific instant, but the
    # router sometimes passes through a day-part phrase verbatim (e.g. "is my
    # afternoon free tomorrow?" -> when="tomorrow afternoon"). dateparser has
    # no notion of "afternoon"/"tonight" and raises on all of these; resolve
    # to the window's start as the representative instant instead of
    # rejecting a request that resolve_timerange would otherwise understand.
    daypart = _match_daypart(text)
    if daypart is not None:
        offset, part = daypart
        start, _ = _daypart_bounds(now + timedelta(days=offset), tz, part)
        return start.astimezone(timezone.utc)

    parsed = dateparser.parse(
        natural_language_input.strip(),
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now.replace(tzinfo=None),
        },
    )
    if parsed is None:
        raise ValueError(f"Could not understand the time expression: {natural_language_input!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)
