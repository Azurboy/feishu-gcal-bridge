from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from fgbridge.events import Occurrence, Snapshot, opaque_key
from fgbridge.google_calendar import GoogleError, RemoteEvent
from fgbridge.storage import State
from fgbridge.sync import Reconciler, window_bounds


class FakeGoogle:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.tombstones: set[str] = set()
        self.writes = 0
        self.fail_next_readback = False
        self.fail_after_insert = False
        self.tombstone_get_missing = False
        self.patch_conflict_once = False
        self.delete_conflict_once = False

    def assert_calendar(self, calendar_id, install_id):
        assert calendar_id == "owned" and install_id == "install"

    def list_managed(self, calendar_id, install_id):
        return {
            item["extendedProperties"]["private"]["fgbridge_key"]: RemoteEvent(key, deepcopy(item))
            for key, item in self.items.items()
            if item.get("extendedProperties", {}).get("private", {}).get("fgbridge_install") == install_id
        }

    def get(self, calendar_id, google_id):
        if self.fail_next_readback:
            self.fail_next_readback = False
            raise GoogleError("network_after_write")
        if google_id in self.items:
            return "exists", deepcopy(self.items[google_id])
        return (
            "tombstone" if google_id in self.tombstones and not self.tombstone_get_missing
            else "missing"
        ), None

    def tombstone_exists(self, calendar_id, google_id):
        return google_id in self.tombstones

    def insert(self, calendar_id, body, google_id):
        self.writes += 1
        if google_id in self.items or google_id in self.tombstones:
            raise GoogleError("google_http_409", 409)
        self.items[google_id] = {**deepcopy(body), "id": google_id, "etag": "1"}
        if self.fail_after_insert:
            self.fail_after_insert = False
            self.fail_next_readback = True

    def patch(self, calendar_id, google_id, body, etag):
        self.writes += 1
        item = self.items[google_id]
        if self.patch_conflict_once:
            self.patch_conflict_once = False
            item["etag"] = str(int(item["etag"]) + 1)
            raise GoogleError("google_http_412", 412)
        if item["etag"] != etag:
            raise GoogleError("google_http_412", 412)
        self.items[google_id] = {**item, **deepcopy(body), "etag": str(int(etag) + 1)}

    def delete(self, calendar_id, google_id, etag):
        self.writes += 1
        if google_id not in self.items:
            raise GoogleError("google_http_404", 404)
        if self.delete_conflict_once:
            self.delete_conflict_once = False
            self.items[google_id]["etag"] = str(int(self.items[google_id]["etag"]) + 1)
            raise GoogleError("google_http_412", 412)
        if self.items[google_id]["etag"] != etag:
            raise GoogleError("google_http_412", 412)
        del self.items[google_id]
        self.tombstones.add(google_id)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("FGBRIDGE_HOME", str(tmp_path / "state"))
    state = State()
    google = FakeGoogle()
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    start, end = window_bounds(now, "Asia/Shanghai")
    reconciler = Reconciler(google, state, "owned", "install", "full", start, end)
    yield state, google, reconciler, now
    state.close()


def event(name: str, now: datetime, key_suffix="a", mode="full") -> Occurrence:
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    return Occurrence(
        key=opaque_key("source", key_suffix), series_key=opaque_key("series", key_suffix),
        summary=name if mode == "full" else "Busy",
        start={"dateTime": start.isoformat(), "timeZone": "Asia/Shanghai"},
        end={"dateTime": end.isoformat(), "timeZone": "Asia/Shanghai"},
        transparency="opaque", logical_start=start, logical_end=end, private=False,
    )


def snapshot(*items: Occurrence, complete=True, errors=None) -> Snapshot:
    return Snapshot(instances={item.key: item for item in items}, complete=complete, errors=errors or [])


def test_idempotent_updates_and_two_scan_delete(harness):
    state, google, sync, now = harness
    a, b = event("A", now), event("B", now, "b")
    assert sync.run(snapshot(a, b)).created == 2
    first_ids = set(google.items)
    for _ in range(10):
        assert sync.run(snapshot(a, b)).status == "ok"
    assert google.writes == 2 and set(google.items) == first_ids
    moved = event("A renamed", now + timedelta(hours=2))
    assert moved.key == a.key
    assert sync.run(snapshot(moved, b)).updated == 1
    assert set(google.items) == first_ids
    assert sync.run(snapshot(moved)).pending_delete == 1
    assert sync.run(snapshot(moved)).deleted == 1
    assert len(google.items) == 1


