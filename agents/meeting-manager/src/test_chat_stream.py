"""Self-check for /chat/stream SSE (token chunks + final done event).

Mocks calendar_tools + a streaming LLM tool-call response — no network.
Run with: python test_chat_stream.py
"""

from __future__ import annotations

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


def _delta_chunk(*, name: str | None = None, arguments: str | None = None, call_id: str | None = None):
    chunk = MagicMock()
    tc = MagicMock()
    tc.index = 0
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    delta = MagicMock()
    delta.tool_calls = [tc]
    delta.content = None
    choice = MagicMock()
    choice.delta = delta
    chunk.choices = [choice]
    return chunk


def _fake_stream(tool_name: str, args: dict):
    """Simulate OpenAI-compatible tool-call streaming: name first, then args."""
    return iter(
        [
            _delta_chunk(call_id="call_abc", name=tool_name, arguments=""),
            _delta_chunk(arguments=json.dumps(args)),
        ]
    )


def _parse_sse(body: str) -> list:
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_lines = [
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            events.append("[DONE]")
        else:
            events.append(json.loads(data))
    return events


def demo() -> None:
    with patch.object(calendar_tools, "list_events", return_value=[_FAKE_EVENT]), \
         patch.object(main, "_llm_client") as mock_client_factory, \
         patch.object(main.time, "sleep"):  # don't slow the test for word pacing
        mock_client_factory.return_value.chat.completions.create.return_value = _fake_stream(
            "fetch_calendar_events", {"query": "this week"}
        )
        with client.stream("POST", "/chat/stream", json={"message": "what's on my calendar?"}) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            raw = "".join(resp.iter_text())

    events = _parse_sse(raw)
    assert events[-1] == "[DONE]"
    typed = [e for e in events if isinstance(e, dict)]
    assert any(e.get("type") == "status" and e.get("phase") == "routing" for e in typed)
    tokens = [e["content"] for e in typed if e.get("type") == "token"]
    assert tokens, "expected incremental token events"
    # Tokens should arrive as multiple chunks, not one blob
    assert len(tokens) > 1
    full = "".join(tokens)
    assert "Sprint Planning" in full or "event" in full.lower() or "found" in full.lower()

    done = next(e for e in typed if e.get("type") == "done")
    assert done["events"] is not None
    assert done["events"][0]["title"] == "Sprint Planning"
    assert done["state"]["messages"]
    assert done["reply"] == full

    # create() must have been called with stream=True
    create_kwargs = mock_client_factory.return_value.chat.completions.create.call_args.kwargs
    assert create_kwargs.get("stream") is True

    print("ok")


if __name__ == "__main__":
    demo()
