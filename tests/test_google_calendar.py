from pathlib import Path

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

import fgbridge.google_calendar as module
from fgbridge.google_calendar import GoogleCalendar, GoogleError, event_id


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
