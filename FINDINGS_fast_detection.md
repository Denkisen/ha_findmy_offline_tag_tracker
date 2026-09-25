# FAST DETECTION — FINDINGS (2026-09-25)

## The user's problem
- Tag shows "Away" for 14h. User did NOT restart HA after deploying the new
  version, so the **OLD** integration code (commit `50bdc46 "init"`) is still
  running in memory.
- User asked: save current version to a branch, rollback to the previous commit,
  try to fix it. Also mentioned Bermuda + GoogleFindMy-HA work WITHOUT MQTT.

## WHY the old code (50bdc46) could NOT work on HA 2026.9.3 (multiple hard breaks)
1. `PRESENT_TIMEOUT_SECONDS = 120` (in `__init__.py`) << EDDYSTONE rotation
   period 1024 s. Since the callback only fires when service_data CHANGES
   (habluetooth dedup), `last_seen` updates at most once/1024 s. With a 120 s
   timeout the sensor is "away" ~85% of the time (and can stay away for hours
   across a rotation + no-ad window). THIS is the 14h "Away".
2. `from bleak import AdvertisementData, BLEDevice` — HAOS does not ship `bleak`
   to custom integrations; import fails on HAOS.
3. `async_register_callback(hass, cb, {"connectable": False})` — the current API
   requires a 4th `mode` arg (BluetoothScanningMode). Old 3-arg call would raise.
4. Callback signature `def cb(service_data, add_global_scan_results)` is wrong;
   HA passes `(BluetoothServiceInfoBleak, BluetoothChange)`.
5. `create_fhn_beacon` / `original_mac` / `last_clock_skew_ticks` don't exist in
   the current codec shape; `manifest.json` pins `fastecdsa` (uninstallable on
   HAOS).
=> The old version is not a viable base. Do NOT rollback to it. Keep the NEW
   version (commits c2d15ac..42446d2) and ENHANCE it.

## THE KEY DISCOVERY: a manager API fires on EVERY advertisement (no MQTT, no sidecar)
`HomeAssistantBluetoothManager.async_register_advertisement_callback(callback, address)`.
It is NOT in `bluetooth.__all__` (that list has `async_register_callback`,
`async_track_unavailable`, `async_set_fallback_availability_interval`, etc.).
Reach it via the public helper:
  `from homeassistant.components.bluetooth.api import _get_manager`
  `_get_manager(hass).async_register_advertisement_callback(cb, address)`  # sync, returns unregister callable

- callback receives a `BluetoothServiceInfoBleak` (ONE arg).
- Fires for **every** advertisement from that address, INCLUDING ones whose data
  is UNCHANGED (i.e. it bypasses the habluetooth dedup that throttles
  `async_register_callback`). Confirmed in `habluetooth/manager.py` ~line 1135:
  "Per-address subscribers see every advertisement, including ones whose data
  matches the previous one and would be skipped further down." Dispatch happens
  at ~1139-1142, BEFORE the identical-service_data `return` at ~1349.
- This is EXACTLY the "fhn_track.py sees the tag every 16-92 s" behavior, but
  inside HA, using HA's own scanner. No separate bleak process, no MQTT.

### Related public APIs (same api.py, all in `__all__`)
- `async_track_unavailable(hass, cb, address, connectable)` — per-address
  "stopped advertising" signal (already used in the current coordinator).
- `async_set_fallback_availability_interval(hass, address, interval)` — lets us
  TUNE the stale/availability gate per address (default fallback is 900 s; the
  300 s UNAVAILABLE_TRACK_SECONDS is the sweep cadence). Setting a shorter
  interval for the tag's address makes the OFF ("away") transition faster and
  deterministic. (VERIFY semantics against 2026.9.3 before relying on it.)
- `async_get_fallback_availability_interval(hass, address)`.
- `async_get_learned_advertising_interval(hass, address)`.

## How Bermuda does it (for reference)
- Does NOT use the change-driven callback for liveness. Every ~1.05 s
  (UPDATE_INTERVAL) it reads `ha_scanner.discovered_devices_and_advertisement_data`
  (a `{address: (BLEDevice, AdvertisementData)}` dict) straight from HA's
  internal scanners (`homeassistant.components.bluetooth.api._get_manager`).
  That dict is refreshed on every ad, so Bermuda effectively sees every ad too.
  (Heavier approach; the per-address advertisement callback is the cleaner path
  for a single tag.)
- Bermuda resolves rotating-MAC identity via IRK (Apple) / FMDN (Google) hooks;
  it cannot resolve OUR Loshall FHN EID offline (it has no key material), which
  is why the user says "can't determine if it's the same device."

## GoogleFindMy-HA
- Cloud-centric: needs Google OAuth/AAS token + email (config_flow CONF_OAUTH_TOKEN,
  CONF_GOOGLE_EMAIL); uses the Find My Device Network via the cloud / FCM. It does
  have `eid_resolver.py` but the whole flow assumes a Google account. Not a clean
  offline path for the Loshall tag. Not the right tool here.

## RECOMMENDED FIX (all in-HA, no MQTT/sidecar)
Enhance the CURRENT coordinator (keep the new version):
1. Register `async_register_advertisement_callback` for the tag's CURRENT MAC.
   On every ad: update `last_seen`, set present=True, refresh RSSI/MAC.
   This makes ON (arrival) instant and keeps last_seen fresh so the tag never
   falsely flips away mid-rotation.
2. Keep `async_register_callback` (matcher on the Eddystone service_data UUID)
   as the discovery/anchor: it tells us the tag's CURRENT MAC (which rotates)
   and lets us (re)register the per-address advertisement callback + unavailable
   tracker for the new MAC on each rotation.
3. OFF ("away"): drive from the per-address `async_track_unavailable` signal
   (already implemented) AND optionally `async_set_fallback_availability_interval`
   to bound the OFF latency. Keep the presence-timeout as a backstop.
4. Absence threshold must still exceed the beacon's ~148 s rotation silence
   (use ~200 s) to avoid false "away" during a rotation gap.

This removes the need for the `scanner/` sidecar + MQTT broker for the basic
presence case. Keep the sidecar only if the user wants sub-rotation OFF latency
that the in-HA unavailable gate can't reach.

## Status / next actions
- [x] Findings saved (this file).
- [ ] Verify `async_register_advertisement_callback` exact public signature +
      that it needs the address known (register AFTER first ad).
- [ ] Enhance coordinator with the per-address advertisement callback.
- [ ] Add a test replaying the capture through the NEW callback path.
- [ ] Decide (with user) whether to keep/remove the MQTT sidecar now that
      in-HA per-ad callbacks give fast detection.
- [ ] Deploy + tell user to RESTART HA (the 14h Away is stale old code in RAM).
