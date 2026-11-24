"""Biruni bridge: the phone and this PC find each other through public relays, then talk
directly (WebRTC) - no tunnel, no program to download, no account, no server of ours.

How it works
  * Biruni keeps a random 32-byte key for this Windows user. The QR code holds the phone
    page's address (PAGE_URL, https://photos.biruni-gis.com/p/) with the key after "#" - the
    part of an address a browser never sends to any server.
  * Both sides derive a meeting name from the key and publish small encrypted messages
    (AES-GCM with the key) on several independent public relays at once: Nostr relays
    (ephemeral events, not kept) and ntfy.sh (sent with caching off). The relays see only
    the meeting name and unreadable bytes. A relay that is down does not matter.
  * The messages carry a WebRTC offer and answer; then phone and PC open a direct,
    encrypted channel (as browser video calls do) and the photos go over it, phone to PC.
    Nothing is stored anywhere on the way.
  * On the PC the receiving side is a hidden Chromium page (the QtWebEngine of the engine)
    in a separate process (bridge_host.py), so a browser fault never takes Biruni down. It
    hands every photo to the local photo server (server.py), which saves it as before.

Some mobile networks do not allow a direct channel to the PC. Such a phone asks the team for
help through the relays, and a teammate's phone that does reach the PC carries its messages
(a channel phone to phone, then that phone's own channel to the PC). The phone and the PC
first agree on a key of their own (ECDH), so the phone that carries cannot read what it
carries. No server of ours, and none of anyone's, is in the way: where others rent a relay
server, the team is its own relay.
"""
import base64
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse

from . import relay_pool

PAGE_URL = "https://photos.biruni-gis.com/p/"
RELAYS = relay_pool.NOSTR
LEGACY_RELAYS = relay_pool.LEGACY_NOSTR
NTFY = "https://ntfy.sh/"
MQTT = relay_pool.MQTT
LEGACY_MQTT = relay_pool.LEGACY_MQTT
TUNNEL = "https://{h}.trycloudflare.com"
OWN = "wss://relay.biruni-gis.com/r/"   # Biruni's own relay (relay/, deployed 2026-11-18)
ORG = "wss://{h}/r/"
ORG_KEY = "biruni/bridge/own-relay"
ORG_RE = re.compile(r"^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$")
if os.environ.get("BIRUNI_BRIDGE_RELAYS"):  # tests: a relay on this PC
    RELAYS = LEGACY_RELAYS = tuple(os.environ["BIRUNI_BRIDGE_RELAYS"].split())
    NTFY = os.environ.get("BIRUNI_BRIDGE_NTFY", NTFY)
    MQTT = LEGACY_MQTT = tuple(os.environ.get("BIRUNI_BRIDGE_MQTT", "").split())
    TUNNEL = os.environ.get("BIRUNI_BRIDGE_TUNNEL", "")
    OWN = os.environ.get("BIRUNI_BRIDGE_OWN", "")
    ORG = os.environ.get("BIRUNI_BRIDGE_ORG", "")
elif "BIRUNI_BRIDGE_ORG" in os.environ:
    ORG = os.environ["BIRUNI_BRIDGE_ORG"]
KEY = "biruni/field-photos/bridge-key"


# ---- the key and the address in the QR code ----------------------------------------------
def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_key():
    return b64u(secrets.token_bytes(32))


def load_key(settings, fresh=False, name=KEY):
    """This user's bridge key (kept encrypted with DPAPI); made the first time or when fresh.
    Each use of the bridge has its own key (name): two of them running at once must never answer each
    other's phones - Phone GPS once received Field Photos' pictures because both used one key."""
    from .tunnel import protect, unprotect

    k = "" if fresh else unprotect(settings.value(name, ""))
    if len(k) != 43:
        k = new_key()
        settings.setValue(name, protect(k))
    return k


# ---- the PC's identity: phones know whom they talk to --------------------------------------
# Every phone holds the bridge key k (it is in the QR code), so k alone cannot tell the PC from another phone that
# answers in its place. The PC also has an ECDSA P-256 key pair: its answers (and the hello it sends a phone carried
# by a teammate) are signed, and a link carries a short pin of the public key (&i=). A phone whose link has the pin
# refuses an answer the PC did not sign; a link without it (QR codes printed before 2026-11) works as before.
# The private key stays on this PC (DPAPI) and reaches only the hidden receiver page, in its own address (&s=).
ID_STORE = "biruni/bridge/identities"
ID_KEEP = 24
_IDS = {}


def _id_new():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    sk = ec.generate_private_key(ec.SECP256R1())
    return b64u(sk.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()))


def id_pin(pkcs8_b64u):
    """The short pin of an identity's public key (what a link carries as &i=)."""
    import hashlib

    from cryptography.hazmat.primitives import serialization

    der = base64.urlsafe_b64decode(pkcs8_b64u + "=" * (-len(pkcs8_b64u) % 4))
    sk = serialization.load_der_private_key(der, None)
    raw = sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(hashlib.sha256(raw).digest())[:22]


def identity_slot(key):
    """Where a key's identity is kept in ID_STORE."""
    import hashlib

    return hashlib.sha256(("biruni-bridge-id:" + key).encode("ascii")).hexdigest()[:24]


def identity(key, settings=None):
    """This PC's identity for one bridge key: (private key as PKCS#8 b64u, pin). Kept (DPAPI) as long as the key, so
    a QR code that stays the same (Field Photos, Phone GPS, printed live papers) keeps its pin; a new key gets a new
    identity. The newest ID_KEEP are kept. settings: another settings store (a key backup's tests); not cached."""
    import json as _json

    h = identity_slot(key)
    if settings is None and h in _IDS:
        return _IDS[h]
    sk = None
    try:
        from .tunnel import protect, unprotect

        if settings is None:
            from qgis.core import QgsSettings

            st = QgsSettings()
        else:
            st = settings
        try:
            d = _json.loads(st.value(ID_STORE, "") or "{}")
        except ValueError:
            d = {}
        if h in d:
            sk = unprotect(d[h]) or None
        if not sk:
            sk = _id_new()
            d.pop(h, None)
            d[h] = protect(sk)
            while len(d) > ID_KEEP:
                d.pop(next(iter(d)))
            st.setValue(ID_STORE, _json.dumps(d))
    except Exception:  # noqa: BLE001 - no settings (a test, a script): an identity for this run only
        sk = sk or _id_new()
    if settings is not None:
        return (sk, id_pin(sk))
    _IDS[h] = (sk, id_pin(sk))
    return _IDS[h]


def forget_cached_identities():
    """After a key backup is restored: read the identities from the settings again."""
    _IDS.clear()


def forget_identity(key):
    """Drop the identity of a key (a new paper address)."""
    import hashlib
    import json as _json

    h = hashlib.sha256(("biruni-bridge-id:" + key).encode("ascii")).hexdigest()[:24]
    _IDS.pop(h, None)
    try:
        from qgis.core import QgsSettings

        st = QgsSettings()
        d = _json.loads(st.value(ID_STORE, "") or "{}")
        if d.pop(h, None) is not None:
            st.setValue(ID_STORE, _json.dumps(d))
    except Exception:  # noqa: BLE001
        pass


def host_frag(key):
    """The fragment of the hidden receiver page's address: the key and the identity's private key (this PC only)."""
    return f"k={key}&s={identity(key)[0]}"


def pin(key):
    return identity(key)[1]


def org_host():
    """Validated workers.dev hostname of this organization's Biruni relay, or '' on any error."""
    try:
        from qgis.core import QgsSettings

        h = str(QgsSettings().value(ORG_KEY, "") or "").strip().lower()
        return h if ORG_RE.fullmatch(h) else ""
    except Exception:  # noqa: BLE001
        return ""


def set_org_host(h):
    """Store (or clear when empty) the organization's relay hostname in QgsSettings."""
    from qgis.core import QgsSettings

    val = str(h or "").strip().lower()
    if val and not ORG_RE.fullmatch(val):
        raise ValueError("invalid organization relay host")
    QgsSettings().setValue(ORG_KEY, val)
    return val


def phone_url(key, lang="en", drive_pk=None):
    """The phone page's address (everything after # stays on the phone). drive_pk: this computer's handover public
    key when the project takes photos through Google Drive - the page then offers Drive and seals each photo for it
    (&d=1&pk=, see drive.py) even before it has ever reached this computer."""
    url = f"{PAGE_URL}#k={key}&i={pin(key)}&l={lang}&p=2"
    oh = org_host()
    if oh:
        url += f"&o={oh}"
    return url + f"&d=1&pk={drive_pk}" if drive_pk else url


