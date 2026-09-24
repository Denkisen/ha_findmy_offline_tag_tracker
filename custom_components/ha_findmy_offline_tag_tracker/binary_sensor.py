"""Binary sensor platform: tag in-range / out-of-range."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import TagCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TagCoordinator = entry.runtime_data
    async_add_entities([LoshallTagPresence(entry, coordinator)])


class LoshallTagPresence(BinarySensorEntity, RestoreEntity):
    """Reports whether the tag is currently in Bluetooth range."""

    _attr_has_entity_name = True
    _attr_name = "In Range"
    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: TagCoordinator) -> None:
        self._coordinator = coordinator
        self._entry = entry

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_presence"

    @property
    def device_info(self) -> DeviceInfo:
        return self._coordinator.device_info

    @property
    def is_on(self) -> bool:
        return self._coordinator.is_home

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_seen = self._coordinator.last_seen
        return {
            "last_seen": last_seen.isoformat() if last_seen else None,
            "rssi": self._coordinator.rssi,
            "address": self._coordinator.address,
        }

    async def async_added_to_hass(self) -> None:
        # Restore the in-range flag from the last known state across restarts.
        if (last_state := await self.async_get_last_state()) is not None:
            self._coordinator.restore_is_home(last_state.state == "on")

        self.async_on_remove(
            self._coordinator.add_listener(self._async_write_state)
        )

    @callback
    def _async_write_state(self) -> None:
        self.async_write_ha_state()
