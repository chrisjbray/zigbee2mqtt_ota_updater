# TODO: Improve zigbee2mqtt_ota_updater

- [x] Modernize script (Paho v2, CLI args, structured logging)
- [x] Replace busy-wait loops with events
- [x] Implement robust update detection and tracking
- [x] Strictly enforce concurrency limits
- [x] Add watchdog and retry logic
- [x] Setup project tracking and infrastructure
- [x] Add `--shuffle` argument to randomize update order
- [x] Add pre-flight mesh stabilization delay and post-update cooling period
- [x] Filter out offline devices before checking for updates
- [x] Add --max-offline-hours CLI argument (defaults to 1.0 hour)
- [x] Add post-update ZCL re-configuration and cache refresh in otacleanup()
- [x] Make main loop run continuously as a daemon instead of exiting when queue is temporarily empty
- [x] Subscribe to wildcard device topic zigbee2mqtt/+ to capture all device progress updates
- [x] Apply `black` to `main.py`. Despite `AGENTS.md` requiring it, the file had
      never actually been formatted - it was not clean at any width from 79 to
      160. Now clean at the default 88, and `AGENTS.md` states that width
