"""Turn complete CalDAV resources into stable, privacy-aware instances."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from icalendar import Calendar
from recurring_ical_events import of as recurring_events


class EventError(ValueError):
    pass


def opaque_key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Occurrence:
    key: str
    series_key: str
    summary: str
    start: dict[str, str]
    end: dict[str, str]
    transparency: str
    logical_start: datetime
    logical_end: datetime
    private: bool

    def google_body(self, install_id: str, generation: int) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "start": self.start,
            "end": self.end,
            "transparency": self.transparency,
            "visibility": "private",
            "reminders": {"useDefault": False, "overrides": []},
            "extendedProperties": {
                "private": {
                    "fgbridge_install": install_id,
                    "fgbridge_key": self.key,
                    "fgbridge_series": self.series_key,
                    "fgbridge_generation": str(generation),
                    "fgbridge_v": "1",
                    "fgbridge_start": self.logical_start.isoformat(),
                    "fgbridge_end": self.logical_end.isoformat(),
                }
            },
        }


@dataclass
class Snapshot:
    instances: dict[str, Occurrence] = field(default_factory=dict)
    cancelled_keys: set[str] = field(default_factory=set)
    cancelled_series: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    complete: bool = True
    private_marker_seen: bool = False

    def absorb(self, other: Snapshot) -> None:
        for key, event in other.instances.items():
            if key in self.instances and self.instances[key] != event:
                self.errors.append("conflicting_duplicate_instance")
                self.complete = False
            else:
                self.instances[key] = event
        self.cancelled_keys |= other.cancelled_keys
        self.cancelled_series |= other.cancelled_series
        self.errors.extend(other.errors)
        self.complete &= other.complete
        self.private_marker_seen |= other.private_marker_seen


def _decoded(component: Any, name: str) -> date | datetime:
    try:
        return component.decoded(name)
    except (KeyError, ValueError, TypeError) as exc:
        raise EventError(f"invalid_{name.lower()}") from exc


def _aware(value: date | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=zone) if value.tzinfo is None else value
    return datetime(value.year, value.month, value.day, tzinfo=zone)


def _identity(value: date | datetime, zone: ZoneInfo) -> str:
    if isinstance(value, datetime):
        return _aware(value, zone).astimezone(timezone.utc).isoformat()
    return value.isoformat()


def _time_fields(value: date | datetime, zone: ZoneInfo) -> dict[str, str]:
    if not isinstance(value, datetime):
        return {"date": value.isoformat()}
    resolved = _aware(value, zone).astimezone(zone)
    return {"dateTime": resolved.isoformat(), "timeZone": zone.key}


def _end(component: Any, start: date | datetime) -> date | datetime:
    if "DTEND" in component:
        return _decoded(component, "DTEND")
    if "DURATION" in component:
        duration = _decoded(component, "DURATION")
        if not isinstance(duration, timedelta):
            raise EventError("invalid_duration")
        return start + duration
    raise EventError("missing_end")


def parse_resource(
    raw: str | bytes,
    calendar_id: str,
    zone_name: str,
    window_start: datetime,
    window_end: datetime,
    mode: str,
) -> Snapshot:
    result = Snapshot()
    try:
        zone = ZoneInfo(zone_name)
        calendar = Calendar.from_ical(raw)
        components = list(calendar.walk("VEVENT"))
        if not components:
            raise EventError("missing_vevent")
        for component in components:
            uid = str(component.get("UID", ""))
            if not uid:
                raise EventError("missing_uid")
            series_key = opaque_key(calendar_id, uid)
            recurrence_id = component.get("RECURRENCE-ID")
            if recurrence_id and str(recurrence_id.params.get("RANGE", "")).upper() == "THISANDFUTURE":
                raise EventError("unsupported_thisandfuture")
            status = str(component.get("STATUS", "")).upper()
            if str(component.get("CLASS", "")).upper() in {"PRIVATE", "CONFIDENTIAL"}:
                result.private_marker_seen = True
            if status == "CANCELLED":
                if recurrence_id:
                    rid = _identity(_decoded(component, "RECURRENCE-ID"), zone)
                    result.cancelled_keys.add(opaque_key(calendar_id, uid, rid))
                else:
                    result.cancelled_series.add(series_key)
        # The library retains original RECURRENCE-ID on moved instances.
        for component in recurring_events(calendar, keep_recurrence_attributes=True).between(
            window_start, window_end
        ):
            if str(component.get("STATUS", "")).upper() == "CANCELLED":
                continue
            uid = str(component.get("UID", ""))
            if not uid:
                raise EventError("missing_uid")
            start = _decoded(component, "DTSTART")
            end = _end(component, start)
            start_aware, end_aware = _aware(start, zone), _aware(end, zone)
            if end_aware <= start_aware:
                raise EventError("invalid_end")
            if end_aware <= window_start or start_aware >= window_end:
                continue
            rid_component = component.get("RECURRENCE-ID")
            has_recurrence = "RRULE" in component or "RDATE" in component or "EXDATE" in component
            if rid_component is not None:
                rid = _identity(_decoded(component, "RECURRENCE-ID"), zone)
            elif has_recurrence:
                # Defensive: a library version that loses generated IDs must fail closed.
                raise EventError("missing_recurrence_identity")
            else:
                rid = "single"
            series_key = opaque_key(calendar_id, uid)
            key = opaque_key(calendar_id, uid, rid)
            private = str(component.get("CLASS", "")).upper() in {"PRIVATE", "CONFIDENTIAL"}
            summary = "Busy" if mode == "busy" or private else str(component.get("SUMMARY") or "Busy")
            occurrence = Occurrence(
                key=key,
                series_key=series_key,
                summary=summary,
                start=_time_fields(start, zone),
                end=_time_fields(end, zone),
                transparency="transparent" if str(component.get("TRANSP", "")).upper() == "TRANSPARENT" else "opaque",
                logical_start=start_aware,
                logical_end=end_aware,
                private=private,
            )
            if key in result.instances and result.instances[key] != occurrence:
                raise EventError("conflicting_duplicate_instance")
            result.instances[key] = occurrence
    except Exception as exc:
        # One bad resource must never become evidence that old instances vanished.
        result.complete = False
        result.errors.append(exc.args[0] if isinstance(exc, EventError) else "calendar_parse_failed")
        result.instances.clear()
        result.cancelled_keys.clear()
        result.cancelled_series.clear()
    return result
