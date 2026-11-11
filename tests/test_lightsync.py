"""Light Sync: bytes through an animated QR code - the fountain code, the frames, the QR round trip
through (blurred, shrunk) pictures, the file packing and the two windows (offscreen, no camera).

Run:  engine\\bin\\python-qgis.bat tests\\test_lightsync.py
"""
import os
import random
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

import numpy as np  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QImage  # noqa: E402

from biruni.lightsync import files, fountain, frames, ui  # noqa: E402
from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- the fountain code ---------------------------------------------------------------------------------
def run(data, block, loss, start, seed, shuffle=False):
    enc = fountain.Encoder(data, block)
    dec = fountain.Decoder(enc.k, block, len(data))
    rnd = random.Random(seed)
    i, got = start, 0
    if shuffle:   # pieces in a random order, 30 % of them lost
        order = list(range(start, start + enc.k * 3))
        rnd.shuffle(order)
        for i in order:
            if rnd.random() < loss:
                continue
            dec.add(i, enc.piece(i))
            got += 1
            if dec.done:
                break
    else:
        while not dec.done and got < enc.k * 3:
            if rnd.random() >= loss:
                dec.add(i, enc.piece(i))
                got += 1
            i += 1
    return dec.data() == data, got / enc.k


worst = 0.0
for n in (1, 900, 5000, 100_000, 600_000):
    data = os.urandom(n)
    for loss, start, shuffle in ((0.0, 0, False), (0.3, 0, False), (0.3, 0, True), (0.0, 10**6, False)):
        ok_all, ovs = True, []
        for t in range(3):
            ok, ov = run(data, 1000, loss, start + t * 7919, t, shuffle)
            ok_all &= ok
            ovs.append(ov)
        if n >= 100_000:
            worst = max(worst, max(ovs))
        check(ok_all, f"fountain {n} bytes, loss {loss:.0%}, start {start}, shuffled {shuffle}: whole "
                      f"(pieces/blocks {', '.join(f'{o:.3f}' for o in ovs)})")
check(worst <= 1.10, f"for 100+ blocks at most 10 % more pieces than blocks (worst {worst:.3f})")
check(fountain.blocks_of(123456, 777) == fountain.blocks_of(123456, 777), "the pieces are the same every time")
check(fountain.blocks_of(5, 100) == [5], "the first pass is the blocks themselves")

# ---- frames --------------------------------------------------------------------------------------------
payload = os.urandom(23_456)
snd = frames.Sender(payload, 1000)
raw = snd.frame(0)
f = frames.unpack(raw)
check(f is not None and f.k == snd.k and f.length == len(payload), "a frame reads back")
bad = bytearray(raw)
bad[40] ^= 1
check(frames.unpack(bytes(bad)) is None, "a damaged frame is dropped (CRC)")
check(frames.unpack(b"hello world, a QR code of something else") is None, "a foreign QR code is dropped")

rx = frames.Receiver()
other = frames.Sender(os.urandom(5000), 1000)
for i in range(0, 60):              # the receiver keeps to the first transfer it saw
    if rx.feed(snd.frame(i)):
        break
    if i % 3 == 0:
        rx.feed(other.frame(i))           # another transfer on a second screen in view
check(rx.result == payload, "the payload is whole and checked")
check(rx.foreign > 0, f"frames of another transfer are ignored ({rx.foreign})")

# a piece damaged in a way the CRC does not catch (forged with a right CRC): the sha256 check refuses it
rx = frames.Receiver()
for i in range(snd.k):
    r = snd.frame(i)
    if i == 3:
        g = frames.unpack(r)
        g.piece = bytes(b ^ 0x55 for b in g.piece)
        r = frames.pack(g)
    rx.feed(r)
check(rx.result is None and rx.error == "checksum", "a wrong piece is detected by the payload checksum")
for i in range(snd.k, snd.k * 3):
    if rx.feed(snd.frame(i)):
        break
check(rx.result == payload, "after a checksum failure the transfer starts again and finishes")


# ---- QR round trip through pictures --------------------------------------------------------------------
def blur(img, r=1):
    a = img.astype(np.float32)
    out = np.zeros_like(a)
    n = 0
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out += np.roll(np.roll(a, dy, 0), dx, 1)
            n += 1
    return (out / n).astype(np.uint8)


def shrink(img, factor):
    q = ui._qimage(img)
    q = q.scaled(int(q.width() * factor), int(q.height() * factor), Qt.AspectRatioMode.KeepAspectRatio,
                 Qt.TransformationMode.SmoothTransformation)
    return frames.gray_of_qimage(q)


def camera_like(img, rnd):
    """A camera picture of the screen: blurred, smaller, darker, with noise, in a larger grey scene."""
    a = shrink(blur(img, 1), 0.8)
    a = (a.astype(np.float32) * 0.75 + 30 + rnd.normal(0, 6, a.shape)).clip(0, 255).astype(np.uint8)
    scene = np.full((a.shape[0] + 200, a.shape[1] + 300), 90, np.uint8)
    scene[90:90 + a.shape[0], 140:140 + a.shape[1]] = a
    return scene


