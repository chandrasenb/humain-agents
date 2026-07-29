"""Self-check for resolve_timerange() / resolve_instant(). Run with: python test_time_resolver.py"""

from datetime import timedelta, timezone

from time_resolver import resolve_instant, resolve_timerange


def demo() -> None:
    today_start, today_end = resolve_timerange("today")
    assert today_end - today_start == timedelta(days=1)

    _, tomorrow_end = resolve_timerange("tomorrow")
    assert tomorrow_end - today_end == timedelta(days=1)

    week_start, week_end = resolve_timerange("this week")
    assert week_end - week_start == timedelta(days=7)
    assert week_start.weekday() == 0  # Monday

    last7_start, last7_end = resolve_timerange("last 7 days")
    assert last7_end - last7_start == timedelta(days=8)  # 7 days back + today

    # Falls back to dateparser for weekday names / explicit dates.
    friday_start, friday_end = resolve_timerange("next Friday")
    assert friday_end - friday_start == timedelta(days=1)
    assert friday_start.weekday() == 4

    # Unknown domain defaults to UTC; a known one resolves to its zone.
    utc_start, _ = resolve_timerange("today", account_email="dev@example.com")
    assert utc_start.utcoffset() == timedelta(0)

    # Point-in-time parse used by conflict_check / schedule_meeting — must be
    # timezone-aware UTC (naive isoformat stamps get Google Calendar 400s).
    instant = resolve_instant("tomorrow 2pm", account_email="dev@example.com")
    assert instant.tzinfo is not None
    assert instant.astimezone(timezone.utc).utcoffset() == timedelta(0)

    # EB-0040 follow-up: day-part / weekend / month / word-number phrases
    # that previously raised ValueError (Prism eval failures on "what's on
    # my schedule this afternoon?", "anything on my calendar this
    # weekend?", "show me this month's meetings", etc.)
    afternoon_start, afternoon_end = resolve_timerange("this afternoon")
    assert afternoon_end - afternoon_start == timedelta(hours=6)
    assert afternoon_start.astimezone(timezone.utc).hour == 12

    tonight_start, tonight_end = resolve_timerange("tonight")
    assert tonight_end - tonight_start == timedelta(hours=6)
    assert tonight_start.astimezone(timezone.utc).hour == 18

    morning_start, morning_end = resolve_timerange("tomorrow morning")
    assert morning_end - morning_start == timedelta(hours=6)
    assert morning_start.astimezone(timezone.utc).hour == 6
    assert (morning_start.date() - afternoon_start.date()).days == 1

    weekend_start, weekend_end = resolve_timerange("this weekend")
    assert weekend_end - weekend_start == timedelta(days=2)
    assert weekend_start.weekday() == 5  # Saturday

    month_start, month_end = resolve_timerange("this month")
    assert month_start.day == 1
    assert month_end.day == 1
    assert month_start.month != month_end.month or month_start.year != month_end.year

    next3_start, next3_end = resolve_timerange("next three days")
    assert next3_end - next3_start == timedelta(days=4)  # today + 3 days out

    # "next N days" still accepts digits alongside the new word-number form.
    next3_digits_start, next3_digits_end = resolve_timerange("next 3 days")
    assert (next3_digits_start, next3_digits_end) == (next3_start, next3_end)

    # conflict_check's "when" sometimes receives a day-part phrase verbatim
    # (e.g. "is my afternoon free tomorrow?" -> when="tomorrow afternoon")
    # instead of a specific instant — resolves to the window's start rather
    # than raising.
    afternoon_instant = resolve_instant("tomorrow afternoon")
    assert afternoon_instant.hour == 12
    tonight_instant = resolve_instant("tonight")
    assert tonight_instant.hour == 18

    # Weekday name + day-part (Prism eval-run 3418: "am I busy this Thursday
    # morning?" -> conflict_check when="Thursday morning" raised ValueError).
    thu_morning_start, thu_morning_end = resolve_timerange("Thursday morning")
    assert thu_morning_end - thu_morning_start == timedelta(hours=6)
    assert thu_morning_start.astimezone(timezone.utc).hour == 6

    thu_morning_instant = resolve_instant("Thursday morning")
    assert thu_morning_instant.hour == 6
    assert thu_morning_instant.date() == thu_morning_start.astimezone(timezone.utc).date()

    friday_evening_instant = resolve_instant("next Friday evening")
    assert friday_evening_instant.hour == 18

    print("ok")


if __name__ == "__main__":
    demo()