# ---- the shared script: encryption, Nostr signatures, relays ------------------------------
CORE_JS = r"""
/* ---- Biruni bridge core: shared by the phone page and the PC's hidden receiver ---- */
const BR={relays:__RELAYS__,legacyRelays:__LEGACY_RELAYS__,ntfy:'__NTFY__',mqtt:__MQTT__,legacyMqtt:__LEGACY_MQTT__,
 poolK:__POOL_K__,tun:'__TUN__',own:'__OWN__',org:'__ORG__',ownLazyMs:10000,kind:25050,
 ice:[{urls:['stun:stun.l.google.com:19302','stun:stun.cloudflare.com:3478']}]};
const HOSTSIDE=typeof location!=='undefined'&&location.hostname==='127.0.0.1';
const POOLV=typeof location!=='undefined'&&new URLSearchParams((location.hash||'').slice(1)).get('p')==='2';
const enc=new TextEncoder(),dec=new TextDecoder();
const b64u={enc:b=>{let s='';const a=new Uint8Array(b);for(let i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);
  return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')},
 dec:s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0))};
const hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
const unhex=h=>Uint8Array.from(h.match(/../g),x=>parseInt(x,16));
const cat=(...a)=>{const o=new Uint8Array(a.reduce((n,x)=>n+x.length,0));let i=0;for(const x of a){o.set(x,i);i+=x.length}return o};
const sha=async b=>new Uint8Array(await crypto.subtle.digest('SHA-256',b));
const rid=()=>b64u.enc(crypto.getRandomValues(new Uint8Array(9)));
var SKEW=typeof SKEW==='number'?SKEW:0,SKEW_OK=typeof SKEW_OK==='boolean'?SKEW_OK:false;
function NOW(){return Date.now()+SKEW}
function noteTime(ms){if(typeof ms==='number'&&isFinite(ms)&&ms>1700000000000){SKEW=Math.round(ms-Date.now());SKEW_OK=true}}
/* the PC's identity (see bridge.py): the receiver signs with &s=, a phone checks against the pin &i= (kept per key,
   so a page that rebuilds its address keeps it). No pin: no check (older QR codes). */
const ID={sk:null,pub:null,pin:null};
async function idInit(k){const h=new URLSearchParams(typeof location!=='undefined'?location.hash.slice(1):''),s=h.get('s'),i=h.get('i'),slot='biruni-pin-'+String(k).slice(0,16);
 if(i&&/^[A-Za-z0-9_-]{22}$/.test(i)){ID.pin=i;try{localStorage.setItem(slot,i)}catch(e){}}
 else{try{const v=localStorage.getItem(slot);if(v&&/^[A-Za-z0-9_-]{22}$/.test(v))ID.pin=v}catch(e){}}
 if(s){try{ID.sk=await crypto.subtle.importKey('pkcs8',b64u.dec(s),{name:'ECDSA',namedCurve:'P-256'},true,['sign']);
   const j=await crypto.subtle.exportKey('jwk',ID.sk);ID.pub=b64u.enc(cat(new Uint8Array([4]),b64u.dec(j.x),b64u.dec(j.y)))}
  catch(e){ID.sk=null}}}
async function IDSIGN(text){if(!ID.sk)return null;
 return {pub:ID.pub,sig:b64u.enc(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},ID.sk,enc.encode(text)))}}
async function IDOK(pub,sig,text){if(!ID.pin)return true;
 try{if(typeof pub!=='string'||typeof sig!=='string')return false;const raw=b64u.dec(pub);
  if(raw.length!==65||b64u.enc(await sha(raw)).slice(0,22)!==ID.pin)return false;
  const k=await crypto.subtle.importKey('raw',raw,{name:'ECDSA',namedCurve:'P-256'},false,['verify']);
  return await crypto.subtle.verify({name:'ECDSA',hash:'SHA-256'},k,b64u.dec(sig),enc.encode(text))}catch(e){return false}}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
/* BIP340 Schnorr signatures on secp256k1 - Nostr relays only take signed events.
   A new throwaway identity per page; the content is what the key protects. */
const SP=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2Fn,
 SN=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141n,
 SG=[0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798n,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8n];
const md=(a,m=SP)=>{const r=a%m;return r<0n?r+m:r};
function minv(a,m=SP){let r0=md(a,m),r1=m,s0=1n,s1=0n;while(r1){const q=r0/r1;[r0,r1]=[r1,r0-q*r1];[s0,s1]=[s1,s0-q*s1]}
 return md(s0,m)}
function padd(p,q){if(!p)return q;if(!q)return p;let l;
 if(p[0]===q[0]){if(md(p[1]+q[1])===0n)return null;l=md(3n*p[0]*p[0]*minv(2n*p[1]))}
 else l=md((q[1]-p[1])*minv(q[0]-p[0]));
 const x=md(l*l-p[0]-q[0]);return [x,md(l*(p[0]-x)-p[1])]}
function pmul(k,p=SG){let r=null;while(k>0n){if(k&1n)r=padd(r,p);p=padd(p,p);k>>=1n}return r}
const big=b=>BigInt('0x'+(hex(b)||'0')),b32=n=>unhex(n.toString(16).padStart(64,'0'));
async function thash(tag,...parts){const t=await sha(enc.encode(tag));return sha(cat(t,t,...parts))}
async function schnorr(sk,msg,aux){const P=pmul(sk),d=(P[1]&1n)?SN-sk:sk;
 const ah=await thash('BIP0340/aux',aux),db=b32(d),t=db.map((x,i)=>x^ah[i]);
 const k0=md(big(await thash('BIP0340/nonce',t,b32(P[0]),msg)),SN);if(!k0)throw new Error('nonce');
 const R=pmul(k0),k=(R[1]&1n)?SN-k0:k0;
 const e=md(big(await thash('BIP0340/challenge',b32(R[0]),b32(P[0]),msg)),SN);
 return cat(b32(R[0]),b32(md(k+e*d,SN)))}
let NID=null;
async function nostrEvent(content,topic){if(!NID){let sk=0n;
  while(!sk||sk>=SN)sk=big(crypto.getRandomValues(new Uint8Array(32)));NID={sk:sk,pk:hex(b32(pmul(sk)[0]))}}
 const ev={pubkey:NID.pk,created_at:Math.floor(NOW()/1000),kind:BR.kind,tags:[['t',topic]],content:content};
 const id=await sha(enc.encode(JSON.stringify([0,ev.pubkey,ev.created_at,ev.kind,ev.tags,ev.content])));
 ev.id=hex(id);ev.sig=hex(await schnorr(NID.sk,id,crypto.getRandomValues(new Uint8Array(32))));return ev}
/* the key: AES-GCM for every message, and the meeting name the relays see */
let BK=null;
async function rankUrls(topic,urls){
 const pairs=await Promise.all((urls||[]).map(async u=>({u:u,h:hex(await sha(enc.encode(topic+'|'+u)))})));
 pairs.sort((a,b)=>(b.h>a.h?1:b.h<a.h?-1:0));return pairs.map(x=>x.u)}
async function bridgeKey(k){const raw=b64u.dec(k);
 const topic='biruni'+hex(await sha(cat(enc.encode('biruni-bridge-1:'),raw))).slice(0,40);
 const order={nostr:await rankUrls(topic,BR.relays),mqtt:await rankUrls(topic,BR.mqtt||[])};
 BK={k:await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt','decrypt']),
  topic:topic,order:order};await idInit(k);return BK}
const canZ=typeof CompressionStream!=='undefined'&&typeof DecompressionStream!=='undefined';
async function zz(d,pack){const s=new Blob([d]).stream().pipeThrough(pack?new CompressionStream('deflate-raw'):
 new DecompressionStream('deflate-raw'));return new Uint8Array(await new Response(s).arrayBuffer())}
async function seal(obj,z){let d=enc.encode(JSON.stringify(obj)),f=0;
 if(z&&canZ){const c=await zz(d,true);if(c.length<d.length){d=c;f=1}}
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:iv},BK.k,cat(new Uint8Array([f]),d)));
 return b64u.enc(cat(iv,ct))}
async function unseal(s){try{const b=b64u.dec(s);
  const p=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:b.slice(0,12)},BK.k,b.slice(12)));
  const d=p[0]===1?await zz(p.slice(1),false):p.slice(1);return JSON.parse(dec.decode(d))}catch(e){return null}}
/* MQTT 3.1.1 wire helpers */
const MQ={
 len(n){const o=[];do{let d=n&127;n>>>=7;if(n>0)d|=128;o.push(d)}while(n>0);return o},
 str(s){const b=enc.encode(s);return cat(new Uint8Array([b.length>>8,b.length&255]),b)},
 connect(id){const b=cat(this.str('MQTT'),new Uint8Array([4,2,0,60]),this.str(id));
  return cat(new Uint8Array([0x10,...this.len(b.length)]),b)},
 subscribe(pid,topic){const b=cat(new Uint8Array([(pid>>8)&255,pid&255]),this.str(topic),new Uint8Array([0]));
  return cat(new Uint8Array([0x82,...this.len(b.length)]),b)},
 publish(topic,bytes,qos=0,pid=0){
  const p=typeof bytes==='string'?enc.encode(bytes):new Uint8Array(bytes);
  const vh=qos>0?cat(this.str(topic),new Uint8Array([(pid>>8)&255,pid&255])):this.str(topic);
  const b=cat(vh,p);return cat(new Uint8Array([0x30|((qos&3)<<1),...this.len(b.length)]),b)},
 ping(){return new Uint8Array([0xC0,0x00])},
 parse(buf){const b=buf instanceof Uint8Array?buf:new Uint8Array(buf),pk=[];let i=0;
  while(i+1<b.length){const h=b[i],type=h>>4,flags=h&15;let j=i+1,mul=1,len=0,ok=false;
   for(let k=0;k<4;k++){if(j>=b.length)break;const c=b[j++];len+=(c&127)*mul;
    if(!(c&128)){ok=true;break}mul*=128}
   if(!ok||j+len>b.length)break;
   pk.push({type:type,flags:flags,body:b.slice(j,j+len)});i=j+len}
  return {pk:pk,rest:b.slice(i)}}
};
/* the relays: control messages go to all; preview chunks go to at most 2 healthy relays with rate limiting */
class NostrRelay{constructor(url,sig){this.kind='nostr';this.url=url;this.sig=sig;this.up=false;this.wait=3000;
  this.fails=0;this.backoffUntil=0;this.oks=new Map();this.notices=[];this.closed=false;this._tm=null;
  this.openedAt=Date.now();this.open()}
 open(){if(this.closed)return;this.openedAt=Date.now();let ws;
  try{ws=new WebSocket(this.url)}catch(e){return this.later()}this.ws=ws;
  ws.onopen=()=>{this.up=true;this.wait=3000;ws.send(JSON.stringify(['REQ','b'+rid(),
   {kinds:[BR.kind],'#t':[BK.topic],since:Math.floor(Date.now()/1000)-60}]));this.sig.changed()};
  ws.onmessage=e=>{try{const m=JSON.parse(e.data);
   if(m[0]==='EVENT'&&m[2]&&typeof m[2].content==='string')this.sig.arrived(m[2].content,'nostr',this);
   else if(m[0]==='OK'&&typeof m[1]==='string'){const ok=!!m[2];
    if(!ok){this.fails++;this.backoffUntil=Date.now()+Math.min(60000,4000*Math.pow(2,this.fails-1))}
    else{this.fails=0;this.backoffUntil=0}
    const cb=this.oks.get(m[1]);if(cb){this.oks.delete(m[1]);cb(ok)}}
   else if(m[0]==='NOTICE'){const w=String(m[1]||'');this.notices.push(w);
    if(/rate|limit|slow|quota|ban|block|too/i.test(w)){this.fails++;this.backoffUntil=Date.now()+15000}}}catch(x){}};
  ws.onclose=()=>{const was=this.up;this.up=false;for(const [,cb] of this.oks)cb(false);this.oks.clear();
   if(was)this.sig.changed();if(!this.closed){this.openedAt=Date.now();this.later()}};
  ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){if(this.closed)return;clearTimeout(this._tm);
  this._tm=setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 close(){this.closed=true;clearTimeout(this._tm);const was=this.up;this.up=false;
  for(const [,cb] of this.oks)cb(false);this.oks.clear();
  try{if(this.ws)this.ws.close()}catch(x){}if(was)this.sig.changed()}
 send(s,ev){if(this.up&&ev&&Date.now()>=this.backoffUntil)try{this.ws.send(JSON.stringify(['EVENT',ev]))}catch(e){}}
 sendWait(ev,_s,ms=3500){if(typeof _s==='number')ms=_s;
  if(!this.up||!ev||Date.now()<this.backoffUntil)return Promise.resolve(false);
  return new Promise(r=>{const t=setTimeout(()=>{if(this.oks.delete(ev.id))r(false)},ms);
   this.oks.set(ev.id,ok=>{clearTimeout(t);r(ok)});
   try{this.ws.send(JSON.stringify(['EVENT',ev]))}catch(e){clearTimeout(t);this.oks.delete(ev.id);r(false)}})}}
NostrRelay.prototype.kind='nostr';
class NtfyRelay{constructor(sig){this.kind='ntfy';this.url=BR.ntfy;this.sig=sig;this.up=false;this.wait=3000;this.open()}
 open(){let ws;try{ws=new WebSocket(BR.ntfy.replace(/^http/,'ws')+BK.topic+'/ws')}catch(e){return this.later()}this.ws=ws;
  ws.onopen=()=>{this.up=true;this.wait=3000;this.sig.changed()};
  ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.event==='message'){
   if(m.attachment&&m.attachment.url)
    fetch(m.attachment.url).then(r=>r.ok?r.text():'').then(t=>{if(t)this.sig.arrived(t,'ntfy',this)}).catch(()=>{});
   else if(m.message)this.sig.arrived(m.message,'ntfy',this)}}catch(x){}};
  ws.onclose=()=>{const was=this.up;this.up=false;if(was)this.sig.changed();this.later()};ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 send(s){if(s.length<=4000)fetch(BR.ntfy+BK.topic+'?cache=no&firebase=no',{method:'POST',body:s}).catch(()=>{})}
 async sendChunk(s){try{const u=BR.ntfy+BK.topic+(s.length<=4000?'?cache=no&firebase=no':'?firebase=no'),
   h=s.length>4000?{'Filename':'p.bin'}:{};return (await fetch(u,{method:'POST',body:s,headers:h})).ok}catch(e){return false}}}
class MqttRelay{
 constructor(url,sig){this.kind='mqtt';this.url=url;this.sig=sig;this.up=false;this.wait=3000;this.fails=0;
  this.backoffUntil=0;this.oks=new Map();this.notices=[];this.pid=1;this.buf=new Uint8Array(0);
  this._ping=null;this._dead=null;this.closed=false;this._tm=null;this.openedAt=Date.now();this.open()}
 _cid(){let id='b';const r=crypto.getRandomValues(new Uint8Array(16));
  for(const x of r)id+=(x%36).toString(36);return id}
 _fail(){this.fails++;this.backoffUntil=Date.now()+Math.min(60000,4000*Math.pow(2,this.fails-1))}
 _touch(ws){clearTimeout(this._dead);this._dead=setTimeout(()=>{if(this.ws===ws)try{ws.close()}catch(x){}},60000)}
 open(){if(this.closed)return;this.openedAt=Date.now();let ws;
  try{ws=new WebSocket(this.url,'mqtt')}catch(e){return this.later()}
  ws.binaryType='arraybuffer';this.ws=ws;this.buf=new Uint8Array(0);
  ws.onopen=()=>{this._touch(ws);try{ws.send(MQ.connect(this._cid()))}catch(x){try{ws.close()}catch(y){}}};
  ws.onmessage=e=>{this._touch(ws);
   const chunk=e.data instanceof ArrayBuffer?new Uint8Array(e.data):enc.encode(String(e.data||''));
   this.buf=cat(this.buf,chunk);const {pk,rest}=MQ.parse(this.buf);this.buf=rest;
   for(const p of pk){
    if(p.type===2){if(p.body.length>=2&&p.body[1]===0){
      this.pid=(this.pid%65535)+1;ws.send(MQ.subscribe(this.pid,'biruni/1/'+BK.topic))}
     else{this._fail();try{ws.close()}catch(x){}}}
    else if(p.type===9){if(p.body.length>=3&&p.body[2]!==0x80){
      this.up=true;this.wait=3000;this.fails=0;this.backoffUntil=0;
      clearInterval(this._ping);
      this._ping=setInterval(()=>{if(this.ws===ws&&ws.readyState===1)try{ws.send(MQ.ping())}catch(x){}},25000);
      this.sig.changed()}
     else{this._fail();try{ws.close()}catch(x){}}}
    else if(p.type===3&&p.body.length>=2){
     const tl=(p.body[0]<<8)|p.body[1],qos=(p.flags>>1)&3,off=2+tl+(qos>0?2:0);
     if(qos===1&&p.body.length>=4+tl)try{ws.send(new Uint8Array([0x40,2,p.body[2+tl],p.body[3+tl]]))}catch(x){}
     if(off<=p.body.length)this.sig.arrived(dec.decode(p.body.subarray(off)),'mqtt',this)}
    else if(p.type===4&&p.body.length>=2){
     const pid=(p.body[0]<<8)|p.body[1],cb=this.oks.get(pid);if(cb){this.oks.delete(pid);cb(true)}}}};
  ws.onclose=()=>{clearInterval(this._ping);clearTimeout(this._dead);const was=this.up;this.up=false;
   for(const [,cb] of this.oks)cb(false);this.oks.clear();if(was)this.sig.changed();
   if(!this.closed){this.openedAt=Date.now();this.later()}};
  ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){if(this.closed)return;clearTimeout(this._tm);
  this._tm=setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 close(){this.closed=true;clearTimeout(this._tm);clearInterval(this._ping);clearTimeout(this._dead);
  const was=this.up;this.up=false;for(const [,cb] of this.oks)cb(false);this.oks.clear();
  try{if(this.ws)this.ws.close()}catch(x){}if(was)this.sig.changed()}
 send(s){if(!this.up||Date.now()<this.backoffUntil||typeof s!=='string'||s.length>90000)return;
  try{this.ws.send(MQ.publish('biruni/1/'+BK.topic,s,0,0))}catch(e){}}
 sendWait(ev,s,ms=3500){if(typeof ev==='string'&&typeof s!=='string'){ms=typeof s==='number'?s:3500;s=ev}
  if(!this.up||Date.now()<this.backoffUntil||typeof s!=='string'||s.length>90000)return Promise.resolve(false);
  const pid=this.pid=(this.pid%65535)+1;
  return new Promise(r=>{const t=setTimeout(()=>{if(this.oks.delete(pid))r(false)},ms);
   this.oks.set(pid,ok=>{clearTimeout(t);r(ok)});
   try{this.ws.send(MQ.publish('biruni/1/'+BK.topic,s,1,pid))}
   catch(e){clearTimeout(t);this.oks.delete(pid);r(false)}})}}
class TunnelRelay{
 constructor(h,sig){this.kind='tunnel';this.h=h;this.sig=sig;this.base=BR.tun.replace('{h}',h).replace(/\/+$/,'');
  this.up=false;this.fails=0;this.backoffUntil=0;this.cur=0;this.closed=false;this.lastPost=0;this._inflight=0;
  this.post('');this._timer=setInterval(()=>{const n=Date.now();
   if(!this.closed&&!this._inflight&&n>=this.backoffUntil&&n-this.lastPost>=4000)this.post('')},1000)}
 async post(s){if(this.closed)return false;this.lastPost=Date.now();this._inflight++;
  const c=new AbortController(),t=setTimeout(()=>c.abort(),8000);
  try{const r=await fetch(this.base+'/x/'+BK.topic+'?a='+this.cur,{method:'POST',body:s,
    headers:{'Content-Type':'text/plain'},signal:c.signal});
   clearTimeout(t);
   if(r.status!==200)throw new Error(String(r.status));
   const j=await r.json();if(typeof j.a==='number'&&j.a>this.cur)this.cur=j.a;
   if(Array.isArray(j.m))for(const m of j.m)if(typeof m==='string')this.sig.arrived(m,'tunnel',this);
   const was=this.up;this.up=true;this.fails=0;this.backoffUntil=0;if(!was)this.sig.changed();return true}
  catch(e){clearTimeout(t);this.fails++;
   if(this.fails>=3){this.backoffUntil=Date.now()+Math.min(60000,5000*Math.pow(2,this.fails-3));
    if(this.up){this.up=false;this.sig.changed()}}
   return false}
  finally{this._inflight--}}
 send(s){if(!this.closed&&Date.now()>=this.backoffUntil)this.post(s).catch(()=>{})}
 sendWait(ev,s){if(typeof ev==='string'&&typeof s!=='string')s=ev;
  if(this.closed||Date.now()<this.backoffUntil||typeof s!=='string')return Promise.resolve(false);
  return this.post(s)}
 close(){this.closed=true;clearInterval(this._timer);const was=this.up;this.up=false;if(was)this.sig.changed()}}
class OwnRelay{
 constructor(sig,url=BR.own,kind='own'){this.kind=kind;this.url=url;this.sig=sig;this.up=false;this.wait=3000;
  this.fails=0;this._bo=0;this.backoffUntil=0;this._ping=null;this._opened=false;this._tm=0;this.closed=false;this.open()}
 _backoff(){this._bo=this._bo?Math.min(600000,this._bo*2):60000;this.backoffUntil=Date.now()+this._bo}
 open(){if(this.closed||!this.url)return;const left=this.backoffUntil-Date.now();
  if(left>0){clearTimeout(this._tm);this._tm=setTimeout(()=>this.open(),left);return}
  const base=this.url.endsWith('/')?this.url:this.url+'/';
  let ws;try{ws=new WebSocket(base+BK.topic)}catch(e){this.fails++;
   if(this.fails>=3)this._backoff();return this.later()}
  this.ws=ws;this._opened=false;
  ws.onopen=()=>{this._opened=true;this.up=true;this.wait=3000;this.fails=0;this._bo=0;this.backoffUntil=0;
   clearInterval(this._ping);const pingMs=this.kind==='own'?55000:25000;
   this._ping=setInterval(()=>{if(this.ws===ws&&ws.readyState===1)try{ws.send('p')}catch(x){}},pingMs);
   this.sig.changed()};
  ws.onmessage=e=>{if(typeof e.data==='string'&&e.data!=='q')this.sig.arrived(e.data,this.kind,this)};
  ws.onclose=e=>{clearInterval(this._ping);const was=this.up;this.up=false;if(was)this.sig.changed();
   if(this.closed)return;
   if(e&&e.code===1013)this._backoff();
   else if(!this._opened){this.fails++;if(this.fails>=3)this._backoff()}
   this.later()};
  ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){if(this.closed)return;const ms=Math.max(this.wait+Math.random()*1000,this.backoffUntil-Date.now());
  clearTimeout(this._tm);this._tm=setTimeout(()=>this.open(),ms);this.wait=Math.min(this.wait*2,60000)}
 close(){this.closed=true;clearTimeout(this._tm);clearInterval(this._ping);const was=this.up;this.up=false;
  try{if(this.ws)this.ws.close()}catch(x){}if(was)this.sig.changed()}
 send(s){if(this.up&&Date.now()>=this.backoffUntil&&typeof s==='string'&&s.length<=90000)
  try{this.ws.send(s)}catch(e){}}
 sendWait(ev,s){if(typeof ev==='string'&&typeof s!=='string')s=ev;
  if(!this.up||Date.now()<this.backoffUntil||typeof s!=='string'||s.length>90000)return Promise.resolve(false);
  try{this.ws.send(s);return Promise.resolve(this.up)}catch(e){return Promise.resolve(false)}}}
const SIG={relays:[],seen:new Set(),loose:new Set(),on:null,onchange:null,_lastChunk:0,_tun:null,_org:null,_own:null,
 _hc:null,_lazyTm:null,_startedAt:0,_lastPcAt:0,_pcHeard:false,_goodSince:0,_tried:null,
 notePc(){this._pcHeard=true;this._lastPcAt=Date.now()},
 _pick(kind){
  const pool=(BK&&BK.order&&BK.order[kind])||(kind==='nostr'?BR.relays:BR.mqtt)||[];
  const leg=(kind==='nostr'?BR.legacyRelays:BR.legacyMqtt)||[];
  const cfg=(BR.poolK&&BR.poolK[kind])||{phone:3,host:5,scan:8};
  if(HOSTSIDE){const top=pool.slice(0,cfg.host),ls=new Set(leg),anc=pool.find(u=>ls.has(u))||leg[0];
   if(anc&&!top.includes(anc))top.push(anc);return top}
  if(POOLV)return pool.slice(0,cfg.phone);
  return leg.slice()},
 _checkOwn(){if(HOSTSIDE||!BR.own)return;const now=Date.now();
  const p=typeof LAD==='object'&&LAD.primary?LAD.primary(now):null;
  const good=!!(p&&(p.id==='direct'||p.id==='tunnel'||p.id==='org'));
  if(good){if(!this._goodSince)this._goodSince=now;
   if(this._own&&(now-this._goodSince)>=60000){this._own.close();this.relays=this.relays.filter(r=>r!==this._own);this._own=null}
   return}
  this._goodSince=0;if(this._own)return;
  const lm=BR.ownLazyMs!=null?BR.ownLazyMs:10000;
  if(typeof lineOpen==='function'&&lineOpen())return;
  if((!this._pcHeard&&(now-this._startedAt)>=lm)||(typeof LAD==='object'&&(now-this._lastPcAt)>=lm)){
   this._own=new OwnRelay(this,BR.own,'own');this.relays.push(this._own)}},
 _health(){const now=Date.now();
  for(let i=0;i<this.relays.length;i++){const r=this.relays[i];
   if(r.kind!=='nostr'&&r.kind!=='mqtt')continue;
   if(!((!r.up&&(now-(r.openedAt||0))>=8000)||(r.fails||0)>=2))continue;
   const pool=(BK&&BK.order&&BK.order[r.kind])||(r.kind==='nostr'?BR.relays:BR.mqtt)||[];
   const scan=((BR.poolK&&BR.poolK[r.kind])||{}).scan||pool.length;
   const tried=this._tried[r.kind]||(this._tried[r.kind]=new Set());
   const nextUrl=pool.slice(0,scan).find(u=>!tried.has(u));
   if(!nextUrl)continue;
   tried.add(nextUrl);r.close();
   this.relays[i]=r.kind==='nostr'?new NostrRelay(nextUrl,this):new MqttRelay(nextUrl,this)}
  this._checkOwn()},
 start(onMsg,onChange){this.on=onMsg;this.onchange=onChange;clearInterval(this._hc);clearTimeout(this._lazyTm);
  if(this._own){this._own.close();this._own=null}
  if(this._org){this._org.close();this._org=null}
  if(this._tun){this._tun.close();this._tun=null}
  this._startedAt=Date.now();this._lastPcAt=this._startedAt;this._pcHeard=false;this._goodSince=0;
  const nUrls=this._pick('nostr'),mUrls=this._pick('mqtt');
  this._tried={nostr:new Set(nUrls),mqtt:new Set(mUrls)};
  this.relays=nUrls.map(u=>new NostrRelay(u,this)).concat([new NtfyRelay(this)])
   .concat(mUrls.map(u=>new MqttRelay(u,this)));
  if(HOSTSIDE&&BR.own){this._own=new OwnRelay(this,BR.own,'own');this.relays.push(this._own)}
  else if(!HOSTSIDE&&BR.own){const lm=BR.ownLazyMs!=null?BR.ownLazyMs:10000;
   this._lazyTm=setTimeout(()=>this._checkOwn(),lm)}
  this._hc=setInterval(()=>this._health(),4000)},
 tunnel(h){if(!BR.tun||!/^[a-z0-9-]{3,80}$/.test(String(h||'')))return null;
  if(this._tun&&this._tun.h===h&&!this._tun.closed)return this._tun;
  if(this._tun){this._tun.close();this.relays=this.relays.filter(r=>r!==this._tun)}
  const r=new TunnelRelay(h,this);this._tun=r;this.relays.push(r);return r},
 org(h){const v=String(h||'').trim().toLowerCase();
  if(!BR.org||!/^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$/.test(v))return null;
  if(this._org&&this._org.h===v&&!this._org.closed)return this._org;
  if(this._org){this._org.close();this.relays=this.relays.filter(r=>r!==this._org)}
  const r=new OwnRelay(this,BR.org.replace('{h}',v),'org');r.h=v;this._org=r;this.relays.push(r);return r},
 up(){return this.relays.filter(r=>r.up).length},
 hosts(){const out=[];for(const r of this.relays)if(r.up&&r.url){try{
   const h=new URL(r.url).hostname;if(h&&!out.includes(h))out.push(h)}catch(e){}}return out},
 kinds(){const k={};for(const r of this.relays){const n=r.kind||'nostr';k[n]=(k[n]||0)+(r.up?1:0)}return k},
 healthyAck(){const ord={org:0,mqtt:1,nostr:2,own:3};
  return this.relays.filter(r=>r.up&&typeof r.sendWait==='function'&&Date.now()>=(r.backoffUntil||0))
   .sort((a,b)=>((ord[a.kind]??9)-(ord[b.kind]??9))||((a.fails||0)-(b.fails||0)))},
 healthyNostr(){return this.healthyAck()},
 changed(){if(this.onchange)this.onchange(this.up())},
 async _sign(obj){
  if(obj.t==='answer'&&ID.sk){const g=await IDSIGN('BRA1|'+obj.sid+'|'+obj.sdp);obj.ip=g.pub;obj.is=g.sig}
  else if(obj.t==='rhello'&&ID.sk){const g=await IDSIGN('BRH0|'+obj.sid+'|'+(obj.pk||''));obj.ip=g.pub;obj.is=g.sig}
  else if(obj.t==='rack'&&ID.sk){
   const g=await IDSIGN('BRAK1|'+obj.id+'|'+obj.kind+'|'+obj.nonce);obj.ip=g.pub;obj.is=g.sig;
   if(obj.h!=null||obj.c!=null||obj.rt!=null){
    const g2=await IDSIGN('BRAK2|'+obj.id+'|'+obj.kind+'|'+obj.nonce+'|'+(obj.h||'')+'|'+(obj.c??0)+'|'+(obj.rt??0));
    if(g2)obj.is2=g2.sig}}
  else if(obj.t==='rcgot'&&ID.sk){
   const s=(obj.items||[]).map(x=>x[0]+':'+x[1]+':'+(x[2]||'')).join(',');
   const g=await IDSIGN('BRCP1|'+obj.nonce+'|'+(obj.rt||0)+'|'+s);obj.ip=g.pub;obj.is=g.sig}
  else if(obj.t==='tun'&&ID.sk){const g=await IDSIGN('BRTN1|'+obj.h);obj.ip=g.pub;obj.is=g.sig}
  else if(obj.t==='org'&&ID.sk){const g=await IDSIGN('BROR1|'+obj.h);obj.ip=g.pub;obj.is=g.sig}},
 async send(obj,z){obj.n=rid();obj.ts=NOW();this.seen.add(obj.n);await this._sign(obj);
  const s=await seal(obj,z);let ev=null;
  try{ev=await nostrEvent(s,BK.topic)}catch(e){}for(const r of this.relays)r.send(s,ev)},
 async sendRaw(s,kind){if(typeof s!=='string')return false;
  const list=this.relays.filter(r=>(r.kind||'nostr')===kind&&r.up&&Date.now()>=(r.backoffUntil||0))
   .sort((a,b)=>(a.fails||0)-(b.fails||0)).slice(0,1);
  if(!list.length)return false;let ev=null;
  if(kind==='nostr')try{ev=await nostrEvent(s,BK.topic)}catch(e){return false}
  for(const r of list)r.send(s,ev);return true},
 async sendOn(relay,s){
  if(!relay||typeof s!=='string'||!relay.up||Date.now()<(relay.backoffUntil||0))return false;
  let ev=null;
  if((relay.kind||'nostr')==='nostr')try{ev=await nostrEvent(s,BK.topic)}catch(e){return false}
  relay.send(s,ev);return true},
 async sendChunk(obj){
  const pace=BR.paceMs!=null?BR.paceMs:180,wait=Math.max(0,this._lastChunk+pace-Date.now());
  if(wait)await sleep(wait);this._lastChunk=Date.now();
  obj.n=rid();obj.ts=NOW();this.seen.add(obj.n);const s=await seal(obj,true);
  let ev=null;try{ev=await nostrEvent(s,BK.topic)}catch(e){}
  const tried=new Set();
  while(true){const pair=this.healthyAck().filter(r=>!tried.has(r)&&(ev||(r.kind||'nostr')!=='nostr')).slice(0,2);
   if(!pair.length)break;
   pair.forEach(r=>tried.add(r));const res=await Promise.all(pair.map(r=>r.sendWait(ev,s)));
   if(res.some(Boolean))return true}
  const nr=this.relays.find(r=>r.kind==='ntfy');return nr?await nr.sendChunk(s):false},
 async arrived(s,kind,relay){const o=await unseal(s);if(!o||!o.n||this.seen.has(o.n))return;this.seen.add(o.n);
  if(this.seen.size>4000)this.seen.delete(this.seen.values().next().value);
  if(o.t==='ltime'){
   if(ID.pin&&!await IDOK(o.ip,o.is,'BRLT1|'+o.now)){this.refused=(this.refused||0)+1;return}
   noteTime(o.now);this.notePc();if(this.on)this.on(o,kind,s,relay);return}
  if(Math.abs(NOW()-(o.ts||0))>15*60000)return;
  /* answers and receipts to this phone must come from the PC (signed) */
  if(o.t==='answer'&&ID.pin&&!this.loose.has(o.sid)&&!await IDOK(o.ip,o.is,'BRA1|'+o.sid+'|'+o.sdp)){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='rhello'&&ID.pin&&!await IDOK(o.ip,o.is,'BRH0|'+o.sid+'|'+(o.pk||''))){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='rack'&&ID.pin&&!await IDOK(o.ip,o.is,'BRAK1|'+o.id+'|'+o.kind+'|'+o.nonce)){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='rcgot'&&ID.pin){
   const cs=(Array.isArray(o.items)?o.items:[]).map(x=>x[0]+':'+x[1]+':'+(x[2]||'')).join(',');
   if(!await IDOK(o.ip,o.is,'BRCP1|'+o.nonce+'|'+(o.rt||0)+'|'+cs)){this.refused=(this.refused||0)+1;return}}
  if(o.t==='lack'&&ID.pin){
   if(!await IDOK(o.ip,o.is,'BRLA1|'+o.d+'|'+o.e+'|'+o.upto)){this.refused=(this.refused||0)+1;return}
   if(o.is2!=null&&!await IDOK(o.ip,o.is2,'BRLA2|'+o.d+'|'+o.e+'|'+o.upto+'|'+(o.u2||0))){this.refused=(this.refused||0)+1;return}}
  if(o.t==='tun'&&ID.pin&&!await IDOK(o.ip,o.is,'BRTN1|'+o.h)){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='org'&&ID.pin&&!await IDOK(o.ip,o.is,'BROR1|'+o.h)){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='answer'||o.t==='rhello'||o.t==='rguide'||o.t==='rack'||o.t==='rcgot'||o.t==='lack'||o.t==='tun'||o.t==='org'
   ||(typeof o.t==='string'&&o.t.startsWith('mesh_')))this.notePc();
  if(this.on)this.on(o,kind,s,relay)}};
/* a private line through a teammate's phone: the phone and the PC agree on a key of their own (ECDH on P-256),
   so the phone that carries the messages cannot read them. Every frame after the first is sealed; its first
   byte says what it is (0 a message, 1 a piece of a photo). */
const E2E={async pair(){const kp=await crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},false,['deriveBits']);
  return {kp:kp,pub:b64u.enc(await crypto.subtle.exportKey('raw',kp.publicKey))}},
 async key(kp,pub){const pk=await crypto.subtle.importKey('raw',b64u.dec(pub),{name:'ECDH',namedCurve:'P-256'},false,[]);
  const bits=await crypto.subtle.deriveBits({name:'ECDH',public:pk},kp.privateKey,256);
  return crypto.subtle.importKey('raw',await sha(new Uint8Array(bits)),'AES-GCM',false,['encrypt','decrypt'])},
 async seal(k,kind,bytes){const iv=crypto.getRandomValues(new Uint8Array(12));
  return cat(iv,new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:iv},k,cat(new Uint8Array([kind]),bytes))))},
 async open(k,bytes){const b=new Uint8Array(bytes);
  return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:b.slice(0,12)},k,b.slice(12)))}};
function gathered(pc,ms){return new Promise(r=>{if(pc.iceGatheringState==='complete')return r();
 const t=setTimeout(r,ms);pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){
 clearTimeout(t);r()}})})}
"""

