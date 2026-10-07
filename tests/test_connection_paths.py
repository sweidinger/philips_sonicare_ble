"""Which adapter a direct connection goes through.

Seen in the field on 2026-10-07: the brush woke, an ESP32 proxy a room away
reported it first and got the connect, the link could not be encrypted
there, and the Android proxy next to the bathroom - which heard the brush
at -74 dBm the whole time - was never tried.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.philips_sonicare_ble import connection_paths as cp
from custom_components.philips_sonicare_ble.exceptions import TransportError

from .test_stored_session import _bare_coordinator

ADDR = "24:E5:AA:E1:32:1B"
PANEL = "7A:B5:FE:0D:C6:7A"
ESP = "EC:DA:3B:4E:11:72"


class _Scanner:
    def __init__(self, source, stamp=None):
        self.source = source
        self.stamp = stamp
        self._connect_failures: dict[str, int] = {}

    @property
    def discovered_device_timestamps(self):
        return {} if self.stamp is None else {ADDR: self.stamp}


def _seen(scanner, rssi):
    return SimpleNamespace(scanner=scanner, advertisement=SimpleNamespace(rssi=rssi))


@pytest.fixture
def air(monkeypatch):
    """What the scanners currently report, editable by the test."""
    state = {"devices": [], "scanners": {}}
    monkeypatch.setattr(
        cp, "async_scanner_devices_by_address",
        lambda hass, address, connectable: list(state["devices"]),
    )
    monkeypatch.setattr(
        cp, "async_scanner_by_source",
        lambda hass, source: state["scanners"].get(source),
    )
    monkeypatch.setattr(cp, "POLL_INTERVAL", 0.01)
    return state


# --- policy ---------------------------------------------------------------

def test_auto_means_no_preference():
    assert cp.ConnectionPathPolicy("auto").preferred_source is None
    assert cp.ConnectionPathPolicy("").preferred_source is None
    assert cp.ConnectionPathPolicy(PANEL).preferred_source == PANEL


def test_avoid_expires_and_forgive_clears():
    t = [100.0]
    policy = cp.ConnectionPathPolicy(PANEL, clock=lambda: t[0])
    policy.avoid(ESP, seconds=10)
    assert policy.avoided() == {ESP}
    t[0] = 111.0
    assert policy.avoided() == set()
    policy.avoid(ESP)
    policy.forgive(ESP)
    assert policy.avoided() == set()


def test_fresh_sources_skips_invalidated_and_stale(air):
    now = cp.now()
    air["devices"] = [
        _seen(_Scanner(PANEL, stamp=now), -74),
        _seen(_Scanner(ESP, stamp=now - 60), -88),       # heard a minute ago
        _seen(_Scanner("11:22:33:44:55:66"), -127),     # BlueZ leftover
    ]
    assert cp.fresh_sources(None, ADDR, since=now - 5) == {PANEL: -74}
    assert set(cp.fresh_sources(None, ADDR)) == {PANEL, ESP}


def test_has_alternative_ignores_avoided(air):
    policy = cp.ConnectionPathPolicy(PANEL)
    air["devices"] = [_seen(_Scanner(ESP, stamp=cp.now()), -88)]
    policy.avoid(ESP)
    assert not policy.has_alternative(None, ADDR)
    air["devices"].append(_seen(_Scanner(PANEL, stamp=cp.now()), -74))
    assert policy.has_alternative(None, ADDR)


# --- waiting for the preferred scanner -------------------------------------

async def test_waits_until_the_preferred_scanner_reports(air):
    policy = cp.ConnectionPathPolicy(PANEL)
    air["devices"] = [_seen(_Scanner(ESP, stamp=cp.now()), -88)]

    async def panel_reports_later():
        await asyncio.sleep(0.05)
        air["devices"].append(_seen(_Scanner(PANEL, stamp=cp.now()), -74))

    task = asyncio.create_task(panel_reports_later())
    assert await policy.wait_for_preferred(None, ADDR, timeout=1.0) is True
    await task


async def test_gives_up_when_the_preferred_scanner_stays_silent(air):
    policy = cp.ConnectionPathPolicy(PANEL)
    air["devices"] = [_seen(_Scanner(ESP, stamp=cp.now()), -88)]
    assert await policy.wait_for_preferred(None, ADDR, timeout=0.05) is False


async def test_old_report_from_the_preferred_scanner_does_not_count(air):
    policy = cp.ConnectionPathPolicy(PANEL)
    air["devices"] = [_seen(_Scanner(PANEL, stamp=cp.now() - 120), -74)]
    assert await policy.wait_for_preferred(None, ADDR, timeout=0.05) is False


async def test_no_wait_without_preference_or_when_avoided(air):
    assert await cp.ConnectionPathPolicy().wait_for_preferred(None, ADDR) is False
    policy = cp.ConnectionPathPolicy(PANEL)
    policy.avoid(PANEL)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await policy.wait_for_preferred(None, ADDR) is False
    assert loop.time() - t0 < 0.5


# --- counting a failure in HA's ranking ------------------------------------

def test_penalize_uses_habluetooth_counter(air):
    esp = _Scanner(ESP)
    air["scanners"][ESP] = esp
    assert cp.penalize_source(None, ESP, ADDR) is True
    assert cp.penalize_source(None, ESP, ADDR) is True
    assert esp._connect_failures == {ADDR: 2}


def test_penalize_prefers_the_public_helper(air):
    calls = []
    esp = SimpleNamespace(source=ESP, _add_connect_failure=calls.append)
    air["scanners"][ESP] = esp
    assert cp.penalize_source(None, ESP, ADDR) is True
    assert calls == [ADDR]


def test_penalize_unknown_scanner_is_a_no_op(air):
    assert cp.penalize_source(None, "00:00:00:00:00:00", ADDR) is False


# --- the coordinator moving on after an unencrypted link --------------------

class _Transport:
    """Connects through the sources it is given, one per connect."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.connected_source = None
        self.connects = 0
        self.disconnects = 0

    async def connect(self):
        self.connects += 1
        route = self.routes.pop(0)
        if isinstance(route, Exception):
            raise route
        self.connected_source = route

    async def disconnect(self):
        self.disconnects += 1
        self.connected_source = None


