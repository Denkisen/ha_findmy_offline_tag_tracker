"""Device tracker platform: tag in Bluetooth range = at the associated zone.

The person component lists entities in the ``device_tracker`` domain, so this
platform is what makes the tag assignable to a person (Settings → Devices &
Services → Person). In range maps to the tracker's *associated zone*
(default: Home) via ``BaseScannerEntity``; out of range maps to ``not_home``.
"""

from __future__ import annotations

from homeassistant.components.device_tracker import (
    BaseScannerEntity,
    SourceType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    async_add_entities([FindMyTag(entry, coordinator)])


class FindMyTag(BaseScannerEntity, RestoreEntity):
    """Tracker entity for one Find My tag's Bluetooth presence."""

    _attr_has_entity_name = True
    _attr_name = "Tag"
    _attr_source_type = SourceType.BLUETOOTH_LE
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: TagCoordinator) -> None:
        self._coordinator = coordinator
        self._entry = entry

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_tag"

    @property
    def device_info(self):
        return self._coordinator.device_info

    @property
    def is_connected(self) -> bool | None:
        """In Bluetooth range of the home scanner == connected."""
        return self._coordinator.is_home

    async def async_added_to_hass(self) -> None:
        # Restore the in-range flag from the last known state across restarts.
        if (last_state := await self.async_get_last_state()) is not None:
            self._coordinator.restore_is_home(last_state.state == "home")

        self.async_on_remove(
            self._coordinator.add_listener(self._async_write_state)
        )

    @callback
    def _async_write_state(self) -> None:
        self.async_write_ha_state()
