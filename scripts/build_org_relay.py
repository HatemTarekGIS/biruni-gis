"""Build app/plugins/biruni/field_photos/org_relay_worker.js from relay/hub.js and relay/worker.js.

Run:  engine\\bin\\python-qgis.bat scripts\\build_org_relay.py
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))

from biruni.field_photos import org_relay  # noqa: E402

HUB_PATH = os.path.join(ROOT, "relay", "hub.js")
WORKER_PATH = os.path.join(ROOT, "relay", "worker.js")
OUT_PATH = os.path.join(ROOT, "app", "plugins", "biruni", "field_photos", "org_relay_worker.js")


def main():
    with open(HUB_PATH, "r", encoding="utf-8") as f:
        hub_src = f.read()
    with open(WORKER_PATH, "r", encoding="utf-8") as f:
        worker_src = f.read()
    bundled = org_relay.bundle(hub_src, worker_src)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(bundled)
    print(f"wrote {OUT_PATH} ({len(bundled)} bytes)")


if __name__ == "__main__":
    main()
