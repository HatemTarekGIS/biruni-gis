"""Python libraries bundled with Biruni GIS in <app>/python (cryptography for
the phone page certificate, segno for QR codes). The folder is appended to
sys.path, so the engine's own packages keep priority."""
import os
import sys

VENDOR = os.path.join(os.environ.get("BIRUNI_HOME") or os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")), "python")


def ensure_vendor():
    if os.path.isdir(VENDOR) and VENDOR not in sys.path:
        sys.path.append(VENDOR)
