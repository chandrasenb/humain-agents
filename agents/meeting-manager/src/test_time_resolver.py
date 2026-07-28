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

    print("ok")


if __name__ == "__main__":
    demo()
