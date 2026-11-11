"""The phone hub server: the phone scans one code and opens one page served by this PC. The page lists the
phone tools and opens the ones the user has started on this PC; the others say to ask the person at the
computer. The phone cannot start anything: the server answers only the page and the tools' on/off states
(GET), never a change.

Nothing of the project goes through here - only which tools are on and where each one is. Like the field
photos page it is HTTPS with this PC's own certificate (gps/browser.py), and a random token in the address
keeps everyone else on the network out.
"""
import json
import os
import queue
import re
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from ..local_server import ExclusiveServer

HUB_PORT = 10115  # 10111 GPS, 10112 field photos, 10113 see my map, 10114 the GPS bridge
PORT_TRIES = 10
FEATURES = ("gps", "photos", "map")  # the order on the phone
PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./~]{0,200}$")
URL_RE = re.compile(r"^https://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]{1,2000}$")


def clean_link(link):
    """Where a started tool is: {"port", "path"} on this PC (the phone uses the address it reached the hub
    with) or {"url"} (a tool reached over the internet). None when it is not a safe address."""
    if not isinstance(link, dict):
        return None
    if link.get("url") is not None:
        url = str(link["url"])
        return {"url": url} if URL_RE.match(url) else None
    port, path = link.get("port"), str(link.get("path") or "/")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536 or not PATH_RE.match(path):
        return None
    return {"port": port, "path": path}


def clean_features(items):
    """The states the page may see: every known tool once, on or off; a link only for a tool that is on."""
    got = {}
    for it in items if isinstance(items, (list, tuple)) else ():
        if isinstance(it, dict) and it.get("id") in FEATURES and it["id"] not in got:
            on = bool(it.get("on"))
            got[it["id"]] = {"id": it["id"], "on": on, "link": clean_link(it.get("link")) if on else None}
    return [got.get(f) or {"id": f, "on": False, "link": None} for f in FEATURES]


class HubServer:
    """HTTPS server in a background thread. ("phone", user agent) goes to self.events when a phone opens
    the page. set_features() changes what the page shows; the page asks again every few seconds."""

    def __init__(self, cert_folder, page_texts, lang="en", port=HUB_PORT, addresses=(), bind="0.0.0.0", tries=1):
        from ..gps.browser import ensure_certificate

        self.token = secrets.token_urlsafe(12)
        self.events = queue.Queue()
        self.cert, self.key = ensure_certificate(cert_folder, list(addresses))
        self.page = render_page(page_texts, lang)
        self._phones = set()
        self.version = 0
        self.state = b""
        self.set_features([])
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")  # the hub's token never leaves with a link
                if ctype.startswith("text/html"):
                    self.send_header("Content-Security-Policy",
                                     "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                     "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                bits = parts.path.strip("/").split("/")
                if not bits or not secrets.compare_digest(bits[0], server.token):
                    self._send(404)
                    return
                route = "/".join(bits[1:])
                if route == "" and not parts.path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", f"/{server.token}/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif route == "":
                    agent = (self.headers.get("User-Agent") or "")[:160]
                    if agent not in server._phones:
                        server._phones.add(agent)
                        server.events.put(("phone", agent))
                    self._send(200, server.page, "text/html; charset=utf-8")
                elif route == "state":
                    self._send(200, server.state)
                else:
                    self._send(404)

            def _refuse(self):
                self.close_connection = True
                self._send(405)

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815 - the phone never changes anything

        last = None
        for p in range(port, port + max(1, tries)):
            try:
                self.httpd = ExclusiveServer((bind, p), Handler)
                break
            except OSError as e:
                last = e
        else:
            raise last
        self.port = self.httpd.server_address[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def set_features(self, items):
        """[{"id": "gps"|"photos"|"map", "on": bool, "link": {...}}] - what the phones see from now on.
        Returns True when it changed."""
        feats = clean_features(items)
        if feats == getattr(self, "_features", None):
            return False
        self._features = feats
        self.version += 1
        self.state = json.dumps({"v": self.version, "features": feats}, separators=(",", ":")).encode("utf-8")
        return True

    def url(self, host):
        return f"https://{host}:{self.port}/{self.token}/"

    def path(self):
        return f"/{self.token}/"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def phone_texts(tr):
    """The hub page's texts in one language (tr: the translation of an English text)."""
    return {"title": tr("Biruni GIS - phone"), "hero": tr("Phone tools"),
            "intro": tr("Open a tool below. The tools are started on the computer; this page shows the ones "
                        "that are on."),
            "gps": tr("Phone GPS"), "gpsAbout": tr("Send this phone's location to the computer's map."),
            "photos": tr("Field photos"), "photosAbout": tr("Take photos that appear on the computer's map."),
            "map": tr("See my map"), "mapAbout": tr("See the computer's map live on this phone."),
            "open": tr("Open"), "ask": tr("Not started - ask the person at the computer to start it."),
            "notReady": tr("On the computer, but not ready for the phone yet."),
            "online": tr("Connected"), "offline": tr("Not connected"), "linking": tr("Connecting…"),
            "lost": tr("Not reaching the computer - check the Wi-Fi. This page keeps trying."),
            "warn": tr("The first time, a tool may warn that the connection is not private: choose Advanced, then "
                       "Continue - the connection stays inside your network."),
            "back": tr("Come back to this page to open another tool."), "language": tr("Language")}


def all_phone_texts():
    """The hub page's texts in every language Biruni has, so the page can switch."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "i18n")
    out = {"en": phone_texts(lambda s: s)}
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else ():
        if name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    table = json.load(f)
            except (OSError, ValueError):
                continue
            out[name[:-5]] = phone_texts(lambda s, t=table: t.get(s) or s)
    return out


def render_page(texts, lang="en"):
    """texts: {language: phone_texts(...)}; lang: the language the page opens in (the phone can switch)."""
    lang = re.sub(r"[^A-Za-z_]", "", lang or "en")[:8] or "en"
    return (PAGE.replace("__LANG__", lang.replace("_", "-")).replace("__DEFLANG__", lang)
            .replace("__DIR__", "rtl" if lang in ("ar", "fa") else "ltr")
            .replace("__TEXTS__", json.dumps(texts, ensure_ascii=False).replace("</", "<\\/"))
            ).encode("utf-8")


PAGE = r"""<!doctype html><html lang="__LANG__" dir="__DIR__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#F4F6F8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E1114" media="(prefers-color-scheme: dark)">
<meta name="color-scheme" content="light dark"><title>Biruni GIS</title>
<style>
:root{--bg:#F4F6F8;--card:#FFFFFF;--text:#14181D;--dim:#5F6B76;--line:#E1E6EB;--accent:#12A594;--ink:#FFFFFF;
 --soft:#E1F4F1;--good:#12A36E;--fair:#C98A12;--poor:#D24B4B;--shadow:0 1px 2px rgba(16,24,32,.05),0 6px 20px rgba(16,24,32,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0E1114;--card:#171B20;--text:#E8ECEF;--dim:#97A3AE;--line:#262C33;
 --accent:#2CC5B4;--ink:#05201C;--soft:#11302C;--good:#3DD39A;--fair:#E8B04B;--poor:#F07070;--shadow:none}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Arabic","Noto Sans",Tahoma,sans-serif;
 min-height:100vh;-webkit-font-smoothing:antialiased}
svg{width:1em;height:1em;flex:none}
button{font:inherit;cursor:pointer}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:10px;
 padding:calc(10px + env(safe-area-inset-top)) 16px 10px;background:var(--bg)}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.logo{width:30px;height:30px;border-radius:10px;background:var(--accent);color:var(--ink);display:grid;place-items:center;font-size:18px}
.tools{display:flex;gap:8px;align-items:center}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:6px 11px;border-radius:999px;
 background:var(--card);border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.pill svg{font-size:15px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--good)}.dot.off{background:var(--poor)}.dot.wait{background:var(--fair);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.3}}
