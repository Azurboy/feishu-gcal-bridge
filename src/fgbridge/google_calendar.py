"""Google Calendar operations constrained to this installation's calendar."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .storage import atomic_private_write, home


SCOPE = "https://www.googleapis.com/auth/calendar.app.created"


class GoogleError(RuntimeError):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def event_id(install_id: str, source_key: str, generation: int) -> str:
    return hashlib.sha256(f"{install_id}\0{source_key}\0{generation}".encode()).hexdigest()


def _error_reason(exc: HttpError) -> str:
    try:
        body = json.loads(exc.content)
        return str(body["error"]["errors"][0].get("reason", ""))
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


@dataclass(frozen=True)
class RemoteEvent:
    id: str
    data: dict[str, Any]


class GoogleCalendar:
    def __init__(self, credentials: Credentials, token_path: Path) -> None:
        if not credentials.has_scopes([SCOPE]):
            raise GoogleError("google_missing_scope")
        self.credentials = credentials
        self.token_path = token_path
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @classmethod
    def connect(cls, client_json: Path, interactive: bool = False) -> GoogleCalendar:
        token_path = home() / "token.json"
        credentials: Credentials | None = None
        if token_path.exists():
            if token_path.is_symlink():
                raise GoogleError("google_token_symlink")
            credentials = Credentials.from_authorized_user_file(str(token_path))
            if not credentials.valid and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                    atomic_private_write(token_path, credentials.to_json().encode())
                except RefreshError as exc:
                    if not interactive:
                        raise GoogleError("google_needs_auth") from exc
                    credentials = None
        if credentials is None or not credentials.valid:
            if not interactive:
                raise GoogleError("google_needs_auth")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_json), scopes=[SCOPE])
            credentials = flow.run_local_server(port=0, open_browser=True)
            if not credentials.has_scopes([SCOPE]):
                raise GoogleError("google_missing_scope")
            atomic_private_write(token_path, credentials.to_json().encode())
        return cls(credentials, token_path)

    def _execute(self, request: Any) -> Any:
        refreshed = False
        for attempt in range(4):
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                status = int(exc.resp.status)
                reason = _error_reason(exc)
                if status == 401 and not refreshed:
                    refreshed = True
                    try:
                        self.credentials.refresh(Request())
                        atomic_private_write(self.token_path, self.credentials.to_json().encode())
                    except RefreshError as refresh_exc:
                        raise GoogleError("google_needs_auth", status) from refresh_exc
                    continue
                retryable = status in {429, 500, 502, 503, 504} or (
                    status == 403 and reason in {"rateLimitExceeded", "userRateLimitExceeded"}
                )
                if retryable and attempt < 3:
                    retry_after = exc.resp.get("retry-after")
                    try:
                        delay = min(float(retry_after), 20) if retry_after else 0
                    except (TypeError, ValueError):
                        delay = 0
                    time.sleep(max(delay, min(2**attempt + random.random(), 8)))
                    continue
                if status in {404, 410, 409, 412}:
                    raise GoogleError(f"google_http_{status}", status) from exc
                if status == 403:
                    raise GoogleError("google_forbidden", status) from exc
                raise GoogleError("google_api_error", status) from exc
            except RefreshError as exc:
                raise GoogleError("google_needs_auth") from exc
        raise GoogleError("google_retry_exhausted")

    def create_calendar(self, name: str, zone_name: str, install_id: str) -> str:
        body = {
            "summary": name,
            "timeZone": zone_name,
            "description": f"Managed by Feishu Calendar Bridge; installation {install_id}",
        }
        result = self._execute(self.service.calendars().insert(body=body))
        return str(result["id"])

    def assert_calendar(self, calendar_id: str, install_id: str) -> None:
        if not calendar_id or calendar_id == "primary":
            raise GoogleError("unsafe_target_calendar")
        try:
            info = self._execute(self.service.calendars().get(calendarId=calendar_id))
        except GoogleError as exc:
            if exc.status == 404:
                raise GoogleError("target_calendar_missing") from exc
            raise
        if info.get("id") != calendar_id or (
            f"installation {install_id}" not in info.get("description", "")
        ):
            raise GoogleError("target_calendar_binding_mismatch")

    def list_managed(self, calendar_id: str, install_id: str) -> dict[str, RemoteEvent]:
        output: dict[str, RemoteEvent] = {}
        page: str | None = None
        while True:
            response = self._execute(
                self.service.events().list(
                    calendarId=calendar_id,
                    privateExtendedProperty=f"fgbridge_install={install_id}",
                    maxResults=2500,
                    pageToken=page,
                    singleEvents=False,
                    showDeleted=False,
                )
            )
            for item in response.get("items", []):
                props = item.get("extendedProperties", {}).get("private", {})
                if props.get("fgbridge_install") != install_id:
                    continue
                key = props.get("fgbridge_key")
                if not key:
                    raise GoogleError("mirror_missing_identity")
                if key in output:
                    raise GoogleError("duplicate_mirror_identity")
                output[key] = RemoteEvent(str(item["id"]), item)
            page = response.get("nextPageToken")
            if not page:
                return output

    def get(self, calendar_id: str, google_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            item = self._execute(self.service.events().get(calendarId=calendar_id, eventId=google_id))
            if item.get("status") == "cancelled":
                return "tombstone", item
            return "exists", item
        except GoogleError as exc:
            if exc.status == 404:
                return "missing", None
            if exc.status == 410:
                return "tombstone", None
            raise

    def insert(self, calendar_id: str, body: dict[str, Any], google_id: str) -> dict[str, Any]:
        return self._execute(
            self.service.events().insert(
                calendarId=calendar_id, body={**body, "id": google_id}, sendUpdates="none"
            )
        )

    def patch(
        self, calendar_id: str, google_id: str, body: dict[str, Any], etag: str
    ) -> dict[str, Any]:
        request = self.service.events().patch(
            calendarId=calendar_id, eventId=google_id, body=body, sendUpdates="none"
        )
        request.headers["If-Match"] = etag
        return self._execute(request)

    def delete(self, calendar_id: str, google_id: str, etag: str) -> None:
        request = self.service.events().delete(
            calendarId=calendar_id, eventId=google_id, sendUpdates="none"
        )
        request.headers["If-Match"] = etag
        self._execute(request)
