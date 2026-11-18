"""Extension: share by sound («المشاركة بالصوت»).

In a meeting room, the presenter (a phone or this PC) plays a 1.6-second burst of tones (sound_modem.py). The
phones around that have the dashboard page open with "Listen" decode it: it carries how to reach the PC (the
bridge key, or this PC's address on the network) and a join token. The phone sends the token to the PC
({"t":"sound_join"} - the only message a session may send before its hello), and the PC issues it a short card
(minutes, not days) that can never see more than the presenter's own card: the same widgets, hidden fields,
zone and forced filters. The phone keeps the card in its address, so a reload stays in.

A token is good for 10 minutes and 60 phones; a presenter session may ask for 10 tokens a minute.
"""
import secrets
import time

from ..i18n import tr
from . import cards, sound_modem
from .service import DashboardService, handler

TOKEN_TTL = 600
MAX_USES = 60
TOKENS = {}                 # hex token -> {dash, widgets, hide, zone, filters, minutes, exp_limit, until, uses}


def new_token(dash_id, widgets="*", hide=(), zone=None, filters=None, minutes=120, exp_limit=None):
    tok = secrets.token_bytes(8)
    now = time.time()
    for k in [k for k, v in TOKENS.items() if v["until"] < now]:
        del TOKENS[k]
    TOKENS[tok.hex()] = {"dash": dash_id, "widgets": widgets, "hide": list(hide or ()), "zone": zone,
                         "filters": filters, "minutes": int(minutes), "exp_limit": exp_limit,
                         "until": now + TOKEN_TTL, "uses": 0}
    return tok


def token_for_card(dash_id, card, minutes=120):
    """A join token that gives no more than ``card`` (a card payload; None = the whole dashboard, for this PC)."""
    c = card or {}
    return new_token(dash_id, c.get("widgets", "*"), c.get("hide") or (), c.get("zone"), c.get("filters"),
                     minutes, c.get("exp"))


@handler("sound_token")
def sound_token(svc, s, msg):
    now = time.time()
    hist = [t for t in getattr(s, "_sound_asks", []) if now - t < 60]
    if len(hist) >= 10:
        s._out({"t": "error", "why": "busy"})
        return
    s._sound_asks = hist + [now]
    try:
        minutes = max(5, min(720, int(msg.get("minutes") or 120)))
    except (TypeError, ValueError):
        minutes = 120
    tok = token_for_card(s.dash_id, s.card, minutes)
    s._out({"t": "sound_token", "token": tok.hex(), "minutes": minutes, "ttl": TOKEN_TTL})


def join(svc, s, msg):
    tok = str(msg.get("token", ""))[:32]
    info = TOKENS.get(tok)
    now = time.time()
    if info is None or info["until"] < now or info["uses"] >= MAX_USES or info["dash"] not in svc.defs:
        s._out({"t": "error", "why": "bad token"})
        return
    info["uses"] += 1
    exp = now + info["minutes"] * 60
    if info.get("exp_limit"):
        exp = min(exp, float(info["exp_limit"]))
    card = cards.issue(info["dash"], "Guest (by sound)", widgets=info["widgets"], hide=info["hide"],
                       zone_wkt=info["zone"], filters=info["filters"], exp=exp, project=svc.project)
    s._out({"t": "sound_card", "card": card})
    svc._receive(s, {"t": "hello", "card": card, "lang": msg.get("lang")})


_plain_receive = DashboardService._receive


def _receive(self, s, msg):
    if isinstance(msg, dict) and msg.get("t") == "sound_join" and not s.ready:
        try:
            return join(self, s, msg)
        except Exception as e:  # noqa: BLE001
            s._out({"t": "error", "why": "failed: " + str(e)[:120]})
            return None
    return _plain_receive(self, s, msg)


if not getattr(DashboardService._receive, "_sound", False):
    _receive._sound = True
    DashboardService._receive = _receive


