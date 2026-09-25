# Find My Tag Tracker

WARNING: VIBE CODED!

A Home Assistant custom integration that passively tracks any **Find My /
Find Hub Network (FHN) beacon** — Loshall Tag, Tile, and similar — over
Bluetooth, **offline**, with no cloud, no MQTT, and no sidecar process.

Given the key material printed when you provisioned the tag, the integration
verifies the tag's rotating Ephemeral Identity (EID) against your keys. That
lets it recognize the tag even though the tag **randomizes its MAC address**
and **rotates its EID every ~17 minutes**.

## Features

- **Presence** — `binary_sensor.<name>_in_range` (device class: presence)
- **RSSI** — `sensor.<name>_rssi` (dBm of the last matching advertisement)
- **Last seen** — `sensor.<name>_last_seen` (timestamp of the last match)
- **Device tracker** — `device_tracker.<name>_tag`, so you can assign the tag
  to a *person* (Settings → Devices & Services → Person) and use it for
  zone-based automations. In range = the tracker's associated zone (Home by
  default, configurable per entity); out of range = `not_home`.
- **Fast "tag left"** — HA's per-address unavailable signal flips presence
  off in ~5 minutes when the tag stops advertising (not just when the
  timeout expires).
- **Multiple tags** — add one config entry per tag. Every advertisement is
  checked against every tag's key material and routed to exactly one; tags
  cannot cross-talk.

## How it works

FHN beacons advertise an Eddystone service-data payload containing the
current EID, computed as `AES-256-EIK(rotation counter)` → EC point on
`secp160r1` (or `secp256r1`). The EID changes every 2^10 = 1024 s.

The coordinator:

1. keeps a rolling window of ~90 expected EIDs (covering one day of clock
   skew, past and future rotations);
2. receives advertisements via the Bluetooth service-UUID callback;
3. re-scans the scanner's discovered-devices cache every 30 s (the
   liveness backbone — the same pattern Bermuda uses), so presence stays
   fresh even when push callbacks are under-delivered;
4. re-points its per-address trackers every time the tag's MAC rotates.

The EID/ephemeral identity key — not the MAC — is the stable device
identifier in HA's device registry.

## Requirements

- Home Assistant with a working Bluetooth adapter (BlueZ / HAOS)
- The tag's **key material** from provisioning:
  - `clock_offset` (a float, e.g. `17000000.5`)
  - `ephemeral_identity_key` (hex, 32 or 64 characters)
  - `curve` (`secp160r1` or `secp256r1`)

  For Loshall-style tags this is what `fhn_provision.py` (see
  [fhn-beacon-tools](https://github.com/Denkisen/fhn-beacon-tools)) prints
  when you pair and provision the tag.

No extra Python packages: the only runtime dependency is `cryptography`,
which Home Assistant Core already ships. The EC math is pure Python, so the
integration installs cleanly on HAOS.

## Installation

1. Copy the `custom_components/ha_findmy_offline_tag_tracker/` folder into
   `<config dir>/custom_components/`.
2. Restart Home Assistant.
3. Add the integration: *Settings → Devices & Services → Add Integration →
   Find My Tag Tracker*.
4. Enter the tag name and its key material.

To add more tags, repeat step 3 — one entry per tag.

## Options

- **Presence timeout (seconds)** — default 600. A *backstop* only: with the
  30 s discovery poll, an in-range tag stays present indefinitely. The
  timeout flips the tag out of range only if neither the poll nor the
  per-address signals see it (e.g. adapter wedged). Keep it comfortably
  above the beacon's worst-case rotation silence (~150 s) plus its ad
  cadence (16–92 s).

## Usage examples

Assign to a person (door/entry use case):

1. *Settings → Devices & Services → Person → Edit → Device trackers* → pick
   `device_tracker.<name>_tag`.

Intrusion-style automation (door open + your tag away):

```yaml
alias: Possible intruder
trigger:
  - platform: state
    entity_id: binary_sensor.entry_door
    to: "on"
condition:
  - condition: state
    entity_id: device_tracker.my_tag_tag
    state: not_home
action:
  - service: notify.mobile_app
    data:
      message: Door opened while tag is out of range!
```

## Development

```bash
python3 -m venv venv
venv/bin/pip install homeassistant==2026.9.3 pytest pytest-homeassistant-custom-component
venv/bin/python -m pytest tests/
```

Tested on Python 3.14 with the versions above. Tests use the real Home
Assistant test harness (no BlueZ required — BLE registration is stubbed).
The codec is verified byte-identical to the reference `fhn-beacon-tools`
implementation.

## Troubleshooting

- **Tag stuck "Away" while it's clearly advertising** — check that the
  Bluetooth integration is set up and the adapter is scanning
  (*Settings → Devices & Services → Bluetooth*). The 30 s poll reads the
  scanner's discovered-devices cache; if the tag appears there but not in
  our entities, check the EID key material (a wrong `clock_offset` shifts
  the rotation window).
- **RSSI shows a value but presence is "Away"** — the RSSI sensor is only
  cleared by the timeout; a fresh ad refreshes both at once.
- **Entity IDs look odd after a rename** — entity IDs derive from the tag
  *name* you entered; the unique IDs (and device registry entry) are based
  on the EID key, so renaming only affects IDs, not the wiring.

## License

See [LICENSE](LICENSE).
