"""Which adapter a direct connection goes through.

Home Assistant routes a connect through whichever connectable scanner holds
the strongest advertisement *at that moment*. It has no notion of a
preferred adapter or a backup one, and it only considers scanners that have
already reported the device. A brush is connected to on its very first
advertisement, so a scanner that reports quickly - an ESP32 proxy scanning
actively - regularly wins over one that reports a second or two later, even
when the slower one is right next to the bathroom and the fast one is a
room away.

Two things follow from that, and both live here:

* A preferred scanner is given a short moment to report the brush before
  the connect goes out. Once it has, the RSSI ranking picks it on its own.
* A scanner the link could not be encrypted through is set aside for a
  while. Home Assistant counts only failed connects against a scanner; a
  connect that succeeds and then cannot encrypt looks like a success to it,
  so it would pick the same scanner again on the next try.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from homeassistant.components.bluetooth import (
    async_scanner_by_source,
    async_scanner_devices_by_address,
)
from homeassistant.core import HomeAssistant

try:
    from bluetooth_data_tools import monotonic_time_coarse as _monotonic
except ImportError:  # pragma: no cover - ships with habluetooth
    _monotonic = time.monotonic

_LOGGER = logging.getLogger(__name__)

# How long a connect waits for the preferred scanner to report the brush.
# Measured in the field: an Android proxy reported the brush one to two
# seconds after an ESP32 proxy did.
PREFERRED_SCANNER_WAIT = 3.0
# How far back an advertisement may lie and still count as "the brush is
# awake now". Covers the wait itself plus the advertisement that woke us.
ADVERTISEMENT_SLACK = 1.5
# How long a scanner the link could not be encrypted through is set aside.
AVOID_SOURCE_SECONDS = 600.0
# Advertisements older than this do not count as a live alternative path.
ALTERNATIVE_MAX_AGE = 10.0
POLL_INTERVAL = 0.2

# Option value meaning "no preference - strongest signal wins".
PREFERRED_SCANNER_AUTO = "auto"


def now() -> float:
    """The clock habluetooth stamps advertisements with."""
    return _monotonic()


def fresh_sources(
    hass: HomeAssistant, address: str, since: float | None = None
) -> dict[str, int | None]:
    """Connectable scanners that have heard *address*, by source, with RSSI.

    With *since*, only scanners whose latest advertisement from the device
    arrived at or after that moment count. A scanner that cannot say when it
    last heard the device counts as fresh: leaving it out would hide a real
    path, while counting it costs at most a connect that HA would have
    attempted anyway.
    """
    sources: dict[str, int | None] = {}
    try:
        devices = async_scanner_devices_by_address(hass, address, connectable=True)
    except Exception:  # noqa: BLE001 - routing hint only, never break a connect
        return sources
    for sd in devices:
        rssi = getattr(sd.advertisement, "rssi", None)
        # BlueZ leaves a -127 entry behind when it invalidates a cached
        # device; that scanner does not currently hear anything.
        if rssi is not None and rssi <= -127:
            continue
        scanner = sd.scanner
        source = getattr(scanner, "source", None)
        if not source:
            continue
        if since is not None:
            try:
                stamp = scanner.discovered_device_timestamps.get(address)
            except Exception:  # noqa: BLE001
                stamp = None
            if stamp is not None and stamp < since:
                continue
        sources[source] = rssi
    return sources


def penalize_source(hass: HomeAssistant, source: str, address: str) -> bool:
    """Count a failed connect against *source* in HA's own path ranking.

    habluetooth lowers a scanner's score per recorded failure and clears the
    count on its next successful connect, so this nudges the ranking without
    taking the scanner out of service. The counter is internal to
    habluetooth, hence the defensive access: if it is ever renamed the
    nudge is skipped and the explicit avoid list still applies.
    """
    try:
        scanner = async_scanner_by_source(hass, source)
    except Exception:  # noqa: BLE001
        scanner = None
    if scanner is None:
        return False
    add = getattr(scanner, "_add_connect_failure", None)
    if callable(add):
        add(address)
        return True
    failures = getattr(scanner, "_connect_failures", None)
    if isinstance(failures, dict):
        failures[address] = failures.get(address, 0) + 1
        return True
    return False


class ConnectionPathPolicy:
    """Preferred scanner and the scanners currently set aside, for one brush."""

    def __init__(
        self,
        preferred_source: str | None = None,
        clock: Callable[[], float] = now,
    ) -> None:
        if preferred_source == PREFERRED_SCANNER_AUTO:
            preferred_source = None
        self.preferred_source = preferred_source or None
        self._clock = clock
        self._avoid_until: dict[str, float] = {}

    def avoid(self, source: str, seconds: float = AVOID_SOURCE_SECONDS) -> None:
        """Set *source* aside for *seconds*."""
        self._avoid_until[source] = self._clock() + seconds

    def avoided(self) -> set[str]:
        """Sources currently set aside; expired entries are dropped."""
        current = self._clock()
        for source in [s for s, until in self._avoid_until.items() if until <= current]:
            del self._avoid_until[source]
        return set(self._avoid_until)

    def forgive(self, source: str) -> None:
        """Take *source* off the avoid list - it just carried a working link."""
        self._avoid_until.pop(source, None)

    def has_alternative(self, hass: HomeAssistant, address: str) -> bool:
        """Whether a scanner not set aside has heard the brush recently."""
        avoided = self.avoided()
        since = now() - ALTERNATIVE_MAX_AGE
        return any(
            source not in avoided
            for source in fresh_sources(hass, address, since=since)
        )

    async def wait_for_preferred(
        self,
        hass: HomeAssistant,
        address: str,
        timeout: float = PREFERRED_SCANNER_WAIT,
    ) -> bool:
        """Give the preferred scanner up to *timeout* s to report the brush.

        Returns True as soon as it has, False when there is no preference,
        the preferred scanner is set aside, or it stayed silent. Either way
        the connect goes ahead afterwards - this only decides whether the
        preferred scanner is in the running when HA ranks the paths.
        """
        source = self.preferred_source
        if not source or source in self.avoided():
            return False
        since = now() - ADVERTISEMENT_SLACK
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if source in fresh_sources(hass, address, since=since):
                return True
            if loop.time() >= deadline:
                _LOGGER.debug(
                    "%s: preferred scanner %s did not report the device "
                    "within %.1fs - connecting through whatever has",
                    address, source, timeout,
                )
                return False
            await asyncio.sleep(POLL_INTERVAL)
