"""Meeting Manager agent — FastAPI entry point.

Three tool nodes (see ../agent.yaml): fetch_calendar_events, conflict_check,
and schedule_meeting. Each endpoint accepts an optional `state` object
carrying the conversation history so far and returns it with a new turn
appended — the graph runtime threads this straight back in on the next
node call (in-context memory only; no Redis/pgvector yet — HUMAIN-1966/1967).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import dateparser
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import calendar_tools
from time_resolver import resolve_timerange

app = FastAPI(title="meeting-manager")

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


def _with_turn(state: ConversationState | None, content: str) -> ConversationState:
    state = state or ConversationState()
    return ConversationState(messages=[*state.messages, Message(role="assistant", content=content)])


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