# ---- this PC as the presenter: the dashboard panel's action ------------------------------------------------------------
def invite_audio(dock, minutes=120):
    """(wav bytes, payload, token) inviting the phones in the room to the dock's current dashboard (with the
    selected card's limits, if a card is selected)."""
    sharing = getattr(dock, "sharing", None)
    if sharing is None or not sharing.running():
        raise RuntimeError("not sharing")
    d = dock.current()
    if d is None:
        raise RuntimeError("no dashboard")
    tok = token_for_card(d["id"], dock.selected_card(), minutes)
    if sharing.anywhere is not None and sharing.anywhere.running():
        payload = sound_modem.bridge_payload(sharing.anywhere.key, tok)
    else:
        ips = [a for a in sharing.addresses() if a.count(".") == 3 and not a.startswith("127.")]
        if not ips:
            raise RuntimeError("no address")
        payload = sound_modem.lan_payload(ips[0], sharing.lan.port, sharing.lan.token, tok)
    wav = sound_modem.wav_bytes(sound_modem.synth(sound_modem.block(payload)))
    return wav, payload, tok


def _dialog(plugin, dock):
    import os
    import tempfile

    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout

    title = tr("Invite by sound")
    dlg = QDialog(dock)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    lab = QLabel(tr("The phones in the room open the dashboard page and tap «Listen»; this computer plays a short "
                    "sound and they join with a card of a few hours that sees no more than the card selected in "
                    "the list (or the whole dashboard)."))
    lab.setWordWrap(True)
    lay.addWidget(lab)
    mins = QSpinBox()
    mins.setRange(5, 720)
    mins.setValue(120)
    mins.setSuffix(" " + tr("minutes"))
    lay.addWidget(mins)
    state = QLabel("")
    state.setWordWrap(True)
    lay.addWidget(state)
    play = QPushButton(tr("Play the sound"))
    lay.addWidget(play)
    holder = {}

    def go():
        try:
            wav, _p, _t = invite_audio(dock, mins.value())
        except RuntimeError as e:
            why = {"not sharing": tr("Share the dashboard first (on this network or from anywhere)."),
                   "no dashboard": tr("Choose a dashboard first."),
                   "no address": tr("This computer has no network address the phones can reach.")}.get(str(e), str(e))
            QMessageBox.information(dlg, title, why)
            return
        path = os.path.join(tempfile.gettempdir(), "biruni_invite.wav")
        with open(path, "wb") as f:
            f.write(wav)
        try:
            from qgis.PyQt.QtMultimedia import QSoundEffect

            fx = holder.get("fx") or QSoundEffect(dlg)
            holder["fx"] = fx
            fx.setSource(QUrl.fromLocalFile(path))
            fx.setLoopCount(3)
            fx.setVolume(1.0)
            fx.play()
            state.setText(tr("Playing… (three times). The invitation is good for 10 minutes."))
        except Exception:  # noqa: BLE001 - no sound device: the file can still be played
            state.setText(tr("The sound could not be played here; it is saved in {path}").format(path=path))

    play.clicked.connect(go)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.show()
    return dlg


ACTIONS = [("sound_invite", "Invite by sound", "antenna", _dialog)]

