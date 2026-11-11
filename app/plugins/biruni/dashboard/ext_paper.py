"""The live paper on the phone («اللوحة على الورق») - a dashboard extension (SPEC.md, Extensions).

A header button opens a full-screen camera view. The page finds the paper's square markers in every frame
(adaptive threshold, connected components, convex hull to a quadrilateral, 6x6 decoding against the marker
dictionary of paper.py, edges refined to a fraction of a pixel along the image gradient), works out the page's
homography from all markers it sees (least squares, the worst marker dropped when it disagrees), smooths it with
a one-euro filter and draws the dashboard's live map exactly over the printed parcels: their live colours, a glow
when one changes, the cross-filter of the dashboard, parcel numbers upright; a tap on a parcel of the paper shows
its details. The detection runs in a Web Worker made from a blob, so the picture never waits for it.

Without a camera (or in the claude.ai sandbox) the same view runs a demo: the paper itself is drawn on a desk, held
by a moving, slightly shaking hand (a real perspective camera), and the SAME tracker locks onto it frame by frame -
the difference to the true position is shown, which proves the whole path.

The PC answers "paper_list" with the passports of this dashboard's papers (paper.py), signed by its key; the paper
itself only carries markers, so a paper made elsewhere can never send the phone to another place.
"""
import json

from . import paper as _paper

try:  # the PC side; the page alone (tests of the page) does not need it
    from .service import handler
except Exception:  # noqa: BLE001
    handler = None


if handler is not None:
    @handler("paper_list")
    def _paper_list(service, session, msg):
        d = service._def_of(session) or {}
        papers = _paper.papers_for(d.get("id"), service.project)
        card = session.card or {}
        allowed = card.get("widgets")
        if allowed not in (None, "*"):
            papers = [p for p in papers if p.get("widget") in allowed]
        session._out({"t": "paper_list", "papers": papers[:20]})


ACTIONS = [("paper_print", "Print a live paper", "scan", lambda plugin, dock: _paper.print_ui(plugin, dock))]

TEXTS = {
    "en": {"paper_btn": "Live paper", "paper_title": "Live paper",
           "paper_search": "Point the camera at a live paper printed from Biruni",
           "paper_locked": "Locked on the paper", "paper_lost": "Looking for the paper…", "paper_demo": "Demo",
           "paper_camera": "Camera", "paper_close": "Close",
           "paper_nocam": "No camera here — a demo with a simulated paper on a desk",
           "paper_markers": "markers", "paper_err": "match error", "paper_px": "px", "paper_fps": "fps",
           "paper_verified": "Paper verified — signed by this dashboard's computer",
           "paper_unverified": "This paper was not signed by this dashboard's computer",
           "paper_tip": "Tap a parcel on the paper to see its details",
           "paper_demo_note": "Simulated camera: the same tracker locks onto the printed markers",
           "paper_noprint": "No paper printed yet — the demo uses a paper made from this map",
           "paper_kind": "A live paper from Biruni GIS",
           "paper_hint": "Point a phone camera at this paper to see it live", "paper_check": "Check code"},
    "ar": {"paper_btn": "الورقة الحيّة", "paper_title": "الورقة الحيّة",
           "paper_search": "وجّه الكاميرا إلى ورقة حيّة مطبوعة من بيروني",
           "paper_locked": "مثبَّتة على الورقة", "paper_lost": "جارٍ البحث عن الورقة…", "paper_demo": "عرض توضيحي",
           "paper_camera": "الكاميرا", "paper_close": "إغلاق",
           "paper_nocam": "لا كاميرا هنا — عرض توضيحي بورقة محاكاة على مكتب",
           "paper_markers": "علامات", "paper_err": "خطأ التطابق", "paper_px": "بكسل", "paper_fps": "إطار/ث",
           "paper_verified": "ورقة موثّقة — موقّعة من حاسوب هذه اللوحة",
           "paper_unverified": "هذه الورقة غير موقّعة من حاسوب هذه اللوحة",
           "paper_tip": "اضغط على قطعة في الورقة لترى تفاصيلها",
           "paper_demo_note": "كاميرا محاكاة: المتتبّع نفسه يثبت على العلامات المطبوعة",
           "paper_noprint": "لم تُطبع ورقة بعد — يستخدم العرض ورقة من هذه الخريطة",
           "paper_kind": "ورقة حيّة من بيروني",
           "paper_hint": "وجّه كاميرا الهاتف إلى هذه الورقة لتراها حيّة", "paper_check": "رمز التحقق"},
    "es": {"paper_btn": "Papel vivo", "paper_title": "Papel vivo",
           "paper_search": "Apunte la cámara a un papel vivo impreso desde Biruni",
           "paper_locked": "Fijado sobre el papel", "paper_lost": "Buscando el papel…", "paper_demo": "Demostración",
           "paper_camera": "Cámara", "paper_close": "Cerrar",
           "paper_nocam": "Sin cámara aquí: una demostración con un papel simulado sobre una mesa",
           "paper_markers": "marcadores", "paper_err": "error de ajuste", "paper_px": "px", "paper_fps": "fps",
           "paper_verified": "Papel verificado: firmado por el equipo de este panel",
           "paper_unverified": "Este papel no está firmado por el equipo de este panel",
           "paper_tip": "Toque una parcela del papel para ver sus datos",
           "paper_demo_note": "Cámara simulada: el mismo seguidor se fija en los marcadores impresos",
           "paper_noprint": "Aún no se imprimió ningún papel: la demostración usa uno hecho con este mapa",
           "paper_kind": "Un papel vivo de Biruni GIS",
           "paper_hint": "Apunte la cámara de un teléfono a este papel para verlo en vivo",
           "paper_check": "Código de verificación"},
    "fr": {"paper_btn": "Papier vivant", "paper_title": "Papier vivant",
           "paper_search": "Pointez la caméra vers un papier vivant imprimé depuis Biruni",
           "paper_locked": "Accroché au papier", "paper_lost": "Recherche du papier…", "paper_demo": "Démo",
           "paper_camera": "Caméra", "paper_close": "Fermer",
           "paper_nocam": "Pas de caméra ici : une démo avec un papier simulé sur un bureau",
           "paper_markers": "repères", "paper_err": "écart", "paper_px": "px", "paper_fps": "i/s",
           "paper_verified": "Papier vérifié : signé par l'ordinateur de ce tableau de bord",
           "paper_unverified": "Ce papier n'est pas signé par l'ordinateur de ce tableau de bord",
           "paper_tip": "Touchez une parcelle du papier pour voir ses détails",
           "paper_demo_note": "Caméra simulée : le même suivi s'accroche aux repères imprimés",
           "paper_noprint": "Aucun papier imprimé : la démo utilise un papier fait de cette carte",
           "paper_kind": "Un papier vivant de Biruni GIS",
           "paper_hint": "Pointez la caméra d'un téléphone vers ce papier pour le voir vivant",
           "paper_check": "Code de vérification"},
    "pt_BR": {"paper_btn": "Papel vivo", "paper_title": "Papel vivo",
              "paper_search": "Aponte a câmera para um papel vivo impresso pelo Biruni",
              "paper_locked": "Travado no papel", "paper_lost": "Procurando o papel…", "paper_demo": "Demonstração",
              "paper_camera": "Câmera", "paper_close": "Fechar",
              "paper_nocam": "Sem câmera aqui: uma demonstração com um papel simulado sobre a mesa",
              "paper_markers": "marcadores", "paper_err": "erro de ajuste", "paper_px": "px", "paper_fps": "qps",
              "paper_verified": "Papel verificado: assinado pelo computador deste painel",
              "paper_unverified": "Este papel não foi assinado pelo computador deste painel",
              "paper_tip": "Toque num lote do papel para ver os detalhes",
              "paper_demo_note": "Câmera simulada: o mesmo rastreador trava nos marcadores impressos",
              "paper_noprint": "Nenhum papel impresso ainda: a demonstração usa um feito com este mapa",
              "paper_kind": "Um papel vivo do Biruni GIS",
              "paper_hint": "Aponte a câmera de um celular para este papel para vê-lo ao vivo",
              "paper_check": "Código de verificação"},
    "tr": {"paper_btn": "Canlı kâğıt", "paper_title": "Canlı kâğıt",
           "paper_search": "Kamerayı Biruni'den basılmış canlı bir kâğıda tutun",
           "paper_locked": "Kâğıda kilitlendi", "paper_lost": "Kâğıt aranıyor…", "paper_demo": "Tanıtım",
           "paper_camera": "Kamera", "paper_close": "Kapat",
           "paper_nocam": "Burada kamera yok: masada benzetilmiş bir kâğıtla tanıtım",
           "paper_markers": "işaret", "paper_err": "eşleşme hatası", "paper_px": "px", "paper_fps": "kare/sn",
           "paper_verified": "Kâğıt doğrulandı: bu panonun bilgisayarınca imzalı",
           "paper_unverified": "Bu kâğıt bu panonun bilgisayarınca imzalanmamış",
           "paper_tip": "Ayrıntılar için kâğıttaki bir parsele dokunun",
           "paper_demo_note": "Benzetilmiş kamera: aynı izleyici basılı işaretlere kilitlenir",
           "paper_noprint": "Henüz kâğıt basılmadı: tanıtım bu haritadan yapılmış bir kâğıt kullanır",
           "paper_kind": "Biruni GIS'ten canlı bir kâğıt",
           "paper_hint": "Canlı görmek için telefon kamerasını bu kâğıda tutun", "paper_check": "Doğrulama kodu"},
    "fa": {"paper_btn": "کاغذ زنده", "paper_title": "کاغذ زنده",
           "paper_search": "دوربین را به سوی یک کاغذ زندهٔ چاپ‌شده از Biruni بگیرید",
           "paper_locked": "روی کاغذ قفل شد", "paper_lost": "در جست‌وجوی کاغذ…", "paper_demo": "نمایش آزمایشی",
           "paper_camera": "دوربین", "paper_close": "بستن",
           "paper_nocam": "اینجا دوربین نیست — نمایشی با کاغذ شبیه‌سازی‌شده روی میز",
           "paper_markers": "نشانه", "paper_err": "خطای انطباق", "paper_px": "پیکسل", "paper_fps": "فریم/ث",
           "paper_verified": "کاغذ تأیید شد — امضاشده با رایانهٔ این داشبورد",
           "paper_unverified": "این کاغذ با رایانهٔ این داشبورد امضا نشده است",
           "paper_tip": "برای دیدن جزئیات، روی قطعه‌ای در کاغذ بزنید",
           "paper_demo_note": "دوربین شبیه‌سازی‌شده: همان ردیاب روی نشانه‌های چاپی قفل می‌شود",
           "paper_noprint": "هنوز کاغذی چاپ نشده — نمایش از کاغذی ساخته‌شده از این نقشه استفاده می‌کند",
           "paper_kind": "کاغذ زنده‌ای از Biruni GIS",
           "paper_hint": "دوربین تلفن را به این کاغذ بگیرید تا آن را زنده ببینید", "paper_check": "کد بررسی"},
}

# [stabilised view, 11-08] the steady paper, augmented reality, hold
_MORE = {
    "en": {"paper_mode_stab": "Steady paper", "paper_mode_ar": "Augmented reality", "paper_freeze": "Hold",
           "paper_resume": "Resume", "paper_held": "Held — the numbers keep updating",
           "paper_zoom_tip": "Pinch to zoom the paper; double tap to fit"},
    "ar": {"paper_mode_stab": "الورقة المثبّتة", "paper_mode_ar": "الواقع المعزّز", "paper_freeze": "ثبّت",
           "paper_resume": "استئناف", "paper_held": "الصورة مثبّتة — والأرقام تتحدّث",
           "paper_zoom_tip": "قرّب بإصبعين لتكبير الورقة، واضغط مرتين للعودة"},
    "es": {"paper_mode_stab": "Papel estable", "paper_mode_ar": "Realidad aumentada", "paper_freeze": "Fijar",
           "paper_resume": "Reanudar", "paper_held": "Imagen fija: los números siguen actualizándose",
           "paper_zoom_tip": "Pellizque para ampliar el papel; doble toque para ajustar"},
    "fr": {"paper_mode_stab": "Papier stable", "paper_mode_ar": "Réalité augmentée", "paper_freeze": "Figer",
           "paper_resume": "Reprendre", "paper_held": "Image figée — les chiffres restent à jour",
           "paper_zoom_tip": "Pincez pour zoomer sur le papier ; double-touchez pour l'ajuster"},
    "pt_BR": {"paper_mode_stab": "Papel estável", "paper_mode_ar": "Realidade aumentada", "paper_freeze": "Fixar",
              "paper_resume": "Retomar", "paper_held": "Imagem fixa — os números continuam atualizando",
              "paper_zoom_tip": "Use dois dedos para ampliar o papel; toque duas vezes para ajustar"},
    "tr": {"paper_mode_stab": "Sabit kâğıt", "paper_mode_ar": "Artırılmış gerçeklik", "paper_freeze": "Sabitle",
           "paper_resume": "Devam", "paper_held": "Görüntü sabit — sayılar güncellenmeye devam ediyor",
           "paper_zoom_tip": "Kâğıdı büyütmek için iki parmakla yakınlaştırın; sığdırmak için çift dokunun"},
    "fa": {"paper_mode_stab": "کاغذ ثابت", "paper_mode_ar": "واقعیت افزوده", "paper_freeze": "ثابت کن",
           "paper_resume": "ادامه", "paper_held": "تصویر ثابت است — اعداد همچنان به‌روز می‌شوند",
           "paper_zoom_tip": "برای بزرگ‌نمایی کاغذ با دو انگشت بکشید؛ برای اندازهٔ کامل دو بار بزنید"},
}
for _l, _add in _MORE.items():
    TEXTS[_l].update(_add)

