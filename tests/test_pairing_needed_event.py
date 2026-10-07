"""The event that tells somebody an adapter is waiting for a pairing tap.

Seen on 2026-10-08 at 00:19: the brush had lost its bond, the Android proxy
on the hallway panel connected, and Android put up its pairing dialog. The
probe read hung for 30 s while the dialog waited, and nobody was at the
panel to see it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from .test_stored_session import _bare_coordinator

PANEL = "7A:B5:FE:0D:C6:7A"


class _Bus:
    def __init__(self):
        self.fired = []

    def async_fire(self, event, data):
        self.fired.append((event, data))


class _Transport:
    is_connected = True
    connection_path = f"android_ble_proxy ({PANEL})"
    connected_source = PANEL

    def __init__(self, reads, error=None, hang=0.0):
        self.reads = list(reads)
        self.error = error
        self.hang = hang

    async def read_char(self, _uuid):
        if self.hang:
            await asyncio.sleep(self.hang)
        return self.reads.pop(0) if self.reads else None

    def pop_read_error(self, _uuid):
        return self.error


def _coordinator(transport):
    c = _bare_coordinator()
    c.hass = SimpleNamespace(bus=_Bus())
    c.entry = SimpleNamespace(entry_id="e1")
    c.transport = transport
    c._smp_failed = False
    return c


def _events(c):
    return [d for e, d in c.hass.bus.fired
            if e == "philips_sonicare_ble_pairing_needed"]


async def test_hanging_probe_is_announced_while_it_hangs():
    c = _coordinator(_Transport([None], error="Timeout waiting for "
                                "BluetoothGATTReadResponse", hang=0.3))
    c.SMP_SLOW_NOTICE = 0.05
    await c._eager_smp_probe()
    events = _events(c)
    assert len(events) == 1                      # not again at the timeout
    assert events[0]["reason"] == "timeout"
    assert events[0]["source"] == PANEL
    assert events[0]["adapter"].startswith("android_ble_proxy")
    assert c._smp_failed


async def test_missing_bond_on_the_adapter_is_named(monkeypatch):
    c = _coordinator(_Transport([], error="Bluetooth GATT Error "
                                "error=15 description=Insufficient encryption"))
    loop = asyncio.get_running_loop()
    # Run the 3 s probe deadline instantly.
    start = loop.time()
    times = iter([start, start, start + 10, start + 10, start + 10])
    monkeypatch.setattr(loop, "time", lambda: next(times, start + 10))
    await c._eager_smp_probe()
    events = _events(c)
    assert [e["reason"] for e in events] == ["insufficient_encryption"]


async def test_encrypted_link_announces_nothing():
    c = _coordinator(_Transport([b"\x01"]))
    c.SMP_SLOW_NOTICE = 0.05
    await c._eager_smp_probe()
    await asyncio.sleep(0.1)                     # past the notice delay
    assert _events(c) == []
    assert not c._smp_failed


def test_no_hass_no_event():
    c = _bare_coordinator()
    c._fire_pairing_needed("Timeout")            # must not raise