# ---- the phone: the photo page of server.py, sending over the bridge ------------------------
BOOT_JS = r"""
let HP=new URLSearchParams(location.hash.slice(1)),KEYB=HP.get('k')||'';
if(!KEYB){const _kl=kept('biruni-last-link')||'';if(_kl){const _fi=_kl.indexOf('#');
 const _hp=new URLSearchParams(_fi>=0?_kl.slice(_fi+1):_kl),_k=_hp.get('k')||'';
 if(/^[A-Za-z0-9_-]{43}$/.test(_k)){HP=_hp;KEYB=_k}}}
HASHL=HP.get('l');ROOM=KEYB.slice(0,12);
if(KEYB&&/^[A-Za-z0-9_-]{43}$/.test(KEYB)&&location.hash)keep('biruni-last-link',location.href);
const _TH=HP.get('t')||'';
const TUNH=/^[a-z0-9-]{3,80}$/.test(_TH)?(keep('biruni-tun-'+ROOM,_TH),_TH):(kept('biruni-tun-'+ROOM)||'');
const _ORG_RE=/^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$/;
const _OH=(HP.get('o')||'').trim().toLowerCase();
const _OK=_ORG_RE.test(_OH)?(keep('biruni-org-'+ROOM,_OH),_OH):(kept('biruni-org-'+ROOM)||'');
const ORGH=_ORG_RE.test(_OK)?_OK:'';
GPSMODE=HP.get('m')==='gps';const VIAONLY=HP.get('v')==='1',GDRIVE=GPSMODE&&HP.get('g')==='1',DRFULL=HP.get('f')==='1';
/* d=1&pk=: this project takes photos through Google Drive; pk is the computer's key, so photos are sealed for it
   before the phone ever reaches the computer (drive_page.py) */
const DPK=HP.get('pk')||'',DRIVE_ON=HP.get('d')==='1'&&/^[\w-]{81,99}$/.test(DPK)&&!GPSMODE;if(DRIVE_ON)PCKEY=DPK;
if('serviceWorker' in navigator&&location.protocol==='https:'&&/(^|\.)biruni-gis\.com$|\.github\.io$/.test(location.hostname))
 navigator.serviceWorker.register('sw.js').catch(()=>{});   /* opens again in the field without internet *//* v=1: never direct, always through a teammate (to try it) */
try{const c=JSON.parse(kept('biruni-hello-'+ROOM)||'null');if(c&&Array.isArray(c.kinds))KINDS=c.kinds;
 if(c&&Array.isArray(c.form))FORM=c.form}catch(e){}
"""

