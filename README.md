# Zigbee2MQTT Sequential OTA-Updater

A robust CLI tool to automate and track OTA updates for your Zigbee network. Designed to handle massive networks by processing updates sequentially while strictly respecting concurrency limits.

## Features

- **Automated Scanning:** Automatically checks all OTA-supported devices for available updates on startup.
- **Concurrent Updates:** Control how many devices update simultaneously with `--max-concurrent`.
- **Intelligent Tracking:** Detects and tracks updates already in progress (e.g., started via the Z2M UI).
- **Watchdog Timer:** Automatically detects and recovers from stalled updates (no progress for a defined period).
- **Automatic Retries:** Retries failed updates up to a configurable limit.
- **Modern CLI:** Full support for command-line arguments and environment variables.
- **Real-time Feedback:** Shows detailed progress percentages and estimated remaining time.

## Prerequisites

- Python 3.x
- Access to a Zigbee2MQTT MQTT broker.

## Setup

It is recommended to use the shared virtual environment located in the parent directory:

```bash
# Install dependencies (if not already done)
../venv/bin/pip install -r requirements.txt
```

## Configuration

You can configure the updater via CLI arguments or environment variables. CLI arguments take precedence.

| Argument | Environment Variable | Default | Description |
|----------|----------------------|---------|-------------|
| `--host` | `MQTT_SERVER` | `127.0.0.1` | MQTT Broker hostname or IP. |
| `--port` | `MQTT_PORT` | `1883` | MQTT Broker port. |
| `--user` | `MQTT_USER` | (None) | MQTT Username. |
| `--password` | `MQTT_PASSWORD` | (None) | MQTT Password. |
| `--max-concurrent` | `MAX_CONCURRENT_UPDATES` | `1` | Max simultaneous updates. |
| `--timeout` | (None) | `1800` | Seconds of no progress before timing out. |
| `--retries` | (None) | `3` | Max retry attempts per device. |
| `--dry-run` | (None) | `False` | Check for updates without starting them. |

## Usage

Run the updater with your broker details:

```bash
../venv/bin/python main.py --host <broker_ip> --user <username> --password <password>
```

### Advanced Examples

**Run with 2 concurrent updates and a 10-minute progress timeout:**
```bash
../venv/bin/python main.py --host <broker_ip> --max-concurrent 2 --timeout 600
```

**Dry run to see what needs updating:**
```bash
../venv/bin/python main.py --host <broker_ip> --dry-run
```

## Reliability

The script is designed to be restarted at any time. On startup, it will:
1. Fetch the current device list.
2. Identify devices that already have an `available` update.
3. Identify devices currently in the `updating` state and resume tracking them immediately.
4. Check all other OTA-capable devices for new updates.
