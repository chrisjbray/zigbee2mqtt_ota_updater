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
        # Ensure we're subscribed to all devices we already know about
        for dev in otadict.values():
            if dev.updating:
                topic = f"zigbee2mqtt/{dev.friendly_name}"
                client.subscribe(topic)
                logger.info(f"Subscribed to {topic} on reconnect.")
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
            update_progress(device_fn, obj["update"])


def update_progress(device_fn, update_data):
    if not isinstance(update_data, dict):
        # Handle string or other simple types
        logger.info(f"Update status for {device_fn}: {update_data}")
        if update_data == "idle":
            r = [
                d
                for d in otadict.values()
                if d.updating and d.friendly_name == device_fn
            ]
            if r:
                otacleanup(client, r[0])
        return

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
            d for d in otadict.values() if d.updating and d.friendly_name == device_fn
        ]
        if res:
            res[0].last_progress = time.time()

    elif state:
        logger.info(f"Update status for {device_fn}: {state}")

        if state == "idle":
            r = [
                d for d in otadict.values() if d.updating and d.friendly_name == device_fn
            ]
            if r:
                otacleanup(client, r[0])
        elif state == "updating":
            # Ensure we have a heartbeat for starting
            res = [
                d for d in otadict.values() if d.updating and d.friendly_name == device_fn
            ]
            if res:
                res[0].last_progress = time.time()


def handle_devicelist(client, devicelist):
    logger.info("Looking for supported devices:")
    global otadict, num_total, currently_updating
    for device in devicelist:
        if device.get("definition"):
            # Initial detection of update available from raw device data
            raw_update_available = (
                device.get("update_available") is True
                or device.get("available") is True
            )

            dev = OtaDevice(
                friendly_name=device["friendly_name"],
                ieee_addr=device["ieee_address"],
                supports_ota=device["definition"]["supports_ota"],
                update_available=raw_update_available,
            )

            if dev.supports_ota:
                # Detect existing update states from the 'update' object
                already_handled = False
                if "update" in device:
                    u_obj = device["update"]
                    u_state = u_obj.get("state")
                    u_available = (
                        u_obj.get("update_available")
                        or u_obj.get("available")
                        or (u_state == "available")
                    )

                    if u_available:
                        dev.update_available = True
                        already_handled = True
                        logger.info(
                            f"  {dev.friendly_name} has an update available (from update object)."
                        )
                    elif u_state == "updating":
                        dev.updating = True
                        dev.update_available = True
                        already_handled = True
                        if dev.ieee_addr not in currently_updating:
                            currently_updating.append(dev.ieee_addr)

                        topic = f"zigbee2mqtt/{dev.friendly_name}"
                        client.subscribe(topic)
                        logger.info(
                            f"  {dev.friendly_name} is already updating. Subscribed to {topic}"
                        )
                        dev.last_progress = time.time()

                otadict[dev.ieee_addr] = dev
                num_total += 1
                if already_handled:
                    logger.debug(
                        f"  {dev.friendly_name} skip initial check, state handled."
                    )
                else:
                    logger.info(
                        f"  {dev.friendly_name} supports OTA Updates, checking for new updates"
                    )
                    check_for_update(client, dev)

    if num_total == 0 or not sent_request:
        logger.info("No OTA-supported devices found (or all already checked).")
        if not sent_request:
            init_done_event.set()


