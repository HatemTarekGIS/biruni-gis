"""The viewer page of "See my map", published on GitHub Pages (PAGE_URL). The link holds a
new key after "#" (never sent to any server); the page meets the sharing PC through the
Biruni bridge and asks it, over a direct encrypted channel, for the map of the view on
the screen. Pan, pinch and wheel move the last image at once; when the gesture ends the
PC draws the new view. A tap lists the features there; "my location" stays on the phone.

The page has every language in it, so it is complete on its own: public_page().
"""
import json

PAGE_URL = "https://biruni-gis.com/s/"


def share_url(key, lang="en"):
    from ..field_photos.bridge import pin

    return f"{PAGE_URL}#k={key}&i={pin(key)}&l={lang}&p=2"


TEXTS = {
    "en": {"title": "Live map", "by": "Shared live from Biruni GIS", "online": "Live", "offline": "Not connected",
           "linking": "Connecting…", "connecting": "Connecting to the computer that shares this map…",
           "lost": "The connection was lost. Reconnecting…",
           "pcoff": "The computer is not answering. The map may no longer be shared - ask for a new link.",
           "p2pfail": "This network does not allow a direct link to the computer. Try another network or Wi-Fi.",
           "ended": "Sharing of this map has ended.",
           "nokey": "Open this page with the link or QR code shown in Biruni GIS.",
           "insecure": "This browser cannot open a direct link. Use a recent Chrome, Safari, Edge or Firefox.",
           "waiting": "Waiting for the map…", "language": "Language", "layers": "Layers",
           "nolayers": "No layers are shown.", "locate": "My location", "zoomin": "Zoom in", "zoomout": "Zoom out",
           "home": "Whole map", "updated": "The map was updated", "nothing": "Nothing here",
           "noattrs": "Feature details are not shared.", "nogps": "Your location is not available.",
           "close": "Close", "more": "More features here - zoom in to pick one.",
           "viewonly": "View only - changes are made on the computer.",
           "busy": "The computer is busy - trying again…"},
    "ar": {"title": "خريطة مباشرة", "by": "تُشارك مباشرة من بيروني GIS", "online": "مباشر", "offline": "غير متصل",
           "linking": "جارٍ الاتصال…", "connecting": "جارٍ الاتصال بالحاسوب الذي يشارك هذه الخريطة…",
           "lost": "انقطع الاتصال. جارٍ إعادة الاتصال…",
           "pcoff": "الحاسوب لا يستجيب. ربما توقفت مشاركة الخريطة - اطلب رابطًا جديدًا.",
           "p2pfail": "هذه الشبكة لا تسمح بربط مباشر بالحاسوب. جرّب شبكة أخرى أو Wi-Fi.",
           "ended": "انتهت مشاركة هذه الخريطة.",
           "nokey": "افتح هذه الصفحة من الرابط أو رمز QR الظاهر في بيروني GIS.",
           "insecure": "لا يستطيع هذا المتصفح فتح ربط مباشر. استخدم نسخة حديثة من Chrome أو Safari أو Edge أو Firefox.",
           "waiting": "بانتظار الخريطة…", "language": "اللغة", "layers": "الطبقات",
           "nolayers": "لا توجد طبقات معروضة.", "locate": "مكاني", "zoomin": "تكبير", "zoomout": "تصغير",
           "home": "الخريطة كلها", "updated": "تم تحديث الخريطة", "nothing": "لا يوجد شيء هنا",
           "noattrs": "تفاصيل العناصر غير مُشارَكة.", "nogps": "موقعك غير متاح.",
           "close": "إغلاق", "more": "توجد عناصر أخرى هنا - كبّر لاختيار أحدها.",
           "viewonly": "للعرض فقط - يُجرى التعديل على الحاسوب.",
           "busy": "الحاسوب مشغول - جارٍ المحاولة مرة أخرى…"},
    "es": {"title": "Mapa en vivo", "by": "Compartido en vivo desde Biruni GIS", "online": "En vivo",
           "offline": "Sin conexión", "linking": "Conectando…",
           "connecting": "Conectando con el equipo que comparte este mapa…",
           "lost": "Se perdió la conexión. Reconectando…",
           "pcoff": "El equipo no responde. Puede que el mapa ya no se comparta: pida un enlace nuevo.",
           "p2pfail": "Esta red no permite una conexión directa con el equipo. Pruebe otra red o Wi-Fi.",
           "ended": "Se dejó de compartir este mapa.",
           "nokey": "Abra esta página con el enlace o el código QR que muestra Biruni GIS.",
           "insecure": "Este navegador no puede abrir una conexión directa. Use un Chrome, Safari, Edge o Firefox "
                       "reciente.",
           "waiting": "Esperando el mapa…", "language": "Idioma", "layers": "Capas",
           "nolayers": "No se muestra ninguna capa.", "locate": "Mi ubicación", "zoomin": "Acercar",
           "zoomout": "Alejar", "home": "Mapa completo", "updated": "El mapa se actualizó", "nothing": "Nada aquí",
           "noattrs": "Los detalles de los elementos no se comparten.", "nogps": "Su ubicación no está disponible.",
           "close": "Cerrar", "more": "Hay más elementos aquí: acerque el mapa para elegir uno.",
           "viewonly": "Solo lectura: los cambios se hacen en el equipo.",
           "busy": "El equipo está ocupado; reintentando…"},
    "pt_BR": {"title": "Mapa ao vivo", "by": "Compartilhado ao vivo pelo Biruni GIS", "online": "Ao vivo",
              "offline": "Sem conexão", "linking": "Conectando…",
              "connecting": "Conectando ao computador que compartilha este mapa…",
              "lost": "A conexão caiu. Reconectando…",
              "pcoff": "O computador não responde. O mapa pode não estar mais compartilhado - peça um novo link.",
              "p2pfail": "Esta rede não permite uma conexão direta com o computador. Tente outra rede ou Wi-Fi.",
              "ended": "O compartilhamento deste mapa terminou.",
              "nokey": "Abra esta página pelo link ou QR code mostrado no Biruni GIS.",
              "insecure": "Este navegador não consegue abrir uma conexão direta. Use um Chrome, Safari, Edge ou "
                          "Firefox recente.",
              "waiting": "Aguardando o mapa…", "language": "Idioma", "layers": "Camadas",
              "nolayers": "Nenhuma camada é exibida.", "locate": "Minha localização", "zoomin": "Aproximar",
              "zoomout": "Afastar", "home": "Mapa inteiro", "updated": "O mapa foi atualizado", "nothing": "Nada aqui",
              "noattrs": "Os detalhes das feições não são compartilhados.",
              "nogps": "Sua localização não está disponível.", "close": "Fechar",
              "more": "Há mais feições aqui - aproxime para escolher uma.",
              "viewonly": "Somente visualização - as alterações são feitas no computador.",
              "busy": "O computador está ocupado - tentando de novo…"},
    "fr": {"title": "Carte en direct", "by": "Partagée en direct depuis Biruni GIS", "online": "En direct",
           "offline": "Non connecté", "linking": "Connexion…",
           "connecting": "Connexion à l’ordinateur qui partage cette carte…",
           "lost": "Connexion perdue. Reconnexion…",
           "pcoff": "L’ordinateur ne répond pas. La carte n’est peut-être plus partagée : demandez un nouveau lien.",
           "p2pfail": "Ce réseau n’autorise pas de liaison directe avec l’ordinateur. Essayez un autre réseau ou le "
                      "Wi-Fi.",
           "ended": "Le partage de cette carte est terminé.",
           "nokey": "Ouvrez cette page avec le lien ou le code QR affiché dans Biruni GIS.",
           "insecure": "Ce navigateur ne peut pas ouvrir de liaison directe. Utilisez un Chrome, Safari, Edge ou "
                       "Firefox récent.",
           "waiting": "En attente de la carte…", "language": "Langue", "layers": "Couches",
           "nolayers": "Aucune couche n’est affichée.", "locate": "Ma position", "zoomin": "Zoom avant",
           "zoomout": "Zoom arrière", "home": "Carte entière", "updated": "La carte a été mise à jour",
           "nothing": "Rien ici", "noattrs": "Les détails des entités ne sont pas partagés.",
           "nogps": "Votre position n’est pas disponible.", "close": "Fermer",
           "more": "D’autres entités ici : zoomez pour en choisir une.",
           "viewonly": "Lecture seule : les modifications se font sur l’ordinateur.",
           "busy": "L’ordinateur est occupé, nouvel essai…"},
    "tr": {"title": "Canlı harita", "by": "Biruni GIS'ten canlı paylaşılıyor", "online": "Canlı",
           "offline": "Bağlı değil", "linking": "Bağlanıyor…",
           "connecting": "Bu haritayı paylaşan bilgisayara bağlanılıyor…",
           "lost": "Bağlantı koptu. Yeniden bağlanılıyor…",
           "pcoff": "Bilgisayar yanıt vermiyor. Harita artık paylaşılmıyor olabilir - yeni bir bağlantı isteyin.",
           "p2pfail": "Bu ağ bilgisayarla doğrudan bağlantıya izin vermiyor. Başka bir ağ veya Wi-Fi deneyin.",
           "ended": "Bu haritanın paylaşımı sona erdi.",
           "nokey": "Bu sayfayı Biruni GIS'te gösterilen bağlantı veya QR kod ile açın.",
           "insecure": "Bu tarayıcı doğrudan bağlantı açamıyor. Güncel bir Chrome, Safari, Edge veya Firefox "
                       "kullanın.",
           "waiting": "Harita bekleniyor…", "language": "Dil", "layers": "Katmanlar",
           "nolayers": "Gösterilen katman yok.", "locate": "Konumum", "zoomin": "Yakınlaştır",
           "zoomout": "Uzaklaştır", "home": "Haritanın tamamı", "updated": "Harita güncellendi",
           "nothing": "Burada bir şey yok", "noattrs": "Öğe ayrıntıları paylaşılmıyor.",
           "nogps": "Konumunuz kullanılamıyor.", "close": "Kapat",
           "more": "Burada başka öğeler de var - birini seçmek için yakınlaştırın.",
           "viewonly": "Yalnızca görüntüleme - değişiklikler bilgisayarda yapılır.",
           "busy": "Bilgisayar meşgul - yeniden deneniyor…"},
    "fa": {"title": "نقشهٔ زنده", "by": "اشتراک زنده از Biruni GIS", "online": "زنده", "offline": "متصل نیست",
           "linking": "در حال اتصال…", "connecting": "در حال اتصال به رایانه‌ای که این نقشه را به اشتراک گذاشته…",
           "lost": "اتصال قطع شد. در حال اتصال دوباره…",
           "pcoff": "رایانه پاسخ نمی‌دهد. شاید اشتراک نقشه تمام شده باشد - پیوند تازه‌ای بخواهید.",
           "p2pfail": "این شبکه اجازهٔ اتصال مستقیم به رایانه را نمی‌دهد. شبکه یا Wi-Fi دیگری را امتحان کنید.",
           "ended": "اشتراک این نقشه پایان یافت.",
           "nokey": "این صفحه را با پیوند یا کد QR نمایش‌داده‌شده در Biruni GIS باز کنید.",
           "insecure": "این مرورگر نمی‌تواند اتصال مستقیم برقرار کند. از نسخهٔ تازهٔ Chrome، Safari، Edge یا "
                       "Firefox استفاده کنید.",
           "waiting": "در انتظار نقشه…", "language": "زبان", "layers": "لایه‌ها",
           "nolayers": "هیچ لایه‌ای نمایش داده نمی‌شود.", "locate": "موقعیت من", "zoomin": "بزرگ‌نمایی",
           "zoomout": "کوچک‌نمایی", "home": "کل نقشه", "updated": "نقشه به‌روز شد", "nothing": "اینجا چیزی نیست",
           "noattrs": "جزئیات عوارض به اشتراک گذاشته نمی‌شود.", "nogps": "موقعیت شما در دسترس نیست.",
           "close": "بستن", "more": "عوارض دیگری هم اینجا هست - برای انتخاب یکی بزرگ‌نمایی کنید.",
           "viewonly": "فقط مشاهده - تغییرات روی رایانه انجام می‌شود.",
           "busy": "رایانه مشغول است - تلاش دوباره…"},
}


