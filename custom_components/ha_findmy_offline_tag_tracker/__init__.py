"""The Find My Tag Tracker integration (Loshall Tag, Tile, and any other
Find My / Find Hub Network beacon, given its provisioning key material)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import TagCoordinator
from .fhn_codec import build_tracker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry.

    Builds the tracker from the stored key material, creates a shared
    :class:`TagCoordinator`, starts it (which registers the Bluetooth
    advertisement callback), and forwards the config entry to the entity
    platforms.
    """
    data = entry.data
    try:
        tracker = build_tracker(
            clock_offset=float(data["clock_offset"]),
            curve_name=data["curve"],
            ephemeral_identity_key_hex=data["ephemeral_identity_key"],
            name=data["name"],
            now=datetime.now(timezone.utc),
        )
    except (ValueError, KeyError) as err:
        _LOGGER.error("Invalid beacon configuration: %s", err)
        return False

    coordinator = TagCoordinator(hass, entry, tracker, data["name"])
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start only after all platforms have registered their listeners.
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the integration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
