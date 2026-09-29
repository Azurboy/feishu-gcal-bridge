from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import fgbridge.source as module
from fgbridge.source import FeishuSource, SourceError, _check_multistatus


CAL = "https://caldav.feishu.cn/calendars/synthetic/"
START = datetime(2026, 9, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 10, tzinfo=timezone.utc)


def ics(vevent: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + vevent + "END:VCALENDAR\n"


def fake_source(username="me@example.test"):
    obj = FeishuSource.__new__(FeishuSource)
    obj.base_url = "https://caldav.feishu.cn/"
    obj.username = username
    obj.client = object()
    return obj


def test_split_detached_resource_grouped_before_expansion(monkeypatch):
    master = ics(
        """BEGIN:VEVENT
UID:one
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=DAILY;COUNT=2
SUMMARY:Original
END:VEVENT
"""
    )
    detached = ics(
        """BEGIN:VEVENT
UID:one
RECURRENCE-ID:20260929T090000Z
DTSTART:20260929T110000Z
DTEND:20260929T120000Z
SUMMARY:Moved
END:VEVENT
"""
    )

    class Calendar:
        def __init__(self, client, url):
            pass

        def search(self, **kwargs):
            return [SimpleNamespace(url=CAL + "a.ics", data=master),
                    SimpleNamespace(url=CAL + "b.ics", data=detached)]

    monkeypatch.setattr(module, "DAVCalendar", Calendar)
    result = fake_source().scan(CAL, "Asia/Shanghai", START, END, "full")
    assert result.complete and len(result.instances) == 2
    assert {item.summary for item in result.instances.values()} == {"Original", "Moved"}


def test_partial_report_status_rejected():
    response = b'''<d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/a.ics</d:href><d:status>HTTP/1.1 200 OK</d:status></d:response>
      <d:response><d:href>/b.ics</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>
    </d:multistatus>'''
    with pytest.raises(SourceError, match="partial_caldav_multistatus"):
        _check_multistatus(response, report=True)


def test_cross_host_resource_rejected(monkeypatch):
    class Calendar:
        def __init__(self, client, url):
            pass

        def search(self, **kwargs):
            return [SimpleNamespace(url="https://other.example/a.ics", data="secret")]

    monkeypatch.setattr(module, "DAVCalendar", Calendar)
    result = fake_source().scan(CAL, "Asia/Shanghai", START, END, "full")
    assert not result.complete and result.errors == ["caldav_cross_host_url_rejected"]