def public_page():
    """The complete static page for GitHub Pages (…/s/index.html)."""
    from ..field_photos.bridge import _core

    return (PAGE.replace("/*CORE*/", _core())
            .replace("__TEXTS__", json.dumps(TEXTS, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")))


PAGE = r"""<!doctype html><html lang="en" dir="ltr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#F4F6F8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E1114" media="(prefers-color-scheme: dark)">
<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex">
<meta name="referrer" content="no-referrer"><title>Biruni GIS</title>
<style>
:root{--bg:#F4F6F8;--card:#FFFFFF;--text:#14181D;--dim:#5F6B76;--line:#E1E6EB;--accent:#12A594;--ink:#FFFFFF;
 --soft:#E1F4F1;--good:#12A36E;--fair:#C98A12;--poor:#D24B4B;--shadow:0 1px 2px rgba(16,24,32,.05),0 6px 20px rgba(16,24,32,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0E1114;--card:#171B20;--text:#E8ECEF;--dim:#97A3AE;--line:#262C33;
 --accent:#2CC5B4;--ink:#05201C;--soft:#11302C;--good:#3DD39A;--fair:#E8B04B;--poor:#F07070;--shadow:0 2px 10px rgba(0,0,0,.4)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);overflow:hidden}
body{font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Arabic","Noto Sans",Tahoma,sans-serif;
 display:flex;flex-direction:column;height:100vh;height:100dvh;-webkit-font-smoothing:antialiased}
svg{width:1em;height:1em;flex:none}
button{font:inherit;cursor:pointer}
.top{flex:none;display:flex;align-items:center;justify-content:space-between;gap:10px;
 padding:calc(10px + env(safe-area-inset-top)) 14px 10px;background:var(--bg);border-bottom:1px solid var(--line);z-index:5}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:17px;letter-spacing:-.01em;min-width:0}
.brand #title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.logo{width:30px;height:30px;border-radius:10px;background:var(--accent);color:var(--ink);display:grid;place-items:center;font-size:18px;flex:none}
.tools{display:flex;gap:8px;align-items:center;flex:none}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:6px 11px;border-radius:999px;
 background:var(--card);border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.pill svg{font-size:15px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--good);box-shadow:0 0 0 3px color-mix(in srgb,var(--good) 25%,transparent)}
.dot.off{background:var(--poor)}.dot.wait{background:var(--fair);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.3}}
.stage{position:relative;flex:1;min-height:0}
#map{position:absolute;inset:0;overflow:hidden;touch-action:none;user-select:none;-webkit-user-select:none;
 cursor:grab;background:#fff;direction:ltr;outline:none}
#map.drag{cursor:grabbing}
#tiles{position:absolute;inset:0}
#tiles img{position:absolute;left:0;top:0;transform-origin:0 0;max-width:none;pointer-events:none;-webkit-user-drag:none}
#me,#acc,#pin{position:absolute;left:0;top:0;pointer-events:none}
#acc{border-radius:50%;background:rgba(47,123,245,.14);border:1px solid rgba(47,123,245,.55)}
#me{width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:#2F7BF5;border:3px solid #fff;
 box-shadow:0 0 0 1px rgba(0,0,0,.15),0 2px 6px rgba(0,0,0,.3)}
#pin{width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;border:3px solid var(--accent);
 background:color-mix(in srgb,var(--accent) 18%,transparent);animation:pop .25s ease-out}
@keyframes pop{from{transform:scale(.5);opacity:0}}
.load{position:absolute;top:0;left:0;right:0;height:3px;overflow:hidden;z-index:3;opacity:0;transition:opacity .2s;pointer-events:none}
.load.on{opacity:1}.load i{position:absolute;top:0;bottom:0;width:30%;background:var(--accent);animation:slide 1.1s linear infinite}
@keyframes slide{from{left:-30%}to{left:100%}}
.banner{position:absolute;top:10px;left:12px;right:12px;z-index:4;max-width:520px;margin:0 auto;padding:10px 13px;
 border-radius:14px;font-size:14px;line-height:1.45;background:var(--card);border:1px solid var(--line);
 border-inline-start:4px solid var(--fair);box-shadow:var(--shadow)}
.banner.end{border-inline-start-color:var(--poor)}
.waitbox{position:absolute;inset:0;display:grid;place-items:center;z-index:2;pointer-events:none}
.waitbox div{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:18px;
 padding:12px 18px;color:var(--dim);font-weight:600;box-shadow:var(--shadow)}
.spin{width:18px;height:18px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.fabs{position:absolute;inset-inline-end:12px;bottom:calc(18px + env(safe-area-inset-bottom));z-index:4;display:flex;
 flex-direction:column;gap:10px}
.grp{display:flex;flex-direction:column;border-radius:14px;overflow:hidden;border:1px solid var(--line);
 background:var(--card);box-shadow:var(--shadow)}
.fab{width:46px;height:46px;padding:0;border:0;background:var(--card);color:var(--text);display:grid;place-items:center;font-size:21px}
.grp .fab+.fab{border-top:1px solid var(--line)}
.fab:active{background:var(--soft)}.fab.on{color:var(--accent)}
.by{position:absolute;inset-inline-start:10px;bottom:calc(10px + env(safe-area-inset-bottom));z-index:3;font-size:11px;
 font-weight:600;padding:3px 9px;border-radius:999px;background:var(--card);color:var(--dim);border:1px solid var(--line);opacity:.9;
 max-width:60vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;pointer-events:none}
.sheet{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.42);display:flex;align-items:flex-end;animation:fade .2s}
.sheet.light{background:rgba(0,0,0,.12)}
.panel{width:100%;max-width:520px;max-height:72vh;overflow:auto;margin:0 auto;background:var(--card);border-radius:24px 24px 0 0;
 padding:10px 12px calc(14px + env(safe-area-inset-bottom));animation:up .25s ease-out}
.grab{width:40px;height:5px;border-radius:3px;background:var(--line);margin:0 auto 10px}
.lbl{font-size:13px;font-weight:600;color:var(--dim)}
.head{display:flex;justify-content:space-between;align-items:center;padding:0 12px 6px}
.panel button.opt{display:flex;justify-content:space-between;align-items:center;width:100%;font-size:17px;padding:13px 12px;
 border:0;border-radius:13px;background:transparent;color:var(--text);text-align:start}
.panel button.opt.sel{background:var(--soft);font-weight:700}.panel button.opt.sel::after{content:"✓";color:var(--accent)}
.x{border:0;background:transparent;color:var(--accent);font-weight:700;font-size:14px;padding:6px 4px}
.lyr{display:flex;gap:11px;align-items:center;padding:11px 12px;font-size:16px;border-radius:12px}
.lyr i{width:11px;height:11px;border-radius:3px;background:var(--accent);flex:none;opacity:.8}
.note{padding:8px 12px 2px;color:var(--dim);font-size:13px}
.feat{padding:8px 12px 14px}.feat+.feat{border-top:1px solid var(--line)}
.feat h3{margin:2px 0 0;font-size:18px;line-height:1.3;word-break:break-word}
.feat .of{font-size:12px;font-weight:600;color:var(--accent)}
dl{display:grid;grid-template-columns:minmax(84px,38%) 1fr;gap:7px 14px;margin:10px 0 0;font-size:14px}
dt{color:var(--dim);word-break:break-word}dd{margin:0;word-break:break-word;font-weight:500}
@keyframes up{from{transform:translateY(48px);opacity:0}}@keyframes fade{from{opacity:0}}
.toast{position:fixed;left:50%;bottom:calc(84px + env(safe-area-inset-bottom));z-index:8;transform:translate(-50%,16px);
 opacity:0;background:var(--text);color:var(--bg);padding:10px 18px;border-radius:999px;font-size:14px;font-weight:600;
 transition:opacity .25s,transform .25s;pointer-events:none;white-space:nowrap;max-width:90vw;overflow:hidden;text-overflow:ellipsis}
.toast.show{opacity:1;transform:translate(-50%,0)}
.hide{display:none!important}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top">
 <div class="brand"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l6-3 6 3 6-3v13l-6 3-6-3-6 3z"/><path d="M9 4v13M15 7v13"/></svg></span><span id="title">Biruni GIS</span></div>
 <div class="tools"><span class="pill" id="chip"><i class="dot wait" id="dot"></i><span id="chipText"></span></span>
 <button class="pill" id="langBtn" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M11.5 3a17 17 0 0 0 0 18M12.5 3a17 17 0 0 1 0 18"/></svg><span id="langCode"></span></button></div>
</header>
<div class="stage">
 <div id="map" tabindex="0"><div id="tiles"></div><div id="acc" class="hide"></div><div id="me" class="hide"></div><div id="pin" class="hide"></div></div>
 <div class="load" id="load"><i></i></div>
 <div class="waitbox" id="wait"><div><span class="spin"></span><span id="waitText"></span></div></div>
 <div class="banner hide" id="st"></div>
 <div class="fabs">
  <div class="grp"><button class="fab" id="layersBtn" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4M4 16l8 4 8-4"/></svg></button>
   <button class="fab" id="home" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/></svg></button></div>
  <div class="grp"><button class="fab" id="locBtn" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2"/></svg></button></div>
  <div class="grp"><button class="fab" id="zin" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
   <button class="fab" id="zout" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/></svg></button></div>
 </div>
 <div class="by" id="by"></div>
</div>
<div class="sheet hide" id="langSheet"><div class="panel"><div class="grab"></div><div class="head"><span class="lbl" id="lLang"></span></div><div id="langs"></div></div></div>
<div class="sheet hide" id="layerSheet"><div class="panel"><div class="grab"></div><div class="head"><span class="lbl" id="lLayers"></span><button class="x" type="button" data-close></button></div><div id="layerList"></div><div class="note" id="viewonly"></div></div></div>
<div class="sheet light hide" id="identSheet"><div class="panel"><div class="grab"></div><div class="head"><span class="lbl" id="lIdent"></span><button class="x" type="button" data-close></button></div><div id="identBody"></div></div></div>
<div class="toast" id="toast"></div>
<script>
/*CORE*/
/* ---- See my map: the viewer ---- */
const ALL=__TEXTS__,$=i=>document.getElementById(i);let T={},LANG='en';
const NAMES={en:'English',ar:'العربية',es:'Español',pt_BR:'Português',fr:'Français',tr:'Türkçe',fa:'فارسی'};
function pick(l){if(!l)return null;l=String(l).replace('-','_');if(ALL[l])return l;const b=l.split('_')[0].toLowerCase();
 return Object.keys(ALL).find(k=>k.split('_')[0].toLowerCase()===b)||null}
function keep(k,v){try{if(v==null)localStorage.removeItem(k);else localStorage.setItem(k,v)}catch(e){}}
function kept(k){try{return localStorage.getItem(k)}catch(e){return null}}
const HP=new URLSearchParams(location.hash.slice(1)),KEYB=HP.get('k')||'',HASHL=HP.get('l');
let ST='',INFO=null,V=null,HOME=null,CHANGED=0,LAST=null,IDENT=null,ME=null;
function label(){const t=INFO&&INFO.title?INFO.title:T.title;document.title=t+' - Biruni GIS';$('title').textContent=t;
 $('lLang').textContent=T.language;$('lLayers').textContent=T.layers;$('viewonly').textContent=T.viewonly;$('by').textContent=T.by;
 $('waitText').textContent=T.waiting;$('langCode').textContent=LANG.split('_')[0].toUpperCase();
 document.querySelectorAll('[data-close]').forEach(b=>b.textContent=T.close);
 [['zin','zoomin'],['zout','zoomout'],['home','home'],['layersBtn','layers'],['locBtn','locate'],['langBtn','language']]
  .forEach(([i,k])=>{$(i).title=T[k];$(i).setAttribute('aria-label',T[k])});drawLayers()}
function setLang(l){LANG=l;T=Object.assign({},ALL.en,ALL[l]||{});const h=document.documentElement;
 h.lang=l.replace('_','-');h.dir=(l==='ar'||l==='fa')?'rtl':'ltr';label();if(ST)say(ST)}
function chip(s){$('dot').className='dot '+s;$('chipText').textContent=s==='on'?T.online:s==='wait'?T.linking:T.offline}
function say(key){ST=key;const on=key==='connected',b=$('st');chip(on?'on':(key==='connecting'||key==='lost')?'wait':'off');
 if(on)b.classList.add('hide');else{b.textContent=T[key]||key;b.classList.toggle('end',key==='ended');b.classList.remove('hide')}}
let tt=null;function toast(s){const t=$('toast');t.textContent=s;t.classList.add('show');clearTimeout(tt);
 tt=setTimeout(()=>t.classList.remove('show'),2200)}
function busy(on){$('load').classList.toggle('on',!!on)}
/* ---- the view: centre and metres per CSS pixel in EPSG:3857 ---- */
const R=20037508.342789244;
function size(){const r=$('map').getBoundingClientRect();return [r.width||innerWidth||360,r.height||innerHeight||640]}
const clampRes=r=>Math.min(Math.max(r,0.01),2*R/256);
function toScreen(x,y){const [w,h]=size();return [(x-V.cx)/V.res+w/2,(V.cy-y)/V.res+h/2]}
function toMap(px,py){const [w,h]=size();return [V.cx+(px-w/2)*V.res,V.cy-(py-h/2)*V.res]}
function extentOf(w,h){return [V.cx-V.res*w/2,V.cy-V.res*h/2,V.cx+V.res*w/2,V.cy+V.res*h/2]}
function fit(e){const [w,h]=size();V={cx:(e[0]+e[2])/2,cy:(e[1]+e[3])/2,res:clampRes(Math.max((e[2]-e[0])/w,(e[3]-e[1])/h)*1.02||1)};place()}
function merc(lon,lat){lat=Math.max(-85.05,Math.min(85.05,lat));return [lon*R/180,Math.log(Math.tan((90+lat)*Math.PI/360))*R/Math.PI]}
function spot(el,x,y){const p=toScreen(x,y);el.style.transform='translate('+p[0]+'px,'+p[1]+'px)'}
function place(){if(!V)return;for(const img of $('tiles').children){const e=img._ext,s=(e[2]-e[0])/img._w/V.res,p=toScreen(e[0],e[3]);
  img.style.transform='translate('+p[0]+'px,'+p[1]+'px) scale('+s+')'}
 if(ME){spot($('me'),ME.x,ME.y);const a=$('acc'),r=Math.max(ME.acc/V.res,9);a.style.width=a.style.height=2*r+'px';
  a.style.margin=(-r)+'px 0 0 '+(-r)+'px';spot(a,ME.x,ME.y)}
 if(PIN)spot($('pin'),PIN[0],PIN[1])}
function zoomAt(px,py,f){const m=toMap(px,py),[w,h]=size();V.res=clampRes(V.res*f);V.cx=m[0]-(px-w/2)*V.res;V.cy=m[1]+(py-h/2)*V.res;place()}
/* ---- asking the computer for the map of this view ---- */
let reqId=0,shownId=0,identId=0,rt=null,PIN=null;const pend=new Map();
function soon(ms){clearTimeout(rt);rt=setTimeout(request,ms==null?220:ms)}
function request(){if(!live()||!V)return;const s=size(),w=Math.round(s[0]),h=Math.round(s[1]);if(w<8||h<8)return;
 const d=Math.floor(Math.max(0.25,Math.min(window.devicePixelRatio||1,2,1600/w,1600/h))*1000)/1000;
 const e=extentOf(w,h),id=++reqId;pend.set(id,{w:w,h:h,ext:e,pw:Math.round(w*d),ph:Math.round(h*d)});
 if(pend.size>20)pend.delete(pend.keys().next().value);
 busy(true);send({t:'render',id:id,x0:e[0],y0:e[1],x1:e[2],y1:e[3],w:w,h:h,d:d})}
async function show(c){const q=pend.get(c.id);pend.delete(c.id);if(!q||c.id<shownId)return;
 const blob=new Blob(c.parts,{type:c.type}),url=URL.createObjectURL(blob),img=new Image();img.alt='';img.draggable=false;
 await new Promise(r=>{img.onload=r;img.onerror=r;img.src=url});
 if(c.id<shownId||!img.naturalWidth){URL.revokeObjectURL(url);return}shownId=c.id;
 img._ext=Array.isArray(c.ext)&&c.ext.length===4&&c.ext.every(Number.isFinite)?c.ext:q.ext;img._w=q.w;img._url=url;
 img.style.width=q.w+'px';img.style.height=q.h+'px';$('tiles').appendChild(img);
 [...$('tiles').children].slice(0,-2).forEach(x=>{URL.revokeObjectURL(x._url);x.remove()});
 place();$('wait').classList.add('hide');if(c.id===reqId)busy(false);
 LAST={id:c.id,size:c.size,type:c.type,w:img.naturalWidth,h:img.naturalHeight,pw:q.pw,ph:q.ph,blob:blob}}
function onInfo(d){const first=!INFO;INFO=d;HOME=Array.isArray(d.extent)&&d.extent.length===4?d.extent:[-R,-R/2,R,R/2];
 if(/^#[0-9a-fA-F]{6}$/.test(d.bg||''))$('map').style.background=d.bg;label();if(first&&!V)fit(HOME);request()}
function askInfo(){send({t:'info',id:0})}
/* ---- a tap: what is here ---- */
function identify(p){if(!live()||!V)return;if(INFO&&INFO.attrs===false){toast(T.noattrs);return}
 const m=toMap(p[0],p[1]);PIN=m;$('pin').classList.remove('hide');place();
 send({t:'identify',id:++identId,x:m[0],y:m[1],r:V.res*12})}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function onIdent(d){IDENT=d;if(!d||d.off||!Array.isArray(d.features)||!d.features.length){PIN=null;$('pin').classList.add('hide');
  toast(!d?T.busy:d.off?T.noattrs:T.nothing);return}
 const box=$('identBody');box.textContent='';$('lIdent').textContent=d.features.length>1?d.features.length+' ×':'';
 d.features.forEach(f=>{const s=el('div','feat');s.appendChild(el('div','of',f.layer));
  if(f.title)s.appendChild(el('h3','',f.title));const dl=el('dl');
  (f.attrs||[]).forEach(a=>{dl.appendChild(el('dt','',a[0]));dl.appendChild(el('dd','',a[1]===''?'—':a[1]))});
  s.appendChild(dl);box.appendChild(s)});
 if(d.more)box.appendChild(el('div','note',T.more));openSheet('identSheet')}
function drawLayers(){const box=$('layerList');box.textContent='';const ls=INFO&&INFO.layers||[];
 if(!ls.length){box.appendChild(el('div','note',T.nolayers));return}
 ls.forEach(n=>{const r=el('div','lyr');r.appendChild(el('i'));r.appendChild(el('bdi','',n));box.appendChild(r)})}
function openSheet(id){document.querySelectorAll('.sheet').forEach(s=>s.classList.add('hide'));$(id).classList.remove('hide')}
function closeSheet(s){s.classList.add('hide');if(s.id==='identSheet'){PIN=null;$('pin').classList.add('hide')}}
/* ---- the link: relays to meet, then a direct channel to the computer ---- */
let pc=null,dc=null,sid=null,linking=false,retry=null,backoff=4000,done=false,cur=null;
const live=()=>!!(dc&&dc.readyState==='open');
function send(o){if(live())try{dc.send(JSON.stringify(o))}catch(e){}}
function drop(){const p=pc;pc=null;dc=null;linking=false;cur=null;busy(false);if(p)try{p.close()}catch(e){}}
function later(){if(done)return;clearTimeout(retry);retry=setTimeout(connect,backoff);backoff=Math.min(backoff*2,60000)}
function ended(){done=true;clearTimeout(retry);drop();say('ended')}
function onMsg(e){if(typeof e.data!=='string'){if(cur){cur.parts.push(e.data);cur.got+=e.data.byteLength;if(cur.got>cur.size)cur=null}return}
 let m;try{m=JSON.parse(e.data)}catch(x){return}if(!m||typeof m!=='object')return;
 if(m.t==='img'){cur=null;if(m.status===200&&m.size>0)cur={id:m.id,size:m.size,type:String(m.type||'image/png'),ext:m.ext,parts:[],got:0};
  else{pend.delete(m.id);if(m.id===reqId){busy(false);if(m.status===503||m.status===504){toast(T.busy);soon(2000)}}}}
 else if(m.t==='end'){if(cur&&cur.id===m.id){const c=cur;cur=null;if(c.got===c.size)show(c)}}
 else if(m.t==='info'){if(m.status===200&&m.data)onInfo(m.data)}
 else if(m.t==='ident'){if(m.id===identId)onIdent(m.status===200?m.data:null)}
 else if(m.t==='changed'){CHANGED++;toast(T.updated);askInfo()}
 else if(m.t==='bye')ended()}
async function connect(){if(done||!BK||linking||live())return;
 if(!SIG.up()){if(ST!=='lost')say('connecting');return later()}
 linking=true;if(ST!=='lost'&&ST!=='p2pfail'&&ST!=='pcoff')say('connecting');const my=sid=rid();
 try{pc=new RTCPeerConnection({iceServers:BR.ice});const ch=dc=pc.createDataChannel('map',{ordered:true}),me=pc;
  ch.binaryType='arraybuffer';
  ch.onopen=()=>{linking=false;backoff=4000;send({t:'hi',ua:String(navigator.userAgent||'').slice(0,160)});say('connected');askInfo()};
  ch.onclose=()=>{if(pc===me){drop();if(!done){say('lost');later()}}};
  ch.onmessage=onMsg;
  pc.oniceconnectionstatechange=()=>{if(pc!==me)return;if(me.iceConnectionState==='failed'){drop();say('p2pfail');later()}};
  await me.setLocalDescription(await me.createOffer());await gathered(me,3000);
  if(pc!==me)return;
  await SIG.send({t:'offer',sid:my,sdp:me.localDescription.sdp,z:canZ},true);
  await sleep(20000);
  if(pc===me&&linking&&me.signalingState==='have-local-offer'){drop();say('pcoff');later()}
 }catch(e){drop();say('lost');later()}}
async function onSignal(o){if(o.t==='answer'&&o.sid===sid&&pc&&pc.signalingState==='have-local-offer'){
 try{await pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){drop();later()}}}
/* ---- gestures: the image moves at once, the computer draws when the gesture ends ---- */
const map=$('map'),P=new Map();let G=null,tapT=null,lastTap=null;
function pt(e){const r=map.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top]}
function pinch(){const [a,b]=[...P.values()];return {d:Math.hypot(a[0]-b[0],a[1]-b[1])||1,x:(a[0]+b[0])/2,y:(a[1]+b[1])/2}}
map.addEventListener('pointerdown',e=>{if(!V)return;try{map.setPointerCapture(e.pointerId)}catch(x){}const p=pt(e);P.set(e.pointerId,p);
 clearTimeout(rt);if(P.size===1)G={start:p,last:p,t:Date.now(),moved:false};
 else if(P.size===2){const q=pinch();G={pinch:true,moved:true,d0:q.d,res0:V.res,m:toMap(q.x,q.y)}}});
map.addEventListener('pointermove',e=>{if(!P.has(e.pointerId)||!G)return;const p=pt(e);P.set(e.pointerId,p);
 if(G.pinch){if(P.size<2)return;const q=pinch(),[w,h]=size();V.res=clampRes(G.res0*G.d0/q.d);
  V.cx=G.m[0]-(q.x-w/2)*V.res;V.cy=G.m[1]+(q.y-h/2)*V.res;place();return}
 if(!G.moved&&Math.hypot(p[0]-G.start[0],p[1]-G.start[1])>6){G.moved=true;G.last=G.start;map.classList.add('drag')}
 if(G.moved){V.cx-=(p[0]-G.last[0])*V.res;V.cy+=(p[1]-G.last[1])*V.res;G.last=p;place()}});
function up(e){if(!P.has(e.pointerId))return;const p=pt(e);P.delete(e.pointerId);
 if(P.size===0){const g=G;G=null;map.classList.remove('drag');if(g&&!g.moved&&Date.now()-g.t<500)tapped(p);else soon()}
 else if(P.size===1){const q=[...P.values()][0];G={start:q,last:q,t:0,moved:true}}}
map.addEventListener('pointerup',up);map.addEventListener('pointercancel',up);
function tapped(p){if(lastTap&&Date.now()-lastTap.t<300&&Math.hypot(p[0]-lastTap.p[0],p[1]-lastTap.p[1])<30){
  clearTimeout(tapT);lastTap=null;zoomAt(p[0],p[1],0.5);soon();return}
 lastTap={t:Date.now(),p:p};clearTimeout(tapT);tapT=setTimeout(()=>{lastTap=null;identify(p)},280)}
map.addEventListener('wheel',e=>{e.preventDefault();if(!V)return;const k=e.deltaMode===1?33:e.deltaMode===2?400:1,p=pt(e);
 zoomAt(p[0],p[1],Math.exp(Math.max(-300,Math.min(300,e.deltaY*k))*0.002));soon(260)},{passive:false});
map.addEventListener('keydown',e=>{if(!V)return;const[w,h]=size(),s=V.res*80;
 if(e.key==='+'||e.key==='=')zoomAt(w/2,h/2,0.5);else if(e.key==='-')zoomAt(w/2,h/2,2);
 else if(e.key==='ArrowLeft')V.cx-=s;else if(e.key==='ArrowRight')V.cx+=s;else if(e.key==='ArrowUp')V.cy+=s;
 else if(e.key==='ArrowDown')V.cy-=s;else return;e.preventDefault();place();soon()});
$('zin').onclick=()=>{if(!V)return;const[w,h]=size();zoomAt(w/2,h/2,0.5);soon()};
$('zout').onclick=()=>{if(!V)return;const[w,h]=size();zoomAt(w/2,h/2,2);soon()};
$('home').onclick=()=>{if(HOME){fit(HOME);soon(0)}};
$('layersBtn').onclick=()=>{drawLayers();openSheet('layerSheet')};
addEventListener('resize',()=>{place();soon(300)});
/* ---- my location: read on this device only, never sent ---- */
let watchId=null;
$('locBtn').onclick=()=>{if(!navigator.geolocation||!window.isSecureContext){toast(T.nogps);return}
 if(ME&&V){V.cx=ME.x;V.cy=ME.y;place();soon(0);return}if(watchId!=null)return;$('locBtn').classList.add('on');
 watchId=navigator.geolocation.watchPosition(p=>{const c=p.coords,q=merc(c.longitude,c.latitude),first=!ME;
   ME={x:q[0],y:q[1],acc:(c.accuracy||0)/Math.cos(c.latitude*Math.PI/180)};$('me').classList.remove('hide');$('acc').classList.remove('hide');
   if(first&&V){V.cx=ME.x;V.cy=ME.y;if(V.res>4)V.res=2;soon(0)}place()},
  ()=>{toast(T.nogps);navigator.geolocation.clearWatch(watchId);watchId=null;$('locBtn').classList.remove('on')},
  {enableHighAccuracy:true,maximumAge:10000,timeout:30000})};
/* ---- sheets and language ---- */
document.querySelectorAll('.sheet').forEach(s=>s.addEventListener('click',e=>{if(e.target===s||e.target.hasAttribute('data-close'))closeSheet(s)}));
$('langBtn').onclick=()=>{const box=$('langs');box.textContent='';Object.keys(ALL).forEach(k=>{const b=el('button','opt',NAMES[k]||k);
 b.type='button';b.lang=k.replace('_','-');b.classList.toggle('sel',k===LANG);
 b.onclick=()=>{keep('biruni-lang',k);setLang(k);$('langSheet').classList.add('hide')};box.appendChild(b)});openSheet('langSheet')};
setLang(pick(kept('biruni-lang'))||pick(HASHL)||pick(navigator.language)||'en');
(async()=>{if(!KEYB||KEYB.length!==43){$('wait').classList.add('hide');say('nokey');return}
 if(!window.isSecureContext||!window.RTCPeerConnection||!window.crypto||!crypto.subtle){$('wait').classList.add('hide');say('insecure');return}
 say('connecting');await bridgeKey(KEYB);SIG.start(onSignal,n=>{if(n&&!linking&&!live()){backoff=4000;connect()}});
 addEventListener('online',()=>{backoff=4000;connect()});
 document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!live()){backoff=4000;connect()}})})();
</script></body></html>"""
