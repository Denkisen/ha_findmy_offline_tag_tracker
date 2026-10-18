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

    The window is sampled at ``ROTATION_PERIOD`` (1024 s) steps (``generate_eid``
    floors its input to the beacon's period boundary, so non-aligned sampling
    is safe) and covers ``MAX_CLOCK_SKEW_SECONDS`` (1 day) *behind and ahead*
    of "now". The ahead margin matters: a beacon's stored clock can run ahead
    of wall time (its EID at wall time ``t`` is the one for period ``P(t)+1``),
    and that drift *grows* as the beacon's clock runs fast. A front pinned to
    a fixed small wall-time offset (the reference tool's 90 periods behind +
    90 ahead) is permanently outrun once the drift passes it — no match ever
    succeeds again and even a restart re-seeds the same wall-anchored window,
    so the tag stays "away" forever while advertising continuously.

    Two mechanisms keep the front ahead of the beacon for good:

    * the wall-anchored front (1 day ahead, symmetric with the skew back
      edge) covers any beacon clock up to a day off wall time at any moment,
      including right after a restart or a long absence;
    * every successful match records the beacon's *current* period and pins
      the front to ``MAX_TRACKED_ROTATIONS`` periods ahead of it, so a
      steadily drifting clock can never run past the front as long as the
      beacon keeps being observed.

    The front is capped at 2 * MAX_CLOCK_SKEW ahead of wall time, so the
    window stays bounded (<= ~3 days of periods).
    """

    def __init__(self, beacon: FHNBeacon, now: datetime) -> None:
        self._beacon = beacon
        self._eids: dict[bytes, datetime] = {}
        self.last_seen: Optional[datetime] = None
        self._front: Optional[datetime] = None
        self._last_matched_period: Optional[datetime] = None
        self._drift_lo: float = float("inf")
        self._drift_hi: float = float("-inf")
        self._generate_eids(now)

    def _generate_eids(self, now: datetime) -> None:
        self._eids.clear()
        self._front = None
        self._last_matched_period = None
        self._drift_lo = float("inf")
        self._drift_hi = float("-inf")
        adv_time = now - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
        end = now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
        while adv_time <= end:
            self._eids[self._beacon.generate_eid(adv_time.timestamp())] = adv_time
            self._front = adv_time
            adv_time += timedelta(seconds=ROTATION_PERIOD)

    @property
    def beacon(self) -> FHNBeacon:
        return self._beacon

    @property
    def window_front(self) -> Optional[datetime]:
        """The adv_time of the frontmost EID entry (diagnostics)."""
        return self._front

    @property
    def window_back(self) -> Optional[datetime]:
        """The adv_time of the oldest EID entry (diagnostics)."""
        if not self._eids:
            return None
        return min(self._eids.values())

    @property
    def window_entries(self) -> int:
        """Number of EIDs currently in the window (diagnostics)."""
        return len(self._eids)

    @property
    def last_matched_period(self) -> Optional[datetime]:
        """Wall time of the window entry matched most recently (diagnostics).

        That entry's period is the beacon's *current* EID period, which is
        what the beacon-clock drift estimate below is derived from.
        """
        return self._last_matched_period

    def estimated_clock_drift(self) -> Optional[tuple[float, float]]:
        """Range (low, high) of seconds the beacon's stored clock is *ahead*
        of wall time, estimated from all successful matches so far.

        Each match resolves the beacon's clock to its current period, which
        pins the drift to a one-period-wide interval; intersecting the
        intervals over matches converges on the true (possibly growing)
        drift. Negative values mean the beacon's clock runs behind wall
        time. ``None`` until the first match.
        """
        if self._drift_hi == float("-inf"):
            return None
        return (self._drift_lo, self._drift_hi)

    def _extend_front(self, now: datetime) -> None:
        """Grow the window's front so it stays ahead of the beacon's clock.

        See the class docstring for why. Adds at most a handful of entries
        per call in steady state (one per 1024 s of wall time); a larger
        one-time catch-up burst can happen on the first call after a long
        absence, but the cap keeps it bounded.
        """
        target = now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
        if self._last_matched_period is not None:
            anchored = self._last_matched_period + timedelta(
                seconds=MAX_TRACKED_ROTATIONS * ROTATION_PERIOD
            )
            if anchored > target:
                target = anchored
        cap = now + timedelta(seconds=2 * MAX_CLOCK_SKEW_SECONDS)
        if target > cap:
            target = cap
        step = timedelta(seconds=ROTATION_PERIOD)
        while self._front is not None and self._front + step <= target:
            self._front += step
            self._eids[self._beacon.generate_eid(self._front.timestamp())] = (
                self._front
            )

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
        # Evict entries that have fallen behind the skew window. Sort by
        # adv_time so we can stop at the first non-expired entry (a plain
        # dict has no ordering guarantee).
        for old_eid, adv_time in sorted(self._eids.items(), key=lambda kv: kv[1]):
            if adv_time >= min_adv_time:
                break
            del self._eids[old_eid]
        self._extend_front(now)
        eid = advertisement[1 : 1 + eid_length]
        adv_time = self._eids.get(eid)
        if adv_time is not None:
            self.last_seen = now
            # The advertised EID is the beacon's *current* period; remember
            # it so _extend_front can pin the front ahead of the beacon's
            # own (possibly drifting) clock.
            self._last_matched_period = adv_time
            self._record_drift(adv_time, now)
            return True
        return False

    def _record_drift(self, adv_time: datetime, now: datetime) -> None:
        """Narrow the beacon-clock drift estimate with a successful match.

        A match at wall time ``now`` against the entry sampled at
        ``adv_time`` means the beacon's current period is the one that entry
        covers, i.e. the beacon's clock is in ``[period, period+1024)`` where
        ``period`` is that entry's period boundary. In terms of the drift
        (beacon clock minus wall time, relative to the stored offset) that
        pins it to a one-period interval; we intersect it with the running
        estimate so it converges. A mismatching interval means the drift
        grew while we watched, so we restart from the fresh interval.
        """
        period = int(adv_time.timestamp() - self._beacon.clock_offset)
        period -= period % ROTATION_PERIOD
        lo = period - now.timestamp() + self._beacon.clock_offset
        hi = lo + ROTATION_PERIOD
        if lo > self._drift_hi or hi < self._drift_lo:
            self._drift_lo, self._drift_hi = lo, hi
        else:
            self._drift_lo = max(self._drift_lo, lo)
            self._drift_hi = min(self._drift_hi, hi)


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
