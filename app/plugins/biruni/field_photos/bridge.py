"""Biruni bridge: the phone and this PC find each other through public relays, then talk
directly (WebRTC) - no tunnel, no program to download, no account, no server of ours.

How it works
  * Biruni keeps a random 32-byte key for this Windows user. The QR code holds the phone
    page's address on GitHub Pages with the key after "#" - the part of an address a
    browser never sends to any server.
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

PAGE_URL = "https://biruni-gis.com/p/"
RELAYS = ("wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net", "wss://nostr.mom",
          "wss://relay.nostr.band")
NTFY = "https://ntfy.sh/"
if os.environ.get("BIRUNI_BRIDGE_RELAYS"):  # tests: a relay on this PC
    RELAYS = tuple(os.environ["BIRUNI_BRIDGE_RELAYS"].split())
    NTFY = os.environ.get("BIRUNI_BRIDGE_NTFY", NTFY)
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


def identity(key):
    """This PC's identity for one bridge key: (private key as PKCS#8 b64u, pin). Kept (DPAPI) as long as the key, so
    a QR code that stays the same (Field Photos, Phone GPS, printed live papers) keeps its pin; a new key gets a new
    identity. The newest ID_KEEP are kept."""
    import hashlib
    import json as _json

    h = hashlib.sha256(("biruni-bridge-id:" + key).encode("ascii")).hexdigest()[:24]
    if h in _IDS:
        return _IDS[h]
    sk = None
    try:
        from qgis.core import QgsSettings

        from .tunnel import protect, unprotect

        st = QgsSettings()
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
    _IDS[h] = (sk, id_pin(sk))
    return _IDS[h]


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


def phone_url(key, lang="en", drive_pk=None):
    """The phone page's address (everything after # stays on the phone). drive_pk: this computer's handover public
    key when the project takes photos through Google Drive - the page then offers Drive and seals each photo for it
    (&d=1&pk=, see drive.py) even before it has ever reached this computer."""
    url = f"{PAGE_URL}#k={key}&i={pin(key)}&l={lang}"
    return url + f"&d=1&pk={drive_pk}" if drive_pk else url


# ---- the shared script: encryption, Nostr signatures, relays ------------------------------
CORE_JS = r"""
/* ---- Biruni bridge core: shared by the phone page and the PC's hidden receiver ---- */
const BR={relays:__RELAYS__,ntfy:'__NTFY__',kind:25050,
 ice:[{urls:['stun:stun.l.google.com:19302','stun:stun.cloudflare.com:3478']}]};
const enc=new TextEncoder(),dec=new TextDecoder();
const b64u={enc:b=>{let s='';const a=new Uint8Array(b);for(let i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);
  return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')},
 dec:s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0))};
const hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
const unhex=h=>Uint8Array.from(h.match(/../g),x=>parseInt(x,16));
const cat=(...a)=>{const o=new Uint8Array(a.reduce((n,x)=>n+x.length,0));let i=0;for(const x of a){o.set(x,i);i+=x.length}return o};
const sha=async b=>new Uint8Array(await crypto.subtle.digest('SHA-256',b));
const rid=()=>b64u.enc(crypto.getRandomValues(new Uint8Array(9)));
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
 const ev={pubkey:NID.pk,created_at:Math.floor(Date.now()/1000),kind:BR.kind,tags:[['t',topic]],content:content};
 const id=await sha(enc.encode(JSON.stringify([0,ev.pubkey,ev.created_at,ev.kind,ev.tags,ev.content])));
 ev.id=hex(id);ev.sig=hex(await schnorr(NID.sk,id,crypto.getRandomValues(new Uint8Array(32))));return ev}
