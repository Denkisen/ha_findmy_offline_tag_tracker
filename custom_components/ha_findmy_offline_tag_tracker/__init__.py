"""FindMy Tag integration for Home Assistant."""

import logging
import asyncio
from datetime import datetime
from typing import Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.bluetooth import async_register_callback
from homeassistant.exceptions import ConfigEntryNotReady

from bleak.backends.scanner import BLEDevice as BleakBLEDevice

from .fhn_codec import (
    FHNTracker,
    create_fhn_beacon,
    EDDYSTONE_SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]
PRESENT_TIMEOUT_SECONDS = 120


class TagDevice:
    """Represents a provisioned Tag."""

    def __init__(self, config_data: dict):
        self.beacon = create_fhn_beacon(config_data)
        self.tracker = FHNTracker([self.beacon])

        self.is_present = False
        self.last_rssi = None
        self.last_seen = None
        self.last_clock_skew_ticks = None

        self._presence_timeout_handle = None
        self._coordinator = None

    def _schedule_presence_timeout(self):
        """Schedule task to mark device absent after timeout."""
        if self._presence_timeout_handle:
            self._presence_timeout_handle.cancel()

        async def mark_absent():
            await asyncio.sleep(PRESENT_TIMEOUT_SECONDS)
            if self._presence_timeout_handle is None:
                self.is_present = False
                _LOGGER.debug(
                    f"Tag ({self.beacon.model_id}) marked absent"
                )

        self._presence_timeout_handle = asyncio.create_task(mark_absent())

    async def process_advertisement(
        self,
        device: BleakBLEDevice,
        advertisement
    ):
        """Process a Bluetooth advertisement packet."""
        service_data = getattr(advertisement, 'service_data', {})
        if EDDYSTONE_SERVICE_UUID not in service_data:
            return

        matched_beacon = self.tracker.process_advertisement(device, advertisement)

        if matched_beacon:
            self.is_present = True
            self.last_rssi = matched_beacon.last_rssi
            self.last_seen = matched_beacon.last_seen
            self.last_clock_skew_ticks = matched_beacon.last_clock_skew_ticks

            _LOGGER.debug(
                f"Tag present: RSSI={self.last_rssi}, "
                f"skew={self.last_clock_skew_ticks} ticks"
            )

            self._schedule_presence_timeout()

            if self._coordinator:
                self._coordinator.async_set_updated_data(self)
        else:
            _LOGGER.debug("Advertisement received but no match")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FHN Tracker from a config entry."""
    _LOGGER.info(f"Setting up FHN tracker for FindMy Tag")

    device = TagDevice(entry.data)
    hass.data.setdefault(entry.entry_id, {"device": device})

    try:
        @callback
        def bluetooth_callback(service_data, add_global_scan_results):
            """Handle incoming Bluetooth advertisements."""
            dev = getattr(service_data, 'device', None)
            if dev is None:
                return

            asyncio.create_task(device.process_advertisement(dev, service_data))

        callback_handle = async_register_callback(
            hass,
            bluetooth_callback,
            {"connectable": False}
        )

        hass.data[entry.entry_id]["callback"] = callback_handle

        _LOGGER.info("FHN Tracker initialized, listening for Eddystone ads")

        await hass.config_entries.async_forward_platform_setups(entry, PLATFORMS)

    except Exception as e:
        _LOGGER.error(f"Failed to setup FHN tracker: {e}", exc_info=True)
        raise ConfigEntryNotReady(f"Failed to setup FHN tracker: {e}")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload FHN Tracker entry."""
    device_data = hass.data.get(entry.entry_id, {})

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        callback = device_data.pop("callback", None)
        if callback and hasattr(callback, 'unregister'):
            callback.unregister()

        device = device_data.get("device")
        if device and device._presence_timeout_handle:
            device._presence_timeout_handle.cancel()

    return unload_ok