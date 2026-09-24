"""End-to-end integration test using the real Home Assistant test fixture."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "custom_components"))

from homeassistant.components import bluetooth  # noqa: E402
from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from ha_findmy_offline_tag_tracker.const import (  # noqa: E402
    DEFAULT_PRESENCE_TIMEOUT,
    DOMAIN,
    EDDYSTONE_SERVICE_UUID,
)

CLOCK_OFFSET = "17000000.5"
EIK_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
CURVE = "secp160r1"


def _entry_data() -> dict:
    return {
        "name": "Loshall Tag 1",
        "clock_offset": CLOCK_OFFSET,
        "curve": CURVE,
        "ephemeral_identity_key": EIK_HEX,
    }


def _current_advertisement(eik: bytes) -> bytes:
    """Build a realistic Eddystone service-data payload with the current EID."""
    from ha_findmy_offline_tag_tracker import fhn_codec

    beacon = fhn_codec.FHNBeacon(
        float(CLOCK_OFFSET), fhn_codec.SECP160R1, eik, "Loshall Tag 1"
    )
    return b"\x40" + beacon.generate_eid(datetime.now(timezone.utc).timestamp())


@pytest.fixture
def ble_register_stub():
    """Stub out the real BLE callback registration (no BlueZ in tests).

    HA imports the coordinator as ``custom_components.ha_findmy_offline_tag_tracker.coordinator``,
    so we must patch the attributes on *that* module object (not a separately
    imported copy) for the running coordinator to see the stubs.
    """
    calls = []

    def _fake_register(hass, callback, match_dict, mode, *a, **k):
        calls.append({"callback": callback, "match_dict": match_dict, "mode": mode})

        def _unsub():
            calls[-1]["unsubscribed"] = True

        return _unsub

    def _fake_track_unavailable(hass, callback, address, connectable=True, *a, **k):
        calls.append({"unavailable": address, "callback": callback})

        def _unsub():
            calls[-1]["unsubscribed"] = True

        return _unsub

    with (
        patch(
            "custom_components.ha_findmy_offline_tag_tracker.coordinator.async_register_callback",
            _fake_register,
        ),
        patch(
            "custom_components.ha_findmy_offline_tag_tracker.coordinator.async_track_unavailable",
            _fake_track_unavailable,
        ),
    ):
        yield calls


async def test_full_setup_and_advertisement(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    assert await async_setup_component(hass, "bluetooth", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Loshall Tag 1",
        data=_entry_data(),
        unique_id=None,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The BLE callback was registered with the Eddystone service-data filter.
    assert len(ble_register_stub) == 1
    assert (
        ble_register_stub[0]["match_dict"]["service_data_uuid"]
        == EDDYSTONE_SERVICE_UUID
    )
    callback = ble_register_stub[0]["callback"]

    # Both platforms created entities.
    all_states = list(hass.states.async_entity_ids())
    presence = [e for e in all_states if e.startswith("binary_sensor.")]
    rssi = [e for e in all_states if e.startswith("sensor.") and "rssi" in e]
    last_seen = [e for e in all_states if e.startswith("sensor.") and "last" in e]
    assert len(presence) == 1, all_states
    assert len(rssi) == 1, all_states
    assert len(last_seen) == 1, all_states
    presence_id, rssi_id, last_seen_id = presence[0], rssi[0], last_seen[0]

    # Initial state: out of range.
    assert hass.states.get(presence_id).state == "off"
    assert hass.states.get(rssi_id).state == "unavailable"

    # Simulate the tag advertising (drive the registered callback directly).
    coordinator = entry.runtime_data
    eik = bytes.fromhex(EIK_HEX)
    ad = _current_advertisement(eik)
    service_info = SimpleNamespace(
        service_data={EDDYSTONE_SERVICE_UUID: ad},
        rssi=-63,
        address="AA:BB:CC:00:00:01",
    )
    callback(service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()

    assert coordinator.is_home is True
    assert hass.states.get(presence_id).state == "on"
    assert hass.states.get(rssi_id).state == "-63"
    assert hass.states.get(last_seen_id).state is not None

    # Once the tag's address is known, an unavailable tracker is registered
    # for it (this is the fast 'tag left' path for the door use case).
    assert any(
        c.get("unavailable") == "AA:BB:CC:00:00:01" for c in ble_register_stub
    )

    # Presence timeout flips the tag back out of range. Drive the check
    # directly: set last_seen beyond the timeout, then invoke the presence
    # check (which in production is scheduled by async_track_time_interval).
    timeout = entry.options.get("presence_timeout", DEFAULT_PRESENCE_TIMEOUT)
    coordinator._last_seen = datetime.now(timezone.utc) - timedelta(
        seconds=timeout + 1
    )
    coordinator._async_check_presence(datetime.now(timezone.utc))
    await hass.async_block_till_done()

    assert coordinator.is_home is False
    assert hass.states.get(presence_id).state == "off"


async def test_setup_with_invalid_key_fails(hass: HomeAssistant, enable_custom_integrations) -> None:
    assert await async_setup_component(hass, "bluetooth", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Bad",
        data={
            "name": "Bad",
            "clock_offset": "not-a-number",
            "curve": CURVE,
            "ephemeral_identity_key": EIK_HEX,
        },
        unique_id=None,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    assert entry.state.value == "setup_error"


async def test_restored_in_range_can_time_out(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """A restored "in range" state must still be re-evaluated by the timeout.

    Regression: restoring ``is_home=True`` with ``last_seen=None`` used to make
    the tag stay "on" forever (the presence check returned early when
    last_seen was None).
    """
    assert await async_setup_component(hass, "bluetooth", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Loshall Tag 1",
        data=_entry_data(),
        unique_id=None,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data

    # Simulate a restored "on" state from a previous session.
    coordinator.restore_is_home(True)
    assert coordinator.is_home is True
    # restore_is_home() stamps last_seen so the timeout can re-evaluate.
    assert coordinator.last_seen is not None

    # Let the timeout elapse with no advertisements.
    timeout = entry.options.get("presence_timeout", DEFAULT_PRESENCE_TIMEOUT)
    coordinator._last_seen = datetime.now(timezone.utc) - timedelta(
        seconds=timeout + 1
    )
    coordinator._async_check_presence(datetime.now(timezone.utc))
    await hass.async_block_till_done()

    assert coordinator.is_home is False


async def test_unavailable_signal_flips_presence_off(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """HA's per-address 'unavailable' signal must flip the tag out of range.

    This is the snappy 'tag left' path: when the tag's address stops
    advertising, HA fires the unavailable callback and we go OFF without
    waiting for the (long) presence timeout.
    """
    assert await async_setup_component(hass, "bluetooth", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Loshall Tag 1",
        data=_entry_data(),
        unique_id=None,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    callback = ble_register_stub[0]["callback"]

    # Bring the tag in range.
    eik = bytes.fromhex(EIK_HEX)
    ad = _current_advertisement(eik)
    service_info = SimpleNamespace(
        service_data={EDDYSTONE_SERVICE_UUID: ad},
        rssi=-55,
        address="AA:BB:CC:00:00:01",
    )
    callback(service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    assert coordinator.is_home is True

    # The unavailable tracker for the address was registered on first sight.
    unavailable_cb = next(
        c["callback"]
        for c in ble_register_stub
        if c.get("unavailable") == "AA:BB:CC:00:00:01"
    )

    # The tag's address stops advertising -> HA fires unavailable.
    unavailable_cb(SimpleNamespace(address="AA:BB:CC:00:00:01"))
    await hass.async_block_till_done()
    assert coordinator.is_home is False

    # A stale unavailable for an old address must be ignored.
    callback(service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    assert coordinator.is_home is True
    unavailable_cb(SimpleNamespace(address="OLD:ADDRESS:00:00:00"))
    await hass.async_block_till_done()
    assert coordinator.is_home is True