/* the key: AES-GCM for every message, and the meeting name the relays see */
let BK=null;
async function bridgeKey(k){const raw=b64u.dec(k);
 BK={k:await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt','decrypt']),
  topic:'biruni'+hex(await sha(cat(enc.encode('biruni-bridge-1:'),raw))).slice(0,40)};await idInit(k);return BK}
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
/* the relays: control messages go to all; preview chunks go to at most 2 healthy Nostr relays with rate limiting */
class NostrRelay{constructor(url,sig){this.url=url;this.sig=sig;this.up=false;this.wait=3000;this.fails=0;
  this.backoffUntil=0;this.oks=new Map();this.notices=[];this.open()}
 open(){let ws;try{ws=new WebSocket(this.url)}catch(e){return this.later()}this.ws=ws;
  ws.onopen=()=>{this.up=true;this.wait=3000;ws.send(JSON.stringify(['REQ','b'+rid(),
   {kinds:[BR.kind],'#t':[BK.topic],since:Math.floor(Date.now()/1000)-60}]));this.sig.changed()};
  ws.onmessage=e=>{try{const m=JSON.parse(e.data);
   if(m[0]==='EVENT'&&m[2]&&typeof m[2].content==='string')this.sig.arrived(m[2].content);
   else if(m[0]==='OK'&&typeof m[1]==='string'){const ok=!!m[2];
    if(!ok){this.fails++;this.backoffUntil=Date.now()+Math.min(60000,4000*Math.pow(2,this.fails-1))}
    else{this.fails=0;this.backoffUntil=0}
    const cb=this.oks.get(m[1]);if(cb){this.oks.delete(m[1]);cb(ok)}}
   else if(m[0]==='NOTICE'){const w=String(m[1]||'');this.notices.push(w);
    if(/rate|limit|slow|quota|ban|block|too/i.test(w)){this.fails++;this.backoffUntil=Date.now()+15000}}}catch(x){}};
  ws.onclose=()=>{const was=this.up;this.up=false;for(const [,cb] of this.oks)cb(false);this.oks.clear();
   if(was)this.sig.changed();this.later()};ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 send(s,ev){if(this.up&&ev&&Date.now()>=this.backoffUntil)try{this.ws.send(JSON.stringify(['EVENT',ev]))}catch(e){}}
 sendWait(ev,ms=3500){if(!this.up||!ev||Date.now()<this.backoffUntil)return Promise.resolve(false);
  return new Promise(r=>{const t=setTimeout(()=>{if(this.oks.delete(ev.id))r(false)},ms);
   this.oks.set(ev.id,ok=>{clearTimeout(t);r(ok)});
   try{this.ws.send(JSON.stringify(['EVENT',ev]))}catch(e){clearTimeout(t);this.oks.delete(ev.id);r(false)}})}}
class NtfyRelay{constructor(sig){this.sig=sig;this.up=false;this.wait=3000;this.open()}
 open(){let ws;try{ws=new WebSocket(BR.ntfy.replace(/^http/,'ws')+BK.topic+'/ws')}catch(e){return this.later()}this.ws=ws;
  ws.onopen=()=>{this.up=true;this.wait=3000;this.sig.changed()};
  ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.event==='message'){
   if(m.attachment&&m.attachment.url)fetch(m.attachment.url).then(r=>r.ok?r.text():'').then(t=>{if(t)this.sig.arrived(t)}).catch(()=>{});
   else if(m.message)this.sig.arrived(m.message)}}catch(x){}};
  ws.onclose=()=>{const was=this.up;this.up=false;if(was)this.sig.changed();this.later()};ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 send(s){if(s.length<=4000)fetch(BR.ntfy+BK.topic+'?cache=no&firebase=no',{method:'POST',body:s}).catch(()=>{})}
 async sendChunk(s){try{const u=BR.ntfy+BK.topic+(s.length<=4000?'?cache=no&firebase=no':'?firebase=no'),
   h=s.length>4000?{'Filename':'p.bin'}:{};return (await fetch(u,{method:'POST',body:s,headers:h})).ok}catch(e){return false}}}
