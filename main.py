#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Event
from time import sleep

import paho.mqtt.client as mqtt

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Argument Parsing
parser = argparse.ArgumentParser(description="Zigbee2MQTT OTA Update All Devices")
parser.add_argument(
    "--host", default=os.getenv("MQTT_SERVER", "hostname/ip"), help="MQTT Server"
)
parser.add_argument(
    "--port", type=int, default=int(os.getenv("MQTT_PORT", 1883)), help="MQTT Port"
)
parser.add_argument("--user", default=os.getenv("MQTT_USER", "user"), help="MQTT User")
parser.add_argument(
    "--password", default=os.getenv("MQTT_PASSWORD", "password"), help="MQTT Password"
)
parser.add_argument(
    "--max-concurrent",
    type=int,
    default=int(os.getenv("MAX_CONCURRENT_UPDATES", 1)),
    help="Max concurrent updates",
)
parser.add_argument(
    "--timeout",
    type=int,
    default=1800,
    help="Update timeout in seconds (default: 1800)",
)
parser.add_argument(
    "--retries", type=int, default=3, help="Max retries per device (default: 3)"
)
parser.add_argument(
    "--dry-run", action="store_true", help="Check for updates but do not install"
)
args = parser.parse_args()

if args.host == "hostname/ip":
    logger.error("Please configure your MQTT_SERVER (via env var or --host).")
    sys.exit(1)

MQTT_SERVER = args.host
MQTT_PORT = args.port
MQTT_USER = args.user
MQTT_PASSWORD = args.password
MAX_CONCURRENT_UPDATES = args.max_concurrent

# Global State
otadict = {}
currently_updating = []
sent_request = []
init_done_event = Event()
nicer_output_flag = False
only_once = True
num_total = 0


@dataclass
class OtaDevice:
    friendly_name: str
    ieee_addr: str
    supports_ota: bool
    checked_for_update: bool = False
    update_available: bool = False
    updating: bool = False
    last_progress: float = 0
    retries: int = 0
    failed: bool = False


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe("zigbee2mqtt/bridge/devices")
        client.subscribe("zigbee2mqtt/bridge/response/device/ota_update/check")
        client.subscribe("zigbee2mqtt/bridge/response/device/ota_update/update")
        # Request device list explicitly
        client.publish("zigbee2mqtt/bridge/request/devices", payload="")
    else:
        logger.error(f"Failed to connect, return code {rc}")


def on_message(client, userdata, msg):
    global nicer_output_flag, only_once, otadict
    try:
        message = (msg.payload).decode("utf-8")
        if not message:
            return
        obj = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug(f"Could not decode message on topic {msg.topic}: {e}")
        return

    lower_topic = msg.topic.lower()
    if lower_topic == "zigbee2mqtt/bridge/devices":
        if only_once:
            handle_devicelist(client, obj)
            only_once = False
    elif lower_topic == "zigbee2mqtt/bridge/response/device/ota_update/check":
        if not nicer_output_flag:
            logger.info("Fetching update responses:")
            nicer_output_flag = True
        handle_otacheck(client, obj)
    elif lower_topic == "zigbee2mqtt/bridge/response/device/ota_update/update":
        handle_otasuccess(client, obj)
    elif msg.topic.startswith("zigbee2mqtt/") and "bridge" not in lower_topic:
        if "update" in obj:
            device_fn = msg.topic.split("/", 1)[1]
            if isinstance(obj["update"], dict):
                update_progress(device_fn, obj["update"])


def update_progress(device_fn, update_data):
    if isinstance(update_data, dict):
        state = update_data.get("state")
        progress = update_data.get("progress")
        remaining = update_data.get("remaining")

        if progress is not None:
            try:
                msg = f"Updating {device_fn} - {float(progress):6.2f}%"
                if remaining is not None:
                    remaining_time = timedelta(seconds=int(remaining))
                    msg += f", {remaining_time} remaining"
                logger.info(msg)
            except (ValueError, TypeError):
                logger.debug(
                    f"Could not format progress data for {device_fn}: {update_data}"
                )

            # Update heartbeat
            res = [
                d
                for d in otadict.values()
                if d.updating and d.friendly_name == device_fn
            ]
            if res:
                res[0].last_progress = time.time()

        elif state:
            logger.info(f"Update status for {device_fn}: {state}")

            if state == "idle":
                r = [
                    d
                    for d in otadict.values()
                    if d.updating and d.friendly_name == device_fn
                ]
                if r:
                    otacleanup(client, r[0])


