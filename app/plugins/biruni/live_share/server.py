"""The share server: HTTPS on 127.0.0.1 only, for the hidden host page of this PC.

The hidden page (bridge_host.py, a Chromium page in its own process) meets the viewers
through the Biruni bridge (field_photos/bridge.py) and passes their requests in here: the
map as an image for a view, the features under a tap, the map's title and layers. This
server never draws: each request becomes a Job on self.jobs and the map service (share.py)
answers it in Biruni's main thread, where QGIS draws. A random token in every address and
the 127.0.0.1-only binding keep everyone else out.
"""
import json
import math
import queue
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from ..local_server import ExclusiveServer

SHARE_PORT = 10113
MAX_SIDE = 1600        # the longest side of a map image, in pixels
MAX_WAITING = 8        # requests waiting for the map at once; more get "busy"
MAX_VIEWERS = 20       # direct links the host page keeps at once
WORLD = 20037508.342789244 * 2


class Job:
    """A request for Biruni's main thread; the server thread waits for finish()."""

    def __init__(self, kind, args):
        self.kind = kind
        self.args = args
        self.done = threading.Event()
        self.cancelled = False
        self.result = (503, b"", "application/json", {})

    def finish(self, status, body=b"", ctype="application/json", headers=None):
        self.result = (status, body, ctype, headers or {})
        self.done.set()


def _float(q, name, lo, hi):
    try:
        v = float(q.get(name, [""])[0])
    except ValueError:
        return None
    return v if math.isfinite(v) and lo <= v <= hi else None


def render_args(q):
    """The view asked for, checked: extent in EPSG:3857, size in CSS pixels, pixel ratio."""
    x0, y0 = _float(q, "x0", -WORLD, WORLD), _float(q, "y0", -WORLD, WORLD)
    x1, y1 = _float(q, "x1", -WORLD, WORLD), _float(q, "y1", -WORLD, WORLD)
    w, h, d = _float(q, "w", 8, 4000), _float(q, "h", 8, 4000), _float(q, "d", 0.25, 3)
    if None in (x0, y0, x1, y1, w, h, d) or x1 <= x0 or y1 <= y0:
        return None
    w, h = int(w), int(h)
    pw, ph = round(w * d), round(h * d)
    if pw > MAX_SIDE or ph > MAX_SIDE or pw < 8 or ph < 8:
        return None
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": w, "h": h, "d": d, "pw": pw, "ph": ph}


def identify_args(q):
    x, y, r = _float(q, "x", -WORLD, WORLD), _float(q, "y", -WORLD, WORLD), _float(q, "r", 0, 1e6)
    return None if None in (x, y, r) else {"x": x, "y": y, "r": r}


class ShareServer:
    """Events for the GUI go to self.events: ("host", dict) from the hidden page
    (relays, viewer, viewers, p2pfail, bye). Map requests go to self.jobs."""

    def __init__(self, cert_folder, port=SHARE_PORT):
        from ..gps.browser import ensure_certificate

        self.token = secrets.token_urlsafe(12)
        self.port = port
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.version = 0
        self.stopping = False
        self._cond = threading.Condition()
        self._waiting = 0
        self._lock = threading.Lock()
        self.cert, self.key = ensure_certificate(cert_folder, ["127.0.0.1"])
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _route(self):
                parts = urlsplit(self.path)
                bits = parts.path.strip("/").split("/")
                if (self.client_address[0] != "127.0.0.1" or not bits
                        or not secrets.compare_digest(bits[0], server.token)):
                    return None, parts
                return "/".join(bits[1:]), parts

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except OSError:  # the host page went away (sharing stopped) - nobody to answer
                    self.close_connection = True

            def do_GET(self):  # noqa: N802
                route, parts = self._route()
                q = parse_qs(parts.query)
                if route is None:
                    self._send(404)
                elif route == "host":
                    self._send(200, host_page(), "text/html; charset=utf-8")
                elif route == "changes":
                    self._send(200, json.dumps(server.wait_change(q.get("v", ["-1"])[0])).encode())
                elif route == "info":
                    self._send(*server.ask("info", {}, 10))
                elif route == "identify":
                    a = identify_args(q)
                    self._send(*server.ask("identify", a, 10)) if a else self._send(400)
                elif route == "render":
                    a = render_args(q)
                    self._send(*server.ask("render", a, 60)) if a else self._send(400)
                else:
                    self._send(404)

            def do_POST(self):  # noqa: N802
                route, _parts = self._route()
                if route != "event":
                    self._send(404)
                    return
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 4096)
                    ev = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    self._send(400)
                    return
                if isinstance(ev, dict) and isinstance(ev.get("type"), str):
                    server.events.put(("host", ev))
                self._send(200, b'{"ok":true}')

        self.httpd = ExclusiveServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self):
        return f"https://127.0.0.1:{self.port}/{self.token}/"

    # ---- requests for the main thread ------------------------------------------------
    def ask(self, kind, args, timeout):
        """(status, body, type, headers) of a Job answered by the map service."""
        with self._lock:
            if self.stopping or self._waiting >= MAX_WAITING:
                return 503, b'{"busy":true}', "application/json", {}
            self._waiting += 1
        try:
            job = Job(kind, args)
            self.jobs.put(job)
            if not job.done.wait(timeout):
                job.cancelled = True
                return 504, b"", "application/json", {}
            return job.result
        finally:
            with self._lock:
                self._waiting -= 1

    # ---- "the map changed" and "sharing stops", for the host page's long poll ----------
    def bump(self):
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait_change(self, seen, timeout=25):
        try:
            seen = int(seen)
        except ValueError:
            seen = -1
        with self._cond:
            self._cond.wait_for(lambda: self.stopping or self.version != seen, timeout)
            return {"v": self.version, "stop": self.stopping}

    def announce_stop(self):
        with self._cond:
            self.stopping = True
            self._cond.notify_all()

    def stop(self):
        self.announce_stop()
        while not self.jobs.empty():  # nobody draws any more
            try:
                self.jobs.get_nowait().finish(503)
            except queue.Empty:
                break
        self.httpd.shutdown()
        self.httpd.server_close()