CSS = r"""
.paperView{position:fixed;inset:0;z-index:9000;background:#05080b;color:#fff;font-family:var(--font);overflow:hidden;
 touch-action:none;user-select:none;-webkit-user-select:none}
.paperView.hide{display:none}
.paperView canvas.paperCv{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:block}
.paperTop{position:absolute;inset-inline:0;top:0;display:flex;align-items:flex-start;gap:12px;padding:12px 14px 26px;
 background:linear-gradient(#05080be6,#05080b00);pointer-events:none}
.paperTop>*{pointer-events:auto}
.paperId{display:flex;flex-direction:column;gap:2px;min-width:0;flex:1}
.paperId b{font-size:16px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;unicode-bidi:plaintext}
.paperId span{font-size:12px;color:#9fd9d1;display:flex;align-items:center;gap:6px}
.paperId span i{width:8px;height:8px;border-radius:50%;background:#FBBF24;box-shadow:0 0 0 0 #FBBF2499;animation:paperPing 1.6s infinite}
.paperView.locked .paperId span i{background:#34d399;box-shadow:0 0 0 0 #34d39999}
@keyframes paperPing{0%{box-shadow:0 0 0 0 currentColor}70%{box-shadow:0 0 0 9px #0000}100%{box-shadow:0 0 0 0 #0000}}
.paperKpis{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.paperKpi{background:#0b1a1fcc;border:1px solid #ffffff22;border-radius:12px;padding:6px 10px;min-width:84px;
 backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
.paperKpi small{display:block;font-size:10.5px;color:#a9c1c7;white-space:nowrap;max-width:140px;overflow:hidden;text-overflow:ellipsis}
.paperKpi b{font-size:19px;font-weight:750;font-variant-numeric:tabular-nums;color:#fff}
.paperBtn{background:#0b1a1fcc;border:1px solid #ffffff2a;color:#fff;border-radius:999px;padding:8px 14px;font:inherit;
 font-size:13px;cursor:pointer;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
.paperBtn.on{background:#FBBF24;color:#1d1300;border-color:#FBBF24;font-weight:700}
.paperBtn.x{width:38px;height:38px;padding:0;font-size:20px;line-height:36px;text-align:center}
.paperBottom{position:absolute;inset-inline:0;bottom:0;display:flex;align-items:flex-end;justify-content:space-between;gap:10px;
 padding:28px 14px 14px;background:linear-gradient(#05080b00,#05080bd9);pointer-events:none}
.paperBottom>*{pointer-events:auto}
.paperLegend{display:flex;flex-wrap:wrap;gap:6px 12px;font-size:12px;color:#e8f2f4;max-width:60%}
.paperLegend span{display:inline-flex;align-items:center;gap:6px}
.paperLegend i{width:12px;height:12px;border-radius:3px;display:inline-block;box-shadow:0 0 0 1px #0006 inset}
.paperTools{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.paperMsg{position:absolute;left:50%;bottom:86px;transform:translateX(-50%);background:#0b1a1fe0;border:1px solid #ffffff22;
 border-radius:12px;padding:8px 14px;font-size:13px;max-width:min(92vw,560px);text-align:center;transition:opacity .4s}
.paperMsg.hide{opacity:0;pointer-events:none}
.paperTech{position:absolute;inset-inline-start:14px;bottom:70px;font:11px/1.45 ui-monospace,Consolas,monospace;color:#7ff3df;
 background:#04110fbf;border:1px solid #2dd4bf44;border-radius:8px;padding:6px 9px;direction:ltr;text-align:left;pointer-events:none}
.paperPop{position:absolute;min-width:190px;max-width:260px;background:#0d1c22f2;border:1px solid #ffffff2e;border-radius:14px;
 padding:10px 12px 12px;box-shadow:0 14px 40px #000a;font-size:12.5px;transform:translate(-50%,calc(-100% - 14px))}
.paperPop.hide{display:none}
.paperPop h4{margin:0 0 6px;font-size:14.5px;display:flex;align-items:center;gap:8px;unicode-bidi:plaintext}
.paperPop h4 i{width:12px;height:12px;border-radius:3px;flex:none}
.paperPop dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:3px 10px}
.paperPop dt{color:#9fb3ba}.paperPop dd{margin:0;font-weight:600;unicode-bidi:plaintext}
.paperPop:after{content:"";position:absolute;left:50%;bottom:-7px;width:12px;height:12px;background:#0d1c22;border-right:1px solid #ffffff2e;
 border-bottom:1px solid #ffffff2e;transform:translateX(-50%) rotate(45deg)}
@media (max-width:640px){.paperKpi{min-width:70px;padding:5px 8px}.paperKpi b{font-size:16px}.paperLegend{max-width:100%}
 .paperBottom{flex-direction:column;align-items:stretch}.paperTools{justify-content:center}.paperTech{bottom:118px}}
"""

HTML = r"""<div class="paperView hide" id="paperView" role="dialog" aria-modal="true" aria-labelledby="paperName">
 <canvas class="paperCv" id="paperCv"></canvas>
 <div class="paperTop"><button class="paperBtn x" id="paperClose" type="button">×</button>
  <div class="paperId"><b id="paperName"></b><span><i></i><em id="paperState"></em></span></div>
  <div class="paperKpis" id="paperKpis"></div></div>
 <div class="paperTech" id="paperTech"></div>
 <div class="paperMsg hide" id="paperMsg"></div>
 <div class="paperPop hide" id="paperPop"></div>
 <div class="paperBottom"><div class="paperLegend" id="paperLegend"></div>
  <div class="paperTools"><button class="paperBtn" id="paperModeBtn" type="button"></button>
   <button class="paperBtn" id="paperFreezeBtn" type="button"></button>
   <button class="paperBtn" id="paperCamBtn" type="button"></button>
   <button class="paperBtn" id="paperDemoBtn" type="button"></button></div></div>
</div>"""