PHONE_JS = r"""
/* the phone sends over the bridge: relays to meet, then a direct channel to the PC - or, where the network
   allows none, a channel to a teammate's phone that carries the messages to the PC */
let pc=null,dc=null,sid=null,linking=false,retry=null,backoff=4000,lastState='',acks=new Map();
let VIA=null,RELAY=false,rsid=rid();   /* VIA: through a teammate; RELAY: encrypted previews over relays when P2P blocked */
const carried=new Map();      /* teammates this phone carries to the PC: their request's id -> their channel */
function say(on,key){lastState=key;status(on,key)}
const direct=()=>!!(dc&&dc.readyState==='open');
const lineOpen=()=>direct()||!!(VIA&&VIA.ready);
const relayOnly=()=>!lineOpen()&&RELAY;
function lineText(s){if(direct())return dc.send(s);const v=VIA;if(!v||!v.ready)throw new Error('closed');
 v.q=v.q.then(async()=>v.ch.send(await E2E.seal(v.key,0,enc.encode(s)))).catch(()=>{})}
function lineBin(u8){if(direct())return dc.send(u8);const v=VIA;if(!v||!v.ready)throw new Error('closed');
 v.pend+=u8.length;v.q=v.q.then(async()=>{v.ch.send(await E2E.seal(v.key,1,u8));v.pend-=u8.length}).catch(()=>{})}
const lineBuffered=()=>direct()?dc.bufferedAmount:VIA?VIA.ch.bufferedAmount+VIA.pend:0;
async function sendPos(p,force){
 const pts=(GPSMODE&&!DRFULL&&typeof DR==='object')?DR.offer(p,force):[p];
 for(const pt of pts){if(typeof LAD==='object'&&LAD.cap)await LAD.push(pt);
  else if(lineOpen())lineText(JSON.stringify({t:'pos',p:pt}));
  else if(RELAY&&SIG.up())SIG.send({t:'rpos',p:pt},true)}}
function pollGuide(){}
function hello(m){if(Array.isArray(m.kinds))KINDS=m.kinds.map(String).slice(0,24);drawKinds();
 if(typeof m.pk==='string'&&m.pk.length>80&&m.pk.length<100){const npk=m.pk;
  /* with a signed list kept, an unsigned hello cannot move the office key: only a list naming the new key can */
  (typeof rlPk==='function'?rlPk(npk,m.rl||null):Promise.resolve(true)).then(ok=>{
   if(!ok){if(typeof SIG==='object'&&SIG)SIG.refused=(SIG.refused||0)+1;return}
   PCKEY=npk;keep('biruni-pckey-'+ROOM,npk);sealWaiting()})}
 else if(m.rl&&typeof rlTake==='function')rlTake(m.rl).then(ok=>{if(ok)sealWaiting()});
 if(Array.isArray(m.form)){FORM=m.form.filter(f=>f&&f.name&&f.label).slice(0,30);drawForm()}
 if(m&&m.nl===1&&typeof setNl==='function')setNl(true);
 if(m&&typeof m.now==='number')noteTime(m.now);
 if(m&&typeof m.eod==='number'&&typeof setEodHour==='function')setEodHour(m.eod);
 if(typeof LAD==='object')LAD.onCap(m);
 if(!kept('biruni-lang')&&!HASHL&&pick(m.lang))setLang(pick(m.lang));keep('biruni-hello-'+ROOM,JSON.stringify({kinds:KINDS,form:FORM}))}
let COPY_REQ=null;
async function copyPoll(){
 const a=(await all()).filter(x=>{const s=typeof itemSt==='function'?itemSt(x):(x.st||0);return s===1||s===2}).slice(0,100);
 if(!a.length)return;
 const ids=a.map(x=>x.id),nonce=rid();COPY_REQ={nonce:nonce,ids:new Set(ids)};
 if(lineOpen()){try{lineText(JSON.stringify({t:'chave',nonce:nonce,ids:ids}))}catch(e){}}
 else if(SIG.up()){try{await SIG.send({t:'rchave',nonce:nonce,ids:ids},true)}catch(e){}}}
async function handleCopyGot(m){
 const q=COPY_REQ;if(!q||!m||m.nonce!==q.nonce||!Array.isArray(m.items))return;
 const cs=m.items.map(x=>Array.isArray(x)?(x[0]+':'+x[1]+':'+(x[2]||'')):'').join(',');
 if(ID.pin&&!await IDOK(m.ip,m.is,'BRCP1|'+m.nonce+'|'+(m.rt||0)+'|'+cs)){SIG.refused=(SIG.refused||0)+1;return}
 COPY_REQ=null;if(typeof m.rt==='number')noteTime(m.rt);
 for(const row of m.items){if(!Array.isArray(row)||row.length<2)continue;
  const id=String(row[0]||''),c=+row[1]||0,h8=String(row[2]||'');
  if(!q.ids.has(id))continue;
  const it=typeof getItem==='function'?await getItem(id):(await all()).find(x=>x.id===id);
  if(!it)continue;
  if(it.h&&h8&&!it.h.startsWith(h8)){SIG.refused=(SIG.refused||0)+1;continue}
  if(c>=2&&typeof setState==='function')await setState(id,3,c);
  else if(c===1&&typeof setState==='function'&&(typeof itemSt==='function'?itemSt(it)<2:(it.st||0)<2))await setState(id,2,1)}}
async function handleAck(m){const f=acks.get(m.id);if(!f)return;
 if(m.status===200){const k=m.kind||'original',n=m.nonce||'';
  if(k!==f.kind||n!==f.nonce||!await IDOK(m.ip,m.is,'BRAK1|'+m.id+'|'+k+'|'+n)){
   SIG.refused=(SIG.refused||0)+1;return}
  if(k==='original'&&(m.is2!=null||m.h!=null||m.c!=null)){
   if(m.is2!=null&&!await IDOK(m.ip,m.is2,'BRAK2|'+m.id+'|'+k+'|'+n+'|'+(m.h||'')+'|'+(m.c??0)+'|'+(m.rt??0))){
    SIG.refused=(SIG.refused||0)+1;return}
   if(f.h&&m.h&&f.h!==m.h){SIG.refused=(SIG.refused||0)+1;return}
   if(typeof m.rt==='number')noteTime(m.rt);
   acks.delete(m.id);f.resolve({status:200,c:typeof m.c==='number'?m.c:1,h:m.h||'',rt:m.rt||0,nl:true});return}}
 acks.delete(m.id);
 if(m.status!==200&&m.reason)f.resolve({status:m.status,reason:String(m.reason)});
 else f.resolve(m.status===200&&f.kind==='preview'?202:m.status)}
/* what the PC says, however it comes */
function fromPc(m){SIG.notePc();if(m.t==='hello')hello(m);else if(m.t==='guide')setGuide(m.g);
 else if(m.t==='ack')handleAck(m);else if(m.t==='cgot')handleCopyGot(m);
 else if(m.t==='dgot'&&typeof driveGot==='function')driveGot(m);
 else if(m.t==='la'&&typeof m.s==='string')SIG.arrived(m.s,'direct');
 else if(m.t==='tun'&&typeof LAD==='object'){
  if(!ID.pin)LAD.onTun(m.h);
  else IDOK(m.ip,m.is,'BRTN1|'+m.h).then(ok=>{if(ok)LAD.onTun(m.h);else SIG.refused=(SIG.refused||0)+1})}
 else if(m.t==='org'&&typeof LAD==='object'){
  if(!ID.pin)LAD.onOrg(m.h);
  else IDOK(m.ip,m.is,'BROR1|'+m.h).then(ok=>{if(ok)LAD.onOrg(m.h);else SIG.refused=(SIG.refused||0)+1})}}
function failAcks(){for(const [id,f] of acks)if(f.kind==='original'||f.line){acks.delete(id);f.reject(new Error('closed'))}}
function drop(){const p=pc;pc=null;dc=null;linking=false;if(p)try{p.close()}catch(e){}
 for(const [k,c] of carried){try{if(c)c.close()}catch(e){}}carried.clear();if(!lineOpen())failAcks()}
function dropVia(){const v=VIA;VIA=null;if(v&&v.pc)try{v.pc.close()}catch(e){}if(!lineOpen())failAcks()}
function later(){clearTimeout(retry);retry=setTimeout(connect,backoff);
 const cap=typeof LAD==='object'&&LAD.primary()?300000:60000;backoff=Math.min(backoff*2,cap)}
function startRelay(){if(lineOpen()||!SIG.up())return;SIG.send({t:'rhi',sid:rsid,ua:dev},true)}
async function connect(){if(!BK||linking||lineOpen())return;
 if(!SIG.up()){say(false,'connecting');return later()}
 if(VIAONLY){say(false,'connecting');help();return later()}
 linking=true;if(!RELAY)say(false,'connecting');const my=sid=rid();
 try{pc=new RTCPeerConnection({iceServers:BR.ice});const ch=dc=pc.createDataChannel('photos',{ordered:true});
  ch.binaryType='arraybuffer';const me=pc;
  ch.onopen=()=>{linking=false;RELAY=false;backoff=4000;dropVia();ch.send(JSON.stringify({t:'hi',ua:dev}));say(true,'connected');
   lineFresh();flush();if(typeof drivePump==='function')drivePump();tellPos(true)};
  ch.onclose=()=>{if(pc===me){drop();say(false,RELAY?'relayMode':'lost');later()}};
  ch.onmessage=e=>{if(typeof e.data!=='string')return;let m;try{m=JSON.parse(e.data)}catch(x){return}
   if(m.t==='fwd'&&m.to){const c=carried.get(m.to);   /* the PC answers a teammate this phone carries */
    if(c&&c.readyState==='open')try{c.send(m.b?b64u.dec(m.b):m.m)}catch(x){}}
   else fromPc(m)};
  pc.oniceconnectionstatechange=()=>{if(pc!==me)return;const s=me.iceConnectionState;
   if(s==='failed'||s==='disconnected'){drop();say(false,RELAY?'relayMode':'p2pfail');startRelay();help();later()}};
  await me.setLocalDescription(await me.createOffer());await gathered(me,3000);
  if(pc!==me)return;
  await SIG.send({t:'offer',sid:my,sdp:me.localDescription.sdp,z:canZ},true);
  await sleep(BR.directMs||10000);
  if(pc===me&&!direct()){const gotAns=me.signalingState!=='have-local-offer';drop();
   if(gotAns){say(false,'relayMode');startRelay();help()}
   else{say(false,RELAY?'relayMode':'pcoff');startRelay()}
   later()}
 }catch(e){drop();say(false,RELAY?'relayMode':'lost');startRelay();later()}}
/* no direct channel to the PC: ask the team. A teammate's phone that reaches the PC answers and carries. */
async function help(){if(VIA||direct()||!SIG.up())return;
 const v=VIA={ready:false,q:Promise.resolve(),rq:Promise.resolve(),sid:rid(),pend:0,key:null};SIG.loose.add(v.sid);
 try{v.kp=await E2E.pair();v.pc=new RTCPeerConnection({iceServers:BR.ice});
  const ch=v.ch=v.pc.createDataChannel('via',{ordered:true});ch.binaryType='arraybuffer';
  ch.onopen=()=>ch.send(JSON.stringify({t:'hi',ua:dev,e:v.kp.pub}));   /* the one clear message: the public key */
  ch.onclose=()=>{if(VIA===v){dropVia();if(!direct()){say(false,'lost');later()}}};
  ch.onmessage=e=>{v.rq=v.rq.then(async()=>{
    if(typeof e.data==='string'){let m;try{m=JSON.parse(e.data)}catch(x){return}
     if(m.t==='hello'&&m.e&&!v.key){if(!await IDOK(m.ip,m.is,'BRH1|'+v.kp.pub+'|'+m.e)){SIG.refused=(SIG.refused||0)+1;
       if(VIA===v)dropVia();return}   /* the teammate swapped the PC's key: refused */
      v.key=await E2E.key(v.kp.kp,m.e);v.ready=true;backoff=4000;hello(m);
      say(true,'connected');if(T.via)toast(T.via);lineFresh();flush();if(typeof drivePump==='function')drivePump();
      tellPos(true)}return}
    if(!v.key)return;const p=await E2E.open(v.key,e.data);if(p[0]===0)fromPc(JSON.parse(dec.decode(p.slice(1))))}).catch(()=>{})};
  v.pc.oniceconnectionstatechange=()=>{if(VIA===v&&v.pc.iceConnectionState==='failed')dropVia()};
  await v.pc.setLocalDescription(await v.pc.createOffer());await gathered(v.pc,3000);
  if(VIA!==v)return;
  await SIG.send({t:'help',sid:v.sid,sdp:v.pc.localDescription.sdp,z:canZ},true);
  await sleep(30000);if(VIA===v&&!v.ready)dropVia()
 }catch(e){if(VIA===v)dropVia()}}
/* a teammate asks: this phone reaches the PC, so it carries. What it carries is sealed for the PC alone. */
async function carry(o){if(!o.sid||carried.has(o.sid)||!direct()||carried.size>=4||(VIA&&VIA.sid===o.sid))return;
 const p=new RTCPeerConnection({iceServers:BR.ice});carried.set(o.sid,null);
 const end=()=>{if(!carried.has(o.sid))return;carried.delete(o.sid);try{p.close()}catch(e){}
  if(direct())try{dc.send(JSON.stringify({t:'fwdx',from:o.sid}))}catch(e){}};
 p.ondatachannel=e=>{const ch=e.channel;ch.binaryType='arraybuffer';carried.set(o.sid,ch);
  ch.onmessage=m=>{if(!direct())return end();
   if(typeof m.data==='string')dc.send(JSON.stringify({t:'fwd',from:o.sid,m:m.data}));
   else{dc.send(JSON.stringify({t:'fwdb',from:o.sid}));dc.send(m.data)}};   /* the two together: nothing between */
  ch.onclose=end};
 p.oniceconnectionstatechange=()=>{const s=p.iceConnectionState;if(s==='failed'||s==='closed')end()};
 setTimeout(()=>{if(carried.has(o.sid)&&!carried.get(o.sid))end()},45000);
 try{await p.setRemoteDescription({type:'offer',sdp:o.sdp});await p.setLocalDescription(await p.createAnswer());
  await gathered(p,3000);await SIG.send({t:'answer',sid:o.sid,sdp:p.localDescription.sdp},!!o.z)}catch(e){end()}}
async function onSignal(o,kind){
 if(o.t==='answer'&&o.sid===sid&&pc&&pc.signalingState==='have-local-offer'){
  try{await pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){drop();later()}}
 else if(o.t==='answer'&&VIA&&o.sid===VIA.sid&&VIA.pc&&VIA.pc.signalingState==='have-local-offer'){
  try{await VIA.pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){dropVia()}}   /* the first teammate to answer */
 else if(o.t==='help')carry(o);
 else if(o.t==='rhello'&&(!o.sid||o.sid===rsid)){hello(o);if(!lineOpen()){RELAY=true;say(false,'relayMode');flush();tellPos(true)}}
 else if(o.t==='rguide')setGuide(o.g);
 else if(o.t==='rack')handleAck(o);
 else if(o.t==='rcgot')handleCopyGot(o);
 else if(o.t==='ltime'){if(typeof LAD==='object')LAD.tick()}
 else if(o.t==='lack'&&typeof LAD==='object')LAD.onAck(o,kind);
 else if(o.t==='tun'&&typeof LAD==='object')LAD.onTun(o.h);
 else if(o.t==='org'&&typeof LAD==='object')LAD.onOrg(o.h)}
async function sendRelayPreview(it){
 if(!PCKEY)throw new Error('nokey');
 if(!it.prev&&it.meta&&!it.meta.sealed)it=await put(it);
 const pblob=it.prev||it.blob;if(!pblob)throw new Error('noprev');
 const buf=new Uint8Array(await pblob.arrayBuffer()),cs=BR.chunkSize||24576,k=Math.max(1,Math.ceil(buf.length/cs)),nonce=rid();
 const pmeta=Object.assign({},it.meta,{preview:true});
 const done=new Promise((resolve,reject)=>{acks.set(it.id,{kind:'preview',nonce:nonce,resolve:resolve,reject:reject});
  setTimeout(()=>{const f=acks.get(it.id);if(f&&f.nonce===nonce){acks.delete(it.id);reject(new Error('timeout'))}},60000)});
 for(let i=0;i<k;i++){const slice=buf.slice(i*cs,(i+1)*cs);
  const ok=await SIG.sendChunk({t:'rchunk',id:it.id,nonce:nonce,i:i,k:k,meta:pmeta,d:b64u.enc(slice)});
  if(!ok){acks.delete(it.id);throw new Error('relay-down')}}
 return await done}
/* With Google Drive linked and the office folder chosen, Drive carries every original: the line sends only the
   small preview, so the photo shows on the computer at once (202, then Drive's copy replaces it). Otherwise the line
   sends the original, unless Drive is sending it (0: skipped); one this line is sending is held from Drive. */
const NOPREV=new Set();
function lineFresh(){if(typeof DRV==='object')DRV.bad=0;NOPREV.clear();
 if(typeof dreceipts==='function')setTimeout(dreceipts,3000);
 if(typeof copyPoll==='function')setTimeout(copyPoll,1000)}
async function sendItem(it){
 if(lineOpen()&&typeof driveFirst==='function'&&driveFirst(it)){
  if(it.prevSent||!it.prev||NOPREV.has(it.id))return 0;
  let st=0;try{st=await sendLine(it,it.prev,'preview')}catch(e){if(!lineOpen())throw e}
  if(st===202)return 202;NOPREV.add(it.id);return 0}   /* a refused preview never deletes the photo Drive will carry */
 if(DRV.holds(it.id))return 0;const own=lineOpen()&&!DRV.HOLD.has(it.id);
 if(own)DRV.HOLD.set(it.id,'line');try{return await sendItem1(it)}finally{if(own)DRV.HOLD.delete(it.id)}}
async function sendLine(it,blob,kind){
 const buf=new Uint8Array(await blob.arrayBuffer()),nonce=rid();
 const baseMeta=Object.assign({},it.meta);
 if((it.tu||(it.meta&&it.meta.tu))&&SKEW_OK)baseMeta.dt=Math.max(-2592000000,Math.min(2592000000,SKEW));
 const meta=kind==='preview'?Object.assign(baseMeta,{preview:true}):baseMeta;
 const h=kind==='original'?(it.h||(typeof blobSha==='function'?await blobSha(blob):'')):'';
 const done=new Promise((resolve,reject)=>{acks.set(it.id,{kind:kind,nonce:nonce,h:h,line:true,resolve:resolve,reject:reject});
  setTimeout(()=>{const f=acks.get(it.id);if(f&&f.nonce===nonce){acks.delete(it.id);reject(new Error('timeout'))}},
   kind==='preview'?60000:180000)});
 lineText(JSON.stringify({t:'photo',id:it.id,kind:kind,nonce:nonce,meta:meta,size:buf.length,type:blob.type||'image/jpeg'}));
 for(let o=0;o<buf.length;o+=16384){while(lineBuffered()>1048576){if(!lineOpen())throw new Error('closed');
   await sleep(25)}lineBin(buf.slice(o,o+16384))}
 lineText(JSON.stringify({t:'end',id:it.id}));return await done}
async function sendItem1(it){
 if(lineOpen())return await sendLine(it,it.blob,'original');
 if(RELAY&&SIG.up())return await sendRelayPreview(it);
 throw new Error('closed')}
async function alive(){if(lineOpen())return true;if(RELAY&&SIG.up())return true;connect();return false}
function offline(){if(lineOpen())return;drivePump();
 if(typeof LAD==='object'&&LAD.statusText()){say(false,'ladVia');return}
 if(DRV.on&&DRV.go()&&(typeof GDRIVE==='undefined'||!GDRIVE)){say(false,'driveMode');connect();return}
 if(RELAY&&SIG.up()){say(false,'relayMode');return}
 if(lastState==='connected'||!lastState||lastState==='driveMode')say(false,'connecting');else say(false,lastState);connect()}
async function startBridge(k){
 if(k&&k!==KEYB){KEYB=k;ROOM=KEYB.slice(0,12)}
 if(!KEYB||KEYB.length!==43||!window.isSecureContext||!window.RTCPeerConnection){
  $('go').disabled=true;status(false,!KEYB?'nokey':'insecure');
  if(!KEYB&&typeof checkIosGate==='function')checkIosGate();return false}
 if(typeof checkIosGate==='function')checkIosGate();
 await bridgeKey(KEYB);SIG.start(onSignal,n=>{if(n&&!linking&&!lineOpen()){backoff=4000;connect()}});
 if(typeof LAD==='object')LAD.init();
 return true}
(async()=>{await startBridge(KEYB);
 window.addEventListener('online',()=>{backoff=4000;connect()})})();
"""

