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
- The beacon's **MAC address randomises** and its EID rotates every 1024 s
  (`ROTATION_PERIOD`), so the stable identity for `device_info`/`unique_id` is
  the ephemeral identity key (EID), not the MAC. MAC is exposed only as an
  attribute.
- **`DEFAULT_PRESENCE_TIMEOUT` must stay > `ROTATION_PERIOD` (1024 s).** HA's
  bluetooth manager (habluetooth) *suppresses advertisements whose
  `service_data` is unchanged* (see `habluetooth/manager.py`, the
  identical-`service_data` short-circuit before `_subclass_discover_info`), and
  the tag's Eddystone payload only changes when the EID rotates. So our BLE
  callback fires **at most once per 1024 s**, not per advertisement (unlike a
  raw bleak scanner such as `fhn_track.py`, which sees every ad). A timeout
  below 1024 s therefore flips the tag "away" on every rotation even while it
  is in range — the default is `ROTATION_PERIOD * 2`.
- **Presence OFF ("tag left") is driven by HA's per-address *unavailable*
  signal, not by the timeout.** The timeout is only a backstop. The coordinator
  registers `async_track_unavailable(hass, cb, address, connectable=False)` for
  the tag's *current* MAC and **re-points it whenever it sees a new MAC**
  (the MAC randomises). When the address stops advertising, habluetooth fires
  the unavailable callback, so the tag flips "away" in ~5 min (the
  `UNAVAILABLE_TRACK_SECONDS = 300 s` sweep is the binding gate) instead of
  ~34 min (the timeout). This is what makes door-correlation usable: a real
  departure must produce an "away" so the next arrival is a clean ON edge.
- **The unavailable callback MUST be guarded by the *event's* address, not the
  closure-captured one** (`if _service_info.address != self._address: return`).
  A MAC rotation makes the *old* address stop advertising, which fires its
  stale unavailable event; without the guard (and the guard comparing the event
  address to the *current* address) a stale event would wrongly flip the tag
  "away" mid-rotation. Safe because the 1024 s rotation gap cannot host a
  300 s sweep landing, and even at worst the tag re-advertises within ~1 s of
  the gap.
- All datetimes are **timezone-aware UTC** (required by the TIMESTAMP sensor
  and for correct EID epoch computation). Do not introduce naive `datetime.now()`.
