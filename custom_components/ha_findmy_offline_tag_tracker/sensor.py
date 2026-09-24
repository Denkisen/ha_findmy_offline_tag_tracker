"""Sensor platform for FHN Tracker."""

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
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
    """Set up the FHN Tracker sensors."""
    device: TagDevice = hass.data[entry.entry_id]["device"]

    async def async_update_data():
        return device

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="fhn_tracker_sensors",
        update_method=async_update_data,
        update_interval=None,
    )
    device._coordinator = coordinator

    async_add_entities([
        RSSISensor(device, coordinator),
        LastSeenSensor(device, coordinator),
        ClockSkewSensor(device, coordinator),
    ])


class FHNTrackerSensor(CoordinatorEntity, SensorEntity):
    """Base class for FHN Tracker sensors."""

    def __init__(self, device: TagDevice, coordinator: DataUpdateCoordinator):
        super().__init__(coordinator)
        self.device = device
        self._attr_has_entity_name = True
        self._attr_available = True


class RSSISensor(FHNTrackerSensor):
    """RSSI signal strength sensor."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: TagDevice, coordinator: DataUpdateCoordinator):
        super().__init__(device, coordinator)
        self._attr_unique_id = f"{device.beacon.model_id}_rssi"
        self._attr_translation_key = "rssi"

    @property
    def native_value(self):
        return self.device.last_rssi

    @property
    def available(self):
        return self.device.last_rssi is not None


class LastSeenSensor(FHNTrackerSensor):
    """Last seen timestamp sensor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: TagDevice, coordinator: DataUpdateCoordinator):
        super().__init__(device, coordinator)
        self._attr_unique_id = f"{device.beacon.model_id}_last_seen"
        self._attr_translation_key = "last_seen"

    @property
    def native_value(self):
        if self.device.last_seen is None:
            return None
        return self.device.last_seen

    @property
    def available(self):
        return self.device.last_seen is not None


class ClockSkewSensor(FHNTrackerSensor):
    """Clock skew indicator from tag."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "ticks"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, device: TagDevice, coordinator: DataUpdateCoordinator):
        super().__init__(device, coordinator)
        self._attr_unique_id = f"{device.beacon.model_id}_clock_skew"
        self._attr_translation_key = "clock_skew"

    @property
    def native_value(self):
        return self.device.last_clock_skew_ticks

    @property
    def available(self):
        return self.device.last_clock_skew_ticks is not None