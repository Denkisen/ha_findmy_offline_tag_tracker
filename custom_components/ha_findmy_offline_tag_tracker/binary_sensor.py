"""Binary sensor platform for FHN Tracker."""

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from datetime import datetime
import logging

from . import TagDevice, PRESENT_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
):
    """Set up the FHN Tracker binary sensors."""
    device: TagDevice = hass.data[entry.entry_id]["device"]

    async def async_update_data():
        return device

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="fhn_tracker_presence",
        update_method=async_update_data,
        update_interval=None,
    )

    async_add_entities([
        PresenceSensor(device, coordinator),
    ])


class PresenceSensor(CoordinatorEntity, BinarySensorEntity):
    """Presence detection binary sensor."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE

    def __init__(self, device: TagDevice, coordinator: DataUpdateCoordinator):
        super().__init__(coordinator)
        self.device = device
        self._attr_has_entity_name = True
        self._attr_unique_id = f"{device.beacon.model_id}_presence"
        self._attr_translation_key = "presence"

    @property
    def is_on(self):
        if self.device.last_seen is None:
            return False
        elapsed = (datetime.now() - self.device.last_seen).total_seconds()
        return elapsed < PRESENT_TIMEOUT_SECONDS

    @property
    def available(self):
        return self.device.last_seen is not None

    @property
    def extra_state_attributes(self):
        attrs = {
            "timeout_seconds": PRESENT_TIMEOUT_SECONDS,
            "model_id": self.device.beacon.model_id,
            "original_mac": self.device.beacon.original_mac,
        }
        if self.device.last_rssi is not None:
            attrs["rssi_dbm"] = self.device.last_rssi
        if self.device.last_clock_skew_ticks is not None:
            attrs["clock_skew_ticks"] = self.device.last_clock_skew_ticks
        return attrs