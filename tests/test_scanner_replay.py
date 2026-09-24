"""Replay the user's real fhn_track.py capture through the scanner engine.

This is the ground-truth validation of the MAC-change-driven design:

* the two EIDs captured in the log must be *generated* by the beacon's key
  material (codec correctness against real-world data);
* the beacon's stored clock_offset is shown to be too drifted to predict
  rotation timing from a schedule (proving the MAC-change approach is
  necessary);
* the engine must stay "present" across the 148 s rotation silence (no false
  away); and
* after the capture ends (tag gone), the engine must flip "away" per the
  threshold in effect.

The beacon's key material is read at runtime from the user's local
``fhn-beacon-tools/fhn_track.py`` so no key material is ever committed to
this repo. The test skips when that file is absent.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scanner.fhn_const import ROTATION_PERIOD
from scanner.fhn_codec import build_tracker
from scanner.presence_engine import BeaconSpec, PresenceEngine

FHN_TRACK = Path("/home/marko/Projects/fhn-beacon-tools/fhn_track.py")

# The user's real capture (fhn_track.py, 2026-09-24, local time CEST/UTC+2).
CAPTURE = """
2026-09-24 17:16:37.000919 AA:BB:CC:00:00:02 -50 40bc056b6bade956e01b4dc734dcd6feebb329cbc4a0
2026-09-24 17:17:18.932617 AA:BB:CC:00:00:02 -59 40bc056b6bade956e01b4dc734dcd6feebb329cbc4a0
2026-09-24 17:17:36.855233 AA:BB:CC:00:00:02 -49 40bc056b6bade956e01b4dc734dcd6feebb329cbc4a0
2026-09-24 17:17:52.903565 AA:BB:CC:00:00:02 -57 40bc056b6bade956e01b4dc734dcd6feebb329cbc4a0
2026-09-24 17:20:20.647393 AA:BB:CC:00:00:03 -58 40fbdef5eed52e465b76f2ad0d324d6b006404e8660a
2026-09-24 17:21:16.688049 AA:BB:CC:00:00:03 -57 40fbdef5eed52e465b76f2ad0d324d6b006404e8660a
2026-09-24 17:22:16.687008 AA:BB:CC:00:00:03 -61 40fbdef5eed52e465b76f2ad0d324d6b006404e8660a
2026-09-24 17:22:50.700650 AA:BB:CC:00:00:03 -56 40fbdef5eed52e465b76f2ad0d324d6b006404e8660a
2026-09-24 17:23:46.736479 AA:BB:CC:00:00:03 -57 40fbdef5eed52e465b76f2ad0d324d6b006404e8660a
2026-09-24 17:25:18.824115 AA:BB:CC:00:00:03 -59 40fbdef5eed52e465b76f2ad0d324d6b006404e8660a
"""

pytestmark = pytest.mark.skipif(not FHN_TRACK.exists(), reason="fhn-track capture not available")


def _load_beacon() -> tuple[float, str, str]:
    """Read (clock_offset, curve, eik_hex) from the user's fhn_track.py.

    The EIK is the ``bytes.fromhex`` inside the KNOWN_FHN_BEACONS block (the
    *last* fromhex in the file — the earlier ones are curve OIDs).
    """
    text = FHN_TRACK.read_text()
    m_offset = re.search(r"FHNBeacon\(\s*([0-9.]+),", text)
    m_curve = re.search(r"(SECP160R1|SECP256R1),", text)
    assert m_offset and m_curve, "fhn_track.py layout changed"
    # The EIK is the longest fromhex in the file (curve OIDs are 10-16 chars).
    m_eik = max(
        re.finditer(r'bytes\.fromhex\(\s*"([0-9a-fA-F]{64,})"', text),
        key=lambda m: len(m.group(1)),
        default=None,
    )
    assert m_eik, "EIK not found in fhn_track.py"
    curve = "secp160r1" if m_curve.group(1) == "SECP160R1" else "secp256r1"
    return float(m_offset.group(1)), curve, m_eik.group(1)


def _parse_capture() -> list[tuple[datetime, str, int, bytes]]:
    rows = []
    for line in CAPTURE.strip().splitlines():
        date_s, time_s, mac, rssi, payload = line.split()
        # Capture was taken in CEST (UTC+2).
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=timezone(timedelta(hours=2))
        )
        rows.append((dt.astimezone(timezone.utc), mac, int(rssi), bytes.fromhex(payload)))
    return rows


@pytest.fixture
def beacon_material() -> tuple[float, str, str]:
    return _load_beacon()


def test_captured_eids_are_generated_by_key_material(beacon_material):
    """The codec must reproduce the *real* EIDs seen on the air."""
    offset, curve, eik = beacon_material
    tracker = build_tracker(offset, curve, eik, "first", datetime.now(timezone.utc))
    rows = _parse_capture()
    for dt, _mac, _rssi, payload in rows:
        assert tracker.match(payload, dt), f"EID {payload.hex()} at {dt} should match"


def test_beacon_clock_too_drifted_for_schedule_based_rotation(beacon_material):
    """A schedule computed from clock_offset misses the real rotation.

    The capture's MAC change (17:17:52 -> 17:20:20) is the real rotation. If
    the schedule were reliable, the next rotation boundary after the last
    old-MAC ad would sit within the observed 148 s gap. It does not — the
    beacon's internal clock has drifted minutes from the stored offset, so
    rotation timing can only be *observed* (MAC change), not predicted.
    """
    offset, _curve, _eik = beacon_material
    rows = _parse_capture()
    old_last = rows[3][0]
    new_first = rows[4][0]
    gap = (new_first - old_last).total_seconds()
    t = old_last.timestamp()
    clock_value = (t - offset) // ROTATION_PERIOD
    boundary = offset + (clock_value + 1) * ROTATION_PERIOD
    print(f"real gap={gap:.0f}s  schedule boundary in {boundary - t:.0f}s")
    # The real rotation happens ~148s after old_last; the schedule says the
    # boundary is >300s away — far outside the observed gap.
    assert 100 < gap < 200
    assert boundary - t > 300


def test_engine_stays_present_across_rotation_silence(beacon_material):
    """No false 'away' during the 148 s rotation silence in the real capture."""
    offset, curve, eik = beacon_material
    engine = PresenceEngine(
        BeaconSpec("first", offset, curve, eik), check_interval=1.0
    )
    events: list[tuple[datetime, bool]] = []
    engine.on_change = lambda _e, p: events.append((datetime.now(timezone.utc), p["present"]))

    rows = _parse_capture()
    # Replay second-by-second, feeding ads at their timestamps.
    start = rows[0][0]
    end = rows[-1][0]
    next_i = 0
    t = start
    max_silence = 0.0
    last_ad_t: datetime | None = None
    while t <= end:
        while next_i < len(rows) and rows[next_i][0] <= t:
            _dt, mac, rssi, payload = rows[next_i]
            engine.on_advertisement(mac, rssi, payload, t)
            last_ad_t = t
            next_i += 1
        engine.tick(t)
        assert engine.present, f"false 'away' at {t}"
        max_silence = max(max_silence, (t - (last_ad_t or start)).total_seconds())
        t += timedelta(seconds=1)
    print(f"max silence in capture: {max_silence:.0f}s (rotation gap)")
    assert max_silence > 100  # sanity: the capture really contains a long gap


def test_engine_flips_away_after_capture_ends(beacon_material):
    """After the tag stops advertising, 'away' comes after the normal threshold."""
    offset, curve, eik = beacon_material
    engine = PresenceEngine(
        BeaconSpec("first", offset, curve, eik), check_interval=1.0
    )
    rows = _parse_capture()
    last_t = rows[-1][0]
    for dt, mac, rssi, payload in rows:
        engine.on_advertisement(mac, rssi, payload, dt)
    assert engine.present

    # Walk forward in 1 s steps; record when presence flips.
    t = last_t
    away_after: float | None = None
    for _ in range(400):
        t += timedelta(seconds=1)
        engine.tick(t)
        if not engine.present and away_after is None:
            away_after = (t - last_t).total_seconds()
            break
    assert away_after is not None, "engine never went away"
    threshold = engine._absence_threshold(t)
    print(f"away after {away_after:.0f}s (threshold at flip: {threshold:.0f}s)")
    # Must respect the threshold in effect at the flip moment.
    assert away_after <= threshold + 2
    # And never faster than the normal threshold (no false early away).
    assert away_after >= engine.normal_threshold


def test_engine_returns_when_new_mac_appears_while_away(beacon_material):
    """An ad from a *new* MAC while away means the tag is present again.

    The beacon rotates MAC out of range; when the tag comes back (possibly
    under yet another MAC), the first ad must flip presence back on.
    """
    offset, curve, eik = beacon_material
    engine = PresenceEngine(BeaconSpec("first", offset, curve, eik))
    rows = _parse_capture()
    last_t = rows[-1][0]
    for dt, mac, rssi, payload in rows:
        engine.on_advertisement(mac, rssi, payload, dt)
    assert engine.present

    # Let it go away.
    t = last_t
    for _ in range(300):
        t += timedelta(seconds=1)
        engine.tick(t)
    assert not engine.present

    # Tag returns, a new MAC, ~10 min later.
    t += timedelta(seconds=600)
    mac_new = "AA:BB:CC:DD:EE:FF"
    # Use the same (still-valid) EID payload — the engine's tracker accepts
    # the current window, and a real beacon would be advertising a new one.
    engine.on_advertisement(mac_new, -55, rows[-1][3], t)
    assert engine.present, "fresh ad from a new MAC must mark the tag present"
    assert engine.last_mac == mac_new
