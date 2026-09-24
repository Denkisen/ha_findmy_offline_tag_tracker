# AGENTS.md — ha_findmy_offline_tag_tracker

Home Assistant custom integration that passively tracks a **Loshall Tag**
(Find Hub Network / "Find My" beacon) by BLE and verifies its rotating
Ephemeral Identity (EID) against the user's provisioning key material.

## Comments
- The EC point math lives in **`ec.py` (pure Python)** instead of using
  `fastecdsa`. `fastecdsa` 4.x is a compiled/Rust wheel that has no build
  path on Home Assistant OS, and pinning `cryptography` (which HA Core already
  ships) caused a hard install failure on HAOS. Keeping the only runtime
  dependency on `cryptography` (already a hard Core dependency) makes the
  manifest `requirements` empty, so HA never attempts an install. The pure-Python
  EC is verified byte-identical to `fastecdsa` (see `tests/test_fhn_codec.py`
  and the 0/200 cross-check).
- **Never** pin `cryptography` (or any package HA Core already ships) in
  `manifest.json` `requirements` — HAOS can't override its constrained version
  and setup will fail with "Requirements ... not found".
- The beacon's **MAC address randomises** and its EID rotates every 1024 s, so
  the stable identity for `device_info`/`unique_id` is the ephemeral identity
  key (EID), not the MAC. MAC is exposed only as an attribute.
- All datetimes are **timezone-aware UTC** (required by the TIMESTAMP sensor
  and for correct EID epoch computation). Do not introduce naive `datetime.now()`.