const SIG={relays:[],seen:new Set(),loose:new Set(),on:null,onchange:null,_lastChunk:0,
 start(onMsg,onChange){this.on=onMsg;this.onchange=onChange;
  this.relays=BR.relays.map(u=>new NostrRelay(u,this)).concat([new NtfyRelay(this)])},
 up(){return this.relays.filter(r=>r.up).length},
 healthyNostr(){return this.relays.filter(r=>r instanceof NostrRelay&&r.up&&Date.now()>=r.backoffUntil).sort((a,b)=>a.fails-b.fails)},
 changed(){if(this.onchange)this.onchange(this.up())},
 async _sign(obj){
  if(obj.t==='answer'&&ID.sk){const g=await IDSIGN('BRA1|'+obj.sid+'|'+obj.sdp);obj.ip=g.pub;obj.is=g.sig}
  else if(obj.t==='rhello'&&ID.sk){const g=await IDSIGN('BRH0|'+obj.sid+'|'+(obj.pk||''));obj.ip=g.pub;obj.is=g.sig}
  else if(obj.t==='rack'&&ID.sk){const g=await IDSIGN('BRAK1|'+obj.id+'|'+obj.kind+'|'+obj.nonce);obj.ip=g.pub;obj.is=g.sig}},
 async send(obj,z){obj.n=rid();obj.ts=Date.now();this.seen.add(obj.n);await this._sign(obj);
  const s=await seal(obj,z);let ev=null;
  try{ev=await nostrEvent(s,BK.topic)}catch(e){}for(const r of this.relays)r.send(s,ev)},
 async sendChunk(obj){
  const pace=BR.paceMs!=null?BR.paceMs:180,wait=Math.max(0,this._lastChunk+pace-Date.now());
  if(wait)await sleep(wait);this._lastChunk=Date.now();
  obj.n=rid();obj.ts=Date.now();this.seen.add(obj.n);const s=await seal(obj,true);
  let ev=null;try{ev=await nostrEvent(s,BK.topic)}catch(e){}
  const tried=new Set();
  while(ev){const pair=this.healthyNostr().filter(r=>!tried.has(r)).slice(0,2);if(!pair.length)break;
   pair.forEach(r=>tried.add(r));const res=await Promise.all(pair.map(r=>r.sendWait(ev)));
   if(res.some(Boolean))return true}
  const nr=this.relays.find(r=>r instanceof NtfyRelay);return nr?await nr.sendChunk(s):false},
 async arrived(s){const o=await unseal(s);if(!o||!o.n||this.seen.has(o.n))return;this.seen.add(o.n);
  if(Math.abs(Date.now()-(o.ts||0))>15*60000)return;
  /* answers and receipts to this phone must come from the PC (signed) */
  if(o.t==='answer'&&ID.pin&&!this.loose.has(o.sid)&&!await IDOK(o.ip,o.is,'BRA1|'+o.sid+'|'+o.sdp)){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='rhello'&&ID.pin&&!await IDOK(o.ip,o.is,'BRH0|'+o.sid+'|'+(o.pk||''))){
   this.refused=(this.refused||0)+1;return}
  if(o.t==='rack'&&ID.pin&&!await IDOK(o.ip,o.is,'BRAK1|'+o.id+'|'+o.kind+'|'+o.nonce)){
   this.refused=(this.refused||0)+1;return}
  if(this.on)this.on(o)}};
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
const HP=new URLSearchParams(location.hash.slice(1)),KEYB=HP.get('k')||'';HASHL=HP.get('l');ROOM=KEYB.slice(0,12);
GPSMODE=HP.get('m')==='gps';const VIAONLY=HP.get('v')==='1';
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
async function sendPos(p){if(lineOpen())lineText(JSON.stringify({t:'pos',p:p}));
 else if(RELAY&&SIG.up())SIG.send({t:'rpos',p:p},true)}
function pollGuide(){}
function hello(m){if(Array.isArray(m.kinds))KINDS=m.kinds.map(String).slice(0,24);drawKinds();
 if(typeof m.pk==='string'&&m.pk.length>80&&m.pk.length<100){PCKEY=m.pk;keep('biruni-pckey-'+ROOM,m.pk);sealWaiting()}
 if(Array.isArray(m.form)){FORM=m.form.filter(f=>f&&f.name&&f.label).slice(0,30);drawForm()}
 if(!kept('biruni-lang')&&!HASHL&&pick(m.lang))setLang(pick(m.lang));keep('biruni-hello-'+ROOM,JSON.stringify({kinds:KINDS,form:FORM}))}
async function handleAck(m){const f=acks.get(m.id);if(!f)return;
 if(m.status===200){const k=m.kind||'original',n=m.nonce||'';
  if(k!==f.kind||n!==f.nonce||!await IDOK(m.ip,m.is,'BRAK1|'+m.id+'|'+k+'|'+n)){
   SIG.refused=(SIG.refused||0)+1;return}}
 acks.delete(m.id);f.resolve(m.status===200&&f.kind==='preview'?202:m.status)}
/* what the PC says, however it comes */
function fromPc(m){if(m.t==='hello')hello(m);else if(m.t==='guide')setGuide(m.g);
 else if(m.t==='ack')handleAck(m)}
function failAcks(){for(const [id,f] of acks)if(f.kind==='original'||f.line){acks.delete(id);f.reject(new Error('closed'))}}
function drop(){const p=pc;pc=null;dc=null;linking=false;if(p)try{p.close()}catch(e){}
 for(const [k,c] of carried){try{if(c)c.close()}catch(e){}}carried.clear();if(!lineOpen())failAcks()}
