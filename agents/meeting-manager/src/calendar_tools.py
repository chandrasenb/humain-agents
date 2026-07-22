"""Google Calendar API wrapper.

Uses the OAuth access token the HUMAIN platform injects into the container
at runtime (see agent.yaml `requires: [google]`) — no service-account key,
no local OAuth flow.
"""

from __future__ import annotations

import os
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

CALENDAR_ID = "primary"


def build_service() -> Resource:
    token = os.environ.get("GOOGLE_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "GOOGLE_ACCESS_TOKEN is not set — this agent requires the Google "
            "connector (agent.yaml `requires: [google]`); the platform injects "
            "this at sandbox spin-up."
        )
    credentials = Credentials(token=token)
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def list_events(time_min: datetime, time_max: datetime) -> list[dict]:
    """Events on the primary calendar in [time_min, time_max), expanded and sorted."""
    service = build_service()
    response = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=_iso(time_min),
            timeMax=_iso(time_max),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return response.get("items", [])


def check_conflicts(start: datetime, end: datetime) -> list[dict]:
    """Events already on the calendar that overlap [start, end)."""
    return list_events(start, end)


def create_event(
    title: str,
    start: datetime,
    end: datetime,
    attendees: list[str] | None = None,
    description: str | None = None,
) -> dict:
    service = build_service()
    body = {
        "summary": title,
        "start": {"dateTime": _iso(start)},
        "end": {"dateTime": _iso(end)},
    }
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]

    return (
        service.events()
        .insert(calendarId=CALENDAR_ID, body=body, sendUpdates="all" if attendees else "none")
        .execute()
    )
