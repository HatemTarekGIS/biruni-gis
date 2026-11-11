"""Extension: viewers relay to viewers («شبكة الموبايلات»).

When many people watch one dashboard from anywhere (a meeting of 200), the PC's hidden host page serves at most
a few dozen direct channels. With the mesh, every viewer that has the dashboard can pass it on to a few others
over direct WebRTC channels between the phones; a phone that cannot reach the PC gets it from a phone that can.

Nobody in the middle can fake a number: for a viewer that asks for it ({"t":"mesh_hello"}), the PC wraps every
dashboard and data message of that session in a signed envelope

    {"t":"mesh_env", "b": "<the JSON text of {card, sid, full, m}>", "sg": "<ed25519 of b, hex>"}

signed with the dashboard key - the key that signed the viewer's own card, so the page knows the key is the
PC's without trusting anyone. A relaying phone forwards the envelopes unchanged; every phone checks the
signature, the card id and the order (rev + 1 in one session's chain, or a full state) before it applies or
forwards anything. A gap asks upstream for a full state ({"t":"mesh_full"} at the phone that has the PC).

Signalling between phones uses the bridge's relays (the same meeting name as the link's key: only the viewers
of the same links meet), with message types mesh_hi / mesh_offer / mesh_answer.
"""
import json
import time

from ..secure import crypto
from . import cards
from .service import Session, handler

FULL_EVERY = 2.0            # seconds between two full states asked by one session


def _sign(text):
    return crypto.sign(cards.seed(), text.encode("utf-8")).hex()


def envelope(session, msg):
    """The signed envelope of a dash/data message for this session."""
    full = msg.get("t") == "dash" or bool(getattr(session, "_mesh_full", False))
    if msg.get("t") == "data":
        session._mesh_full = False
    body = json.dumps({"card": (session.card or {}).get("id"), "sid": session.id, "full": full, "m": msg},
                      ensure_ascii=False, separators=(",", ":"), default=str)
    return {"t": "mesh_env", "b": body, "sg": _sign(body)}


_plain_out = Session._out


def _out(self, msg):
    if (getattr(self, "mesh", False) and isinstance(msg, dict) and msg.get("t") in ("dash", "data")
            and self.card is not None):
        try:
            msg = envelope(self, msg)
        except Exception:  # noqa: BLE001 - never lose a message: send it as it is
            pass
    return _plain_out(self, msg)


if not getattr(Session._out, "_mesh", False):
    _out._mesh = True
    Session._out = _out


@handler("mesh_hello")
def mesh_hello(svc, s, msg):
    """From now on this session's messages are signed envelopes; start with a full state (dash + all data)."""
    if s.card is None:                       # the builder's preview has no card: nothing to relay
        s._out({"t": "error", "why": "no mesh"})
        return
    s.mesh = True
    s._mesh_full = True
    s._mesh_at = time.monotonic()
    s.sent = {}
    svc._welcome(s)


@handler("mesh_full")
def mesh_full(svc, s, msg):
    """A full state again (a phone further down the mesh missed a change)."""
    if not getattr(s, "mesh", False):
        return
    now = time.monotonic()
    if now - getattr(s, "_mesh_at", 0) < FULL_EVERY:
        return
    s._mesh_at = now
    s._mesh_full = True
    s.sent = {}
    svc._push(s)


TEXTS = {
    "en": {"ext_mesh_via": "Through a nearby viewer", "ext_mesh_relay": "Passing it on to {n}",
           "ext_mesh_bad": "A changed message from another viewer was refused."},
    "ar": {"ext_mesh_via": "عبر مشاهد قريب", "ext_mesh_relay": "تُمرَّر إلى {n}",
           "ext_mesh_bad": "رُفضت رسالة معدَّلة من مشاهد آخر."},
    "es": {"ext_mesh_via": "A través de un espectador cercano", "ext_mesh_relay": "Se reenvía a {n}",
           "ext_mesh_bad": "Se rechazó un mensaje modificado de otro espectador."},
    "fr": {"ext_mesh_via": "Par un spectateur proche", "ext_mesh_relay": "Relayé vers {n}",
           "ext_mesh_bad": "Un message modifié venant d'un autre spectateur a été refusé."},
    "pt_BR": {"ext_mesh_via": "Por um espectador próximo", "ext_mesh_relay": "Repassando para {n}",
              "ext_mesh_bad": "Uma mensagem alterada de outro espectador foi recusada."},
    "tr": {"ext_mesh_via": "Yakındaki bir izleyici üzerinden", "ext_mesh_relay": "{n} kişiye aktarılıyor",
           "ext_mesh_bad": "Başka bir izleyiciden gelen değiştirilmiş bir mesaj reddedildi."},
    "fa": {"ext_mesh_via": "از طریق بیننده‌ای نزدیک", "ext_mesh_relay": "در حال ارسال به {n}",
           "ext_mesh_bad": "پیامی تغییریافته از بیننده‌ای دیگر رد شد."},
}