# ---- the hidden host page: answers viewers and passes their requests to the server ----------
HOST_JS = r"""
const HP=new URLSearchParams(location.hash.slice(1)),peers=new Map(),viewers=new Set(),MAXV=__MAXV__;
function tell(ev){return fetch('event',{method:'POST',body:JSON.stringify(ev)}).catch(()=>{})}
function count(){tell({type:'viewers',n:viewers.size})}
function put(ch,o){try{if(ch.readyState==='open')ch.send(JSON.stringify(o))}catch(e){}}
async function onOffer(o){if(o.t!=='offer'||!o.sid||peers.has(o.sid))return;
 if(peers.size>=MAXV){tell({type:'full'});return}
 const pc=new RTCPeerConnection({iceServers:BR.ice});peers.set(o.sid,pc);
 const end=()=>{peers.delete(o.sid);try{pc.close()}catch(e){}};
 pc.ondatachannel=e=>serve(e.channel,end);
 pc.oniceconnectionstatechange=()=>{const s=pc.iceConnectionState;if(s==='failed'){tell({type:'p2pfail'});end()}
  else if(s==='closed')end()};
 setTimeout(()=>{if(pc.iceConnectionState==='new'||pc.iceConnectionState==='checking')end()},45000);
 try{await pc.setRemoteDescription({type:'offer',sdp:o.sdp});await pc.setLocalDescription(await pc.createAnswer());
  await gathered(pc,3000);await SIG.send({t:'answer',sid:o.sid,sdp:pc.localDescription.sdp},!!o.z)}catch(e){end()}}
function serve(ch,end){ch.binaryType='arraybuffer';const v={ch:ch,busy:false,next:null,last:0};
 ch.onopen=()=>{viewers.add(v);count()};
 ch.onclose=()=>{if(viewers.delete(v))count();end()};
 ch.onmessage=e=>{if(typeof e.data!=='string')return;let m;try{m=JSON.parse(e.data)}catch(x){return}
  if(!m||typeof m!=='object')return;
  if(m.t==='hi')tell({type:'viewer',ua:String(m.ua||'').slice(0,160)});
  else if(m.t==='info')ask('info',{},v,m.id,'info');
  else if(m.t==='identify')ask('identify',{x:m.x,y:m.y,r:m.r},v,m.id,'ident');
  else if(m.t==='render'){v.next=m;pump(v)}}}
async function ask(path,q,v,id,t){let data=null,st=503;
 try{const r=await fetch(path+'?'+new URLSearchParams(q),{cache:'no-store'});st=r.status;if(r.ok)data=await r.json()}catch(e){}
 put(v.ch,{t:t,id:id,status:st,data:data})}
/* one image at a time per viewer, the newest view only, at most about seven a second */
async function pump(v){if(v.busy||!v.next)return;v.busy=true;
 try{const w=v.last+150-Date.now();if(w>0)await sleep(w);const m=v.next;v.next=null;v.last=Date.now();
  const q=new URLSearchParams({x0:m.x0,y0:m.y0,x1:m.x1,y1:m.y1,w:m.w,h:m.h,d:m.d});let r=null;
  try{r=await fetch('render?'+q,{cache:'no-store'})}catch(e){}
  if(!r||!r.ok)put(v.ch,{t:'img',id:m.id,status:r?r.status:503});
  else await sendImage(v.ch,m.id,new Uint8Array(await r.arrayBuffer()),r.headers.get('Content-Type')||'image/png',
   r.headers.get('X-Extent'))}catch(e){}
 v.busy=false;if(v.next)pump(v)}
async function sendImage(ch,id,buf,type,ext){if(ch.readyState!=='open')return;
 put(ch,{t:'img',id:id,status:200,size:buf.length,type:type,ext:ext?ext.split(',').map(Number):null});
 for(let o=0;o<buf.length;o+=16384){while(ch.bufferedAmount>1048576){if(ch.readyState!=='open')return;await sleep(20)}
  ch.send(buf.slice(o,o+16384))}
 put(ch,{t:'end',id:id})}
/* the server answers when the map changed, or when sharing stops */
async function watch(){let v=-1;for(;;){let j=null;
 try{j=await (await fetch('changes?v='+v,{cache:'no-store'})).json()}catch(e){await sleep(1000);continue}
 if(j.stop){for(const x of viewers)put(x.ch,{t:'bye'});await sleep(200);await tell({type:'bye'});return}
 if(v>=0&&j.v!==v)for(const x of viewers)put(x.ch,{t:'changed',v:j.v});v=j.v}}
(async()=>{await bridgeKey(HP.get('k')||'');SIG.start(onOffer,n=>tell({type:'relays',up:n,of:SIG.relays.length}));
 tell({type:'started'});watch()})();
"""


def host_page():
    from ..field_photos.bridge import _core

    return ("<!doctype html><html><head><meta charset='utf-8'><title>Biruni live map</title></head><body><script>"
            + _core() + HOST_JS.replace("__MAXV__", str(MAX_VIEWERS)) + "</script></body></html>").encode("utf-8")
