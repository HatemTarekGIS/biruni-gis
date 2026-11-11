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
import json
import os
import queue
import secrets
import subprocess
import sys
import threading

PAGE_URL = "https://hatemtarekgis.github.io/biruni-gis/p/"
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


def phone_url(key, lang="en"):
    return f"{PAGE_URL}#k={key}&i={pin(key)}&l={lang}"


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
/* the relays: every message goes to all of them; the first copy to arrive counts */
class NostrRelay{constructor(url,sig){this.url=url;this.sig=sig;this.up=false;this.wait=3000;this.open()}
 open(){let ws;try{ws=new WebSocket(this.url)}catch(e){return this.later()}this.ws=ws;
  ws.onopen=()=>{this.up=true;this.wait=3000;ws.send(JSON.stringify(['REQ','b'+rid(),
   {kinds:[BR.kind],'#t':[BK.topic],since:Math.floor(Date.now()/1000)-60}]));this.sig.changed()};
  ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m[0]==='EVENT'&&m[2]&&typeof m[2].content==='string')
   this.sig.arrived(m[2].content)}catch(x){}};
  ws.onclose=()=>{const was=this.up;this.up=false;if(was)this.sig.changed();this.later()};ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 send(s,ev){if(this.up&&ev)try{this.ws.send(JSON.stringify(['EVENT',ev]))}catch(e){}}}
class NtfyRelay{constructor(sig){this.sig=sig;this.up=false;this.wait=3000;this.open()}
 open(){let ws;try{ws=new WebSocket(BR.ntfy.replace(/^http/,'ws')+BK.topic+'/ws')}catch(e){return this.later()}this.ws=ws;
  ws.onopen=()=>{this.up=true;this.wait=3000;this.sig.changed()};
  ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.event==='message'&&m.message)this.sig.arrived(m.message)}catch(x){}};
  ws.onclose=()=>{const was=this.up;this.up=false;if(was)this.sig.changed();this.later()};ws.onerror=()=>{try{ws.close()}catch(x){}}}
 later(){setTimeout(()=>this.open(),this.wait+Math.random()*1000);this.wait=Math.min(this.wait*2,60000)}
 send(s){if(s.length<=4000)fetch(BR.ntfy+BK.topic+'?cache=no&firebase=no',{method:'POST',body:s}).catch(()=>{})}}
