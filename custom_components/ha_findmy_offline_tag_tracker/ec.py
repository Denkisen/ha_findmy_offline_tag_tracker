"""Pure-Python short-Weierstrass elliptic-curve point math.

Implements just enough EC to compute ``r * G`` (the public point for a scalar
``r``) and read its x-coordinate, which is all the FHN EID algorithm needs.

This replaces ``fastecdsa`` so the integration has no compiled/Rust
dependencies and only relies on ``cryptography`` (shipped by Home Assistant
Core). The arithmetic is verified to produce byte-identical EIDs to the
reference ``fhn_track.py`` implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class Point:
    """An affine point on a short-Weierstrass curve (None = point at infinity)."""

    __slots__ = ("x", "y", "_curve")

    def __init__(self, x: int, y: int, curve: "Curve") -> None:
        self.x = x
        self.y = y
        self._curve = curve

    @staticmethod
    def _inv(a: int, p: int) -> int:
        return pow(a, p - 2, p)

    def double(self) -> Optional[Point]:
        curve = self._curve
        p, a = curve.p, curve.a
        if self.y == 0:
            return None
        three_x2_plus_a = (3 * self.x * self.x + a) % p
        two_y = (2 * self.y) % p
        lam = (three_x2_plus_a * self._inv(two_y, p)) % p
        x3 = (lam * lam - 2 * self.x) % p
        y3 = (lam * (self.x - x3) - self.y) % p
        return Point(x3, y3, curve)

    def add(self, other: Optional[Point]) -> Optional[Point]:
        if other is None:
            return self
        if self is None:
            return other
        curve = self._curve
        p = curve.p
        if self.x == other.x:
            if (self.y + other.y) % p == 0:
                return None  # P + (-P) = O
            return self.double()
        dx = (other.x - self.x) % p
        lam = ((other.y - self.y) % p * self._inv(dx, p)) % p
        x3 = (lam * lam - self.x - other.x) % p
        y3 = (lam * (self.x - x3) - self.y) % p
        return Point(x3, y3, curve)

    def multiply(self, k: int) -> Optional[Point]:
        """Scalar multiplication via double-and-add."""
        result: Optional[Point] = None
        addend: Optional[Point] = self
        k %= self._curve.q
        while k:
            if k & 1:
                if result is None:
                    result = addend
                else:
                    result = result.add(addend)
            addend = addend.double() if addend is not None else None
            k >>= 1
        return result

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Point({self.x:#x}, {self.y:#x}) on {self._curve.name}"


@dataclass(frozen=True)
class Curve:
    """A short-Weierstrass curve over a prime field."""

    name: str
    p: int  # field prime
    a: int  # curve parameter a
    b: int  # curve parameter b
    q: int  # group order
    gx: int  # generator x
    gy: int  # generator y

    @property
    def generator(self) -> Point:
        return Point(self.gx, self.gy, self)

    @property
    def eid_length(self) -> int:
        return (self.p.bit_length() + 7) // 8


# The two EID curves from the Find Hub Network Accessory Specification.
# Parameters are identical to the reference fhn_track.py definitions.
SECP160R1 = Curve(
    name="secp160r1",
    p=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_7FFFFFFF,
    a=0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_7FFFFFFC,
    b=0x1C97BEFC_54BD7A8B_65ACF89F_81D4D4AD_C565FA45,
    q=0x01_00000000_00000000_0001F4C8_F927AED3_CA752257,
    gx=0x4A96B568_8EF57328_46646989_68C38BB9_13CBFC82,
    gy=0x23A62855_3168947D_59DCC912_04235137_7AC5FB32,
)

SECP256R1 = Curve(
    name="secp256r1",
    p=0xFFFFFFFF_00000001_00000000_00000000_00000000_FFFFFFFF_FFFFFFFF_FFFFFFFF,
    a=0xFFFFFFFF_00000001_00000000_00000000_00000000_FFFFFFFF_FFFFFFFF_FFFFFFFC,
    b=0x5AC635D8_AA3A93E7_B3EBBD55_769886BC_651D06B0_CC53B0F6_3BCE3C3E_27D2604B,
    q=0xFFFFFFFF_00000000_FFFFFFFF_FFFFFFFF_BCE6FAAD_A7179E84_F3B9CAC2_FC632551,
    gx=0x6B17D1F2_E12C4247_F8BCE6E5_63A440F2_77037D81_2DEB33A0_F4A13945_D898C296,
    gy=0x4FE342E2_FE1A7F9B_8EE7EB4A_7C0F9E16_2BCE3357_6B315ECE_CBB64068_37BF51F5,
)

_CURVES = {
    "secp160r1": SECP160R1,
    "secp256r1": SECP256R1,
}


def curve_for_name(name: str) -> Curve:
    """Return the curve for a provisioning-reported name; KeyError if unknown."""
    return _CURVES[name]
