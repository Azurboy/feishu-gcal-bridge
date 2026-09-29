from datetime import datetime, timezone

from fgbridge.events import opaque_key, parse_resource


START = datetime(2026, 9, 25, tzinfo=timezone.utc)
END = datetime(2026, 10, 10, tzinfo=timezone.utc)
CAL = "https://caldav.feishu.cn/calendars/synthetic/"


def parse(body: str, mode: str = "full"):
    return parse_resource(
        "BEGIN:VCALENDAR\nVERSION:2.0\n" + body + "END:VCALENDAR\n",
        CAL, "Asia/Shanghai", START, END, mode,
    )


def test_recurrence_move_keeps_original_identity_and_cancel() -> None:
    before = parse(
        """BEGIN:VEVENT
UID:series-1
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Planning
END:VEVENT
"""
    )
    after = parse(
        """BEGIN:VEVENT
UID:series-1
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Planning
END:VEVENT
BEGIN:VEVENT
UID:series-1
RECURRENCE-ID:20260929T090000Z
DTSTART:20260929T110000Z
DTEND:20260929T120000Z
SUMMARY:Moved
END:VEVENT
BEGIN:VEVENT
UID:series-1
RECURRENCE-ID:20260930T090000Z
DTSTART:20260930T090000Z
DTEND:20260930T100000Z
STATUS:CANCELLED
END:VEVENT
"""
    )
    assert before.complete and after.complete
    assert len(before.instances) == 3
    assert len(after.instances) == 2
    assert len(after.cancelled_keys) == 1
    assert set(after.instances) <= set(before.instances)
    assert next(x for x in after.instances.values() if x.summary == "Moved").start["dateTime"].endswith("19:00:00+08:00")


def test_plain_event_reschedule_keeps_uid_identity() -> None:
    def body(start: str, end: str) -> str:
        return f"""BEGIN:VEVENT
UID:plain-1
DTSTART;TZID=Asia/Shanghai:{start}
DTEND;TZID=Asia/Shanghai:{end}
END:VEVENT
"""

    before = parse(body("20260928T030000", "20260928T031500"))
    after = parse(body("20260928T033000", "20260928T034500"))
    assert before.complete and after.complete
    assert set(before.instances) == set(after.instances) == {opaque_key(CAL, "plain-1", "single")}
    assert next(iter(after.instances.values())).start["dateTime"].endswith("03:30:00+08:00")


def test_all_day_exclusive_end_private_and_busy() -> None:
    result = parse(
        """BEGIN:VEVENT
UID:private-1
DTSTART;VALUE=DATE:20260928
DTEND;VALUE=DATE:20260930
CLASS:PRIVATE
SUMMARY:Hidden name
TRANSP:TRANSPARENT
END:VEVENT
"""
    )
    assert result.complete and result.private_marker_seen
    item = next(iter(result.instances.values()))
    assert item.start == {"date": "2026-09-28"}
    assert item.end == {"date": "2026-09-30"}
    assert item.summary == "Busy"
    assert item.transparency == "transparent"
    assert item.google_body("install", 0)["reminders"]["useDefault"] is False


def test_this_and_future_keeps_each_original_occurrence_identity() -> None:
    before = parse(
        """BEGIN:VEVENT
UID:series-1
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=DAILY;COUNT=4
END:VEVENT
"""
    )
    result = parse(
        """BEGIN:VEVENT
UID:series-1
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=DAILY;COUNT=4
END:VEVENT
BEGIN:VEVENT
UID:series-1
RECURRENCE-ID;RANGE=THISANDFUTURE:20260929T090000Z
DTSTART:20260929T110000Z
DTEND:20260929T120000Z
END:VEVENT
"""
    )
    assert result.complete and len(result.instances) == 4
    assert set(result.instances) == set(before.instances)
    starts = [x.start["dateTime"] for x in result.instances.values()]
    assert starts.count("2026-09-29T19:00:00+08:00") == 1
    assert starts.count("2026-09-30T19:00:00+08:00") == 1


def test_weekly_recurrence_and_event_overlapping_window_start() -> None:
    weekly = parse(
        """BEGIN:VEVENT
UID:weekly
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4
END:VEVENT
"""
    )
    assert weekly.complete and len(weekly.instances) == 4
    overlapping = parse_resource(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:cross-midnight
DTSTART:20260924T230000Z
DTEND:20260925T020000Z
END:VEVENT
END:VCALENDAR
""",
        CAL, "Asia/Shanghai", START, END, "full",
    )
    assert overlapping.complete and len(overlapping.instances) == 1
    item = next(iter(overlapping.instances.values()))
    assert item.start["dateTime"] == "2026-09-25T07:00:00+08:00"
    assert item.end["dateTime"] == "2026-09-25T10:00:00+08:00"


def test_invalid_end_does_not_invent_duration() -> None:
    result = parse("BEGIN:VEVENT\nUID:x\nDTSTART:20260928T090000Z\nEND:VEVENT\n")
    assert not result.complete and result.errors == ["invalid_end"]


def test_exdate_rdate_and_declined_self_invitation() -> None:
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-2
DTSTART:20260928T090000Z
DTEND:20260928T100000Z
RRULE:FREQ=DAILY;COUNT=3
EXDATE:20260929T090000Z
RDATE:20261001T090000Z
ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.test
END:VEVENT
END:VCALENDAR
"""
    declined = parse_resource(raw, CAL, "Asia/Shanghai", START, END, "full", "me@example.test")
    assert declined.complete and not declined.instances
    accepted = parse_resource(raw, CAL, "Asia/Shanghai", START, END, "full", "other@example.test")
    assert accepted.complete and len(accepted.instances) == 3


def test_dst_and_floating_gap_fail_closed() -> None:
    zone_start = datetime(2026, 3, 7, tzinfo=timezone.utc)
    zone_end = datetime(2026, 3, 12, tzinfo=timezone.utc)
    recurring = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:dst-1
DTSTART;TZID=America/New_York:20260307T090000
DTEND;TZID=America/New_York:20260307T100000
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
END:VCALENDAR
"""
    result = parse_resource(recurring, CAL, "America/New_York", zone_start, zone_end, "full")
    assert result.complete and len(result.instances) == 3
    starts = [x.start["dateTime"] for x in result.instances.values()]
    assert any(x.endswith("09:00:00-05:00") for x in starts)
    assert any(x.endswith("09:00:00-04:00") for x in starts)
    gap = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:gap
DTSTART:20260308T023000
DTEND:20260308T033000
END:VEVENT
END:VCALENDAR
"""
    invalid = parse_resource(gap, CAL, "America/New_York", zone_start, zone_end, "full")
    assert not invalid.complete and invalid.errors == ["nonexistent_floating_time"]
