"""Constants for the Find My Offline Tag Tracker (Loshall Tag) integration."""

from datetime import timedelta

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
# This is a *backstop* only. The coordinator also follows a per-address
# advertisement callback (which fires on every ad, unchanged payloads
# included) and HA's per-address unavailable signal, so ``last_seen`` stays
# fresh at the tag's real ad cadence and a real departure flips "away" via the
# unavailable event (~5 min). The timeout only catches the edge where neither
# per-address signal fired.
#
# It must stay comfortably above the beacon's worst-case *rotation silence*
# (observed ~148 s, and slowly growing as the beacon's stored clock drifts)
# plus its normal ad cadence (16–92 s observed), so the backstop itself never
# false-flips an in-range tag across a rotation. 600 s sits well above both.
DEFAULT_PRESENCE_TIMEOUT = 600  # s

# How often the coordinator pulls the bluetooth scanner's discovered-devices
# cache to look for the tag (see ``_async_poll_discovered`` in coordinator.py).
# This is the liveness backbone: unlike push callbacks it works even when the
# per-address advertisement callback is not delivered, and it lets the
# coordinator learn the tag's *current* MAC within one interval after a HA
# restart (the push callbacks can take up to a full EID rotation).
PRESENCE_POLL_INTERVAL = timedelta(seconds=30)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]