# ---- the PC: a hidden page that answers phones and hands their photos to the photo server ----
RECV_JS = r"""
const HP=new URLSearchParams(location.hash.slice(1)),peers=new Map(),rphotos=new Map();let CFG={kinds:[],lang:'en',form:[]};
function tell(ev){fetch('bridge-event',{method:'POST',body:JSON.stringify(ev)}).catch(()=>{})}
const ports=new Set();let GUIDE_TXT='{"v":0,"items":[]}',GV=0;   /* every phone: on its own channel, or carried */
setInterval(async()=>{try{const txt=await (await fetch('guide',{cache:'no-store'})).text(),g=JSON.parse(txt);
 if(g.v!==GV){GV=g.v;GUIDE_TXT=txt;for(const p of ports)try{p.send('{"t":"guide","g":'+txt+'}')}catch(e){}
  SIG.send({t:'rguide',g:g},true)}}catch(e){}},3000);
async function onOffer(o){if(o.t!=='offer'||!o.sid||peers.has(o.sid))return;
 const pc=new RTCPeerConnection({iceServers:BR.ice});peers.set(o.sid,pc);
 const end=()=>{peers.delete(o.sid);try{pc.close()}catch(e){}};
 pc.ondatachannel=e=>serve(e.channel,end,pc);
 pc.oniceconnectionstatechange=()=>{const s=pc.iceConnectionState;if(s==='failed'){tell({type:'p2pfail'});end()}
  else if(s==='closed')end()};
 setTimeout(()=>{if(pc.iceConnectionState==='new'||pc.iceConnectionState==='checking')end()},45000);
 try{await pc.setRemoteDescription({type:'offer',sdp:o.sdp});await pc.setLocalDescription(await pc.createAnswer());
  await gathered(pc,3000);await SIG.send({t:'answer',sid:o.sid,sdp:pc.localDescription.sdp},!!o.z)}catch(e){end()}}
async function onRelay(o,kind,s,relay){
 if(o.t==='lpos'&&typeof s==='string'){
  const via=kind==='nostr'||kind==='ntfy'?'relay':(kind||'relay');
  try{const r=await fetch('lpos?via='+encodeURIComponent(via),{method:'POST',body:s});
   if(r.status===200){const ack=await r.text();
    if(ack&&!(await SIG.sendOn(relay,ack)))await SIG.sendRaw(ack,kind)}}catch(x){}return}
 if(o.t==='offer')return onOffer(o);
 if(o.t==='rhi'){tell({type:'phone',ua:String(o.ua||'').slice(0,160),relay:true});
  await SIG.send({t:'rhello',sid:String(o.sid||''),kinds:CFG.kinds,lang:CFG.lang,form:CFG.form||[],pk:CFG.pk||'',rl:CFG.rl||null,
   lad:CFG.lad?1:0,tun:CFG.tun||'',org:CFG.org||'',nl:1,now:Date.now(),eod:CFG.eod??18},true);
  if(GV)try{await SIG.send({t:'rguide',g:JSON.parse(GUIDE_TXT)},true)}catch(e){}return}
 if(o.t==='rchave'&&Array.isArray(o.ids)&&typeof o.nonce==='string'&&o.nonce.length<80){
  const ids=o.ids.filter(x=>typeof x==='string'&&x.length<80).slice(0,100);let r={};
  try{r=await (await fetch('copy-status',{method:'POST',body:JSON.stringify({ids:ids})})).json()}catch(x){}
  const items=Array.isArray(r.items)?r.items:[],rt=typeof r.rt==='number'?r.rt:Date.now();
  await SIG.send({t:'rcgot',nonce:o.nonce,rt:rt,items:items},true);return}
 if(o.t==='rpos'){const p=Object.assign({},o.p||{},{route:(kind==='own'||kind==='org')?kind:'relay'});
  fetch('pos',{method:'POST',body:JSON.stringify(p)}).catch(()=>{});return}
 if(o.t==='rchunk'&&o.id&&o.nonce&&+o.k>0&&+o.k<=128&&+o.i>=0&&+o.i<+o.k&&typeof o.d==='string'){
  const key=o.id+':'+o.nonce;let e=rphotos.get(key);
  if(!e){e={id:o.id,nonce:o.nonce,meta:o.meta||{id:o.id,sealed:1,preview:true},k:+o.k,parts:new Array(+o.k),got:0,st:null,ts:Date.now()};
   rphotos.set(key,e);if(rphotos.size>64)rphotos.delete(rphotos.keys().next().value)}
  if(e.st!=null){await SIG.send({t:'rack',id:e.id,kind:'preview',nonce:e.nonce,status:e.st},true);return}
  if(!e.parts[+o.i]){let b;try{b=b64u.dec(o.d)}catch(x){return}if(b.length>65536)return;e.parts[+o.i]=b;e.got++}
  if(e.got===e.k){const whole=cat(...e.parts);let st=400;
   try{st=(await fetch('photo?m='+encodeURIComponent(JSON.stringify(e.meta)),{method:'POST',
    body:new Blob([whole],{type:'application/octet-stream'}),headers:{'Content-Type':'application/octet-stream'}})).status}catch(x){st=503}
   e.st=st;e.parts=null;await SIG.send({t:'rack',id:e.id,kind:'preview',nonce:e.nonce,status:st},true)}}}
/* the phone's link as this PC sees it, for the team list: the time to the phone and back, and the way it goes
   (the same network, directly over the internet, through a relay - or carried by a teammate's phone) */
async function link(pc){if(!pc)return {route:'peer'};try{const s=await pc.getStats();let pair=null;
 s.forEach(r=>{if(r.type==='transport'&&r.selectedCandidatePairId)pair=s.get(r.selectedCandidatePairId)});
 if(!pair)s.forEach(r=>{if(r.type==='candidate-pair'&&r.nominated&&r.state==='succeeded')pair=r});
 if(!pair)return {};const ty=[s.get(pair.localCandidateId),s.get(pair.remoteCandidateId)].map(c=>c&&c.candidateType);
 const out={route:ty.includes('relay')?'relay':ty.every(t=>t==='host')?'lan':'direct'};
 if(pair.currentRoundTripTime!=null)out.ping=Math.round(pair.currentRoundTripTime*1000);return out}catch(e){return {}}}
/* one phone's messages, on its own channel or carried: P={send(text),pc,cur} */
async function handle(P,data){
 if(typeof data!=='string'){const cur=P.cur;if(cur){cur.parts.push(data);cur.got+=data.byteLength;
   if(cur.got>cur.size||cur.got>41943040){P.send(JSON.stringify({t:'ack',id:cur.id,status:413}));P.cur=null}}return}
 let m;try{m=JSON.parse(data)}catch(x){return}
 if(m.t==='hi')tell({type:'phone',ua:String(m.ua||'').slice(0,160)});
 else if(m.t==='lp'&&typeof m.s==='string'){const lk=await link(P.pc),route=lk.route||'direct';
  try{const r=await fetch('lpos?via='+encodeURIComponent(route),{method:'POST',body:m.s});
   if(r.status===200){const ack=await r.text();if(ack)P.send(JSON.stringify({t:'la',s:ack}))}}catch(x){}}
 else if(m.t==='pos'){const p=Object.assign({},m.p||{},await link(P.pc));
  fetch('pos',{method:'POST',body:JSON.stringify(p)}).catch(()=>{})}
 else if(m.t==='photo')P.cur={id:m.id,kind:m.kind==='preview'?'preview':'original',nonce:String(m.nonce||''),
  meta:m.meta,size:+m.size||0,type:String(m.type||'image/jpeg'),parts:[],got:0};
 else if(m.t==='chave'&&Array.isArray(m.ids)&&typeof m.nonce==='string'&&m.nonce.length<80){
  const ids=m.ids.filter(x=>typeof x==='string'&&x.length<80).slice(0,100);let r={};
  try{r=await (await fetch('copy-status',{method:'POST',body:JSON.stringify({ids:ids})})).json()}catch(x){}
  const items=Array.isArray(r.items)?r.items:[],rt=typeof r.rt==='number'?r.rt:Date.now();
  const cs=items.map(x=>x[0]+':'+x[1]+':'+(x[2]||'')).join(',');
  const g=await IDSIGN('BRCP1|'+m.nonce+'|'+rt+'|'+cs)||{};
  try{P.send(JSON.stringify({t:'cgot',nonce:m.nonce,rt:rt,items:items,ip:g.pub,is:g.sig}))}catch(x){}}
 else if(m.t==='dhave'&&Array.isArray(m.ids)&&typeof m.nonce==='string'&&m.nonce.length<80){
  /* which of these photos does this PC hold as full originals (receipts: the phone may free its Drive copies) */
  const ids=m.ids.filter(x=>typeof x==='string'&&x.length<80).slice(0,200);let r={};
  try{r=await (await fetch('drive-got',{method:'POST',body:JSON.stringify({ids:ids,for_clean:!!m.nl})})).json()}catch(x){}
  const got=(Array.isArray(r.ids)?r.ids:[]).filter(x=>ids.includes(x)),
   cl=['auto','manual','keep'].includes(r.clean)?r.clean:'keep';
  const g=await IDSIGN('BRDG1|'+m.nonce+'|'+cl+'|'+got.join(','))||{};
  try{P.send(JSON.stringify({t:'dgot',nonce:m.nonce,clean:cl,ids:got,ip:g.pub,is:g.sig}))}catch(x){}}
 else if(m.t==='end'&&P.cur&&P.cur.id===m.id){const c=P.cur;P.cur=null;let st=400,j=null;
  if(c.got===c.size){try{const res=await fetch('photo?m='+encodeURIComponent(JSON.stringify(c.meta)),{method:'POST',
    body:new Blob(c.parts,{type:c.type}),headers:{'Content-Type':c.type}});st=res.status;
    try{j=await res.json()}catch(e){}}catch(x){st=503}}
  const cnt=(j&&typeof j.c==='number')?j.c:(st===200?1:0),h=(j&&j.h)||'',rt=(j&&j.rt)||Date.now(),reason=(j&&j.reason)||'';
  const g=await IDSIGN('BRAK1|'+c.id+'|'+c.kind+'|'+c.nonce)||{};
  const g2=await IDSIGN('BRAK2|'+c.id+'|'+c.kind+'|'+c.nonce+'|'+h+'|'+cnt+'|'+rt)||{};
  try{P.send(JSON.stringify({t:'ack',id:c.id,kind:c.kind,nonce:c.nonce,status:st,c:cnt,h:h,rt:rt,reason:reason,ip:g.pub,is:g.sig,is2:g2.sig}))}catch(x){}}}
function serve(ch,end,pc){ch.binaryType='arraybuffer';const P={send:s=>ch.send(s),pc:pc,cur:null},carried=new Map();
 let nextBin=null,q=Promise.resolve();
 /* a phone this phone carries: its messages are sealed for this PC; the first, in clear, brings its public key */
 const guest=id=>{let v=carried.get(id);if(!v){v={id:id,key:null,q:Promise.resolve()};
   v.P={pc:null,cur:null,send:s=>{v.q=v.q.then(async()=>{if(!v.key)return;
     ch.send(JSON.stringify({t:'fwd',to:id,b:b64u.enc(await E2E.seal(v.key,0,enc.encode(s)))}))}).catch(()=>{})}};
   carried.set(id,v)}return v};
 const fromGuest=(v,data)=>{v.q=v.q.then(async()=>{
   if(typeof data==='string'){let m;try{m=JSON.parse(data)}catch(x){return}
    if(m.t==='hi'&&m.e&&!v.key){const kp=await E2E.pair();v.key=await E2E.key(kp.kp,m.e);ports.add(v.P);
     const g=await IDSIGN('BRH1|'+m.e+'|'+kp.pub)||{};
     ch.send(JSON.stringify({t:'fwd',to:v.id,m:JSON.stringify({t:'hello',kinds:CFG.kinds,lang:CFG.lang,
      form:CFG.form||[],pk:CFG.pk||'',rl:CFG.rl||null,lad:CFG.lad?1:0,tun:CFG.tun||'',org:CFG.org||'',nl:1,now:Date.now(),eod:CFG.eod??18,e:kp.pub,ip:g.pub,is:g.sig})}));
     tell({type:'phone',ua:String(m.ua||'').slice(0,160),via:true});if(GV)v.P.send('{"t":"guide","g":'+GUIDE_TXT+'}')}
    return}
   if(!v.key)return;const p=await E2E.open(v.key,data);
   if(p[0]===0)await handle(v.P,dec.decode(p.slice(1)));else await handle(v.P,p.slice(1).buffer)}).catch(()=>{})};
 const gone=id=>{const v=carried.get(id);if(v){ports.delete(v.P);carried.delete(id)}};
 ch.onopen=()=>{ports.add(P);ch.send(JSON.stringify({t:'hello',kinds:CFG.kinds,lang:CFG.lang,form:CFG.form||[],
  pk:CFG.pk||'',rl:CFG.rl||null,lad:CFG.lad?1:0,tun:CFG.tun||'',org:CFG.org||'',nl:1,now:Date.now(),eod:CFG.eod??18}));
  if(GV)ch.send('{"t":"guide","g":'+GUIDE_TXT+'}')};
 ch.onclose=()=>{ports.delete(P);for(const id of [...carried.keys()])gone(id);end()};
 ch.onmessage=e=>{
  if(typeof e.data!=='string'){const v=nextBin;nextBin=null;
   if(v)fromGuest(v,e.data);else{const d=e.data;q=q.then(()=>handle(P,d)).catch(()=>{})}return}
  let m=null;if(e.data.length<4096&&e.data.includes('"fwd'))try{m=JSON.parse(e.data)}catch(x){}
  if(m&&m.t==='fwd'&&m.from)fromGuest(guest(m.from),String(m.m||''));
  else if(m&&m.t==='fwdb'&&m.from)nextBin=guest(m.from);
  else if(m&&m.t==='fwdx'&&m.from)gone(m.from);
  else{const d=e.data;q=q.then(()=>handle(P,d)).catch(()=>{})}}}
class LocalRelay{
 constructor(sig){this.sig=sig;this.kind='tunnel';this.up=!!CFG.lad;this.loop()}
 async loop(){while(true){try{
   const r=await fetch('x-pull',{cache:'no-store'});
   if(r.status===404){if(this.up){this.up=false;this.sig.changed()}await sleep(30000);continue}
   if(!r.ok){if(this.up){this.up=false;this.sig.changed()}await sleep(3000);continue}
   if(!this.up){this.up=true;this.sig.changed()}
   const a=await r.json();if(Array.isArray(a))for(const m of a)if(typeof m==='string')this.sig.arrived(m,'tunnel',this)}
  catch(e){if(this.up){this.up=false;this.sig.changed()}await sleep(3000)}}}
 send(s){if(this.up&&typeof s==='string')fetch('x-push',{method:'POST',body:s}).catch(()=>{})}}
let LAST_TUN='',LAST_ORG='';
setInterval(async()=>{try{
 const c=await (await fetch('bridge-config',{cache:'no-store'})).json();
 if(c&&typeof c==='object'){CFG=c;
  if(CFG.tun&&CFG.tun!==LAST_TUN){LAST_TUN=CFG.tun;
   await SIG.send({t:'tun',h:CFG.tun},true);
   const g=await IDSIGN('BRTN1|'+CFG.tun)||{};
   const msg=JSON.stringify({t:'tun',h:CFG.tun,ip:g.pub,is:g.sig});
   for(const p of ports)try{p.send(msg)}catch(e){}}
  if(CFG.org&&CFG.org!==LAST_ORG){LAST_ORG=CFG.org;SIG.org(CFG.org);
   await SIG.send({t:'org',h:CFG.org},true);
   const g=await IDSIGN('BROR1|'+CFG.org)||{};
   const msg=JSON.stringify({t:'org',h:CFG.org,ip:g.pub,is:g.sig});
   for(const p of ports)try{p.send(msg)}catch(e){}}}}catch(e){}},10000);
(async()=>{try{CFG=await (await fetch('bridge-config',{cache:'no-store'})).json()}catch(e){}
 LAST_TUN=CFG.tun||'';LAST_ORG=CFG.org||'';
 await bridgeKey(HP.get('k')||'');
 SIG.start(onRelay,n=>tell({type:'relays',up:n,of:SIG.relays.length,kinds:SIG.kinds(),hosts:SIG.hosts()}));
 if(CFG.org)SIG.org(CFG.org);
 SIG.relays.push(new LocalRelay(SIG));if(CFG.lad)SIG.changed();
 tell({type:'started'})})();
"""


