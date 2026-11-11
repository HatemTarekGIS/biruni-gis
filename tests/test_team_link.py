r"""A room reached from outside its network without a tunnel: a direct link between the computers (team/link.py).

All on this PC: a Nostr relay in place of the public ones, the host's and the member's hidden pages (their own
processes), the room's own server. Checked: the link carries requests both ways, big ones, many at once (a page's
own requests would be held to six); then a whole room - a member joins by "link:" alone, gets the map, and edits
go both ways.

Run:  engine\bin\python-qgis.bat tests\test_team_link.py
"""
import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
RELAY_PORT = 10169
os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{RELAY_PORT}"
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"
os.environ["BIRUNI_TEAM_NTFY"] = "http://127.0.0.1:9/"     # nothing there: the address is not left anywhere
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from biruni.team import code as codes, link, session, wire  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.03)
    return until()


# ---- a Nostr relay on this PC ----------------------------------------------------------------------------------
class Relay:
    def __init__(self, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.subs, self.events, self.lock = {}, [], threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            c, _ = self.sock.accept()
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        f = c.makefile("rb")
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            b = f.read(1)
            if not b:
                return
            head += b
        k = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(k + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        with self.lock:
            self.subs[c] = []
        try:
            while True:
                b0, b1 = f.read(2)
                n = b1 & 127
                if n == 126:
                    n = struct.unpack(">H", f.read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", f.read(8))[0]
                mask = f.read(4) if b1 & 128 else bytes(4)
                data = bytes(x ^ mask[i % 4] for i, x in enumerate(f.read(n)))
                if b0 & 15 == 8:
                    break
                if b0 & 15 == 1:
                    self._msg(c, json.loads(data.decode("utf-8")))
        except (OSError, ValueError):
            pass
        with self.lock:
            self.subs.pop(c, None)

    def _send(self, c, obj):
        d = json.dumps(obj).encode("utf-8")
        h = bytes([0x81]) + (bytes([len(d)]) if len(d) < 126 else bytes([126]) + struct.pack(">H", len(d))
                             if len(d) < 65536 else bytes([127]) + struct.pack(">Q", len(d)))
        try:
            c.sendall(h + d)
        except OSError:
            pass

    def _msg(self, c, msg):
        if msg[0] == "REQ":
            with self.lock:
                self.subs[c].append((msg[1], set(msg[2].get("#t", []))))
        elif msg[0] == "EVENT":
            ev = msg[1]
            self.events.append(ev)
            self._send(c, ["OK", ev["id"], True, ""])
            topics = {t[1] for t in ev["tags"] if t[0] == "t"}
            with self.lock:
                targets = [(o, sid) for o, subs in self.subs.items() for sid, want in subs if want & topics]
            for o, sid in targets:
                self._send(o, ["EVENT", sid, ev])


relay = Relay(RELAY_PORT)


# ---- 1. the link itself, in front of a stand-in for the room's server ---------------------------------------------
class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/slow":
            time.sleep(2)
        out = b"" if self.path == "/empty" else hashlib.sha256(body).digest() + body[::-1]
        self.send_response(418 if self.path == "/teapot" else 200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


echo = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
echo.daemon_threads = True
threading.Thread(target=echo.serve_forever, daemon=True).start()
room_key = os.urandom(32)
host_link = link.HostLink(link.link_key(room_key), echo.server_address[1])
address = link.member_address(room_key)
check(address.startswith("http://127.0.0.1:") and address.endswith("/r") and link.member_address(room_key) == address,
      "a member's room client gets one address on this computer for the link")
check(link.wait_member_open(room_key, 90), "the member's page and the host's page link up through the relay"
      + " | " + " | ".join(host_link.host.log[-3:] + link._MEMBERS[link.link_key(room_key)].host.log[-3:]))
check(pump(10, lambda: host_link.members() == 1), "the host counts one member on the link")
u = address.split("/", 3)
base = "/" + u[3]


def post(path, body, timeout=60):
    c = http.client.HTTPConnection("127.0.0.1", int(u[2].split(":")[1]), timeout=timeout)
    c.request("POST", base + "/" + path, body=body, headers={"Content-Type": "application/octet-stream"})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


for size in (0, 1, 15999, 16000, 16001, 300000):
    body = os.urandom(size)
    st, out = post("echo", body)
    if not (st == 200 and out == hashlib.sha256(body).digest() + body[::-1]):
        check(False, f"a request of {size} bytes comes back whole ({st}, {len(out)} bytes)")
        break
else:
    check(True, "requests of 0 B to 300 KB go to the host's server and come back whole")
body = os.urandom(5 * 2**20)
t0 = time.time()
st, out = post("echo", body, timeout=120)
check(st == 200 and out[32:] == body[::-1], f"5 MB there and back in {time.time() - t0:.1f} s (a piece of the map)")
check(post("teapot", b"x")[0] == 418 and post("empty", b"x") == (200, b""), "the host's answer comes back as it is, status and all")
check(post("Bad-Path", b"x")[0] == 404, "only the room's own requests are taken")
results = []
t0 = time.time()
threads = [threading.Thread(target=lambda: results.append(post("slow", b"wait", 30)[0])) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
took = time.time() - t0
check(results == [200] * 12 and took < 6, f"12 waiting requests at once take {took:.1f} s, not 12 × 2 s (no limit of six)")
link.stop_member(room_key)
host_link.stop()
echo.shutdown()

# ---- 2. a whole room: a member joins by "link:" alone --------------------------------------------------------------
root = tempfile.mkdtemp()
office = os.path.join(root, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "P", "memory")
for i in range(5):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"o{i + 1}"])
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({i * 20} 0,{i * 20 + 10} 0,{i * 20 + 10} 10,{i * 20} 10,{i * 20} 0))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(office, "office.qgz"))
host = session.open_room([hl], "Link room", approve=False, external=True)
code = session.room_code(host)
check(pump(10, lambda: host.external_url == "link:") and host.link is not None and host.tunnel is None,
      "a room open to the outside starts the link - no tunnel, nothing downloaded")

ident = {"id": "c" * 32, "seed": "2" * 64, "name": "Away"}
real = session.identity
session.identity = lambda: dict(ident)
box, steps = {}, []
t = threading.Thread(target=lambda: box.update(session.join_steps(code, os.path.join(root, "away"),
                                                                  lambda *a: steps.append(a[0]), address="link:")))
t0 = time.time()
t.start()
pump(150, lambda: not t.is_alive())
session.identity = real
check("snap" in box and box["address"] == "link:", f"a member outside joins by the link alone in {time.time() - t0:.0f} s "
      f"(steps {sorted(set(steps))})")
snap = box["snap"]
ml = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", "Away", "ogr")
check(ml.isValid() and ml.featureCount() == 5, "the room's map came over the link")
st = Store(os.path.join(box["folder"], ".biruni", "team", "room.sqlite"))
st.put("layers", box["hello"]["room"]["layers"])
st.put("since", snap["head"])
st.set_versions([tuple(v) for v in snap["versions"]])
away = Node(st, codes.RoomKeys(code), dict(ident))
away.roster = box["hello"]["room"]["members"]
away.attach_layers({"L1": ml})
away.start_member("link:", box["hello"]["member"])
check(pump(20, lambda: away.state == "connected"), "the member follows the room over the link")
fld = ml.fields().indexOf("owner")
ml.startEditing()
ml.changeAttributeValue(2, fld, "from far away")
away.commit_layer("L1")
check(pump(30, lambda: hl.getFeature(2)["owner"] == "from far away"), "an edit made outside reaches the host")
hl.startEditing()
hl.changeAttributeValue(3, hl.fields().indexOf("owner"), "from the office")
host.commit_layer("L1")
check(pump(30, lambda: ml.getFeature(3)["owner"] == "from the office"), "an edit made in the office reaches the member outside")
check(host.link.members() >= 1, f"the host sees {host.link.members()} computer on the link")
away.shutdown()
check(link.link_key(codes.RoomKeys(code).key) not in link._MEMBERS, "closing the room stops this computer's link")
session.close_room(host)
check(host.link is None, "closing the room stops the host's link")

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
