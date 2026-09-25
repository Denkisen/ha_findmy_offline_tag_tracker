"""Unit tests for the FHN EID crypto (no Home Assistant required)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make the integration package importable directly (it lives under
# custom_components/ at the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from ha_findmy_offline_tag_tracker import fhn_codec  # noqa: E402

CLOCK_OFFSET = 17000000.5
EIK = bytes.fromhex(
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
EID_LEN = 20  # secp160r1


def _beacon() -> fhn_codec.FHNBeacon:
    return fhn_codec.FHNBeacon(CLOCK_OFFSET, fhn_codec.SECP160R1, EIK, "first")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_eid_deterministic_and_length():
    beacon = _beacon()
    now = _now()
    a = beacon.generate_eid(now.timestamp())
    b = beacon.generate_eid(now.timestamp())
    assert a == b
    assert len(a) == EID_LEN


def test_eid_rotates_across_periods():
    beacon = _beacon()
    now = _now()
    current = beacon.generate_eid(now.timestamp())
    next_period = beacon.generate_eid(now.timestamp() + 1024)
    assert current != next_period


def test_eid_matches_reference_algorithm():
    """Recompute the EID with an independent AES+ECDSA path and compare.

    This guards against a regression in the AES plaintext layout, the
    rprime -> point reduction, or the pure-Python EC arithmetic. It builds a
    fastecdsa curve from the same parameters and compares x-coordinates.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from fastecdsa.curve import Curve as FE
    from fastecdsa.keys import get_public_key

    beacon = _beacon()
    now = _now()
    ts = now.timestamp()

    # A fastecdsa curve with identical parameters, for an independent check.
    c = fhn_codec.SECP160R1
    fe_curve = FE(
        name=c.name, p=c.p, a=c.a, b=c.b, q=c.q, gx=c.gx, gy=c.gy
    )

    clock_value = int(ts - CLOCK_OFFSET)
    clock_value -= clock_value % 1024
    plaintext = bytearray(32)
    plaintext[0:11] = b"\xff" * 11
    plaintext[11] = 10
    plaintext[12:16] = clock_value.to_bytes(4)
    plaintext[16:27] = b"\x00" * 11
    plaintext[27] = 10
    plaintext[28:32] = clock_value.to_bytes(4)
    enc = Cipher(algorithms.AES256(EIK), modes.ECB()).encryptor()
    rprime = int.from_bytes(enc.update(bytes(plaintext)) + enc.finalize())
    r = rprime % c.q
    expected = get_public_key(r, fe_curve).x.to_bytes(20)

    assert beacon.generate_eid(ts) == expected


def test_match_current_eid():
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    adv = b"\x40" + beacon.generate_eid(now.timestamp())
    assert tracked.match(adv, now) is True
    assert tracked.last_seen is not None


def test_match_eid_with_two_byte_frame_control():
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    adv = b"\x41" + beacon.generate_eid(now.timestamp())
    assert tracked.match(adv, now) is True


@pytest.mark.parametrize("periods_ago", [1, 2, 5, 20, 80])
def test_match_past_eid_within_skew(periods_ago):
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    past_ts = now.timestamp() - periods_ago * 1024
    adv = b"\x40" + beacon.generate_eid(past_ts)
    assert tracked.match(adv, now) is True


def test_match_rejects_bogus_eid():
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    assert tracked.match(b"\x40" + b"\xff" * EID_LEN, now) is False


def test_match_rejects_wrong_length():
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    assert tracked.match(b"\x40" + b"\x00" * 5, now) is False


def test_match_rejects_bad_frame_control():
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    # 0x00 is not a valid Eddystone beacon frame control byte.
    assert tracked.match(b"\x00" + beacon.generate_eid(now.timestamp()), now) is False


def test_match_rejects_very_old_eid():
    beacon = _beacon()
    now = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, now)
    old_ts = now.timestamp() - (90 * 1024 + 100_000)
    adv = b"\x40" + beacon.generate_eid(old_ts)
    assert tracked.match(adv, now) is False


def test_window_grows_and_stays_bounded():
    beacon = _beacon()
    start = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, start)
    size_after_init = len(tracked._eids)
    # Advance time so some entries expire; the window must grow forward and
    # not grow without bound.
    later = start + timedelta(days=3)
    tracked.match(b"\x40" + beacon.generate_eid(later.timestamp()), later)
    assert len(tracked._eids) <= size_after_init + 5


def test_match_stays_current_through_many_rotations_and_clock_drift():
    """Regression: the window must keep matching the beacon's *current* EID
    for hours of continuous operation, even when the beacon's stored clock
    runs ahead of wall time.

    Before the window re-anchoring fix, the slide only appended a single
    "now" entry, so the front edge lagged the beacon's ahead-shifted current
    period and matching stopped after ~80-150 minutes — the tag falsely
    flipped away while still advertising.
    """
    beacon = _beacon()
    start = _now()
    tracked = fhn_codec.TrackedBeacon(beacon, start)
    # The beacon's stored clock can run ahead of wall time; the broadcast EID
    # at wall time ``t`` is the one for ``t + drift``. Exercise a range of
    # drifts, including full-period offsets, across 3+ hours of 30 s polls.
    for drift in (0, 300, 624, 1023, 1024, 2047):
        for step in range(400):
            now = start + timedelta(seconds=step * 30)
            broadcast = beacon.generate_eid(now.timestamp() + drift)
            assert tracked.match(b"\x40" + broadcast, now), (
                f"missed the tag at step {step} with drift {drift}s"
            )
    # The window must remain bounded after all the re-anchoring.
    assert len(tracked._eids) <= 95


def test_curve_for_name():
    assert fhn_codec.curve_for_name("secp160r1") is fhn_codec.SECP160R1
    assert fhn_codec.curve_for_name("secp256r1") is fhn_codec.SECP256R1


def test_curve_for_name_unknown_raises():
    with pytest.raises(KeyError):
        fhn_codec.curve_for_name("not-a-curve")


def test_build_tracker_from_config_hex():
    tracked = fhn_codec.build_tracker(
        clock_offset=CLOCK_OFFSET,
        curve_name="secp160r1",
        ephemeral_identity_key_hex=EIK.hex(),
        name="first",
        now=_now(),
    )
    assert tracked.beacon.name == "first"
    assert tracked.beacon.ephemeral_identity_key == EIK


def test_build_tracker_bad_hex_raises():
    with pytest.raises(ValueError):
        fhn_codec.build_tracker(
            clock_offset=CLOCK_OFFSET,
            curve_name="secp160r1",
            ephemeral_identity_key_hex="zznothex",
            name="x",
            now=_now(),
        )
