
from datetime import datetime, timezone

#!/usr/bin/env python3
import argparse
import fnmatch
import json
import logging
import os
import random
import re
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
    "--host", default=os.getenv("MQTT_HOST", "127.0.0.1"), help="MQTT Server"
)
parser.add_argument(
    "--port", type=int, default=int(os.getenv("MQTT_PORT", 1883)), help="MQTT Port"
)
parser.add_argument("--user", default=os.getenv("MQTT_USER", ""), help="MQTT User")
parser.add_argument(
    "--password", default=os.getenv("MQTT_PASSWORD", ""), help="MQTT Password"
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
parser.add_argument(
    "--shuffle",
    action="store_true",
    help="Shuffle the order of updates once the list is made",
)
parser.add_argument(
    "--max-offline-hours",
    type=float,
    default=float(os.getenv("MAX_OFFLINE_HOURS", 1.0)),
    help="Max hours a device can be offline before skipping (default: 1.0 hour)",
)
parser.add_argument(
    "--exclude",
    action="append",
    default=[],
    metavar="PATTERN",
    help="Skip devices matching PATTERN (friendly_name or ieee_addr). "
    "Glob by default (e.g. 'Kitchen *'), or regex via i/<exp>/ "
    "(case-insensitive). Repeatable.",
)
args = parser.parse_args()


def _compile_exclude_matcher(pattern):
    m = re.match(r"^i/(.*)/$", pattern)
    if m:
        rx = re.compile(m.group(1), re.IGNORECASE)
        return rx.search
    return lambda s: fnmatch.fnmatchcase(s, pattern)


EXCLUDE_MATCHERS = [_compile_exclude_matcher(p) for p in args.exclude]


def is_excluded(friendly_name, ieee_addr):
    return any(
        m(friendly_name) or m(ieee_addr) for m in EXCLUDE_MATCHERS
    )


from datetime import datetime, timezone

def is_online(ieee_addr):
    max_offline_seconds = args.max_offline_hours * 3600
    try:
        with open("/data/config/z2m/data/state.json") as f:
            st = json.load(f)
        dev_st = st.get(ieee_addr, {})
        last_seen = dev_st.get("last_seen")
        if not last_seen:
            return False
        dt = datetime.fromisoformat(last_seen)
        if dt.tzinfo is None:
            # advanced.last_seen is ISO_8601_local in this deployment, so a
            # naive timestamp here means local system time, not UTC.
            dt = dt.astimezone()
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() < max_offline_seconds
    except Exception as e:
        logger.debug(f"Error checking online status for {ieee_addr}: {e}")
        return True


if args.host == "hostname/ip":
    logger.error("Please configure your MQTT_HOST (via env var or --host).")
    sys.exit(1)

MQTT_HOST = args.host
MQTT_PORT = args.port
MQTT_USER = args.user
MQTT_PASSWORD = args.password
MAX_CONCURRENT_UPDATES = args.max_concurrent

# Global State
otadict = {}
currently_updating = []
cooldown_until = 0
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
    last_state: str = 'unknown'
    manufacturer: str = ""

    @property
    def is_inovelli(self) -> bool:
        return "inovelli" in self.manufacturer.lower()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe("zigbee2mqtt/bridge/devices")
        client.subscribe("zigbee2mqtt/bridge/response/device/ota_update/check")
        client.subscribe("zigbee2mqtt/bridge/response/device/ota_update/update")
        client.subscribe("zigbee2mqtt/+")
        # zigbee2mqtt/+ above already covers every device's topic (no friendly_name
        # contains "/") - per-device subscribe() calls were redundant and, since they
        # ran synchronously from inside on_message elsewhere, could self-deadlock
        # paho-mqtt's network thread. Removed; do not re-add.
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
            logger.debug(f"Received update message for {msg.topic}: {obj['update']}")
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
        res = [
            d for d in otadict.values() if d.updating and d.friendly_name == device_fn
        ]
        if res:
            dev = res[0]
            dev.last_progress = time.time()
            current_p = float(progress)
            if abs(current_p - dev.last_progress_val if hasattr(dev, "last_progress_val") else -1) >= 0.01:
                dev.last_progress_val = current_p
                try:
                    msg = f"Updating {device_fn} - {current_p:6.2f}%"
                    if remaining is not None:
                        remaining_time = timedelta(seconds=int(remaining))
                        msg += f", {remaining_time} remaining"
                    if current_p >= 100.0:
                        msg += " (Awaiting switch reboot & verification)"
                    logger.info(msg)
                except (ValueError, TypeError):
                    pass

    if state:
        res = [d for d in otadict.values() if d.friendly_name == device_fn]
        if res:
            dev = res[0]
            if dev.last_state != state:
                old_s = dev.last_state
                dev.last_state = state
                logger.info(f"Update status for {device_fn} {old_s} -> {state}")

        if state == "idle":
            r = [
                d
                for d in otadict.values()
                if d.updating and d.friendly_name == device_fn
            ]
            if r:
                otacleanup(client, r[0])
        elif state == "updating":
            res = [
                d
                for d in otadict.values()
                if d.updating and d.friendly_name == device_fn
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
                manufacturer=device["definition"].get("vendor", ""),
            )

            if is_excluded(dev.friendly_name, dev.ieee_addr):
                logger.info(f"  {dev.friendly_name} excluded by --exclude, skipping")
                continue

            if dev.supports_ota:
                # Detect existing update state FIRST, straight from data z2m
                # has already published - no new query is sent, so this is
                # safe regardless of last_seen. OTA block-transfer traffic
                # never refreshes last_seen, so a transfer running past
                # --max-offline-hours would otherwise look "offline" here
                # and get silently dropped from tracking entirely.
                already_handled = False
                if "update" in device:
                    u_obj = device["update"]
                    u_state = u_obj.get("state")
                    u_available = (
                        u_obj.get("update_available")
                        or u_obj.get("available")
                        or (u_state == "available")
                    )

                    if u_state == "updating":
                        dev.updating = True
                        dev.update_available = True
                        already_handled = True
                        if dev.ieee_addr not in currently_updating:
                            currently_updating.append(dev.ieee_addr)

                        logger.info(
                            f"  {dev.friendly_name} is already updating (covered by zigbee2mqtt/+)"
                        )
                        dev.last_progress = time.time()
                    elif u_available:
                        dev.update_available = True
                        already_handled = True
                        logger.info(
                            f"  {dev.friendly_name} has an update available (from update object)."
                        )

                # Only devices that still need a fresh OTA check go through
                # the offline gate - that's the only path that sends a live
                # query to the device, and querying a genuinely unresponsive
                # device risks an unfulfilled promise in z2m that can crash
                # (and restart) the whole z2m process, dropping every other
                # in-flight transfer with it.
                if not already_handled and not is_online(dev.ieee_addr):
                    logger.warning(f"  Skipping {dev.friendly_name} (OFFLINE: last seen > {args.max_offline_hours}h ago)")
                    continue

                otadict[dev.ieee_addr] = dev
                num_total += 1
                if already_handled:
                    logger.debug(
                        f"  {dev.friendly_name} skip initial check, state handled."
                    )
                else:
                    logger.info(
                        f"  {dev.friendly_name} supports OTA Updates (added to queue)"
                    )

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
            device.last_progress = time.time()
            logger.info(
                f"  {device.friendly_name} is already performing an operation (covered by zigbee2mqtt/+)"
            )

    if not sent_request:
        init_done_event.set()


def handle_otasuccess(client, obj):
    global otadict
    if obj.get("status") == "error":
        data = obj.get("data")
        ieee = data.get("id") if isinstance(data, dict) else None
        if not ieee:
            error_msg = str(obj.get("error", ""))
            for marker in ["Update of '", "for '"]:
                if marker in error_msg:
                    try:
                        ieee = error_msg.split(marker)[1].split("'")[0]
                        break
                    except IndexError:
                        pass
        logger.error(f"Update error for {ieee or 'unknown'}: {obj.get('error')}")
        dev = None
        if ieee:
            res = [d for d in otadict.values() if d.friendly_name == ieee or d.ieee_addr == ieee]
            if res:
                dev = res[0]
        if dev:
            handle_failed_update(client, dev)
        elif currently_updating:
            logger.warning("Unmatched update error, clearing oldest active updating device.")
            oldest_ieee = currently_updating[0]
            if oldest_ieee in otadict:
                handle_failed_update(client, otadict[oldest_ieee])
    else:
        data = obj.get("data")
        name = data.get("id") if isinstance(data, dict) else None
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
    try:
        if dev.is_inovelli:
            logger.info(
                f"Post-update: Restoring periodicPowerAndEnergyReports=15 for {dev.friendly_name}..."
            )
            client.publish(
                f"zigbee2mqtt/{dev.friendly_name}/set",
                payload=json.dumps({"periodicPowerAndEnergyReports": 15}),
            )
            sleep(2)
        client.publish(
            "zigbee2mqtt/bridge/request/device/ota_update/check",
            payload=json.dumps({"id": dev.ieee_addr}),
        )
    except Exception as e:
        logger.warning(f"Failed post-update restore/refresh for {dev.friendly_name}: {e}")

    global cooldown_until
    cooldown_until = time.time() + 30
    logger.info(
        f"Update for {dev.friendly_name} finished - {len(get_updateable_devices())} more updates to go. Network cooling down for 30s (non-blocking)..."
    )
    # No unsubscribe: zigbee2mqtt/+ (subscribed once in on_connect) is what's
    # actually delivering these messages. Unsubscribing the exact-topic string
    # here never touched that wildcard subscription, so this was a no-op that
    # looked like cleanup - removed rather than leave misleading dead code.


def check_for_update(client, device: OtaDevice):
    global sent_request
    if currently_updating:
        logger.debug(f"Skipping update check for {device.friendly_name} while another device is updating.")
        return
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

    if device.is_inovelli:
        logger.info(f"Pre-flight: Setting periodicPowerAndEnergyReports=600 for {device.friendly_name}")
        try:
            client.publish(
                f"zigbee2mqtt/{device.friendly_name}/set",
                payload=json.dumps({"periodicPowerAndEnergyReports": 600}),
            )
            sleep(2)
        except Exception as e:
            logger.debug(f"Could not set periodicPowerAndEnergyReports for {device.friendly_name}: {e}")

    logger.info(f"Starting Update for {device.friendly_name}")
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
    client.connect(MQTT_HOST, MQTT_PORT, 60)
except Exception as e:
    logger.error(f"Could not connect to MQTT broker: {e}")
    sys.exit(1)

client.loop_start()

if not init_done_event.wait(timeout=60):
    logger.warning("Initialization timed out. Some devices might not have responded.")

logger.info("Finished initialization. Passive listening for 30s before checking for available updates...")
sleep(30)
if currently_updating:
    logger.info(f"Active update detected on startup for {currently_updating}. Suppressing update checks.")
else:
    logger.info("No active updates running. Checking device update availability sequentially...")
    for dev in list(otadict.values()):
        if currently_updating:
            break
        if dev.supports_ota and not dev.checked_for_update and not dev.update_available:
            check_for_update(client, dev)
            sleep(1)

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
        if args.shuffle:
            random.shuffle(updateable)

        if init_done_event.is_set() and not updateable and not currently_updating:
            logger.info("No active updates in queue. Waiting 30s for next available device...")
            sleep(30)
            continue

        # Strictly respect the limit
        if time.time() < cooldown_until:
            sleep(1)
            continue

        current_count = len(currently_updating)
        if updateable and current_count < MAX_CONCURRENT_UPDATES:
            device = updateable.pop(0)
            start_update(client, device)
        elif updateable and current_count >= MAX_CONCURRENT_UPDATES:
            logger.debug(
                f"Update queue full ({current_count}/{MAX_CONCURRENT_UPDATES}). Waiting..."
            )

        sleep(1)

except KeyboardInterrupt:
    logger.info("Aborted by user")

client.loop_stop()
logger.info("Finished updating")