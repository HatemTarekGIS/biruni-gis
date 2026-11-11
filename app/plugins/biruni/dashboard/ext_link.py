"""Extension: the dashboard in a link («اللوحة جوه رسالة»), see snapshot.py.

The page side opens a link "#s=<snapshot>": inflates it (DecompressionStream, or a small inflate here), checks
the ed25519 signature (WebCrypto Ed25519, or a small BigInt implementation here), and shows the dashboard
with no transport - "verified snapshot from <PC> at <time>" in green, or a red warning when the link was
changed. It also keeps window.BiruniEd (base64url, inflate, ed25519 verify, key fingerprint) for the other
extensions, and remembers the public keys of the PCs this browser has seen live.
"""
from ..i18n import tr

TEXTS = {
    "en": {"ext_link_snap": "Snapshot", "ext_link_ok": "Verified snapshot from {pc} — {t}",
           "ext_link_bad": "Warning: this snapshot was changed after it was signed — do not trust its numbers.",
           "ext_link_broken": "This snapshot link is damaged or incomplete.",
           "ext_link_key": "Key {k}", "ext_link_known": "the same computer you saw live",
           "ext_link_offline": "Opened from the link itself — the computer may be off.",
           "ext_link_noseal": "A seal is checked on the computer that made the snapshot (Check a Dashboard Seal)."},
    "ar": {"ext_link_snap": "لقطة", "ext_link_ok": "لقطة موثّقة من {pc} — {t}",
           "ext_link_bad": "تحذير: عُدّلت هذه اللقطة بعد ختمها — لا تعتمد على أرقامها.",
           "ext_link_broken": "رابط هذه اللقطة تالف أو ناقص.",
           "ext_link_key": "المفتاح {k}", "ext_link_known": "الحاسوب نفسه الذي رأيته مباشرة",
           "ext_link_offline": "فُتحت من الرابط نفسه — قد يكون الحاسوب مغلقًا.",
           "ext_link_noseal": "يُتحقَّق من الختم على الحاسوب الذي صنع اللقطة (التحقق من ختم لوحة)."},
    "es": {"ext_link_snap": "Instantánea", "ext_link_ok": "Instantánea verificada de {pc} — {t}",
           "ext_link_bad": "Atención: esta instantánea se modificó después de firmarse; no confíe en sus cifras.",
           "ext_link_broken": "El enlace de esta instantánea está dañado o incompleto.",
           "ext_link_key": "Clave {k}", "ext_link_known": "el mismo equipo que vio en directo",
           "ext_link_offline": "Abierta desde el propio enlace: el equipo puede estar apagado.",
           "ext_link_noseal": "Un sello se comprueba en el equipo que hizo la instantánea (Comprobar un sello)."},
    "fr": {"ext_link_snap": "Instantané", "ext_link_ok": "Instantané vérifié de {pc} — {t}",
           "ext_link_bad": "Attention : cet instantané a été modifié après sa signature — ne vous fiez pas à ses chiffres.",
           "ext_link_broken": "Le lien de cet instantané est endommagé ou incomplet.",
           "ext_link_key": "Clé {k}", "ext_link_known": "le même ordinateur que vous avez vu en direct",
           "ext_link_offline": "Ouvert depuis le lien lui-même — l'ordinateur peut être éteint.",
           "ext_link_noseal": "Un sceau se vérifie sur l'ordinateur qui a fait l'instantané (Vérifier un sceau)."},
    "pt_BR": {"ext_link_snap": "Instantâneo", "ext_link_ok": "Instantâneo verificado de {pc} — {t}",
              "ext_link_bad": "Atenção: este instantâneo foi alterado depois de assinado — não confie nos números.",
              "ext_link_broken": "O link deste instantâneo está danificado ou incompleto.",
              "ext_link_key": "Chave {k}", "ext_link_known": "o mesmo computador que você viu ao vivo",
              "ext_link_offline": "Aberto a partir do próprio link — o computador pode estar desligado.",
              "ext_link_noseal": "Um selo é verificado no computador que fez o instantâneo (Verificar um selo)."},
    "tr": {"ext_link_snap": "Anlık görüntü", "ext_link_ok": "{pc} kaynaklı doğrulanmış anlık görüntü — {t}",
           "ext_link_bad": "Uyarı: bu anlık görüntü imzalandıktan sonra değiştirildi — sayılarına güvenmeyin.",
           "ext_link_broken": "Bu anlık görüntü bağlantısı bozuk veya eksik.",
           "ext_link_key": "Anahtar {k}", "ext_link_known": "canlı gördüğünüz bilgisayarın aynısı",
           "ext_link_offline": "Bağlantının kendisinden açıldı — bilgisayar kapalı olabilir.",
           "ext_link_noseal": "Mühür, anlık görüntüyü yapan bilgisayarda doğrulanır (Pano mührünü doğrula)."},
    "fa": {"ext_link_snap": "نما", "ext_link_ok": "نمای تأییدشده از {pc} — {t}",
           "ext_link_bad": "هشدار: این نما پس از امضا تغییر کرده است — به عددهایش اعتماد نکنید.",
           "ext_link_broken": "پیوند این نما آسیب‌دیده یا ناقص است.",
           "ext_link_key": "کلید {k}", "ext_link_known": "همان رایانه‌ای که زنده دیدید",
           "ext_link_offline": "از خود پیوند باز شد — ممکن است رایانه خاموش باشد.",
           "ext_link_noseal": "مهر روی رایانه‌ای که نما را ساخته بررسی می‌شود (بررسی مهر داشبورد)."},
}

