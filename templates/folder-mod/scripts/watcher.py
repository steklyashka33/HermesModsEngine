#!/usr/bin/env python3
"""Example background monitor daemon for Hermes Folder Mod."""
import os
import sys
import time


def main() -> None:
    mod_id = os.environ.get("HERMES_MOD_ID", "unknown")
    monitor_id = os.environ.get("HERMES_MONITOR_ID", "watcher")
    data_dir = os.environ.get("HERMES_MOD_DATA", "/tmp")

    sys.stdout.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Started {mod_id}:{monitor_id}\n")
    sys.stdout.flush()

    heartbeat_file = os.path.join(data_dir, f"{monitor_id}_heartbeat.txt")
    with open(heartbeat_file, "w", encoding="utf-8") as f:
        f.write(f"Started at {time.time()}\n")

    sys.stdout.write(f"Heartbeat recorded in {heartbeat_file}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
