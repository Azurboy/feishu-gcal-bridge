"""Reconcile a complete or partial source snapshot with one owned Google calendar."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .events import Occurrence, Snapshot
from .google_calendar import GoogleCalendar, GoogleError, RemoteEvent, event_id
from .storage import Mapping, State


@dataclass
class Result:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    pending_delete: int = 0
    errors: list[str] = field(default_factory=list)
    empty_confirmation_needed: bool = False
    dry_run: bool = False

    @property
    def status(self) -> str:
        return "partial" if self.errors else "ok"


def window_bounds(now: datetime, zone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(zone_name)
    today = now.astimezone(zone).date()
    return (
        datetime.combine(today - timedelta(days=7), time.min, zone),
        datetime.combine(today + timedelta(days=181), time.min, zone),
    )


def _time_key(field: dict[str, str]) -> str:
    if "date" in field:
        return "date:" + field["date"]
    value = datetime.fromisoformat(field["dateTime"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("unqualified_google_time")
    return "instant:" + value.astimezone(timezone.utc).isoformat()


def _projection(body: dict[str, Any]) -> tuple[Any, ...]:
    reminders = body.get("reminders") or {}
    return (
        body.get("summary", ""),
        _time_key(body["start"]),
        _time_key(body["end"]),
        body.get("transparency", "opaque"),
        body.get("visibility", "default"),
        bool(reminders.get("useDefault", True)),
        tuple(json.dumps(x, sort_keys=True) for x in reminders.get("overrides", [])),
    )


def _unsafe_remote(item: dict[str, Any]) -> bool:
    return bool(
        item.get("attendees")
        or item.get("conferenceData")
        or item.get("hangoutLink")
        or item.get("attachments")
        or item.get("recurrence")
    )


def _owned(item: dict[str, Any], install_id: str, key: str) -> bool:
    props = item.get("extendedProperties", {}).get("private", {})
    return props.get("fgbridge_install") == install_id and props.get("fgbridge_key") == key


def _remote_mapping(key: str, remote: RemoteEvent) -> Mapping:
    props = remote.data.get("extendedProperties", {}).get("private", {})
    required = ("fgbridge_series", "fgbridge_generation", "fgbridge_start", "fgbridge_end")
    if any(not props.get(name) for name in required):
        raise ValueError("mirror_mapping_incomplete")
    try:
        generation = int(props["fgbridge_generation"])
        datetime.fromisoformat(props["fgbridge_start"])
        datetime.fromisoformat(props["fgbridge_end"])
    except (TypeError, ValueError) as exc:
        raise ValueError("mirror_mapping_invalid") from exc
    if generation < 0 or remote.id != event_id(props["fgbridge_install"], key, generation):
        raise ValueError("mirror_identity_invalid")
    return Mapping(
        key=key, series_key=props["fgbridge_series"], google_id=remote.id,
        generation=generation, last_start=props["fgbridge_start"],
        last_end=props["fgbridge_end"], misses=0,
    )


def _in_current_window(mapping: Mapping, start: datetime, end: datetime) -> bool:
    return datetime.fromisoformat(mapping.last_end) > start and datetime.fromisoformat(mapping.last_start) < end


class Reconciler:
    def __init__(
        self,
        google: GoogleCalendar,
        state: State,
        calendar_id: str,
        install_id: str,
        mode: str,
        start: datetime,
        end: datetime,
    ) -> None:
        self.google = google
        self.state = state
        self.calendar_id = calendar_id
        self.install_id = install_id
        self.mode = mode
        self.start = start
        self.end = end

    def _delete_remote(self, key: str, remote: RemoteEvent | None, mapping: Mapping) -> None:
        if remote is None:
            status, fetched = self.google.get(self.calendar_id, mapping.google_id)
            if status != "exists" or fetched is None:
                self.state.delete_mapping(key)
                return
            if not _owned(fetched, self.install_id, key):
                raise GoogleError("mirror_identity_lost")
            remote = RemoteEvent(mapping.google_id, fetched)
        if _unsafe_remote(remote.data):
            raise GoogleError("mirror_has_external_participants")
        self.state.intent(key, "delete", remote.id, mapping.generation)
        latest = remote.data
        for attempt in range(2):
            try:
                self.google.delete(self.calendar_id, remote.id, latest["etag"])
                break
            except GoogleError as exc:
                if exc.status in {404, 410}:
                    break
                if exc.status != 412 or attempt:
                    raise
                status, refreshed = self.google.get(self.calendar_id, remote.id)
                if status != "exists" or refreshed is None:
                    break
                if not _owned(refreshed, self.install_id, key) or _unsafe_remote(refreshed):
                    raise GoogleError("mirror_changed_during_delete")
                latest = refreshed
        self.state.delete_mapping(key)

    def _update_remote(self, event: Occurrence, remote: RemoteEvent, mapping: Mapping) -> bool:
        if _unsafe_remote(remote.data):
            raise GoogleError("mirror_has_external_participants")
        body = event.google_body(self.install_id, mapping.generation)
        current_props = remote.data.get("extendedProperties", {}).get("private", {})
        expected_props = body["extendedProperties"]["private"]
        changed = _projection(remote.data) != _projection(body) or any(
            current_props.get(k) != v for k, v in expected_props.items()
        )
        if not changed:
            return False
        self.state.intent(event.key, "update", remote.id, mapping.generation)
        latest = remote.data
        for attempt in range(2):
            try:
                self.google.patch(self.calendar_id, remote.id, body, latest["etag"])
                status, readback = self.google.get(self.calendar_id, remote.id)
                if status != "exists" or readback is None or _projection(readback) != _projection(body):
                    raise GoogleError("google_update_readback_mismatch")
                return True
            except GoogleError as exc:
                if exc.status != 412 or attempt:
                    raise
                status, refreshed = self.google.get(self.calendar_id, remote.id)
                if status != "exists" or refreshed is None:
                    raise GoogleError("mirror_disappeared_during_update") from exc
                if not _owned(refreshed, self.install_id, event.key) or _unsafe_remote(refreshed):
                    raise GoogleError("mirror_changed_during_update") from exc
                latest = refreshed
        raise GoogleError("google_update_conflict")

    def _insert_or_recover(self, event: Occurrence, mapping: Mapping | None) -> Mapping:
        generation = mapping.generation if mapping else 0
        for _ in range(4):
            google_id = event_id(self.install_id, event.key, generation)
            status, existing = self.google.get(self.calendar_id, google_id)
            if status == "tombstone":
                generation += 1
                self.state.intent(event.key, "insert", event_id(self.install_id, event.key, generation), generation)
                continue
            if status == "exists":
                if existing is None or not _owned(existing, self.install_id, event.key):
                    raise GoogleError("google_event_id_collision")
                recovered = Mapping(
                    event.key, event.series_key, google_id, generation,
                    event.logical_start.isoformat(), event.logical_end.isoformat(), 0,
                )
                self._update_remote(event, RemoteEvent(google_id, existing), recovered)
                return recovered
            body = event.google_body(self.install_id, generation)
            self.state.intent(event.key, "insert", google_id, generation)
            try:
                self.google.insert(self.calendar_id, body, google_id)
            except GoogleError as exc:
                if exc.status == 409:
                    continue
                raise
            read_status, readback = self.google.get(self.calendar_id, google_id)
            if read_status != "exists" or readback is None or not _owned(readback, self.install_id, event.key):
                raise GoogleError("google_insert_readback_mismatch")
            if _projection(readback) != _projection(body):
                raise GoogleError("google_insert_fields_mismatch")
            return Mapping(
                event.key, event.series_key, google_id, generation,
                event.logical_start.isoformat(), event.logical_end.isoformat(), 0,
            )
        raise GoogleError("google_event_id_unavailable")

    def run(self, snapshot: Snapshot, *, dry_run: bool = False, confirm_empty: bool = False) -> Result:
        result = Result(errors=list(snapshot.errors), dry_run=dry_run)
        self.google.assert_calendar(self.calendar_id, self.install_id)
        remote = self.google.list_managed(self.calendar_id, self.install_id)
        local = self.state.mappings()
        intents = self.state.intents()
        for key, item in remote.items():
            reconstructed = _remote_mapping(key, item)
            if key not in local or local[key].google_id != item.id:
                local[key] = reconstructed
                if not dry_run:
                    self.state.put_mapping(reconstructed)
            if key in intents and not dry_run:
                self.state.clear_intent(key)

        # Privacy downgrade covers historical copies outside the rolling source window.
        if self.mode == "busy" and self.state.get("last_mode") != "busy":
            for key, item in remote.items():
                if key in snapshot.instances:
                    continue
                if item.data.get("summary") == "Busy":
                    continue
                if dry_run:
                    result.updated += 1
                    continue
                if _unsafe_remote(item.data):
                    result.errors.append("mirror_has_external_participants")
                    continue
                try:
                    request_body = {"summary": "Busy"}
                    self.state.intent(key, "privacy", item.id, local[key].generation)
                    self.google.patch(self.calendar_id, item.id, request_body, item.data["etag"])
                    status, checked = self.google.get(self.calendar_id, item.id)
                    if status != "exists" or checked is None or checked.get("summary") != "Busy":
                        raise GoogleError("privacy_downgrade_readback_mismatch")
                    self.state.clear_intent(key)
                    remote[key] = RemoteEvent(item.id, checked)
                    result.updated += 1
                except GoogleError as exc:
                    result.errors.append(exc.code)
            if not dry_run and not result.errors:
                self.state.set("last_mode", "busy")

        source_keys = set(snapshot.instances)
        uncancelled_current = any(
            _in_current_window(mapping, self.start, self.end)
            and key not in snapshot.cancelled_keys
            and mapping.series_key not in snapshot.cancelled_series
            for key, mapping in local.items()
        )
        sudden_empty = snapshot.complete and not source_keys and uncancelled_current
        if sudden_empty and not confirm_empty:
            result.empty_confirmation_needed = True
            result.errors.append("source_suddenly_empty_confirm_required")

        for key, event in snapshot.instances.items():
            try:
                previous = local.get(key)
                on_google = remote.get(key)
                if on_google is None:
                    if dry_run:
                        result.created += 1
                        continue
                    new_mapping = self._insert_or_recover(event, previous)
                    self.state.put_mapping(new_mapping)
                    self.state.clear_intent(key)
                    local[key] = new_mapping
                    result.created += 1
                else:
                    prior = local[key]
                    body = event.google_body(self.install_id, prior.generation)
                    current_props = on_google.data.get("extendedProperties", {}).get("private", {})
                    will_update = _projection(on_google.data) != _projection(body) or any(
                        current_props.get(k) != v for k, v in body["extendedProperties"]["private"].items()
                    )
                    if will_update:
                        if dry_run or self._update_remote(event, on_google, prior):
                            result.updated += 1
                    if not dry_run:
                        self.state.put_mapping(Mapping(
                            key, event.series_key, prior.google_id, prior.generation,
                            event.logical_start.isoformat(), event.logical_end.isoformat(), 0,
                        ))
                        self.state.clear_intent(key)
            except (GoogleError, ValueError, KeyError) as exc:
                result.errors.append(exc.code if isinstance(exc, GoogleError) else "mirror_reconcile_failed")

        # No source failure, missing resource or unconfirmed empty snapshot can cause deletion.
        deletion_allowed = snapshot.complete and not result.empty_confirmation_needed and not result.errors
        if deletion_allowed:
            for key, mapping in list(local.items()):
                if key in source_keys:
                    continue
                if not _in_current_window(mapping, self.start, self.end):
                    continue
                explicit_cancel = key in snapshot.cancelled_keys or mapping.series_key in snapshot.cancelled_series
                misses = mapping.misses + 1
                if explicit_cancel or (confirm_empty and sudden_empty) or misses >= 2:
                    if dry_run:
                        result.deleted += 1
                    else:
                        try:
                            self._delete_remote(key, remote.get(key), mapping)
                            result.deleted += 1
                        except (GoogleError, KeyError) as exc:
                            result.errors.append(exc.code if isinstance(exc, GoogleError) else "mirror_delete_failed")
                else:
                    result.pending_delete += 1
                    if not dry_run:
                        self.state.put_mapping(Mapping(
                            key, mapping.series_key, mapping.google_id, mapping.generation,
                            mapping.last_start, mapping.last_end, misses,
                        ))
        if not dry_run:
            if result.errors:
                self.state.set("status", "partial")
                self.state.set("last_error", result.errors[0])
            else:
                self.state.set("status", "ok")
                self.state.set("last_error", "")
                self.state.set("last_success", datetime.now(timezone.utc).isoformat())
                self.state.set("last_mode", self.mode)
            self.state.set("last_created", str(result.created))
            self.state.set("last_updated", str(result.updated))
            self.state.set("last_deleted", str(result.deleted))
        return result