TEXTS = {
    "en": {"ext_sound_invite": "Invite by sound", "ext_sound_listen": "Listen to join",
           "ext_sound_playing": "Playing the invitation… Phones nearby: open the dashboard page and tap «Listen».",
           "ext_sound_again": "Play again", "ext_sound_hint": "Hold the phone near the speaker. Listening…",
           "ext_sound_heard": "Invitation heard — joining…", "ext_sound_nomic": "This browser cannot use the microphone.",
           "ext_sound_bad": "This invitation has expired. Ask for a new one.",
           "ext_sound_valid": "Guests see what you see, for {m} minutes."},
    "ar": {"ext_sound_invite": "دعوة بالصوت", "ext_sound_listen": "استمع للانضمام",
           "ext_sound_playing": "تُشغَّل الدعوة… الهواتف القريبة: افتحوا صفحة اللوحة واضغطوا «استمع».",
           "ext_sound_again": "شغّلها مرة أخرى", "ext_sound_hint": "قرّب الهاتف من مكبّر الصوت. جارٍ الاستماع…",
           "ext_sound_heard": "سُمعت الدعوة — جارٍ الانضمام…", "ext_sound_nomic": "لا يستطيع هذا المتصفح استخدام الميكروفون.",
           "ext_sound_bad": "انتهت صلاحية هذه الدعوة. اطلب دعوة جديدة.",
           "ext_sound_valid": "يرى الضيوف ما تراه، لمدة {m} دقيقة."},
    "es": {"ext_sound_invite": "Invitar por sonido", "ext_sound_listen": "Escuchar para unirse",
           "ext_sound_playing": "Reproduciendo la invitación… Teléfonos cercanos: abran la página y toquen «Escuchar».",
           "ext_sound_again": "Reproducir de nuevo", "ext_sound_hint": "Acerque el teléfono al altavoz. Escuchando…",
           "ext_sound_heard": "Invitación recibida — uniéndose…", "ext_sound_nomic": "Este navegador no puede usar el micrófono.",
           "ext_sound_bad": "Esta invitación ha caducado. Pida una nueva.",
           "ext_sound_valid": "Los invitados ven lo que usted ve, durante {m} minutos."},
    "fr": {"ext_sound_invite": "Inviter par le son", "ext_sound_listen": "Écouter pour rejoindre",
           "ext_sound_playing": "Lecture de l'invitation… Téléphones proches : ouvrez la page et touchez « Écouter ».",
           "ext_sound_again": "Rejouer", "ext_sound_hint": "Approchez le téléphone du haut-parleur. Écoute…",
           "ext_sound_heard": "Invitation entendue — connexion…", "ext_sound_nomic": "Ce navigateur ne peut pas utiliser le micro.",
           "ext_sound_bad": "Cette invitation a expiré. Demandez-en une nouvelle.",
           "ext_sound_valid": "Les invités voient ce que vous voyez, pendant {m} minutes."},
    "pt_BR": {"ext_sound_invite": "Convidar por som", "ext_sound_listen": "Ouvir para entrar",
              "ext_sound_playing": "Tocando o convite… Celulares próximos: abram a página e toquem em «Ouvir».",
              "ext_sound_again": "Tocar de novo", "ext_sound_hint": "Aproxime o celular do alto-falante. Ouvindo…",
              "ext_sound_heard": "Convite ouvido — entrando…", "ext_sound_nomic": "Este navegador não pode usar o microfone.",
              "ext_sound_bad": "Este convite expirou. Peça um novo.",
              "ext_sound_valid": "Os convidados veem o que você vê, por {m} minutos."},
    "tr": {"ext_sound_invite": "Sesle davet et", "ext_sound_listen": "Katılmak için dinle",
           "ext_sound_playing": "Davet çalınıyor… Yakındaki telefonlar: pano sayfasını açıp «Dinle»ye dokunun.",
           "ext_sound_again": "Tekrar çal", "ext_sound_hint": "Telefonu hoparlöre yaklaştırın. Dinleniyor…",
           "ext_sound_heard": "Davet duyuldu — katılınıyor…", "ext_sound_nomic": "Bu tarayıcı mikrofonu kullanamıyor.",
           "ext_sound_bad": "Bu davetin süresi doldu. Yenisini isteyin.",
           "ext_sound_valid": "Konuklar {m} dakika boyunca sizin gördüğünüzü görür."},
    "fa": {"ext_sound_invite": "دعوت با صدا", "ext_sound_listen": "برای پیوستن گوش کنید",
           "ext_sound_playing": "دعوت پخش می‌شود… گوشی‌های نزدیک: صفحهٔ داشبورد را باز کنید و «گوش کنید» را بزنید.",
           "ext_sound_again": "پخش دوباره", "ext_sound_hint": "گوشی را نزدیک بلندگو نگه دارید. در حال گوش دادن…",
           "ext_sound_heard": "دعوت شنیده شد — در حال پیوستن…", "ext_sound_nomic": "این مرورگر نمی‌تواند از میکروفون استفاده کند.",
           "ext_sound_bad": "این دعوت منقضی شده است. دعوت تازه‌ای بخواهید.",
           "ext_sound_valid": "مهمانان آنچه شما می‌بینید را {m} دقیقه می‌بینند."},
}

CSS = r"""
.ext_sound_box{display:flex;flex-direction:column;gap:14px;align-items:center;text-align:center;padding:8px 4px 4px}
.ext_sound_wave{width:min(320px,80vw);height:64px}
.ext_sound_wave path{fill:none;stroke:var(--accent);stroke-width:2.5;stroke-linecap:round}
.ext_sound_box button{font:inherit;padding:10px 18px;border-radius:12px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
.ext_sound_meter{width:min(320px,80vw);height:8px;border-radius:9px;background:var(--line,#8883);overflow:hidden}
.ext_sound_meter i{display:block;height:100%;width:0;background:var(--accent);transition:width .1s}
"""