const SIG={relays:[],seen:new Set(),loose:new Set(),on:null,onchange:null,
 start(onMsg,onChange){this.on=onMsg;this.onchange=onChange;
  this.relays=BR.relays.map(u=>new NostrRelay(u,this)).concat([new NtfyRelay(this)])},
 up(){return this.relays.filter(r=>r.up).length},
 changed(){if(this.onchange)this.onchange(this.up())},
 async send(obj,z){obj.n=rid();obj.ts=Date.now();this.seen.add(obj.n);
  if(obj.t==='answer'&&ID.sk){const g=await IDSIGN('BRA1|'+obj.sid+'|'+obj.sdp);obj.ip=g.pub;obj.is=g.sig}   /* the PC signs */
  const s=await seal(obj,z);let ev=null;
  try{ev=await nostrEvent(s,BK.topic)}catch(e){}for(const r of this.relays)r.send(s,ev)},
 async arrived(s){const o=await unseal(s);if(!o||!o.n||this.seen.has(o.n))return;this.seen.add(o.n);
  if(Math.abs(Date.now()-(o.ts||0))>15*60000)return;
  /* an answer to this phone must come from the PC (signed) - unless it answers a call for a teammate (loose) */
  if(o.t==='answer'&&ID.pin&&!this.loose.has(o.sid)&&!await IDOK(o.ip,o.is,'BRA1|'+o.sid+'|'+o.sdp)){
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
if('serviceWorker' in navigator&&location.protocol==='https:'&&/\.github\.io$/.test(location.hostname))
 navigator.serviceWorker.register('sw.js').catch(()=>{});   /* opens again in the field without internet *//* v=1: never direct, always through a teammate (to try it) */
try{const c=JSON.parse(kept('biruni-hello-'+ROOM)||'null');if(c&&Array.isArray(c.kinds))KINDS=c.kinds;
 if(c&&Array.isArray(c.form))FORM=c.form}catch(e){}
"""

PHONE_JS = r"""
/* the phone sends over the bridge: relays to meet, then a direct channel to the PC - or, where the network
   allows none, a channel to a teammate's phone that carries the messages to the PC */
let pc=null,dc=null,sid=null,linking=false,retry=null,backoff=4000,lastState='',acks=new Map();
let VIA=null;                 /* the line through a teammate: {pc,ch,sid,kp,key,ready,q,rq,pend} */
const carried=new Map();      /* teammates this phone carries to the PC: their request's id -> their channel */
function say(on,key){lastState=key;status(on,key)}
const direct=()=>!!(dc&&dc.readyState==='open');
const lineOpen=()=>direct()||!!(VIA&&VIA.ready);
function lineText(s){if(direct())return dc.send(s);const v=VIA;if(!v||!v.ready)throw new Error('closed');
 v.q=v.q.then(async()=>v.ch.send(await E2E.seal(v.key,0,enc.encode(s)))).catch(()=>{})}
function lineBin(u8){if(direct())return dc.send(u8);const v=VIA;if(!v||!v.ready)throw new Error('closed');
 v.pend+=u8.length;v.q=v.q.then(async()=>{v.ch.send(await E2E.seal(v.key,1,u8));v.pend-=u8.length}).catch(()=>{})}
const lineBuffered=()=>direct()?dc.bufferedAmount:VIA?VIA.ch.bufferedAmount+VIA.pend:0;
async function sendPos(p){if(lineOpen())lineText(JSON.stringify({t:'pos',p:p}))}
function pollGuide(){}
function hello(m){if(Array.isArray(m.kinds))KINDS=m.kinds.map(String).slice(0,24);drawKinds();
 if(typeof m.pk==='string'&&m.pk.length>80&&m.pk.length<100){PCKEY=m.pk;keep('biruni-pckey-'+ROOM,m.pk)}
 if(Array.isArray(m.form)){FORM=m.form.filter(f=>f&&f.name&&f.label).slice(0,30);drawForm()}
 if(!kept('biruni-lang')&&!HASHL&&pick(m.lang))setLang(pick(m.lang));keep('biruni-hello-'+ROOM,JSON.stringify({kinds:KINDS,form:FORM}))}
/* what the PC says, however it comes */
function fromPc(m){if(m.t==='hello')hello(m);else if(m.t==='guide')setGuide(m.g);
 else if(m.t==='ack'){const f=acks.get(m.id);if(f){acks.delete(m.id);f.resolve(m.status)}}}
function failAcks(){for(const [id,f] of acks){f.reject(new Error('closed'))}acks.clear()}
function drop(){const p=pc;pc=null;dc=null;linking=false;if(p)try{p.close()}catch(e){}
 for(const [k,c] of carried){try{if(c)c.close()}catch(e){}}carried.clear();if(!lineOpen())failAcks()}
function dropVia(){const v=VIA;VIA=null;if(v&&v.pc)try{v.pc.close()}catch(e){}if(!lineOpen())failAcks()}
function later(){clearTimeout(retry);retry=setTimeout(connect,backoff);backoff=Math.min(backoff*2,60000)}
async function connect(){if(!BK||linking||lineOpen())return;
 if(!SIG.up()){say(false,'connecting');return later()}
 if(VIAONLY){say(false,'connecting');help();return later()}
 linking=true;say(false,'connecting');const my=sid=rid();
 try{pc=new RTCPeerConnection({iceServers:BR.ice});const ch=dc=pc.createDataChannel('photos',{ordered:true});
  ch.binaryType='arraybuffer';const me=pc;
  ch.onopen=()=>{linking=false;backoff=4000;dropVia();ch.send(JSON.stringify({t:'hi',ua:dev}));say(true,'connected');flush();
   tellPos(true)};
  ch.onclose=()=>{if(pc===me){drop();say(false,'lost');later()}};
  ch.onmessage=e=>{if(typeof e.data!=='string')return;let m;try{m=JSON.parse(e.data)}catch(x){return}
   if(m.t==='fwd'&&m.to){const c=carried.get(m.to);   /* the PC answers a teammate this phone carries */
    if(c&&c.readyState==='open')try{c.send(m.b?b64u.dec(m.b):m.m)}catch(x){}}
   else fromPc(m)};
  pc.oniceconnectionstatechange=()=>{if(pc!==me)return;const s=me.iceConnectionState;
   if(s==='failed'){drop();say(false,'p2pfail');help();later()}};
  await me.setLocalDescription(await me.createOffer());await gathered(me,3000);
  if(pc!==me)return;
  await SIG.send({t:'offer',sid:my,sdp:me.localDescription.sdp,z:canZ},true);
  await sleep(20000);
  if(pc===me&&linking&&me.signalingState==='have-local-offer'){drop();say(false,'pcoff');later()}
 }catch(e){drop();say(false,'lost');later()}}
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
      say(true,'connected');if(T.via)toast(T.via);flush();tellPos(true)}return}
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
 else if(o.t==='help')carry(o)}