CSS = r"""
.ext_link_bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px clamp(10px,2vw,22px) 0;padding:9px 14px;
 border-radius:12px;font-size:13.5px;font-weight:600;border:1px solid transparent}
.ext_link_bar.hide{display:none}
.ext_link_bar.ok{background:color-mix(in srgb,#2e7d32 14%,var(--surface));border-color:color-mix(in srgb,#2e7d32 45%,transparent);color:var(--text)}
.ext_link_bar.bad{background:color-mix(in srgb,#c62828 18%,var(--surface));border-color:#c62828;color:var(--text)}
.ext_link_bar .ext_link_k{font-family:ui-monospace,Consolas,monospace;font-weight:700;letter-spacing:.04em;opacity:.85}
.ext_link_bar .ext_link_sub{font-weight:500;color:var(--muted)}
.conn.snap .dot{background:#8d6e63}
"""

HTML = '<div class="ext_link_bar hide" id="ext_link_bar" role="status"></div>'

JS = r"""
const BD=window.BiruniDash;
/* ---- shared helpers for the extensions: base64url, inflate, ed25519 verify, key fingerprint ---- */
const B64={dec(s){s=String(s).replace(/-/g,'+').replace(/_/g,'/');s+='==='.slice((s.length+3)%4);const b=atob(s),o=new Uint8Array(b.length);
  for(let i=0;i<b.length;i++)o[i]=b.charCodeAt(i);return o},
 enc(a){let s='';const u=new Uint8Array(a);for(let i=0;i<u.length;i+=8192)s+=String.fromCharCode.apply(null,u.subarray(i,i+8192));
  return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')}};
const fromHex=h=>{h=String(h||'');const o=new Uint8Array(h.length>>1);for(let i=0;i<o.length;i++)o[i]=parseInt(h.substr(i*2,2),16);return o};
const join=(...a)=>{const o=new Uint8Array(a.reduce((n,x)=>n+x.length,0));let i=0;for(const x of a){o.set(x,i);i+=x.length}return o};
/* raw inflate (RFC 1951), for browsers without DecompressionStream */
function inflateRaw(src){let pos=0,bit=0,bb=0,out=new Uint8Array(Math.max(1024,src.length*4)),n=0;
 const need=k=>{if(n+k>out.length){const o=new Uint8Array(Math.max(out.length*2,n+k));o.set(out.subarray(0,n));out=o}};
 const bits=k=>{let v=0;for(let i=0;i<k;i++){if(!bit){if(pos>=src.length)throw new Error('eof');bb=src[pos++];bit=8}v|=(bb&1)<<i;bb>>=1;bit--}return v};
 const huff=lens=>{const count=new Uint16Array(16),offs=new Uint16Array(16),sym=new Uint16Array(lens.length);
  for(const l of lens)count[l]++;count[0]=0;for(let i=1;i<16;i++)offs[i]=offs[i-1]+count[i-1];
  for(let s=0;s<lens.length;s++)if(lens[s])sym[offs[lens[s]]++]=s;return {count,sym}};
 const decode=h=>{let code=0,first=0,index=0;for(let len=1;len<16;len++){code|=bits(1);const c=h.count[len];
  if(code-c<first)return h.sym[index+(code-first)];index+=c;first+=c;first<<=1;code<<=1}throw new Error('bad code')};
 const LB=[3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258],
  LE=[0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0],
  DB=[1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577],
  DE=[0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13],
  ORD=[16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15];
 let fixL=null,fixD=null,last=0;
 while(!last){last=bits(1);const type=bits(2);
  if(type===0){bit=0;if(pos+4>src.length)throw new Error('eof');const len=src[pos]|(src[pos+1]<<8);pos+=4;need(len);
   out.set(src.subarray(pos,pos+len),n);n+=len;pos+=len;continue}
  let L,D;
  if(type===1){if(!fixL){const l=new Uint8Array(288);l.fill(8,0,144);l.fill(9,144,256);l.fill(7,256,280);l.fill(8,280,288);
    fixL=huff(l);fixD=huff(new Uint8Array(30).fill(5))}L=fixL;D=fixD}
  else if(type===2){const hl=bits(5)+257,hd=bits(5)+1,hc=bits(4)+4,cl=new Uint8Array(19);for(let i=0;i<hc;i++)cl[ORD[i]]=bits(3);
   const ch=huff(cl),lens=new Uint8Array(hl+hd);let i=0;
   while(i<hl+hd){const s=decode(ch);if(s<16)lens[i++]=s;else{let r=0,v=0;if(s===16){if(!i)throw new Error('bad');v=lens[i-1];r=3+bits(2)}
     else if(s===17)r=3+bits(3);else r=11+bits(7);if(i+r>hl+hd)throw new Error('bad');lens.fill(v,i,i+r);i+=r}}
   L=huff(lens.subarray(0,hl));D=huff(lens.subarray(hl))}
  else throw new Error('bad block');
  for(;;){const s=decode(L);if(s<256){need(1);out[n++]=s}else if(s===256)break;else{const k=s-257;if(k>=29)throw new Error('bad');
    const len=LB[k]+bits(LE[k]),ds=decode(D);if(ds>=30)throw new Error('bad');const dist=DB[ds]+bits(DE[ds]);if(dist>n)throw new Error('bad');
    need(len);for(let j=0;j<len;j++){out[n]=out[n-dist];n++}}}}
 return out.subarray(0,n)}
async function inflate(z){if(typeof DecompressionStream!=='undefined'&&!window.__noDS){
  try{const st=new Blob([z]).stream().pipeThrough(new DecompressionStream('deflate-raw'));return new Uint8Array(await new Response(st).arrayBuffer())}
  catch(e){throw new Error('inflate')}}
 return inflateRaw(z)}
/* ed25519 verify: WebCrypto where the browser has it, else BigInt arithmetic (RFC 8032, cofactorless) */
const EP=(1n<<255n)-19n,EL=(1n<<252n)+27742317777372353535851937790883648493n;
const em=a=>{const r=a%EP;return r<0n?r+EP:r};
function epw(b,e){let r=1n;b=em(b);while(e>0n){if(e&1n)r=r*b%EP;b=b*b%EP;e>>=1n}return r}
const einv=a=>epw(a,EP-2n),ED=em(-121665n*einv(121666n)),EI=epw(2n,(EP-1n)/4n);
function eadd(P,Q){const A=em((P[1]-P[0])*(Q[1]-Q[0])),B=em((P[1]+P[0])*(Q[1]+Q[0])),C=em(2n*P[3]*Q[3]*ED),D=em(2n*P[2]*Q[2]);
 const E=B-A,F=D-C,G=D+C,H=B+A;return [em(E*F),em(G*H),em(F*G),em(E*H)]}
function emul(P,k){let R=[0n,1n,1n,0n];while(k>0n){if(k&1n)R=eadd(R,P);P=eadd(P,P);k>>=1n}return R}
const le=b=>{let v=0n;for(let i=b.length-1;i>=0;i--)v=(v<<8n)|BigInt(b[i]);return v};
function eenc(P){const zi=einv(P[2]),x=em(P[0]*zi);let y=em(P[1]*zi);const b=new Uint8Array(32);
 for(let i=0;i<32;i++){b[i]=Number(y&255n);y>>=8n}if(x&1n)b[31]|=128;return b}
function edec(b0){const b=Uint8Array.from(b0),sg=b[31]>>7;b[31]&=127;const y=le(b);if(y>=EP)return null;
 const u=em(y*y-1n),v=em(ED*y*y+1n);let x=em(u*epw(v,3n)*epw(em(u*epw(v,7n)),(EP-5n)/8n));const vx=em(v*x*x);
 if(vx!==u){if(vx===em(-u))x=em(x*EI);else return null}if(x===0n&&sg)return null;if(Number(x&1n)!==sg)x=em(-x);return [x,y,1n,em(x*y)]}
let EB=null;
async function edVerify(pub,sig,msg){pub=Uint8Array.from(pub);sig=Uint8Array.from(sig);msg=Uint8Array.from(msg);
 if(pub.length!==32||sig.length!==64)return false;
 if(!window.__noWebEd){try{const k=await crypto.subtle.importKey('raw',pub,{name:'Ed25519'},false,['verify']);
   return await crypto.subtle.verify({name:'Ed25519'},k,sig,msg)}catch(e){}}
 if(!EB){const b=new Uint8Array(32).fill(0x66);b[0]=0x58;EB=edec(b)}
 const A=edec(pub),R=edec(sig.subarray(0,32));if(!A||!R)return false;const S=le(sig.subarray(32));if(S>=EL)return false;
 const h=new Uint8Array(await crypto.subtle.digest('SHA-512',join(sig.subarray(0,32),pub,msg)));const k=le(h)%EL;
 const l=eenc(emul(EB,S)),r=eenc(eadd(R,emul(A,k)));for(let i=0;i<32;i++)if(l[i]!==r[i])return false;return true}
async function keyPrint(pubHex){const h=new Uint8Array(await crypto.subtle.digest('SHA-256',fromHex(pubHex)));
 const A='0123456789ABCDEFGHJKMNPQRSTVWXYZ';let n=0n;for(let i=0;i<5;i++)n=(n<<8n)|BigInt(h[i]);let s='';
 for(let i=0;i<8;i++)s+=A[Number((n>>BigInt(35-5*i))&31n)];return s.slice(0,4)+'-'+s.slice(4)}
window.BiruniEd={b64:B64,fromHex:fromHex,join:join,inflate:inflate,inflateRaw:inflateRaw,verify:edVerify,keyPrint:keyPrint};

/* ---- the keys of the PCs seen live (to say "the same computer" on a snapshot) ---- */
const KEYS='biruni-dash-keys';
function known(){try{return JSON.parse(localStorage.getItem(KEYS)||'{}')||{}}catch(e){return {}}}
const HP=new URLSearchParams(location.hash.slice(1)),SNAP=HP.get('s');
BD.onBuild(()=>{if(window.__extLinkSnap)return;const st=BD.state;if(!st.pub||!/^[0-9a-f]{64}$/.test(st.pub))return;
 try{const k=known();k[st.pub]=String(st.pc||'').slice(0,80);localStorage.setItem(KEYS,JSON.stringify(k))}catch(e){}});

/* ---- opening a snapshot link ---- */
function fill(s,o){return String(s).replace(/\{(\w+)\}/g,(m,k)=>o[k]!=null?o[k]:m)}
function bar(cls,parts){const b=document.getElementById('ext_link_bar');b.className='ext_link_bar '+cls;b.textContent='';
 for(const [c,t] of parts){const s=document.createElement('span');if(c)s.className=c;s.textContent=t;b.appendChild(s)}}
function when(ts){try{const d=new Date(ts*1000);return d.toLocaleString(BD.lang().replace('_','-'),{dateStyle:'medium',timeStyle:'short'})}catch(e){return new Date(ts*1000).toISOString()}}
async function openSnapshot(){window.__extLinkSnap=true;const st=BD.state;let payload=null,ok=false;
 try{const [a,b]=SNAP.split('.');const z=B64.dec(a),sig=B64.dec(b||'');const raw=await inflate(z);
  payload=JSON.parse(new TextDecoder().decode(raw));ok=await edVerify(fromHex(payload.pub),sig,z)}
 catch(e){payload=null}
 window.__extLinkResult={ok:ok,opened:!!payload};
 if(!payload||!payload.dash||!payload.dash.def){bar('bad',[[null,BD.t('ext_link_broken')]]);
  const w=document.getElementById('wait');if(w)w.classList.add('hide');return}
 BD.feed(Object.assign({t:'dash'},payload.dash));
 BD.feed({t:'data',rev:1,ts:payload.ts,widgets:payload.data||{}});
 st.lastData=(+payload.ts||0)*1000;
 const c=document.getElementById('conn');if(c)c.className='conn snap';const ct=document.getElementById('connText');if(ct)ct.textContent=BD.t('ext_link_snap');
 const f=document.getElementById('fresh');if(f)f.style.display='none';
 const kp=await keyPrint(payload.pub||'');const k=known();
 if(ok)bar('ok',[[null,'✓ '+fill(BD.t('ext_link_ok'),{pc:payload.pc||payload.dash.pc||'—',t:when(+payload.ts||0)})],
  ['ext_link_k',fill(BD.t('ext_link_key'),{k:kp})],['ext_link_sub',k[payload.pub]?'· '+BD.t('ext_link_known'):'· '+BD.t('ext_link_offline')]]);
 else bar('bad',[[null,'⚠ '+BD.t('ext_link_bad')],['ext_link_k',fill(BD.t('ext_link_key'),{k:kp})]]);
 window.__extLinkResult.shown=true}
/* in a snapshot there is no computer to ask: seals and "what is behind it" say so instead of waiting */
if(SNAP){const orig=send;send=function(o){if(o&&(o.t==='seal'||o.t==='explain'||o.t==='basemap'||o.t==='filter'||o.t==='hello')){
  if(o.t==='seal'||o.t==='explain'){setTimeout(()=>{BD.close();BD.toast(BD.t('ext_link_noseal'))},50)}return}return orig(o)};
 openSnapshot()}
"""


