"""End-to-end fallback ladder test: Nostr and ntfy dead; MQTT broker and PhotoServer /x/ tunnel
carry signalling, live positions, store-and-forward backlog, late-flagging, CSP, and old-PC compat.

Run:  engine\\bin\\python-qgis.bat tests\\test_bridge_ladder.py
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


from fake_mqtt import FakeMqttBroker  # noqa: E402

broker = FakeMqttBroker(split=True)
SRV_PORT = free_port()

os.environ["BIRUNI_BRIDGE_RELAYS"] = "ws://127.0.0.1:9"
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"
os.environ["BIRUNI_BRIDGE_MQTT"] = broker.url
os.environ["BIRUNI_BRIDGE_TUNNEL"] = f"https://127.0.0.1:{SRV_PORT}"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox --disable-features=WebRtcHideLocalIpsWithMdns"

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])
profile = QWebEngineProfile.defaultProfile()

from biruni.field_photos import bridge  # noqa: E402
from biruni.field_photos.server import PhotoServer, phone_texts  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


work = tempfile.mkdtemp(prefix="biruni-bridge-lad-")
texts = {"en": phone_texts(lambda s: s)}
page = bridge.phone_page(texts)
csp_page = bridge.with_csp(page)

conn_src = csp_page.split("connect-src", 1)[1].split(";", 1)[0]
check(f"ws://127.0.0.1:{broker.port}" in conn_src and f"https://127.0.0.1:{SRV_PORT}" in conn_src,
      f"CSP connect-src includes MQTT and tunnel origins ({conn_src})")

srv = PhotoServer(os.path.join(work, "photos"), os.path.join(work, "cert"), texts,
                  "en", (), SRV_PORT, ["127.0.0.1"], bind="127.0.0.1")
key = bridge.new_key()
srv.set_ladder(key)
host = bridge.BridgeHost(srv.url("127.0.0.1") + "bridge#" + bridge.host_frag(key))
evs = []


def drain():
    while not srv.events.empty():
        evs.append(srv.events.get_nowait())
    return evs


check(wait(lambda: any(e[0] == "bridge" and e[1].get("type") == "relays"
                       and (e[1].get("kinds") or {}).get("mqtt") == 1 for e in drain()), 60),
      "hidden receiver connected to MQTT broker with Nostr/ntfy dead")

CSP_REFUSED = []


class Phone(QWebEnginePage):
    def __init__(self, prof):
        super().__init__(prof)
        self.certificateError.connect(lambda err: err.acceptCertificate())

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print("phone js:", message, line, flush=True)
        if "Content Security Policy" in message:
            CSP_REFUSED.append(message)


phone = Phone(profile)
phone.setHtml(csp_page, QUrl(f"https://phone.invalid/p/#k={key}&i={bridge.pin(key)}&l=en&m=gps"))
res = {}


def js(expr):
    res.pop("v", None)
    phone.runJavaScript(expr, lambda v: res.update(v=v))
    wait(lambda: "v" in res, 10)
    return res.get("v")


# ---- 1. Signalling and direct channel over MQTT alone ---------------------------------------
check(wait(lambda: js("typeof dc!=='undefined'&&!!(dc&&dc.readyState==='open')") is True, 60),
      "signalling and direct WebRTC channel open over MQTT alone")
check(wait(lambda: js("LAD.cap") is True, 10), "LAD.cap turns true from PC hello lad:1")

# Old PC fallback check (LAD.cap = false sends old {t:'pos'})
drain().clear()
js("LAD.cap=false;fix={lat:24.7001,lon:46.6601,acc:4,t:Date.now()};$('me').value='OldPc';tellPos(true);1")
old_pos = []
check(wait(lambda: bool(old_pos.extend(e[1] for e in drain() if e[0] == "pos" and e[1].get("name") == "OldPc")
                        or old_pos), 10) and "seq" not in old_pos[0] and "via" not in old_pos[0],
      f"with LAD.cap=false, phone sends old pos unchanged ({old_pos[:1]})")
js("LAD.cap=true;1")

# ---- 2. P2P blocked -> positions arrive via=mqtt --------------------------------------------
srv.tunnel_host = "test"
drain().clear()
js(r"""(() => {
 BR.ice = []; BR.directMs = 1000;
 window._blockP2P = true;
 const scrubSdp = s => String(s || '').replace(/a=candidate:(\S+) (\d+) udp (\d+) \S+ \d+ typ host/gi,
  'a=candidate:$1 $2 udp $3 192.0.2.1 9 typ host');
 const origSigSend = SIG.send.bind(SIG);
 SIG.send = async (obj, z) => {
  if (window._blockP2P && obj && obj.t === 'offer' && obj.sdp) obj = Object.assign({}, obj, {sdp: scrubSdp(obj.sdp)});
  return await origSigSend(obj, z);
 };
 window._origRTC = window.RTCPeerConnection;
 window.RTCPeerConnection = function() {
  const p = new window._origRTC({iceServers: []});
  const origSetRemote = p.setRemoteDescription.bind(p);
  p.setRemoteDescription = async (desc) => {
   const sdp = window._blockP2P ? scrubSdp(desc.sdp) : desc.sdp;
   return await origSetRemote({type: desc.type, sdp: sdp});
  };
  return p;
 };
 drop(); RELAY = false;
 fix = {lat: 24.7136, lon: 46.6753, acc: 5, t: Date.now()};
 $('me').value = 'Surveyor1';
 tellPos(true);
})();1""")
mqtt_pos = []
check(wait(lambda: bool(mqtt_pos.extend(e[1] for e in drain() if e[0] == "pos" and e[1].get("via") == "mqtt")
                        or mqtt_pos), 20),
      f"with P2P blocked, position arrives via=mqtt ({mqtt_pos[:1]})")

# Wait for RECV_JS 10s bridge-config poll to push signed tun:'test' so phone creates TunnelRelay
check(wait(lambda: (js("SIG.kinds().tunnel||0") or 0) == 1, 20),
      "phone receives signed tun from PC and brings TunnelRelay up")

# ---- 3. Broker stopped -> positions arrive via=tunnel ---------------------------------------
broker.stop()
drain().clear()
js("fix={lat:24.7145,lon:46.6762,acc:4,t:Date.now()};tellPos(true);1")
tun_pos = []
check(wait(lambda: bool(tun_pos.extend(e[1] for e in drain() if e[0] == "pos" and e[1].get("via") == "tunnel")
                        or tun_pos), 20),
      f"with MQTT broker stopped, positions arrive via=tunnel ({tun_pos[:1]})")
check(wait(lambda: "direct address" in (js("$('st').textContent") or ""), 10),
      f"phone banner reports sending through direct address ({js('$(\"st\").textContent')})")

# ---- 4. Everything down -> backlog grows; on recovery every seq arrives once, in order, flagged late
drain().clear()
js(r"""(() => {
 window._realFetch = window.fetch;
 window.fetch = (u, o) => String(u).includes('/x/')
  ? Promise.reject(new Error('net down')) : window._realFetch(u, o);
 const now = Date.now();
 (async () => {
  for (let i = 0; i < 4; i++) {
   await LAD.push({id: DEVID, name: 'Surveyor1', lat: 24.715 + i * 0.001, lon: 46.677,
    acc: 4, t: now - (4 - i) * 1000});
  }
 })();
})();1""")
check(wait(lambda: (js("LAD.backlog") or 0) >= 4, 10),
      f"with everything down, backlog grows (backlog={js('LAD.backlog')})")
check(wait(lambda: "kept on the phone" in (js("$('waitPill').textContent") or ""), 15),
      f"pill shows kept-on-phone status ({js('$(\"waitPill\").textContent')})")

# Recover tunnel and push one newer live position
js(r"""(() => {
 window.fetch = window._realFetch;
 LAD.push({id: DEVID, name: 'Surveyor1', lat: 24.720, lon: 46.677, acc: 4, t: Date.now()});
})();1""")
check(wait(lambda: (js("LAD.backlog") or 0) == 0, 20), "on recovery, backlog drains to 0")
wait(lambda: len([e[1] for e in drain() if e[0] == "pos"]) >= 5, 10)
rec_pos = [e[1] for e in drain() if e[0] == "pos"]
seqs = [p["seq"] for p in rec_pos]
check(len(rec_pos) == 5 and seqs == sorted(seqs) and len(set(seqs)) == 5,
      f"every seq arrives once, in order ({seqs})")
check(all(p.get("late") is True for p in rec_pos[:-1]) and not rec_pos[-1].get("late"),
      f"earlier backlog positions flagged late, live newest not late ({[p.get('late') for p in rec_pos]})")

# ---- 5. Broker saw no plaintext -------------------------------------------------------------
pub_dump = json.dumps([(t, p.decode("utf-8", "replace")) for t, p in broker.published])
devid = js("DEVID") or ""
check(len(broker.published) > 0 and key not in pub_dump and "24.7136" not in pub_dump
      and "Surveyor1" not in pub_dump and (not devid or devid not in pub_dump),
      f"MQTT broker saw {len(broker.published)} encrypted frames and zero plaintext")

# ---- 6. No CSP violations -------------------------------------------------------------------
check(not CSP_REFUSED, f"no CSP violations logged ({CSP_REFUSED})")

host.stop()
wait(lambda: not host.running(), 10)
srv.stop()

# ---- 7. Biruni own relay (Nostr + MQTT dead) and 1013 backoff -> tunnel ---------------------
from fake_own import FakeOwnRelay  # noqa: E402

own_srv = FakeOwnRelay()
SRV_PORT2 = free_port()
DEAD_MQTT = f"ws://127.0.0.1:{free_port()}/mqtt"
os.environ["BIRUNI_BRIDGE_MQTT"] = DEAD_MQTT
os.environ["BIRUNI_BRIDGE_TUNNEL"] = f"https://127.0.0.1:{SRV_PORT2}"
os.environ["BIRUNI_BRIDGE_OWN"] = own_srv.url
bridge.MQTT = (DEAD_MQTT,)
bridge.TUNNEL = os.environ["BIRUNI_BRIDGE_TUNNEL"]
bridge.OWN = own_srv.url

csp_page2 = bridge.with_csp(bridge.phone_page(texts))
conn_src2 = csp_page2.split("connect-src", 1)[1].split(";", 1)[0]
check(f"ws://127.0.0.1:{own_srv.port}" in conn_src2,
      f"CSP connect-src includes own relay origin ({conn_src2})")

srv2 = PhotoServer(os.path.join(work, "photos2"), os.path.join(work, "cert"), texts,
                   "en", (), SRV_PORT2, ["127.0.0.1"], bind="127.0.0.1")
key2 = bridge.new_key()
srv2.set_ladder(key2)
host2 = bridge.BridgeHost(srv2.url("127.0.0.1") + "bridge#" + bridge.host_frag(key2))
evs2 = []


def drain2():
    while not srv2.events.empty():
        evs2.append(srv2.events.get_nowait())
    return evs2


check(wait(lambda: any(e[0] == "bridge" and e[1].get("type") == "relays"
                       and (e[1].get("kinds") or {}).get("own") == 1 for e in drain2()), 60),
      "hidden receiver connected to Biruni own relay with Nostr/MQTT dead")

phone2 = Phone(profile)
phone2.setHtml(csp_page2, QUrl(f"https://phone.invalid/p/#k={key2}&i={bridge.pin(key2)}&l=en&m=gps"))


def js2(expr):
    res.pop("v", None)
    phone2.runJavaScript(expr, lambda v: res.update(v=v))
    wait(lambda: "v" in res, 10)
    return res.get("v")


check(wait(lambda: js2("typeof dc!=='undefined'&&!!(dc&&dc.readyState==='open')") is True, 60),
      "signalling and direct WebRTC channel open over Biruni own relay alone")

drain2().clear()
js2(r"""(() => {
 BR.ice = []; BR.directMs = 1000;
 window._blockP2P = true;
 const scrubSdp = s => String(s || '').replace(/a=candidate:(\S+) (\d+) udp (\d+) \S+ \d+ typ host/gi,
  'a=candidate:$1 $2 udp $3 192.0.2.1 9 typ host');
 const origSigSend = SIG.send.bind(SIG);
 SIG.send = async (obj, z) => {
  if (window._blockP2P && obj && obj.t === 'offer' && obj.sdp) obj = Object.assign({}, obj, {sdp: scrubSdp(obj.sdp)});
  return await origSigSend(obj, z);
 };
 window._origRTC = window.RTCPeerConnection;
 window.RTCPeerConnection = function() {
  const p = new window._origRTC({iceServers: []});
  const origSetRemote = p.setRemoteDescription.bind(p);
  p.setRemoteDescription = async (desc) => {
   const sdp = window._blockP2P ? scrubSdp(desc.sdp) : desc.sdp;
   return await origSetRemote({type: desc.type, sdp: sdp});
  };
  return p;
 };
 drop(); RELAY = false;
 fix = {lat: 24.7301, lon: 46.6901, acc: 5, t: Date.now()};
 $('me').value = 'SurveyorOwn';
 tellPos(true);
})();1""")
own_pos = []
check(wait(lambda: bool(own_pos.extend(e[1] for e in drain2() if e[0] == "pos" and e[1].get("via") == "own")
                        or own_pos), 20),
      f"with P2P blocked and Nostr/MQTT dead, position arrives via=own ({own_pos[:1]})")
own_dump = json.dumps(own_srv.frames)
check(len(own_srv.frames) > 0 and key2 not in own_dump and "24.7301" not in own_dump
      and "SurveyorOwn" not in own_dump,
      f"Biruni own relay saw {len(own_srv.frames)} encrypted frames and zero plaintext")

srv2.tunnel_host = "test"
check(wait(lambda: (js2("SIG.kinds().tunnel||0") or 0) == 1, 20),
      "phone2 receives signed tun and brings TunnelRelay up")

own_srv.reject_code = 1013
own_srv.kick_all(1013)
check(wait(lambda: js2("(()=>{const r=SIG.relays.find(x=>x.kind==='own');"
                       "return r&&!r.up&&(r.backoffUntil-Date.now())>=55000})()") is True, 15),
      "1013 close gives OwnRelay a backoff of at least 60 s")

drain2().clear()
js2("fix={lat:24.7315,lon:46.6915,acc:4,t:Date.now()};tellPos(true);1")
tun_pos2 = []
check(wait(lambda: bool(tun_pos2.extend(e[1] for e in drain2() if e[0] == "pos" and e[1].get("via") == "tunnel")
                        or tun_pos2), 20),
      f"after 1013 close, positions continue via=tunnel ({tun_pos2[:1]})")

check(not CSP_REFUSED, f"no CSP violations logged ({CSP_REFUSED})")

host2.stop()
wait(lambda: not host2.running(), 10)
srv2.stop()
own_srv.stop()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