async function sendItem(it){if(!lineOpen())throw new Error('closed');
 const buf=new Uint8Array(await it.blob.arrayBuffer());
 const done=new Promise((resolve,reject)=>{acks.set(it.id,{resolve:resolve,reject:reject});
  setTimeout(()=>{if(acks.delete(it.id))reject(new Error('timeout'))},180000)});
 lineText(JSON.stringify({t:'photo',id:it.id,meta:it.meta,size:buf.length,type:it.blob.type||'image/jpeg'}));
 for(let o=0;o<buf.length;o+=16384){while(lineBuffered()>1048576){if(!lineOpen())throw new Error('closed');
   await sleep(25)}lineBin(buf.slice(o,o+16384))}
 lineText(JSON.stringify({t:'end',id:it.id}));return await done}
async function alive(){if(lineOpen())return true;connect();return false}
function offline(){if(lineOpen())return;
 if(lastState==='connected'||!lastState)say(false,'connecting');else say(false,lastState);connect()}
(async()=>{if(!KEYB||KEYB.length!==43||!window.isSecureContext||!window.RTCPeerConnection){
  $('go').disabled=true;status(false,!KEYB?'nokey':'insecure');return}
 await bridgeKey(KEYB);SIG.start(onSignal,n=>{if(n&&!linking&&!lineOpen()){backoff=4000;connect()}});
 window.addEventListener('online',()=>{backoff=4000;connect()})})();
"""

# ---- the PC: a hidden page that answers phones and hands their photos to the photo server ----
RECV_JS = r"""
const HP=new URLSearchParams(location.hash.slice(1)),peers=new Map();let CFG={kinds:[],lang:'en',form:[]};
function tell(ev){fetch('bridge-event',{method:'POST',body:JSON.stringify(ev)}).catch(()=>{})}
const ports=new Set();let GUIDE_TXT='{"v":0,"items":[]}',GV=0;   /* every phone: on its own channel, or carried */
setInterval(async()=>{try{const txt=await (await fetch('guide',{cache:'no-store'})).text(),g=JSON.parse(txt);
 if(g.v!==GV){GV=g.v;GUIDE_TXT=txt;for(const p of ports)try{p.send('{"t":"guide","g":'+txt+'}')}catch(e){}}}catch(e){}},3000);
async function onOffer(o){if(o.t!=='offer'||!o.sid||peers.has(o.sid))return;
 const pc=new RTCPeerConnection({iceServers:BR.ice});peers.set(o.sid,pc);
 const end=()=>{peers.delete(o.sid);try{pc.close()}catch(e){}};
 pc.ondatachannel=e=>serve(e.channel,end,pc);
 pc.oniceconnectionstatechange=()=>{const s=pc.iceConnectionState;if(s==='failed'){tell({type:'p2pfail'});end()}
  else if(s==='closed')end()};
 setTimeout(()=>{if(pc.iceConnectionState==='new'||pc.iceConnectionState==='checking')end()},45000);
 try{await pc.setRemoteDescription({type:'offer',sdp:o.sdp});await pc.setLocalDescription(await pc.createAnswer());
  await gathered(pc,3000);await SIG.send({t:'answer',sid:o.sid,sdp:pc.localDescription.sdp},!!o.z)}catch(e){end()}}
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
 else if(m.t==='photo')P.cur={id:m.id,meta:m.meta,size:+m.size||0,type:String(m.type||'image/jpeg'),parts:[],got:0};
 else if(m.t==='end'&&P.cur&&P.cur.id===m.id){const c=P.cur;P.cur=null;let st=400;
  if(c.got===c.size){try{st=(await fetch('photo?m='+encodeURIComponent(JSON.stringify(c.meta)),{method:'POST',
    body:new Blob(c.parts,{type:c.type}),headers:{'Content-Type':c.type}})).status}catch(x){st=503}}
  try{P.send(JSON.stringify({t:'ack',id:c.id,status:st}))}catch(x){}}}
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
 await bridgeKey(HP.get('k')||'');SIG.start(onOffer,n=>tell({type:'relays',up:n,of:SIG.relays.length}));
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
    page = head + _core() + PHONE_JS + tail
    page = page.replace("/*BOOT*/", BOOT_JS)
    return (page.replace("__LANG__", "en").replace("__DEFLANG__", "").replace("__DIR__", "ltr")
            .replace("__TEXTS__", json.dumps(all_texts, ensure_ascii=False, sort_keys=True).replace("</", "<\\/"))
            .replace("__KINDS__", "[]").replace("__FORM__", "[]").replace("__PCKEY__", ""))


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