.banner{margin:2px 16px 4px;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.45;
 background:var(--card);border:1px solid var(--line);border-inline-start:4px solid var(--fair)}
main{max-width:520px;margin:0 auto;padding:6px 16px 24px}
h1{font-size:26px;line-height:1.2;margin:14px 0 6px;letter-spacing:-.02em}
.intro{color:var(--dim);margin:0 0 18px}
.card{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:20px;
 padding:15px;margin-bottom:12px;box-shadow:var(--shadow)}
.card.off{opacity:.72}
.ico{width:52px;height:52px;border-radius:16px;background:var(--soft);color:var(--accent);display:grid;place-items:center;
 font-size:28px;flex:none}
.card.off .ico{background:var(--line);color:var(--dim)}
.txt{flex:1;min-width:0}
.txt b{display:block;font-size:17px}
.txt span{display:block;font-size:14px;color:var(--dim)}
.txt em{display:block;font-style:normal;font-size:13px;font-weight:600;color:var(--fair);margin-top:3px}
a.go{flex:none;text-decoration:none;font-weight:700;padding:11px 18px;border-radius:14px;background:var(--accent);color:var(--ink)}
.note{font-size:13px;color:var(--dim);margin:14px 4px 0}
.sheet{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.4);display:flex;align-items:flex-end}
.panel{width:100%;background:var(--card);border-radius:22px 22px 0 0;padding:12px 16px calc(18px + env(safe-area-inset-bottom))}
.grab{width:40px;height:5px;border-radius:3px;background:var(--line);margin:0 auto 12px}
#langs button{display:block;width:100%;text-align:start;padding:13px 12px;border:0;border-radius:12px;background:transparent;color:var(--text);font-size:16px}
#langs button.sel{background:var(--soft);color:var(--accent);font-weight:700}
.lbl{font-size:13px;font-weight:600;color:var(--dim)}
.hide{display:none!important}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top">
 <div class="brand"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="3" width="12" height="18" rx="2"/><path d="M11 4h2M12 17v.01"/></svg></span><span>Biruni GIS</span></div>
 <div class="tools"><span class="pill"><i class="dot wait" id="dot"></i><span id="chipText"></span></span>
 <button class="pill" id="langBtn" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M11.5 3a17 17 0 0 0 0 18M12.5 3a17 17 0 0 1 0 18"/></svg><span id="langCode"></span></button></div>
