"""Raw-BLE Loshall Tag presence scanner.

A standalone passive scanner (bleak) that sees *every* advertisement from the
tag — unlike Home Assistant's bluetooth stack, which suppresses unchanged
service-data. It runs on the HA host (or any Linux box with the tag in range),
decides presence with the MAC-change-driven :class:`PresenceEngine`, and pushes
state changes to Home Assistant via MQTT.

MQTT layout (HA auto-discovery):
  * ``<base>/config``     — one-time HA discovery for a binary_sensor
  * ``<base>/state``      — "on"/"off"
  * ``<base>/attrs``      — JSON attributes (mac, rssi, last_seen, name)
  * ``<base>/status``     — availability ("online"/"offline", LWT on exit)

Usage (CLI arguments win over environment variables):

    python fhn_presence.py \
        --eik <hex EIK> --clock-offset <float> --curve secp160r1 --name "Loshall Tag" \
        --mqtt-url mqtt://host:1883 --mqtt-base-topic fhn/losall_tag \
        [--mqtt-username U --mqtt-password P] \
        [--normal-threshold 200 --rotation-threshold 400]

Or, for a beacon without EIK (accepts any well-formed FHN Eddystone frame):

    python fhn_presence.py --name "Some Tag" --mqtt-url ...

The scanner is intentionally dependency-light: bleak + cryptography (+ paho-mqtt).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .fhn_const import EDDYSTONE_SERVICE_UUID
from .presence_engine import BeaconSpec, PresenceEngine

_LOGGER = logging.getLogger("fhn_presence")


# --------------------------------------------------------------------- #
# MQTT push (HA auto-discovery)
# --------------------------------------------------------------------- #
class MQTTReporter:
    """Publishes presence to Home Assistant over MQTT with auto-discovery."""

    def __init__(self, args: argparse.Namespace) -> None:
        import paho.mqtt.client as mqtt

        self._args = args
        self._base = args.mqtt_base_topic.rstrip("/")
        self._unique_id = args.mqtt_unique_id or f"fhn_tag_{uuid.uuid4().hex[:12]}"

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if args.mqtt_username:
            self._client.username_pw_set(args.mqtt_username, args.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        # LWT marks the sensor unavailable if we die.
        self._client.will_set(f"{self._base}/status", payload="offline", qos=1, retain=True)
        self._client.connect(*self._endpoint())
        self._client.loop_start()

    def _endpoint(self) -> tuple[str, int]:
        rest = self._args.mqtt_url.split("://", 1)[1]
        host, _, port = rest.partition(":")
        return host, int(port or 1883)

    def _on_connect(self, _c, _u, _f, rc, _p=None) -> None:
        _LOGGER.info("MQTT connected (rc=%s)", rc)
        if rc == 0:
            self._publish_discovery()
            self._client.publish(f"{self._base}/status", "online", qos=1, retain=True)

    def _on_disconnect(self, _c, _u, _f, rc) -> None:
        _LOGGER.warning("MQTT disconnected (rc=%s); paho will reconnect", rc)

    def _publish_discovery(self) -> None:
        disc = {
            "name": self._args.name,
            "unique_id": self._unique_id,
            "object_id": self._unique_id,
            "state_topic": f"{self._base}/state",
            "json_attributes_topic": f"{self._base}/attrs",
            "availability_topic": f"{self._base}/status",
            "payload_on": "on",
            "payload_off": "off",
            "device": {
                "name": self._args.name,
                "identifiers": [self._unique_id],
                "manufacturer": "Loshall",
                "model": "Loshall Tag (raw-BLE scanner)",
            },
        }
        self._client.publish(
            f"homeassistant/binary_sensor/{self._unique_id}/config",
            json.dumps(disc),
            qos=1,
            retain=True,
        )
        _LOGGER.info("published HA discovery to homeassistant/binary_sensor/%s/config", self._unique_id)

    def report(self, _event: str, payload: dict) -> None:
        state = "on" if payload["present"] else "off"
        attrs = {
            "name": payload["name"],
            "mac": payload["last_mac"],
            "rssi": payload["rssi"],
            "last_seen": payload["last_seen"],
        }
        self._client.publish(f"{self._base}/state", state, qos=1, retain=True)
        self._client.publish(f"{self._base}/attrs", json.dumps(attrs), qos=1, retain=True)

    def close(self) -> None:
        try:
            self._client.publish(f"{self._base}/status", "offline", qos=1, retain=True)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()


# --------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------- #
def _build_engine(args: argparse.Namespace) -> PresenceEngine:
    beacon = BeaconSpec(
        name=args.name,
        clock_offset=args.clock_offset,
        curve_name=args.curve,
        eik_hex=args.eik,
    )
    return PresenceEngine(
        beacon=beacon,
        normal_threshold=args.normal_threshold,
        rotation_threshold=args.rotation_threshold,
    )


def _make_ad_cb(engine: PresenceEngine, reporter: MQTTReporter):
    def _on_advertisement(
        device: "BLEDevice", advertisement_data: AdvertisementData
    ) -> None:
        payload = advertisement_data.service_data.get(EDDYSTONE_SERVICE_UUID)
        if payload is None:
            return
        engine.on_advertisement(
            device.address, advertisement_data.rssi, payload, datetime.now(timezone.utc)
        )

    def _on_change(event: str, payload: dict) -> None:
        _LOGGER.info("state change: %s -> %s", event, payload)
        try:
            reporter.report(event, payload)
        except Exception:
            _LOGGER.exception("failed to report %s", event)

    return _on_advertisement, _on_change


async def run(args: argparse.Namespace) -> None:
    engine = _build_engine(args)
    reporter = MQTTReporter(args)
    _on_ad, _on_change = _make_ad_cb(engine, reporter)
    engine.on_change = _on_change

    scanner = BleakScanner(_on_ad, [EDDYSTONE_SERVICE_UUID], "passive")
    await scanner.start()
    _LOGGER.info(
        "scanning for %s (normal=%ss, rotation=%ss)",
        args.name,
        args.normal_threshold,
        args.rotation_threshold,
    )
    try:
        while True:
            engine.tick(datetime.now(timezone.utc))
            await asyncio.sleep(engine.check_interval)
    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()
        reporter.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Raw-BLE Loshall Tag presence scanner")
    p.add_argument("--name", default=os.environ.get("FHN_TAG_NAME", "Loshall Tag"))
    p.add_argument("--eik", default=os.environ.get("FHN_TAG_EIK"))
    p.add_argument(
        "--clock-offset",
        type=float,
        default=float(os.environ.get("FHN_TAG_CLOCK_OFFSET", "0")),
    )
    p.add_argument("--curve", default=os.environ.get("FHN_TAG_CURVE", "secp160r1"))
    p.add_argument(
        "--normal-threshold",
        type=float,
        default=float(os.environ.get("FHN_NORMAL_THRESHOLD", "200")),
    )
    p.add_argument(
        "--rotation-threshold",
        type=float,
        default=float(os.environ.get("FHN_ROTATION_THRESHOLD", "400")),
    )
    p.add_argument("--mqtt-url", required=True, default=os.environ.get("FHN_MQTT_URL"))
    p.add_argument(
        "--mqtt-base-topic",
        default=os.environ.get("FHN_MQTT_BASE_TOPIC", "fhn/losall_tag"),
    )
    p.add_argument("--mqtt-username", default=os.environ.get("FHN_MQTT_USER"))
    p.add_argument("--mqtt-password", default=os.environ.get("FHN_MQTT_PASS"))
    p.add_argument("--mqtt-unique-id", default=os.environ.get("FHN_MQTT_UNIQUE_ID"))
    p.add_argument("--log-level", default=os.environ.get("FHN_LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
