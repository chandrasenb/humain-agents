"""Self-check for /chat's events field (the frontend's card-carousel data).

Mocks calendar_tools + the LLM tool-call response — no network, no Google
API, no real NIM call. Run with: python test_chat_events.py
"""

import json
import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import calendar_tools
import main

os.environ.setdefault("OPENAI-API-KEY", "fake")
os.environ.setdefault("OPENAI-BASE-URL", "https://fake.invalid/v1")

client = TestClient(main.app)

_FAKE_EVENT = {
    "summary": "Sprint Planning",
    "start": {"dateTime": "2026-07-27T10:00:00-07:00"},
    "end": {"dateTime": "2026-07-27T11:00:00-07:00"},
    "attendees": [{"email": "a@x.com"}],
    "hangoutLink": "https://meet.google.com/abc-defg-hij",
}


def _fake_completion(tool_name: str, args: dict) -> MagicMock:
    tool_call = MagicMock()
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(args)
    completion = MagicMock()
    completion.choices[0].message.tool_calls = [tool_call]
    return completion


def demo() -> None:
    # fetch_calendar_events -> events populated from the events found
    with patch.object(calendar_tools, "list_events", return_value=[_FAKE_EVENT]), \
         patch.object(main, "_llm_client") as mock_client_factory:
        mock_client_factory.return_value.chat.completions.create.return_value = _fake_completion(
            "fetch_calendar_events", {"query": "this week"}
        )
        resp = client.post("/chat", json={"message": "what's on my calendar?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] is not None
        assert body["events"][0]["title"] == "Sprint Planning"
        assert body["events"][0]["meeting_link"] == "https://meet.google.com/abc-defg-hij"

    # schedule_meeting (scheduled) -> events populated from the one created event
    with patch.object(calendar_tools, "list_events", return_value=[]), \
         patch.object(calendar_tools, "create_event", return_value=_FAKE_EVENT), \
         patch.object(main, "_llm_client") as mock_client_factory:
        mock_client_factory.return_value.chat.completions.create.return_value = _fake_completion(
            "schedule_meeting", {"title": "Sprint Planning", "when": "tomorrow 10am"}
        )
        resp = client.post("/chat", json={"message": "schedule sprint planning tomorrow at 10am"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] is not None
        assert body["events"][0]["title"] == "Sprint Planning"

    # schedule_meeting (conflict) -> no event was created, events stays None
    with patch.object(calendar_tools, "list_events", return_value=[_FAKE_EVENT]), \
         patch.object(main, "_llm_client") as mock_client_factory:
        mock_client_factory.return_value.chat.completions.create.return_value = _fake_completion(
            "schedule_meeting", {"title": "Sprint Planning", "when": "2026-07-27T10:30:00-07:00"}
        )
        resp = client.post("/chat", json={"message": "schedule sprint planning at 10:30"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] is None

    # conflict_check -> events was never meant to carry cards for this tool
    with patch.object(calendar_tools, "list_events", return_value=[]), \
         patch.object(main, "_llm_client") as mock_client_factory:
        mock_client_factory.return_value.chat.completions.create.return_value = _fake_completion(
            "conflict_check", {"when": "tomorrow 2pm"}
        )
        resp = client.post("/chat", json={"message": "is tomorrow at 2pm free?"})
        assert resp.status_code == 200
        assert resp.json()["events"] is None

    print("ok")


if __name__ == "__main__":
    demo()
