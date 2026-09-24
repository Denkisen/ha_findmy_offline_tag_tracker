"""Constants for the Find My Offline Tag Tracker (Loshall Tag) integration."""

from homeassistant.const import Platform

DOMAIN = "ha_findmy_offline_tag_tracker"

# Eddystone service data UUID. Loshall Tag (and other FHN beacons) advertise
# their Ephemeral Identity (EID) as Eddystone service data under this UUID.
EDDYSTONE_SERVICE_UUID = "0000feaa-0000-1000-8000-00805f9b34fb"

# The FHN Accessory Specification rotates the EID every 2^10 seconds.
ROTATION_PERIOD_EXPONENT = 10
ROTATION_PERIOD = 1 << ROTATION_PERIOD_EXPONENT

# How far in the past we consider an advertisement valid (clock skew tolerance).
MAX_CLOCK_SKEW_SECONDS = 86400  # 1 day

# How many EID rotations we pre-compute ahead of "now".
MAX_TRACKED_ROTATIONS = 90

# After this many seconds without a matching advertisement, the tag is
# considered no longer in range.
DEFAULT_PRESENCE_TIMEOUT = 300

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
