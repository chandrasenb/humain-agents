"""Google Calendar API wrapper.

Uses the OAuth access token the HUMAIN platform injects into the container
at runtime (see agent.yaml `connectors: [google]`) — no service-account key,
no local OAuth flow. The token is short-lived and non-refreshable in-process;
the platform re-injects a fresh one on the next sandbox spin-up.
"""

from __future__ import annotations

import os
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

CALENDAR_ID = "primary"


class AccessTokenCredentials(Credentials):
    """Bearer-token credentials that never attempt OAuth refresh.

    ``Credentials(token=...)`` alone is enough for the first request, but when
    Google returns HTTP 401 ``google_auth_httplib2`` calls ``refresh()`` and
    retries. Without ``refresh_token`` / ``client_id`` / ``client_secret`` that
    raises ``RefreshError`` and masks the real 401. A no-op ``refresh`` lets
    the library retry once with the same token and then surface the HTTP error.
    """

    def refresh(self, request):  # noqa: ARG002
        return


def build_service() -> Resource:
    token = os.environ.get("GOOGLE_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "GOOGLE_ACCESS_TOKEN is not set — this agent requires the Google "
            "connector (agent.yaml connectors); the platform injects "
            "this at sandbox spin-up."
        )
    credentials = AccessTokenCredentials(token=token)
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
