"""Run one real desktop sync cycle for the integration tests.

This is not a sync engine. It calls the production ``freecloud_cli`` functions
in the same order the desktop GUI worker uses (scan -> publish complete
manifest -> process reconciliation jobs -> republish after installs), against
a disposable test server. The caller isolates each simulated device by setting
XDG_CONFIG_HOME / XDG_STATE_HOME / HOME before starting this process, so the
device identity and state never touch the real user profile.

Usage: sync_device_driver.py BASE_URL PASSWORD LOCAL_ROOT [--rounds N] [--no-publish]
Prints one JSON object describing the cycle.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import freecloud_cli as cli  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("password")
    parser.add_argument("local_root")
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()

    for guarded in (cli.CONFIG_DIR, cli.STATE_DIR, Path(args.local_root).resolve()):
        # Refuse to run against a real profile or folder; the test harness
        # always places every simulated device under a freecloud-it-* tempdir.
        if "freecloud-it-" not in str(guarded):
            raise SystemExit(f"Refusing to use a non-test location: {guarded}")

    root = Path(args.local_root)
    client = cli.FreeCloudClient(args.base_url, args.password)
    totals = {"installed": 0, "uploaded": 0, "conflicts": 0, "failed": 0, "jobs": 0}
    device = cli.register_desktop_device(client)
    device_id = str(device["device_id"])
    if not args.no_publish:
        cli.publish_desktop_manifest(client, root)
    rounds = 0
    for rounds in range(1, args.rounds + 1):
        counts = cli.process_desktop_reconciliation(client, device_id, root)
        for key in totals:
            totals[key] += int(counts.get(key, 0))
        if counts.get("installed", 0) or counts.get("uploaded", 0):
            if not args.no_publish:
                cli.publish_desktop_manifest(client, root)
            continue
        if not counts.get("jobs", 0):
            break
    print(json.dumps({"device_id": device_id, "rounds": rounds, **totals}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
