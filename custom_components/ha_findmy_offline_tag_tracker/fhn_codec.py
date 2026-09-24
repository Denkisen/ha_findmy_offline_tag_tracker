"""Find Hub Network (FHN) EID crypto, ported from fhn-beacon-tools.

This module reproduces the Ephemeral Identity (EID) generation and matching
logic from ``fhn_track.py``. It lets us associate an advertisement with a
specific beacon even though the beacon's MAC address randomises and its EID
rotates every ``2**10`` seconds.

The only differences from the reference tool are:
* the EC point math (``r * G``) is done in pure Python (``.ec``) instead of
  ``fastecdsa`` — this keeps the integration free of compiled/Rust
  dependencies, so it only needs ``cryptography`` (shipped by HA core); and
* the AES input is passed as ``bytes`` rather than ``bytearray``.

Everything else (curve parameters, AES-ECB construction of the EID, rotation
period and skew window) is identical to the reference implementation, so it
matches beacons provisioned by ``fhn_provision.py``. The pure-Python EC is
verified to produce byte-identical points to ``fastecdsa``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import MAX_CLOCK_SKEW_SECONDS, MAX_TRACKED_ROTATIONS, ROTATION_PERIOD
from .ec import (
    Curve,
    SECP160R1,
    SECP256R1,
    curve_for_name,
)

__all__ = [
    "SECP160R1",
    "SECP256R1",
    "curve_for_name",
    "FHNBeacon",
    "TrackedBeacon",
    "build_tracker",
]


def _aes256_ecb_encrypt(key: bytes, message: bytes) -> bytes:
    """AES-256-ECB encrypt (single block, no padding needed for 32-byte input)."""
    encryptor = Cipher(algorithms.AES256(key), modes.ECB()).encryptor()
    return encryptor.update(message) + encryptor.finalize()


@dataclass
class FHNBeacon:
    """Static key material for one beacon (from provisioning output)."""

    clock_offset: float
    curve: Curve
    ephemeral_identity_key: bytes
    name: str

    @property
    def eid_length(self) -> int:
        return self.curve.eid_length

    def generate_eid(self, epoch_seconds: float) -> bytes:
        """Compute the EID that is valid for the period containing ``epoch_seconds``."""
        clock_value = int(epoch_seconds - self.clock_offset)
        clock_value -= clock_value % ROTATION_PERIOD
        plaintext = bytearray(32)
        plaintext[0:11] = b"\xff" * 11
        plaintext[11] = 10  # ROTATION_PERIOD_EXPONENT
        plaintext[12:16] = clock_value.to_bytes(4)
        plaintext[16:27] = b"\x00" * 11
        plaintext[27] = 10  # ROTATION_PERIOD_EXPONENT
        plaintext[28:32] = clock_value.to_bytes(4)
        rprime = int.from_bytes(
            _aes256_ecb_encrypt(self.ephemeral_identity_key, bytes(plaintext))
        )
        r = rprime % self.curve.q
        point = self.curve.generator.multiply(r)
        # r == 0 (astronomically unlikely) yields the identity point; the
        # reference would also fail here, so emit zero bytes defensively.
        if point is None:
            return b"\x00" * self.eid_length
        return point.x.to_bytes(self.eid_length)


class TrackedBeacon:
    """Maintains the rolling EID window for one beacon and matches ads to it.

    A faithful port of the ``TrackedFHNBeacon`` logic from ``fhn_track.py``:
    the window is sampled at ``ROTATION_PERIOD`` (1024 s) steps starting from
    ``now - MAX_CLOCK_SKEW``, and grows forward one step whenever the oldest
    sampled time has drifted past the skew window. Because ``generate_eid``
    floors its input to the beacon's period boundary, sampling at arbitrary
    (non-aligned) times is safe and matches the reference exactly.
    """

    def __init__(self, beacon: FHNBeacon, now: datetime) -> None:
        self._beacon = beacon
        self._eids: dict[bytes, datetime] = {}
        self.last_seen: Optional[datetime] = None
        self._generate_eids(now)

    def _generate_eids(self, now: datetime) -> None:
        self._eids.clear()
        adv_time = now - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
        for _ in range(MAX_TRACKED_ROTATIONS):
            self._eids[self._beacon.generate_eid(adv_time.timestamp())] = adv_time
            adv_time += timedelta(seconds=ROTATION_PERIOD)

    @property
    def beacon(self) -> FHNBeacon:
        return self._beacon

    def match(self, advertisement: bytes, now: datetime) -> bool:
        """Return True if ``advertisement`` (the Eddystone service-data payload)
        matches this beacon's current or recent EID.

        ``advertisement`` is the raw service-data value: a 1- or 2-byte
        frame control byte (0x40/0x41) followed by the EID.
        """
        eid_length = self._beacon.eid_length
        if len(advertisement) - eid_length not in (1, 2):
            return False
        if advertisement[0] not in (0x40, 0x41):
            return False
        min_adv_time = now - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
        # Evict stale entries, then extend the window forward one step.
        expired = [eid for eid, adv_time in self._eids.items() if adv_time < min_adv_time]
        for eid in expired:
            del self._eids[eid]
        if expired:
            self._eids[self._beacon.generate_eid(now.timestamp())] = now
        eid = advertisement[1 : 1 + eid_length]
        if eid in self._eids:
            self.last_seen = now
            return True
        return False


def build_tracker(
    clock_offset: float,
    curve_name: str,
    ephemeral_identity_key_hex: str,
    name: str,
    now: datetime,
) -> TrackedBeacon:
    """Build a :class:`TrackedBeacon` from config-entry (hex) key material.

    Raises ``ValueError`` on malformed hex and ``KeyError`` on an unknown
    curve name — both surfaced by the caller.
    """
    curve = curve_for_name(curve_name)
    ephemeral_identity_key = bytes.fromhex(ephemeral_identity_key_hex)
    beacon = FHNBeacon(
        clock_offset=clock_offset,
        curve=curve,
        ephemeral_identity_key=ephemeral_identity_key,
        name=name,
    )
    return TrackedBeacon(beacon, now)