def test_partial_and_empty_snapshots_never_mass_delete(harness):
    state, google, sync, now = harness
    a, b = event("A", now), event("B", now, "b")
    sync.run(snapshot(a, b))
    for _ in range(3):
        result = sync.run(snapshot(a, complete=False, errors=["partial_caldav_multistatus"]))
        assert result.status == "partial"
    assert len(google.items) == 2
    empty = sync.run(snapshot())
    assert empty.empty_confirmation_needed and empty.deleted == 0
    assert len(google.items) == 2
    confirmed = sync.run(snapshot(), confirm_empty=True)
    assert confirmed.deleted == 2 and google.items == {}


def test_crash_after_remote_insert_and_db_loss_recover(harness, monkeypatch, tmp_path):
    state, google, sync, now = harness
    a = event("A", now)
    google.fail_after_insert = True
    assert sync.run(snapshot(a)).status == "partial"
    assert len(google.items) == 1
    assert sync.run(snapshot(a)).status == "ok"
    assert google.writes == 1
    state.close()
    (tmp_path / "state" / "state.sqlite3").unlink()
    new_state = State()
    try:
        recovered = Reconciler(google, new_state, "owned", "install", "full", sync.start, sync.end)
        assert recovered.run(snapshot(a)).status == "ok"
        assert len(new_state.mappings()) == 1
        assert google.writes == 1
    finally:
        new_state.close()


def test_manual_delete_recreates_with_new_generation(harness):
    state, google, sync, now = harness
    a = event("A", now)
    sync.run(snapshot(a))
    old_id = next(iter(google.items))
    del google.items[old_id]
    google.tombstones.add(old_id)
    assert sync.run(snapshot(a)).created == 1
    assert old_id not in google.items
    assert len(google.items) == 1
    assert state.mappings()[a.key].generation == 1


def test_hidden_tombstone_409_recreates_after_confirmation(harness):
    state, google, sync, now = harness
    a = event("A", now)
    sync.run(snapshot(a))
    old_id = next(iter(google.items))
    del google.items[old_id]
    google.tombstones.add(old_id)
    google.tombstone_get_missing = True
    assert sync.run(snapshot(a)).created == 1
    assert state.mappings()[a.key].generation == 1


def test_private_downgrade_scrubs_historical_copies(harness):
    state, google, sync, now = harness
    old = event("Private history", now - timedelta(days=20))
    sync.run(snapshot(old))
    assert next(iter(google.items.values()))["summary"] == "Private history"
    busy_sync = Reconciler(google, state, "owned", "install", "busy", sync.start, sync.end)
    result = busy_sync.run(snapshot())
    assert result.status == "ok"
    assert next(iter(google.items.values()))["summary"] == "Busy"
    assert state.get("last_mode") == "busy"


def test_attendees_prevent_mutation_and_dry_run_does_not_advance(harness):
    state, google, sync, now = harness
    a, b = event("A", now), event("B", now, "b")
    sync.run(snapshot(a, b))
    old_writes = google.writes
    first = sync.run(snapshot(a), dry_run=True)
    assert first.pending_delete == 1
    assert state.mappings()[b.key].misses == 0
    assert google.writes == old_writes
    remote_b = next(item for item in google.items.values() if item["extendedProperties"]["private"]["fgbridge_key"] == b.key)
    remote_b["attendees"] = [{"email": "external@example.test"}]
    sync.run(snapshot(a))
    result = sync.run(snapshot(a))
    assert result.status == "partial"
    assert remote_b["id"] in google.items


def test_forged_marker_with_wrong_event_id_is_not_managed(harness):
    state, google, sync, now = harness
    a = event("A", now)
    forged = a.google_body("install", 0)
    forged["id"] = "manually-created-event"
    forged["etag"] = "1"
    google.items[forged["id"]] = forged
    with pytest.raises(ValueError, match="mirror_identity_invalid"):
        sync.run(snapshot(a))
    assert google.writes == 0


def test_etag_conflicts_refetch_and_retry_once(harness):
    state, google, sync, now = harness
    a, b = event("A", now), event("B", now, "b")
    sync.run(snapshot(a, b))
    google.patch_conflict_once = True
    renamed = event("A renamed", now)
    assert sync.run(snapshot(renamed, b)).updated == 1
    assert all(item["summary"] != "A" for item in google.items.values())
    google.delete_conflict_once = True
    assert sync.run(snapshot(renamed)).pending_delete == 1
    assert sync.run(snapshot(renamed)).deleted == 1
    assert len(google.items) == 1
