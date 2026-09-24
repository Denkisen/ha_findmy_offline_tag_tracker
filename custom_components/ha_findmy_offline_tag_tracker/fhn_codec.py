"""Google FHN EID decryption codec - ported from fhn-beacon-tools."""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import OrderedDict

try:
    from fastecdsa.curve import Curve
    from fastecdsa.keys import get_public_key
except ImportError:
    raise RuntimeError("fastecdsa is required. Install with: pip install fastecdsa")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from bleak import AdvertisementData, BLEDevice

_LOGGER = logging.getLogger(__name__)

# Constants from fhn_track.py
EDDYSTONE_SERVICE_UUID = "0000feaa-0000-1000-8000-00805f9b34fb"
ROTATION_PERIOD_EXPONENT = 10
ROTATION_PERIOD = 1 << ROTATION_PERIOD_EXPONENT  # 1024 seconds
ROTATION_PERIOD_DELTA = timedelta(seconds=ROTATION_PERIOD)

# secp160r1 curve definition (from source)
SECP160R1 = Curve(
    name="secp160r1",
    p=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_7FFFFFFF,
    a=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_7FFFFFFC,
    b=0x1C97BEFC_54BD7A8B_65ACF89F_81D4D4AD_C565FA45,
    q=0x01_00000000_00000000_0001F4C8_F927AED3_CA752257,
    gx=0x4A96B568_8EF57328_46646989_68C38BB9_13CBFC82,
    gy=0x23A62855_3168947D_59DCC912_04235137_7AC5FB32,
    oid=bytes.fromhex("2b81040008"),
)

# Matching parameters
MAX_CLOCK_SKEW = timedelta(days=1)
MAX_TRACKED_ROTATIONS = 90

# EID frame headers
FRAME_HEADER_VALID = (0x40, 0x41)


@dataclass
class FHNBeacon:
    """Represents a provisioned FHN beacon."""
    clock_offset: float
    curve: Curve
    ephemeral_identity_key: bytes
    name: str
    model_id: str
    original_mac: str

    def _aes256_encrypt(self, key: bytes, message: bytes) -> bytes:
        """Encrypt with AES-256-ECB."""
        encryptor = Cipher(algorithms.AES256(key), modes.ECB()).encryptor()
        return encryptor.update(message) + encryptor.finalize()

    @property
    def eid_length(self) -> int:
        """Return EID length for this curve."""
        return {
            SECP160R1: 20,
        }[self.curve]

    def generate_eid(self, when: datetime) -> bytes:
        """Generate EID for a specific timestamp."""
        clock_value = int(when.timestamp() - self.clock_offset)
        clock_value -= clock_value % ROTATION_PERIOD

        # Build plaintext buffer
        plaintext = bytearray(32)
        plaintext[0:11] = b"\xff" * 11
        plaintext[11] = ROTATION_PERIOD_EXPONENT
        plaintext[12:16] = clock_value.to_bytes(4, 'big')
        plaintext[16:27] = b"\x00" * 11
        plaintext[27] = ROTATION_PERIOD_EXPONENT
        plaintext[28:32] = clock_value.to_bytes(4, 'big')

        # Encrypt and derive point
        rprime = int.from_bytes(
            self._aes256_encrypt(self.ephemeral_identity_key, bytes(plaintext))
        )
        r = rprime % self.curve.q
        R = get_public_key(r, self.curve)

        return R.x.to_bytes(self.eid_length, 'big')