function dropVia(){const v=VIA;VIA=null;if(v&&v.pc)try{v.pc.close()}catch(e){}if(!lineOpen())failAcks()}
function later(){clearTimeout(retry);retry=setTimeout(connect,backoff);backoff=Math.min(backoff*2,60000)}
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
async function onSignal(o){
 if(o.t==='answer'&&o.sid===sid&&pc&&pc.signalingState==='have-local-offer'){
  try{await pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){drop();later()}}
 else if(o.t==='answer'&&VIA&&o.sid===VIA.sid&&VIA.pc&&VIA.pc.signalingState==='have-local-offer'){
  try{await VIA.pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){dropVia()}}   /* the first teammate to answer */
 else if(o.t==='help')carry(o);
 else if(o.t==='rhello'&&(!o.sid||o.sid===rsid)){hello(o);if(!lineOpen()){RELAY=true;say(false,'relayMode');flush();tellPos(true)}}
 else if(o.t==='rguide')setGuide(o.g);
 else if(o.t==='rack')handleAck(o)}
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
function lineFresh(){if(typeof DRV==='object')DRV.bad=0;NOPREV.clear()}
async function sendItem(it){
 if(lineOpen()&&typeof driveFirst==='function'&&driveFirst(it)){
  if(it.prevSent||!it.prev||NOPREV.has(it.id))return 0;
  let st=0;try{st=await sendLine(it,it.prev,'preview')}catch(e){if(!lineOpen())throw e}
  if(st===202)return 202;NOPREV.add(it.id);return 0}   /* a refused preview never deletes the photo Drive will carry */
 if(DRV.holds(it.id))return 0;const own=lineOpen()&&!DRV.HOLD.has(it.id);
 if(own)DRV.HOLD.set(it.id,'line');try{return await sendItem1(it)}finally{if(own)DRV.HOLD.delete(it.id)}}
async function sendLine(it,blob,kind){
 const buf=new Uint8Array(await blob.arrayBuffer()),nonce=rid(),meta=kind==='preview'?Object.assign({},it.meta,{preview:true}):it.meta;
 const done=new Promise((resolve,reject)=>{acks.set(it.id,{kind:kind,nonce:nonce,line:true,resolve:resolve,reject:reject});
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
 if(DRV.on&&DRV.go()){say(false,'driveMode');connect();return}
 if(RELAY&&SIG.up()){say(false,'relayMode');return}
 if(lastState==='connected'||!lastState||lastState==='driveMode')say(false,'connecting');else say(false,lastState);connect()}
(async()=>{if(!KEYB||KEYB.length!==43||!window.isSecureContext||!window.RTCPeerConnection){
  $('go').disabled=true;status(false,!KEYB?'nokey':'insecure');return}
 await bridgeKey(KEYB);SIG.start(onSignal,n=>{if(n&&!linking&&!lineOpen()){backoff=4000;connect()}});
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
async function onRelay(o){
 if(o.t==='offer')return onOffer(o);
 if(o.t==='rhi'){tell({type:'phone',ua:String(o.ua||'').slice(0,160),relay:true});
  await SIG.send({t:'rhello',sid:String(o.sid||''),kinds:CFG.kinds,lang:CFG.lang,form:CFG.form||[],pk:CFG.pk||''},true);
  if(GV)try{await SIG.send({t:'rguide',g:JSON.parse(GUIDE_TXT)},true)}catch(e){}return}
 if(o.t==='rpos'){const p=Object.assign({},o.p||{},{route:'relay'});
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
 else if(m.t==='pos'){const p=Object.assign({},m.p||{},await link(P.pc));
  fetch('pos',{method:'POST',body:JSON.stringify(p)}).catch(()=>{})}
 else if(m.t==='photo')P.cur={id:m.id,kind:m.kind==='preview'?'preview':'original',nonce:String(m.nonce||''),
  meta:m.meta,size:+m.size||0,type:String(m.type||'image/jpeg'),parts:[],got:0};
 else if(m.t==='end'&&P.cur&&P.cur.id===m.id){const c=P.cur;P.cur=null;let st=400;
  if(c.got===c.size){try{st=(await fetch('photo?m='+encodeURIComponent(JSON.stringify(c.meta)),{method:'POST',
    body:new Blob(c.parts,{type:c.type}),headers:{'Content-Type':c.type}})).status}catch(x){st=503}}
  const g=await IDSIGN('BRAK1|'+c.id+'|'+c.kind+'|'+c.nonce)||{};
  try{P.send(JSON.stringify({t:'ack',id:c.id,kind:c.kind,nonce:c.nonce,status:st,ip:g.pub,is:g.sig}))}catch(x){}}}
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
     ch.send(JSON.stringify({t:'fwd',to:v.id,m:JSON.stringify({t:'hello',kinds:CFG.kinds,lang:CFG.lang,form:CFG.form||[],pk:CFG.pk||'',e:kp.pub,ip:g.pub,is:g.sig})}));
     tell({type:'phone',ua:String(m.ua||'').slice(0,160),via:true});if(GV)v.P.send('{"t":"guide","g":'+GUIDE_TXT+'}')}
    return}
   if(!v.key)return;const p=await E2E.open(v.key,data);
   if(p[0]===0)await handle(v.P,dec.decode(p.slice(1)));else await handle(v.P,p.slice(1).buffer)}).catch(()=>{})};
 const gone=id=>{const v=carried.get(id);if(v){ports.delete(v.P);carried.delete(id)}};
 ch.onopen=()=>{ports.add(P);ch.send(JSON.stringify({t:'hello',kinds:CFG.kinds,lang:CFG.lang,form:CFG.form||[],pk:CFG.pk||''}));
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
(async()=>{try{CFG=await (await fetch('bridge-config',{cache:'no-store'})).json()}catch(e){}
 await bridgeKey(HP.get('k')||'');SIG.start(onRelay,n=>tell({type:'relays',up:n,of:SIG.relays.length}));
 tell({type:'started'})})();
"""


# ---- the phone page opens without internet once it was opened with it (GitHub Pages) -------------------------
SW_JS = r"""/* Biruni phone page: kept on the phone, so it opens in the field without internet (the network first, this
   copy when there is none). */
const C='biruni-p-1';
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET'||new URL(r.url).origin!==location.origin)return;
 e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(C).then(x=>x.put(r,c))}return res})
  .catch(()=>caches.match(r,{ignoreSearch:true})))});
