"""Meeting Manager agent — FastAPI entry point.

Three tool endpoints (see ../agent.yaml): fetch_calendar_events,
conflict_check, and schedule_meeting. /chat is the natural-language entry
point (../agent.yaml's `router` node): an LLM call picks one of the three
and extracts its arguments, this module dispatches to it directly (no HTTP
self-call), then a deterministic template turns the structured result into
a reply. Each endpoint accepts an optional `state` object carrying the
conversation history so far and returns it with a new turn appended — the
graph runtime threads this straight back in on the next node call
(in-context memory only; no Redis/pgvector yet — HUMAIN-1966/1967).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import dateparser
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

import calendar_tools
from time_resolver import resolve_timerange

app = FastAPI(title="meeting-manager")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


SEARCH_WINDOW_HOURS = 6
SLOT_STEP_MINUTES = 30
MAX_ALTERNATIVES = 3


def _account_email() -> str | None:
    return os.environ.get("GOOGLE_ACCOUNT_EMAIL")


# ── Conversation state (in-context memory) ──────────────────────────────────


class Message(BaseModel):
    role: str
    content: str


class ConversationState(BaseModel):
    messages: list[Message] = Field(default_factory=list)


def _with_turn(state: ConversationState | None, content: str, role: str = "assistant") -> ConversationState:
    state = state or ConversationState()
    return ConversationState(messages=[*state.messages, Message(role=role, content=content)])


# ── /fetch_calendar_events ───────────────────────────────────────────────────


class FetchEventsRequest(BaseModel):
    query: str = "this week"  # natural language: "today", "this week", "next Friday", ...
    state: ConversationState | None = None


class EventSummary(BaseModel):
    title: str
    start: str
    end: str
    attendees: list[str]
    meeting_link: str | None
    duration_minutes: int


class FetchEventsResponse(BaseModel):
    events: list[EventSummary]
    state: ConversationState


def _to_event_summary(raw: dict) -> EventSummary:
    start_raw = raw.get("start", {}).get("dateTime") or raw.get("start", {}).get("date")
    end_raw = raw.get("end", {}).get("dateTime") or raw.get("end", {}).get("date")
    start = dateparser.parse(start_raw) if start_raw else None
    end = dateparser.parse(end_raw) if end_raw else None
    duration = int((end - start).total_seconds() // 60) if start and end else 0
    return EventSummary(
        title=raw.get("summary", "(no title)"),
        start=start_raw or "",
        end=end_raw or "",
        attendees=[a.get("email") for a in raw.get("attendees", []) if a.get("email")],
        meeting_link=raw.get("hangoutLink"),
        duration_minutes=duration,
    )


# include_in_schema=False on this and the other internal tool endpoints below
# (EB-0040): OpenAPI-driven consumers like Prism discover the chat contract by
# scanning POST routes, and an internal tool endpoint sorting before /chat can
# get wired up as the "chat" endpoint by mistake.
@app.post(
    "/fetch_calendar_events",
    response_model=FetchEventsResponse,
    include_in_schema=False,
)
def fetch_calendar_events(req: FetchEventsRequest) -> FetchEventsResponse:
    try:
        time_min, time_max = resolve_timerange(req.query, account_email=_account_email())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    raw_events = calendar_tools.list_events(time_min, time_max)
    events = [_to_event_summary(e) for e in raw_events]
    state = _with_turn(req.state, f"Found {len(events)} event(s) for {req.query!r}.")
    return FetchEventsResponse(events=events, state=state)


# ── Shared conflict-detection logic (used by /conflict_check and /schedule_meeting) ──


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


class AlternativeSlot(BaseModel):
    start: str
    end: str


def _find_alternatives(
    desired_start: datetime,
    duration: timedelta,
    busy_events: list[dict],
) -> list[AlternativeSlot]:
    busy_windows = []
    for raw in busy_events:
        start_raw = raw.get("start", {}).get("dateTime")
        end_raw = raw.get("end", {}).get("dateTime")
        if start_raw and end_raw:
            busy_windows.append((dateparser.parse(start_raw), dateparser.parse(end_raw)))

    alternatives: list[AlternativeSlot] = []
    cursor = desired_start
    search_limit = desired_start + timedelta(hours=SEARCH_WINDOW_HOURS)
    while cursor < search_limit and len(alternatives) < MAX_ALTERNATIVES:
        cursor += timedelta(minutes=SLOT_STEP_MINUTES)
        candidate_end = cursor + duration
        if not any(_overlaps(cursor, candidate_end, b_start, b_end) for b_start, b_end in busy_windows):
            alternatives.append(AlternativeSlot(start=cursor.isoformat(), end=candidate_end.isoformat()))
    return alternatives


class _ConflictResult(BaseModel):
    status: str  # "clear" | "conflict" — string enum so the H2O-O edge `when`
    #                 condition can use the documented `$.status == clear` idiom
    #                 rather than an undocumented boolean-literal comparison.
    has_conflict: bool
    conflicts: list[EventSummary] = Field(default_factory=list)
    suggested_alternatives: list[AlternativeSlot] = Field(default_factory=list)


def _check_conflicts(start: datetime, duration: timedelta) -> _ConflictResult:
    end = start + duration
    # One window wide enough to both check the requested slot and search for
    # alternatives, fetched once.
    window_events = calendar_tools.list_events(start, start + timedelta(hours=SEARCH_WINDOW_HOURS))
    conflicts = [
        e
        for e in window_events
        if e.get("start", {}).get("dateTime")
        and _overlaps(start, end, dateparser.parse(e["start"]["dateTime"]), dateparser.parse(e["end"]["dateTime"]))
    ]
    if not conflicts:
        return _ConflictResult(status="clear", has_conflict=False)

    return _ConflictResult(
        status="conflict",
        has_conflict=True,
        conflicts=[_to_event_summary(e) for e in conflicts],
        suggested_alternatives=_find_alternatives(start, duration, window_events),
    )


# ── /conflict_check ───────────────────────────────────────────────────────────


class ConflictCheckRequest(BaseModel):
    when: str  # natural language start time, e.g. "tomorrow 2pm"
    duration_minutes: int = 30
    state: ConversationState | None = None


class ConflictCheckResponse(BaseModel):
    status: str
    has_conflict: bool
    conflicts: list[EventSummary]
    suggested_alternatives: list[AlternativeSlot]
    state: ConversationState


@app.post(
    "/conflict_check",
    response_model=ConflictCheckResponse,
    include_in_schema=False,
)
def conflict_check(req: ConflictCheckRequest) -> ConflictCheckResponse:
    start = _parse_time(req.when)
    result = _check_conflicts(start, timedelta(minutes=req.duration_minutes))
    note = "No conflicts found." if result.status == "clear" else f"{len(result.conflicts)} conflict(s) found."
    state = _with_turn(req.state, note)
    return ConflictCheckResponse(**result.model_dump(), state=state)


def _parse_time(text: str) -> datetime:
    parsed = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future"})
    if parsed is None:
        raise HTTPException(status_code=422, detail=f"Could not understand the time expression: {text!r}")
    return parsed


# ── /schedule_meeting ─────────────────────────────────────────────────────────


class ScheduleMeetingRequest(BaseModel):
    title: str
    when: str  # natural language start time, e.g. "tomorrow 2pm"
    duration_minutes: int = 30
    attendees: list[str] = Field(default_factory=list)
    description: str | None = None
    state: ConversationState | None = None


class ScheduleMeetingResponse(BaseModel):
    status: str  # "scheduled" | "conflict"
    event: dict | None = None
    conflicts: list[EventSummary] = Field(default_factory=list)
    suggested_alternatives: list[AlternativeSlot] = Field(default_factory=list)
    state: ConversationState


@app.post(
    "/schedule_meeting",
    response_model=ScheduleMeetingResponse,
    include_in_schema=False,
)
def schedule_meeting(req: ScheduleMeetingRequest) -> ScheduleMeetingResponse:
    start = _parse_time(req.when)
    duration = timedelta(minutes=req.duration_minutes)

    # Self-sufficient: re-checks conflicts even if a conflict_check node
    # already ran earlier in the graph, so this endpoint is also safe to
    # call directly.
    conflict = _check_conflicts(start, duration)
    if conflict.has_conflict:
        state = _with_turn(
            req.state,
            f"{req.title!r} conflicts with {len(conflict.conflicts)} existing event(s); suggested alternatives.",
        )
        return ScheduleMeetingResponse(
            status="conflict",
            conflicts=conflict.conflicts,
            suggested_alternatives=conflict.suggested_alternatives,
            state=state,
        )

    event = calendar_tools.create_event(
        title=req.title,
        start=start,
        end=start + duration,
        attendees=req.attendees,
        description=req.description,
    )
    state = _with_turn(req.state, f"Scheduled {req.title!r} at {start.isoformat()}.")
    return ScheduleMeetingResponse(status="scheduled", event=event, state=state)


# ── /chat (natural-language entry point — ../agent.yaml's `router` node) ─────

# NVIDIA NIM's OpenAI-compatible API (same model agent.yaml's `router` node
# declares). The platform's shared pool-secret env vars use hyphens, not
# underscores (confirmed via kubectl against the running pod's envFrom
# secret) — OPENAI-API-KEY / OPENAI-BASE-URL / OPENAI-MODEL-ID, not the
# OPENAI_* names the openai SDK would pick up on its own, hence the explicit
# reads below instead of relying on the SDK's implicit env lookup.
_MODEL = os.environ.get("OPENAI-MODEL-ID", "nvidia/nemotron-3-ultra-550b-a55b")

ROUTER_SYSTEM_PROMPT = (
    "You are the Meeting Manager assistant. You help the user view their Google "
    "Calendar and schedule meetings, with conflict detection.\n\n"
    "When to call a tool:\n"
    "- fetch_calendar_events — look up events for a described time range "
    "(e.g. \"today\", \"this week\", \"next Friday\", \"what's on my calendar\").\n"
    "- conflict_check — test whether a specific proposed time is free without "
    "scheduling anything (e.g. \"is tomorrow at 2pm free?\").\n"
    "- schedule_meeting — book a new meeting when the user wants to schedule "
    "something and has given at least a time. If they omit a title "
    "(e.g. \"schedule something at 3pm today\"), still call schedule_meeting "
    "with a sensible default title such as \"Meeting\".\n\n"
    "When NOT to call a tool — reply with a short plain-text message instead:\n"
    "- Greetings and small talk (e.g. \"hi\", \"hello\", \"thanks\") — greet "
    "them back and briefly offer to help with calendar or scheduling. Do not "
    "fetch their calendar.\n"
    "- Cancel, delete, reschedule, or modify an existing event "
    "(e.g. \"cancel my 2pm\", \"delete tomorrow's standup\") — you have no "
    "tool for this. Politely explain that you can only view the calendar and "
    "schedule new meetings, not cancel or change existing ones. Do not fetch "
    "their calendar as a substitute.\n\n"
    "Pass the user's time phrase straight through as the query/when argument — "
    "do not convert it to a date yourself, the tool does that. Default "
    "duration_minutes to 30 if unspecified. Only include attendees the user "
    "explicitly named as email addresses."
)

ROUTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_calendar_events",
            "description": "Look up existing calendar events for a natural-language time range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language time range, e.g. 'today', 'this week', 'next Friday'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "conflict_check",
            "description": (
                "Check whether a proposed meeting time conflicts with existing events, "
                "without scheduling anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "Natural language start time, e.g. 'tomorrow 2pm'.",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Meeting length in minutes.",
                        "default": 30,
                    },
                },
                "required": ["when"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": (
                "Schedule a new meeting on the calendar after checking for conflicts. "
                "Use even when the user gives only a time and no title "
                "(supply a default title such as 'Meeting')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Meeting title. If the user did not specify one, use a "
                            "sensible default such as 'Meeting'."
                        ),
                    },
                    "when": {
                        "type": "string",
                        "description": "Natural language start time, e.g. 'tomorrow 2pm'.",
                    },
                    "duration_minutes": {"type": "integer", "default": 30},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attendee email addresses explicitly mentioned by the user.",
                    },
                    "description": {"type": "string"},
                },
                "required": ["title", "when"],
            },
        },
    },
]


def _llm_client() -> OpenAI:
    api_key = os.environ.get("OPENAI-API-KEY")
    base_url = os.environ.get("OPENAI-BASE-URL")
    if not api_key or not base_url:
        raise RuntimeError(
            "OPENAI-API-KEY and OPENAI-BASE-URL must both be set — /chat calls "
            "NVIDIA NIM's OpenAI-compatible API through them."
        )
    return OpenAI(api_key=api_key, base_url=base_url)


def _format_reply(tool_name: str, result: BaseModel) -> str:
    """Deterministic (not a second LLM call) — see README 'Why format_response
    isn't an LLM call'. Structured calendar results template cleanly; a
    second model round-trip would only add cost, latency, and a new failure
    mode for no real benefit here."""
    if tool_name == "fetch_calendar_events":
        events = result.events
        if not events:
            return "You have nothing on your calendar for that range."
        lines = [f"- {e.title} ({e.start} to {e.end})" for e in events]
        return f"Here's what I found ({len(events)} event(s)):\n" + "\n".join(lines)

    if tool_name == "conflict_check":
        if not result.has_conflict:
            return "That time is free — no conflicts."
        alternatives = ", ".join(a.start for a in result.suggested_alternatives)
        return (
            f"That time conflicts with {len(result.conflicts)} existing event(s). "
            f"Free alternatives: {alternatives or 'none found in the next few hours'}."
        )

    if tool_name == "schedule_meeting":
        if result.status == "scheduled":
            return "Done — it's on your calendar."
        alternatives = ", ".join(a.start for a in result.suggested_alternatives)
        return (
            f"I couldn't schedule that — it conflicts with {len(result.conflicts)} "
            f"existing event(s). Free alternatives: {alternatives or 'none found in the next few hours'}."
        )

    return "Done."


class ChatRequest(BaseModel):
    message: str
    state: ConversationState | None = None


class ChatResponse(BaseModel):
    reply: str
    state: ConversationState
    # Populated only for fetch_calendar_events (the events found) and a
    # successful schedule_meeting (the one event just created) — the
    # frontend renders these as a card carousel below the reply text.
    # None (not an empty list) when there's nothing to show, so the
    # frontend's "any cards at all?" check is a single falsy check.
    events: list[EventSummary] | None = None


def _router_messages(req: ChatRequest) -> list[dict[str, Any]]:
    history = [{"role": m.role, "content": m.content} for m in (req.state.messages if req.state else [])]
    return [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": req.message},
    ]


def _select_tool_blocking(
    client: OpenAI, messages: list[dict[str, Any]]
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Non-streaming router call — used by /chat.

    Returns (tool_name, args, None) when the model picks a tool, or
    (None, None, content) when it replies conversationally with no tool call
    (greetings, cancel/modify declines, etc.). tool_choice="auto" allows both.
    """
    try:
        completion = client.chat.completions.create(
            model=_MODEL,
            messages=messages,
            tools=ROUTER_TOOLS,
            tool_choice="auto",
        )
    except Exception as exc:  # openai SDK's own exception hierarchy — network/auth/API errors
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    message = completion.choices[0].message
    tool_calls = message.tool_calls
    if tool_calls:
        tool_call = tool_calls[0]
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail=f"Router returned malformed arguments: {exc}") from exc
        return tool_call.function.name, args, None

    content = (message.content or "").strip()
    if content:
        return None, None, content
    raise HTTPException(status_code=502, detail="Router returned neither a tool call nor a reply")


