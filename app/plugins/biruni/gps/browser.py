"""Phone GPS without an app: the phone scans a QR code, opens a page served by
this PC, and the browser sends its location here.

Browsers only give the location to secure (HTTPS) pages, so Biruni serves the
page over HTTPS with its own certificate, made once for this computer. The
phone shows a warning the first time ("connection not private") - tapping
Advanced > Continue is expected, the connection stays inside the local
network. A random token in the address keeps other devices on the network out.

Experimental: browsers pause the page when the screen turns off, so the page
asks to keep the screen awake.
"""
import datetime as dt
import ipaddress
import json
import os
import queue
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .nmea import UERE_M, Fix

BROWSER_PORT = 10111

PAGE = """<!doctype html><html lang="{lang}" dir="{dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Biruni GIS - GPS</title>
<style>body{{margin:0;font:17px/1.5 system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;background:#14171B;
color:#E4E8EC;display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{max-width:420px;padding:24px;text-align:center}}h1{{font-size:22px;margin:0 0 6px}}
p{{color:#A2ACB6}}button{{font:inherit;font-weight:700;border:0;border-radius:12px;padding:16px 28px;
background:#2CC5B4;color:#0B1F1D;width:100%;margin-top:12px}}button.stop{{background:#D64545;color:#fff}}
#v{{font-size:15px;margin-top:16px;font-variant-numeric:tabular-nums}}b{{color:#2CC5B4}}</style></head>
<body><main><h1>{title}</h1><p>{intro}</p><button id="b">{start}</button><div id="v"></div></main>
<script>
const T={texts};let id=null,lock=null,n=0;const b=document.getElementById('b'),v=document.getElementById('v');
async function wake(){{try{{lock=await navigator.wakeLock.request('screen')}}catch(e){{}}}}
document.addEventListener('visibilitychange',()=>{{if(id!==null&&document.visibilityState==='visible')wake()}});
function send(p){{const c=p.coords;fetch('fix',{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{lat:c.latitude,lon:c.longitude,acc:c.accuracy,alt:c.altitude,speed:c.speed,
heading:c.heading,t:p.timestamp}})}}).then(r=>{{n++;v.innerHTML='<b>'+T.sent+' '+n+'</b><br>'+
c.latitude.toFixed(6)+', '+c.longitude.toFixed(6)+'<br>± '+Math.round(c.accuracy)+' m'}})
.catch(()=>{{v.textContent=T.lost}})}}
b.onclick=()=>{{if(id!==null){{navigator.geolocation.clearWatch(id);id=null;b.textContent=T.start;
b.className='';if(lock)lock.release();return}}
if(!window.isSecureContext||!navigator.geolocation){{v.textContent=T.insecure;return}}
id=navigator.geolocation.watchPosition(send,e=>{{v.textContent=T.denied+' ('+e.message+')'}},
{{enableHighAccuracy:true,maximumAge:0,timeout:20000}});b.textContent=T.stop;b.className='stop';wake()}};
</script></body></html>"""


def cert_paths(folder):
    return os.path.join(folder, "biruni-gps-cert.pem"), os.path.join(folder, "biruni-gps-key.pem")


def ensure_certificate(folder, addresses):
    """A self-signed certificate for this PC's local addresses (made once,
    remade when the addresses change)."""
    cert, key = cert_paths(folder)
    meta = os.path.join(folder, "biruni-gps-cert.json")
    want = sorted(set(addresses) | {"127.0.0.1"})
    if os.path.exists(cert) and os.path.exists(key) and os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as f:
                if sorted(json.load(f).get("ips", [])) == want:
                    return cert, key
        except (OSError, ValueError):
            pass
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(folder, exist_ok=True)
    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Biruni GIS (this computer)")])
    now = dt.datetime.now(dt.timezone.utc)
    san = x509.SubjectAlternativeName([x509.DNSName("localhost")] +
                                      [x509.IPAddress(ipaddress.ip_address(ip)) for ip in want])
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
           .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
           .not_valid_after(now + dt.timedelta(days=825)).add_extension(san, critical=False)
           .sign(pk, hashes.SHA256()))
    with open(key, "wb") as f:
        f.write(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()))
    with open(cert, "wb") as f:
        f.write(crt.public_bytes(serialization.Encoding.PEM))
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"ips": want}, f)
    return cert, key


def fix_from_json(d):
    t = d.get("t")
    when = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc) if t else dt.datetime.now(dt.timezone.utc)
    speed = d.get("speed")
    acc = d.get("acc")
    lat, lon = float(d["lat"]), float(d["lon"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("position out of range")
    return Fix(time=when, lat=lat, lon=lon, alt=d.get("alt"),
               speed_kmh=speed * 3.6 if speed is not None else None, course=d.get("heading"),
               hdop=(acc / UERE_M) if acc else None, quality=1, valid=True, sentences={"browser"})


class BrowserServer:
    """HTTPS server in a background thread; positions are queued for the GUI."""

    def __init__(self, cert_folder, page_texts, lang="en", port=BROWSER_PORT, addresses=()):
        self.token = secrets.token_urlsafe(9)
        self.port = port
        self.positions = queue.Queue()
        self.cert, self.key = ensure_certificate(cert_folder, list(addresses))
        self.page = PAGE.format(lang=lang, dir="rtl" if lang == "ar" else "ltr", title=page_texts["title"],
                                intro=page_texts["intro"], start=page_texts["start"],
                                texts=json.dumps(page_texts, ensure_ascii=False)).encode("utf-8")
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _ok(self, path):
                return path.split("?")[0].strip("/").split("/")[0] == server.token

            def do_GET(self):  # noqa: N802
                if not self._ok(self.path):
                    self.send_error(404)
                    return
                if not self.path.split("?")[0].endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", f"/{server.token}/")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(server.page)

            def do_POST(self):  # noqa: N802
                if not self._ok(self.path) or not self.path.rstrip("/").endswith("/fix"):
                    self.send_error(404)
                    return
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                try:
                    server.positions.put(fix_from_json(json.loads(self.rfile.read(n))))
                    self.send_response(204)
                except (ValueError, KeyError, TypeError):
                    self.send_response(400)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, host):
        return f"https://{host}:{self.port}/{self.token}/"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def qr_png(text, scale=6):
    import io

    import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=2, dark="#111", light="#fff")
    return buf.getvalue()
