"""The dashboard from anywhere: the Biruni bridge (field_photos/bridge.py), as "See my map" uses it.

The viewer opens the public page (https://hatemtarekgis.github.io/biruni-gis/d/) with the card and a key
after "#" - never sent to any server. Viewer and PC meet through public relays that see only unreadable
bytes, then talk over a direct encrypted WebRTC channel. On this PC a hidden Chromium page (BridgeHost,
in its own process) answers the viewers and relays their messages to the dashboard's LAN server on
127.0.0.1 (server.py, route ``host``), marked as bridge viewers - the same sessions, the same checks of
the card as on the LAN.

A message to the viewer longer than PART characters goes in parts (SPEC "Changes").
"""
import time

PAGE_URL = "https://hatemtarekgis.github.io/biruni-gis/d/"
PART = 48000
MAX_VIEWERS = 30


def viewer_url(card, key, lang="en", tv=False):
    from ..field_photos.bridge import pin

    frag = f"c={card}&k={key}&i={pin(key)}&l={lang}" + ("&tv=1" if tv else "")
    return PAGE_URL + "#" + frag


HOST_JS = r"""
const HP=new URLSearchParams(location.hash.slice(1)),peers=new Map(),viewers=new Set(),MAXV=__MAXV__,PART=__PART__;
function put(ch,o){try{if(ch.readyState!=='open')return;const s=JSON.stringify(o);
 if(s.length<=PART){ch.send(s);return}
 const id=Math.random().toString(36).slice(2),n=Math.ceil(s.length/PART);
 for(let i=0;i<n;i++)ch.send(JSON.stringify({t:'part',id:id,i:i,n:n,d:s.slice(i*PART,(i+1)*PART)}))}catch(e){}}
async function onOffer(o){if(o.t!=='offer'||!o.sid||peers.has(o.sid))return;
 if(peers.size>=MAXV)return;
 const pc=new RTCPeerConnection({iceServers:BR.ice});peers.set(o.sid,pc);
 const end=()=>{peers.delete(o.sid);try{pc.close()}catch(e){}};
 pc.ondatachannel=e=>serve(e.channel,end);
 pc.oniceconnectionstatechange=()=>{const s=pc.iceConnectionState;if(s==='failed'||s==='closed')end()};
 setTimeout(()=>{if(pc.iceConnectionState==='new'||pc.iceConnectionState==='checking')end()},45000);
 try{await pc.setRemoteDescription({type:'offer',sdp:o.sdp});await pc.setLocalDescription(await pc.createAnswer());
  await gathered(pc,3000);await SIG.send({t:'answer',sid:o.sid,sdp:pc.localDescription.sdp},!!o.z)}catch(e){end()}}
/* one viewer = one dashboard session of the PC: its messages go to m, the PC's answers come from ev */
function serve(ch,end){const v={ch:ch,s:null,n:0,alive:true};
 ch.onopen=()=>viewers.add(v);
 ch.onclose=()=>{v.alive=false;viewers.delete(v);if(v.s)fetch('bye',{method:'POST',body:JSON.stringify({s:v.s})}).catch(()=>{});end()};
 ch.onmessage=async e=>{if(typeof e.data!=='string'||e.data.length>16384)return;let m;try{m=JSON.parse(e.data)}catch(x){return}
  if(!m||typeof m!=='object'||typeof m.t!=='string')return;
  let r=null;try{r=await fetch('m',{method:'POST',body:JSON.stringify({s:v.s,tr:'bridge',m:m})})}catch(x){}
  if(!r||!r.ok){put(ch,{t:'error',why:r&&r.status===503?'busy':'pc'});return}
  const j=await r.json();if(!v.s&&j.s){v.s=j.s;v.n=j.n||0;for(const o of j.out||[])put(ch,o);poll(v)}}}
async function poll(v){while(v.alive){let j=null;
 try{const r=await fetch('ev?s='+encodeURIComponent(v.s)+'&n='+v.n,{cache:'no-store'});j=await r.json()}catch(e){await sleep(1000);continue}
 for(const o of j.out||[])put(v.ch,o);if(typeof j.n==='number')v.n=Math.max(v.n,j.n);
 if((j.out||[]).some(o=>o.t==='bye')){await sleep(300);try{v.ch.close()}catch(e){}return}}}
(async()=>{await bridgeKey(HP.get('k')||'');SIG.start(onOffer,()=>{});})();
"""


def host_page():
    from ..field_photos.bridge import _core

    js = HOST_JS.replace("__MAXV__", str(MAX_VIEWERS)).replace("__PART__", str(PART))
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Biruni dashboard host</title></head><body>"
            "<script>" + _core() + js + "</script></body></html>").encode("utf-8")


class Anywhere:
    """The hidden host page for one key (the links of every card shared from anywhere carry it; a new key
    ends them all)."""

    def __init__(self, lan):
        from ..field_photos import bridge

        self.lan = lan
        self.key = bridge.new_key()
        self.host = bridge.BridgeHost(lan.local_url() + "host#" + bridge.host_frag(self.key))
        self.started = time.monotonic()

    def url(self, card, lang="en", tv=False):
        return viewer_url(card, self.key, lang, tv)

    def running(self):
        return self.host is not None and self.host.running()

    def log(self):
        return list(self.host.log) if self.host is not None else []

    def stop(self):
        if self.host is not None:
            self.host.stop()
            self.host = None


def write_public_page(folder):
    """build/bridge-page/d/index.html: the page the links from anywhere open (published by the user's own
    push to GitHub Pages). -> path"""
    from .page import write_public_page as write

    return write(folder)