JS = r"""
const BD=window.BiruniDash;
/* ---- the modem (the same as sound_modem.py) ---- */
const F0=1875,DF=46.875,SF=1406.25,NB=6,NT=16,FR=0.064,ST=0.192,DATA=44,NSYM=16,TOTAL=60,NFR=20;
const GE=new Uint8Array(512),GL=new Uint8Array(256);{let x=1;for(let i=0;i<255;i++){GE[i]=x;GL[x]=i;x<<=1;if(x&256)x^=0x11d}for(let i=255;i<512;i++)GE[i]=GE[i-255]}
const gm=(a,b)=>a&&b?GE[GL[a]+GL[b]]:0,gpw=(x,p)=>GE[(((GL[x]*p)%255)+255)%255],ginv=x=>GE[255-GL[x]],gdv=(a,b)=>a?GE[(GL[a]+255-GL[b])%255]:0;
const pScale=(p,x)=>p.map(c=>gm(c,x));
function pAdd(p,q){const r=new Array(Math.max(p.length,q.length)).fill(0);for(let i=0;i<p.length;i++)r[i+r.length-p.length]=p[i];
 for(let i=0;i<q.length;i++)r[i+r.length-q.length]^=q[i];return r}
function pMul(p,q){const r=new Array(p.length+q.length-1).fill(0);for(let j=0;j<q.length;j++)for(let i=0;i<p.length;i++)r[i+j]^=gm(p[i],q[j]);return r}
function pEval(p,x){let y=p[0];for(let i=1;i<p.length;i++)y=gm(y,x)^p[i];return y}
function pDiv(d,v){const o=[...d];for(let i=0;i<d.length-(v.length-1);i++){const c=o[i];if(c)for(let j=1;j<v.length;j++)if(v[j])o[i+j]^=gm(v[j],c)}
 const k=o.length-(v.length-1);return [o.slice(0,k),o.slice(k)]}
function rsEnc(msg,n){let g=[1];for(let i=0;i<n;i++)g=pMul(g,[1,gpw(2,i)]);const o=[...msg,...new Array(n).fill(0)];
 for(let i=0;i<msg.length;i++){const c=o[i];if(c)for(let j=1;j<g.length;j++)o[i+j]^=gm(g[j],c)}return [...msg,...o.slice(msg.length)]}
function rsDec(msg,n){let m=[...msg];const sy=[0];for(let i=0;i<n;i++)sy.push(pEval(m,gpw(2,i)));if(sy.every(x=>!x))return m.slice(0,m.length-n);
 let el=[1],ol=[1];const sh=sy.length-n;
 for(let i=0;i<n;i++){const K=i+sh;let d=sy[K];for(let j=1;j<el.length;j++)d^=gm(el[el.length-1-j],sy[K-j]);ol=[...ol,0];
  if(d){if(ol.length>el.length){const nl=pScale(ol,d);ol=pScale(el,ginv(d));el=nl}el=pAdd(el,pScale(ol,d))}}
 while(el.length&&!el[0])el.shift();const errs=el.length-1;if(errs<1||errs*2>n)return null;
 const rl=[...el].reverse(),pos=[];for(let i=0;i<m.length;i++)if(pEval(rl,gpw(2,i))===0)pos.push(m.length-1-i);if(pos.length!==errs)return null;
 const cp=pos.map(p=>m.length-1-p);let eloc=[1];for(const c of cp)eloc=pMul(eloc,pAdd([1],[gpw(2,c),0]));
 const rem=pDiv(pMul([...sy].reverse(),eloc),[1,...new Array(eloc.length).fill(0)])[1];
 const X=cp.map(c=>gpw(2,-(255-c))),E=new Array(m.length).fill(0);
 for(let i=0;i<X.length;i++){const xi=ginv(X[i]);let lp=1;for(let j=0;j<X.length;j++)if(j!==i)lp=gm(lp,1^gm(xi,X[j]));if(!lp)return null;
  E[pos[i]]=gdv(gm(X[i],pEval(rem,xi)),lp)}
 m=pAdd(m,E);for(let i=0;i<n;i++)if(pEval(m,gpw(2,i)))return null;return m.slice(0,m.length-n)}
function block(payload){const p=[...payload];if(p.length>DATA-1)throw new Error('long');return rsEnc([p.length,...p,...new Array(DATA-1-p.length).fill(0)],NSYM)}
const tone=(b,k)=>F0+(b*NT+k)*DF;
function synth(code,sr){const ramp=Math.round(0.004*sr),parts=[];
 const burst=(fs,dur,amp)=>{const n=Math.round(dur*sr),s=new Float32Array(n);for(const f of fs){const w=2*Math.PI*f/sr;for(let i=0;i<n;i++)s[i]+=amp*Math.sin(w*i)}
  for(let i=0;i<Math.min(ramp,n);i++){const k=0.5-0.5*Math.cos(Math.PI*i/ramp);s[i]*=k;s[n-1-i]*=k}return s};
 parts.push(new Float32Array(Math.round(0.08*sr)),burst([SF],ST,0.8));const nib=[];for(const b of code)nib.push(b>>4,b&15);
 for(let k=0;k<code.length/3;k++)parts.push(burst([0,1,2,3,4,5].map(b=>tone(b,nib[k*6+b])),FR,0.14));parts.push(new Float32Array(Math.round(0.12*sr)));
 const out=new Float32Array(parts.reduce((n,p)=>n+p.length,0));let o=0;for(const p of parts){out.set(p,o);o+=p.length}return out}
function gz(x,s,n,f,sr){const c=2*Math.cos(2*Math.PI*f/sr);let s1=0,s2=0;for(let i=0;i<n;i++){const s0=x[s+i]+c*s1-s2;s2=s1;s1=s0}return s1*s1+s2*s2-c*s1*s2}
/* one frame's tone energies; "late" listens at the end of the frame, where the echo of the frame before has
   died down most; alpha takes away that echo (a part of the frame before's energies) */
function frameE(x,start,sr,late){const n=Math.round(2*sr/DF),fl=Math.round(FR*sr);
 const s=late?start+fl-n-Math.round(0.003*sr):start+Math.round((fl-n)/2);if(s<0||s+n>x.length)return null;
 const e=new Float64Array(NB*NT);for(let b=0;b<NB;b++)for(let t=0;t<NT;t++)e[b*NT+t]=gz(x,s,n,tone(b,t),sr);return e}
function pick(e,prev,alpha){const nib=[];let conf=0;for(let b=0;b<NB;b++){let best=-1e300,bi=0,sec=-1e300;
  for(let t=0;t<NT;t++){const v=e[b*NT+t]-(prev?alpha*prev[b*NT+t]:0);if(v>best){sec=best;best=v;bi=t}else if(v>sec)sec=v}
  nib.push(bi);conf+=Math.log((Math.max(best,1e-12)+1e-12)/(Math.max(sec,1e-12)+1e-12))}return {nib:nib,conf:conf}}
function frameAt(x,start,sr){const e=frameE(x,start,sr,false);return e?pick(e,null,0):null}
function decodeAt(x,start,sr,alpha,late){const bytes=[];let prev=null;
 for(let k=0;k<NFR;k++){const e=frameE(x,start+Math.round(k*FR*sr),sr,!!late);if(!e)return null;const f=pick(e,prev,alpha||0);prev=e;
  for(let b=0;b<NB;b+=2)bytes.push((f.nib[b]<<4)|f.nib[b+1])}
 const blk=rsDec(bytes,NSYM);if(!blk)return null;const len=blk[0];if(len>DATA-1)return null;return Uint8Array.from(blk.slice(1,1+len))}
/* sync: windows where most of the energy is the 1406.25 Hz tone, for at least 120 ms; then the data starts (an echo
   can hold the tone on a while: the start is searched from a little before to 200 ms after) */
function syncEnds(x,sr,from){const n=Math.round(sr/DF),hop=Math.round(0.004*sr),out=[];let run=0,lastHi=-1;
 for(let i=Math.max(0,from||0);i+n<=x.length;i+=hop){let en=0;for(let j=0;j<n;j++)en+=x[i+j]*x[i+j];
  const fr=en>1e-7?gz(x,i,n,SF,sr)/(en*n/2):0;
  if(fr>0.5){run++;lastHi=i}else{if(run*hop>=0.12*sr&&fr<0.3)out.push(lastHi+n);if(fr<0.3)run=0}}
 return out}
const VARIANTS=[[0,0],[0.6,1],[0,1],[0.6,0],[1.0,1]];
function decodeSamples(x,sr,from){for(const end of syncEnds(x,sr,from)){const cands=[];
  for(let d=-0.04;d<=0.2001;d+=0.002){const st=end+Math.round(d*sr);let c=0,ok=true;for(let k=0;k<5;k++){const f=frameAt(x,st+Math.round(k*FR*sr),sr);if(!f){ok=false;break}c+=f.conf}
   if(ok)cands.push([c,st])}
  /* a start shifted by whole frames scores almost as well: keep the separate peaks only, and try them all */
  cands.sort((a,b)=>b[0]-a[0]);const peaks=[],gap=Math.round(0.016*sr);
  for(const c of cands){if(peaks.every(p=>Math.abs(p[1]-c[1])>gap))peaks.push(c);if(peaks.length>=10)break}
  for(const [al,late] of VARIANTS)for(const [,st] of peaks)for(const dd of [0,-2,2]){const p=decodeAt(x,st+Math.round(dd*sr/1000),sr,al,late);
   if(p)return {payload:p,at:st}}}
 return null}
function parsePayload(p){if(!p||p.length<2||p[0]!==1)return null;
 if(p[1]===1&&p.length===42)return {mode:'bridge',key:window.BiruniEd.b64.enc(p.subarray(2,34)),token:[...p.subarray(34,42)].map(x=>x.toString(16).padStart(2,'0')).join('')};
 if(p[1]===2&&p.length>=9){const ip=[...p.subarray(2,6)].join('.'),port=(p[6]<<8)|p[7],n=p[8];if(p.length!==9+n+8)return null;
  return {mode:'lan',ip:ip,port:port,path:new TextDecoder().decode(p.subarray(9,9+n)),token:[...p.subarray(9+n)].map(x=>x.toString(16).padStart(2,'0')).join('')}}
 return null}
function payloadFor(token){const tok=Uint8Array.from(token.match(/../g).map(h=>parseInt(h,16)));
 if(BD.transport==='bridge'&&typeof KEYB!=='undefined'&&KEYB)return [1,1,...window.BiruniEd.b64.dec(KEYB),...tok];
 const ip=location.hostname.split('.').map(Number),port=+location.port||443,m=location.pathname.match(/\/d\/([^/]+)\//);
 if(ip.length!==4||ip.some(isNaN)||!m)return null;const pt=new TextEncoder().encode(m[1]);return [1,2,...ip,port>>8,port&255,pt.length,...pt,...tok]}
window.BiruniSound={block:block,synth:synth,decodeSamples:decodeSamples,parsePayload:parsePayload,rsEnc:rsEnc,rsDec:rsDec,payloadFor:payloadFor};

/* ---- the presenter: "Invite by sound" ---- */
const ANT='M12 21V11M8 13a5 5 0 0 1 8 0M5 10a9 9 0 0 1 14 0M12 11a1 1 0 1 0 0-.01';
let lastCode=null,audio=null;
async function play(code){try{audio=audio||new (window.AudioContext||window.webkitAudioContext)();await audio.resume();
  const sr=audio.sampleRate,s=synth(code,sr),buf=audio.createBuffer(1,s.length,sr);buf.copyToChannel(s,0);
  for(let r=0;r<3;r++){const src=audio.createBufferSource();src.buffer=buf;src.connect(audio.destination);src.start(audio.currentTime+r*(s.length/sr+0.6))}}catch(e){}}
function waveSvg(){const ns='http://www.w3.org/2000/svg',s=document.createElementNS(ns,'svg');s.setAttribute('viewBox','0 0 320 64');s.setAttribute('class','ext_sound_wave');
 const p=document.createElementNS(ns,'path');let d='M0 32';for(let x=0;x<=320;x+=4)d+=' L'+x+' '+(32+Math.sin(x/9)*Math.sin(x/53)*26).toFixed(1);p.setAttribute('d',d);s.appendChild(p);return s}
BD.on('sound_token',m=>{const p=payloadFor(String(m.token||''));if(!p)return;lastCode=block(p);
 const box=document.createElement('div');box.className='ext_sound_box';box.appendChild(waveSvg());
 const t=document.createElement('p');t.textContent=BD.t('ext_sound_playing');box.appendChild(t);
 const v=document.createElement('p');v.textContent=BD.t('ext_sound_valid').replace('{m}',m.minutes||'');box.appendChild(v);
 const b=document.createElement('button');b.type='button';b.textContent=BD.t('ext_sound_again');b.onclick=()=>play(lastCode);box.appendChild(b);
 BD.sheet(BD.t('ext_sound_invite'),box);play(lastCode)});
let inviteBtn=null;
BD.onBuild(()=>{const st=BD.state;if(inviteBtn||window.__extLinkSnap)return;if(BD.transport==='preview')return;
 inviteBtn=BD.button('ext_sound_inv',BD.t('ext_sound_invite'),ANT,()=>BD.send({t:'sound_token',minutes:120}))});

/* ---- the listener: no card yet - "Listen to join" ---- */
const HP=new URLSearchParams(location.hash.slice(1)),J=HP.get('j');
async function listen(){const box=document.createElement('div');box.className='ext_sound_box';box.appendChild(waveSvg());
 const t=document.createElement('p');t.textContent=BD.t('ext_sound_hint');box.appendChild(t);
 const meter=document.createElement('div');meter.className='ext_sound_meter';const bar=document.createElement('i');meter.appendChild(bar);box.appendChild(meter);
 BD.sheet(BD.t('ext_sound_listen'),box);
 let stream;try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}})}
 catch(e){t.textContent=BD.t('ext_sound_nomic');return}
 const ctx=new (window.AudioContext||window.webkitAudioContext)(),sr=ctx.sampleRate,src=ctx.createMediaStreamSource(stream);
 const proc=ctx.createScriptProcessor(4096,1,1),keep=Math.round(4*sr);let buf=new Float32Array(0),busy=false,done=false;
 proc.onaudioprocess=e=>{if(done)return;const inp=e.inputBuffer.getChannelData(0);let pk=0;for(let i=0;i<inp.length;i+=8)pk=Math.max(pk,Math.abs(inp[i]));
  bar.style.width=Math.min(100,pk*300)+'%';const nb=new Float32Array(Math.min(keep,buf.length+inp.length));
  const from=buf.length+inp.length-nb.length;nb.set(buf.subarray(Math.max(0,from)),0);nb.set(inp,nb.length-inp.length);buf=nb};
 src.connect(proc);proc.connect(ctx.destination);
 const scan=setInterval(()=>{if(busy||done||buf.length<sr*2)return;busy=true;try{const r=decodeSamples(buf,sr);const p=r&&parsePayload(r.payload);
   if(p){done=true;clearInterval(scan);t.textContent=BD.t('ext_sound_heard');stream.getTracks().forEach(x=>x.stop());try{proc.disconnect();ctx.close()}catch(x){}go(p)}}
  finally{busy=false}},400)}
function go(p){const lang=BD.lang();
 if(p.mode==='bridge'){const base=BD.transport==='bridge'?location.pathname:'https://biruni-gis.com/d/';
  const frag='k='+p.key+'&j='+p.token+'&l='+lang+'&p=2';
  const url=(BD.transport==='bridge'?location.origin:'')+base+'#'+frag;
  if(BD.transport==='bridge'){location.hash=frag;location.reload()}else location.href=url}
 else location.href='https://'+p.ip+':'+p.port+'/d/'+p.path+'/#j='+p.token+'&l='+lang}
const noCard=(typeof CARD==='undefined'||!CARD)&&!HP.get('s');
if(noCard&&!J&&BD.transport!=='preview'){const b=BD.button('ext_sound_lis',BD.t('ext_sound_listen'),ANT,()=>listen());b.classList.add('ext_sound_listen')}
/* ---- joining with a token heard: the first message is sound_join instead of hello ---- */
function keepCard(card){const k=(typeof KEYB!=='undefined'&&KEYB)?'&k='+KEYB:'';
 try{history.replaceState(null,'',location.pathname+location.search+'#c='+card+k+'&l='+BD.lang())}catch(e){}}
BD.on('sound_card',m=>{if(m&&m.card)keepCard(String(m.card))});
if(noCard&&J){const b=document.getElementById('banner');if(b)b.classList.add('hide');
 if(BD.transport==='bridge'&&typeof TR!=='undefined'){const orig=send;send=function(o){if(o&&o.t==='hello'&&!o.card)o={t:'sound_join',token:J,lang:BD.lang()};return orig(o)};
  TR.connect()}
 else if(BD.transport==='lan'){(async()=>{try{const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
   const r=await fetch(base+'m',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({s:null,m:{t:'sound_join',token:J,lang:BD.lang()}})});
   const j=await r.json();const c=(j.out||[]).find(x=>x.t==='sound_card');
   if(j.s)fetch(base+'bye',{method:'POST',body:JSON.stringify({s:j.s})}).catch(()=>{});
   if(c){keepCard(c.card);location.reload()}else BD.toast(BD.t('ext_sound_bad'))}catch(e){BD.toast(BD.t('ext_sound_bad'))}})()}}
"""
