"""Self-check: timezone-aware conflict/schedule timestamps + Calendar HttpError handling.

Mocks calendar_tools + LLM — no network. Run with: python test_tz_and_httperror.py
"""

from __future__ import annotations

import json
import os
from datetime import timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from httplib2 import Response

import calendar_tools
import main
from time_resolver import resolve_instant

os.environ.setdefault("OPENAI-API-KEY", "fake")
os.environ.setdefault("OPENAI-BASE-URL", "https://fake.invalid/v1")

client = TestClient(main.app)


def _fake_completion(tool_name: str, args: dict) -> MagicMock:
    tool_call = MagicMock()
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(args)
    completion = MagicMock()
    completion.choices[0].message.tool_calls = [tool_call]
    completion.choices[0].message.content = None
    return completion


def _httperror(status: int = 400, reason: str = "Bad Request") -> HttpError:
    resp = Response({"status": status, "reason": reason})
    return HttpError(resp, content=b'{"error":{"message":"Bad Request"}}', uri="https://www.googleapis.com/calendar/v3/…")


def demo() -> None:
    # resolve_instant always returns timezone-aware UTC
    instant = resolve_instant("tomorrow 3pm", account_email="dev@example.com")
    assert instant.tzinfo is not None
    assert instant.utcoffset() is not None
    assert instant.utcoffset().total_seconds() == 0  # UTC

    # conflict_check passes timezone-aware time_min/time_max into list_events
    with patch.object(calendar_tools, "list_events", return_value=[]) as mock_list:
        resp = client.post("/conflict_check", json={"when": "tomorrow 3pm", "duration_minutes": 30})
        assert resp.status_code == 200, resp.text
        assert mock_list.called
        time_min, time_max = mock_list.call_args.args[:2]
        assert time_min.tzinfo is not None, f"naive time_min: {time_min!r}"
        assert time_max.tzinfo is not None, f"naive time_max: {time_max!r}"
        assert time_min.tzinfo == timezone.utc or time_min.utcoffset().total_seconds() == 0

    # schedule_meeting (no conflict) passes timezone-aware start/end to create_event
    with patch.object(calendar_tools, "list_events", return_value=[]), \
         patch.object(calendar_tools, "create_event", return_value={
             "summary": "Meeting",
             "start": {"dateTime": "2026-07-28T15:00:00+00:00"},
             "end": {"dateTime": "2026-07-28T15:30:00+00:00"},
             "attendees": [],
         }) as mock_create:
        resp = client.post(
            "/schedule_meeting",
            json={"title": "Meeting", "when": "3pm today", "duration_minutes": 30},
        )
        assert resp.status_code == 200, resp.text
        assert mock_create.called
        start = mock_create.call_args.kwargs["start"]
        end = mock_create.call_args.kwargs["end"]
        assert start.tzinfo is not None, f"naive start: {start!r}"
        assert end.tzinfo is not None, f"naive end: {end!r}"

    # /chat: simulated Calendar HttpError → clean 502, not an unhandled 500
    with patch.object(calendar_tools, "list_events", side_effect=_httperror(400)), \
         patch.object(main, "_llm_client") as mock_client_factory:
        mock_client_factory.return_value.chat.completions.create.return_value = _fake_completion(
            "conflict_check", {"when": "tomorrow 3pm"}
        )
        resp = client.post("/chat", json={"message": "am I free at 3pm tomorrow?"})
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert "Google Calendar" in detail
        assert "400" in detail
        assert "Traceback" not in detail

    print("ok")


if __name__ == "__main__":
    demo()