# ---- the phone page opens without internet once it was opened with it ---------------------------------------
SW_JS = r"""/* Biruni phone page: kept on the phone, so it opens in the field without internet (the network first, this
   copy when there is none). */
const C='biruni-p-1';
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET'||new URL(r.url).origin!==location.origin)return;
 e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(C).then(x=>x.put(r,c))}return res})
  .catch(()=>caches.match(r,{ignoreSearch:true})))});
"""

SW_GONE_JS = r"""/* Old service worker left behind on biruni-gis.com/p/: drops its cache and unregisters itself. */
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(
 caches.keys().then(ks=>Promise.all(ks.filter(k=>k.startsWith('biruni-p-')).map(k=>caches.delete(k))))
  .then(()=>self.registration.unregister())
));
"""


def moved_page(target):
    """Tiny forwarding HTML page left at an old URL; preserves query and #fragment."""
    t = json.dumps(target).replace("</", "<\\/")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex">'
        '<meta name="referrer" content="no-referrer">'
        "<title>Biruni GIS</title>"
        f"<script>location.replace({t}+location.search+location.hash)</script>"
        "</head><body></body></html>"
    )


def _core():
    relays = list(RELAYS)
    mqtt = list(MQTT)
    rset, mset = set(relays), set(mqtt)
    leg_relays = [u for u in LEGACY_RELAYS if u in rset] or relays
    leg_mqtt = [u for u in LEGACY_MQTT if u in mset] or mqtt
    return (CORE_JS.replace("__RELAYS__", json.dumps(relays))
            .replace("__LEGACY_RELAYS__", json.dumps(leg_relays))
            .replace("__NTFY__", NTFY)
            .replace("__MQTT__", json.dumps(mqtt))
            .replace("__LEGACY_MQTT__", json.dumps(leg_mqtt))
            .replace("__POOL_K__", json.dumps(relay_pool.K))
            .replace("__TUN__", TUNNEL)
            .replace("__OWN__", OWN)
            .replace("__ORG__", ORG))