def handle_devicelist(client, devicelist):
    logger.info("Looking for supported devices:")
    global otadict, num_total
    for device in devicelist:
        if device.get("definition"):
            dev = OtaDevice(
                friendly_name=device["friendly_name"],
                ieee_addr=device["ieee_address"],
                supports_ota=device["definition"]["supports_ota"],
            )
            otadict[dev.ieee_addr] = dev
            if dev.supports_ota:
                logger.info(
                    f"  {dev.friendly_name} supports OTA Updates, checking for new updates"
                )
                num_total += 1
                check_for_update(client, dev)

    if num_total == 0 or not sent_request:
        logger.info("No OTA-supported devices found (or all already checked).")
        if not sent_request:
            init_done_event.set()


def handle_otacheck(client, obj):
    global otadict, sent_request, num_total
    ieee = obj.get("data", {}).get("id")
    if not ieee or ieee not in otadict:
        return

    device: OtaDevice = otadict[ieee]
    if ieee in sent_request:
        sent_request.remove(ieee)

    progress = f"[{num_total - len(sent_request)}/{num_total}]"
    if obj.get("status") == "ok":
        device.update_available = obj["data"].get("updateAvailable", False)
        logger.info(
            f"  {progress} {device.friendly_name} has an update available: {device.update_available}"
        )
    else:
        error_msg = obj.get("error", "Unknown error")
        logger.warning(f"  {progress} {device.friendly_name}: {error_msg}")

    if not sent_request:
        init_done_event.set()


def handle_otasuccess(client, obj):
    global otadict
    if obj.get("status") == "error":
        logger.error(f"Update error: {obj.get('error')}")
    else:
        name = obj.get("data", {}).get("id")
        res = [
            d
            for d in otadict.values()
            if d.friendly_name == name or d.ieee_addr == name
        ]
        if res:
            otacleanup(client, res[0])


def get_updateable_devices():
    return [
        device
        for device in otadict.values()
        if device.update_available and not device.updating and not device.failed
    ]


def otacleanup(client, dev: OtaDevice):
    global currently_updating
    dev.updating = False
    dev.update_available = False
    if dev.ieee_addr in currently_updating:
        currently_updating.remove(dev.ieee_addr)
    logger.info(
        f"Update for {dev.friendly_name} finished - {len(get_updateable_devices())} more updates to go"
    )
    client.unsubscribe(f"zigbee2mqtt/{dev.friendly_name}")


def check_for_update(client, device: OtaDevice):
    global sent_request
    client.publish(
        "zigbee2mqtt/bridge/request/device/ota_update/check",
        payload=json.dumps({"id": device.ieee_addr}),
    )
    sent_request.append(device.ieee_addr)
    device.checked_for_update = True


def start_update(client, device: OtaDevice):
    global currently_updating
    if args.dry_run:
        logger.info(f"[DRY-RUN] Would start update for {device.friendly_name}")
        device.update_available = False
        return

    logger.info(f"Starting Update for {device.friendly_name}")
    client.subscribe(f"zigbee2mqtt/{device.friendly_name}")
    client.publish(
        "zigbee2mqtt/bridge/request/device/ota_update/update",
        payload=json.dumps({"id": device.ieee_addr}),
    )
    device.updating = True
    device.last_progress = time.time()
    currently_updating.append(device.ieee_addr)


def on_log(client, userdata, level, buf):
    logger.debug(f"MQTT Log: {buf}")


# Main Execution
try:
    # Try Paho MQTT v2 API
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    # v2 on_connect signature includes 'properties'
    def on_connect_v2(client, userdata, flags, rc, properties=None):
        on_connect(client, userdata, flags, rc)

    client.on_connect = on_connect_v2
except (AttributeError, TypeError):
    # Fallback to Paho MQTT v1 API
    client = mqtt.Client()
    client.on_connect = on_connect

client.on_message = on_message
client.on_log = on_log

if args.user and args.password:
    client.username_pw_set(args.user, args.password)

logger.info("Starting initialization")
try:
    client.connect(MQTT_SERVER, MQTT_PORT, 60)
except Exception as e:
    logger.error(f"Could not connect to MQTT broker: {e}")
    sys.exit(1)

client.loop_start()

if not init_done_event.wait(timeout=60):
    logger.warning("Initialization timed out. Some devices might not have responded.")

logger.info("Finished initialization")

try:
    while True:
        if not client.is_connected():
            logger.warning("Lost connection to MQTT broker. Waiting...")
            sleep(5)
            continue

        updateable = get_updateable_devices()

        if init_done_event.is_set() and not updateable and not currently_updating:
            break

        if updateable and len(currently_updating) < MAX_CONCURRENT_UPDATES:
            device = updateable.pop(0)
            start_update(client, device)

        sleep(1)

except KeyboardInterrupt:
    logger.info("Aborted by user")

client.loop_stop()
logger.info("Finished updating")