def _select_tool_streaming(
    client: OpenAI, messages: list[dict[str, Any]]
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Stream the router's decision, accumulating deltas until complete.

    With tool_choice="auto" the model may emit tool_call deltas OR content
    tokens (plain conversational reply). We assemble whichever arrives and
    return (tool_name, args, None) or (None, None, content).
    """
    try:
        stream = client.chat.completions.create(
            model=_MODEL,
            messages=messages,
            tools=ROUTER_TOOLS,
            tool_choice="auto",
            stream=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    # index → {id, name, arguments} — OpenAI may interleave multiple tool
    # calls by index; we only ever expect at most one.
    accumulated: dict[int, dict[str, str]] = {}
    content_parts: list[str] = []
    try:
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta:
                continue
            if delta.content:
                content_parts.append(delta.content)
            if not delta.tool_calls:
                continue
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index if tc_delta.index is not None else 0
                slot = accumulated.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc_delta.id:
                    slot["id"] += tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        slot["name"] += tc_delta.function.name
                    if tc_delta.function.arguments:
                        slot["arguments"] += tc_delta.function.arguments
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM stream failed: {exc}") from exc

    if accumulated:
        tool = accumulated[min(accumulated)]
        tool_name = tool["name"]
        if not tool_name:
            raise HTTPException(status_code=502, detail="Router did not select a tool")
        try:
            args = json.loads(tool["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail=f"Router returned malformed arguments: {exc}") from exc
        return tool_name, args, None

    content = "".join(content_parts).strip()
    if content:
        return None, None, content
    raise HTTPException(status_code=502, detail="Router returned neither a tool call nor a reply")


def _dispatch_tool(tool_name: str, args: dict[str, Any]) -> BaseModel:
    try:
        if tool_name == "fetch_calendar_events":
            return fetch_calendar_events(FetchEventsRequest(**args))
        if tool_name == "conflict_check":
            return conflict_check(ConflictCheckRequest(**args))
        if tool_name == "schedule_meeting":
            return schedule_meeting(ScheduleMeetingRequest(**args))
        raise HTTPException(status_code=502, detail=f"Router selected an unknown tool: {tool_name!r}")
    except ValueError as exc:  # e.g. missing/malformed required argument from the LLM
        raise HTTPException(status_code=422, detail=f"Could not act on that request: {exc}") from exc


def _events_for_reply(tool_name: str, result: BaseModel) -> list[EventSummary] | None:
    if tool_name == "fetch_calendar_events":
        return result.events or None
    if tool_name == "schedule_meeting" and result.status == "scheduled" and result.event:
        return [_to_event_summary(result.event)]
    return None


def _run_chat_turn(
    req: ChatRequest,
    *,
    stream_tool_call: bool = False,
) -> tuple[str, ConversationState, list[EventSummary] | None]:
    """Shared /chat + /chat/stream pipeline: route → (tool | plain reply) → format."""
    try:
        client = _llm_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    messages = _router_messages(req)
    if stream_tool_call:
        tool_name, args, plain_reply = _select_tool_streaming(client, messages)
    else:
        tool_name, args, plain_reply = _select_tool_blocking(client, messages)

    if tool_name is None:
        reply = plain_reply or ""
        events = None
    else:
        result = _dispatch_tool(tool_name, args or {})
        events = _events_for_reply(tool_name, result)
        reply = _format_reply(tool_name, result)

    state = _with_turn(req.state, req.message, role="user")
    state = _with_turn(state, reply)
    return reply, state, events


def _chunk_reply_words(reply: str) -> Iterator[str]:
    """Yield reply text word-by-word for SSE streaming.

    `_format_reply` is a deterministic template (not an LLM call — see README),
    so the full string already exists; we chunk it artificially so the UI can
    render progressive text instead of dumping the whole reply at once.
    """
    if not reply:
        return
    # Split on whitespace but keep the separators so spacing is preserved.
    parts = reply.split(" ")
    for i, part in enumerate(parts):
        chunk = part if i == len(parts) - 1 else part + " "
        if chunk:
            yield chunk


def _sse_data(payload: Any) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, default=str)}\n\n"


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    reply, state, events = _run_chat_turn(req, stream_tool_call=False)
    return ChatResponse(reply=reply, state=state, events=events)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE streaming variant of /chat.

    1. Stream-accumulate the LLM's routing decision (tool call or plain reply —
       tool_choice="auto").
    2. If a tool was chosen, execute it (blocking calendar call); otherwise use
       the model's conversational content as the reply.
    3. Stream the reply word-by-word as `{"type":"token","content":…}`
       events, then a final `{"type":"done","state":…,"events":…}` followed by
       the literal `[DONE]` sentinel.
    """

    def generate() -> Iterator[str]:
        try:
            # Establish the SSE stream immediately so the client can show a
            # typing indicator while the router LLM call is in flight.
            yield _sse_data({"type": "status", "phase": "routing"})

            try:
                client = _llm_client()
            except RuntimeError as exc:
                yield _sse_data({"type": "error", "detail": str(exc)})
                yield _sse_data("[DONE]")
                return

            messages = _router_messages(req)
            try:
                tool_name, args, plain_reply = _select_tool_streaming(client, messages)
            except HTTPException as exc:
                yield _sse_data({"type": "error", "detail": exc.detail})
                yield _sse_data("[DONE]")
                return
            except Exception as exc:
                yield _sse_data({"type": "error", "detail": f"LLM call failed: {exc}"})
                yield _sse_data("[DONE]")
                return

            if tool_name is None:
                reply = plain_reply or ""
                events = None
            else:
                yield _sse_data({"type": "status", "phase": "tool", "tool": tool_name})

                try:
                    result = _dispatch_tool(tool_name, args or {})
                except HTTPException as exc:
                    yield _sse_data({"type": "error", "detail": exc.detail})
                    yield _sse_data("[DONE]")
                    return

                events = _events_for_reply(tool_name, result)
                reply = _format_reply(tool_name, result)

            state = _with_turn(req.state, req.message, role="user")
            state = _with_turn(state, reply)

            yield _sse_data({"type": "status", "phase": "reply"})

            for word in _chunk_reply_words(reply):
                yield _sse_data({"type": "token", "content": word})
                # Tiny pause so curl -N / browsers actually observe incremental
                # chunks rather than one buffered flush at the end.
                time.sleep(0.025)

            events_payload = [e.model_dump() for e in events] if events else None
            yield _sse_data(
                {
                    "type": "done",
                    "reply": reply,
                    "state": state.model_dump(),
                    "events": events_payload,
                }
            )
            yield _sse_data("[DONE]")
        except Exception as exc:  # last-resort — never leave the client hanging
            yield _sse_data({"type": "error", "detail": str(exc)})
            yield _sse_data("[DONE]")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Static frontend (Dockerfile stage 2 copies frontend/dist here) ──────────
# Mounted last so it never shadows the API routes above — Starlette matches
# routes in registration order. Guarded by existence so `uvicorn src.main:app`
# still works for backend-only local dev/tests without a frontend build.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
