"""The date of a certificate: an OpenTimestamps stamp, made, completed and checked (calendars and block explorer
played by this test, nothing sent anywhere).

Run:  engine\\bin\\python-qgis.bat tests\\test_stamp.py
"""
import hashlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.fingerprint import stamp as st  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


class World:
    """Two calendars, one of them down, and a Bitcoin block explorer."""

    def __init__(self):
        self.sent = []
        self.done = False
        self.root_of = {}         # calendar commitment -> merkle root it ends in
        self.header = None

    def __call__(self, url, data=None):
        self.sent.append((url, data))
        if url.startswith("https://alice") and url.endswith("/digest"):
            t = st.Timestamp(data)
            p = t.op(st.PREPEND, b"calendar-salt").op(st.SHA256)
            uri = b"https://alice.btc.calendar.opentimestamps.org"
            p.atts.append((st.PENDING, bytes([len(uri)]) + uri))
            self.root_of[p.msg] = None
            w = io.BytesIO()
            st.write_timestamp(w, t)
            return w.getvalue()
        if url.startswith("https://bob"):
            return None
        if "/timestamp/" in url:
            if not self.done:
                return None
            msg = bytes.fromhex(url.rsplit("/", 1)[1])
            t = st.Timestamp(msg)
            leaf = t.op(st.APPEND, b"\x11" * 32).op(st.SHA256).op(st.PREPEND, b"\x22" * 32).op(st.SHA256)
            leaf.atts.append((st.BITCOIN, bytes([0x80, 0xea, 0x30])))    # height 800,000 (varuint)
            self.header = b"\x00" * 36 + leaf.msg + (1690000000).to_bytes(4, "little") + b"\x00" * 8
            w = io.BytesIO()
            st.write_timestamp(w, t)
            return w.getvalue()
        if url.endswith("/block-height/800000"):
            return b"00000000000000000002a7c4c1e48d76c5a37902165a270156b7a8d72728a054"
        if url.endswith("/header") and self.header is not None:
            return self.header.hex().encode()
        return None


root = tempfile.mkdtemp()
cert = os.path.join(root, "delivery.biruni-proof.json")
with open(cert, "w", encoding="utf-8") as f:
    f.write('{"kind": "biruni-fingerprint", "code": "ABCD-EFGH-IJKL-MNOP"}')
world = World()
took = st.stamp(cert, fetch=world)
check(took == 1 and os.path.exists(cert + ".ots"), "stamped by the calendar that answered (the other down)")
sent = [d for u, d in world.sent if u.endswith("/digest")]
digest = hashlib.sha256(open(cert, "rb").read()).digest()
check(all(len(d) == 32 and d != digest for d in sent), "only 32 bytes sent, not even the certificate's own hash")
data = open(cert + ".ots", "rb").read()
d, t = st.read_file(data)
check(d == digest and st.write_file(d, t) == data, "the stamp file reads back to the same bytes")
check(data.startswith(st.MAGIC), "in the standard OpenTimestamps format")

r = st.verify(cert, fetch=world)
check(not r.ok and r.why == "pending" and r.pending, "checked at once: pending, a calendar still to answer")
world.done = True
r = st.verify(cert, fetch=world)
check(r.ok and r.height == 800000 and r.time.year == 2023 and r.upgraded,
      f"hours later: completed and checked against the block ({r.height}, {r.time})")
r = st.verify(cert, fetch=lambda u, d=None: world(u, d) if "/block" in u else None)
check(r.ok and not r.upgraded, "completed once, kept in the file: checked again without the calendar")

with open(cert, "a", encoding="utf-8") as f:
    f.write(" ")
r = st.verify(cert, fetch=world)
check(not r.ok and r.why == "other file", "the certificate changed by one character: not the stamped one")

world.header = b"\x00" * 80
open(cert, "w", encoding="utf-8").write('{"kind": "biruni-fingerprint", "code": "ABCD-EFGH-IJKL-MNOP"}')
r = st.verify(cert, fetch=world)
check(not r.ok and r.why == "block", "a block that does not hold the path: refused")
r = st.verify(cert, fetch=lambda u, d=None: None)
check(not r.ok and r.why == "offline", "no network: said so, not refused")
open(cert + ".ots", "wb").write(b"junk")
check(st.verify(cert, fetch=world).why == "format", "not a stamp: said so")
try:
    st.stamp(cert, fetch=lambda u, d=None: None)
    check(False, "stamped with no calendar")
except st.StampError as e:
    check(str(e) == "offline", "no calendar reached: said so")

# ---- the buttons ----------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class Bar:
    def __init__(self):
        self.said = []

    def pushMessage(self, title, text, *a, **k):
        self.said.append(text)


class Iface:
    def __init__(self):
        self.w, self.bar = QWidget(), Bar()

    def mainWindow(self):
        return self.w

    def messageBar(self):
        return self.bar


class Plugin:
    iface = Iface()


from biruni.fingerprint import stamp_ui  # noqa: E402

open(cert, "w", encoding="utf-8").write('{"kind": "biruni-fingerprint", "code": "QRST-UVWX-YZ23-4567"}')
w2 = World()
plug = Plugin()
check(stamp_ui.stamp_ui(plug, cert, ask=False, fetch=w2) == 1 and "Stamped by 1" in plug.iface.bar.said[-1],
      "stamped from the button: " + plug.iface.bar.said[-1][:60])
r = stamp_ui.check_ui(plug, cert + ".ots", quiet=True, fetch=w2)
check(not r.ok and "few hours" in r.text, "checked from the button at once: " + r.text[:50])
w2.done = True
r = stamp_ui.check_ui(plug, cert, quiet=True, fetch=w2)
check(r.ok and "block 800000" in r.text, "checked from the button later: " + r.text[:70])

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
