"""Self-check for the pure scheduling logic (no network, no Google API).

Run with: python test_scheduling.py
"""

from datetime import datetime, timedelta

from main import _find_alternatives, _overlaps


def demo() -> None:
    day = datetime(2026, 8, 3, 9, 0)

    # Overlap detection
    assert _overlaps(day, day + timedelta(hours=1), day + timedelta(minutes=30), day + timedelta(hours=2))
    assert not _overlaps(day, day + timedelta(hours=1), day + timedelta(hours=1), day + timedelta(hours=2))

    # Alternative-slot search skips a busy 09:00-10:00 block and offers the next free half-hour slots
    busy = [
        {
            "start": {"dateTime": day.isoformat()},
            "end": {"dateTime": (day + timedelta(hours=1)).isoformat()},
        }
    ]
    alternatives = _find_alternatives(day, timedelta(minutes=30), busy)
    assert len(alternatives) == 3
    # 09:30 still overlaps the 09:00-10:00 block; 10:00 is the first free slot.
    assert alternatives[0].start == (day + timedelta(hours=1)).isoformat()
    assert alternatives[1].start == (day + timedelta(hours=1, minutes=30)).isoformat()

    print("ok")


if __name__ == "__main__":
    demo()
