from datetime import datetime, timezone
from types import SimpleNamespace
from xml.sax.saxutils import escape

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
    with pytest.raises(SourceError, match="partial_caldav_multistatus"):
        _check_multistatus(response, report=False)


def test_cross_host_resource_rejected(monkeypatch):
    class Calendar:
        def __init__(self, client, url):
            pass

        def search(self, **kwargs):
            return [SimpleNamespace(url="https://other.example/a.ics", data="secret")]

    monkeypatch.setattr(module, "DAVCalendar", Calendar)
    result = fake_source().scan(CAL, "Asia/Shanghai", START, END, "full")
    assert not result.complete and result.errors == ["caldav_cross_host_url_rejected"]


def test_feishu_calendars_fallback_discovers_collection(monkeypatch):
    xml = b'''<d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/calendars/</d:href></d:response>
      <d:response><d:href>/calendars/abc-123/</d:href></d:response>
      <d:response><d:href>https://evil.example/calendars/abc/</d:href></d:response>
    </d:multistatus>'''

    class Calendar:
        def __init__(self, client, url):
            self.url = url

        def get_display_name(self):
            return "Work"

    class Client:
        def principal(self):
            raise ValueError("no discovery")

        def request(self, url, **kwargs):
            return SimpleNamespace(status=207, raw=xml)

    source = fake_source()
    source.client = Client()
    monkeypatch.setattr(module, "DAVCalendar", Calendar)
    with pytest.raises(SourceError, match="caldav_cross_host_url_rejected"):
        source.calendars()
    source.client.request = lambda url, **kwargs: SimpleNamespace(
        status=207, raw=xml.replace(b'<d:response><d:href>https://evil.example/calendars/abc/</d:href></d:response>', b'')
    )
    assert source.calendars() == [("Work", CAL.replace("synthetic", "abc-123"))]


def test_timezone_property_used_before_prompt(monkeypatch):
    class Calendar:
        def __init__(self, client, url):
            pass

        def get_properties(self, properties):
            assert properties[0].__class__.__name__ == "CalendarTimeZone"
            return {"tz": "BEGIN:VTIMEZONE\nTZID:Asia/Shanghai\nEND:VTIMEZONE"}

    monkeypatch.setattr(module, "DAVCalendar", Calendar)
    assert fake_source().timezone(CAL) == "Asia/Shanghai"


def test_multiget_fallback_reads_every_listed_resource(monkeypatch):
    first = ics("BEGIN:VEVENT\nUID:one\nDTSTART:20260928T090000Z\nDTEND:20260928T100000Z\nEND:VEVENT\n")
    second = ics("BEGIN:VEVENT\nUID:two\nDTSTART:20260929T090000Z\nDTEND:20260929T100000Z\nEND:VEVENT\n")
    listing = b'''<d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/calendars/synthetic/one.ics</d:href><d:propstat><d:prop><d:getetag/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/calendars/synthetic/two.ics</d:href><d:propstat><d:prop><d:getetag/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    </d:multistatus>'''
    report = (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + ''.join(
            '<d:response><d:href>/calendars/synthetic/' + name + '.ics</d:href>'
            '<d:propstat><d:prop><c:calendar-data>' + escape(data) + '</c:calendar-data></d:prop>'
            '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
            for name, data in [('one', first), ('two', second)]
        ) + '</d:multistatus>'
    ).encode()

    class Client:
        def request(self, _url, *, method, **_kwargs):
            return SimpleNamespace(status=207, raw=listing if method == "PROPFIND" else report)

    class Calendar:
        def __init__(self, client, url):
            pass

        def search(self, **_kwargs):
            raise SourceError("calendar_query_unavailable")

    source = fake_source()
    source.client = Client()
    monkeypatch.setattr(module, "DAVCalendar", Calendar)
    snapshot = source.scan(CAL, "Asia/Shanghai", START, END, "busy")
    assert snapshot.complete and len(snapshot.instances) == 2


def test_multiget_missing_resource_is_incomplete():
    listing = b'''<d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/calendars/synthetic/one.ics</d:href><d:status>HTTP/1.1 200 OK</d:status></d:response>
    </d:multistatus>'''
    report = b'<d:multistatus xmlns:d="DAV:"/>'

    class Client:
        def request(self, _url, *, method, **_kwargs):
            return SimpleNamespace(status=207, raw=listing if method == "PROPFIND" else report)

    source = fake_source()
    source.client = Client()
    with pytest.raises(SourceError, match="partial_caldav_multistatus"):
        source._multiget_resources(CAL)