def handle_otacheck(client, obj):
    global otadict, sent_request, num_total
    logger.debug(f"Raw check response: {json.dumps(obj)}")

    # Robust lookup by ID or Friendly Name
    res_id = obj.get("data", {}).get("id")

    if not res_id and obj.get("status") == "error":
        # Fallback: Extract friendly name from error message if possible
        error_msg = obj.get("error", "")
        for marker in ["already in progress for '", "available for '"]:
            if marker in error_msg:
                try:
                    res_id = error_msg.split(marker)[1].split("'")[0]
                    logger.info(f"Extracted device name '{res_id}' from error message.")
                    break
                except IndexError:
                    pass

    if not res_id:
        logger.warning(f"Check response missing ID in data: {obj}")
        return

    res = [
        d
        for d in otadict.values()
        if d.friendly_name == res_id or d.ieee_addr == res_id
    ]
    if not res:
        logger.debug(f"Check response for unknown device {res_id}")
        return

    device = res[0]
    ieee = device.ieee_addr

    if ieee in sent_request:
        sent_request.remove(ieee)

    progress = f"[{num_total - len(sent_request)}/{num_total}]"
    if obj.get("status") == "ok":
        raw_val = obj["data"].get("update_available")
        if raw_val is None:
            raw_val = obj["data"].get("updateAvailable")
        if raw_val is None:
            raw_val = obj["data"].get("available")

        if isinstance(raw_val, bool):
            device.update_available = raw_val
        elif isinstance(raw_val, str):
            device.update_available = raw_val.lower() == "available"
        else:
            device.update_available = bool(raw_val)

        logger.info(
            f"  {progress} {device.friendly_name} has an update available: {device.update_available}"
        )
    else:
        error_msg = obj.get("error", "Unknown error")
        logger.warning(f"  {progress} {device.friendly_name}: {error_msg}")
        # If it's already in progress, mark it as available/updating
        if "in progress" in error_msg.lower():
            device.update_available = True
            device.updating = True
            if device.ieee_addr not in currently_updating:
                currently_updating.append(device.ieee_addr)
            topic = f"zigbee2mqtt/{device.friendly_name}"
            client.subscribe(topic)
            device.last_progress = time.time()
            logger.info(
                f"  {device.friendly_name} is already performing an operation. Subscribed to {topic}"
            )

    if not sent_request:
        init_done_event.set()


def handle_otasuccess(client, obj):
    global otadict
    if obj.get("status") == "error":
        ieee = obj.get("data", {}).get("id")
        logger.error(f"Update error for {ieee}: {obj.get('error')}")
        if ieee and ieee in otadict:
            handle_failed_update(client, otadict[ieee])
    else:
        name = obj.get("data", {}).get("id")
        res = [
            d
            for d in otadict.values()
            if d.friendly_name == name or d.ieee_addr == name
        ]
        if res:
            otacleanup(client, res[0])


def handle_failed_update(client, dev: OtaDevice):
    global currently_updating
    logger.warning(f"Update failed for {dev.friendly_name}")
    dev.updating = False
    if dev.ieee_addr in currently_updating:
        currently_updating.remove(dev.ieee_addr)

    if dev.retries < args.retries:
        dev.retries += 1
        logger.info(
            f"Retrying {dev.friendly_name} (Attempt {dev.retries + 1}/{args.retries + 1})"
        )
    else:
        logger.error(f"Max retries reached for {dev.friendly_name}. Skipping.")
        dev.failed = True
        dev.update_available = False


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

        # Watchdog check
        now = time.time()
        for ieee in list(currently_updating):
            dev = otadict.get(ieee)
            if dev and dev.updating and (now - dev.last_progress > args.timeout):
                logger.error(
                    f"Timeout updating {dev.friendly_name} (no progress for {args.timeout}s)"
                )
                handle_failed_update(client, dev)

        updateable = get_updateable_devices()

        if init_done_event.is_set() and not updateable and not currently_updating:
            break

        # Strictly respect the limit
        current_count = len(currently_updating)
        if updateable and current_count < MAX_CONCURRENT_UPDATES:
            device = updateable.pop(0)
            start_update(client, device)
            sleep(10)
        elif updateable and current_count >= MAX_CONCURRENT_UPDATES:
            logger.debug(
                f"Update queue full ({current_count}/{MAX_CONCURRENT_UPDATES}). Waiting..."
            )

        sleep(1)

except KeyboardInterrupt:
    logger.info("Aborted by user")

client.loop_stop()
logger.info("Finished updating")