def receiver_page():
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Biruni bridge</title></head><body><script>"
            + _core() + RECV_JS + "</script></body></html>").encode("utf-8")


def phone_page(all_texts):
    """The page published at PAGE_URL: the photo page of server.py with every language
    in it, sending over the bridge."""
    from .server import PAGE

    head, rest = PAGE.split("/*SEND{*/", 1)
    _, tail = rest.split("/*}SEND*/", 1)
    from . import drive, drive_page, ladder_page

    page = (head + _core() + drive.BFP_JS + drive_page.drive_js()
            + ladder_page.DR_JS + ladder_page.LADDER_JS + PHONE_JS + tail)
    page = page.replace("/*BOOT*/", BOOT_JS)
    return (page.replace("__LANG__", "en").replace("__DEFLANG__", "").replace("__DIR__", "ltr")
            .replace("__TEXTS__", json.dumps(all_texts, ensure_ascii=False, sort_keys=True).replace("</", "<\\/"))
            .replace("__KINDS__", "[]").replace("__FORM__", "[]").replace("__PCKEY__", ""))


def _origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def connect_sources(tunnel=False, org=False):
    """Unique connect-src origins for public bridge pages."""
    sources = [_origin(u) for u in RELAYS if u]
    if NTFY:
        nt = _origin(NTFY)
        sources.extend([nt, re.sub(r"^http", "ws", nt)])
    sources.extend(_origin(u) for u in MQTT if u)
    if OWN:
        sources.append(_origin(OWN))
    if tunnel and TUNNEL:
        sources.append(_origin(TUNNEL.replace("{h}", "*")))
    if org and ORG:
        sources.append(_origin(ORG.replace("{h}", "*.workers.dev")))
    return list(dict.fromkeys(sources))


