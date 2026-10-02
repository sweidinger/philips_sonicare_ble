"""Catching up on stored sessions the handle ran while nobody was connected."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.philips_sonicare_ble.classic_protocol import (
    decode_session_record,
)

from .test_stored_session import _bare_coordinator

# Session 933 exactly as Stefan's HX991X answered it over the ESP bridge on
# 2026-10-01 - 13 bytes, which v0.28.0 refused as too short.
REAL_933 = bytes.fromhex("0017348201a503b400b4000302")
REAL_CLOCK = 25_349_078  # handle clock read on the same connect


class _Bus:
    def __init__(self):
        self.fired = []

    def async_fire(self, event, data):
        self.fired.append((event, data))


class _Store:
    def __init__(self, records, clock):
        self.records, self.clock, self.asked = records, clock, []

    async def fetch_stored_session(self, session_id=None):
        self.asked.append(session_id)
        rec = self.records.get(session_id)
        return dict(rec, handle_clock=self.clock) if rec else None


def _wired(c, records, clock=1_000_000, data=None):
    c.transport = SimpleNamespace(is_connected=True)
    c._link_lock = asyncio.Lock()
    c._protocol = _Store(records, clock)
    c.hass = SimpleNamespace(bus=_Bus())
    c.entry = SimpleNamespace(entry_id="e1")
    c._sync_failures = {}
    c._sync_target = None
    c._sync_task = None
    c.data = dict(data or {})

    def publish(d):
        c.data = d
    c.async_set_updated_data = publish
    return c


def _rec(sid, ago, clock=1_000_000, duration=120):
    return {"session_id": sid, "duration": duration, "routine_length": 120,
            "brushing_mode": "clean", "intensity": "high",
            "timestamp": clock - ago}


def _events(c):
    return [d for e, d in c.hass.bus.fired if e == "philips_sonicare_ble_session"]


def test_the_real_13_byte_record_decodes():
    rec = decode_session_record(REAL_933, "HX991X", chunked=True)
    assert rec["session_id"] == 933
    assert rec["duration"] == 180 and rec["routine_length"] == 180
    assert rec["brushing_mode"] == "deep_clean_plus"
    assert rec["intensity"] == "high"
    age = REAL_CLOCK - rec["timestamp"]
    assert 10 * 3600 < age < 11 * 3600   # began the morning of 1 Oct


def test_start_is_the_held_record_or_what_it_carries():
    c = _wired(_bare_coordinator(), {})
    c._schedule_sync = lambda: None
    new = {"last_session": {"session_id": None, "previous_id": 930}}
    c._note_sync_target(new, 933)
    assert new["synced_session_id"] == 930 and c._sync_target == 933


def test_a_first_connect_starts_at_the_newest():
    c = _wired(_bare_coordinator(), {})
    c._schedule_sync = lambda: None
    new = {}
    c._note_sync_target(new, 415)
    assert new["synced_session_id"] == 415


def test_noted_mid_session_and_picked_up_afterwards():
    """Switched on while waking it: the number arrives during a session."""
    c = _wired(_bare_coordinator(), {})
    calls = []
    c._schedule_sync = lambda: calls.append(1)
    c._note_sync_target({"brushing_state": "on", "synced_session_id": 930}, 933)
    assert calls == [] and c._sync_target == 933


async def test_catches_up_oldest_first_with_real_dates():
    day = 86400
    c = _wired(_bare_coordinator(), {
        931: _rec(931, 2 * day + 6 * 3600),
        932: _rec(932, day),
        933: _rec(933, 3600),
    }, data={"synced_session_id": 930})
    c._sync_target = 933
    now = datetime.now(timezone.utc)
    await c._run_sync()
    ev = _events(c)
    assert [e["session_id"] for e in ev] == [931, 932, 933]
    assert all(e["backfill"] and e["time_source"] == "handle_clock" for e in ev)
    started = datetime.fromisoformat(ev[1]["started_at"])
    assert abs((now - started) - timedelta(days=1)) < timedelta(minutes=1)
    assert c.data["synced_session_id"] == 933


async def test_the_real_record_goes_all_the_way_through():
    real = decode_session_record(REAL_933, "HX991X", chunked=True)
    c = _wired(_bare_coordinator(), {933: real}, clock=REAL_CLOCK,
               data={"synced_session_id": 932})
    c._sync_target = 933
    await c._run_sync()
    (ev,) = _events(c)
    assert ev["session_id"] == 933 and ev["duration_seconds"] == 180


async def test_a_dropped_link_keeps_progress():
    c = _wired(_bare_coordinator(), {931: _rec(931, 7200), 932: _rec(932, 3600)},
               data={"synced_session_id": 930})
    c._sync_target = 932
    orig = c._protocol.fetch_stored_session

    async def fetch(sid=None):
        rec = await orig(sid)
        if sid == 931:
            c.transport.is_connected = False
        return rec
    c._protocol.fetch_stored_session = fetch
    await c._run_sync()
    assert [e["session_id"] for e in _events(c)] == [931]
    assert c.data["synced_session_id"] == 931
    c.transport.is_connected = True
    await c._run_sync()
    assert [e["session_id"] for e in _events(c)] == [931, 932]


async def test_a_bad_record_is_retried_then_skipped():
    c = _wired(_bare_coordinator(), {932: _rec(932, 3600)},
               data={"synced_session_id": 930})   # 931 never answers
    c._sync_target = 932
    for _ in range(c.MAX_SYNC_ATTEMPTS - 1):
        await c._run_sync()
        assert _events(c) == [] and c.data["synced_session_id"] == 930
    await c._run_sync()
    assert [e["session_id"] for e in _events(c)] == [932]
    assert c.data["synced_session_id"] == 932


async def test_nothing_to_do_when_caught_up():
    c = _wired(_bare_coordinator(), {}, data={"synced_session_id": 933})
    c._sync_target = 933
    await c._run_sync()
    assert c._protocol.asked == []


async def test_a_long_absence_is_capped():
    c = _wired(_bare_coordinator(), {}, data={"synced_session_id": 10})
    c._sync_target = 200
    c.MAX_SYNC_ATTEMPTS = 1
    await c._run_sync()
    assert c._protocol.asked[0] == 151 and c._protocol.asked[-1] == 200


async def test_an_older_record_is_not_filed_as_the_session_just_ended():
    """Held record lags (930), handle answers with 933 filed before brushing."""
    c = _wired(_bare_coordinator(), {933: _rec(933, 36000)})
    c._link_lock = asyncio.Lock()
    c._latest_at_start = 933
    observed = {"session_id": None, "previous_id": 930, "source": "observed",
                "started_at": "2026-10-01T16:11:11+00:00", "duration": 1}
    c.data = {"last_session": observed}
    c._protocol.fetch_stored_session = (
        lambda sid=None: c._protocol.__class__.fetch_stored_session(c._protocol, 933))
    await c._run_session_end(None, witnessed=True)
    assert c.data["last_session"] is observed


def test_the_session_start_remembers_the_newest_filed_number():
    c = _bare_coordinator()
    c._track_session({"brushing_state": "off"},
                     {"brushing_state": "on", "latest_session_id": 933})
    assert c._latest_at_start == 933