rnd = np.random.default_rng(1)
for key, (block, fps) in frames.PRESETS.items():
    data = os.urandom(block * 12 + 77)
    s = frames.Sender(data, block)
    r = frames.Receiver()
    t_make = t_read = 0.0
    n = 0
    read_fail = 0
    for i in range(s.k * 3):
        t0 = time.perf_counter()
        img = frames.qr_gray(s.frame(), scale=3, border=4)
        t1 = time.perf_counter()
        codes = frames.read_codes(camera_like(img, rnd))
        t2 = time.perf_counter()
        t_make += t1 - t0
        t_read += t2 - t1
        n += 1
        if not codes:
            read_fail += 1
        if any(r.feed(c) for c in codes):
            break
    check(r.result == data, f"preset {key}: {s.k} blocks through blurred, shrunk, noisy pictures in {n} frames "
                            f"({read_fail} unread)")
    print(f"  {key}: block {block} B at {fps} fps = {block * fps / 1024:.1f} KB/s at best; frame made in "
          f"{t_make / n * 1000:.1f} ms, read in {t_read / n * 1000:.1f} ms; QR version "
          f"{(frames.qr_matrix(s.frame()).shape[0] - 17) // 4}",
          flush=True)

# ---- files ---------------------------------------------------------------------------------------------
content = os.urandom(3000) + b"parcel " * 2000
p = files.pack("../../evil\\plan.dxf", content)
check(files.unpack(p) == ("plan.dxf", content), "a file packs and unpacks; its name stays a plain file name")
check(len(p) < len(content), "a file is deflated when that is smaller")
ps = files.pack("plan.dxf", content, "correct horse")
check(files.sealed(ps) and b"parcel" not in ps, "a sealed file shows nothing of its content")
try:
    files.unpack(ps, "wrong")
    check(False, "a wrong passphrase is refused")
except files.FileError as e:
    check(str(e) == "passphrase", "a wrong passphrase is refused")
check(files.unpack(ps, "correct horse") == ("plan.dxf", content), "the right passphrase opens it")
broken = bytearray(p)
broken[-5] ^= 0xFF
try:
    files.unpack(bytes(broken))
    check(False, "a damaged file is refused")
except files.FileError:
    check(True, "a damaged file is refused")

# ---- the windows, offscreen, without a camera ----------------------------------------------------------
note = "سلام عليكم - Light Sync ".encode("utf-8") * 300 + os.urandom(20000)
data = files.pack("note.txt", note)
sd = ui.SendDialog(None, data, "test")
sd.resize(700, 760)
rd = ui.ReceiveDialog(None, camera=False)
done = False
for i in range(400):
    q = sd.frame_image()
    big = q.scaled(q.width() * 3, q.height() * 3)
    done = rd.feed_image(frames.gray_of_qimage(big), preview=big)
    if done:
        break
check(done and rd.result == data, f"the send window's frames are read by the receive window ({i + 1} frames)")
sd.tick()
check(sd.code.pixmap() is not None and not sd.code.pixmap().isNull(), "the send window draws its code")
sd.speed.setCurrentIndex(sd.speed.findData("weak"))
check(sd.sender_.block == frames.PRESETS["weak"][0], "a changed speed restarts with its block size")
sd.done(0)
check(ui.open_received(None, data) == ("note.txt", note),
      "the received file opens")
check(ui._multimedia() is not None, "Qt Multimedia is there for the camera")
check("{n}" in ui.tr("{n} bytes"), "the texts keep their placeholders")

# ---- speed ---------------------------------------------------------------------------------------------
big = os.urandom(2 * 2**20)
t0 = time.perf_counter()
s = frames.Sender(big, 1000)
r = frames.Receiver()
worst_add = last = 0.0
i = 10**6   # repair pieces only (the hardest case: a receiver that missed the whole first pass)
while r.result is None:
    raw = s.frame(i)
    t = time.perf_counter()
    finished = r.feed(raw)
    took1 = time.perf_counter() - t
    if finished:
        last = took1                      # the back-substitution, once at the end
    else:
        worst_add = max(worst_add, took1)
    i += 1
took = time.perf_counter() - t0
check(r.result == big, f"2 MB, repair pieces only: {i - 10**6} frames for {s.k} blocks "
                       f"(+{(i - 10**6) / s.k - 1:.1%})")
print(f"  2 MB coded and decoded in {took:.1f} s on this computer; slowest frame {worst_add * 1000:.0f} ms, the last "
      f"(back-substitution) {last * 1000:.0f} ms "
      f"(a camera frame comes every 100 ms); at the normal speed the screen needs about "
      f"{frames.seconds_for(len(big), 1000, 10) / 60:.1f} min", flush=True)
check(worst_add < 0.1, "every frame is decoded faster than the next one comes")
check(last < 1.5, "the last step takes under a second and a half")

sd.deleteLater()
rd.deleteLater()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED: " + "; ".join(failures)))
app.exitQgis()
sys.exit(1 if failures else 0)