def with_csp(page, connect=(), frames=()):
    """The phone page with a Content-Security-Policy in a <meta>: only its own inline script and style run (by their
    SHA-256), and it talks only to the relays and ntfy (plus `connect`, and may frame only `frames`). The key is in
    the page's memory, so nothing else - an injected script, a stray site - may load or receive anything."""
    def sha(tag):
        body = page.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"

    assert page.count("<script>") == 1 and page.count("<style>") == 1 and "<script " not in page, "one inline script"
    conn = list(dict.fromkeys([*connect_sources(True, org=True), *map(_origin, connect)]))
    frame = " ".join(map(_origin, frames)) or "'none'"
    csp = "; ".join([
        "default-src 'none'", f"script-src {sha('script')}", f"style-src {sha('style')}",
        "img-src 'self' blob:", "media-src blob:", f"connect-src 'self' {' '.join(conn)}", "worker-src 'self'",
        "manifest-src 'self'", f"frame-src {frame}",
        "base-uri 'none'", "form-action 'none'"])
    meta = f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
    return page.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + meta, 1)


# ---- the hidden receiver process ---------------------------------------------------------
def python_exe():
    for p in (os.path.join(sys.base_prefix, "python.exe"), os.path.join(sys.exec_prefix, "python.exe"),
              sys.executable):
        if p and os.path.isfile(p) and os.path.basename(p).lower().startswith("python"):
            return p
    return None


SANDBOX_DLLS = ("brotlicommon.dll", "brotlidec.dll", "freetype.dll", "libpng16.dll", "zlib.dll", "zstd.dll")


def sandbox_ready(exe):
    """True when Chromium's sandbox can start: the libraries QtWebEngineProcess.exe needs lie beside it
    (scripts/patch_engine.py puts them there; a sandboxed process does not search PATH)."""
    try:
        here = os.path.join(os.path.dirname(os.path.abspath(exe)), "..", "qt6", "bin")
        return (os.path.isfile(os.path.join(here, "QtWebEngineProcess.exe"))
                and all(os.path.isfile(os.path.join(here, n)) for n in SANDBOX_DLLS))
    except Exception:  # noqa: BLE001 - unknown layout: the old way
        return False


class BridgeHost:
    """Runs bridge_host.py (hidden Chromium) on the receiver page. Events: ("ended", code)."""

    def __init__(self, url):
        from .tunnel import NO_WINDOW, _Job
        from . import tunnel

        exe = python_exe()
        if exe is None:
            raise OSError("python.exe not found")
        # Real local addresses (not mDNS names) let a phone on the same Wi-Fi link up directly.
        flags = "--disable-gpu --disable-features=WebRtcHideLocalIpsWithMdns"
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS=flags, BIRUNI_BRIDGE_URL=url)
        env.pop("QTWEBENGINE_DISABLE_SANDBOX", None)
        self.sandboxed = sandbox_ready(exe)
        if not self.sandboxed:
            # an engine installed before scripts/patch_engine.py placed the sandbox's libraries: without them the
            # sandboxed page dies at start, so it runs as before (our own page only, no other site is opened)
            env["QTWEBENGINE_CHROMIUM_FLAGS"] = flags + " --no-sandbox"
            env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_host.py")
        self.events = queue.Queue()
        self.proc = subprocess.Popen([exe, "-I", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW, env=env)
        try:
            if tunnel._JOB is None:
                tunnel._JOB = _Job()
            tunnel._JOB.add(self.proc)
        except Exception:  # noqa: BLE001 - a safety net; stop() still ends it
            pass
        self.log = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in iter(self.proc.stderr.readline, b""):
            self.log = (self.log + [raw.decode("utf-8", "replace").rstrip()])[-40:]
        self.events.put(("ended", self.proc.wait()))

    def running(self):
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
