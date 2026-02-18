# Zigbee2MQTT OTA Updater

Automate and track OTA updates for all supported devices in Zigbee2MQTT.

## Development Rules

- **Formatting:** Use `black` for all Python code formatting.
- **Environment:** Use the shared virtual environment located in the parent directory: `../venv/`.
- **Tracking:** Keep the `TODO.md` file updated with progress and remaining tasks.

## Key Logic

- **Client:** Supports both Paho MQTT v1 and v2 APIs.
- **Initialization:** Detects devices already in `updating` or `available` states from the initial device list.
- **Concurrency:** Strictly adheres to the `MAX_CONCURRENT_UPDATES` limit.
- **Reliability:** Includes a watchdog timer for stalled updates and retry logic for failures.
