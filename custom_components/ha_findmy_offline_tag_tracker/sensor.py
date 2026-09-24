"""Sensor platform: RSSI and last-seen time for the tracked tag."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import TagCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TagCoordinator = entry.runtime_data
    async_add_entities(
        [
            LoshallTagRssi(entry, coordinator),
            LoshallTagLastSeen(entry, coordinator),
        ]
    )


class _LoshallTagSensor(SensorEntity):
    """Base class for tag sensors that subscribe to coordinator updates."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: TagCoordinator) -> None:
        self._coordinator = coordinator
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return self._coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.add_listener(self._async_write_state)
        )

    @callback
    def _async_write_state(self) -> None:
        self.async_write_ha_state()


class LoshallTagRssi(_LoshallTagSensor):
    """Report the RSSI of the last matching advertisement."""

    _attr_name = "RSSI"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "dBm"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_rssi"

    @property
    def native_value(self) -> int | None:
        return self._coordinator.rssi

    @property
    def available(self) -> bool:
        return self._coordinator.rssi is not None


class LoshallTagLastSeen(_LoshallTagSensor):
    """Report when the tag was last seen in range."""

    _attr_name = "Last Seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_last_seen"

    @property
    def native_value(self) -> Any:
        return self._coordinator.last_seen