</header>
<div class="banner hide" id="st"></div>
<main>
 <h1 id="hero"></h1><p class="intro" id="introText"></p>
 <div id="list"></div>
 <p class="note" id="warn"></p><p class="note" id="back"></p>
</main>
<div class="sheet hide" id="sheet"><div class="panel"><div class="grab"></div><div class="lbl" id="lLang"></div><div id="langs"></div></div></div>
<script>
const ALL=__TEXTS__,DEF='__DEFLANG__',$=i=>document.getElementById(i);let T={},LANG='en',STATE=null,OK=null;
const NAMES={en:'English',ar:'العربية',es:'Español',pt_BR:'Português',fr:'Français',tr:'Türkçe',fa:'فارسی'};
const ICONS={gps:'<circle cx="12" cy="11" r="3"/><path d="M17.66 16.66L13.41 20.9a2 2 0 0 1-2.83 0l-4.24-4.24a8 8 0 1 1 11.32 0z"/>',
 photos:'<path d="M5 7h1a2 2 0 0 0 2-2a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1a2 2 0 0 0 2 2h1a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2"/><circle cx="12" cy="13" r="3"/>',
 map:'<path d="M3 7l6-3l6 3l6-3v13l-6 3l-6-3l-6 3z"/><path d="M9 4v13M15 7v13"/>'};
function pick(l){if(!l)return null;l=String(l).replace('-','_');if(ALL[l])return l;const b=l.split('_')[0].toLowerCase();
 return Object.keys(ALL).find(k=>k.split('_')[0].toLowerCase()===b)||null}
function keep(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function kept(k){try{return localStorage.getItem(k)}catch(e){return null}}
function href(link){if(!link)return null;
 if(typeof link.url==='string')return /^https:\/\//.test(link.url)?link.url:null;
 const p=Number(link.port);if(!(p>0&&p<65536)||typeof link.path!=='string'||link.path[0]!=='/')return null;
 return 'https://'+location.hostname+':'+p+link.path}
function icon(id){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 24 24');
 s.setAttribute('fill','none');s.setAttribute('stroke','currentColor');s.setAttribute('stroke-width','1.8');
 s.setAttribute('stroke-linecap','round');s.setAttribute('stroke-linejoin','round');s.innerHTML=ICONS[id]||'';return s}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function draw(){const box=$('list');box.textContent='';if(!STATE)return;
 STATE.features.forEach(f=>{if(!ICONS[f.id])return;const url=f.on?href(f.link):null;
  const c=el('div','card'+(url?'':' off'));c.dataset.id=f.id;const i=el('div','ico');i.appendChild(icon(f.id));c.appendChild(i);
  const t=el('div','txt');t.appendChild(el('b','',T[f.id]));t.appendChild(el('span','',T[f.id+'About']));
  if(!url)t.appendChild(el('em','',f.on?T.notReady:T.ask));c.appendChild(t);
  if(url){const a=el('a','go',T.open);a.href=url;a.rel='noreferrer';c.appendChild(a)}
  box.appendChild(c)})}
function chip(){const d=$('dot');d.className='dot '+(OK===null?'wait':OK?'on':'off');
 $('chipText').textContent=OK===null?T.linking:OK?T.online:T.offline;
 $('st').textContent=T.lost;$('st').classList.toggle('hide',OK!==false)}
function label(){document.title=T.title;$('hero').textContent=T.hero;$('introText').textContent=T.intro;
 $('warn').textContent=T.warn;$('back').textContent=T.back;$('lLang').textContent=T.language;
 $('langBtn').setAttribute('aria-label',T.language);$('langCode').textContent=LANG.split('_')[0].toUpperCase();chip();draw()}
function setLang(l){LANG=l;T=Object.assign({},ALL.en||{},ALL[l]||{});const h=document.documentElement;
 h.lang=l.replace('_','-');h.dir=(l==='ar'||l==='fa')?'rtl':'ltr';label()}
async function poll(){try{const r=await fetch('state',{cache:'no-store'});if(!r.ok)throw new Error(r.status);
  const s=await r.json();OK=true;if(!STATE||s.v!==STATE.v){STATE=s;draw()}}catch(e){OK=false}chip()}
$('langBtn').onclick=()=>{const box=$('langs');box.textContent='';Object.keys(ALL).forEach(k=>{const b=document.createElement('button');
 b.type='button';b.textContent=NAMES[k]||k;b.lang=k.replace('_','-');b.classList.toggle('sel',k===LANG);
 b.onclick=()=>{keep('biruni-lang',k);setLang(k);$('sheet').classList.add('hide')};box.appendChild(b)});
 $('sheet').classList.remove('hide')};
$('sheet').onclick=e=>{if(e.target===$('sheet'))$('sheet').classList.add('hide')};
setLang(pick(kept('biruni-lang'))||pick(DEF)||pick(navigator.language)||'en');
poll();setInterval(poll,3000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')poll()});
</script></body></html>"""
