"""Confirm AccessTokenCredentials never raises RefreshError on 401 retry."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Allow `python src/test_access_token_credentials.py` from the agent root.
sys.path.insert(0, os.path.dirname(__file__))

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from calendar_tools import AccessTokenCredentials, list_events


class _FakeHttpResponse:
    def __init__(self, status: int):
        self.status = status
        self.reason = "Unauthorized" if status == 401 else "OK"


def test_refresh_is_noop():
    creds = AccessTokenCredentials(token="fake-access-token")
    # Must not raise RefreshError (the old Credentials(token=...) path did).
    creds.refresh(request=None)
    assert creds.token == "fake-access-token"
    assert creds.valid is True


def test_legacy_credentials_refresh_raises():
    """Document the bug we fixed: bare Credentials(token=) cannot refresh."""
    from google.oauth2.credentials import Credentials

    bare = Credentials(token="fake-access-token")
    try:
        bare.refresh(request=None)
        raise AssertionError("expected RefreshError from bare Credentials.refresh")
    except RefreshError as exc:
        assert "refresh_token" in str(exc)


def test_list_events_surfaces_http_401_not_refresh_error():
    """Simulate Google's 401 → httplib2 refresh retry → still 401."""
    os.environ["GOOGLE_ACCESS_TOKEN"] = "fake-access-token"

    calls = {"n": 0}

    def fake_request(self, uri, method="GET", body=None, headers=None, **kwargs):
        calls["n"] += 1
        # Always 401 — mirrors an invalid/expired injected access token.
        return _FakeHttpResponse(401), b'{"error": {"message": "Invalid Credentials"}}'

    # AuthorizedHttp wraps httplib2.Http; patch the underlying transport so a
    # Google 401 triggers the refresh-retry path inside google_auth_httplib2.
    with patch("httplib2.Http.request", fake_request):
        try:
            list_events(
                datetime.now(timezone.utc),
                datetime.now(timezone.utc) + timedelta(hours=1),
            )
            raise AssertionError("expected HttpError")
        except RefreshError as exc:
            raise AssertionError(f"RefreshError must not be raised, got: {exc}") from exc
        except HttpError as exc:
            assert exc.resp.status == 401, exc

    # google_auth_httplib2 retries once after refresh(); both attempts 401.
    assert calls["n"] >= 1


if __name__ == "__main__":
    test_refresh_is_noop()
    test_legacy_credentials_refresh_raises()
    test_list_events_surfaces_http_401_not_refresh_error()
    print("OK: AccessTokenCredentials avoids RefreshError; 401 surfaces as HttpError")