# ---- the dashboard panel's action ------------------------------------------------------------------------------------
def _dialog(plugin, dock):
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QGuiApplication
    from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                                     QPushButton, QVBoxLayout)

    from . import snapshot
    from .dock import card_text, qr_pixmap

    payload = dock.selected_card() if hasattr(dock, "selected_card") else None
    if not payload:
        QMessageBox.information(dock, tr("Send as a snapshot link"),
                                tr("Choose a card in the list first: the snapshot shows what that card may see."))
        return None
    try:
        lang = getattr(getattr(dock, "sharing", None), "lang", None) or "en"
        res = snapshot.build(dock.service(), card_text(payload), lang=lang)
    except snapshot.SnapshotError as e:
        QMessageBox.warning(dock, tr("Send as a snapshot link"), tr("The snapshot could not be made: {why}").format(why=e.why))
        return None
    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Send as a snapshot link"))
    lay = QVBoxLayout(dlg)
    kb = res["size"] / 1024.0
    msg = tr("The whole dashboard, as the card «{name}» sees it now, is inside this link ({kb:.0f} KB). It opens "
             "with this computer off; any change to it is detected.").format(name=payload.get("name", ""), kb=kb)
    info = QLabel(msg)
    info.setWordWrap(True)
    lay.addWidget(info)
    if res["steps"]:
        lab = QLabel(tr("Made smaller to fit: {steps}").format(steps=", ".join(_step_label(s) for s in res["steps"])))
        lab.setWordWrap(True)
        lay.addWidget(lab)
    if res["warn"]:
        w = QLabel(tr("The link is long: some chat apps cut long messages. Send it as a file or by e-mail if it "
                      "does not open."))
        w.setWordWrap(True)
        w.setStyleSheet("color:#b26a00")
        lay.addWidget(w)
    lay.addWidget(QLabel(tr("Key fingerprint: {k}").format(k=res["fingerprint"])))
    box = QPlainTextEdit(res["link"])
    box.setReadOnly(True)
    box.setMaximumHeight(90)
    lay.addWidget(box)
    if res["qr"]:
        q = QLabel()
        q.setPixmap(qr_pixmap(res["link"], 3))
        q.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(q)
    row = QHBoxLayout()
    copy = QPushButton(tr("Copy the link"))
    copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(res["link"]))
    row.addWidget(copy)
    row.addStretch(1)
    lay.addLayout(row)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.result_info = res
    dlg.show()
    return dlg


def _step_label(step):
    return {"round6": tr("coordinates rounded"), "round5": tr("coordinates rounded"), "rows": tr("long lists shortened"),
            "simplify": tr("map shapes simplified"), "points": tr("map points left out"),
            "maps": tr("maps left out")}.get(step, step)


ACTIONS = [("snapshot_link", "Send as a snapshot link", "link", _dialog)]