CSS = r"""
.ext_mesh_tag{font-size:11.5px;font-weight:600;color:var(--muted);padding:2px 8px;border-radius:99px;border:1px solid var(--line,#8884)}
.ext_mesh_tag.hide{display:none}
"""

JS = r"""
const BD=window.BiruniDash,ED=window.BiruniEd,te=new TextEncoder();
const FANOUT=4,MAXCHAIN=60,MAXHOPS=4,PART=15000;
function cardInfo(card){try{const p=String(card||'').split('.');if(p.length!==3||p[0]!=='BDC1')return null;
  const body=ED.b64.dec(p[1]);return {body:body,sig:ED.b64.dec(p[2]),id:JSON.parse(new TextDecoder().decode(body)).id}}catch(e){return null}}
/* one viewer's place in the mesh: what it trusts, what it has, whom it serves */
class MeshNode{
 constructor(o){this.id=o.id;this.cinfo=cardInfo(o.card);this.apply=o.apply||(()=>{});this.askPc=o.askPc||null;this.fanout=o.fanout||FANOUT;
  this.up=null;this.upSend=null;this.hops=99;this.down=new Map();this.dash=null;this.chain=[];this.last=null;this.pub=null;
  this.seen=new Set();this.q=Promise.resolve();this.lastNeed=0;this.onChange=o.onChange||(()=>{});
  this.stats={applied:0,rejected:0,forwarded:0,dups:0,needs:0}}
 receive(env,from){this.q=this.q.then(()=>this._receive(env,from)).catch(()=>{});return this.q}
 reject(why){this.stats.rejected++;this.lastReject=why;return false}
 async _receive(env,from){
  if(!env||typeof env.b!=='string'||typeof env.sg!=='string'||env.b.length>4e6)return this.reject('shape');
  let inner;try{inner=JSON.parse(env.b)}catch(e){return this.reject('json')}const m=inner&&inner.m;
  if(!m||(m.t!=='dash'&&m.t!=='data'))return this.reject('type');
  if(!this.cinfo)return this.reject('nocard');
  if(!this.pub){const pub=m.t==='dash'&&/^[0-9a-f]{64}$/.test(m.pub||'')?m.pub:null;
   if(!pub||!(await ED.verify(ED.fromHex(pub),this.cinfo.sig,this.cinfo.body)))return this.reject('key');
   this.pub=pub}
  else if(m.t==='dash'&&m.pub!==this.pub)return this.reject('key');
  if(!(await ED.verify(ED.fromHex(this.pub),ED.fromHex(env.sg),te.encode(env.b))))return this.reject('sig');
  if(inner.card!==this.cinfo.id)return this.reject('card');
  const key=inner.sid+':'+(m.t==='dash'?'d':m.rev)+':'+(inner.full?1:0);
  if(this.seen.has(key)){this.stats.dups++;return false}
  if(m.t==='dash'){this.seen.add(key);this.dash=env;this.apply(m);this.stats.applied++;this.forward(env,from);this.onChange();return true}
  if(inner.full){this.seen.add(key);this.chain=[env];this.last={sid:inner.sid,rev:m.rev};this.apply(m);this.stats.applied++;this.forward(env,from);this.onChange();return true}
  if(this.last&&inner.sid===this.last.sid&&m.rev===this.last.rev+1){this.seen.add(key);this.chain.push(env);this.last.rev=m.rev;
   this.apply(m);this.stats.applied++;this.forward(env,from);if(this.chain.length>MAXCHAIN)this.need();return true}
  if(this.last&&inner.sid===this.last.sid&&m.rev<=this.last.rev){this.stats.dups++;return false}
  this.need();return false}
 forward(env,from){for(const [id,send] of this.down)if(id!==from){try{send({t:'env',env:env});this.stats.forwarded++}catch(e){}}}
 need(){this.stats.needs++;const now=Date.now();if(now-this.lastNeed<1500)return;this.lastNeed=now;
  if(this.up==='pc'&&this.askPc)this.askPc();else if(this.upSend)try{this.upSend({t:'need'})}catch(e){}}
 addDown(id,send){if(this.down.size>=this.fanout||this.down.has(id))return false;this.down.set(id,send);
  if(this.dash)send({t:'env',env:this.dash});for(const e of this.chain)send({t:'env',env:e});this.onChange();return true}
 dropDown(id){this.down.delete(id);this.onChange()}
 setUp(id,send,hops){this.up=id;this.upSend=send;this.hops=hops;this.onChange()}
 has(){return !!(this.dash&&this.last)}
 onPeer(from,msg){if(!msg||typeof msg!=='object')return;if(msg.t==='env')this.receive(msg.env,from);
  else if(msg.t==='need'&&this.down.has(from))this.need()}}

/* channels: JSON messages in parts (data channels keep messages small) */
function chanSend(ch){return o=>{if(ch.readyState!=='open')return;const s=JSON.stringify(o);
 if(s.length<=PART){ch.send(s);return}const id=Math.random().toString(36).slice(2),n=Math.ceil(s.length/PART);
 for(let i=0;i<n;i++)ch.send(JSON.stringify({t:'part',id:id,i:i,n:n,d:s.slice(i*PART,(i+1)*PART)}))}}
function chanRecv(fn){const parts={};return e=>{if(typeof e.data!=='string'||e.data.length>PART*2)return;let m;try{m=JSON.parse(e.data)}catch(x){return}
 if(m&&m.t==='part'){const p=parts[m.id]||(parts[m.id]={n:+m.n||0,got:0,s:[]});if(p.n>400)return;if(p.s[m.i]==null){p.s[m.i]=String(m.d||'');p.got++}
  if(p.got>=p.n){delete parts[m.id];try{fn(JSON.parse(p.s.join('')))}catch(x){}}return}fn(m)}}

/* the network side: meeting other viewers through a signalling bus ({send(obj), set onmsg}) and WebRTC */
class MeshNet{
 constructor(node,bus,o){this.node=node;this.bus=bus;this.o=o||{};this.peers=new Map();this.his=new Map();this.tried=new Map();
  this.cid=node.cinfo&&node.cinfo.id;bus.onmsg=m=>this.onSig(m);this.timer=setInterval(()=>this.tick(),this.o.every||15000)}
 ice(){return this.o.ice||((typeof BR!=='undefined')?BR.ice:[])}
 hops(){return this.node.up==='pc'?1:(this.node.up?this.node.hops:99)}
 tick(){const n=this.node;if(n.has()&&this.hops()<MAXHOPS)this.bus.send({t:'mesh_hi',id:n.id,cid:this.cid,hops:this.hops(),free:n.fanout-n.down.size});
  if(!n.up&&!this.linking)this.seek()}
 needy(){return !this.node.up}
 seek(){const now=Date.now();const c=[...this.his.values()].filter(h=>now-h.at<60000&&h.free>0&&h.hops<MAXHOPS&&!(this.tried.get(h.id)>now-30000))
  .sort((a,b)=>a.hops-b.hops||b.free-a.free)[0];if(c)this.offer(c)}
 async offer(h){this.linking=true;this.tried.set(h.id,Date.now());const pc=new RTCPeerConnection({iceServers:this.ice()});
  const ch=pc.createDataChannel('mesh',{ordered:true});const peer={pc:pc,ch:ch,id:h.id,role:'up'};this.peers.set(h.id,peer);
  ch.onopen=()=>{this.linking=false;this.node.setUp(h.id,chanSend(ch),h.hops+1)};
  ch.onmessage=chanRecv(m=>this.node.onPeer(h.id,m));
  ch.onclose=()=>this.lost(h.id);
  try{await pc.setLocalDescription(await pc.createOffer());await this.gathered(pc);
   this.bus.send({t:'mesh_offer',to:h.id,from:this.node.id,cid:this.cid,sdp:pc.localDescription.sdp});
   setTimeout(()=>{if(this.linking&&peer.ch.readyState!=='open'){this.linking=false;this.lost(h.id)}},20000)}catch(e){this.linking=false;this.lost(h.id)}}
 async answer(m){const n=this.node;if(n.down.size>=n.fanout||!n.has()||this.peers.has(m.from))return;
  const pc=new RTCPeerConnection({iceServers:this.ice()});const peer={pc:pc,id:m.from,role:'down'};this.peers.set(m.from,peer);
  pc.ondatachannel=e=>{const ch=e.channel;peer.ch=ch;ch.onopen=()=>{if(!n.addDown(m.from,chanSend(ch))){try{pc.close()}catch(x){}}};
   ch.onmessage=chanRecv(x=>n.onPeer(m.from,x));ch.onclose=()=>this.lost(m.from)};
  try{await pc.setRemoteDescription({type:'offer',sdp:m.sdp});await pc.setLocalDescription(await pc.createAnswer());await this.gathered(pc);
   this.bus.send({t:'mesh_answer',to:m.from,from:n.id,cid:this.cid,sdp:pc.localDescription.sdp})}catch(e){this.lost(m.from)}}
 gathered(pc){return new Promise(r=>{if(pc.iceGatheringState==='complete')return r();const t=setTimeout(r,this.o.gather||3000);
  pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){clearTimeout(t);r()}})})}
 onSig(m){if(!m||m.cid!==this.cid||m.from===this.node.id||m.id===this.node.id)return;
  if(m.t==='mesh_hi'){this.his.set(m.id,{id:m.id,hops:+m.hops||99,free:+m.free||0,at:Date.now()});if(this.needy()&&!this.linking)this.seek()}
  else if(m.t==='mesh_offer'&&m.to===this.node.id)this.answer(m);
  else if(m.t==='mesh_answer'&&m.to===this.node.id){const p=this.peers.get(m.from);if(p&&p.role==='up'&&p.pc.signalingState==='have-local-offer')
   p.pc.setRemoteDescription({type:'answer',sdp:m.sdp}).catch(()=>this.lost(m.from))}}
 lost(id){const p=this.peers.get(id);this.peers.delete(id);if(p)try{p.pc.close()}catch(e){}
  if(this.node.up===id){this.node.setUp(null,null,99);this.linking=false}if(this.node.down.has(id))this.node.dropDown(id)}
 direct(){for(const [id,p] of this.peers)if(p.role==='up')this.lost(id);this.node.setUp('pc',null,0)}
 stop(){clearInterval(this.timer);for(const id of [...this.peers.keys()])this.lost(id)}}
window.BiruniMesh={MeshNode:MeshNode,MeshNet:MeshNet,cardInfo:cardInfo};

/* ---- on a real page: viewers of a link from anywhere ---- */
if(BD.transport==='bridge'&&typeof CARD!=='undefined'&&CARD&&typeof SIG!=='undefined'&&!window.__noMesh){
 let fromMesh=false;const tag=document.createElement('span');tag.className='ext_mesh_tag hide';tag.id='ext_mesh_tag';
 const st=document.querySelector('.status');if(st)st.insertBefore(tag,st.firstChild);
 const node=new MeshNode({id:rid(),card:CARD,apply:m=>{fromMesh=true;try{BD.feed(m)}finally{fromMesh=false}},
  askPc:()=>BD.send({t:'mesh_full'}),onChange:()=>{const n=node;
   if(n.up&&n.up!=='pc'){tag.textContent=BD.t('ext_mesh_via');tag.classList.remove('hide')}
   else if(n.down.size){tag.textContent=BD.t('ext_mesh_relay').replace('{n}',n.down.size);tag.classList.remove('hide')}else tag.classList.add('hide')}});
 BD.onBuild(()=>{if(fromMesh)return;net&&net.direct();BD.send({t:'mesh_hello'})});
 BD.on('mesh_env',m=>{if(node.up!=='pc'){if(net)net.direct();else node.setUp('pc',null,0)}node.receive(m,'pc')});
 const bus={onmsg:null,send:o=>{SIG.send(o,true).catch(()=>{})}};let net=null;
 (function hook(){if(!SIG.on||!SIG.relays||!SIG.relays.length)return setTimeout(hook,800);if(SIG.on.__mesh)return;const prev=SIG.on;
  const f=o=>{if(o&&typeof o.t==='string'&&o.t.indexOf('mesh_')===0){if(bus.onmsg)bus.onmsg(o)}else return prev(o)};f.__mesh=1;SIG.on=f;
  net=new MeshNet(node,bus);
  /* no dashboard from the PC after a while: ask the viewers around */
  setTimeout(()=>{if(!node.up)net.tick()},25000)})();
 window.__mesh={node:()=>node,net:()=>net}}
"""
