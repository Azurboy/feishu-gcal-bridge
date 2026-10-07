from pathlib import Path

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

import fgbridge.google_calendar as module
from fgbridge.google_calendar import GoogleCalendar, GoogleError, event_id
from fgbridge.storage import atomic_private_write


class Request:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def execute(self, num_retries=0):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def http_error(status, reason=""):
    content = ('{"error":{"errors":[{"reason":"' + reason + '"}]}}').encode()
    return HttpError(Response({"status": status}), content)


def gateway():
    obj = GoogleCalendar.__new__(GoogleCalendar)
    obj.credentials = None
    obj.token_path = Path("/unused")
    return obj


def test_retry_limited_for_rate_error(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    request = Request([http_error(429), {"ok": True}])
    assert gateway()._execute(request) == {"ok": True}
    assert request.calls == 2


def test_permission_error_does_not_retry(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    request = Request([http_error(403, "insufficientPermissions")])
    with pytest.raises(GoogleError, match="google_forbidden"):
        gateway()._execute(request)
    assert request.calls == 1


def test_rate_limit_403_retries_but_etag_conflict_does_not(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    request = Request([http_error(403, "rateLimitExceeded"), {"ok": True}])
    assert gateway()._execute(request) == {"ok": True}
    request = Request([http_error(412)])
    with pytest.raises(GoogleError) as captured:
        gateway()._execute(request)
    assert captured.value.status == 412 and request.calls == 1


def test_event_id_is_stable_and_google_compatible():
    first = event_id("install", "source", 0)
    assert first == event_id("install", "source", 0)
    assert first != event_id("install", "source", 1)
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_reauthorize_checks_existing_calendar_before_replacing_token(monkeypatch, tmp_path):
    monkeypatch.setenv("FGBRIDGE_HOME", str(tmp_path))
    token_path = tmp_path / "token.json"
    atomic_private_write(token_path, b"old-token")

    class Credentials:
        refresh_token = "new-refresh-token"

        def has_scopes(self, scopes):
            return scopes == [module.SCOPE]

        def to_json(self):
            return "new-token"

    class Flow:
        def run_local_server(self, **kwargs):
            assert kwargs["prompt"] == "consent"
            return Credentials()

    monkeypatch.setattr(
        module.InstalledAppFlow, "from_client_secrets_file",
        lambda *args, **kwargs: Flow(),
    )
    monkeypatch.setattr(module, "build", lambda *args, **kwargs: object())

    def wrong_account(self, *_args):
        assert self.persist_token is False
        raise GoogleError("target_calendar_missing")

    monkeypatch.setattr(GoogleCalendar, "assert_calendar", wrong_account)
    with pytest.raises(GoogleError, match="target_calendar_missing"):
        GoogleCalendar.reauthorize(tmp_path / "client.json", "existing-target", "install")
    assert token_path.read_bytes() == b"old-token"

    def expected_binding(self, calendar_id, install_id):
        assert self.persist_token is False
        assert (calendar_id, install_id) == ("existing-target", "install")

    monkeypatch.setattr(GoogleCalendar, "assert_calendar", expected_binding)
    GoogleCalendar.reauthorize(tmp_path / "client.json", "existing-target", "install")
    assert token_path.read_bytes() == b"new-token"


def test_calendar_validation_refresh_does_not_replace_saved_token(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    atomic_private_write(token_path, b"old-token")

    class Credentials:
        token = "before"

        def to_json(self):
            return "new-token"

    class Session:
        def __init__(self, credentials):
            self.credentials = credentials

        def request(self, *_args, **_kwargs):
            self.credentials.token = "after"
            return type("Result", (), {
                "headers": {}, "status_code": 200, "reason": "OK", "content": b"{}",
            })()

    monkeypatch.setattr(module, "AuthorizedSession", Session)
    transport = module._RequestsTransport(Credentials(), token_path, persist_token=False)
    transport.request("https://www.googleapis.com/calendar/v3/calendars/test")
    assert token_path.read_bytes() == b"old-token"
