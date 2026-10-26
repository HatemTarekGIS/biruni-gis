r"""Tests for phone GPS through the phone's browser (HTTPS page + QR code).

Run:  engine\bin\python-qgis.bat tests\test_gps_browser.py
"""
import json
import os
import ssl
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.append(os.path.join(HERE, "..", "app", "python"))

from biruni.gps import browser  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


folder = tempfile.mkdtemp()
texts = {"title": "T", "intro": "I", "start": "S", "stop": "X", "sent": "n", "lost": "l", "denied": "d",
         "insecure": "i"}
srv = browser.BrowserServer(folder, texts, "ar", port=0 or 18111, addresses=["192.168.1.33"])
cert, key = browser.cert_paths(folder)
check(os.path.exists(cert) and os.path.exists(key), "self-signed certificate created")
from cryptography import x509  # noqa: E402

crt = x509.load_pem_x509_certificate(open(cert, "rb").read())
ips = [str(i) for i in crt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
    x509.IPAddress)]
check("192.168.1.33" in ips and "127.0.0.1" in ips, f"certificate covers the local addresses {ips}")
mtime = os.path.getmtime(cert)
browser.ensure_certificate(folder, ["192.168.1.33"])
check(os.path.getmtime(cert) == mtime, "certificate reused when addresses are unchanged")

ctx = ssl._create_unverified_context()
base = f"https://127.0.0.1:{srv.port}"
page = urllib.request.urlopen(f"{base}/{srv.token}/", context=ctx).read().decode("utf-8")
check('dir="rtl"' in page and "watchPosition" in page, "location page served over HTTPS (RTL in Arabic)")
try:
    urllib.request.urlopen(f"{base}/wrongtoken/", context=ctx)
    check(False, "wrong token refused")
except urllib.error.HTTPError as e:
    check(e.code == 404, "wrong token refused")
body = json.dumps({"lat": 30.0444, "lon": 31.2357, "acc": 8, "alt": 22.5, "speed": 1.5, "heading": 90,
                   "t": 1792900000000}).encode()
req = urllib.request.Request(f"{base}/{srv.token}/fix", data=body, headers={"Content-Type": "application/json"})
check(urllib.request.urlopen(req, context=ctx).status == 204, "position accepted")
fix = srv.positions.get(timeout=2)
check(abs(fix.lat - 30.0444) < 1e-9 and fix.accuracy_m == 8 and abs(fix.speed_kmh - 5.4) < 1e-9 and fix.valid,
      "position converted to a GPS fix (accuracy, speed in km/h)")
bad = urllib.request.Request(f"{base}/{srv.token}/fix", data=b'{"lat": 999, "lon": 0}')
try:
    urllib.request.urlopen(bad, context=ctx)
    check(False, "impossible position rejected")
except urllib.error.HTTPError as e:
    check(e.code == 400, "impossible position rejected")
srv.stop()
png = browser.qr_png(srv.url("192.168.1.33"))
check(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 300, "QR code image made")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