def _coordinator(air, routes, preferred=PANEL):
    c = _bare_coordinator()
    c.address = ADDR
    c.hass = None
    c._path_policy = cp.ConnectionPathPolicy(preferred)
    c.transport = _Transport(routes)
    c._smp_failed = False
    c._wake_event = asyncio.Event()
    c._adv_wake = False
    c.PATH_FALLBACK_WAIT = 0.05
    air["scanners"][ESP] = _Scanner(ESP)
    air["devices"] = [
        _seen(_Scanner(ESP, stamp=cp.now()), -88),
        _seen(_Scanner(PANEL, stamp=cp.now()), -74),
    ]
    return c


def test_auth_failure_is_recognised():
    c = _bare_coordinator()
    c._smp_failed = False
    assert c._is_link_auth_failure(
        TransportError("No notifications could be subscribed"))
    assert not c._is_link_auth_failure(TransportError("Device not in range"))
    c._smp_failed = True
    assert c._is_link_auth_failure(RuntimeError("anything"))


async def test_reconnects_through_the_panel(air):
    c = _coordinator(air, [PANEL])
    c._wake_event.set()  # the disconnect nudge
    assert await c._reconnect_elsewhere(ESP) is True
    assert c.transport.connected_source == PANEL
    assert ESP in c._path_policy.avoided()
    assert air["scanners"][ESP]._connect_failures == {ADDR: 1}
    assert not c._wake_event.is_set()


async def test_routed_the_same_way_again_is_dropped_and_retried(air):
    c = _coordinator(air, [ESP, PANEL])
    assert await c._reconnect_elsewhere(ESP) is True
    assert c.transport.connects == 2 and c.transport.disconnects == 1
    assert air["scanners"][ESP]._connect_failures == {ADDR: 2}


async def test_failed_connects_use_up_the_attempts(air):
    c = _coordinator(air, [TransportError("x")] * 3)
    assert await c._reconnect_elsewhere(ESP) is False
    assert c.transport.connects == 3


async def test_nothing_else_hears_the_brush(air):
    c = _coordinator(air, [PANEL])
    air["devices"] = [_seen(_Scanner(ESP, stamp=cp.now()), -88)]
    assert await c._reconnect_elsewhere(ESP) is False
    assert c.transport.connects == 0


# --- the options list ------------------------------------------------------

def test_options_list_connectable_scanners(monkeypatch):
    import habluetooth

    from custom_components.philips_sonicare_ble.config_flow import (
        _connectable_scanner_options,
    )

    scanners = [
        SimpleNamespace(source=ESP, name="esp32-bluetooth-proxy-4e1170 (EC:DA:3B:4E:11:72)",
                        connectable=True),
        SimpleNamespace(source=PANEL, name="android_ble_proxy (7A:B5:FE:0D:C6:7A)",
                        connectable=True),
        SimpleNamespace(source="8C:BF:EA:94:1B:EE", name="Shelly X1C", connectable=False),
    ]
    monkeypatch.setattr(
        habluetooth, "get_manager",
        lambda: SimpleNamespace(async_current_scanners=lambda: scanners),
    )
    values = [o["value"] for o in _connectable_scanner_options(None, PANEL)]
    assert values == ["auto", PANEL, ESP]

    # A saved choice that is currently offline stays selectable.
    values = [o["value"] for o in _connectable_scanner_options(None, "00:11:22:33:44:55")]
    assert values[-1] == "00:11:22:33:44:55"
