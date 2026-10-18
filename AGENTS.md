# AGENTS.md — ha_findmy_offline_tag_tracker

Home Assistant custom integration that passively tracks a **Find My tag**
(any Find Hub Network / "Find My" beacon — Loshall Tag, Tile, …) by BLE and
verifies its rotating Ephemeral Identity (EID) against the user's
provisioning key material.

## Comments
- **Multiple tags = multiple config entries.** Each entry builds its own
  `TrackedBeacon` + `TagCoordinator` and registers its own BLE callbacks; HA
  loads every entry of the domain when the first one is set up. Every
  advertisement is delivered to *all* entries' callbacks, and each
  coordinator's EID window match is what routes it to exactly one tag — a
  foreign tag's Eddystone payload never matches, so tags can't cross-talk.
  Verified by `test_two_tags_are_tracked_independently`.
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
- **Presence "stays in range" is driven by a *30 s poll of the
  scanner's discovered-devices cache*, not the service-UUID callback.**
  The service-UUID callback is throttled by habluetooth's identical-
  `service_data` dedup (it fires at most once per 1024 s EID rotation), and
  the per-address advertisement callback, while dispatched before the dedup
  short-circuit, proved unreliable on this deployment (ads reached the
  coordinator only every 8–30 min). The coordinator therefore also re-scans
  `scanner.discovered_devices_and_advertisement_data` (via
  `_get_manager(hass).async_current_scanners()`) every 30 s, EID-matching
  each cached payload — the same pull pattern Bermuda uses. This keeps
  `last_seen`/RSSI fresh at the tag's real ad cadence (16–92 s observed),
  re-learns the MAC after a HA restart within one interval, and catches MAC
  rotations the push callbacks swallowed. Cache entries are freshness-filtered
  against `discovered_device_timestamps` (monotonic; negative age = restored
  from a previous boot) and capped at the presence timeout.
- **The per-address advertisement callback is a *fast-path bonus that only
  exists on newer habluetooth* (≥ 7.x); HA 2026.9.3 ships
  `habluetooth==6.26.11` which does NOT have
  `async_register_advertisement_callback`.** Registration must be guarded
  with `getattr(manager, "async_register_advertisement_callback", None)` —
  an unguarded call crashed the coordinator on the user's box during history
  replay (`AttributeError`, fuzzy-suggested `async_register_allocation_callback`).
  On 6.x the poll below is the *only* liveness signal, so it must work on its
  own; likewise `scanner.discovered_device_timestamps` access is guarded
  (falls back to trusting the scanner's own entry expiration).
  **Never develop/test against a newer habluetooth than the box** — the venv
  must pin `habluetooth==6.26.11` (see README), otherwise tests pass on APIs
  that don't exist in production.
- **The Eddystone service-UUID callback and the 30 s poll are the guaranteed
  signals on every supported habluetooth.** The per-address advertisement
  callback (manager method, *not* in `bluetooth.__all__`, re-pointed on every
  MAC change exactly like the unavailable tracker) additionally keeps
  `last_seen` fresh at the tag's real ad cadence when it is available.
- **`DEFAULT_PRESENCE_TIMEOUT` (600 s) is a *backstop* only.** Because the
  per-address ad callback above keeps `last_seen` fresh every ad, the timeout
  only catches the edge where neither the per-address signal nor the
  unavailable event fired. It must stay comfortably above the beacon's
  worst-case *rotation silence* (~148 s observed, slowly growing as the
  beacon's stored clock drifts) plus its normal ad cadence (16–92 s) so the
  backstop itself never false-flips an in-range tag across a rotation.
- **Presence OFF ("tag left") is driven by HA's per-address *unavailable*
  signal, not by the timeout.** The coordinator registers
  `async_track_unavailable(hass, cb, address, connectable=False)` for the
  tag's *current* MAC and **re-points it whenever it sees a new MAC**
  (the MAC randomises). When the address stops advertising, habluetooth fires
  the unavailable callback, so the tag flips "away" in ~5 min (the
  `UNAVAILABLE_TRACK_SECONDS = 300 s` sweep is the binding gate) instead of
  waiting for the 600 s timeout backstop. This is what makes door-correlation
  usable: a real departure must produce an "away" so the next arrival is a
  clean ON edge.
- **Both per-address callbacks MUST be guarded by the *event's* address, not
  the closure-captured one** (`if service_info.address != self._address:
  return`). A MAC rotation makes the *old* address stop advertising (firing
  its stale unavailable event) and any late ad from it; without the guard a
  stale event/ad would wrongly flip the tag "away" mid-rotation or refresh
  presence from a wrong address.
- **The beacon's stored `clock_offset` drifts — do NOT predict rotation
  timing from it, and assume the drift *grows*.** A real capture showed the
  schedule-computed rotation boundary ~506 s away from the observed MAC
  change, and the observed rotation silence slowly grows — the beacon's
  stored clock runs ahead of wall time and runs fast. The only reliable
  rotation signal is the **MAC change itself** (the tag changes MAC exactly
  when the EID rotates). MAC rotation is therefore detected by the
  service-UUID callback's EID match landing on a new address (the per-address
  callback alone cannot see a *new* MAC, because it is registered per
  address).
- **The EID window must stay ahead of the beacon's *own* clock, not just of
  wall time.** The beacon's EID at wall time `t` is the one for its own
  clock period, and its stored clock runs ahead of wall time by a *growing*
  drift. A front edge pinned to a fixed wall-time offset (the reference
  tool's 90 periods: `now - 86400 + 90*1024 = now + 5760`) is permanently
  outrun once the drift passes ~1.6 h: `match()` then returns False
  **forever**, the 30 s poll can no longer see the tag, MAC re-anchoring
  dies at the next rotation, the 600 s backstop flips the tag "away", and —
  because a restart re-seeds the same wall-anchored window — **no restart
  ever recovers it**. This was the real cause of the "away after some time,
  updates were 2–5 s before that" bug. `TrackedBeacon` therefore:
  (a) seeds the window ±1 day around "now" (covers any beacon clock up to a
  day off at any moment, including right after a restart — the
  re-acquisition case), (b) on every successful match pins the front to
  `MAX_TRACKED_ROTATIONS` periods ahead of the beacon's last-seen period
  (a drifting clock can never run past it while the beacon is observed), and
  (c) caps the front at 2 days ahead of wall time so the window stays
  bounded (≤ ~3 days of periods). The match also narrows a drift estimate
  (`estimated_clock_drift()`, intervals intersected across matches) which
  the coordinator logs. Covered by
  `test_match_survives_growing_drift_past_old_front_margin`,
  `test_match_bootstraps_ahead_drifted_clock_on_first_poll`, and
  `test_estimated_clock_drift_converges`.
- **On-box diagnostics live in the coordinator logs, not the recorder.**
  To get real data from the box, set
  `logger: {custom_components.ha_findmy_offline_tag_tracker: debug}` and
  watch: the startup line reports the seeded window size (170 entries on
  v1.1.0+ — a 90-entry report means stale code in RAM, restart); the 5-min
  heartbeat shows window edges + estimated beacon-clock drift; the poll
  logs every Eddystone payload it saw that did NOT match (EID hex + window
  front) — those lines are the smoking gun for a match-layer fault (tag in
  range yet "away"); IN RANGE / OUT OF RANGE flips are logged at INFO with
  the reason (timeout vs unavailable-event) and the window state.
- All datetimes are **timezone-aware UTC** (required by the TIMESTAMP sensor
  and for correct EID epoch computation). Do not introduce naive `datetime.now()`.
