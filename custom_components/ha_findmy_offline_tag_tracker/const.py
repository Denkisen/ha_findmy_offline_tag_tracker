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
#
# This MUST be larger than ROTATION_PERIOD: Home Assistant's bluetooth manager
# (habluetooth) suppresses advertisements whose service_data is unchanged, and
# the tag's Eddystone payload only changes when the EID rotates. So the
# callback fires at most once every ROTATION_PERIOD seconds, and any timeout
# below that would flip the tag "away" on every rotation even while in range.
DEFAULT_PRESENCE_TIMEOUT = ROTATION_PERIOD * 2  # 2048 s

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
