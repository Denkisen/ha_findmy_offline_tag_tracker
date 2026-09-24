"""Standalone constants for the raw-BLE presence scanner (no HA imports)."""

# Eddystone service data UUID used by Loshall Tag / FHN beacons.
EDDYSTONE_SERVICE_UUID = "0000feaa-0000-1000-8000-00805f9b34fb"

# The FHN Accessory Specification rotates the EID every 2^10 seconds.
ROTATION_PERIOD_EXPONENT = 10
ROTATION_PERIOD = 1 << ROTATION_PERIOD_EXPONENT

# How far in the past we consider an advertisement valid (clock skew tolerance).
MAX_CLOCK_SKEW_SECONDS = 86400  # 1 day

# How many EID rotations we pre-compute ahead of "now".
MAX_TRACKED_ROTATIONS = 90