"""


def _core():
    return CORE_JS.replace("__RELAYS__", json.dumps(list(RELAYS))).replace("__NTFY__", NTFY)


def receiver_page():
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Biruni bridge</title></head><body><script>"
            + _core() + RECV_JS + "</script></body></html>").encode("utf-8")


def phone_page(all_texts):
    """The page published on GitHub Pages: the photo page of server.py with every language
    in it, sending over the bridge."""
    from .server import PAGE

    head, rest = PAGE.split("/*SEND{*/", 1)
    _, tail = rest.split("/*}SEND*/", 1)
    from . import drive, drive_page

    page = head + _core() + drive.BFP_JS + drive_page.drive_js() + PHONE_JS + tail
    page = page.replace("/*BOOT*/", BOOT_JS)
    return (page.replace("__LANG__", "en").replace("__DEFLANG__", "").replace("__DIR__", "ltr")
            .replace("__TEXTS__", json.dumps(all_texts, ensure_ascii=False, sort_keys=True).replace("</", "<\\/"))
            .replace("__KINDS__", "[]").replace("__FORM__", "[]").replace("__PCKEY__", ""))


def _origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def with_csp(page, connect=(), frames=()):
    """The phone page with a Content-Security-Policy in a <meta> (GitHub Pages sends no headers): only its own
    inline script and style run (by their SHA-256), and it talks only to the relays and ntfy (plus `connect`, and may
    frame only `frames`). The key is in the page's memory, so nothing else - an injected script, a stray site - may
    load or receive anything."""
    def sha(tag):
        body = page.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"

    assert page.count("<script>") == 1 and page.count("<style>") == 1 and "<script " not in page, "one inline script"
    nt = _origin(NTFY)
    conn = list(dict.fromkeys([*RELAYS, nt, re.sub(r"^http", "ws", nt), *map(_origin, connect)]))
    frame = " ".join(map(_origin, frames)) or "'none'"
    csp = "; ".join([
        "default-src 'none'", f"script-src {sha('script')}", f"style-src {sha('style')}",
        "img-src 'self' blob:", "media-src blob:", f"connect-src {' '.join(conn)}", "worker-src 'self'",
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
