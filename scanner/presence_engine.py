"""MAC-change-driven presence engine for the raw-BLE Loshall Tag scanner.

The engine decides *when a stopped tag is really gone*. A naive
"no ad for N seconds = away" threshold is wrong for this beacon: at every
1024 s EID rotation the tag changes its MAC and goes silent for a while
(observed ~148 s in a real capture), so a threshold below that gap produces a
false "away" on every rotation even while the tag is in range.

The beacon's stored ``clock_offset`` is NOT reliable for predicting *when*
the next rotation happens: a real capture showed the schedule-computed
boundary to be minutes off (the beacon's internal clock drifts; the FHN spec
absorbs that with the rolling EID window, but a *schedule* can't be trusted).

The only reliable rotation signal is the **MAC change itself**: the tag
changes MAC exactly when it rotates to a new EID, and it keeps advertising
regularly between rotations. So the engine:

* on an ad from a *new* MAC while the tag is **present**, records a rotation
  (``_last_rotation``) and opens the longer ``rotation_threshold`` for
  ``recent_rotation_window`` seconds;
* on an ad (new or known MAC) while the tag is **away**, flips it back to
  present — a fresh ad *is* evidence of presence — but does not reopen the
  rotation window (a beacon that rotated out of range doesn't need one);
* flips "away" when the silence exceeds the threshold in effect.

Resulting detection latency: ~``normal_threshold`` for a departure away from
a rotation, up to ~``rotation_threshold`` for one right after one. No
false "away" on rotations as long as ``normal_threshold`` exceeds the
beacon's worst-case rotation silence (observed 148 s, growing slowly with
beacon clock drift — keep >= 200 s) and ``recent_rotation_window`` >=
``rotation_threshold``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .fhn_codec import TrackedBeacon

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[str, dict], None]  # (event, payload)


@dataclass
class BeaconSpec:
    """One tracked beacon: key material plus display name."""

    name: str
    clock_offset: float
    curve_name: str
    eik_hex: Optional[str] = None
    # Optional: without EIK, any well-formed Eddystone EID ad is accepted.
    tracker: Optional[TrackedBeacon] = field(default=None, repr=False)


@dataclass
class PresenceEngine:
    """Owns the in-range state for one beacon.

    ``tick(now)`` must be called at least every ``check_interval`` seconds;
    it re-evaluates the timeout with the current threshold.

    Thresholds (all seconds):
      * ``normal_threshold``     – max silence allowed outside a
        recent-rotation window. Must exceed the beacon's worst-case rotation
        silence (observed 148 s and slowly growing with clock drift); keep
        >= 200 s.
      * ``rotation_threshold``   – max silence allowed inside the
        recent-rotation window (covers worst-case rotation silence plus
        drift growth for the window duration).
      * ``recent_rotation_window`` – how long after a MAC change the
        rotation threshold applies (keep >= rotation_threshold).
    """

    beacon: BeaconSpec
    normal_threshold: float = 200.0
    rotation_threshold: float = 400.0
    recent_rotation_window: float = 400.0
    check_interval: float = 5.0
    on_change: Optional[Listener] = None

    def __post_init__(self) -> None:
        if self.beacon.eik_hex and not self.beacon.tracker:
            from .fhn_codec import build_tracker

            self.beacon.tracker = build_tracker(
                self.beacon.clock_offset,
                self.beacon.curve_name,
                self.beacon.eik_hex,
                self.beacon.name,
                datetime.now(timezone.utc),
            )

        self._last_seen: Optional[datetime] = None
        self._last_mac: Optional[str] = None
        self._last_rotation: Optional[datetime] = None
        self._rssi: Optional[int] = None
        self._present = False

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #
    @property
    def present(self) -> bool:
        return self._present

    @property
    def last_seen(self) -> Optional[datetime]:
        return self._last_seen

    @property
    def last_mac(self) -> Optional[str]:
        return self._last_mac

    @property
    def rssi(self) -> Optional[int]:
        return self._rssi

    def _payload(self) -> dict:
        return {
            "name": self.beacon.name,
            "present": self._present,
            "last_mac": self._last_mac,
            "rssi": self._rssi,
            "last_seen": self._last_seen.isoformat() if self._last_seen else None,
        }

    def _emit(self, event: str) -> None:
        if self.on_change is not None:
            self.on_change(event, self._payload())

    # ------------------------------------------------------------------ #
    # Threshold
    # ------------------------------------------------------------------ #
    def _absence_threshold(self, now: datetime) -> float:
        """How long the tag may be silent before we call it gone."""
        if (
            self._last_rotation is not None
            and (now - self._last_rotation).total_seconds() < self.recent_rotation_window
        ):
            return self.rotation_threshold
        return self.normal_threshold

    # ------------------------------------------------------------------ #
    # Inputs
    # ------------------------------------------------------------------ #
    def on_advertisement(self, mac: str, rssi: int, payload: bytes, now: datetime) -> None:
        """Process one raw advertisement (the Eddystone service-data bytes)."""
        if self.beacon.tracker is not None:
            if not self.beacon.tracker.match(payload, now):
                return  # not our beacon
        else:
            # No EIK: accept only well-formed FHN Eddystone frames.
            if payload[0] not in (0x40, 0x41):
                return

        mac = mac.upper()
        if mac != self._last_mac:
            if self._last_mac is not None:
                if self._present:
                    # MAC changed while the tag was present: a rotation.
                    # The silence between the last ad and this one is the
                    # beacon's rotation silence — log it to tune thresholds.
                    gap = (
                        (now - self._last_seen).total_seconds()
                        if self._last_seen is not None
                        else 0.0
                    )
                    self._last_rotation = now
                    _LOGGER.info(
                        "%s: MAC changed %s -> %s (EID rotation, silence %.0fs)",
                        self.beacon.name,
                        self._last_mac,
                        mac,
                        gap,
                    )
                elif self._last_seen is not None:
                    elapsed = (now - self._last_seen).total_seconds()
                    if elapsed > self.normal_threshold:
                        _LOGGER.info(
                            "%s: MAC changed %s -> %s after %ss out of range — "
                            "beacon rotated while away; marking present again",
                            self.beacon.name,
                            self._last_mac,
                            mac,
                            int(elapsed),
                        )
                    # Do NOT open a rotation window: the rotation (if any)
                    # happened out of range, and a fresh ad is itself
                    # presence evidence (handled below).

        self._last_seen = now
        self._last_mac = mac
        self._rssi = rssi
        if not self._present:
            self._present = True
            self._last_rotation = None
            _LOGGER.info("%s: in range (MAC %s, RSSI %s)", self.beacon.name, mac, rssi)
            self._emit("presence")

    def tick(self, now: datetime) -> None:
        """Re-evaluate presence; call periodically (<= check_interval)."""
        if not self._present or self._last_seen is None:
            return
        elapsed = (now - self._last_seen).total_seconds()
        threshold = self._absence_threshold(now)
        if elapsed > threshold:
            self._present = False
            _LOGGER.info(
                "%s: out of range (silent for %ss, threshold %ss)",
                self.beacon.name,
                int(elapsed),
                int(threshold),
            )
            self._emit("presence")
