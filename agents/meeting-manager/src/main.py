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
from datetime import datetime, timedelta

import dateparser
from fastapi import FastAPI, HTTPException
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


@app.post("/fetch_calendar_events", response_model=FetchEventsResponse)
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


@app.post("/conflict_check", response_model=ConflictCheckResponse)
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


@app.post("/schedule_meeting", response_model=ScheduleMeetingResponse)
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
# declares). Matches agentic-invoice-processing's own pattern: that agent's
# deploy manifests also set OPENAI_BASE_URL, and its code never reads it
# explicitly either — the openai SDK picks up OPENAI_API_KEY/OPENAI_BASE_URL
# on its own. We check both explicitly anyway, so a missing one fails with a
# clear message instead of silently hitting the wrong (real OpenAI) endpoint.
_MODEL = os.environ.get("OPENAI_MODEL_ID", "nvidia/nemotron-3-ultra-550b-a55b")

ROUTER_SYSTEM_PROMPT = (
    "You are the Meeting Manager assistant. You help the user view their Google "
    "Calendar and schedule meetings, with conflict detection. Always respond by "
    "calling exactly one tool: fetch_calendar_events to look up events for a "
    "described time range (e.g. \"today\", \"this week\", \"next Friday\"); "
    "conflict_check to test whether a specific proposed time is free without "
    "scheduling anything; or schedule_meeting to book a new meeting once the "
    "user has given a title and a time. Pass the user's time phrase straight "
    "through as the query/when argument — do not convert it to a date "
    "yourself, the tool does that. Default duration_minutes to 30 if "
    "unspecified. Only include attendees the user explicitly named as email "
    "addresses."
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
            "description": "Schedule a new meeting on the calendar after checking for conflicts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
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
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError(
            "OPENAI_API_KEY and OPENAI_BASE_URL must both be set — /chat calls "
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


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        client = _llm_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    history = [{"role": m.role, "content": m.content} for m in (req.state.messages if req.state else [])]
    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": req.message},
    ]

    try:
        completion = client.chat.completions.create(
            model=_MODEL,
            messages=messages,
            tools=ROUTER_TOOLS,
            tool_choice="required",
        )
    except Exception as exc:  # openai SDK's own exception hierarchy — network/auth/API errors
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    tool_calls = completion.choices[0].message.tool_calls
    if not tool_calls:
        raise HTTPException(status_code=502, detail="Router did not select a tool")
    tool_call = tool_calls[0]
    tool_name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Router returned malformed arguments: {exc}") from exc

    try:
        if tool_name == "fetch_calendar_events":
            result = fetch_calendar_events(FetchEventsRequest(**args))
        elif tool_name == "conflict_check":
            result = conflict_check(ConflictCheckRequest(**args))
        elif tool_name == "schedule_meeting":
            result = schedule_meeting(ScheduleMeetingRequest(**args))
        else:
            raise HTTPException(status_code=502, detail=f"Router selected an unknown tool: {tool_name!r}")
    except ValueError as exc:  # e.g. missing/malformed required argument from the LLM
        raise HTTPException(status_code=422, detail=f"Could not act on that request: {exc}") from exc

    reply = _format_reply(tool_name, result)
    state = _with_turn(req.state, req.message, role="user")
    state = _with_turn(state, reply)
    return ChatResponse(reply=reply, state=state)
