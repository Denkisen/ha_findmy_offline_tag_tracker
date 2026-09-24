"""Event-driven coordinator that tracks a single Find Hub Network beacon.

The coordinator owns all mutable state (last seen, RSSI, MAC address, in-range
flag). It registers one passive Bluetooth advertisement callback filtered on
the Eddystone service-data UUID, matches incoming advertisements against the
beacon's rolling EID window, and notifies a set of listeners (one per entity).

This mirrors the pattern used by built-in passive-BLE integrations such as
``bthome`` (see ``homeassistant.components.bluetooth.update_coordinator``):
a plain object that calls the synchronous ``async_register_callback`` and
tracks its unsubs in ``_on_stop``.

In addition to matching advertisements, the coordinator follows HA's
per-address *unavailable* signal for the tag's current MAC (re-pointing it on
every MAC change) so a real departure flips presence OFF in ~5 min instead of
waiting for the long presence-timeout backstop — see AGENTS.md for the why.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Optional

from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_register_callback,
    async_track_unavailable,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import DEFAULT_PRESENCE_TIMEOUT, DOMAIN, EDDYSTONE_SERVICE_UUID
from .fhn_codec import TrackedBeacon

_LOGGER = logging.getLogger(__name__)

# How often we re-evaluate the presence timeout (seconds).
PRESENCE_CHECK_INTERVAL = timedelta(seconds=10)

Listener = Callable[[], None]


class TagCoordinator:
    """Tracks one FHN beacon and its in-range state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tracker: TrackedBeacon,
        name: str,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.tracker = tracker
        self._name = name
        self._eid_hex = tracker.beacon.ephemeral_identity_key.hex()
        self._on_stop: list[CALLBACK_TYPE] = []
        self._listeners: set[Listener] = set()

        self._last_seen: Optional[datetime] = None
        self._rssi: Optional[int] = None
        self._address: Optional[str] = None
        self._is_home = False
        self._unsub_unavailable: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ #
    # Listener management
    # ------------------------------------------------------------------ #
    def add_listener(self, listener: Listener) -> CALLBACK_TYPE:
        """Subscribe ``listener`` to state changes; returns an unsubscribe callable."""
        self._listeners.add(listener)

        @callback
        def _remove() -> None:
            self._listeners.discard(listener)

        return _remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ------------------------------------------------------------------ #
    # Bluetooth
    # ------------------------------------------------------------------ #
    @callback
    def _async_handle_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        advertisement = service_info.service_data.get(EDDYSTONE_SERVICE_UUID)
        if advertisement is None:
            return
        now = datetime.now(timezone.utc)
        if self.tracker.match(advertisement, now):
            self._last_seen = now
            self._rssi = service_info.rssi
            address = service_info.address
            if address != self._address:
                self._address = address
                self._async_track_address_unavailable(address)
            if not self._is_home:
                self._is_home = True
            self._notify()

    def _async_track_address_unavailable(self, address: str) -> None:
        """Follow HA's per-address "device gone" signal for the current tag address.

        The tag's MAC randomises, so whenever we see a *new* address we re-point
        this tracker. HA fires the callback once the address stops advertising
        (scanner expiry + its 5-minute sweep), which is a physically meaningful
        "tag left" signal — far snappier than waiting for the presence timeout.
        """
        if self._unsub_unavailable is not None:
            self._unsub_unavailable()

        @callback
        def _unavailable(_service_info: BluetoothServiceInfoBleak) -> None:
            if _service_info.address != self._address:
                return  # stale: the tag already moved to a newer address
            self._is_home = False
            _LOGGER.debug("%s: address %s unavailable, marking out of range", self._name, address)
            self._notify()

        self._unsub_unavailable = async_track_unavailable(
            self.hass, _unavailable, address, connectable=False
        )

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Register the Bluetooth callback and presence timer."""
        self._on_stop.append(
            async_register_callback(
                self.hass,
                self._async_handle_advertisement,
                {"service_data_uuid": EDDYSTONE_SERVICE_UUID, "connectable": False},
                BluetoothScanningMode.PASSIVE,
            )
        )
        self._on_stop.append(
            async_track_time_interval(
                self.hass,
                self._async_check_presence,
                PRESENCE_CHECK_INTERVAL,
            )
        )
        return self.async_stop

    @callback
    def async_stop(self) -> None:
        """Unregister all callbacks and clear listeners."""
        for unsub in self._on_stop:
            unsub()
        self._on_stop.clear()
        if self._unsub_unavailable is not None:
            self._unsub_unavailable()
            self._unsub_unavailable = None
        self._listeners.clear()

    @callback
    def _async_check_presence(self, _now: datetime) -> None:
        """Flip in-range to False if the beacon has not been seen recently."""
        if not self._is_home or self._last_seen is None:
            return
        timeout = self.entry.options.get(
            "presence_timeout", DEFAULT_PRESENCE_TIMEOUT
        )
        elapsed = (datetime.now(timezone.utc) - self._last_seen).total_seconds()
        if elapsed > timeout:
            _LOGGER.debug(
                "%s: no advertisement for %ss, marking out of range",
                self._name,
                int(elapsed),
            )
            self._is_home = False
            self._notify()

    # ------------------------------------------------------------------ #
    # State accessors
    # ------------------------------------------------------------------ #
    @property
    def device_info(self) -> DeviceInfo:
        """Stable device registry info.

        The beacon's MAC address randomises and its EID rotates, so neither is
        a stable identifier. The Ephemeral Identity Key is the beacon's real
        cryptographic identity and is stable across power cycles, so we use it
        as the device identifier.
        """
        return {
            "name": self._name,
            "identifiers": {(DOMAIN, self._eid_hex)},
            "connections": {(CONNECTION_BLUETOOTH, self._name.lower())},
            "manufacturer": "Loshall",
            "model": "Loshall Tag",
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_home(self) -> bool:
        return self._is_home

    def restore_is_home(self, value: bool) -> None:
        """Restore the in-range flag from the entity's last known state.

        A restored "in range" state is treated as "seen at restart" so the
        presence timeout can still re-evaluate it: without this, a restored
        ``_is_home=True`` with ``_last_seen=None`` would never flip back to
        out-of-range (the timeout check returns early when last_seen is None).
        """
        self._is_home = value
        if value:
            self._last_seen = datetime.now(timezone.utc)

    @property
    def last_seen(self) -> Optional[datetime]:
        return self._last_seen

    @property
    def rssi(self) -> Optional[int]:
        return self._rssi

    @property
    def address(self) -> Optional[str]:
        return self._address
