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

    # The per-address advertisement callback is registered through the
    # bluetooth manager, which we stub via _get_manager (no BlueZ in tests).
    # Registrations land in the same `calls` list, tagged "advertisement".
    class _FakeManager:
        def async_register_advertisement_callback(self, callback, address, *a, **k):
            calls.append({"advertisement": address, "callback": callback})

            def _unsub():
                calls[-1]["unsubscribed"] = True

            return _unsub

        def async_current_scanners(self):
            return []

    def _fake_get_manager(hass):
        return _FakeManager()

    with (
        patch(
            "custom_components.ha_findmy_offline_tag_tracker.coordinator.async_register_callback",
            _fake_register,
        ),
        patch(
            "custom_components.ha_findmy_offline_tag_tracker.coordinator.async_track_unavailable",
            _fake_track_unavailable,
        ),
        patch(
            "custom_components.ha_findmy_offline_tag_tracker.coordinator._get_manager",
            _fake_get_manager,
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
    uuid_regs = [c for c in ble_register_stub if "match_dict" in c]
    assert len(uuid_regs) == 1
    assert uuid_regs[0]["match_dict"]["service_data_uuid"] == EDDYSTONE_SERVICE_UUID
    callback = uuid_regs[0]["callback"]

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
    callback = next(c["callback"] for c in ble_register_stub if "match_dict" in c)

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


async def test_per_address_advertisement_keeps_presence_fresh(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """The per-address ad callback fires on every ad and keeps the tag in range.

    This is the fast-detection path: even when the Eddystone payload is
    unchanged (so the service-UUID callback is dedup'd), a per-address ad from
    the tracked MAC refreshes last_seen and keeps presence ON. A stale ad from
    an old MAC is ignored.
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
    uuid_cb = next(c["callback"] for c in ble_register_stub if "match_dict" in c)

    # First sight via the service-UUID callback (EID match) registers a
    # per-address advertisement tracker for the tag's MAC.
    address = "AA:BB:CC:DD:EE:FF"
    eik = bytes.fromhex(EIK_HEX)
    ad = _current_advertisement(eik)
    service_info = SimpleNamespace(
        service_data={EDDYSTONE_SERVICE_UUID: ad}, rssi=-50, address=address
    )
    uuid_cb(service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()

    ad_regs = [c for c in ble_register_stub if "advertisement" in c]
    assert any(c["advertisement"] == address for c in ad_regs), ad_regs
    ad_cb = next(c["callback"] for c in ad_regs if c["advertisement"] == address)

    # The tag is in range.
    assert coordinator.is_home is True
    seen_before = coordinator.last_seen
    assert seen_before is not None

    # A per-address ad with an UNCHANGED payload still refreshes last_seen and
    # keeps presence ON (the service-UUID callback would not have fired again).
    coordinator._last_seen = datetime.now(timezone.utc) - timedelta(seconds=300)
    ad_cb(SimpleNamespace(service_data={EDDYSTONE_SERVICE_UUID: ad}, rssi=-48, address=address))
    await hass.async_block_till_done()
    assert coordinator.is_home is True
    assert coordinator.last_seen > seen_before
    assert coordinator.rssi == -48

    # A per-address ad from an OLD (already-replaced) MAC is ignored: it must
    # not refresh presence or re-point the tracker.
    old_addr = "11:22:33:44:55:66"
    before = coordinator.last_seen
    coordinator._async_handle_per_address_advertisement(
        SimpleNamespace(service_data={}, rssi=-90, address=old_addr)
    )
    await hass.async_block_till_done()
    assert coordinator.address == address
    assert coordinator.last_seen == before


async def test_mac_rotation_repoints_advertisement_tracker(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """When the tag rotates to a new MAC, the per-address tracker re-points."""
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
    uuid_cb = next(c["callback"] for c in ble_register_stub if "match_dict" in c)
    eik = bytes.fromhex(EIK_HEX)
    ad = _current_advertisement(eik)

    mac1 = "AA:BB:CC:00:00:01"
    uuid_cb(
        SimpleNamespace(service_data={EDDYSTONE_SERVICE_UUID: ad}, rssi=-50, address=mac1),
        bluetooth.BluetoothChange.ADVERTISEMENT,
    )
    await hass.async_block_till_done()
    assert coordinator.address == mac1

    # Rotation: same EID, new MAC. The EID match still succeeds, so the tag is
    # tracked under the new address and the per-address tracker re-points.
    mac2 = "AA:BB:CC:00:00:02"
    uuid_cb(
        SimpleNamespace(service_data={EDDYSTONE_SERVICE_UUID: ad}, rssi=-52, address=mac2),
        bluetooth.BluetoothChange.ADVERTISEMENT,
    )
    await hass.async_block_till_done()
    assert coordinator.address == mac2
    ad_regs = [c for c in ble_register_stub if "advertisement" in c]
    assert any(c["advertisement"] == mac2 for c in ad_regs), ad_regs
    assert coordinator.is_home is True


async def test_poll_keeps_presence_fresh_and_relearns_mac(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """The 30 s discovery poll keeps the tag in range even without callbacks.

    Regression for the production bug: the per-address push callback was not
    delivered for 10+ minutes while the tag advertised, so the 600 s backstop
    flipped the tag "Away". The poll re-scans the manager's discovered-devices
    cache, EID-matches the cached Eddystone payload, and refreshes presence —
    and also re-learns a rotated MAC that no callback reported.
    """
    assert await async_setup_component(hass, "bluetooth", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Loshall Tag 1",
        data=_entry_data(),
        unique_id=None,
    )
    entry.add_to_hass(hass)

    # Replace the manager stub with one exposing a scanner whose discovered
    # cache holds a fresh Eddystone advertisement for a (new) MAC.
    discovered: dict[str, tuple[object, object]] = {}

    class _PollingScanner:
        @property
        def discovered_devices_and_advertisement_data(self):
            return discovered

        @property
        def discovered_device_timestamps(self):
            # Fresh stamps by default; tests override per-address as needed.
            from bluetooth_data_tools import monotonic_time_coarse

            now = monotonic_time_coarse()
            return {addr: now for addr in discovered}

    class _PollingManager:
        def async_register_advertisement_callback(self, callback, address, *a, **k):
            ble_register_stub.append({"advertisement": address, "callback": callback})

            def _unsub():
                pass

            return _unsub

        def async_current_scanners(self):
            return [_PollingScanner()]

    with patch(
        "custom_components.ha_findmy_offline_tag_tracker.coordinator._get_manager",
        lambda hass: _PollingManager(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert coordinator.is_home is False  # nothing seen yet

        # No callbacks fired at all — only the poll sees the tag.
        address = "77:88:99:AA:BB:CC"
        eik = bytes.fromhex(EIK_HEX)
        ad = _current_advertisement(eik)
        discovered[address] = (
            SimpleNamespace(address=address, name=None),
            SimpleNamespace(
                service_data={EDDYSTONE_SERVICE_UUID: ad},
                rssi=-54,
            ),
        )

        coordinator._async_poll_discovered(datetime.now(timezone.utc))
        await hass.async_block_till_done()

        assert coordinator.is_home is True
        assert coordinator.address == address
        assert coordinator.rssi == -54
        assert coordinator.last_seen is not None
        # Per-address trackers re-pointed at the polled address.
        ad_regs = [c for c in ble_register_stub if "advertisement" in c]
        assert any(c["advertisement"] == address for c in ad_regs), ad_regs

        # A second poll with the same cached payload keeps presence fresh
        # (last_seen advances; no false "away").
        before = coordinator.last_seen
        coordinator._last_seen = datetime.now(timezone.utc) - timedelta(seconds=300)
        coordinator._async_poll_discovered(datetime.now(timezone.utc))
        await hass.async_block_till_done()
        assert coordinator.is_home is True
        assert coordinator.last_seen > before

        # A non-matching Eddystone payload from another device is ignored.
        discovered["11:22:33:44:55:66"] = (
            SimpleNamespace(address="11:22:33:44:55:66", name=None),
            SimpleNamespace(
                service_data={EDDYSTONE_SERVICE_UUID: b"\x40" + b"\x00" * 20},
                rssi=-40,
            ),
        )
        before_addr = coordinator.address
        coordinator._async_poll_discovered(datetime.now(timezone.utc))
        await hass.async_block_till_done()
        assert coordinator.address == before_addr
        assert coordinator.rssi == -54  # not overwritten by the other device

        # -127 RSSI (BlueZ placeholder for paired-but-absent) is ignored.
        discovered[address] = (
            SimpleNamespace(address=address, name=None),
            SimpleNamespace(service_data={EDDYSTONE_SERVICE_UUID: ad}, rssi=-127),
        )
        before_seen = coordinator.last_seen
        coordinator._async_poll_discovered(datetime.now(timezone.utc))
        await hass.async_block_till_done()
        assert coordinator.last_seen == before_seen

        # A stale cached ad (older than the presence timeout) must NOT keep
        # presence alive — otherwise a pre-boot entry would falsely re-arm.
        from bluetooth_data_tools import monotonic_time_coarse

        class _StaleScanner(_PollingScanner):
            @property
            def discovered_device_timestamps(self):
                return {address: monotonic_time_coarse() - 3600}

        with patch(
            "custom_components.ha_findmy_offline_tag_tracker.coordinator."
            "_get_manager",
            lambda hass: SimpleNamespace(
                async_register_advertisement_callback=(
                    lambda cb, addr, *a, **k: (lambda: None)
                ),
                async_current_scanners=lambda: [_StaleScanner()],
            ),
        ):
            coordinator._is_home = False
            coordinator._last_seen = None
            coordinator._async_poll_discovered(datetime.now(timezone.utc))
            await hass.async_block_till_done()
            assert coordinator.is_home is False
            assert coordinator.last_seen is None


async def test_older_habluetooth_without_advertisement_api(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """Older habluetooth (as shipped with the user's HA build) must not crash.

    Regression: the production box's habluetooth lacks
    ``async_register_advertisement_callback`` (AttributeError in the ad
    callback) and its scanners lack ``discovered_device_timestamps``. The
    coordinator must degrade to the discovery poll alone and keep the tag in
    range.
    """
    assert await async_setup_component(hass, "bluetooth", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Loshall Tag 1",
        data=_entry_data(),
        unique_id=None,
    )
    entry.add_to_hass(hass)

    discovered: dict[str, tuple[object, object]] = {}

    class _OldScanner:
        # Only the API the older habluetooth exposes: no timestamps property.
        @property
        def discovered_devices_and_advertisement_data(self):
            return discovered

    class _OldManager:
        # async_register_advertisement_callback intentionally ABSENT.
        def async_current_scanners(self):
            return [_OldScanner()]

    with patch(
        "custom_components.ha_findmy_offline_tag_tracker.coordinator._get_manager",
        lambda hass: _OldManager(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert coordinator.is_home is False

        # Drive the Eddystone service-UUID callback (as HA replays/delivers
        # ads). This is the exact path that crashed in production:
        # _async_mark_seen -> _async_track_address_advertisements ->
        # manager.async_register_advertisement_callback (absent on older
        # habluetooth). It must now degrade to the poll instead of raising.
        address = "77:88:99:AA:BB:CC"
        ad = _current_advertisement(bytes.fromhex(EIK_HEX))
        uuid_regs = [c for c in ble_register_stub if "match_dict" in c]
        uuid_regs[0]["callback"](
            SimpleNamespace(
                service_data={EDDYSTONE_SERVICE_UUID: ad},
                rssi=-54,
                address=address,
            ),
            bluetooth.BluetoothChange.ADVERTISEMENT,
        )
        await hass.async_block_till_done()
        # No AttributeError; presence flipped on.
        assert coordinator.is_home is True
        assert coordinator.address == address
        assert coordinator.rssi == -54

        # The poll (without discovered_device_timestamps) keeps it alive.
        discovered[address] = (
            SimpleNamespace(address=address, name=None),
            SimpleNamespace(service_data={EDDYSTONE_SERVICE_UUID: ad}, rssi=-54),
        )
        coordinator._last_seen = datetime.now(timezone.utc) - timedelta(seconds=300)
        coordinator._async_poll_discovered(datetime.now(timezone.utc))
        await hass.async_block_till_done()
        assert coordinator.is_home is True
        assert coordinator.last_seen is not None


async def test_two_tags_are_tracked_independently(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """Multiple tags = multiple config entries; each is matched by EID.

    Both tags advertise under the same Eddystone UUID, so the single shared
    service-UUID callback must route each advertisement to exactly the entry
    whose rolling EID window matches.
    """
    from ha_findmy_offline_tag_tracker import fhn_codec

    assert await async_setup_component(hass, "bluetooth", {})

    eik_a = bytes.fromhex(EIK_HEX)
    eik_b = bytes.fromhex("11" * 32)  # different 32-byte AES key (secp160r1)

    def _entry(name: str, clock: str, eik_hex: str) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            title=name,
            data={
                "name": name,
                "clock_offset": clock,
                "curve": CURVE,
                "ephemeral_identity_key": eik_hex,
            },
            unique_id=None,
        )

    entry_a = _entry("Tag A", CLOCK_OFFSET, EIK_HEX)
    entry_a.add_to_hass(hass)
    entry_b = _entry("Tag B", "17000001.5", eik_b.hex())
    entry_b.add_to_hass(hass)

    # Both entries are added before setup: HA loads every entry of a domain
    # when the first one is set up.
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    all_states = list(hass.states.async_entity_ids())
    presence = sorted(e for e in all_states if e.startswith("binary_sensor."))
    trackers = sorted(e for e in all_states if e.startswith("device_tracker."))
    assert len(presence) == 2, all_states
    assert len(trackers) == 2, all_states
    rssi = sorted(e for e in all_states if e.startswith("sensor.") and "rssi" in e)
    assert len(rssi) == 2, all_states

    # Each entry registers its own service-UUID callback; every ad is
    # delivered to both, and each coordinator matches only its own EID.
    uuid_regs = [c for c in ble_register_stub if "match_dict" in c]
    assert len(uuid_regs) == 2
    callbacks = [c["callback"] for c in uuid_regs]

    def deliver(service_info) -> None:
        for cb in callbacks:
            cb(service_info, bluetooth.BluetoothChange.ADVERTISEMENT)

    # Tag B advertises (distinct MAC). Only Tag B's entities flip.
    beacon_b = fhn_codec.FHNBeacon(
        float("17000001.5"), fhn_codec.SECP160R1, eik_b, "Tag B"
    )
    ad_b = b"\x40" + beacon_b.generate_eid(datetime.now(timezone.utc).timestamp())
    deliver(
        SimpleNamespace(
            service_data={EDDYSTONE_SERVICE_UUID: ad_b},
            rssi=-50,
            address="AA:00:00:00:00:02",
        )
    )
    await hass.async_block_till_done()

    assert entry_a.runtime_data.is_home is False
    assert entry_b.runtime_data.is_home is True
    assert entry_b.runtime_data.address == "AA:00:00:00:00:02"
    # Per-entity states follow their own coordinator.
    on_entities = [e for e in presence if hass.states.get(e).state == "on"]
    assert len(on_entities) == 1, [
        (e, hass.states.get(e).state) for e in presence
    ]
    on_trackers = [e for e in trackers if hass.states.get(e).state == "home"]
    assert len(on_trackers) == 1

    # Tag A advertising now: its entities flip, Tag B's stay on.
    ad_a = _current_advertisement(eik_a)
    deliver(
        SimpleNamespace(
            service_data={EDDYSTONE_SERVICE_UUID: ad_a},
            rssi=-70,
            address="BB:00:00:00:00:01",
        )
    )
    await hass.async_block_till_done()

    assert entry_a.runtime_data.is_home is True
    assert entry_b.runtime_data.is_home is True
    on_entities = [e for e in presence if hass.states.get(e).state == "on"]
    assert len(on_entities) == 2
    on_trackers = [e for e in trackers if hass.states.get(e).state == "home"]
    assert len(on_trackers) == 2

    # Tag A leaving (timeout) does not affect Tag B.
    timeout = entry_a.options.get("presence_timeout", DEFAULT_PRESENCE_TIMEOUT)
    entry_a.runtime_data._last_seen = datetime.now(timezone.utc) - timedelta(
        seconds=timeout + 1
    )
    entry_a.runtime_data._async_check_presence(datetime.now(timezone.utc))
    await hass.async_block_till_done()

    assert entry_a.runtime_data.is_home is False
    assert entry_b.runtime_data.is_home is True
    on_trackers = [e for e in trackers if hass.states.get(e).state == "home"]
    assert len(on_trackers) == 1


async def test_device_tracker_entity_reflects_presence(
    hass: HomeAssistant, enable_custom_integrations, ble_register_stub
) -> None:
    """The device_tracker platform makes the tag assignable to a person.

    In range maps to the tracker's associated zone (Home by default); out of
    range maps to not_home. The person component lists entities in the
    device_tracker domain, so this is what enables "assign the tag to me".
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

    trackers = [e for e in hass.states.async_entity_ids() if e.startswith("device_tracker.")]
    assert len(trackers) == 1, list(hass.states.async_entity_ids())
    tracker_id = trackers[0]

    # Out of range -> not_home, with the BLE source type.
    state = hass.states.get(tracker_id)
    assert state.state == "not_home"
    assert state.attributes["source_type"] == "bluetooth_le"

    # An advertisement flips the tracker to the associated zone (Home).
    uuid_regs = [c for c in ble_register_stub if "match_dict" in c]
    eik = bytes.fromhex(EIK_HEX)
    service_info = SimpleNamespace(
        service_data={EDDYSTONE_SERVICE_UUID: _current_advertisement(eik)},
        rssi=-57,
        address="AA:BB:CC:00:00:01",
    )
    uuid_regs[0]["callback"](service_info, bluetooth.BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()

    assert hass.states.get(tracker_id).state == "home"

    # Presence timeout flips it back to not_home.
    coordinator = entry.runtime_data
    timeout = entry.options.get("presence_timeout", DEFAULT_PRESENCE_TIMEOUT)
    coordinator._last_seen = datetime.now(timezone.utc) - timedelta(seconds=timeout + 1)
    coordinator._async_check_presence(datetime.now(timezone.utc))
    await hass.async_block_till_done()

    assert hass.states.get(tracker_id).state == "not_home"