_JS = r"""
const BD=window.BiruniDash;if(!BD)return;
const DICT=__DICT__,FIXED=__FIXED__,RE=6378137;
const $=id=>document.getElementById(id);
const T=k=>BD.t(k);
/* ---------------------------------------------------------------- small linear algebra */
function mul(A,B){const C=new Array(9);for(let r=0;r<3;r++)for(let c=0;c<3;c++)C[r*3+c]=A[r*3]*B[c]+A[r*3+1]*B[3+c]+A[r*3+2]*B[6+c];return C}
function app(H,x,y){const w=H[6]*x+H[7]*y+H[8];return [(H[0]*x+H[1]*y+H[2])/w,(H[3]*x+H[4]*y+H[5])/w]}
function inv(H){const [a,b,c,d,e,f,g,h,i]=H,A=e*i-f*h,B=-(d*i-f*g),C=d*h-e*g,det=a*A+b*B+c*C;if(Math.abs(det)<1e-15)return null;
 return [A/det,-(b*i-c*h)/det,(b*f-c*e)/det,B/det,(a*i-c*g)/det,-(a*f-c*d)/det,C/det,-(a*h-b*g)/det,(a*e-b*d)/det]}
function solve(A,b){const n=b.length;for(let i=0;i<n;i++){let p=i;for(let r=i+1;r<n;r++)if(Math.abs(A[r][i])>Math.abs(A[p][i]))p=r;
  if(Math.abs(A[p][i])<1e-14)return null;let t=A[i];A[i]=A[p];A[p]=t;t=b[i];b[i]=b[p];b[p]=t;
  for(let r=i+1;r<n;r++){const f=A[r][i]/A[i][i];if(!f)continue;for(let c=i;c<n;c++)A[r][c]-=f*A[i][c];b[r]-=f*b[i]}}
 const x=new Array(n);for(let i=n-1;i>=0;i--){let s=b[i];for(let c=i+1;c<n;c++)s-=A[i][c]*x[c];x[i]=s/A[i][i]}return x}
function nrm(P){let mx=0,my=0;for(const p of P){mx+=p[0];my+=p[1]}mx/=P.length;my/=P.length;let d=0;for(const p of P)d+=Math.hypot(p[0]-mx,p[1]-my);
 const s=Math.SQRT2/((d/P.length)||1);return [s,-s*mx,-s*my]}
/* least squares homography src -> dst (normalised DLT, h33 = 1), at least 4 pairs */
function homog(src,dst){if(src.length<4)return null;const ns=nrm(src),nd=nrm(dst),A=[],b=[];
 for(let i=0;i<8;i++){A.push([0,0,0,0,0,0,0,0]);b.push(0)}
 for(let i=0;i<src.length;i++){const x=src[i][0]*ns[0]+ns[1],y=src[i][1]*ns[0]+ns[2],u=dst[i][0]*nd[0]+nd[1],v=dst[i][1]*nd[0]+nd[2];
  const r1=[x,y,1,0,0,0,-u*x,-u*y],r2=[0,0,0,x,y,1,-v*x,-v*y];
  for(let a=0;a<8;a++){const p=r1[a],q=r2[a];if(p===0&&q===0)continue;const Ra=A[a];for(let c=0;c<8;c++)Ra[c]+=p*r1[c]+q*r2[c];b[a]+=p*u+q*v}}
 const h=solve(A,b);if(!h)return null;
 const H=mul([1/nd[0],0,-nd[1]/nd[0],0,1/nd[0],-nd[2]/nd[0],0,0,1],mul([h[0],h[1],h[2],h[3],h[4],h[5],h[6],h[7],1],[ns[0],0,ns[1],0,ns[0],ns[2],0,0,1]));
 const k=H[8];return H.map(v=>v/k)}

/* ---------------------------------------------------------------- the marker detector (also runs in a worker) */
function detectorFactory(){
 function sq2quad(q){const x0=q[0][0],y0=q[0][1],x1=q[1][0],y1=q[1][1],x2=q[2][0],y2=q[2][1],x3=q[3][0],y3=q[3][1];
  const dx1=x1-x2,dx2=x3-x2,dy1=y1-y2,dy2=y3-y2,sx=x0-x1+x2-x3,sy=y0-y1+y2-y3;let g=0,h=0;
  if(Math.abs(sx)>1e-9||Math.abs(sy)>1e-9){const den=dx1*dy2-dx2*dy1;if(Math.abs(den)<1e-12)return null;g=(sx*dy2-dx2*sy)/den;h=(dx1*sy-sx*dy1)/den}
  const a=x1-x0+g*x1,b=x3-x0+h*x3,c=x0,d=y1-y0+g*y1,e=y3-y0+h*y3,f=y0;
  return (u,v)=>{const w=g*u+h*v+1;return [(a*u+b*v+c)/w,(d*u+e*v+f)/w]}}
 function pop(x){x=x-((x>>1)&0x5555);x=(x&0x3333)+((x>>2)&0x3333);x=(x+(x>>4))&0x0f0f;return (x+(x>>8))&0x1f}
 function detect(px,W,H,dict){const r=detectAt(px,W,H,dict,W>720?2:1);if(r.length<2&&W>720){const r1=detectAt(px,W,H,dict,1);if(r1.length>r.length)return r1}return r}
 function detectAt(px,W,H,dict,F){
  const n=W*H,g=new Uint8Array(n);
  if(px.length>=n*4){for(let i=0,j=0;i<n;i++,j+=4)g[i]=(px[j]*77+px[j+1]*150+px[j+2]*29)>>8}else g.set(px.subarray?px.subarray(0,n):px);
  const S=(x,y)=>{/* bilinear at pixel-centre convention */x-=.5;y-=.5;if(x<0)x=0;if(y<0)y=0;if(x>W-1.001)x=W-1.001;if(y>H-1.001)y=H-1.001;
   const xi=x|0,yi=y|0,fx=x-xi,fy=y-yi,i=yi*W+xi;return (g[i]*(1-fx)+g[i+1]*fx)*(1-fy)+(g[i+W]*(1-fx)+g[i+W+1]*fx)*fy};
  const f=F,sw=(W/f)|0,sh=(H/f)|0,sm=new Uint8Array(sw*sh);
  if(f===1){for(let y=0;y<sh;y++)for(let x=0;x<sw;x++)sm[y*sw+x]=g[y*W+x]}
  else{for(let y=0;y<sh;y++){const r0=2*y*W,r1=r0+W;for(let x=0;x<sw;x++){const c=2*x;sm[y*sw+x]=(g[r0+c]+g[r0+c+1]+g[r1+c]+g[r1+c+1])>>2}}}
  const I=new Float64Array((sw+1)*(sh+1));
  for(let y=0;y<sh;y++){let s=0;for(let x=0;x<sw;x++){s+=sm[y*sw+x];I[(y+1)*(sw+1)+x+1]=I[y*(sw+1)+x+1]+s}}
  const r=Math.max(7,Math.round(Math.min(sw,sh)/9)),bin=new Uint8Array(sw*sh);
  for(let y=0;y<sh;y++){const y0=Math.max(0,y-r),y1=Math.min(sh,y+r+1);for(let x=0;x<sw;x++){const x0=Math.max(0,x-r),x1=Math.min(sw,x+r+1);
   const m=(I[y1*(sw+1)+x1]-I[y0*(sw+1)+x1]-I[y1*(sw+1)+x0]+I[y0*(sw+1)+x0])/((y1-y0)*(x1-x0));const v=sm[y*sw+x];
   if(v<m*0.86-3)bin[y*sw+x]=1}}
  const lab=new Int32Array(sw*sh),stack=new Int32Array(sw*sh),out=[];let nl=0;
  for(let i0=0;i0<sw*sh;i0++){if(!bin[i0]||lab[i0])continue;nl++;let sp=0;stack[sp++]=i0;lab[i0]=nl;
   let cnt=0,minx=1e9,maxx=-1,miny=1e9,maxy=-1;
   while(sp){const p=stack[--sp];cnt++;const x=p%sw,y=(p-x)/sw;if(x<minx)minx=x;if(x>maxx)maxx=x;if(y<miny)miny=y;if(y>maxy)maxy=y;
    if(x>0&&bin[p-1]&&!lab[p-1]){lab[p-1]=nl;stack[sp++]=p-1}if(x<sw-1&&bin[p+1]&&!lab[p+1]){lab[p+1]=nl;stack[sp++]=p+1}
    if(y>0&&bin[p-sw]&&!lab[p-sw]){lab[p-sw]=nl;stack[sp++]=p-sw}if(y<sh-1&&bin[p+sw]&&!lab[p+sw]){lab[p+sw]=nl;stack[sp++]=p+sw}}
   const bw=maxx-minx+1,bh=maxy-miny+1;
   if(cnt<28||bw<7||bh<7||bw>sw*.6||bh>sh*.6||Math.max(bw,bh)>5*Math.min(bw,bh))continue;
   if(minx===0||miny===0||maxx===sw-1||maxy===sh-1)continue;
   /* hull of the component (pixel corners of each row's ends) */
   const pts=[];for(let y=miny;y<=maxy;y++){let L=-1,R=-1;const row=y*sw;for(let x=minx;x<=maxx;x++)if(lab[row+x]===nl){if(L<0)L=x;R=x}
    if(L>=0){pts.push([L,y],[L,y+1],[R+1,y],[R+1,y+1])}}
   pts.sort((a,b)=>a[0]-b[0]||a[1]-b[1]);const cr=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]);
   const lo=[],hi=[];for(const p of pts){while(lo.length>=2&&cr(lo[lo.length-2],lo[lo.length-1],p)<=0)lo.pop();lo.push(p)}
   for(let k=pts.length-1;k>=0;k--){const p=pts[k];while(hi.length>=2&&cr(hi[hi.length-2],hi[hi.length-1],p)<=0)hi.pop();hi.push(p)}
   const hull=lo.slice(0,-1).concat(hi.slice(0,-1));if(hull.length<4)continue;
   let cx=0,cy=0;for(const p of hull){cx+=p[0];cy+=p[1]}cx/=hull.length;cy/=hull.length;
   let p0=hull[0],d0=-1;for(const p of hull){const d=(p[0]-cx)**2+(p[1]-cy)**2;if(d>d0){d0=d;p0=p}}
   let p2=hull[0],d2=-1;for(const p of hull){const d=(p[0]-p0[0])**2+(p[1]-p0[1])**2;if(d>d2){d2=d;p2=p}}
   let p1=null,p3=null,a1=0,a3=0;for(const p of hull){const c=cr(p0,p2,p);if(c>a1){a1=c;p1=p}if(c<a3){a3=c;p3=p}}
   if(!p1||!p3)continue;
   let q=[p0,p1,p2,p3];const area=((q[0][0]*q[1][1]-q[1][0]*q[0][1])+(q[1][0]*q[2][1]-q[2][0]*q[1][1])+(q[2][0]*q[3][1]-q[3][0]*q[2][1])+(q[3][0]*q[0][1]-q[0][0]*q[3][1]))/2;
   if(area<0)q=[p0,p3,p2,p1];const A=Math.abs(area);if(A<40)continue;
   const fill=cnt/A;if(fill<.42||fill>1.08)continue;
   /* every corner convex, sides not too uneven */
   let ok=true,smin=1e9,smax=0;for(let k=0;k<4;k++){const a=q[k],b=q[(k+1)%4],c=q[(k+2)%4];if(cr(a,b,c)<=0)ok=false;const s=Math.hypot(b[0]-a[0],b[1]-a[1]);smin=Math.min(smin,s);smax=Math.max(smax,s)}
   if(!ok||smin<5||smax>4*smin)continue;
   q=q.map(p=>[p[0]*f,p[1]*f]);
   /* read the 6x6 cells for each of the 4 ways the marker may be turned */
   let best=null;
   for(let j=0;j<4;j++){const qj=[q[j],q[(j+1)%4],q[(j+2)%4],q[(j+3)%4]],m=sq2quad(qj);if(!m)continue;
    const cell=(c,r)=>{let s=0;for(const [du,dv] of [[0,0],[.2,0],[-.2,0],[0,.2],[0,-.2]]){const p=m((c+.5+du)/6,(r+.5+dv)/6);s+=S(p[0],p[1])}return s/5};
    let dark=0,nd=0,light=0,nlt=0;const cells=[];
    for(let r2=0;r2<6;r2++){const row=[];for(let c=0;c<6;c++){const v=cell(c,r2);row.push(v);if(r2===0||r2===5||c===0||c===5){dark+=v;nd++}}cells.push(row)}
    for(let k=0;k<6;k++){for(const [c,r2] of [[k,-1],[k,6],[-1,k],[6,k]]){const p=m((c+.5)/6,(r2+.5)/6);light+=S(p[0],p[1]);nlt++}}
    dark/=nd;light/=nlt;if(light-dark<18)break;const thr=(dark+light)/2;
    let bd=0;for(let r2=0;r2<6;r2++)for(let c=0;c<6;c++)if((r2===0||r2===5||c===0||c===5)&&cells[r2][c]<thr)bd++;
    if(bd<18)break;
    let code=0;for(let r2=0;r2<4;r2++)for(let c=0;c<4;c++)if(cells[r2+1][c+1]<thr)code|=1<<(15-(r2*4+c));
    for(let id=0;id<dict.length;id++){const d=pop(code^dict[id]);if(d<=1&&(!best||d<best.d))best={id:id,d:d,q:qj,contrast:light-dark}}}
   if(!best)continue;
   /* refine the four edges along the gradient, then intersect them */
   const C=best.q,lines=[];
   for(let k=0;k<4;k++){const a=C[k],b=C[(k+1)%4],L=Math.hypot(b[0]-a[0],b[1]-a[1]),dx=(b[0]-a[0])/L,dy=(b[1]-a[1])/L,nx=dy,ny=-dx;
    const R=Math.max(2.5,L*.07),ep=[];
    for(let t=.16;t<=.845;t+=.052){const px0=a[0]+dx*L*t,py0=a[1]+dy*L*t;let bs=0,bg=-1e9,prev=null,gs=[];
     for(let s=-R;s<=R+1e-6;s+=.5){const gv=S(px0+nx*(s+.7),py0+ny*(s+.7))-S(px0+nx*(s-.7),py0+ny*(s-.7));gs.push(gv);if(gv>bg){bg=gv;bs=s}}
     if(bg<10)continue;const i=Math.round((bs+R)/.5);if(i>0&&i<gs.length-1){const y0=gs[i-1],y1=gs[i],y2=gs[i+1],den=y0-2*y1+y2;if(den<0)bs+=.5*.5*(y0-y2)/den}
     ep.push([px0+nx*bs,py0+ny*bs])}
    if(ep.length<5){lines.push(null);continue}
    let mx=0,my=0;for(const p of ep){mx+=p[0];my+=p[1]}mx/=ep.length;my/=ep.length;let sxx=0,sxy=0,syy=0;
    for(const p of ep){const u=p[0]-mx,v=p[1]-my;sxx+=u*u;sxy+=u*v;syy+=v*v}const th=.5*Math.atan2(2*sxy,sxx-syy);
    lines.push([mx,my,Math.cos(th),Math.sin(th)])}
   const crn=[];for(let k=0;k<4;k++){const l1=lines[(k+3)%4],l2=lines[k];let p=C[k];
    if(l1&&l2){const den=l1[2]*l2[3]-l1[3]*l2[2];if(Math.abs(den)>1e-6){const t=((l2[0]-l1[0])*l2[3]-(l2[1]-l1[1])*l2[2])/den;const pp=[l1[0]+l1[2]*t,l1[1]+l1[3]*t];
      if(Math.hypot(pp[0]-p[0],pp[1]-p[1])<Math.max(4,f*3))p=pp}}crn.push(p)}
   out.push({id:best.id,c:crn,d:best.d,k:best.contrast})}
  const byId={};for(const m of out){if(!byId[m.id]||byId[m.id].d>m.d)byId[m.id]=m}
  return Object.keys(byId).map(k=>byId[k])}
 return {detect:detect}}
const DET=detectorFactory();

/* ---------------------------------------------------------------- papers, pose and smoothing */
const PV={papers:[],paper:null,open:false,mode:null,H:null,Hraw:null,seen:[],lastSeen:0,locked:false,fps:0,err:null,
 frames:0,stats:{tracked:0,frames:0},truth:null,verified:null,feat:null,featRev:-1,pulse:{},col:{},legend:[],demoNote:false};
function toMerc(lon,lat){lat=Math.max(-85,Math.min(85,lat));return [RE*lon*Math.PI/180,RE*Math.log(Math.tan(Math.PI/4+lat*Math.PI/360))]}
function pageOf(p,lon,lat){const m=toMerc(lon,lat),f=p.frame,e=p.merc;return [f[0]+(m[0]-e[0])/(e[2]-e[0])*(f[2]-f[0]),f[1]+(e[3]-m[1])/(e[3]-e[1])*(f[3]-f[1])]}
function layout(w,h,code){const s=Math.min(w,h)<=215?16:22,m=8;
 return {m:[[0,m,m,s],[1,w-m-s,m,s],[2,w-m-s,h-m-s,s],[3,m,h-m-s,s],[4,(w-s)/2,m,s],[5,w-m-s,(h-s)/2,s],[6,(w-s)/2,h-m-s,s],[7,m,(h-s)/2,s],
  [code,Math.round((w*.27-s/2)*100)/100,h-m-s,s]],frame:[m+s+4,m+s+4,w-m-s-4,h-m-s-4]}}
function fitMerc(b,frame){let p0=toMerc(b[0],b[1]),p1=toMerc(b[2],b[3]),x0=p0[0],y0=p0[1],x1=p1[0],y1=p1[1],w=Math.max(x1-x0,1),h=Math.max(y1-y0,1);
 x0-=w*.04;x1+=w*.04;y0-=h*.04;y1+=h*.04;w=x1-x0;h=y1-y0;const fa=(frame[2]-frame[0])/(frame[3]-frame[1]);
 if(w/h<fa){const nw=h*fa;x0-=(nw-w)/2;x1+=(nw-w)/2}else{const nh=w/fa;y0-=(nh-h)/2;y1+=(nh-h)/2}return [x0,y0,x1,y1]}
function mapWidget(){const ws=(BD.state.def&&BD.state.def.widgets)||[];return ws.find(w=>w.type==='map')||null}
function layersOf(wid){const v=BD.state.data[wid];if(!v)return [];if(Array.isArray(v.layers))return v.layers;
 if(v.geojson)return [{id:'L0',kind:'fill',geojson:v.geojson,style:{}}];return []}
function bboxOf(layers){let a=[1e9,1e9,-1e9,-1e9];const walk=c=>{if(typeof c[0]==='number'){a[0]=Math.min(a[0],c[0]);a[1]=Math.min(a[1],c[1]);a[2]=Math.max(a[2],c[0]);a[3]=Math.max(a[3],c[1])}else for(const x of c)walk(x)};
 for(const L of layers)for(const f of ((L.geojson||{}).features||[]))if(f.geometry&&f.geometry.coordinates)walk(f.geometry.coordinates);return a[0]<a[2]?a:null}
/* a paper made here from the map (no signature): the demo works for any dashboard with a map */
function makePassport(size,orient){const w0=mapWidget();if(!w0)return null;const L=layersOf(w0.id),b=bboxOf(L);if(!b)return null;
 const dims=size==='A3'?[420,297]:[297,210],page=orient==='portrait'?[dims[1],dims[0]]:dims,lay=layout(page[0],page[1],FIXED);
 return {v:1,id:'p_local',code:FIXED,dash:(BD.state.def||{}).id,widget:w0.id,title:(BD.state.def||{}).title||'',page:page,frame:lay.frame,merc:fitMerc(b,lay.frame),m:lay.m,iat:Math.floor(Date.now()/1000),local:true}}
function pose(markers,p){const pm={};for(const m of p.m)pm[m[0]]=m;let pairs=[];
 for(const k of markers){const q=pm[k.id];if(!q)continue;const x=q[1],y=q[2],s=q[3];pairs.push({id:k.id,src:[[x,y],[x+s,y],[x+s,y+s],[x,y+s]],dst:k.c})}
 if(!pairs.length)return null;let H=null;
 for(let it=0;it<3;it++){const src=[],dst=[];for(const q of pairs){for(let i=0;i<4;i++){src.push(q.src[i]);dst.push(q.dst[i])}}H=homog(src,dst);if(!H)return null;
  if(pairs.length<3)break;let worst=null,we=0,sz=0;for(const q of pairs){let e=0;for(let i=0;i<4;i++){const r=app(H,q.src[i][0],q.src[i][1]);e+=Math.hypot(r[0]-q.dst[i][0],r[1]-q.dst[i][1])}e/=4;
   sz+=Math.hypot(q.dst[1][0]-q.dst[0][0],q.dst[1][1]-q.dst[0][1]);if(e>we){we=e;worst=q}}
  sz/=pairs.length;if(we>Math.max(2.5,.06*sz))pairs=pairs.filter(q=>q!==worst);else break}
 let rms=0,n=0;for(const q of pairs)for(let i=0;i<4;i++){const r=app(H,q.src[i][0],q.src[i][1]);rms+=(r[0]-q.dst[i][0])**2+(r[1]-q.dst[i][1])**2;n++}
 return {H:H,n:pairs.length,ids:pairs.map(q=>q.id),rms:Math.sqrt(rms/n)}}
function pickPaper(markers){const codes=markers.filter(m=>m.id>=FIXED).map(m=>m.id);
 for(const c of codes){const p=PV.papers.find(x=>x.code===c);if(p)return p}
 if(PV.paper&&(!codes.length||codes.includes(PV.paper.code)))return PV.paper;return PV.papers[0]||PV.paper}
/* one-euro filter (Casiez et al.) on the frame corners: still when held still, quick when moved */
function OneEuro(minCut,beta){let x=null,dx=null,t0=0;const al=(c,dt)=>{const r=2*Math.PI*c*dt;return r/(r+1)};
 return {f:(v,t)=>{if(x===null||t-t0>.5){x=v.slice();dx=v.map(()=>0);t0=t;return x}const dt=Math.max(1e-3,t-t0);t0=t;
  const ad=al(1,dt);for(let i=0;i<v.length;i++){const d=(v[i]-x[i])/dt;dx[i]=dx[i]+ad*(d-dx[i]);const c=minCut+beta*Math.abs(dx[i]),a=al(c,dt);x[i]=x[i]+a*(v[i]-x[i])}return x},reset:()=>{x=null}}}
const FILT=OneEuro(1.4,.018);
function frameCorners(p){const f=p.frame;return [[f[0],f[1]],[f[2],f[1]],[f[2],f[3]],[f[0],f[3]]]}
function track(markers,t){const p=pickPaper(markers);PV.seen=markers;if(!p){PV.Hraw=null;return null}PV.paper=p;
 const r=pose(markers,p);if(!r){PV.Hraw=null;return null}PV.Hraw=r.H;PV.lastSeen=t;PV.used=r.n;PV.rms=r.rms;
 const fc=frameCorners(p),v=[];for(const c of fc){const q=app(r.H,c[0],c[1]);v.push(q[0],q[1])}const s=FILT.f(v,t);
 const H=homog(fc,[[s[0],s[1]],[s[2],s[3]],[s[4],s[5]],[s[6],s[7]]]);if(H)PV.H=H;return PV.H}

/* ---------------------------------------------------------------- the paper itself, drawn (demo and tests) */
function b64u(hex){if(!hex)return '';let s='';for(let i=0;i<hex.length;i+=2)s+=String.fromCharCode(parseInt(hex.substr(i,2),16));return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')}
function cellsOf(id){const c=DICT[id],g=[];for(let r=0;r<6;r++){const row=[];for(let k=0;k<6;k++){if(r===0||r===5||k===0||k===5)row.push(1);else row.push((c>>(15-((r-1)*4+(k-1))))&1)}g.push(row)}return g}
const FONT='"Segoe UI","Noto Sans Arabic",Tahoma,Arial,sans-serif';
function geomParts(f){const g=f.geometry||{},c=g.coordinates;switch(g.type){case 'Polygon':return ['fill',[c]];case 'MultiPolygon':return ['fill',c];
 case 'LineString':return ['line',[c]];case 'MultiLineString':return ['line',c];case 'Point':return ['point',[c]];case 'MultiPoint':return ['point',c]}return [null,[]]}
function renderPaper(p,layers,ppm,lang){const W=Math.round(p.page[0]*ppm),Hh=Math.round(p.page[1]*ppm),cv=document.createElement('canvas');cv.width=W;cv.height=Hh;
 const c=cv.getContext('2d'),X=v=>v*ppm,P=(lon,lat)=>{const q=pageOf(p,lon,lat);return [q[0]*ppm,q[1]*ppm]};
 c.fillStyle='#fff';c.fillRect(0,0,W,Hh);const f=p.frame;c.fillStyle='#FBFAF6';c.fillRect(X(f[0]),X(f[1]),X(f[2]-f[0]),X(f[3]-f[1]));
 c.save();c.beginPath();c.rect(X(f[0]),X(f[1]),X(f[2]-f[0]),X(f[3]-f[1]));c.clip();const labels=[];
 const ord=layers.slice().sort((a,b)=>({fill:0,line:1,point:2}[a.kind]||3)-({fill:0,line:1,point:2}[b.kind]||3));
 for(const L of ord){const st=L.style||{},faint=L.kind==='fill'&&st.opacity!=null&&st.opacity<.2;
  for(const ft of ((L.geojson||{}).features||[])){const [k,parts]=geomParts(ft),pr=ft.properties||{};
   if(k==='fill'){c.beginPath();for(const poly of parts)for(const ring of poly){ring.forEach((q,i)=>{const s=P(q[0],q[1]);i?c.lineTo(s[0],s[1]):c.moveTo(s[0],s[1])});c.closePath()}
    if(faint){c.setLineDash([X(1.6),X(1)]);c.lineWidth=X(.55);c.strokeStyle='#5B6770';c.stroke();c.setLineDash([])}else{c.fillStyle='#fff';c.fill();c.lineWidth=X(.16);c.strokeStyle='#7B848C';c.stroke()}
    if(pr._l&&parts[0]&&parts[0][0]){const r=parts[0][0];let sx=0,sy=0,n=Math.max(1,r.length-1);for(let i=0;i<n;i++){sx+=r[i][0];sy+=r[i][1]}labels.push([faint?'area':'parcel',String(pr._l),P(sx/n,sy/n),null])}}
   else if(k==='line'){const main=String(pr._c||'').toUpperCase()!=='#FFFFFF';for(const line of parts){c.beginPath();line.forEach((q,i)=>{const s=P(q[0],q[1]);i?c.lineTo(s[0],s[1]):c.moveTo(s[0],s[1])});
     c.lineJoin='round';c.lineWidth=X(main?4.6:2.8);c.strokeStyle='#A9A08A';c.stroke();c.lineWidth=X(main?3.8:2.1);c.strokeStyle=main?'#FFF3C2':'#fff';c.stroke();
     if(pr._l&&line.length>1){const a=P(line[0][0],line[0][1]),b=P(line[line.length-1][0],line[line.length-1][1]);labels.push(['street',String(pr._l).trim(),[(a[0]+b[0])/2,(a[1]+b[1])/2],Math.atan2(b[1]-a[1],b[0]-a[0])])}}}
   else if(k==='point'){for(const q of parts){const s=P(q[0],q[1]),big=!!pr._l,r=X(big?1.6:.55);c.beginPath();c.arc(s[0],s[1],r,0,7);c.fillStyle=pr._c||'#555';c.fill();c.lineWidth=X(.3);c.strokeStyle='#fff';c.stroke();
     if(big)labels.push(['poi',String(pr._l),[s[0],s[1]-X(2.6)],null])}}}}
 c.textAlign='center';c.textBaseline='middle';c.direction='rtl';
 for(const [k,t,q,a] of labels.sort((x,y)=>({parcel:0,street:1,poi:2,area:3}[x[0]])-({parcel:0,street:1,poi:2,area:3}[y[0]]))){
  const sz={parcel:1.55,street:1.9,poi:2.1,area:4.2}[k];c.save();c.translate(q[0],q[1]);if(a!=null){let r=a;if(r>Math.PI/2)r-=Math.PI;if(r<-Math.PI/2)r+=Math.PI;c.rotate(r)}
  c.font=(k==='area'||k==='poi'?'700 ':'')+X(sz)+'px '+FONT;if(k==='area'){c.lineWidth=X(.9);c.strokeStyle='#ffffffe6';c.strokeText(t,0,0)}
  c.fillStyle=k==='area'?'#37474F':'#4A5560';c.fillText(t,0,0);c.restore()}
 c.restore();c.lineWidth=X(.35);c.strokeStyle='#37474F';c.strokeRect(X(f[0]),X(f[1]),X(f[2]-f[0]),X(f[3]-f[1]));
 for(const [id,mx,my,ms] of p.m){const cell=ms/6;c.fillStyle='#fff';c.fillRect(X(mx-cell),X(my-cell),X(ms+2*cell),X(ms+2*cell));const g=cellsOf(id);c.fillStyle='#000';
  for(let r=0;r<6;r++)for(let k=0;k<6;k++)if(g[r][k])c.fillRect(X(mx+k*cell),X(my+r*cell),X(cell)+.6,X(cell)+.6)}
 const s=p.m[0][3],Wm=p.page[0],Hm=p.page[1],tx=k=>{const a=(window.BiruniDash&&BD.t)?BD.t(k):k;return a};
 c.direction='rtl';c.textAlign='right';c.fillStyle='#1F2A33';c.font='700 '+X(4.4)+'px '+FONT;c.fillText(p.title||'',X(Wm-8-s-4),X(8+s/2),X(Wm-8-s-4)-X((Wm+s)/2+4));
 c.direction='ltr';c.textAlign='left';c.fillStyle='#52606B';c.font=X(2.8)+'px '+FONT;c.fillText(tx('paper_kind')+'  ·  '+new Date((p.iat||0)*1000).toISOString().slice(0,10),X(8+s+4),X(8+s/2),X((Wm-s)/2-4)-X(8+s+4));
 const code=p.m.find(m=>m[0]===p.code)||p.m[8],by=Hm-8-s;c.textAlign='center';c.direction='rtl';c.fillStyle='#0B4F4A';c.font='700 '+X(3.4)+'px '+FONT;
 c.fillText(tx('paper_hint'),(X(code[1]+s+5)+X((Wm-s)/2-5))/2,X(by+s/2),X((Wm-s)/2-5)-X(code[1]+s+5));
 try{if(typeof QR!=='undefined'&&p.sig){const q=QR.make(new TextEncoder().encode('BLP1.'+p.id+'.'+p.code+'.'+b64u(p.sig)),'M'),n=q.size,qs=s,qx=(Wm+s)/2+((Wm-8-s)-(Wm+s)/2)/2-qs/2,md=qs/n;
  c.fillStyle='#fff';c.fillRect(X(qx-1.5),X(by-1.5),X(qs+3),X(qs+3));c.fillStyle='#000';for(let y=0;y<n;y++)for(let x=0;x<n;x++)if(q.m[y][x])c.fillRect(X(qx+x*md),X(by+y*md),X(md)+.5,X(md)+.5)}}catch(e){}
 return cv}

/* ---------------------------------------------------------------- the live layer over the paper */
function hexA(h,a){h=String(h||'#888').replace('#','');if(h.length===3)h=h.split('').map(x=>x+x).join('');const n=parseInt(h.slice(0,6),16)||0x888888;
 return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')'}
function shade(h,k){h=String(h||'#888').replace('#','');if(h.length===3)h=h.split('').map(x=>x+x).join('');const n=parseInt(h.slice(0,6),16)||0x888888;
 const f=v=>Math.max(0,Math.min(255,Math.round(v*k)));return 'rgb('+f(n>>16&255)+','+f(n>>8&255)+','+f(n&255)+')'}
function prepare(){const p=PV.paper;if(!p)return;const L=layersOf(p.widget);const rev=BD.state.rev+':'+p.id;if(PV.featRev===rev&&PV.feat)return;
 const now=performance.now(),items=[],legend=[];
 for(const lay of L){const st=lay.style||{},faint=lay.kind==='fill'&&st.opacity!=null&&st.opacity<.2;if(lay.kind==='line')continue;
  for(const it of (lay.legend||[]))if(lay.kind==='fill'&&!faint&&legend.length<8)legend.push(it);
  for(const ft of ((lay.geojson||{}).features||[])){const [k,parts]=geomParts(ft),pr=ft.properties||{},fid=lay.id+':'+(ft.id!=null?ft.id:pr.id);
   const pg=k==='fill'?parts.map(poly=>poly.map(r=>r.map(q=>pageOf(p,q[0],q[1])))):parts.map(q=>pageOf(p,q[0],q[1]));
   const col=pr._c||st.color||'#2F80ED';if(PV.col[fid]&&PV.col[fid]!==col)PV.pulse[fid]=now;PV.col[fid]=col;
   let cx=0,cy=0,n=0;if(k==='fill'&&pg[0]&&pg[0][0]){const r=pg[0][0];for(let i=0;i<r.length-1;i++){cx+=r[i][0];cy+=r[i][1];n++}cx/=n||1;cy/=n||1}
   items.push({fid:fid,kind:k,faint:faint,pg:pg,c:col,l:pr._l,k:pr._k||{},props:pr,layer:lay,cx:cx,cy:cy})}}
 PV.feat=items;PV.featRev=rev;PV.legend=legend;drawLegend()}
function drawLegend(){const el=$('paperLegend');if(!el)return;el.textContent='';for(const it of PV.legend){const s=document.createElement('span'),i=document.createElement('i');
 i.style.background=it.color;s.appendChild(i);s.appendChild(document.createTextNode(String(it.label)));el.appendChild(s)}}
function drawOverlay(ctx,H,now){if(!H||!PV.feat)return;const xf=BD.state.xf;
 const pr=(x,y)=>app(H,x,y);
 const sc=(()=>{const f=PV.paper.frame,cx=(f[0]+f[2])/2,cy=(f[1]+f[3])/2,a=pr(cx,cy),b=pr(cx+10,cy);return Math.hypot(b[0]-a[0],b[1]-a[1])/10})();
 ctx.save();ctx.lineJoin='round';
 for(const it of PV.feat){if(it.kind!=='fill')continue;let on=null;if(xf){const v=it.k[xf.field];if(v!==undefined)on=String(v)===xf.key}
  if(it.faint){if(on!==true)continue;ctx.beginPath();for(const poly of it.pg)for(const r of poly){r.forEach((q,i)=>{const s=pr(q[0],q[1]);i?ctx.lineTo(s[0],s[1]):ctx.moveTo(s[0],s[1])});ctx.closePath()}
   ctx.lineWidth=Math.max(2,sc*1.1);ctx.strokeStyle='#FBBF24';ctx.shadowColor='#FBBF24';ctx.shadowBlur=12;ctx.stroke();ctx.shadowBlur=0;continue}
  ctx.beginPath();for(const poly of it.pg)for(const r of poly){r.forEach((q,i)=>{const s=pr(q[0],q[1]);i?ctx.lineTo(s[0],s[1]):ctx.moveTo(s[0],s[1])});ctx.closePath()}
  const a=on===false?.1:on===true?.82:.58;ctx.fillStyle=hexA(it.c,a);ctx.fill();
  ctx.lineWidth=Math.max(.8,sc*.22);ctx.strokeStyle=on===false?'rgba(0,0,0,.08)':shade(it.c,.62);ctx.stroke();
  const pt=PV.pulse[it.fid];if(pt&&now-pt<1800){const k=(now-pt)/1800;ctx.lineWidth=Math.max(2,sc*(.6+1.6*k));ctx.strokeStyle='rgba(255,255,255,'+(1-k)+')';
   ctx.shadowColor=it.c;ctx.shadowBlur=18*(1-k);ctx.stroke();ctx.shadowBlur=0}}
 for(const it of PV.feat){if(it.kind!=='point'||it.props._l)continue;for(const q of it.pg){const s=pr(q[0],q[1]),r=Math.max(2.2,sc*.8);ctx.beginPath();ctx.arc(s[0],s[1],r,0,7);
  ctx.fillStyle=hexA(it.c,.95);ctx.fill();ctx.lineWidth=1.2;ctx.strokeStyle='#fff';ctx.stroke()}}
 const fs=sc*2.1;if(fs>=8.5){ctx.font='700 '+Math.min(fs,26)+'px '+FONT;ctx.textAlign='center';ctx.textBaseline='middle';ctx.direction='ltr';
  for(const it of PV.feat){if(it.kind!=='fill'||it.faint||!it.l)continue;const s=pr(it.cx,it.cy);ctx.lineWidth=3;ctx.strokeStyle='rgba(0,0,0,.55)';ctx.strokeText(it.l,s[0],s[1]);ctx.fillStyle='#fff';ctx.fillText(it.l,s[0],s[1])}}
 /* the frame of the map, a thin golden outline that shows the lock */
 const f=PV.paper.frame,fc=[[f[0],f[1]],[f[2],f[1]],[f[2],f[3]],[f[0],f[3]]].map(q=>pr(q[0],q[1]));ctx.beginPath();fc.forEach((q,i)=>i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]));ctx.closePath();
 ctx.lineWidth=2;ctx.strokeStyle='rgba(251,191,36,.85)';ctx.stroke();ctx.restore()}
function drawMarkers(ctx,ms,now){ctx.save();ctx.lineWidth=2;for(const m of ms){const c=m.c;ctx.strokeStyle='rgba(45,212,191,.95)';ctx.beginPath();c.forEach((q,i)=>i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]));ctx.closePath();ctx.stroke();
  ctx.fillStyle='rgba(45,212,191,.95)';ctx.font='600 11px ui-monospace,Consolas,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
  const cx=(c[0][0]+c[2][0])/2,cy=(c[0][1]+c[2][1])/2;ctx.fillText(String(m.id),cx,cy);ctx.beginPath();ctx.arc(c[0][0],c[0][1],3,0,7);ctx.fill()}ctx.restore()}

/* ---------------------------------------------------------------- steady display: a frame warped by a homography.
   «الورقة المثبّتة»: each analysed frame is warped top-down with ITS OWN homography, so the paper stands still on the
   screen however the hand shakes, and the live layer is drawn in paper coordinates (no relative jitter by
   construction). «الواقع المعزّز»: the frame is warped by (smoothed pose)·(its pose)⁻¹ - the way a phone camera
   stabilises video - and the live layer follows the smoothed pose, so both stay together and the tremor is gone. */
const GLW={ok:null};
function glInit(){if(GLW.ok!==null)return GLW.ok;GLW.ok=false;try{const cv=document.createElement('canvas');
 const gl=cv.getContext('webgl',{premultipliedAlpha:false,preserveDrawingBuffer:true,antialias:false,alpha:true});if(!gl)return false;
 const sh=(t,src)=>{const o=gl.createShader(t);gl.shaderSource(o,src);gl.compileShader(o);if(!gl.getShaderParameter(o,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(o));return o};
 const pr=gl.createProgram();gl.attachShader(pr,sh(gl.VERTEX_SHADER,'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}'));
 gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,'precision highp float;uniform sampler2D t;uniform mat3 M;uniform vec2 S;uniform float O;uniform float D;uniform float T;'+
  'void main(){vec3 q=M*vec3(gl_FragCoord.x,O-gl_FragCoord.y,1.);if(q.z<=0.){gl_FragColor=T>.5?vec4(0.):vec4(.02,.03,.04,1.);return;}vec2 uv=q.xy/q.z/S;'+
  'if(uv.x<0.||uv.y<0.||uv.x>1.||uv.y>1.){gl_FragColor=T>.5?vec4(0.):vec4(.02,.03,.04,1.);return;}gl_FragColor=vec4(texture2D(t,uv).rgb*D,1.);}'));
 gl.linkProgram(pr);if(!gl.getProgramParameter(pr,gl.LINK_STATUS))return false;gl.useProgram(pr);
 const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
 const a=gl.getAttribLocation(pr,'a');gl.enableVertexAttribArray(a);gl.vertexAttribPointer(a,2,gl.FLOAT,false,0,0);
 const tx=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,tx);for(const [k,v] of [[gl.TEXTURE_MIN_FILTER,gl.LINEAR],[gl.TEXTURE_MAG_FILTER,gl.LINEAR],
  [gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE],[gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE]])gl.texParameteri(gl.TEXTURE_2D,k,v);
 Object.assign(GLW,{ok:true,cv:cv,gl:gl,uM:gl.getUniformLocation(pr,'M'),uS:gl.getUniformLocation(pr,'S'),uO:gl.getUniformLocation(pr,'O'),uD:gl.getUniformLocation(pr,'D'),uT:gl.getUniformLocation(pr,'T')})}
 catch(e){GLW.ok=false}return GLW.ok}
/* one triangle of src drawn affinely onto three points of dst */
function triDraw(x,img,s,d){const [s0,s1,s2]=s,[d0,d1,d2]=d,den=(s1[0]-s0[0])*(s2[1]-s0[1])-(s2[0]-s0[0])*(s1[1]-s0[1]);if(Math.abs(den)<1e-9)return;
 const a=((d1[0]-d0[0])*(s2[1]-s0[1])-(d2[0]-d0[0])*(s1[1]-s0[1]))/den,b=((d2[0]-d0[0])*(s1[0]-s0[0])-(d1[0]-d0[0])*(s2[0]-s0[0]))/den,
  c=((d1[1]-d0[1])*(s2[1]-s0[1])-(d2[1]-d0[1])*(s1[1]-s0[1]))/den,dd=((d2[1]-d0[1])*(s1[0]-s0[0])-(d1[1]-d0[1])*(s2[0]-s0[0]))/den;
 const mx=(d0[0]+d1[0]+d2[0])/3,my=(d0[1]+d1[1]+d2[1])/3;x.save();x.beginPath();
 d.forEach((q,i)=>{const X=mx+(q[0]-mx)*1.015+(q[0]>mx?.5:-.5),Y=my+(q[1]-my)*1.015+(q[1]>my?.5:-.5);i?x.lineTo(X,Y):x.moveTo(X,Y)});x.closePath();x.clip();
 x.setTransform(a,c,b,dd,d0[0]-a*s0[0]-b*s0[1],d0[1]-c*s0[0]-dd*s0[1]);x.drawImage(img,0,0);x.restore()}
/* draw src onto dst (w×h) so that display pixel p shows src pixel M·p (M: display -> src) */
function warpTo(dst,src,M,w,h,dim,over){dim=dim==null?1:dim;
 if(V.useGL!==false&&glInit()){try{const g=GLW.gl;if(GLW.cv.width!==w||GLW.cv.height!==h){GLW.cv.width=w;GLW.cv.height=h}g.viewport(0,0,w,h);
   g.texImage2D(g.TEXTURE_2D,0,g.RGBA,g.RGBA,g.UNSIGNED_BYTE,src);g.uniformMatrix3fv(GLW.uM,false,[M[0],M[3],M[6],M[1],M[4],M[7],M[2],M[5],M[8]]);
   g.uniform2f(GLW.uS,src.width,src.height);g.uniform1f(GLW.uO,h);g.uniform1f(GLW.uD,dim);g.uniform1f(GLW.uT,over?1:0);g.drawArrays(g.TRIANGLE_STRIP,0,4);
   if(g.getError()===g.NO_ERROR){dst.setTransform(1,0,0,1,0,0);if(!over)dst.clearRect(0,0,w,h);dst.drawImage(GLW.cv,0,0);V.warpBy='webgl';return}}catch(e){}GLW.ok=false}
 /* without WebGL: a mesh of small triangles (each nearly affine) */
 dst.setTransform(1,0,0,1,0,0);if(!over){dst.fillStyle='#05080b';dst.fillRect(0,0,w,h)}const nx=18,ny=12;
 for(let j=0;j<ny;j++)for(let i=0;i<nx;i++){const x0=i*w/nx,x1=(i+1)*w/nx,y0=j*h/ny,y1=(j+1)*h/ny;
  for(const tri of [[[x0,y0],[x1,y0],[x1,y1]],[[x0,y0],[x1,y1],[x0,y1]]]){const sp=tri.map(q=>app(M,q[0],q[1]));
   if(sp.every(q=>q[0]<0)||sp.every(q=>q[1]<0)||sp.every(q=>q[0]>src.width)||sp.every(q=>q[1]>src.height))continue;triDraw(dst,src,sp,tri)}}
 if(dim<1&&!over){dst.fillStyle='rgba(5,8,11,'+(1-dim)+')';dst.fillRect(0,0,w,h)}V.warpBy='2d'}
/* [11-08] the steady paper is a MOSAIC in paper space: every analysed frame is laid onto it with its own homography,
   so a corner the camera does not see right now keeps its last seen picture (never black), and a corner never seen
   shows the paper as printed (drawn here). The newest frame always wins where it sees. */
function mosaicAdd(src,Hr){const p=PV.paper;if(!p||!Hr)return;const ppm=Math.min(8,2400/Math.max(p.page[0],p.page[1])),mw=Math.round(p.page[0]*ppm),mh=Math.round(p.page[1]*ppm);
 if(!V.mos||V.mosId!==p.id||V.mos.width!==mw||V.mos.height!==mh){V.mos=document.createElement('canvas');V.mos.width=mw;V.mos.height=mh;V.mx=V.mos.getContext('2d');V.mosId=p.id;V.mosPpm=ppm;
  try{V.mx.drawImage(renderPaper(p,layersOf(p.widget),ppm,BD.lang()),0,0,mw,mh)}catch(e){V.mx.fillStyle='#f4f3ee';V.mx.fillRect(0,0,mw,mh)}}
 warpTo(V.mx,src,mul(Hr,[1/ppm,0,0,0,1/ppm,0,0,0,1]),mw,mh,1,true)}
/* the steady paper: page (mm) -> display px, the paper turned by quarter turns as the camera sees it, centred */
function stabBase(p,cw,ch,rot){const pw=p.page[0],ph=p.page[1],r=rot||0,rw=r%2?ph:pw,rh=r%2?pw:ph,top=ch*.1,bot=ch*.14,side=cw*.025;
 const s=Math.min((cw-2*side)/rw,(ch-top-bot)/rh),ox=(cw-rw*s)/2,oy=top+((ch-top-bot)-rh*s)/2;
 const R=[[1,0,0,0,1,0],[0,-1,ph,1,0,0],[-1,0,pw,0,-1,ph],[0,1,0,-1,0,pw]][r];return mul([s,0,ox,0,s,oy,0,0,1],[R[0],R[1],R[2],R[3],R[4],R[5],0,0,1])}
function quarter(H,p){const f=p.frame,cx=(f[0]+f[2])/2,cy=(f[1]+f[3])/2,a=app(H,cx,cy),b=app(H,cx+10,cy),ang=Math.atan2(b[1]-a[1],b[0]-a[0]);
 return {q:((Math.round(ang/(Math.PI/2))%4)+4)%4,ang:ang}}
function zoomM(){const z=V.zv;return [z.k,0,z.x,0,z.k,z.y,0,0,1]}
const CROP=1.06;function cropM(W,H){return [CROP,0,-(CROP-1)*W/2,0,CROP,-(CROP-1)*H/2,0,0,1]}
/* the page -> display matrix of the live layer, and the display -> frame matrix of the picture, for a frame whose
   pose is Hr (page -> frame px) and whose smoothed pose is Hs */
function mapsFor(mode,Hr,Hs,p){if(mode==='stab'){const B=mul(zoomM(),stabBase(p,V.cv.width,V.cv.height,V.rot||0)),Bi=inv(B);return Bi?{ov:B,pic:mul(Hr,Bi)}:null}
 const Z=cropM(V.cv.width,V.cv.height),Hri=inv(Hr),C=Hri?mul(Z,mul(Hs,Hri)):null,Ci=C?inv(C):null;return Ci?{ov:mul(Z,Hs),pic:Ci}:null}
function keepFrame(src,Hr,Hs){if(!V.keep){V.keep=document.createElement('canvas');V.kctx=V.keep.getContext('2d')}
 if(V.keep.width!==src.width||V.keep.height!==src.height){V.keep.width=src.width;V.keep.height=src.height}V.kctx.drawImage(src,0,0);
 if(V.mode==='stab')mosaicAdd(src,Hr);V.keepHr=Hr;V.keepHs=Hs||Hr;V.keepSeen=PV.seen;V.keepT=performance.now()}
function renderBg(){if(!V.keep||!V.keepHr||!PV.paper)return false;const w=V.cv.width,h=V.cv.height;
 if(V.mode==='stab'){const qa=quarter(V.keepHr,PV.paper);if(V.rot==null)V.rot=qa.q;else if(qa.q!==V.rot){const d=Math.abs(((qa.ang-V.rot*Math.PI/2+3*Math.PI)%(2*Math.PI))-Math.PI);if(d>.97)V.rot=qa.q}}
 const m=mapsFor(V.mode,V.keepHr,V.keepHs,PV.paper);if(!m)return false;if(!V.bg){V.bg=document.createElement('canvas');V.bgx=V.bg.getContext('2d')}
 if(V.bg.width!==w||V.bg.height!==h){V.bg.width=w;V.bg.height=h}
 if(V.mode==='stab'&&V.mos&&V.mosId===PV.paper.id){const x=V.bgx,B=m.ov,k=1/V.mosPpm;x.setTransform(1,0,0,1,0,0);x.fillStyle='#05080b';x.fillRect(0,0,w,h);
  x.imageSmoothingEnabled=true;x.imageSmoothingQuality='high';x.setTransform(B[0]*k,B[3]*k,B[1]*k,B[4]*k,B[2],B[5]);x.drawImage(V.mos,0,0);x.setTransform(1,0,0,1,0,0)}
 else warpTo(V.bgx,V.keep,m.pic,w,h,1);V.bgOv=m.ov;V.bgMode=V.mode;V.bgDirty=false;
 if(V.mode==='stab'){/* the steady paper: what lies around the sheet (the desk, warped) is darkened, the sheet stands out */
  const pw=PV.paper.page[0],ph=PV.paper.page[1],cs=[[0,0],[pw,0],[pw,ph],[0,ph]].map(q=>app(m.ov,q[0],q[1])),x=V.bgx;x.save();x.beginPath();x.rect(0,0,w,h);
  cs.forEach((q,i)=>i?x.lineTo(q[0],q[1]):x.moveTo(q[0],q[1]));x.closePath();x.fillStyle='rgba(5,8,11,.84)';x.fill('evenodd');
  x.beginPath();cs.forEach((q,i)=>i?x.lineTo(q[0],q[1]):x.moveTo(q[0],q[1]));x.closePath();x.shadowColor='rgba(0,0,0,.6)';x.shadowBlur=18;x.lineWidth=1;x.strokeStyle='rgba(255,255,255,.18)';x.stroke();x.restore()}
 const Dm=V.mode==='ar'?mul(m.ov,inv(V.keepHr)||[1,0,0,0,1,0,0,0,1]):null;
 V.bgSeen=Dm&&V.keepSeen?V.keepSeen.map(k=>({id:k.id,c:k.c.map(q=>app(Dm,q[0],q[1]))})):null;return true}
/* a shaking hand for tests and measurements: tremor 8-12 Hz, a slow drift, a little tilt */
function shakeCam(amp,pan){amp=amp==null?1:amp;return (p,t,W,H)=>{const w=p.page[0],h=p.page[1],port=H>W,rad=Math.PI/180,T2=2*Math.PI*t;
 const th=(port?90:0)+(-3+2*Math.sin(.3*t)+amp*(1.0*Math.sin(9.3*T2)+.45*Math.sin(11.7*T2+1)))*rad,
  ph=(18+1.2*Math.sin(.4*t)+amp*.35*Math.sin(8.4*T2+.3))*rad,ps=(3*Math.sin(.2*t+1)+amp*.25*Math.sin(10.2*T2))*rad;
 const Z=640+10*Math.sin(.23*t),tx=(pan?60*Math.sin(.9*t):4*Math.sin(.25*t))+amp*(1.35*Math.sin(9.1*T2)+.8*Math.sin(11.3*T2+.7)),
  ty=3*Math.sin(.31*t)+amp*(1.1*Math.sin(8.7*T2+2)+.7*Math.sin(10.6*T2));
 const cz=Math.cos(th),sz=Math.sin(th),cx=Math.cos(ph),sx=Math.sin(ph),cy=Math.cos(ps),sy=Math.sin(ps);
 const R=mul([cz,-sz,0,sz,cz,0,0,0,1],mul([1,0,0,0,cx,-sx,0,sx,cx],[cy,0,sy,0,1,0,-sy,0,cy])),long=Math.max(W,H),f=.8*long*Z/Math.max(w,h),u0=W/2,v0=H/2;
 return mul([f*R[0]+u0*R[6],f*R[1]+u0*R[7],f*tx+u0*Z,f*R[3]+v0*R[6],f*R[4]+v0*R[7],f*ty+v0*Z,R[6],R[7],Z],[1,0,-w/2,0,1,-h/2,0,0,1])}}
/* measure: jitter on the screen and the match of the live layer to the paper, frame by frame, for the old display
   ("before": the frame as captured, the layer at its pose), the steady paper and the stabilised AR view */
function measure(o){o=o||{};const p=o.paper||PV.papers[0]||PV.paper;if(!p)return null;const N=o.n||150,fps=o.fps||30,W=o.W||1280,H=o.H||720;
 const cam=o.cam||shakeCam(o.amp==null?1:o.amp,o.pan),ppm=3.4,img=renderPaper(p,layersOf(p.widget),ppm,BD.lang()),dk=desk(W,H);
 const k=Math.min(1,960/Math.max(W,H)),aw=Math.round(W*k),ah=Math.round(H*k),sc=document.createElement('canvas');sc.width=W;sc.height=H;const sx=sc.getContext('2d',{willReadFrequently:true});
 const an=document.createElement('canvas');an.width=aw;an.height=ah;const ax=an.getContext('2d',{willReadFrequently:true});
 const nz=document.createElement('canvas');nz.width=256;nz.height=256;{const z=nz.getContext('2d'),id=z.createImageData(256,256);let r=9;for(let i=0;i<id.data.length;i+=4){r=(r*16807)%2147483647;const v=r%255;id.data[i]=id.data[i+1]=id.data[i+2]=v;id.data[i+3]=255}z.putImageData(id,0,0)}
 const save={demo:V.demo,W:V.W,H:V.H,cv:V.cv,zv:V.zv,rot:V.rot};V.demo={paper:p,img:img,ppm:ppm,desk:dk,imgCpu:img,deskCpu:dk,cam:cam};V.W=W;V.H=H;V.zv={k:1,x:0,y:0};V.rot=null;
 const disp={width:o.dw||W,height:o.dh||H};V.cv=disp;FILT.reset();PV.H=null;
 const f=p.frame,P=[];for(let j=0;j<4;j++)for(let i=0;i<5;i++)P.push([f[0]+(f[2]-f[0])*(i+.5)/5,f[1]+(f[3]-f[1])*(j+.5)/4]);
 const R={before:{o:[],c:[],e:[]},stab:{o:[],c:[],e:[]},ar:{o:[],c:[],e:[]}},prev={},lagA=[],lagT=[];let lost=0,t0=performance.now();
 for(let i=0;i<N;i++){const t=(o.t0||1)+i/fps;sx.filter=o.blur?'blur('+o.blur+'px)':'none';const Ht=drawScene(sx,t);sx.filter='none';
  if(o.noise){sx.globalAlpha=o.noise;sx.globalCompositeOperation='overlay';sx.drawImage(nz,(i*37)%200,(i*53)%200,W,H);sx.globalAlpha=1;sx.globalCompositeOperation='source-over'}
  ax.drawImage(sc,0,0,aw,ah);const d=ax.getImageData(0,0,aw,ah),ms=DET.detect(d.data,aw,ah,DICT);if(k!==1)for(const m of ms)m.c=m.c.map(q=>[q[0]/k,q[1]/k]);
  track(ms,t);if(!PV.Hraw){lost++;continue}const Hr=PV.Hraw,Hs=PV.H||Hr,Hri=inv(Hr);
  if(V.rot==null)V.rot=quarter(Hr,p).q;
  const B=stabBase(p,disp.width,disp.height,V.rot),Z=cropM(W,H),modes={before:[Hr,Ht],stab:[B,mul(B,mul(Hri,Ht))],ar:[mul(Z,Hs),mul(Z,mul(Hs,mul(Hri,Ht)))]};
  for(const m in modes){const [Ov,Ct]=modes[m],O=P.map(q=>app(Ov,q[0],q[1])),C=P.map(q=>app(Ct,q[0],q[1]));let e=0;for(let n=0;n<P.length;n++)e+=(O[n][0]-C[n][0])**2+(O[n][1]-C[n][1])**2;R[m].e.push(Math.sqrt(e/P.length));
   if(prev[m]){let a=0,b=0;for(let n=0;n<P.length;n++){a+=(O[n][0]-prev[m].O[n][0])**2+(O[n][1]-prev[m].O[n][1])**2;b+=(C[n][0]-prev[m].C[n][0])**2+(C[n][1]-prev[m].C[n][1])**2}
    R[m].o.push(Math.sqrt(a/P.length));R[m].c.push(Math.sqrt(b/P.length))}prev[m]={O:O,C:C}}
  const mid=[(f[0]+f[2])/2,(f[1]+f[3])/2];lagT.push(app(Ht,mid[0],mid[1]));lagA.push(app(Hs,mid[0],mid[1]))}
 const rms=a=>a.length?Math.sqrt(a.reduce((s,v)=>s+v*v,0)/a.length):null,mx=a=>a.length?Math.max(...a):null,out={frames:N,lost:lost,ms:performance.now()-t0};
 for(const m in R)out[m]={jitter_layer:rms(R[m].o),jitter_paper:rms(R[m].c),err:rms(R[m].e),err_max:mx(R[m].e)};
 /* lag of the smoothed pose behind the true one, in frames (best shift of the centre's path) */
 let best=0,bv=1e18;for(let s=0;s<=6;s++){let e=0,n=0;for(let i=s;i<lagA.length;i++){e+=(lagA[i][0]-lagT[i-s][0])**2+(lagA[i][1]-lagT[i-s][1])**2;n++}if(n&&e/n<bv){bv=e/n;best=s}}out.lag_frames=best;
 Object.assign(V,{demo:save.demo,W:save.W,H:save.H,cv:save.cv,zv:save.zv,rot:save.rot});FILT.reset();PV.H=null;return out}

/* ---------------------------------------------------------------- the view, the camera, the demo */
const V={cv:null,ctx:null,an:null,actx:null,ak:1,aw:1280,ah:720,W:1280,H:720,video:null,stream:null,raf:0,worker:null,busy:false,last:0,fpsT:[],demo:null,qrT:0,frameId:0,jobs:{},
 mode:(window.matchMedia&&matchMedia('(pointer:coarse)').matches)?'stab':'ar',frozen:false,zv:{k:1,x:0,y:0},rot:null,bg:null,bgOv:null,keep:null,ptr:{}};
function workerOf(){if(V.worker!==null)return V.worker;try{const src='const D=('+detectorFactory.toString()+')();onmessage=e=>{const d=e.data;let r=[];try{r=D.detect(new Uint8ClampedArray(d.buf),d.w,d.h,d.dict)}catch(x){}postMessage({id:d.id,r:r})}';
 const w=new Worker(URL.createObjectURL(new Blob([src],{type:'text/javascript'})));w.onmessage=e=>{V.busy=false;onMarkers(e.data.id,e.data.r)};w.onerror=()=>{V.worker=false;V.busy=false};V.worker=w}catch(e){V.worker=false}return V.worker}
function onMarkers(id,ms){const job=V.jobs[id];delete V.jobs[id];const k=(job&&job.k)||1;if(k!==1)for(const m of ms)m.c=m.c.map(p=>[p[0]/k,p[1]/k]);const t=performance.now()/1000;const H=track(ms,t);PV.stats.frames++;
 if(PV.Hraw)PV.stats.tracked++;if(job&&job.truth&&PV.Hraw&&PV.paper){const fc=frameCorners(PV.paper);let e=0;for(const c of fc){const a=app(PV.Hraw,c[0],c[1]),b=app(job.truth,c[0],c[1]);e+=(a[0]-b[0])**2+(a[1]-b[1])**2}
  const err=Math.sqrt(e/4);PV.err=PV.err==null?err:PV.err*.85+err*.15;PV.lastErr=err}
 if(job&&job.snap!=null&&PV.Hraw){V.shown=job.snap;V.shownH=PV.Hraw;V.shownSeen=ms;if(!V.frozen){keepFrame(V.snaps[job.snap],PV.Hraw,PV.H);renderBg()}}
 const was=PV.locked;PV.locked=!!PV.Hraw;if(PV.locked&&!was){PV.lockedAt=performance.now();if(!PV.firstLock){PV.firstLock=PV.lockedAt;PV.lockFrame=PV.stats.frames}}setState()}
function analyse(truth,st){const now=performance.now();if(V.frozen)return false;if(V.busy&&now-V.last<400)return false;
 /* the frame is kept (two buffers): when its markers come back, THIS frame is shown with its own homography - the
    picture and the live layer always belong to the same instant, as in real augmented reality, however slow the
    tracker. The visible canvas is never read (that waits for the screen). */
 const b=V.snapNext;V.snapNext=1-b;const sx=V.sctx[b];
 const pf=PV.prof||(PV.prof={}),t0=performance.now();sx.setTransform(1,0,0,1,0,0);if(V.demo)drawScene(sx,st);else sx.drawImage(V.video,0,0,V.W,V.H);
 V.actx.setTransform(1,0,0,1,0,0);V.actx.drawImage(V.snaps[b],0,0,V.aw,V.ah);const t1=performance.now();const img=V.actx.getImageData(0,0,V.aw,V.ah);const t2=performance.now();pf.adraw=t1-t0;pf.aread=t2-t1;const id=++V.frameId;V.jobs[id]={truth:truth,k:V.ak,snap:b};V.last=now;
 const w=workerOf();if(w){V.busy=true;w.postMessage({id:id,buf:img.data.buffer,w:V.aw,h:V.ah,dict:DICT},[img.data.buffer])}
 else{onMarkers(id,DET.detect(img.data,V.aw,V.ah,DICT))}return true}
function fit(){if(!V.cv)return;const vw=window.innerWidth,vh=window.innerHeight;
 if(V.mode==='stab'){const dpr=Math.min(2,window.devicePixelRatio||1),lk=Math.min(1,1800/Math.max(vw*dpr,vh*dpr)),cw=Math.max(2,Math.round(vw*dpr*lk)),ch=Math.max(2,Math.round(vh*dpr*lk));
  if(V.cv.width!==cw||V.cv.height!==ch){V.cv.width=cw;V.cv.height=ch;V.bgDirty=true}V.cv.style.width=vw+'px';V.cv.style.height=vh+'px';return}
 if(V.cv.width!==V.W||V.cv.height!==V.H){V.cv.width=V.W;V.cv.height=V.H;V.bgDirty=true}const k=Math.min(vw/V.W,vh/V.H);V.cv.style.width=Math.round(V.W*k)+'px';V.cv.style.height=Math.round(V.H*k)+'px'}
function setSize(w,h){V.W=w;V.H=h;V.cv.width=w;V.cv.height=h;V.shown=null;V.snapNext=0;V.keepHr=null;V.bgOv=null;V.rot=null;V.zv={k:1,x:0,y:0};
 if(!V.snaps){V.snaps=[document.createElement('canvas'),document.createElement('canvas')];V.sctx=V.snaps.map(c=>c.getContext('2d',{willReadFrequently:true}))}
 for(const c of V.snaps){c.width=w;c.height=h}V.ak=Math.min(1,960/Math.max(w,h));V.aw=Math.round(w*V.ak);V.ah=Math.round(h*V.ak);
 V.an.width=V.aw;V.an.height=V.ah;fit()}
function setState(){const v=$('paperView');if(!v)return;v.classList.toggle('locked',PV.locked);
 $('paperState').textContent=PV.locked?T('paper_locked'):T('paper_lost');
 const tech=$('paperTech');if(tech){const lines=[(PV.seen?PV.seen.length:0)+' '+T('paper_markers')+(PV.used?' · '+PV.used+' ✓':''),
   PV.fps?(PV.fps.toFixed(0)+' '+T('paper_fps')):''];if(V.demo&&PV.err!=null)lines.push(T('paper_err')+' '+PV.err.toFixed(2)+' '+T('paper_px'));
  if(PV.rms!=null&&PV.locked)lines.push('rms '+PV.rms.toFixed(2)+' px');tech.textContent=lines.filter(Boolean).join('\n');tech.style.whiteSpace='pre'}}
function kpis(){const el=$('paperKpis');if(!el)return;el.textContent='';const ws=((BD.state.def||{}).widgets||[]).filter(w=>w.type==='kpi'||w.type==='gauge').slice(0,3);
 for(const w of ws){const d=BD.state.data[w.id];if(!d||d.value==null)continue;const b=document.createElement('div');b.className='paperKpi';const s=document.createElement('small');s.textContent=w.title||'';
  const v=document.createElement('b');const st=w.style||{};v.textContent=Number(d.value).toLocaleString('en-US',{maximumFractionDigits:st.decimals!=null?st.decimals:2})+(st.unit?' '+st.unit:'');
  if(st.color)v.style.color=st.color;b.appendChild(s);b.appendChild(v);el.appendChild(b)}}
function msg(t,ms){const m=$('paperMsg');if(!m)return;m.textContent=t||'';m.classList.toggle('hide',!t);clearTimeout(V.msgT);if(t&&ms)V.msgT=setTimeout(()=>m.classList.add('hide'),ms)}
/* ---- demo: the paper on a desk, held by a moving hand (a real perspective camera) */
function desk(w,h){const c=document.createElement('canvas');c.width=w;c.height=h;const x=c.getContext('2d');const g=x.createLinearGradient(0,0,w,h);g.addColorStop(0,'#5a3a22');g.addColorStop(1,'#7a5233');x.fillStyle=g;x.fillRect(0,0,w,h);
 let s=7;const rnd=()=>{s=(s*16807)%2147483647;return s/2147483647};for(let i=0;i<260;i++){const y=rnd()*h,a=.04+rnd()*.09;x.strokeStyle='rgba('+(rnd()<.5?'30,18,8':'160,110,70')+','+a+')';x.lineWidth=.6+rnd()*2.2;x.beginPath();
  for(let X=0;X<=w;X+=24){const yy=y+Math.sin(X/(90+rnd()*40)+i)*3+Math.sin(X/31+i*.7)*1.2;X?x.lineTo(X,yy):x.moveTo(X,yy)}x.stroke()}
 for(let k=1;k<4;k++){x.fillStyle='rgba(20,10,4,.35)';x.fillRect(0,h*k/4-1,w,2)}
 const rg=x.createRadialGradient(w*.45,h*.35,h*.1,w*.5,h*.5,Math.max(w,h)*.75);rg.addColorStop(0,'rgba(255,230,190,.16)');rg.addColorStop(1,'rgba(0,0,0,.45)');x.fillStyle=rg;x.fillRect(0,0,w,h);return c}
function camH(p,t,W,H){const w=p.page[0],h=p.page[1],port=H>W,rad=Math.PI/180;
 const th=(port?90:0)+(-5+9*Math.sin(.21*t)+.35*Math.sin(7.3*t)+.2*Math.sin(11.1*t))*rad,ph=(22+11*Math.sin(.29*t+1)+.3*Math.sin(5.7*t))*rad,ps=(8*Math.sin(.17*t+2))*rad;
 const Z=600+70*Math.sin(.13*t),tx=18*Math.sin(.11*t+.5)+1.2*Math.sin(6.1*t),ty=12*Math.sin(.15*t)+1.2*Math.sin(5.3*t);
 const cz=Math.cos(th),sz=Math.sin(th),cx=Math.cos(ph),sx=Math.sin(ph),cy=Math.cos(ps),sy=Math.sin(ps);
 const Rz=[cz,-sz,0,sz,cz,0,0,0,1],Rx=[1,0,0,0,cx,-sx,0,sx,cx],Ry=[cy,0,sy,0,1,0,-sy,0,cy],R=mul(Rz,mul(Rx,Ry));
 const long=Math.max(W,H),f=.8*long*Z/Math.max(w,h),u0=W/2,v0=H/2;
 const M=[f*R[0]+u0*R[6],f*R[1]+u0*R[7],f*tx+u0*Z, f*R[3]+v0*R[6],f*R[4]+v0*R[7],f*ty+v0*Z, R[6],R[7],Z];
 return mul(M,[1,0,-w/2,0,1,-h/2,0,0,1])}
function drawScene(x,t,s){s=s||1;const d=V.demo,p=d.paper,W=V.W,H=V.H;x.setTransform(1,0,0,1,0,0);x.drawImage(s===1?d.desk:d.deskCpu,0,0,W*s,H*s);
 const Ht=(d.cam||camH)(p,t,W,H),Hd=s===1?Ht:mul([s,0,0,0,s,0,0,0,1],Ht);
 const pw=p.page[0],ph=p.page[1],cs=[[0,0],[pw,0],[pw,ph],[0,ph]].map(q=>app(Hd,q[0],q[1]));
 /* a soft shadow: a few grown copies of the page, each faint (no blur filter: it is slow without a GPU) */
 const cx0=(cs[0][0]+cs[2][0])/2,cy0=(cs[0][1]+cs[2][1])/2;
 for(const [g,a] of [[1.05,.07],[1.035,.08],[1.02,.1],[1.008,.14]]){x.fillStyle='rgba(0,0,0,'+a+')';x.beginPath();
  cs.forEach((q,i)=>{const X=cx0+(q[0]-cx0)*g+14,Y=cy0+(q[1]-cy0)*g+18;i?x.lineTo(X,Y):x.moveTo(X,Y)});x.closePath();x.fill()}
 const img=s===1?d.img:d.imgCpu,k=d.ppm,nx=10,ny=7;
 for(let j=0;j<ny;j++)for(let i=0;i<nx;i++){const u0=i/nx*pw,u1=(i+1)/nx*pw,v0=j/ny*ph,v1=(j+1)/ny*ph;
  for(const tri of [[[u0,v0],[u1,v0],[u1,v1]],[[u0,v0],[u1,v1],[u0,v1]]]){const dst=tri.map(q=>app(Hd,q[0],q[1])),src=tri.map(q=>[q[0]*k,q[1]*k]);
   const mx=(dst[0][0]+dst[1][0]+dst[2][0])/3,my=(dst[0][1]+dst[1][1]+dst[2][1])/3,ex=dst.map(q=>[mx+(q[0]-mx)*1.02+(q[0]>mx?.4:-.4),my+(q[1]-my)*1.02+(q[1]>my?.4:-.4)]);
   const [s0,s1,s2]=src,[d0,d1,d2]=dst,den=(s1[0]-s0[0])*(s2[1]-s0[1])-(s2[0]-s0[0])*(s1[1]-s0[1]);if(Math.abs(den)<1e-9)continue;
   const a=((d1[0]-d0[0])*(s2[1]-s0[1])-(d2[0]-d0[0])*(s1[1]-s0[1]))/den,b=((d2[0]-d0[0])*(s1[0]-s0[0])-(d1[0]-d0[0])*(s2[0]-s0[0]))/den,
    c=((d1[1]-d0[1])*(s2[1]-s0[1])-(d2[1]-d0[1])*(s1[1]-s0[1]))/den,dd=((d2[1]-d0[1])*(s1[0]-s0[0])-(d1[1]-d0[1])*(s2[0]-s0[0]))/den;
   x.save();x.beginPath();x.moveTo(ex[0][0],ex[0][1]);x.lineTo(ex[1][0],ex[1][1]);x.lineTo(ex[2][0],ex[2][1]);x.closePath();x.clip();
   x.setTransform(a,c,b,dd,d0[0]-a*s0[0]-b*s0[1],d0[1]-c*s0[0]-dd*s0[1]);
   /* only the patch of the paper under this triangle (drawing the whole image each time is what costs) */
   const sx=Math.max(0,Math.floor(Math.min(s0[0],s1[0],s2[0]))-2),sy=Math.max(0,Math.floor(Math.min(s0[1],s1[1],s2[1]))-2),
    sx1=Math.min(img.width,Math.ceil(Math.max(s0[0],s1[0],s2[0]))+2),sy1=Math.min(img.height,Math.ceil(Math.max(s0[1],s1[1],s2[1]))+2);
   if(sx1>sx&&sy1>sy)x.drawImage(img,sx,sy,sx1-sx,sy1-sy,sx,sy,sx1-sx,sy1-sy);x.restore()}}
 x.setTransform(1,0,0,1,0,0);const lg=x.createLinearGradient(cs[0][0],cs[0][1],cs[2][0],cs[2][1]);lg.addColorStop(0,'rgba(255,255,255,.07)');lg.addColorStop(.5,'rgba(255,255,255,0)');lg.addColorStop(1,'rgba(0,0,0,.08)');
 x.fillStyle=lg;x.beginPath();cs.forEach((q,i)=>i?x.lineTo(q[0],q[1]):x.moveTo(q[0],q[1]));x.closePath();x.fill();return Ht}
function loop(){if(!PV.open)return;V.raf=requestAnimationFrame(loop);const now=performance.now(),t=now/1000;
 V.fpsT.push(now);while(V.fpsT.length&&now-V.fpsT[0]>1000)V.fpsT.shift();PV.fps=V.fpsT.length;
 let truth=null;const pf=PV.prof||(PV.prof={scene:0,grab:0,blit:0,overlay:0}),av=(k,v)=>{pf[k]=pf[k]?pf[k]*.9+v*.1:v};let tA=performance.now();
 const c=V.ctx;c.setTransform(1,0,0,1,0,0);c.globalAlpha=1;
 if(!V.demo&&!(V.video&&V.video.readyState>=2)){if(V.video&&V.video.paused&&V.video.srcObject&&now-(V.kick||0)>1000){V.kick=now;V.video.play().catch(()=>{})}return}
 const st=V.demo?(V.demo.freeze!=null?V.demo.freeze:t-V.demo.t0+V.demo.phase):0;if(V.demo)truth=(V.demo.cam||camH)(V.demo.paper,st,V.W,V.H);
 analyse(truth,st);let tB=performance.now();av('grab',tB-tA);
 prepare();const lostFor=now/1000-PV.lastSeen,have=V.keepHr&&(V.frozen||lostFor<3);
 if(have&&(V.bgDirty||V.bgMode!==V.mode||!V.bgOv))renderBg();const cw=V.cv.width,ch=V.cv.height,sync=have&&!!V.bgOv;
 if(sync){c.drawImage(V.bg,0,0);if(lostFor>.6&&!V.frozen){c.fillStyle='rgba(5,8,11,.32)';c.fillRect(0,0,cw,ch)}drawOverlay(c,V.bgOv,now);
  if(V.mode==='ar'&&V.bgSeen&&!V.frozen)drawMarkers(c,V.bgSeen,now)}
 else{/* not locked yet: the camera as it is, letterboxed, so the paper can be found */c.fillStyle='#05080b';c.fillRect(0,0,cw,ch);
  const kk=Math.min(cw/V.W,ch/V.H),ox=(cw-V.W*kk)/2,oy=(ch-V.H*kk)/2;
  if(V.demo){const last=V.snaps&&V.snaps[1-V.snapNext];if(last)c.drawImage(last,ox,oy,V.W*kk,V.H*kk)}else c.drawImage(V.video,ox,oy,V.W*kk,V.H*kk);
  if(PV.seen&&PV.seen.length){c.save();c.setTransform(kk,0,0,kk,ox,oy);drawMarkers(c,PV.seen,now);c.restore()}}
 av('overlay',performance.now()-tB);
 if(!sync){const y=(now/6%ch);c.fillStyle='rgba(45,212,191,.10)';c.fillRect(0,y-24,cw,48);c.fillStyle='rgba(45,212,191,.55)';c.fillRect(0,y,cw,1.5)}
 if(now-(V.stT||0)>250){V.stT=now;setState()}
 if(!V.demo&&'BarcodeDetector' in window&&now-V.qrT>1500&&PV.paper&&PV.paper.sig){V.qrT=now;try{new BarcodeDetector({formats:['qr_code']}).detect(V.an).then(rs=>{for(const r of rs){const s=String(r.rawValue||'');
   let ok=null;if(s.indexOf('BLP1.')===0)ok=PV.papers.some(p=>s==='BLP1.'+p.id+'.'+p.code+'.'+b64u(p.sig));else if(s.indexOf('#')>0){const q=new URLSearchParams(s.slice(s.indexOf('#')+1)).get('p');if(q)ok=PV.papers.some(p=>p.id===q&&(!p.url||p.url===s))}if(ok!==null&&ok!==PV.verified){PV.verified=ok;msg(ok?T('paper_verified'):T('paper_unverified'),4000)}}}).catch(()=>{})}catch(e){}}}
function stopCam(){if(V.stream){for(const tr of V.stream.getTracks())tr.stop();V.stream=null}if(V.video){V.video.srcObject=null}}
async function startCamera(){stopCam();V.demo=null;FILT.reset();PV.H=null;PV.locked=false;$('paperCamBtn').classList.add('on');$('paperDemoBtn').classList.remove('on');
 if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){msg(T('paper_nocam'),5000);return startDemo()}
 try{const st=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1920},height:{ideal:1080}},audio:false});V.stream=st;
  if(!V.video){V.video=document.createElement('video');V.video.playsInline=true;V.video.muted=true;V.video.setAttribute('playsinline','');V.video.style.cssText='position:fixed;left:0;top:0;width:2px;height:2px;opacity:.01;pointer-events:none';($('paperView')||document.body).appendChild(V.video)}
  V.video.srcObject=st;await V.video.play();const vw=V.video.videoWidth||1280,vh=V.video.videoHeight||720,k=Math.min(1,1920/Math.max(vw,vh));setSize(Math.round(vw*k),Math.round(vh*k));msg(T('paper_search'),4500)}
 catch(e){msg(T('paper_nocam'),5000);startDemo()}}
function startDemo(){stopCam();$('paperDemoBtn').classList.add('on');$('paperCamBtn').classList.remove('on');FILT.reset();PV.H=null;PV.locked=false;PV.err=null;PV.firstLock=null;
 const port=window.innerHeight>window.innerWidth*1.1;setSize(port?720:1280,port?1280:720);
 let p=PV.papers[0]||PV.paper;if(!p){p=makePassport('A4','landscape');if(p&&!PV.papers.length)msg(T('paper_noprint'),5000)}
 if(!p){msg(T('paper_search'));return}PV.paper=p;PV.featRev=-1;const ppm=3.4,img=renderPaper(p,layersOf(p.widget),ppm,BD.lang());
 const dk=desk(V.W,V.H),cpu=(src,w,h)=>{const c=document.createElement('canvas');c.width=w;c.height=h;c.getContext('2d',{willReadFrequently:true}).drawImage(src,0,0,w,h);return c};
 /* the analysis canvas lives in memory: it gets textures of its own, so drawing never reads back from the screen */
 V.demo={paper:p,img:img,ppm:ppm,desk:dk,imgCpu:cpu(img,img.width,img.height),deskCpu:cpu(dk,V.aw,V.ah),t0:performance.now()/1000,phase:2};V.demoStart=performance.now();
 if(PV.papers.length)msg(T('paper_demo_note'),5000)}
function open(demo){const w=mapWidget();const v=$('paperView');if(!v)return;PV.open=true;v.classList.remove('hide');document.body.style.overflow='hidden';
 V.cv=$('paperCv');V.ctx=V.cv.getContext('2d',{willReadFrequently:true});if(!V.an){V.an=document.createElement('canvas');V.actx=V.an.getContext('2d',{willReadFrequently:true})}
 $('paperName').textContent=(BD.state.def||{}).title||T('paper_title');kpis();setState();tools();BD.send({t:'paper_list'});V.frozen=false;
 cancelAnimationFrame(V.raf);V.raf=requestAnimationFrame(loop);
 const camOk=!demo&&navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.isSecureContext&&!(window.top!==window.self&&/claude|anthropic/.test(document.referrer||''));
 if(camOk)startCamera();else{if(!demo)msg(T('paper_nocam'),5000);startDemo()}}
function close(){PV.open=false;cancelAnimationFrame(V.raf);stopCam();V.demo=null;const v=$('paperView');if(v)v.classList.add('hide');document.body.style.overflow='';$('paperPop').classList.add('hide')}
function tap(e){const Hs=V.bgOv;if(!Hs||!PV.feat)return;const r=V.cv.getBoundingClientRect(),x=(e.clientX-r.left)*V.cv.width/r.width,y=(e.clientY-r.top)*V.cv.height/r.height,Hi=inv(Hs);if(!Hi)return;
 const q=app(Hi,x,y),pop=$('paperPop');let hit=null;
 for(let i=PV.feat.length-1;i>=0&&!hit;i--){const it=PV.feat[i];if(it.kind!=='fill'||it.faint)continue;for(const poly of it.pg){let inside=false;const ring=poly[0];
   for(let a=0,b=ring.length-1;a<ring.length;b=a++){const xa=ring[a][0],ya=ring[a][1],xb=ring[b][0],yb=ring[b][1];if(((ya>q[1])!==(yb>q[1]))&&(q[0]<(xb-xa)*(q[1]-ya)/(yb-ya)+xa))inside=!inside}
   if(inside){hit=it;break}}}
 if(!hit){pop.classList.add('hide');return}pop.textContent='';const h=document.createElement('h4'),i=document.createElement('i');i.style.background=hit.c;h.appendChild(i);
 h.appendChild(document.createTextNode(String(hit.l||hit.layer.name||'')));pop.appendChild(h);const dl=document.createElement('dl');
 for(const k of (hit.layer.popup||[])){const key=typeof k==='string'?k:k.key,lb=typeof k==='string'?k:(k.label||k.key),v=hit.props[key];if(v==null||v==='')continue;
  const dt=document.createElement('dt');dt.textContent=lb;const dd=document.createElement('dd');dd.textContent=String(v);dl.appendChild(dt);dl.appendChild(dd)}
 pop.appendChild(dl);const vr=$('paperView').getBoundingClientRect();pop.style.left=Math.max(140,Math.min(vr.width-140,e.clientX-vr.left))+'px';pop.style.top=Math.max(150,e.clientY-vr.top)+'px';pop.classList.remove('hide');
 PV.pulse[hit.fid]=performance.now()}
function setMode(m){V.mode=m==='ar'?'ar':'stab';V.bgDirty=true;V.zv={k:1,x:0,y:0};fit();tools()}
function setFrozen(f){V.frozen=!!f;tools();msg(V.frozen?T('paper_held'):'',V.frozen?2600:1);if(!V.frozen)V.bgDirty=true}
function tools(){const mb=$('paperModeBtn'),fb=$('paperFreezeBtn');if(!mb)return;mb.textContent=V.mode==='stab'?T('paper_mode_stab'):T('paper_mode_ar');
 mb.title=V.mode==='stab'?T('paper_mode_ar'):T('paper_mode_stab');fb.textContent=V.frozen?T('paper_resume'):T('paper_freeze');fb.classList.toggle('on',V.frozen)}
function zoomAt(f,cx,cy){if(V.mode!=='stab')return;const z=V.zv,k=Math.max(1,Math.min(6,z.k*f)),g=k/z.k;z.x=cx-(cx-z.x)*g;z.y=cy-(cy-z.y)*g;z.k=k;
 if(k===1){z.x=0;z.y=0}V.bgDirty=true}
function toCanvas(e){const r=V.cv.getBoundingClientRect();return [(e.clientX-r.left)*V.cv.width/r.width,(e.clientY-r.top)*V.cv.height/r.height]}
function init(){const v=$('paperView');if(!v||v.dataset.ok)return;v.dataset.ok='1';
 $('paperModeBtn').onclick=()=>setMode(V.mode==='stab'?'ar':'stab');$('paperFreezeBtn').onclick=()=>setFrozen(!V.frozen);tools();$('paperClose').onclick=close;$('paperClose').setAttribute('aria-label',T('paper_close'));
 $('paperCamBtn').textContent=T('paper_camera');$('paperDemoBtn').textContent=T('paper_demo');$('paperCamBtn').onclick=()=>startCamera();$('paperDemoBtn').onclick=()=>startDemo();
 let down=null,moved=0,lastTap=0;const cvE=$('paperCv');
 cvE.addEventListener('pointerdown',e=>{V.ptr[e.pointerId]=[e.clientX,e.clientY];if(Object.keys(V.ptr).length===1){down=[e.clientX,e.clientY];moved=0}else down=null;try{cvE.setPointerCapture(e.pointerId)}catch(x){}});
 cvE.addEventListener('pointermove',e=>{const p0=V.ptr[e.pointerId];if(!p0)return;const ids=Object.keys(V.ptr);
  if(ids.length===2&&V.mode==='stab'){const o=V.ptr[ids[0]===String(e.pointerId)?ids[1]:ids[0]],d0=Math.hypot(p0[0]-o[0],p0[1]-o[1]),d1=Math.hypot(e.clientX-o[0],e.clientY-o[1]);
   if(d0>10){const m=toCanvas({clientX:(e.clientX+o[0])/2,clientY:(e.clientY+o[1])/2});zoomAt(d1/d0,m[0],m[1])}moved=99}
  else if(ids.length===1&&V.mode==='stab'&&V.zv.k>1){const a=toCanvas({clientX:p0[0],clientY:p0[1]}),b=toCanvas(e);V.zv.x+=b[0]-a[0];V.zv.y+=b[1]-a[1];V.bgDirty=true;moved+=Math.hypot(e.clientX-p0[0],e.clientY-p0[1])}
  else moved+=Math.hypot(e.clientX-p0[0],e.clientY-p0[1]);V.ptr[e.pointerId]=[e.clientX,e.clientY]});
 const up=e=>{delete V.ptr[e.pointerId];if(down&&moved<8){const now=performance.now();if(now-lastTap<320&&V.mode==='stab'&&V.zv.k>1){V.zv={k:1,x:0,y:0};V.bgDirty=true}else tap(e);lastTap=now}down=null};
 cvE.addEventListener('pointerup',up);cvE.addEventListener('pointercancel',e=>{delete V.ptr[e.pointerId];down=null});
 cvE.addEventListener('wheel',e=>{if(V.mode!=='stab')return;e.preventDefault();const m=toCanvas(e);zoomAt(e.deltaY<0?1.15:1/1.15,m[0],m[1])},{passive:false});
 window.addEventListener('resize',()=>{fit();V.bgDirty=true});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&PV.open)close()})}
BD.on('paper_list',m=>{const ps=Array.isArray(m.papers)?m.papers.filter(p=>p&&Array.isArray(p.m)&&Array.isArray(p.frame)&&Array.isArray(p.merc)):[];PV.papers=ps;
 if(V.demo&&ps.length&&V.demo.paper.local){startDemo()}});
BD.onBuild(()=>{init();const w=mapWidget(),b=$('paperBtn');if(w&&!b){BD.button('paperBtn',T('paper_btn'),'M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M8 9h8v6H8z',()=>open(false))}else if(!w&&b)b.remove();
 if(PV.open){$('paperName').textContent=(BD.state.def||{}).title||'';kpis()}});
BD.onData(()=>{if(PV.open){kpis();PV.featRev=-1}});
BD.paper={scene:drawScene,detect:DET.detect,pose:pose,homog:homog,apply:app,inv:inv,open:open,close:close,demo:startDemo,camH:camH,renderPaper:renderPaper,
 makePassport:makePassport,layout:layout,state:PV,view:V,dict:DICT,measure:measure,shakeCam:shakeCam,setMode:setMode,setFrozen:setFrozen,
 stabBase:stabBase,warpTo:warpTo,toScreen:(x,y)=>{if(!V.bgOv)return null;const q=app(V.bgOv,x,y),r=V.cv.getBoundingClientRect();return [r.left+q[0]*r.width/V.cv.width,r.top+q[1]*r.height/V.cv.height]}};
"""

JS = _JS.replace("__DICT__", json.dumps(_paper.DICT)).replace("__FIXED__", str(_paper.FIXED))
