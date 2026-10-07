"""A link that cannot be encrypted is given up, not carried on with.

On 2026-10-08 an ESP32 proxy without a bond carried the connect: the SMP
probe failed, every read answered "Insufficient encryption", yet ten
subscribes reported success and live monitoring started on a dead link.
"""
from __future__ import annotations

import pytest

from custom_components.philips_sonicare_ble.exceptions import TransportError

from .test_stored_session import _bare_coordinator


class _Protocol:
    def __init__(self, results):
        self.results = results

    async def read_chars(self, uuids):
        return dict(self.results)


def _coordinator(results, smp_ok):
    c = _bare_coordinator()
    c._poll_chars = list(results)
    c._live_chars = list(results)
    c._full_read_done = False
    c._protocol = _Protocol(results)
    c._smp_failed = False
    c._scanner_needs_eager_smp = lambda: True

    async def probe():
        c._smp_failed = not smp_ok
    c._eager_smp_probe = probe

    async def subscribe_all():
        return 10
    c._start_all_notifications = subscribe_all
    c._process_results = lambda r: {}
    c.async_set_updated_data = lambda d: None
    return c


async def test_nothing_readable_after_failed_probe_aborts():
    c = _coordinator({"a": None, "b": None}, smp_ok=False)
    with pytest.raises(TransportError) as err:
        await c._setup_classic_session()
    assert c._is_link_auth_failure(err.value)


async def test_late_encryption_with_readable_data_carries_on():
    # The probe gave up, but SMP finished in time for the read burst.
    c = _coordinator({"a": b"\x01", "b": None}, smp_ok=False)
    assert await c._setup_classic_session() == 10


async def test_encrypted_link_is_unaffected():
    c = _coordinator({"a": None, "b": None}, smp_ok=True)
    assert await c._setup_classic_session() == 10