class TrackedFHNBeacon:
    """Tracks a single beacon with EID rotation handling."""

    beacon: FHNBeacon
    device: Optional[BLEDevice]
    eids: OrderedDict[bytes, datetime]
    last_seen: Optional[datetime]
    last_rssi: Optional[int]
    last_clock_skew_ticks: Optional[int]

    def __init__(self, beacon: FHNBeacon, now: datetime):
        self.beacon = beacon
        self.device = None
        self.eids = OrderedDict()
        self.last_seen = None
        self.last_rssi = None
        self.last_clock_skew_ticks = None
        self._generate_eids(now)

    def _generate_eids(self, now: datetime):
        """Generate EIDs for time window covering potential clock drift."""
        self.eids.clear()
        adv_time = now - MAX_CLOCK_SKEW

        for _ in range(MAX_TRACKED_ROTATIONS):
            self.eids[self.beacon.generate_eid(adv_time)] = adv_time
            adv_time += ROTATION_PERIOD_DELTA

    def _cleanup_old_eids(self, now: datetime):
        """Remove EIDs outside the valid window and generate new ones."""
        min_adv_time = now - MAX_CLOCK_SKEW
        adv_time_base = None

        while self.eids:
            oldest_eid, oldest_time = next(iter(self.eids.items()))
            if oldest_time >= min_adv_time:
                break

            del self.eids[oldest_eid]

            if adv_time_base is None:
                adv_time_base = oldest_time + timedelta(
                    seconds=MAX_TRACKED_ROTATIONS * ROTATION_PERIOD
                )

        if adv_time_base is not None:
            for i in range(MAX_TRACKED_ROTATIONS):
                adv_time = adv_time_base + timedelta(
                    seconds=i * ROTATION_PERIOD
                )
                self.eids[self.beacon.generate_eid(adv_time)] = adv_time

    def match(self, device: BLEDevice, advertisement: bytes, now: datetime) -> bool:
        """Try to match an advertisement to this beacon."""
        if len(advertisement) - self.beacon.eid_length not in (1, 2):
            return False

        if advertisement[0] not in FRAME_HEADER_VALID:
            return False

        eid = advertisement[1 : 1 + self.beacon.eid_length]

        self._cleanup_old_eids(now)

        try:
            adv_time = self.eids[eid]
        except KeyError:
            return False

        expected_clock = int((now - self.beacon.clock_offset).timestamp())
        expected_clock -= expected_clock % ROTATION_PERIOD
        observed_clock = int((adv_time - self.beacon.clock_offset).timestamp())
        observed_clock -= observed_clock % ROTATION_PERIOD

        skew_ticks = (observed_clock - expected_clock) // ROTATION_PERIOD

        self.device = device
        self.last_seen = now
        self.last_rssi = None
        self.last_clock_skew_ticks = skew_ticks

        _LOGGER.debug(
            f"EID matched for {self.beacon.name}: skew={skew_ticks} ticks"
        )

        return True


class FHNTracker:
    """Main tracker class managing multiple beacons."""

    def __init__(self, beacons: List[FHNBeacon]):
        now = datetime.now()
        self.tracked_beacons = [
            TrackedFHNBeacon(beacon, now) for beacon in beacons
        ]

    def process_advertisement(
        self,
        device: BLEDevice,
        advertisement_data: AdvertisementData
    ) -> Optional[TrackedFHNBeacon]:
        """Process a Bluetooth advertisement and return matching beacon."""
        service_data = getattr(advertisement_data, 'service_data', {})
        eddystone_data = service_data.get(EDDYSTONE_SERVICE_UUID)

        if eddystone_data is None:
            return None

        now = datetime.now()

        for tracked in self.tracked_beacons:
            if tracked.match(device, eddystone_data, now):
                tracked.last_rssi = advertisement_data.rssi
                return tracked

        return None

    def get_all_states(self) -> Dict[str, dict]:
        """Get current state of all tracked beacons."""
        states = {}
        for tracked in self.tracked_beacons:
            states[tracked.beacon.model_id] = {
                'is_present': tracked.last_seen is not None and
                             (datetime.now() - tracked.last_seen).total_seconds() < 120,
                'last_rssi': tracked.last_rssi,
                'last_seen': tracked.last_seen,
                'last_clock_skew_ticks': tracked.last_clock_skew_ticks,
            }
        return states


def create_fhn_beacon(config_data: dict) -> FHNBeacon:
    """Create FHNBeacon from config entry data."""
    return FHNBeacon(
        clock_offset=float(config_data["clock_offset"]),
        curve=SECP160R1,
        ephemeral_identity_key=bytes.fromhex(
            config_data["ephemeral_identity_key"]
        ),
        name=f"Tag ({config_data['model_id'][-6:]})",
        model_id=config_data["model_id"],
        original_mac=config_data.get("original_mac", ""),
    )