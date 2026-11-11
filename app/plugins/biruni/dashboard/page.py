"""The browser side of the live dashboard: one self-contained page (HTML, CSS and JS inline, every language
inside) that draws a dashboard and keeps it live. See SPEC.md for the messages and the data shapes.

page_html(lang, transport) gives the page for one way of reaching the PC:
  * "lan"     - served by the PC itself; messages by POST <page>/m and a long poll GET <page>/ev
  * "bridge"  - the public page on GitHub Pages (…/d/); a direct WebRTC channel to the PC, found through the
                Biruni bridge relays with the key after "#" (field_photos/bridge.py), as the "See my map" page
  * "preview" - inside Biruni (the builder): window.BiruniDash.feed(msg) in, window.BiruniDash.onSend(msg) out

Everything is drawn by the page itself (SVG and HTML, no chart or map library, no font or file from the
internet): it opens in the field, on a TV, or on a computer with no internet the same way. The QR code of a
seal is made by a small encoder in the page (byte mode, error level M).
"""
import json
import os

PAGE_URL = "https://hatemtarekgis.github.io/biruni-gis/d/"
LANGS = ("en", "ar", "es", "fr", "pt_BR", "tr", "fa")
THEMES = ("biruni-light", "biruni-dark", "desert", "ocean", "high-contrast", "tv-wall")
TRANSPORTS = ("lan", "bridge", "preview")


def page_url(card, key, lang="en"):
    """The public (bridge) address of a dashboard for one card; everything after "#" stays on the phone."""
    from ..field_photos.bridge import pin

    return f"{PAGE_URL}#c={card}&k={key}&i={pin(key)}&l={lang}"


TEXTS = {
    "en": {
        "title": "Dashboard", "by": "Live from Biruni GIS", "sharedBy": "Shared by {pc}", "live": "Live",
        "connecting": "Connecting…", "lost": "Connection lost. Reconnecting…", "offline": "Not connected",
        "waiting": "Waiting for the dashboard…", "updated": "Updated {t}", "never": "No data yet",
        "nocard": "Open this dashboard with the link or QR code from Biruni GIS.",
        "expired": "This card has expired. Ask for a new link.",
        "revoked": "This card was withdrawn by the computer that shares the dashboard.",
        "badcard": "This link is not valid for this dashboard.",
        "busy": "The computer is busy. Trying again…", "ended": "Sharing of this dashboard has ended.",
        "insecure": "This browser cannot open a secure direct link. Use a recent Chrome, Safari, Edge or Firefox.",
        "p2pfail": "This network does not allow a direct link to the computer. Try another network or Wi-Fi.",
        "pcoff": "The computer is not answering. It may be off or no longer sharing.",
        "error": "Error: {why}", "hidden": "Hidden for privacy",
        "hiddenWhy": "Too few features to show this safely.", "hiddenItem": "hidden",
        "explain": "Behind this number", "features": "{n} features", "more": "More features are not listed.",
        "nothing": "Nothing to show.", "loading": "Loading…", "byWho": "by {who}",
        "seal": "Seal", "sealTitle": "Signed number",
        "sealHint": "The computer that shares this dashboard signed this number. Anyone can check it in "
                    "Biruni GIS (Check a Seal) with the code or this QR code.",
        "sealNo": "The computer does not know this seal.",
        "sDash": "Dashboard", "sWidget": "Widget", "sTitle": "Title", "sValue": "Value", "sFilters": "Filters",
        "sCard": "Card", "sTs": "Time", "sData": "Data fingerprint", "sSig": "Signature",
        "copy": "Copy", "copied": "Copied", "close": "Close", "settings": "Settings", "theme": "Theme",
        "th_biruni-light": "Biruni light", "th_biruni-dark": "Biruni dark", "th_desert": "Desert",
        "th_ocean": "Ocean", "th_high-contrast": "High contrast", "th_tv-wall": "TV wall",
        "language": "Language", "digits": "Arabic-Indic digits", "tv": "TV mode", "print": "Print",
        "card": "Card: {name}", "expires": "Valid until {d}", "zone": "Limited to an area", "all": "All",
        "total": "{n} of {t}", "of": "{v} of {t}", "target": "{p}% of the target {t}", "vsPrev": "vs previous",
        "rank": "Rank", "reset": "Whole map",
        "legend": "Legend", "layersT": "Layers", "details": "What is behind it", "zoomTo": "Zoom to", "selected": "Selected", "clearSel": "Clear selection", "north": "N", "m": "m", "km": "km", "basemap": "Basemap",
    },
    "ar": {
        "title": "لوحة المعلومات", "by": "مباشرة من Biruni GIS", "sharedBy": "تشاركها {pc}", "live": "مباشر",
        "connecting": "جارٍ الاتصال…", "lost": "انقطع الاتصال. جارٍ إعادة الاتصال…", "offline": "غير متصل",
        "waiting": "في انتظار لوحة المعلومات…", "updated": "حُدِّثت {t}", "never": "لا توجد بيانات بعد",
        "nocard": "افتح لوحة المعلومات بالرابط أو برمز QR من Biruni GIS.",
        "expired": "انتهت صلاحية هذه البطاقة. اطلب رابطًا جديدًا.",
        "revoked": "سحب الحاسوبُ الذي يشارك لوحة المعلومات هذه البطاقة.",
        "badcard": "هذا الرابط غير صالح لهذه اللوحة.",
        "busy": "الحاسوب مشغول. جارٍ المحاولة مرة أخرى…", "ended": "انتهت مشاركة لوحة المعلومات هذه.",
        "insecure": "لا يستطيع هذا المتصفح فتح اتصال مباشر آمن. استخدم إصدارًا حديثًا من Chrome أو Safari أو Edge أو Firefox.",
        "p2pfail": "لا تسمح هذه الشبكة باتصال مباشر بالحاسوب. جرّب شبكة أخرى أو Wi-Fi.",
        "pcoff": "الحاسوب لا يستجيب. ربما أُغلق أو أوقف المشاركة.",
        "error": "خطأ: {why}", "hidden": "مخفي حفاظًا على الخصوصية",
        "hiddenWhy": "عدد المعالم أقل من أن يُعرض بأمان.", "hiddenItem": "مخفي",
        "explain": "ما وراء هذا الرقم", "features": "{n} معلمًا", "more": "توجد معالم أخرى غير مدرجة.",
        "nothing": "لا يوجد ما يُعرض.", "loading": "جارٍ التحميل…", "byWho": "بواسطة {who}",
        "seal": "الختم", "sealTitle": "رقم موقَّع",
        "sealHint": "وقّع الحاسوبُ الذي يشارك لوحة المعلومات هذا الرقم. يستطيع أي شخص التحقق منه في "
                    "Biruni GIS (التحقق من ختم) بالرمز أو برمز QR هذا.",
        "sealNo": "هذا الختم غير معروف لدى الحاسوب.",
        "sDash": "اللوحة", "sWidget": "العنصر", "sTitle": "العنوان", "sValue": "القيمة", "sFilters": "عوامل التصفية",
        "sCard": "البطاقة", "sTs": "الوقت", "sData": "بصمة البيانات", "sSig": "التوقيع",
        "copy": "نسخ", "copied": "نُسخ", "close": "إغلاق", "settings": "الإعدادات", "theme": "النمط",
        "th_biruni-light": "بيروني فاتح", "th_biruni-dark": "بيروني داكن", "th_desert": "صحراء",
        "th_ocean": "محيط", "th_high-contrast": "تباين عالٍ", "th_tv-wall": "شاشة عرض",
        "language": "اللغة", "digits": "الأرقام العربية الهندية", "tv": "وضع شاشة العرض", "print": "طباعة",
        "card": "البطاقة: {name}", "expires": "صالحة حتى {d}", "zone": "مقصورة على منطقة", "all": "الكل",
        "total": "{n} من {t}", "of": "{v} من {t}", "target": "{p}% من الهدف {t}", "vsPrev": "مقارنة بالسابق",
        "rank": "الترتيب", "reset": "الخريطة كاملة",
        "legend": "مفتاح الخريطة", "layersT": "الطبقات", "details": "ما وراء هذا", "zoomTo": "تكبير إليه", "selected": "المحدَّد", "clearSel": "إلغاء التحديد", "north": "ش", "m": "م", "km": "كم", "basemap": "خريطة الأساس",
    },
    "es": {
        "title": "Panel", "by": "En directo desde Biruni GIS", "sharedBy": "Compartido por {pc}", "live": "En directo",
        "connecting": "Conectando…", "lost": "Se perdió la conexión. Reconectando…", "offline": "Sin conexión",
        "waiting": "Esperando el panel…", "updated": "Actualizado {t}", "never": "Aún no hay datos",
        "nocard": "Abra este panel con el enlace o el código QR de Biruni GIS.",
        "expired": "Esta tarjeta ha caducado. Pida un enlace nuevo.",
        "revoked": "El ordenador que comparte el panel retiró esta tarjeta.",
        "badcard": "Este enlace no es válido para este panel.",
        "busy": "El ordenador está ocupado. Reintentando…", "ended": "Se dejó de compartir este panel.",
        "insecure": "Este navegador no puede abrir un enlace directo seguro. Use un Chrome, Safari, Edge o Firefox reciente.",
        "p2pfail": "Esta red no permite un enlace directo con el ordenador. Pruebe otra red o Wi-Fi.",
        "pcoff": "El ordenador no responde. Puede estar apagado o haber dejado de compartir.",
        "error": "Error: {why}", "hidden": "Oculto por privacidad",
        "hiddenWhy": "Hay muy pocos elementos para mostrarlo con seguridad.", "hiddenItem": "oculto",
        "explain": "Detrás de este número", "features": "{n} elementos", "more": "Hay más elementos sin listar.",
        "nothing": "Nada que mostrar.", "loading": "Cargando…", "byWho": "por {who}",
        "seal": "Sello", "sealTitle": "Número firmado",
        "sealHint": "El ordenador que comparte este panel firmó este número. Cualquiera puede comprobarlo en "
                    "Biruni GIS (Comprobar un sello) con el código o este código QR.",
        "sealNo": "El ordenador no conoce este sello.",
        "sDash": "Panel", "sWidget": "Elemento", "sTitle": "Título", "sValue": "Valor", "sFilters": "Filtros",
        "sCard": "Tarjeta", "sTs": "Hora", "sData": "Huella de los datos", "sSig": "Firma",
        "copy": "Copiar", "copied": "Copiado", "close": "Cerrar", "settings": "Ajustes", "theme": "Tema",
        "th_biruni-light": "Biruni claro", "th_biruni-dark": "Biruni oscuro", "th_desert": "Desierto",
        "th_ocean": "Océano", "th_high-contrast": "Alto contraste", "th_tv-wall": "Pantalla mural",
        "language": "Idioma", "digits": "Cifras arábigo-índicas", "tv": "Modo TV", "print": "Imprimir",
        "card": "Tarjeta: {name}", "expires": "Válida hasta {d}", "zone": "Limitada a una zona", "all": "Todo",
        "total": "{n} de {t}", "of": "{v} de {t}", "target": "{p}% del objetivo {t}", "vsPrev": "frente al anterior",
        "rank": "Puesto", "reset": "Todo el mapa",
        "legend": "Leyenda", "layersT": "Capas", "details": "Qué hay detrás", "zoomTo": "Acercar", "selected": "Seleccionado", "clearSel": "Quitar selección", "north": "N", "m": "m", "km": "km", "basemap": "Mapa base",
    },
    "fr": {
        "title": "Tableau de bord", "by": "En direct de Biruni GIS", "sharedBy": "Partagé par {pc}", "live": "En direct",
        "connecting": "Connexion…", "lost": "Connexion perdue. Reconnexion…", "offline": "Non connecté",
        "waiting": "En attente du tableau de bord…", "updated": "Mis à jour {t}", "never": "Pas encore de données",
        "nocard": "Ouvrez ce tableau de bord avec le lien ou le code QR de Biruni GIS.",
        "expired": "Cette carte a expiré. Demandez un nouveau lien.",
        "revoked": "L'ordinateur qui partage le tableau de bord a retiré cette carte.",
        "badcard": "Ce lien n'est pas valable pour ce tableau de bord.",
        "busy": "L'ordinateur est occupé. Nouvel essai…", "ended": "Le partage de ce tableau de bord est terminé.",
        "insecure": "Ce navigateur ne peut pas ouvrir de lien direct sécurisé. Utilisez un Chrome, Safari, Edge ou Firefox récent.",
        "p2pfail": "Ce réseau ne permet pas de lien direct avec l'ordinateur. Essayez un autre réseau ou le Wi-Fi.",
        "pcoff": "L'ordinateur ne répond pas. Il est peut-être éteint ou ne partage plus.",
        "error": "Erreur : {why}", "hidden": "Masqué pour la confidentialité",
        "hiddenWhy": "Trop peu d'entités pour l'afficher sans risque.", "hiddenItem": "masqué",
        "explain": "Derrière ce nombre", "features": "{n} entités", "more": "D'autres entités ne sont pas listées.",
        "nothing": "Rien à afficher.", "loading": "Chargement…", "byWho": "par {who}",
        "seal": "Sceau", "sealTitle": "Nombre signé",
        "sealHint": "L'ordinateur qui partage ce tableau de bord a signé ce nombre. Chacun peut le vérifier dans "
                    "Biruni GIS (Vérifier un sceau) avec le code ou ce code QR.",
        "sealNo": "L'ordinateur ne connaît pas ce sceau.",
        "sDash": "Tableau de bord", "sWidget": "Élément", "sTitle": "Titre", "sValue": "Valeur", "sFilters": "Filtres",
        "sCard": "Carte", "sTs": "Heure", "sData": "Empreinte des données", "sSig": "Signature",
        "copy": "Copier", "copied": "Copié", "close": "Fermer", "settings": "Réglages", "theme": "Thème",
        "th_biruni-light": "Biruni clair", "th_biruni-dark": "Biruni sombre", "th_desert": "Désert",
        "th_ocean": "Océan", "th_high-contrast": "Contraste élevé", "th_tv-wall": "Écran mural",
        "language": "Langue", "digits": "Chiffres arabes orientaux", "tv": "Mode TV", "print": "Imprimer",
        "card": "Carte : {name}", "expires": "Valable jusqu'au {d}", "zone": "Limitée à une zone", "all": "Tout",
        "total": "{n} sur {t}", "of": "{v} sur {t}", "target": "{p} % de l'objectif {t}", "vsPrev": "par rapport au précédent",
        "rank": "Rang", "reset": "Toute la carte",
        "legend": "Légende", "layersT": "Couches", "details": "Ce qu'il y a derrière", "zoomTo": "Zoomer dessus", "selected": "Sélection", "clearSel": "Effacer la sélection", "north": "N", "m": "m", "km": "km", "basemap": "Fond de carte",
    },
    "pt_BR": {
        "title": "Painel", "by": "Ao vivo do Biruni GIS", "sharedBy": "Compartilhado por {pc}", "live": "Ao vivo",
        "connecting": "Conectando…", "lost": "A conexão caiu. Reconectando…", "offline": "Sem conexão",
        "waiting": "Aguardando o painel…", "updated": "Atualizado {t}", "never": "Ainda sem dados",
        "nocard": "Abra este painel com o link ou o QR code do Biruni GIS.",
        "expired": "Este cartão expirou. Peça um novo link.",
        "revoked": "O computador que compartilha o painel retirou este cartão.",
        "badcard": "Este link não é válido para este painel.",
        "busy": "O computador está ocupado. Tentando de novo…", "ended": "O compartilhamento deste painel terminou.",
        "insecure": "Este navegador não consegue abrir um link direto seguro. Use um Chrome, Safari, Edge ou Firefox recente.",
        "p2pfail": "Esta rede não permite um link direto com o computador. Tente outra rede ou Wi-Fi.",
        "pcoff": "O computador não responde. Pode estar desligado ou ter parado de compartilhar.",
        "error": "Erro: {why}", "hidden": "Oculto por privacidade",
        "hiddenWhy": "Poucas feições para mostrar com segurança.", "hiddenItem": "oculto",
        "explain": "Por trás deste número", "features": "{n} feições", "more": "Há mais feições não listadas.",
        "nothing": "Nada para mostrar.", "loading": "Carregando…", "byWho": "por {who}",
        "seal": "Selo", "sealTitle": "Número assinado",
        "sealHint": "O computador que compartilha este painel assinou este número. Qualquer pessoa pode verificá-lo no "
                    "Biruni GIS (Verificar um selo) com o código ou este QR code.",
        "sealNo": "O computador não conhece este selo.",
        "sDash": "Painel", "sWidget": "Elemento", "sTitle": "Título", "sValue": "Valor", "sFilters": "Filtros",
        "sCard": "Cartão", "sTs": "Hora", "sData": "Impressão dos dados", "sSig": "Assinatura",
        "copy": "Copiar", "copied": "Copiado", "close": "Fechar", "settings": "Configurações", "theme": "Tema",
        "th_biruni-light": "Biruni claro", "th_biruni-dark": "Biruni escuro", "th_desert": "Deserto",
        "th_ocean": "Oceano", "th_high-contrast": "Alto contraste", "th_tv-wall": "Painel de TV",
        "language": "Idioma", "digits": "Algarismos arábico-índicos", "tv": "Modo TV", "print": "Imprimir",
        "card": "Cartão: {name}", "expires": "Válido até {d}", "zone": "Limitado a uma área", "all": "Todos",
        "total": "{n} de {t}", "of": "{v} de {t}", "target": "{p}% da meta {t}", "vsPrev": "em relação ao anterior",
        "rank": "Posição", "reset": "Mapa inteiro",
        "legend": "Legenda", "layersT": "Camadas", "details": "O que está por trás", "zoomTo": "Aproximar", "selected": "Selecionado", "clearSel": "Limpar seleção", "north": "N", "m": "m", "km": "km", "basemap": "Mapa base",
    },
    "tr": {
        "title": "Gösterge paneli", "by": "Biruni GIS'ten canlı", "sharedBy": "Paylaşan: {pc}", "live": "Canlı",
        "connecting": "Bağlanıyor…", "lost": "Bağlantı koptu. Yeniden bağlanıyor…", "offline": "Bağlı değil",
        "waiting": "Panel bekleniyor…", "updated": "Güncellendi: {t}", "never": "Henüz veri yok",
        "nocard": "Bu paneli Biruni GIS'teki bağlantı veya QR koduyla açın.",
        "expired": "Bu kartın süresi doldu. Yeni bir bağlantı isteyin.",
        "revoked": "Paneli paylaşan bilgisayar bu kartı geri aldı.",
        "badcard": "Bu bağlantı bu panel için geçerli değil.",
        "busy": "Bilgisayar meşgul. Yeniden deneniyor…", "ended": "Bu panelin paylaşımı sona erdi.",
        "insecure": "Bu tarayıcı güvenli doğrudan bağlantı açamıyor. Güncel bir Chrome, Safari, Edge veya Firefox kullanın.",
        "p2pfail": "Bu ağ bilgisayarla doğrudan bağlantıya izin vermiyor. Başka bir ağ veya Wi-Fi deneyin.",
        "pcoff": "Bilgisayar yanıt vermiyor. Kapalı olabilir ya da paylaşımı durdurmuş olabilir.",
        "error": "Hata: {why}", "hidden": "Gizlilik için gizlendi",
        "hiddenWhy": "Güvenle göstermek için çok az obje var.", "hiddenItem": "gizli",
        "explain": "Bu sayının arkasında", "features": "{n} obje", "more": "Listelenmeyen başka objeler var.",
        "nothing": "Gösterilecek bir şey yok.", "loading": "Yükleniyor…", "byWho": "{who} tarafından",
        "seal": "Mühür", "sealTitle": "İmzalı sayı",
        "sealHint": "Bu paneli paylaşan bilgisayar bu sayıyı imzaladı. Herkes kodu veya bu QR kodunu kullanarak "
                    "Biruni GIS'te (Mühür Doğrula) kontrol edebilir.",
        "sealNo": "Bilgisayar bu mührü tanımıyor.",
        "sDash": "Panel", "sWidget": "Öğe", "sTitle": "Başlık", "sValue": "Değer", "sFilters": "Filtreler",
        "sCard": "Kart", "sTs": "Zaman", "sData": "Veri parmak izi", "sSig": "İmza",
        "copy": "Kopyala", "copied": "Kopyalandı", "close": "Kapat", "settings": "Ayarlar", "theme": "Tema",
        "th_biruni-light": "Biruni açık", "th_biruni-dark": "Biruni koyu", "th_desert": "Çöl",
        "th_ocean": "Okyanus", "th_high-contrast": "Yüksek karşıtlık", "th_tv-wall": "TV duvarı",
        "language": "Dil", "digits": "Doğu Arap rakamları", "tv": "TV modu", "print": "Yazdır",
        "card": "Kart: {name}", "expires": "{d} tarihine kadar geçerli", "zone": "Bir alanla sınırlı", "all": "Tümü",
        "total": "{t} içinden {n}", "of": "{t} içinden {v}", "target": "{t} hedefinin %{p}'i", "vsPrev": "öncekine göre",
        "rank": "Sıra", "reset": "Tüm harita",
        "legend": "Lejant", "layersT": "Katmanlar", "details": "Arkasında ne var", "zoomTo": "Yakınlaştır", "selected": "Seçili", "clearSel": "Seçimi temizle", "north": "K", "m": "m", "km": "km", "basemap": "Altlık harita",
    },
    "fa": {
        "title": "داشبورد", "by": "زنده از Biruni GIS", "sharedBy": "به اشتراک گذاشته‌شده توسط {pc}", "live": "زنده",
        "connecting": "در حال اتصال…", "lost": "اتصال قطع شد. در حال اتصال دوباره…", "offline": "متصل نیست",
        "waiting": "در انتظار داشبورد…", "updated": "به‌روزرسانی {t}", "never": "هنوز داده‌ای نیست",
        "nocard": "این داشبورد را با پیوند یا کد QR از Biruni GIS باز کنید.",
        "expired": "اعتبار این کارت تمام شده است. پیوند تازه‌ای بخواهید.",
        "revoked": "رایانه‌ای که داشبورد را به اشتراک می‌گذارد این کارت را پس گرفت.",
        "badcard": "این پیوند برای این داشبورد معتبر نیست.",
        "busy": "رایانه مشغول است. تلاش دوباره…", "ended": "اشتراک این داشبورد پایان یافت.",
        "insecure": "این مرورگر نمی‌تواند پیوند مستقیم امن باز کند. از نسخهٔ تازهٔ Chrome، Safari، Edge یا Firefox استفاده کنید.",
        "p2pfail": "این شبکه اجازهٔ پیوند مستقیم با رایانه را نمی‌دهد. شبکهٔ دیگر یا Wi-Fi را امتحان کنید.",
        "pcoff": "رایانه پاسخ نمی‌دهد. شاید خاموش است یا دیگر به اشتراک نمی‌گذارد.",
        "error": "خطا: {why}", "hidden": "برای حفظ حریم خصوصی پنهان شد",
        "hiddenWhy": "تعداد عارضه‌ها برای نمایش امن کم است.", "hiddenItem": "پنهان",
        "explain": "پشت این عدد", "features": "{n} عارضه", "more": "عارضه‌های دیگری فهرست نشده‌اند.",
        "nothing": "چیزی برای نمایش نیست.", "loading": "در حال بارگذاری…", "byWho": "توسط {who}",
        "seal": "مهر", "sealTitle": "عدد امضاشده",
        "sealHint": "رایانه‌ای که این داشبورد را به اشتراک می‌گذارد این عدد را امضا کرده است. هر کسی می‌تواند آن را در "
                    "Biruni GIS (بررسی مهر) با کد یا این کد QR بررسی کند.",
        "sealNo": "رایانه این مهر را نمی‌شناسد.",
        "sDash": "داشبورد", "sWidget": "جزء", "sTitle": "عنوان", "sValue": "مقدار", "sFilters": "پالایه‌ها",
        "sCard": "کارت", "sTs": "زمان", "sData": "اثر انگشت داده", "sSig": "امضا",
        "copy": "رونوشت", "copied": "رونوشت شد", "close": "بستن", "settings": "تنظیمات", "theme": "پوسته",
        "th_biruni-light": "بیرونی روشن", "th_biruni-dark": "بیرونی تیره", "th_desert": "بیابان",
        "th_ocean": "اقیانوس", "th_high-contrast": "کنتراست بالا", "th_tv-wall": "نمایشگر دیواری",
        "language": "زبان", "digits": "ارقام فارسی", "tv": "حالت تلویزیون", "print": "چاپ",
        "card": "کارت: {name}", "expires": "معتبر تا {d}", "zone": "محدود به یک ناحیه", "all": "همه",
        "total": "{n} از {t}", "of": "{v} از {t}", "target": "{p}٪ از هدف {t}", "vsPrev": "نسبت به قبل",
        "rank": "رتبه", "reset": "کل نقشه",
        "legend": "راهنمای نقشه", "layersT": "لایه‌ها", "details": "پشت این چیست", "zoomTo": "بزرگ‌نمایی روی آن", "selected": "انتخاب‌شده", "clearSel": "لغو انتخاب", "north": "ش", "m": "م", "km": "کم", "basemap": "نقشهٔ پایه",
    },
}


# Extensions: modules of this package that add to the page. Each may define CSS, HTML (put before the script),
# JS (run after the app; it uses window.BiruniDash) and TEXTS ({lang: {key: text}}, merged into the page's texts).
EXTENSIONS = ("ext_link", "ext_mesh", "ext_sound", "ext_paper", "ext_accounts", "ext_show", "ext_guard", "ext_multi")


def extensions():
    """The extension modules that exist (a missing one is skipped)."""
    import importlib

    out = []
    for name in EXTENSIONS:
        try:
            out.append(importlib.import_module(__package__ + "." + name))
        except Exception:  # noqa: BLE001 - a broken extension is skipped, never breaks the page
            continue
    return out


def page_html(lang="en", transport="lan"):
    """The whole page as one string (UTF-8 HTML). lang is the first language shown (the viewer can change
    it); transport is how the page reaches the PC."""
    if transport not in TRANSPORTS:
        raise ValueError("transport must be one of " + ", ".join(TRANSPORTS))
    lang = lang if lang in TEXTS else "en"
    core = ""
    if transport == "bridge":
        from ..field_photos.bridge import _core

        core = _core()
    exts = extensions()
    all_texts = {k: dict(v) for k, v in TEXTS.items()}
    for x in exts:
        for lg, add in (getattr(x, "TEXTS", None) or {}).items():
            if lg in all_texts:
                all_texts[lg].update(add)
    texts = json.dumps(all_texts, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    css = CSS + "".join(getattr(x, "CSS", "") for x in exts)
    html = "".join(getattr(x, "HTML", "") for x in exts)
    js = APP_JS + "".join("\n;(function(){" + getattr(x, "JS", "") + "\n})();" for x in exts if getattr(x, "JS", ""))
    return (PAGE.replace("/*CORE*/", core)
            .replace("/*CSS*/", css)
            .replace("<!--EXT-->", html)
            .replace("/*QR*/", QR_JS)
            .replace("/*APP*/", js)
            .replace("__TEXTS__", texts)
            .replace("__TRANSPORT__", transport)
            .replace("__LANG__", lang)
            .replace("__DIR__", "rtl" if lang in ("ar", "fa") else "ltr")
            .replace("__HTMLLANG__", lang.replace("_", "-")))


def write_public_page(folder):
    """Write the public page of the bridge (…/d/index.html under folder, normally build\\bridge-page).
    -> the file's path."""
    out = os.path.join(folder, "d")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "index.html")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page_html("en", "bridge"))
    return path


PAGE = r"""<!doctype html><html lang="__HTMLLANG__" dir="__DIR__" data-theme="biruni-light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<title>Dashboard - Biruni GIS</title>
<style>/*CSS*/</style></head><body>
<a class="skip" href="#grid">Skip</a>
<header class="top" id="top">
 <div class="brand"><svg class="logo" viewBox="0 0 256 256" aria-hidden="true"><defs><linearGradient id="bgLogo" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0B4F4A"/><stop offset="1" stop-color="#14A394"/></linearGradient></defs><rect width="256" height="256" rx="58" fill="url(#bgLogo)"/><path d="M0.8 207.6 A248 248 0 0 1 255.2 207.6 A58 58 0 0 1 198 256 L58 256 A58 58 0 0 1 0.8 207.6 Z" fill="#FFFFFF" fill-opacity="0.14"/><path d="M6 204.1 A248 248 0 0 1 250 204.1" fill="none" stroke="#FFFFFF" stroke-opacity="0.85" stroke-width="7"/><path d="M40 186 L92 92 L140 176 Z" fill="#FFFFFF"/><line x1="92" y1="92" x2="226" y2="92" stroke="#FFFFFF" stroke-opacity="0.75" stroke-width="7" stroke-linecap="round" stroke-dasharray="2 16"/><line x1="92" y1="92" x2="246" y2="196" stroke="#FBBF24" stroke-width="10" stroke-linecap="round"/><path d="M140 92 A48 48 0 0 1 131.8 118.9" fill="none" stroke="#FBBF24" stroke-width="7" stroke-linecap="round"/><circle cx="92" cy="92" r="11" fill="#FBBF24" stroke="#FFFFFF" stroke-width="5"/></svg>
  <div class="ttl"><h1 id="title">Dashboard</h1><div class="sub" id="sub"></div></div></div>
 <div class="status"><button class="xpill" id="xpill" type="button"><span id="xpillText"></span><b aria-hidden="true">×</b></button><span class="conn wait" id="conn" role="status" aria-live="polite"><i class="dot"></i><span id="connText"></span></span>
  <span class="fresh" id="fresh"></span>
  <span class="extBtns" id="extBtns"></span>
  <button class="icon" id="tvBtn" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/></svg></button>
  <button class="icon" id="setBtn" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button></div>
</header>
<div class="banner hide" id="banner" role="alert"></div>
<main class="grid" id="grid" aria-busy="true"></main>
<div class="wait" id="wait"><div class="spin" aria-hidden="true"></div><p id="waitText"></p></div>
<footer class="foot" id="foot"><span id="cardInfo"></span><span id="by"></span></footer>
<div class="scrim hide" id="scrim"></div>
<section class="sheet hide" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetTitle">
 <div class="sheetHead"><h2 id="sheetTitle"></h2><button class="icon" id="sheetClose" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
 <div class="sheetBody" id="sheetBody"></div></section>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<!--EXT-->
<script>
/*CORE*/
/*QR*/
/*APP*/
</script></body></html>"""


CSS = r"""
:root{--row:64px;--gap:14px;--radius:14px;--pad:16px;
 --font:"Segoe UI",system-ui,-apple-system,"SF Pro Text",Roboto,"Noto Sans Arabic","Noto Sans",Tahoma,Arial,sans-serif;
 --big:clamp(28px,3.2vw,44px);--fs:14px}
[data-theme=biruni-light]{--bg:#F3F5F8;--surface:#FFFFFF;--surface2:#F7F9FB;--text:#17212B;--muted:#5F6B78;--line:#E3E8EE;
 --accent:#0F6E8C;--accent2:#E8F3F7;--good:#1E8E3E;--bad:#C5221F;--warn:#C77700;--shadow:0 1px 2px rgba(16,24,40,.06),0 4px 14px rgba(16,24,40,.06);
 --c1:#0F6E8C;--c2:#E37B24;--c3:#2E9D57;--c4:#8D5CC7;--c5:#D64545;--c6:#C9A227;--c7:#3E8FD8;--c8:#7A8794;
 --seq0:#E3F1F6;--seq1:#0B4F66;--map:#EEF2F5;--mapline:#FFFFFF}
[data-theme=biruni-dark]{--bg:#0D131A;--surface:#151E28;--surface2:#1B2633;--text:#E7EDF3;--muted:#93A1B0;--line:#253242;
 --accent:#4CB6D8;--accent2:#173544;--good:#4CC07A;--bad:#F26B66;--warn:#F1A43C;--shadow:0 1px 2px rgba(0,0,0,.4),0 6px 18px rgba(0,0,0,.28);
 --c1:#4CB6D8;--c2:#F29A4A;--c3:#55C483;--c4:#B48AF0;--c5:#F26B66;--c6:#E4C55A;--c7:#6FAEF2;--c8:#8E9AA7;
 --seq0:#1B2E3A;--seq1:#7FD3EE;--map:#111A23;--mapline:#0D131A}
[data-theme=desert]{--bg:#F5EEE3;--surface:#FFFBF5;--surface2:#FBF4EA;--text:#3A2A1C;--muted:#806A55;--line:#EADCC8;
 --accent:#B4652B;--accent2:#F7E6D6;--good:#5F8F3A;--bad:#B23A2A;--warn:#C98A12;--shadow:0 1px 2px rgba(80,50,20,.07),0 6px 16px rgba(80,50,20,.07);
 --c1:#B4652B;--c2:#D9A441;--c3:#6E8B3D;--c4:#8C5A7A;--c5:#B23A2A;--c6:#4E7F8A;--c7:#C9805A;--c8:#9C8B78;
 --seq0:#FBEBD6;--seq1:#8A3F12;--map:#F1E6D4;--mapline:#FFFBF5}
[data-theme=ocean]{--bg:#E9F2F7;--surface:#FFFFFF;--surface2:#F3F8FB;--text:#0E2235;--muted:#557086;--line:#D5E3EC;
 --accent:#0B5FA5;--accent2:#E2EEF8;--good:#0E8C6E;--bad:#C2364B;--warn:#D28A00;--shadow:0 1px 2px rgba(10,40,70,.06),0 6px 16px rgba(10,40,70,.07);
 --c1:#0B5FA5;--c2:#16A3A3;--c3:#5FB0E8;--c4:#2E7D5B;--c5:#E0715A;--c6:#7A6FD1;--c7:#F0B33D;--c8:#7E93A6;
 --seq0:#E1F0FA;--seq1:#073E6E;--map:#DDEBF3;--mapline:#FFFFFF}
[data-theme=high-contrast]{--bg:#000;--surface:#000;--surface2:#0A0A0A;--text:#FFF;--muted:#E0E0E0;--line:#FFF;
 --accent:#FFD400;--accent2:#222;--good:#3DFF7A;--bad:#FF5A5A;--warn:#FFB000;--shadow:none;
 --c1:#FFD400;--c2:#00E5FF;--c3:#3DFF7A;--c4:#FF7AE6;--c5:#FF5A5A;--c6:#FFFFFF;--c7:#7AA8FF;--c8:#C0C0C0;
 --seq0:#202020;--seq1:#FFD400;--map:#000;--mapline:#FFF}
[data-theme=high-contrast] .w{border:2px solid var(--line)}
[data-theme=tv-wall]{--bg:#05080C;--surface:#0C131B;--surface2:#111B25;--text:#F2F7FB;--muted:#9AB0C2;--line:#1B2836;
 --accent:#2EC4F1;--accent2:#0F2A38;--good:#38E08A;--bad:#FF6B6B;--warn:#FFC145;--shadow:0 0 0 1px #13202C;
 --c1:#2EC4F1;--c2:#FFC145;--c3:#38E08A;--c4:#B98CFF;--c5:#FF6B6B;--c6:#5EF2D0;--c7:#FF9B5E;--c8:#8BA0B3;
 --seq0:#0F2533;--seq1:#2EC4F1;--map:#081018;--mapline:#05080C;--row:76px;--fs:16px;--big:clamp(34px,3.8vw,60px)}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);font-size:var(--fs);
 -webkit-text-size-adjust:100%;font-variant-numeric:tabular-nums}
body{min-height:100vh;display:flex;flex-direction:column}
button{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:6px}
.skip{position:absolute;inset-inline-start:-999px;top:0}.skip:focus{inset-inline-start:8px;z-index:9;background:var(--surface);padding:6px 10px}
.hide{display:none!important}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:12px;
 padding:12px max(16px,env(safe-area-inset-right)) 12px max(16px,env(safe-area-inset-left));
 background:color-mix(in srgb,var(--bg) 86%,transparent);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{width:36px;height:36px;flex:none;display:block}
.ttl{min-width:0}.ttl h1,.sub,.w>header h2,.lb{unicode-bidi:plaintext}[dir=rtl] .ttl h1,[dir=rtl] .sub,[dir=rtl] .w>header h2{text-align:right}.ttl h1{margin:0;font-size:clamp(17px,1.6vw,22px);font-weight:700;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status{display:flex;align-items:center;gap:8px;flex:none}
.conn{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;background:var(--surface);border:1px solid var(--line);font-size:12.5px;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);position:relative;display:inline-block}
.conn.on .dot{background:var(--good)}.conn.on .dot::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid var(--good);opacity:0;animation:pulse 2.2s ease-out infinite}
.conn.wait .dot{background:var(--warn)}.conn.off .dot{background:var(--bad)}
@keyframes pulse{0%{transform:scale(.6);opacity:.7}100%{transform:scale(1.8);opacity:0}}
.fresh{font-size:12px;color:var(--muted);padding:4px 9px;border-radius:999px;white-space:nowrap}
.fresh.amber{color:var(--warn);background:color-mix(in srgb,var(--warn) 12%,transparent)}
.fresh.red{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}
.icon{width:36px;height:36px;display:inline-grid;place-items:center;border-radius:10px;border:1px solid var(--line);background:var(--surface);cursor:pointer;padding:0}
.icon svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.icon:hover{border-color:var(--accent)}
.banner{margin:12px 16px 0;padding:12px 14px;border-radius:12px;background:color-mix(in srgb,var(--warn) 14%,var(--surface));border:1px solid color-mix(in srgb,var(--warn) 40%,transparent);font-weight:600}
.banner.end{background:color-mix(in srgb,var(--bad) 12%,var(--surface));border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
.grid{flex:1;display:grid;grid-template-columns:repeat(12,minmax(0,1fr));grid-auto-rows:var(--row);gap:var(--gap);
 padding:var(--gap) max(16px,env(safe-area-inset-right)) var(--gap) max(16px,env(safe-area-inset-left));align-content:start}
.w{grid-column:var(--x)/span var(--w);grid-row:var(--y)/span var(--h);background:var(--surface);border-radius:var(--radius);box-shadow:var(--shadow);
 border:1px solid var(--line);display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;position:relative}
.w>header{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;padding:12px var(--pad) 0}
.w>header h2{margin:0;font-size:13px;font-weight:650;color:var(--muted);text-transform:none;letter-spacing:.01em;line-height:1.35;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.sealb{flex:none;border:1px dashed color-mix(in srgb,var(--muted) 55%,transparent);background:transparent;color:var(--muted);border-radius:7px;
 font:600 10.5px/1 ui-monospace,"Cascadia Mono",Consolas,monospace;padding:4px 6px;cursor:pointer;letter-spacing:.04em;direction:ltr}
.sealb:hover{color:var(--accent);border-color:var(--accent)}
.body{flex:1;min-height:0;padding:8px var(--pad) var(--pad);position:relative;display:flex;flex-direction:column}
.w.hid .body>*{visibility:hidden}.hidmsg{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;color:var(--muted);text-align:center;padding:12px;visibility:visible!important}
.hidmsg svg{width:26px;height:26px;fill:none;stroke:currentColor;stroke-width:1.7}
.hidmsg small{font-size:11.5px;opacity:.85}
.tap{cursor:pointer;border-radius:8px;background:none;border:0;padding:0;text-align:inherit;color:inherit;font:inherit}
.tap:hover{background:color-mix(in srgb,var(--accent) 7%,transparent)}
/* kpi */
.kpi{display:flex;flex-direction:column;justify-content:center;gap:6px;flex:1;min-height:0}
.kv{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.kval{font-size:var(--big);font-weight:750;letter-spacing:-.02em;line-height:1.05;padding:0 2px}
.kunit{font-size:15px;color:var(--muted);font-weight:600}
.kicon{position:absolute;inset-inline-end:var(--pad);bottom:var(--pad);width:38px;height:38px;border-radius:11px;display:grid;place-items:center;background:color-mix(in srgb,var(--wc) 13%,transparent);color:var(--wc)}
.kicon svg{width:21px;height:21px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.delta{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;font-weight:650;padding:3px 8px;border-radius:999px;width:max-content}
.delta.up{color:var(--good);background:color-mix(in srgb,var(--good) 12%,transparent)}.delta.down{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}
.delta.flat{color:var(--muted);background:var(--surface2)}.delta small{font-weight:500;opacity:.8}
.tgt{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted);max-width:92%}
.bar{height:7px;border-radius:99px;background:var(--surface2);overflow:hidden;border:1px solid var(--line)}
.bar>i{display:block;height:100%;width:0;border-radius:99px;background:var(--wc);transition:width .7s cubic-bezier(.2,.8,.2,1)}
/* svg charts */
svg.ch{width:100%;height:100%;display:block;overflow:visible;direction:ltr}
.ch text{fill:var(--muted);font-size:11px;font-family:var(--font)}
.ch .grid line{stroke:var(--line);stroke-width:1}
.ch .axis{stroke:var(--line)}
.cwrap{flex:1;min-height:0;position:relative}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);padding-top:6px}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:10px;height:10px;border-radius:3px;display:inline-block}
.tip{position:absolute;pointer-events:none;background:var(--text);color:var(--surface);font-size:12px;padding:6px 9px;border-radius:8px;white-space:nowrap;z-index:3;transform:translate(-50%,-110%);opacity:0;transition:opacity .15s}
.tip.on{opacity:.96}
.gauge{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.gauge svg{width:100%;max-width:320px;height:100%;flex:1;min-height:0}
.ch text.gnum{font-size:34px;font-weight:750;fill:var(--text);letter-spacing:-.02em}
.ch text.gsub{font-size:12px}
.spark{display:flex;flex-direction:column;flex:1;min-height:0}.spark .kval{font-size:calc(var(--big)*.8)}
/* bars (html) */
.hb{display:flex;flex-direction:column;gap:8px;overflow:auto;flex:1;min-height:0;padding-inline-end:2px}
.hrow{display:grid;grid-template-columns:minmax(64px,34%) 1fr auto;align-items:center;gap:10px;font-size:12.5px;width:100%}
.hrow .lb{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hrow .tr{height:16px;border-radius:6px;background:var(--surface2);overflow:hidden}
.hrow .tr>i{display:block;height:100%;width:0;border-radius:6px;background:var(--ic,var(--wc));transition:width .7s cubic-bezier(.2,.8,.2,1)}
.hrow .vl{font-weight:650;min-width:3ch;text-align:end}
.hatch{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--muted) 35%,transparent) 0 4px,transparent 4px 8px)!important;width:100%!important}
.vb{flex:1;min-height:0;display:flex;align-items:stretch;gap:8px;padding-top:16px;
 background:linear-gradient(var(--line),var(--line)) 0 100%/100% 1px no-repeat}
.vcol{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:4px;font-size:11.5px}
.vcol .vl{font-weight:650;font-size:12px}
.vcol .tr{width:min(54px,80%);flex:1;display:flex;align-items:flex-end;min-height:0}
.vcol .tr>i{display:block;width:100%;height:0;border-radius:6px 6px 2px 2px;background:var(--ic,var(--wc));transition:height .7s cubic-bezier(.2,.8,.2,1)}
.vcol .lb{color:var(--muted);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pie{flex:1;min-height:0;display:flex;align-items:center;gap:16px}
.pie svg.ch{width:auto;height:100%;max-height:230px;max-width:55%;aspect-ratio:1;flex:none}
.pie .legend{flex-direction:column;gap:6px;padding:0;flex:1;min-width:0;overflow:auto;max-height:100%}
.pie .legend button{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;width:100%;font-size:12.5px;padding:3px 4px}
.pie .legend b{font-weight:650;color:var(--text)}
.ch text.ptot{font-size:26px;font-weight:750;fill:var(--text)}
/* table */
.tbl{flex:1;min-height:0;overflow:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:8px 10px;text-align:start;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:0;background:var(--surface2);font-weight:650;color:var(--muted);cursor:pointer;user-select:none;z-index:1}
th[aria-sort=ascending]::after{content:" ▲";font-size:9px}th[aria-sort=descending]::after{content:" ▼";font-size:9px}
tbody tr:hover{background:color-mix(in srgb,var(--accent) 5%,transparent)}td.num{text-align:end}
.tfoot{font-size:11.5px;color:var(--muted);padding-top:6px}
/* map */
.map{flex:1;min-height:0;position:relative;border-radius:10px;overflow:hidden;background:var(--map);touch-action:none;border:1px solid var(--line)}
.map svg{width:100%;height:100%;display:block;cursor:grab}
.map path{vector-effect:non-scaling-stroke;stroke-linejoin:round}
.map .f:hover{filter:brightness(1.12)}
.mapbtn{position:absolute;inset-inline-end:8px;top:8px;display:flex;flex-direction:column;gap:6px}
.mapbtn .icon{width:32px;height:32px;box-shadow:var(--shadow)}
.mleg{position:absolute;inset-inline-start:8px;bottom:8px;background:color-mix(in srgb,var(--surface) 92%,transparent);border:1px solid var(--line);border-radius:9px;padding:6px 9px;font-size:11.5px;max-width:70%}
.map{--sel:var(--accent)}.map .gl path,.map .gl circle{transition:fill .6s,opacity .35s,fill-opacity .35s}
.map .bm{opacity:1;transition:opacity .4s}.map.panning .lab{opacity:0}.map .lab{position:absolute;inset:0;pointer-events:none;width:100%;height:100%;transition:opacity .2s}
.map .lab text{font-size:11px;font-weight:650;fill:var(--text);stroke:var(--surface);stroke-width:2.6px;paint-order:stroke;stroke-linejoin:round}
.map .lab text.pt{font-weight:600;font-size:10.5px}.map .lab text.big{font-size:14px;font-weight:800;letter-spacing:.01em;opacity:.92}
.map .xoff{opacity:.13}.map .xon{filter:drop-shadow(0 0 3px var(--sel))}
.map .pulse{animation:mpulse 1.4s ease-out}
@keyframes mpulse{0%{stroke:#fff;stroke-width:7px;stroke-opacity:1}100%{stroke-width:1px}}
.map .hl path,.map .hl circle{fill:none;stroke:var(--sel);stroke-width:3.5px;vector-effect:non-scaling-stroke;filter:drop-shadow(0 0 4px var(--sel))}
.gleg{position:absolute;inset-inline-start:8px;bottom:8px;max-width:min(260px,72%);max-height:calc(100% - 60px);display:flex;flex-direction:column;background:color-mix(in srgb,var(--surface) 94%,transparent);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);font-size:11.5px;backdrop-filter:blur(6px)}
.gleg>button{display:flex;align-items:center;justify-content:space-between;gap:10px;background:none;border:0;color:var(--text);font:inherit;font-weight:700;padding:7px 10px;cursor:pointer}
.gleg>button svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:2;transition:transform .2s}.gleg.closed>button svg{transform:rotate(180deg)}
.gleg .lb2{overflow:auto;padding:0 10px 8px;display:flex;flex-direction:column;gap:7px}.gleg.closed .lb2{display:none}
.gleg .ly{display:flex;flex-direction:column;gap:3px}.gleg .lyh{display:flex;align-items:center;gap:6px;font-weight:650;cursor:pointer}
.gleg .lyh input{margin:0;accent-color:var(--accent)}.gleg .it{display:flex;align-items:center;gap:7px;padding-inline-start:20px;color:var(--muted)}
.gleg .sw{width:12px;height:12px;border-radius:3px;flex:none;border:1px solid color-mix(in srgb,var(--text) 25%,transparent)}
.gleg .sw.ln{height:3px;border:0;border-radius:2px}.gleg .sw.pt{border-radius:50%}
.mscale{position:absolute;inset-inline-end:8px;bottom:8px;font-size:10.5px;color:var(--text);display:flex;flex-direction:column;align-items:flex-end;gap:2px;pointer-events:none;background:color-mix(in srgb,var(--surface) 82%,transparent);border-radius:7px;padding:3px 7px 4px}
.mscale i{display:block;height:6px;border:2px solid var(--text);border-top:0;box-shadow:0 1px 0 var(--surface)}
.map svg.mnorth{position:absolute;inset-inline-end:8px;bottom:44px;width:28px;height:36px;padding:3px;box-sizing:border-box;pointer-events:none;background:color-mix(in srgb,var(--surface) 82%,transparent);border-radius:7px}
.map svg.lab{position:absolute;inset:0;width:100%;height:100%}
.mpop{position:absolute;z-index:3;min-width:180px;max-width:min(300px,85%);background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.25);padding:10px 12px;font-size:12.5px}
.mpop h4{margin:0 0 6px;font-size:13.5px;display:flex;align-items:center;gap:7px}.mpop h4 i{width:11px;height:11px;border-radius:3px;flex:none}
.mpop .x{position:absolute;inset-inline-end:6px;top:6px;background:none;border:0;color:var(--muted);cursor:pointer;font-size:16px;line-height:1}
.mpop dl{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin:0 0 8px}.mpop dt{color:var(--muted)}.mpop dd{margin:0;font-weight:600;overflow-wrap:anywhere}
.mpop .acts{display:flex;gap:6px;flex-wrap:wrap}.mpop .acts button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--surface2);color:var(--text);border-radius:8px;padding:4px 9px;cursor:pointer}
.mpop small{color:var(--muted);display:block;margin-bottom:6px}
.xpill{display:none;align-items:center;gap:6px;border:1px solid var(--accent);color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent);border-radius:999px;padding:3px 6px 3px 11px;font-size:12px;font-weight:650;cursor:pointer;max-width:240px}
.xpill.on{display:inline-flex}.xpill span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.xpill b{font-size:14px;line-height:1;padding:0 4px}
.xsel{outline:2px solid var(--accent);outline-offset:1px;border-radius:6px}.xdim{opacity:.38}
.grad{height:8px;width:150px;max-width:100%;border-radius:4px;margin:4px 0 2px}
.mleg .rng{display:flex;justify-content:space-between;color:var(--muted);gap:12px}
/* progress, leaderboard, feed */
.plist{display:flex;flex-direction:column;gap:11px;overflow:auto;flex:1;min-height:0}
.prow{display:flex;flex-direction:column;gap:5px;width:100%;padding:3px 4px}
.prow .top2{display:flex;justify-content:space-between;gap:8px;font-size:12.5px}.prow .top2 b{font-weight:650}
.prow .bar{height:9px}.prow small{color:var(--muted)}
.lrow{display:grid;grid-template-columns:28px minmax(60px,1fr) 1.2fr auto;align-items:center;gap:10px;font-size:13px;width:100%;padding:4px}
.rk{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:12px;background:var(--surface2);color:var(--muted)}
.rk.r1{background:#F5C542;color:#3B2F00}.rk.r2{background:#C8D0D8;color:#27313B}.rk.r3{background:#D9955B;color:#3A1D05}
.feed{list-style:none;margin:0;padding:0;overflow:auto;flex:1;min-height:0;display:flex;flex-direction:column}
.feed li{display:grid;grid-template-columns:30px 1fr auto;gap:10px;align-items:start;padding:8px 2px;border-bottom:1px solid var(--line);font-size:12.5px}
.feed li.new{animation:slidein .5s ease-out}
@keyframes slidein{from{opacity:0;transform:translateY(-6px);background:color-mix(in srgb,var(--accent) 12%,transparent)}to{opacity:1;transform:none}}
.av{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700;font-size:12px}
.feed .what b{font-weight:650}.feed .lay{display:inline-block;margin-inline-start:6px;font-size:11px;color:var(--muted);background:var(--surface2);border-radius:6px;padding:1px 6px}
.feed time{color:var(--muted);font-size:11.5px;white-space:nowrap}
.txt{font-size:14px;line-height:1.6;overflow:auto}.txt p{margin:0 0 8px}
.t-text{background:linear-gradient(135deg,var(--accent2),var(--surface))}
.chips{display:flex;flex-wrap:wrap;gap:8px;align-content:flex-start;overflow:auto}
.chip{border:1px solid var(--line);background:var(--surface2);border-radius:999px;padding:6px 12px;font-size:12.5px;cursor:pointer;display:inline-flex;gap:6px;align-items:center}
.chip small{color:var(--muted)}.chip[aria-pressed=true]{background:var(--accent);color:#fff;border-color:var(--accent)}
[data-theme=high-contrast] .chip[aria-pressed=true]{color:#000}
.chip[aria-pressed=true] small{color:inherit;opacity:.8}
.empty{color:var(--muted);font-size:12.5px;margin:auto;text-align:center}
/* waiting, footer, sheet, toast */
.wait{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:var(--muted);background:var(--bg);z-index:4;top:62px}
.spin{width:34px;height:34px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.foot{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:10px max(16px,env(safe-area-inset-right)) calc(14px + env(safe-area-inset-bottom));font-size:12px;color:var(--muted)}
.foot .zb{display:inline-block;margin-inline-start:8px;padding:2px 8px;border-radius:99px;background:var(--accent2);color:var(--accent);font-weight:600}
.scrim{position:fixed;inset:0;background:rgba(5,10,15,.42);z-index:7}
.sheet{position:fixed;z-index:8;inset-inline:0;bottom:0;max-height:86vh;background:var(--surface);color:var(--text);border-radius:18px 18px 0 0;
 box-shadow:0 -10px 40px rgba(0,0,0,.25);display:flex;flex-direction:column;animation:up .22s ease-out}
@keyframes up{from{transform:translateY(30px);opacity:0}}
@media (min-width:761px){.sheet{inset:auto;left:50%;top:50%;bottom:auto;transform:translate(-50%,-50%);width:min(560px,92vw);border-radius:18px;animation:none}}
.sheetHead{display:flex;align-items:center;justify-content:space-between;padding:14px 16px 8px;gap:12px}.sheetHead h2{margin:0;font-size:17px}
.sheetBody{padding:4px 16px 18px;overflow:auto}
.kvl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px;margin:8px 0}.kvl dt{color:var(--muted)}.kvl dd{margin:0;word-break:break-word}
.mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:11.5px;direction:ltr;unicode-bidi:embed}
.qr{display:flex;justify-content:center;margin:12px 0}.qr svg{width:min(240px,70vw);height:auto;background:#fff;padding:0;border-radius:8px}
.flist{list-style:none;margin:8px 0;padding:0}.flist li{padding:8px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;font-size:13px}
.flist small{color:var(--muted)}
.opts{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:6px 0 14px}
.opt{display:flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:12px;padding:10px;background:var(--surface2);cursor:pointer;text-align:start}
.opt[aria-pressed=true]{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 35%,transparent)}
.sw{display:flex;gap:3px}.sw i{width:12px;height:22px;border-radius:4px;display:block}
.row2{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.btn{border:1px solid var(--line);background:var(--surface2);border-radius:10px;padding:8px 14px;cursor:pointer;font-weight:600}
.btn.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.sec{font-size:12px;font-weight:700;color:var(--muted);margin:10px 0 4px;text-transform:uppercase;letter-spacing:.05em}
.toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,20px);background:var(--text);color:var(--surface);padding:9px 16px;border-radius:10px;font-size:13px;opacity:0;transition:.25s;z-index:9;pointer-events:none}
.toast.show{opacity:.95;transform:translate(-50%,0)}
/* phones: 2 columns, the definition's order (by row, then column) */
@media (max-width:760px){
 .grid{grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:auto;gap:10px;padding:10px 12px}
 .w{grid-column:span 2;grid-row:auto;min-height:calc(var(--h) * 58px)}
 .w.small{grid-column:span 1;min-height:132px}
 .w.small .kval{font-size:28px}.w.small .kicon{display:none}
 .fresh{display:none}.sub{font-size:11.5px}.top{padding:10px 12px}
 .hrow{grid-template-columns:minmax(56px,38%) 1fr auto}
}
@media (max-width:380px){.w.small{grid-column:span 2}}
/* TV / wall */
body.tv{--fs:17px;--row:84px;--big:clamp(38px,4.2vw,72px)}
body.tv .top{position:static}body.tv #setBtn,body.tv .foot{display:none}body.tv .w>header h2{font-size:15px}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media print{.top{position:static;background:#fff;backdrop-filter:none}.status .icon,.status .fresh,.sheet,.scrim,.toast,.wait,.skip{display:none!important}
 html,body{background:#fff!important;color:#000}.grid{grid-template-columns:repeat(2,1fr);grid-auto-rows:auto}
 .w{grid-column:span 1!important;grid-row:auto!important;break-inside:avoid;box-shadow:none;min-height:calc(var(--h)*52px)}
 .w.wide{grid-column:span 2!important}}
"""


# A small QR code encoder (ISO/IEC 18004): byte mode, error level M, versions 1-40, the mask with the lowest
# penalty. Written for this page; the tables are the standard's.
QR_JS = r"""
const QR=(()=>{
 const ECC={L:[-1,7,10,15,20,26,18,20,24,30,18,20,24,26,30,22,24,28,30,28,28,28,28,30,30,26,28,30,30,30,30,30,30,30,30,30,30,30,30,30,30],
  M:[-1,10,16,26,18,24,16,18,22,22,26,30,22,22,24,24,28,28,26,26,26,26,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28]};
 const BLK={L:[-1,1,1,1,1,1,2,2,2,2,4,4,4,4,4,6,6,6,6,7,8,8,9,9,10,12,12,12,13,14,15,16,17,18,19,19,20,21,22,24,25],
  M:[-1,1,1,1,2,2,4,4,4,5,5,5,8,9,9,10,10,11,13,14,16,17,17,18,20,21,23,25,26,28,29,31,33,35,37,38,40,43,45,47,49]};
 const FMT={L:1,M:0};
 function raw(v){let r=(16*v+128)*v+64;if(v>=2){const n=Math.floor(v/7)+2;r-=(25*n-10)*n-55;if(v>=7)r-=36}return r}
 const dataCw=(v,e)=>Math.floor(raw(v)/8)-ECC[e][v]*BLK[e][v];
 function gmul(x,y){let z=0;for(let i=7;i>=0;i--){z=(z<<1)^((z>>>7)*0x11D);z^=((y>>>i)&1)*x}return z&255}
 function divisor(n){const r=new Array(n).fill(0);r[n-1]=1;let root=1;
  for(let i=0;i<n;i++){for(let j=0;j<n;j++){r[j]=gmul(r[j],root);if(j+1<n)r[j]^=r[j+1]}root=gmul(root,2)}return r}
 function rem(data,div){const r=new Array(div.length).fill(0);
  for(const b of data){const f=b^r.shift();r.push(0);for(let i=0;i<div.length;i++)r[i]^=gmul(div[i],f)}return r}
 function align(v){if(v===1)return[];const n=Math.floor(v/7)+2,step=v===32?26:Math.ceil((v*4+4)/(n*2-2))*2,r=[6];
  for(let p=v*4+10;r.length<n;p-=step)r.splice(1,0,p);return r}
 function make(bytes,ecl){ecl=ecl||'M';let v=1;
  for(;v<=40;v++){const cc=v<10?8:16;if(4+cc+bytes.length*8<=dataCw(v,ecl)*8)break}
  if(v>40)return null;
  const cap=dataCw(v,ecl)*8,bits=[];const put=(val,n)=>{for(let i=n-1;i>=0;i--)bits.push((val>>>i)&1)};
  put(4,4);put(bytes.length,v<10?8:16);for(const b of bytes)put(b,8);
  put(0,Math.min(4,cap-bits.length));while(bits.length%8)bits.push(0);
  for(let p=0xEC;bits.length<cap;p^=0xEC^0x11)put(p,8);
  const data=[];for(let i=0;i<bits.length;i+=8){let b=0;for(let j=0;j<8;j++)b=(b<<1)|bits[i+j];data.push(b)}
  const nb=BLK[ecl][v],el=ECC[ecl][v],rc=Math.floor(raw(v)/8),ns=nb-rc%nb,sl=Math.floor(rc/nb),div=divisor(el),blocks=[];
  for(let i=0,k=0;i<nb;i++){const d=data.slice(k,k+sl-el+(i<ns?0:1));k+=d.length;const e=rem(d,div);if(i<ns)d.push(0);blocks.push(d.concat(e))}
  const cw=[];for(let i=0;i<blocks[0].length;i++)for(let j=0;j<blocks.length;j++)if(i!==sl-el||j>=ns)cw.push(blocks[j][i]);
  const size=v*4+17,M=[],F=[];for(let y=0;y<size;y++){M.push(new Array(size).fill(0));F.push(new Array(size).fill(false))}
  const set=(x,y,d)=>{M[y][x]=d?1:0;F[y][x]=true};
  for(let i=0;i<size;i++){set(6,i,i%2===0);set(i,6,i%2===0)}
  const finder=(cx,cy)=>{for(let dy=-4;dy<=4;dy++)for(let dx=-4;dx<=4;dx++){const d=Math.max(Math.abs(dx),Math.abs(dy)),x=cx+dx,y=cy+dy;
   if(x>=0&&x<size&&y>=0&&y<size)set(x,y,d!==2&&d!==4)}};
  finder(3,3);finder(size-4,3);finder(3,size-4);
  const ap=align(v),na=ap.length;
  for(let i=0;i<na;i++)for(let j=0;j<na;j++){if((i===0&&j===0)||(i===0&&j===na-1)||(i===na-1&&j===0))continue;
   for(let dy=-2;dy<=2;dy++)for(let dx=-2;dx<=2;dx++)set(ap[i]+dx,ap[j]+dy,Math.max(Math.abs(dx),Math.abs(dy))!==1)}
  const fmt=mask=>{const d=FMT[ecl]<<3|mask;let r=d;for(let i=0;i<10;i++)r=(r<<1)^((r>>>9)*0x537);const b=((d<<10)|r)^0x5412,g=i=>((b>>>i)&1)===1;
   for(let i=0;i<=5;i++)set(8,i,g(i));set(8,7,g(6));set(8,8,g(7));set(7,8,g(8));for(let i=9;i<15;i++)set(14-i,8,g(i));
   for(let i=0;i<8;i++)set(size-1-i,8,g(i));for(let i=8;i<15;i++)set(8,size-15+i,g(i));set(8,size-8,true)};
  fmt(0);
  if(v>=7){let r=v;for(let i=0;i<12;i++)r=(r<<1)^((r>>>11)*0x1F25);const b=(v<<12)|r;
   for(let i=0;i<18;i++){const bit=((b>>>i)&1)===1,a=size-11+i%3,c=Math.floor(i/3);set(a,c,bit);set(c,a,bit)}}
  let i=0;for(let right=size-1;right>=1;right-=2){if(right===6)right=5;
   for(let vert=0;vert<size;vert++)for(let j=0;j<2;j++){const x=right-j,up=((right+1)&2)===0,y=up?size-1-vert:vert;
    if(!F[y][x]&&i<cw.length*8){M[y][x]=(cw[i>>>3]>>>(7-(i&7)))&1;i++}}}
  const MK=[(x,y)=>(x+y)%2===0,(x,y)=>y%2===0,(x,y)=>x%3===0,(x,y)=>(x+y)%3===0,(x,y)=>(Math.floor(x/3)+Math.floor(y/2))%2===0,
   (x,y)=>x*y%2+x*y%3===0,(x,y)=>(x*y%2+x*y%3)%2===0,(x,y)=>((x+y)%2+x*y%3)%2===0];
  const apply=m=>{for(let y=0;y<size;y++)for(let x=0;x<size;x++)if(!F[y][x]&&MK[m](x,y))M[y][x]^=1};
  const penalty=()=>{let p=0,dark=0;
   for(let y=0;y<size;y++){let run=1;for(let x=1;x<size;x++){if(M[y][x]===M[y][x-1]){run++;if(run===5)p+=3;else if(run>5)p++}else run=1}}
   for(let x=0;x<size;x++){let run=1;for(let y=1;y<size;y++){if(M[y][x]===M[y-1][x]){run++;if(run===5)p+=3;else if(run>5)p++}else run=1}}
   for(let y=0;y<size-1;y++)for(let x=0;x<size-1;x++){const c=M[y][x];if(c===M[y][x+1]&&c===M[y+1][x]&&c===M[y+1][x+1])p+=3}
   for(let y=0;y<size;y++)for(let x=0;x<size;x++)dark+=M[y][x];
   return p+Math.floor(Math.abs(dark*20-size*size*10)/(size*size))*10};
  let best=0,bp=1e9;for(let m=0;m<8;m++){apply(m);fmt(m);const p=penalty();if(p<bp){bp=p;best=m}apply(m)}
  apply(best);fmt(best);return {size:size,m:M,mask:best,version:v}}
 function svg(text){const b=new TextEncoder().encode(text);if(b.length>1600)return null;   /* denser codes are hard for phone cameras */
  const q=make(b,'M');if(!q)return null;const n=q.size+8;let d='';
  for(let y=0;y<q.size;y++)for(let x=0;x<q.size;x++){if(!q.m[y][x])continue;let r=1;while(x+r<q.size&&q.m[y][x+r])r++;
   d+='M'+(x+4)+' '+(y+4)+'h'+r+'v1h-'+r+'z';x+=r-1}   /* one rectangle per run of dark modules */
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '+n+' '+n+'" shape-rendering="crispEdges" role="img"><rect width="'+n+'" height="'+n+'" fill="#fff"/><path d="'+d+'" fill="#000"/></svg>'}
 return {make:make,svg:svg}})();
"""


APP_JS = r"""
'use strict';
const ALL=__TEXTS__,TRANSPORT='__TRANSPORT__',DEFLANG='__LANG__';
const THEMES=['biruni-light','biruni-dark','desert','ocean','high-contrast','tv-wall'];
const NAMES={en:'English',ar:'العربية',es:'Español',fr:'Français',pt_BR:'Português',tr:'Türkçe',fa:'فارسی'};
const $=i=>document.getElementById(i);
function keep(k,v){try{if(v==null)localStorage.removeItem(k);else localStorage.setItem(k,v)}catch(e){}}
function kept(k){try{return localStorage.getItem(k)}catch(e){return null}}
const HP=new URLSearchParams(location.hash.slice(1));
const CARD=HP.get('c')||null,KEYB=HP.get('k')||'',HASHL=HP.get('l');
let T={},LANG='en',RTL=false;
const S={def:null,data:{},rev:0,lastData:0,lastMsg:0,card:null,pc:'',pub:'',filters:{},theme:null,digits:kept('biruni-dash-digits')==='1',
 tv:HP.get('tv')==='1',conn:'wait',ended:false,widgets:{},explain:null,sealCode:null,sent:[]};
const SVGNS='http://www.w3.org/2000/svg';
function el(tag,cls,text,parent){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;if(parent)parent.appendChild(e);return e}
function sv(tag,attrs,parent){const e=document.createElementNS(SVGNS,tag);if(attrs)for(const k in attrs)e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e}
function fill(s,o){return String(s).replace(/\{(\w+)\}/g,(m,k)=>o&&o[k]!=null?o[k]:m)}
function pick(l){if(!l)return null;l=String(l).replace('-','_');if(ALL[l])return l;const b=l.split('_')[0].toLowerCase();
 return Object.keys(ALL).find(k=>k.split('_')[0].toLowerCase()===b)||null}
/* ---- numbers and dates ---- */
const NF={};
function tag(cal){let t=LANG.replace('_','-'),u='';if(cal)u+='-ca-islamic-umalqura';
 if(S.digits&&(LANG==='ar'||LANG==='fa'))u+='-nu-'+(LANG==='fa'?'arabext':'arab');else if(RTL)u+='-nu-latn';return u?t+'-u'+u:t}
function nf(v,dec,compact){if(v==null||!isFinite(v))return '–';const k=tag()+'|'+dec+'|'+(compact?1:0);
 if(!NF[k]){const o={maximumFractionDigits:dec==null?2:dec};if(dec!=null)o.minimumFractionDigits=dec;if(compact){o.notation='compact';o.maximumFractionDigits=1;delete o.minimumFractionDigits}
  try{NF[k]=new Intl.NumberFormat(tag(),o)}catch(e){NF[k]=new Intl.NumberFormat('en',o)}}return NF[k].format(v)}
function df(ts,opts,cal){try{return new Intl.DateTimeFormat(tag(cal),opts).format(new Date(ts*1000))}catch(e){return new Date(ts*1000).toISOString().slice(0,10)}}
function dateText(ts,time){const mode=(S.def&&S.def.dates)||'greg',o=time?{dateStyle:'medium',timeStyle:'short'}:{dateStyle:'medium'};
 if(mode==='hijri')return df(ts,o,true);if(mode==='both')return df(ts,o,false)+' · '+df(ts,{dateStyle:'medium'},true);return df(ts,o,false)}
function rel(sec){let rf;try{rf=new Intl.RelativeTimeFormat(tag(),{numeric:'auto'})}catch(e){rf=new Intl.RelativeTimeFormat('en',{numeric:'auto'})}
 const a=Math.abs(sec);if(a<45)return rf.format(-Math.round(sec),'second');if(a<2700)return rf.format(-Math.round(sec/60),'minute');
 if(a<79200)return rf.format(-Math.round(sec/3600),'hour');return rf.format(-Math.round(sec/86400),'day')}
/* ---- motion: values move to their new value; the DOM stays ---- */
const RM=matchMedia('(prefers-reduced-motion: reduce)');
const frame=f=>(document.visibilityState==='visible'&&!RM.matches)?requestAnimationFrame(f):f();
function tween(st,key,to,ms,step){const from=st[key];st['_t'+key]=(st['_t'+key]||0)+1;const id=st['_t'+key];
 if(from==null||!isFinite(from)||!isFinite(to)||RM.matches||document.visibilityState!=='visible'||from===to){st[key]=to;step(to);return}
 const t0=performance.now();const f=t=>{if(st['_t'+key]!==id)return;const p=Math.min(1,(t-t0)/ms),e=1-Math.pow(1-p,3),v=from+(to-from)*e;st[key]=v;step(v);
  if(p<1)requestAnimationFrame(f);else{st[key]=to;step(to)}};requestAnimationFrame(f)}
/* ---- colours ---- */
function css(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim()}
const okc=c=>typeof c==='string'&&/^#[0-9a-fA-F]{3,8}$/.test(c)?c:null;
function pal(i){return 'var(--c'+(1+((i%8)+8)%8)+')'}
function hashc(s){let h=0;for(const ch of String(s))h=(h*31+ch.charCodeAt(0))|0;return pal(Math.abs(h))}
function wcolor(w,value){const st=w.style||{};let c=okc(st.color)||'var(--accent)';
 if(value!=null&&Array.isArray(st.thresholds)){const th=st.thresholds.filter(t=>t&&isFinite(t.at)).sort((a,b)=>a.at-b.at);
  const hit=st.invert?th.filter(t=>value>t.at).pop():th.find(t=>value<t.at);if(hit&&okc(hit.color))c=hit.color}return c}
function hexrgb(h){h=h.replace('#','');if(h.length===3)h=h.split('').map(x=>x+x).join('');const n=parseInt(h.slice(0,6),16);return [n>>16&255,n>>8&255,n&255]}
function mix(a,b,t){const A=hexrgb(a),B=hexrgb(b);return 'rgb('+A.map((v,i)=>Math.round(v+(B[i]-v)*t)).join(',')+')'}
const ICONS={check:'M5 12l5 5L20 7',users:'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21v-1a6 6 0 0 1 12 0v1M16 3.1a4 4 0 0 1 0 7.8M22 21v-1a6 6 0 0 0-4-5.6',
 clock:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2',alert:'M12 3l10 18H2zM12 10v4M12 17.5v.5',map:'M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15',
 flag:'M5 21V4h11l-2 4 2 4H5',chart:'M4 20V10M10 20V4M16 20v-7M22 20H2',star:'M12 3l2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z',
 home:'M3 11l9-8 9 8M5 9v12h14V9',ruler:'M3 17L17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2',parcel:'M3 3h18v18H3zM3 12h18M12 3v18',money:'M3 6h18v12H3zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
 camera:'M3 8h4l2-3h6l2 3h4v12H3zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',pin:'M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12zM12 11.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z'};
function icon(name,parent){const p=ICONS[name];if(!p)return null;const s=sv('svg',{viewBox:'0 0 24 24','aria-hidden':'true'},parent);sv('path',{d:p},s);return s}
/* ---- language and theme ---- */
function setLang(l){LANG=l;T=Object.assign({},ALL.en,ALL[l]||{});RTL=(l==='ar'||l==='fa');const h=document.documentElement;
 h.lang=l.replace('_','-');h.dir=RTL?'rtl':'ltr';for(const k in NF)delete NF[k];
 $('tvBtn').title=T.tv;$('tvBtn').setAttribute('aria-label',T.tv);$('setBtn').title=T.settings;$('setBtn').setAttribute('aria-label',T.settings);
 $('sheetClose').setAttribute('aria-label',T.close);$('waitText').textContent=T.waiting;$('by').textContent=T.by;
 header();setConn(S.conn);if(S.def)build();if(S.bannerKey)banner(S.bannerKey,S.bannerEnd)}
function setTheme(t){if(!THEMES.includes(t))t='biruni-light';S.theme=t;document.documentElement.dataset.theme=t;
 document.querySelector('meta[name=color-scheme]').content=(t==='biruni-dark'||t==='tv-wall'||t==='high-contrast')?'dark':'light';
 for(const id in S.widgets){const x=S.widgets[id];if(x.redraw)x.redraw()}}
function header(){const d=S.def;const t=d&&d.title?d.title:T.title;$('title').textContent=t;document.title=t+' - Biruni GIS';
 $('sub').textContent=S.pc?fill(T.sharedBy,{pc:S.pc}):'';
 const ci=$('cardInfo');ci.textContent='';if(S.card){el('span',null,fill(T.card,{name:S.card.name||''}),ci);
  if(S.card.exp)el('span',null,' · '+fill(T.expires,{d:dateText(S.card.exp)}),ci);if(S.card.zone)el('span','zb',T.zone,ci)}}
function setConn(c){S.conn=c;const e=$('conn');e.className='conn '+c;$('connText').textContent=c==='on'?T.live:c==='wait'?T.connecting:T.offline;fresh()}
function fresh(){const f=$('fresh');if(!S.lastData){f.textContent=S.def?T.never:'';f.className='fresh';return}
 const age=(Date.now()-S.lastData)/1000;f.textContent=fill(T.updated,{t:rel(age)});
 const quiet=(Date.now()-(S.lastMsg||0))/1000;f.className='fresh'+((S.conn==='off'||S.ended||quiet>90)?' red':(S.conn==='wait'||quiet>40)?' amber':'')}
setInterval(()=>{fresh();for(const id in S.widgets){const x=S.widgets[id];if(x.tick)x.tick()}},5000);
function banner(key,end,extra){S.bannerKey=key;S.bannerEnd=!!end;const b=$('banner');if(!key){b.classList.add('hide');return}
 b.textContent=T[key]?T[key]:fill(T.error,{why:extra||key});b.classList.toggle('end',!!end);b.classList.remove('hide')}
let tt=null;function toast(s){const t=$('toast');t.textContent=s;t.classList.add('show');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('show'),2200)}
/* ---- the grid ---- */
function build(){const g=$('grid');g.textContent='';S.widgets={};const ws=(S.def.widgets||[]).slice().sort((a,b)=>(a.y||0)-(b.y||0)||(a.x||0)-(b.x||0));
 for(const w of ws){if(!R[w.type])continue;const sec=el('section','w t-'+w.type,null,g);sec.id='w_'+w.id;sec.dataset.wid=w.id;
  const x=Math.max(0,Math.min(11,w.x|0)),wd=Math.max(1,Math.min(12-x,w.w|0||3)),h=Math.max(1,w.h|0||2);
  sec.style.setProperty('--x',x+1);sec.style.setProperty('--y',(w.y|0)+1);sec.style.setProperty('--w',wd);sec.style.setProperty('--h',h);
  if(wd<=3&&h<=2)sec.classList.add('small');if(wd>=7)sec.classList.add('wide');
  sec.style.setProperty('--wc',wcolor(w,null));sec.setAttribute('role','region');sec.setAttribute('aria-label',w.title||w.type);
  const hd=el('header',null,null,sec);el('h2',null,w.title||'',hd);const sb=el('button','sealb hide',null,hd);sb.type='button';
  sb.onclick=()=>openSeal(sb.dataset.code);const body=el('div','body',null,sec);
  const ctx={w:w,el:sec,body:body,seal:sb,st:{}};S.widgets[w.id]=ctx;R[w.type].make(ctx);if(S.data[w.id])update(ctx,S.data[w.id])}
 g.setAttribute('aria-busy','false');header();if(S.tv)tvCycle()}
function update(ctx,d){ctx.d=d;const sec=ctx.el;sec.classList.toggle('hid',!!(d&&d.hidden));
 let hm=sec.querySelector('.hidmsg');if(d&&d.hidden){if(!hm){hm=el('div','hidmsg',null,ctx.body);
   const s=sv('svg',{viewBox:'0 0 24 24','aria-hidden':'true'},hm);sv('path',{d:'M6 11V8a6 6 0 0 1 12 0v3M5 11h14v10H5zM12 15v2'},s);
   el('b',null,T.hidden,hm);el('small',null,T.hiddenWhy,hm)}}else if(hm)hm.remove();
 if(d&&d.seal){ctx.seal.textContent=d.seal;ctx.seal.dataset.code=d.seal;ctx.seal.title=T.seal;ctx.seal.setAttribute('aria-label',T.seal+' '+d.seal);ctx.seal.classList.remove('hide')}
 else ctx.seal.classList.add('hide');
 if(!d||d.hidden)return;try{R[ctx.w.type].update(ctx,d)}catch(e){console.error('widget '+ctx.w.id+': '+e.message)}
 if(S.xf&&ctx.w.type!=='map')applyXF()}
function tapEl(tag,parent,wid,key,label){const b=el(tag,'tap',null,parent);if(tag==='button')b.type='button';b.onclick=ev=>{ev.stopPropagation();tapItem(wid,key)};
 if(key!=null)b.dataset.k=String(key);if(label)b.setAttribute('aria-label',label);return b}
function tapItem(wid,key){const c=S.widgets[wid];if(key!=null&&c&&c.d&&c.d.xf&&hasMaps()){
  if(S.xf&&S.xf.field===c.d.xf&&S.xf.key===String(key)){explain(wid,key);return}setXF(c.d.xf,String(key),wid);return}explain(wid,key)}
/* ---- the widgets: make once, update with new numbers ---- */
const R={};
R.kpi={make(c){const k=el('div','kpi',null,c.body),v=el('div','kv',null,k);c.val=tapEl('button',v,c.w.id,null);c.val.classList.add('kval');c.val.textContent='–';
  const st=c.w.style||{};if(st.unit)el('span','kunit',st.unit,v);c.delta=el('span','delta hide',null,k);
  if(isFinite(st.target)){c.tgt=el('div','tgt',null,k);c.tgtText=el('span',null,'',c.tgt);c.tgtBar=el('i',null,null,el('div','bar',null,c.tgt))}
  if(st.icon){const ic=el('div','kicon',null,c.body);if(!icon(st.icon,ic))ic.remove()}},
 update(c,d){const st=c.w.style||{},dec=st.decimals,col=wcolor(c.w,d.value);c.el.style.setProperty('--wc',col);c.val.style.color=col;
  tween(c.st,'v',+d.value,700,v=>{c.val.textContent=nf(v,dec==null?(Number.isInteger(+d.value)?0:2):dec,!!(c.w.options||{}).compact)});
  c.val.setAttribute('aria-label',(c.w.title||'')+': '+nf(+d.value,dec));
  if(d.delta!=null&&isFinite(d.delta)){const up=d.delta>0,good=st.invert?!up:up;c.delta.className='delta '+(d.delta===0?'flat':good?'up':'down');
   c.delta.textContent=(d.delta>0?'▲ ':d.delta<0?'▼ ':'• ')+nf(Math.abs(d.delta),dec);el('small',null,' '+T.vsPrev,c.delta)}else c.delta.classList.add('hide');
  if(c.tgt){const t=+st.target,p=t?Math.max(0,Math.min(100,100*d.value/t)):0;c.tgtBar.style.width=p+'%';
   c.tgtText.textContent=fill(T.target,{p:nf(Math.round(100*d.value/t),0),t:nf(t,dec)})}}};
R.gauge={make(c){const g=el('div','gauge',null,c.body),s=sv('svg',{viewBox:'0 0 200 122',class:'ch'},g);
  const grp=sv('g',RTL?{transform:'translate(200 0) scale(-1 1)'}:{},s);
  c.track=sv('path',{d:'M20 108A80 80 0 0 1 180 108',fill:'none',stroke:'var(--surface2)','stroke-width':16,'stroke-linecap':'round'},grp);
  c.arc=sv('path',{d:'M20 108A80 80 0 0 1 180 108',fill:'none',stroke:'var(--wc)','stroke-width':16,'stroke-linecap':'round','pathLength':100,'stroke-dasharray':'0 100'},grp);
  c.num=sv('text',{x:100,y:100,'text-anchor':'middle',class:'gnum'},s);c.lo=sv('text',{x:RTL?180:20,y:121,'text-anchor':'middle'},s);c.hi=sv('text',{x:RTL?20:180,y:121,'text-anchor':'middle'},s);
  c.num.style.cursor='pointer';c.num.addEventListener('click',()=>explain(c.w.id,null))},
 update(c,d){const st=c.w.style||{},lo=isFinite(d.min)?+d.min:0,hi=isFinite(d.max)&&+d.max>lo?+d.max:Math.max(lo+1,+d.value||1),col=wcolor(c.w,d.value);
  c.arc.setAttribute('stroke',col);c.lo.textContent=nf(lo,0,true);c.hi.textContent=nf(hi,0,true);
  tween(c.st,'v',+d.value,800,v=>{const p=Math.max(0,Math.min(100,100*(v-lo)/(hi-lo)));c.arc.setAttribute('stroke-dasharray',p+' 100');
   c.num.textContent=nf(v,st.decimals==null?0:st.decimals)+(st.unit?' '+st.unit:'')})}};
/* x-y charts (line, area, sparkline) */
function xy(c,spark){const box=c.cw.getBoundingClientRect();return [Math.max(spark?40:120,box.width||300),Math.max(spark?20:60,box.height||160)]}
function niceTicks(lo,hi,n){if(!(hi>lo)){hi=lo+1}const span=hi-lo,step0=span/Math.max(1,n),mag=Math.pow(10,Math.floor(Math.log10(step0))),
 f=step0/mag,step=(f<1.5?1:f<3?2:f<7?5:10)*mag,a=Math.floor(lo/step)*step,b=Math.ceil(hi/step)*step,out=[];for(let v=a;v<=b+step/2;v+=step)out.push(+v.toFixed(10));return out}
function xLabel(x,kind,span){if(kind==='time'){const o=span>86400*400?{year:'numeric',month:'short'}:span>86400*3?{month:'short',day:'numeric'}:{hour:'2-digit',minute:'2-digit'};
  return df(x,o,(S.def&&S.def.dates)==='hijri')}if(kind==='key')return String(x);return nf(x,null,true)}
function series(d){return (Array.isArray(d.series)?d.series:[]).map(s=>({name:String(s.name||''),pts:(Array.isArray(s.points)?s.points:[]).filter(p=>Array.isArray(p)&&p.length>=2)}))}
function drawXY(c,stacked,spark){const d=c.d;if(!d)return;const ss=series(d),kind=d.x||'number',[W,H]=xy(c,spark);const s=c.svg;s.textContent='';s.setAttribute('viewBox','0 0 '+W+' '+H);
 if(spark)s.setAttribute('preserveAspectRatio','none');
 if(!ss.length||!ss.some(x=>x.pts.length)){const t=sv('text',{x:W/2,y:H/2,'text-anchor':'middle'},s);t.textContent=T.nothing;return}
 let keys=null;if(kind==='key'){keys=[];for(const x of ss)for(const p of x.pts)if(!keys.includes(String(p[0])))keys.push(String(p[0]))}
 const X=p=>kind==='key'?keys.indexOf(String(p)):+p;let xs=[];for(const x of ss)for(const p of x.pts)xs.push(X(p[0]));xs=[...new Set(xs)].sort((a,b)=>a-b);
 const val=(x,xv)=>{const p=x.pts.find(q=>X(q[0])===xv);return p&&isFinite(p[1])?+p[1]:null};
 const stack=stacked?ss.map(()=>({})):null;if(stacked)for(const xv of xs){let acc=0;ss.forEach((x,i)=>{const v=val(x,xv)||0;stack[i][xv]=[acc,acc+v];acc+=v})}
 let lo=Infinity,hi=-Infinity;if(stacked){for(const st of stack)for(const k in st){lo=Math.min(lo,st[k][0]);hi=Math.max(hi,st[k][1])}}
 else for(const x of ss)for(const p of x.pts)if(isFinite(p[1])){lo=Math.min(lo,+p[1]);hi=Math.max(hi,+p[1])}
 if(!isFinite(lo)){lo=0;hi=1}if(!spark&&lo>0&&(stacked||lo<hi*0.5))lo=0;const yt=spark?[lo,hi===lo?lo+1:hi]:niceTicks(lo,hi,4);const y0=yt[0],y1=yt[yt.length-1];
 const ylab=yt.map(v=>nf(v,null,true)),L=spark?2:Math.max(30,Math.min(90,10+7*Math.max(...ylab.map(x=>x.length)))),Rm=spark?2:12,Tm=spark?4:8,B=spark?2:24,x0=xs[0],x1=xs[xs.length-1]===x0?x0+1:xs[xs.length-1];
 const px=v=>{const t=(v-x0)/(x1-x0);return RTL&&kind==='key'?W-Rm-(W-L-Rm)*t:L+(W-L-Rm)*t},py=v=>H-B-(H-B-Tm)*((v-y0)/((y1-y0)||1));
 if(!spark){const gg=sv('g',{class:'grid'},s);for(const v of yt){sv('line',{x1:L,x2:W-Rm,y1:py(v),y2:py(v)},gg);
   const t=sv('text',{x:L-8,y:py(v)+4,'text-anchor':'end'},s);t.textContent=nf(v,null,true)}
  const nx=Math.max(2,Math.min(xs.length,Math.floor((W-L)/90)));for(let i=0;i<nx;i++){const xv=xs[Math.round(i*(xs.length-1)/Math.max(1,nx-1))];
   const t=sv('text',{x:px(xv),y:H-6,'text-anchor':'middle'},s);t.textContent=xLabel(kind==='key'?keys[xv]:xv,kind,x1-x0)}}
 const defs=sv('defs',null,s);
 ss.forEach((x,i)=>{const col=i===0&&ss.length===1?'var(--wc)':pal(i),id='g'+c.w.id.replace(/\W/g,'')+i;
  const lg=sv('linearGradient',{id:id,x1:0,y1:0,x2:0,y2:1},defs);sv('stop',{offset:'0%','stop-color':col,'stop-opacity':stacked?.75:.28},lg);sv('stop',{offset:'100%','stop-color':col,'stop-opacity':stacked?.45:0},lg);
  let pts;if(stacked)pts=xs.map(xv=>[px(xv),py(stack[i][xv][1]),py(stack[i][xv][0])]);else pts=x.pts.map(p=>[px(X(p[0])),py(+p[1]),py(y0)]).sort((a,b)=>a[0]-b[0]);
  if(!pts.length)return;const line='M'+pts.map(p=>p[0].toFixed(1)+' '+p[1].toFixed(1)).join('L');
  if(stacked||c.w.type==='area'||spark){const back=pts.slice().reverse().map(p=>p[0].toFixed(1)+' '+p[2].toFixed(1)).join('L');sv('path',{d:line+'L'+back+'Z',fill:'url(#'+id+')',stroke:'none'},s)}
  const ln=sv('path',{d:line,fill:'none',stroke:col,'stroke-width':spark?2:2.4,'stroke-linejoin':'round','stroke-linecap':'round'},s);if(spark)ln.style.vectorEffect='non-scaling-stroke';
  if(!spark&&pts.length<=40)for(const p of pts)sv('circle',{cx:p[0],cy:p[1],r:2.6,fill:'var(--surface)',stroke:col,'stroke-width':1.8},s)});
 if(spark||!c.tip)return;const guide=sv('line',{y1:Tm,y2:H-B,stroke:'var(--muted)','stroke-dasharray':'3 3',opacity:0},s);
 const hit=sv('rect',{x:L,y:0,width:Math.max(1,W-L-Rm),height:H,fill:'transparent'},s);hit.style.cursor='crosshair';
 const near=ev=>{const r=s.getBoundingClientRect(),mx=(ev.clientX-r.left)*W/r.width;let best=xs[0],bd=1e9;for(const xv of xs){const dd=Math.abs(px(xv)-mx);if(dd<bd){bd=dd;best=xv}}return best};
 hit.addEventListener('pointermove',ev=>{const xv=near(ev);guide.setAttribute('x1',px(xv));guide.setAttribute('x2',px(xv));guide.setAttribute('opacity',.6);
  c.tip.textContent=xLabel(kind==='key'?keys[xv]:xv,kind,0)+' — '+ss.map(x=>(x.name?x.name+': ':'')+nf(val(x,xv))).join(' · ');
  const r=s.getBoundingClientRect(),cr=c.cw.getBoundingClientRect();c.tip.style.left=(r.left-cr.left+px(xv)*r.width/W)+'px';c.tip.style.top=(ev.clientY-cr.top)+'px';c.tip.classList.add('on')});
 hit.addEventListener('pointerleave',()=>{guide.setAttribute('opacity',0);c.tip.classList.remove('on')});
 hit.addEventListener('click',ev=>{const xv=near(ev);explain(c.w.id,String(kind==='key'?keys[xv]:xv))})}
function legend(c,names){if(!c.leg)return;c.leg.textContent='';if(names.length<2){c.leg.classList.add('hide');return}c.leg.classList.remove('hide');
 names.forEach((n,i)=>{const s=el('span',null,null,c.leg);el('i',null,null,s).style.background=pal(i);el('span',null,n,s)})}
function makeXY(c,leg){c.cw=el('div','cwrap',null,c.body);c.svg=sv('svg',{class:'ch',role:'img','aria-label':c.w.title||''},c.cw);c.tip=el('div','tip',null,c.cw);
 if(leg)c.leg=el('div','legend',null,c.body);observe(c)}
R.line={make(c){makeXY(c,true);c.redraw=()=>drawXY(c,false,false)},update(c,d){legend(c,series(d).map(s=>s.name));drawXY(c,false,false)}};
R.area={make(c){makeXY(c,true);c.redraw=()=>drawXY(c,(c.w.options||{}).stacked!==false,false)},update(c,d){legend(c,series(d).map(s=>s.name));c.redraw()}};
R.sparkline={make(c){const sp=el('div','spark',null,c.body),v=el('div','kv',null,sp);c.val=tapEl('button',v,c.w.id,null);c.val.classList.add('kval');
  if((c.w.style||{}).unit)el('span','kunit',c.w.style.unit,v);c.delta=el('span','delta hide',null,sp);c.cw=el('div','cwrap',null,sp);c.svg=sv('svg',{class:'ch'},c.cw);c.redraw=()=>drawXY(c,false,true);observe(c)},
 update(c,d){const ss=series(d),p=ss.length?ss[0].pts:[],last=p.length?+p[p.length-1][1]:null,prev=p.length>1?+p[p.length-2][1]:null,st=c.w.style||{};
  const col=wcolor(c.w,last);c.el.style.setProperty('--wc',col);tween(c.st,'v',last,700,v=>{c.val.textContent=nf(v,st.decimals==null?0:st.decimals)});
  if(prev!=null&&last!=null){const dl=last-prev,good=st.invert?dl<0:dl>0;c.delta.className='delta '+(dl===0?'flat':good?'up':'down');c.delta.textContent=(dl>0?'▲ ':dl<0?'▼ ':'• ')+nf(Math.abs(dl),st.decimals)}
  c.redraw()}};
function observe(c){if(!window.ResizeObserver)return;let raf=0;new ResizeObserver(()=>{cancelAnimationFrame(raf);raf=requestAnimationFrame(()=>{if(c.redraw&&c.d)c.redraw()})}).observe(c.cw||c.body)}
/* bars: html, so they mirror by themselves in Arabic */
function items(d){return (Array.isArray(d.items)?d.items:[]).filter(i=>i&&typeof i==='object').map(i=>({key:i.key!=null?String(i.key):String(i.label||''),
 label:String(i.label!=null?i.label:i.key!=null?i.key:''),value:+i.value,hidden:!!i.hidden,color:okc(i.color)}))}
R.bar={make(c){c.box=el('div',null,null,c.body);c.box.style.cssText='flex:1;min-height:0;display:flex;flex-direction:column';c.rows={}},
 update(c,d){const it=items(d),o=c.w.options||{},orient=o.orient||((it.length>6||it.some(i=>i.label.length>9))?'h':'v');
  if(c.orient!==orient){c.box.textContent='';c.rows={};c.orient=orient;c.wrap=el('div',orient==='h'?'hb':'vb',null,c.box)}
  const max=Math.max(1e-9,...it.filter(i=>!i.hidden).map(i=>i.value||0)),st=c.w.style||{};const seen=new Set();
  it.forEach((i,n)=>{seen.add(i.key);let r=c.rows[i.key];
   if(!r){r={};if(orient==='h'){r.el=tapEl('button',c.wrap,c.w.id,i.key);r.el.classList.add('hrow');r.lb=el('span','lb',null,r.el);r.tr=el('span','tr',null,r.el);r.bar=el('i',null,null,r.tr);r.vl=el('span','vl',null,r.el)}
    else{r.el=tapEl('button',c.wrap,c.w.id,i.key);r.el.classList.add('vcol');r.vl=el('span','vl',null,r.el);r.tr=el('span','tr',null,r.el);r.bar=el('i',null,null,r.tr);r.lb=el('span','lb',null,r.el)}c.rows[i.key]=r}
   c.wrap.appendChild(r.el);r.lb.textContent=i.label;r.lb.title=i.label;r.bar.classList.toggle('hatch',i.hidden);
   r.bar.style.setProperty('--ic',i.color||(o.colors==='each'?pal(n):wcolor(c.w,i.value)));
   const p=i.hidden?100:Math.max(0,100*(i.value||0)/max);frame(()=>{if(orient==='h')r.bar.style.width=p+'%';else r.bar.style.height=p+'%'});
   r.vl.textContent=i.hidden?T.hiddenItem:nf(i.value,st.decimals)+(st.unit?' '+st.unit:'');r.el.setAttribute('aria-label',i.label+': '+r.vl.textContent)});
  for(const k in c.rows)if(!seen.has(k)){c.rows[k].el.remove();delete c.rows[k]}
  if(!it.length){if(!c.empty)c.empty=el('div','empty',T.nothing,c.box)}else if(c.empty){c.empty.remove();c.empty=null}}};
R.leaderboard={make(c){c.list=el('div','plist',null,c.body);c.rows={}},
 update(c,d){const it=items(d).sort((a,b)=>(b.value||0)-(a.value||0)),max=Math.max(1e-9,...it.map(i=>i.value||0)),st=c.w.style||{},seen=new Set();
  it.forEach((i,n)=>{seen.add(i.key);let r=c.rows[i.key];if(!r){r={};r.el=tapEl('button',c.list,c.w.id,i.key);r.el.classList.add('lrow');r.rk=el('span','rk',null,r.el);
    r.nm=el('span','lb',null,r.el);r.nm.style.cssText='overflow:hidden;text-overflow:ellipsis;white-space:nowrap';r.tr=el('span','bar',null,r.el);r.bar=el('i',null,null,r.tr);r.vl=el('b',null,null,r.el);c.rows[i.key]=r}
   c.list.appendChild(r.el);r.rk.textContent=nf(n+1,0);r.rk.className='rk'+(n<3?' r'+(n+1):'');r.nm.textContent=i.label;r.bar.style.background=i.color||wcolor(c.w,i.value);
   const p=i.hidden?100:100*(i.value||0)/max;r.bar.classList.toggle('hatch',i.hidden);frame(()=>{r.bar.style.width=p+'%'});
   r.vl.textContent=i.hidden?T.hiddenItem:nf(i.value,st.decimals);r.el.setAttribute('aria-label',T.rank+' '+(n+1)+': '+i.label+' '+r.vl.textContent)});
  for(const k in c.rows)if(!seen.has(k)){c.rows[k].el.remove();delete c.rows[k]}}};
R.progress={make(c){c.list=el('div','plist',null,c.body);c.rows={}},
 update(c,d){const it=(Array.isArray(d.items)?d.items:[]).filter(i=>i&&typeof i==='object'),seen=new Set();
  it.forEach((i,n)=>{const key=String(i.key!=null?i.key:i.label);seen.add(key);let r=c.rows[key];
   if(!r){r={};r.el=tapEl('button',c.list,c.w.id,key);r.el.classList.add('prow');const t=el('span','top2',null,r.el);r.lb=el('b',null,null,t);r.pc=el('span',null,null,t);
    r.tr=el('span','bar',null,r.el);r.bar=el('i',null,null,r.tr);r.sm=el('small',null,null,r.el);c.rows[key]=r}
   c.list.appendChild(r.el);const done=+i.done||0,tot=+i.total||0,p=tot>0?Math.max(0,Math.min(100,100*done/tot)):0;r.lb.textContent=String(i.label!=null?i.label:key);
   if(i.hidden){r.pc.textContent=T.hiddenItem;r.bar.classList.add('hatch');r.sm.textContent=''}else{r.bar.classList.remove('hatch');
    r.pc.textContent=nf(Math.round(p),0)+'%';r.sm.textContent=fill(T.of,{v:nf(done,0),t:nf(tot,0)});const col=wcolor(c.w,p);r.bar.style.background=col;frame(()=>{r.bar.style.width=p+'%'})}});
  for(const k in c.rows)if(!seen.has(k)){c.rows[k].el.remove();delete c.rows[k]}}};
R.pie={make(c){const p=el('div','pie',null,c.body);c.svg=sv('svg',{viewBox:'-110 -110 220 220',class:'ch',role:'img','aria-label':c.w.title||''},p);c.g=sv('g',null,c.svg);
  c.tot=sv('text',{x:0,y:4,'text-anchor':'middle',class:'ptot'},c.svg);c.tsub=sv('text',{x:0,y:22,'text-anchor':'middle'},c.svg);c.leg=el('div','legend',null,p);c.slices={}},
 update(c,d){const it=items(d),vis=it.filter(i=>!i.hidden&&i.value>0),tot=vis.reduce((a,i)=>a+i.value,0),donut=(c.w.options||{}).donut!==false,st=c.w.style||{};
  const keys=vis.map(i=>i.key),to=vis.map(i=>i.value/(tot||1));const from=keys.map(k=>c.slices[k]!=null?c.slices[k]:0);
  c.tot.textContent=donut?nf(tot,st.decimals,true):'';c.tsub.textContent=donut&&st.unit?st.unit:'';
  const draw=fr=>{c.g.textContent='';let a=-Math.PI/2;fr.forEach((f,n)=>{const i=vis[n],a2=a+f*2*Math.PI*0.99999,R0=100,r0=donut?62:0,large=(a2-a)>Math.PI?1:0;
    const pt=(r,an)=>(RTL?-1:1)*r*Math.cos(an)+' '+r*Math.sin(an),sw=RTL?0:1;
    const dd='M'+pt(R0,a)+'A'+R0+' '+R0+' 0 '+large+' '+sw+' '+pt(R0,a2)+(r0?'L'+pt(r0,a2)+'A'+r0+' '+r0+' 0 '+large+' '+(1-sw)+' '+pt(r0,a):'L0 0')+'Z';
    const p=sv('path',{d:dd,fill:i.color||pal(it.indexOf(i)),stroke:'var(--surface)','stroke-width':2},c.g);p.style.cursor='pointer';p.dataset.k=i.key;p.addEventListener('click',()=>tapItem(c.w.id,i.key));
    const t=sv('title',null,p);t.textContent=i.label+': '+nf(i.value,st.decimals);a=a2})};
  const sm={};const t0=performance.now(),run=t=>{const p=Math.min(1,(t-t0)/700),e=1-Math.pow(1-p,3);draw(to.map((v,n)=>from[n]+(v-from[n])*e));if(p<1)requestAnimationFrame(run)};
  if(RM.matches||document.visibilityState!=='visible')draw(to);else requestAnimationFrame(run);keys.forEach((k,n)=>sm[k]=to[n]);c.slices=sm;
  c.leg.textContent='';it.forEach((i,n)=>{const b=tapEl('button',c.leg,c.w.id,i.key);const sw=el('i',null,null,b);
   sw.style.background=i.hidden?'repeating-linear-gradient(135deg,var(--muted) 0 2px,transparent 2px 4px)':(i.color||pal(n));el('span',null,i.label,b);el('b',null,i.hidden?T.hiddenItem:nf(tot?100*i.value/tot:0,0)+'%',b)})}};
R.table={make(c){c.wrap=el('div','tbl',null,c.body);c.table=el('table',null,null,c.wrap);c.thead=el('thead',null,null,c.table);c.tbody=el('tbody',null,null,c.table);c.foot=el('div','tfoot',null,c.body);c.sort=null},
 update(c,d){const cols=Array.isArray(d.columns)?d.columns:[];let rows=Array.isArray(d.rows)?d.rows.slice():[];
  const tr=el('tr');c.thead.textContent='';cols.forEach((col,i)=>{const th=el('th',null,String(col.label!=null?col.label:col.key),tr);th.tabIndex=0;th.scope='col';
   if(c.sort&&c.sort.i===i)th.setAttribute('aria-sort',c.sort.dir>0?'ascending':'descending');
   const go=()=>{c.sort={i:i,dir:c.sort&&c.sort.i===i?-c.sort.dir:1};update(c,c.d)};th.onclick=go;th.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}}});c.thead.appendChild(tr);
  const num=cols.map((_,i)=>rows.filter(r=>Array.isArray(r)).every(r=>r[i]==null||r[i]===''||isFinite(r[i])));
  if(c.sort){const {i,dir}=c.sort;rows.sort((a,b)=>{if(!Array.isArray(a))return 1;if(!Array.isArray(b))return -1;const x=a[i],y=b[i];
   return (num[i]?(+x||0)-(+y||0):String(x==null?'':x).localeCompare(String(y==null?'':y),LANG.replace('_','-'),{numeric:true}))*dir})}
  c.tbody.textContent='';for(const r of rows){const row=el('tr',null,null,c.tbody);
   if(!Array.isArray(r)){const td=el('td',null,T.hiddenItem,row);td.colSpan=cols.length||1;td.classList.add('hatch');td.style.color='var(--muted)';continue}
   const key=r.length?String(r[0]):'';row.style.cursor='pointer';row.onclick=()=>explain(c.w.id,key);
   cols.forEach((_,i)=>{const v=r[i];const td=el('td',num[i]?'num':null,v==null?'':num[i]&&v!==''?nf(+v):String(v),row)})}
  c.foot.textContent=isFinite(d.total)&&d.total>rows.length?fill(T.total,{n:nf(rows.length,0),t:nf(d.total,0)}):''}};
/* maps: several styled layers in SVG (Web Mercator around the data's centre), the PC's basemap image under them,
   labels that never overlap, a legend with layer switches, a scale bar and a north arrow; a tap opens a popup;
   maps and charts select together (cross-filter) */
const MR=6378137,MK=20037508.34;
function merc(lon,lat){lat=Math.max(-85,Math.min(85,lat));return [lon*MK/180,Math.log(Math.tan((90+lat)*Math.PI/360))*MK/Math.PI]}
function unmerc(x,y){return [x*180/MK,360/Math.PI*Math.atan(Math.exp(y*Math.PI/MK))-90]}
function geoPath(g,o){const ring=r=>r.map((p,i)=>{const m=merc(p[0],p[1]);return (i?'L':'M')+(m[0]-o[0]).toFixed(2)+' '+(o[1]-m[1]).toFixed(2)}).join('');
 if(!g)return '';switch(g.type){case 'Polygon':return g.coordinates.map(r=>ring(r)+'Z').join('');case 'MultiPolygon':return g.coordinates.map(p=>p.map(r=>ring(r)+'Z').join('')).join('');
  case 'LineString':return ring(g.coordinates);case 'MultiLineString':return g.coordinates.map(ring).join('');default:return ''}}
function geoPoints(g){if(!g)return [];if(g.type==='Point')return [g.coordinates];if(g.type==='MultiPoint')return g.coordinates;return []}
function bboxOf(fc){let b=[180,90,-180,-90];const add=p=>{b[0]=Math.min(b[0],p[0]);b[1]=Math.min(b[1],p[1]);b[2]=Math.max(b[2],p[0]);b[3]=Math.max(b[3],p[1])};
 const walk=a=>{if(typeof a[0]==='number')add(a);else a.forEach(walk)};for(const f of (fc&&fc.features)||[])if(f&&f.geometry&&f.geometry.coordinates)walk(f.geometry.coordinates);return b[0]<=b[2]?b:null}
function fbox(g){let b=[1e9,1e9,-1e9,-1e9];const walk=a=>{if(typeof a[0]==='number'){const m=merc(a[0],a[1]);b[0]=Math.min(b[0],m[0]);b[1]=Math.min(b[1],m[1]);b[2]=Math.max(b[2],m[0]);b[3]=Math.max(b[3],m[1])}else a.forEach(walk)};
 if(g&&g.coordinates)walk(g.coordinates);return b}
function anchorOf(g){if(!g)return null;if(/Point/.test(g.type)){const p=geoPoints(g)[0];return p?merc(p[0],p[1]):null}
 if(/Line/.test(g.type)){const l=(g.type==='LineString'?g.coordinates:g.coordinates[0]||[]).map(p=>merc(p[0],p[1]));if(!l.length)return null;if(l.length===1)return l[0];
  let tot=0;for(let i=1;i<l.length;i++)tot+=Math.hypot(l[i][0]-l[i-1][0],l[i][1]-l[i-1][1]);let half=tot/2;
  for(let i=1;i<l.length;i++){const d=Math.hypot(l[i][0]-l[i-1][0],l[i][1]-l[i-1][1]);if(half<=d||i===l.length-1){const t=d?Math.min(1,half/d):0;
    const a=[l[i-1][0]+(l[i][0]-l[i-1][0])*t,l[i-1][1]+(l[i][1]-l[i-1][1])*t];a.ang=Math.atan2(l[i][1]-l[i-1][1],l[i][0]-l[i-1][0]);a.len=tot;return a}half-=d}return l[0]}
 const ring=g.type==='Polygon'?g.coordinates[0]:(g.coordinates[0]||[])[0];if(!ring||ring.length<3)return null;
 let A=0,X=0,Y=0;const ps=ring.map(p=>merc(p[0],p[1]));for(let i=0;i<ps.length-1;i++){const [x0,y0]=ps[i],[x1,y1]=ps[i+1],k=x0*y1-x1*y0;A+=k;X+=(x0+x1)*k;Y+=(y0+y1)*k}
 if(Math.abs(A)<1e-9){const b=fbox(g);return [(b[0]+b[2])/2,(b[1]+b[3])/2]}return [X/(3*A),Y/(3*A)]}
function hexOk(c){return typeof c==='string'&&/^#[0-9a-fA-F]{6}$/.test(c)}
function darker(c,t){return hexOk(c)?mix(c,'#000000',t==null?.35:t):'var(--mapline)'}
/* the layers of a map's data: v2 {layers:[...]} or the first shape {geojson} */
function mapLayers(c,d){if(Array.isArray(d.layers))return d.layers.filter(L=>L&&typeof L==='object');
 const st=c.w.style||{},by=st.by,cats={};let n=0;const fc=d.geojson||{features:[]};
 const colorOf=f=>{if(!by)return okc(st.color)||null;const v=String((f.properties||{})[by]);if(!(v in cats))cats[v]=css('--c'+(1+(n++%8)))||'#2f80ed';return cats[v]};
 for(const f of fc.features||[]){if(f&&f.properties)f.properties._c=colorOf(f)||f.properties._c}
 const leg=by?Object.keys(cats).slice(0,10).map(k=>({label:k,color:cats[k]})):[];
 const kinds=new Set((fc.features||[]).map(f=>f&&f.geometry?(/Point/.test(f.geometry.type)?'point':/Line/.test(f.geometry.type)?'line':'fill'):''));
 return [{id:'L0',name:c.w.title||'',kind:kinds.size===1?[...kinds][0]:'mixed',legacy:true,geojson:fc,legend:leg,style:{color:okc(st.color)||css('--accent')||'#2f80ed',opacity:.8,size:5,width:2.4}}]}
function makeMap(c){c.mp=el('div','map',null,c.body);c.svg=sv('svg',{role:'img','aria-label':c.w.title||''},c.mp);
 const defs=sv('defs',null,c.svg),pt=sv('pattern',{id:'hatch'+c.w.id.replace(/\W/g,''),width:6,height:6,patternUnits:'userSpaceOnUse',patternTransform:'rotate(45)'},defs);
 sv('rect',{width:6,height:6,fill:'var(--surface2)'},pt);sv('line',{x1:0,y1:0,x2:0,y2:6,stroke:'var(--muted)','stroke-width':2,opacity:.5},pt);
 c.bmg=sv('g',{class:'bmg'},c.svg);c.layer=c.gl=sv('g',{class:'gl'},c.svg);c.hl=sv('g',{class:'hl'},c.svg);c.lab=sv('svg',{class:'lab','aria-hidden':'true'},c.mp);
 c.leg=el('div','gleg hide',null,c.mp);c.legBtn=el('button',null,null,c.leg);c.legBtn.type='button';el('span',null,T.legend,c.legBtn);
 const ch=sv('svg',{viewBox:'0 0 24 24','aria-hidden':'true'},c.legBtn);sv('path',{d:'M6 15l6-6 6 6'},ch);c.legBody=el('div','lb2',null,c.leg);
 c.legBtn.onclick=()=>{c.leg.classList.toggle('closed');c.legOpen=!c.leg.classList.contains('closed')};
 c.scale=el('div','mscale',null,c.mp);c.scaleT=el('span',null,'',c.scale);c.scaleB=el('i',null,null,c.scale);
 const nr=sv('svg',{class:'mnorth',viewBox:'0 0 26 34','aria-hidden':'true'},c.mp);sv('path',{d:'M13 2 L21 24 L13 19 L5 24 Z',fill:'var(--text)',stroke:'var(--surface)','stroke-width':1.2},nr);
 sv('path',{d:'M13 2 L13 19 L5 24 Z',fill:'var(--surface)',opacity:.55},nr);const nt=sv('text',{x:13,y:33,'text-anchor':'middle','font-size':9,'font-weight':700,fill:'var(--text)'},nr);nt.textContent=T.north;c.northT=nt;
 const bt=el('div','mapbtn',null,c.mp);
 const zb=(p,f,lbl)=>{const b=el('button','icon',null,bt);b.type='button';b.setAttribute('aria-label',lbl);b.title=lbl;const s=sv('svg',{viewBox:'0 0 24 24'},b);sv('path',{d:p},s);b.onclick=f;return b};
 zb('M12 5v14M5 12h14',()=>zoomAt(c,0.6),'+');zb('M5 12h14',()=>zoomAt(c,1/0.6),'−');zb('M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',()=>{c.view=null;layout(c);closePop(c)},T.reset);
 c.pop=el('div','mpop hide',null,c.mp);
 const P=new Map();let g=null;c.svg.addEventListener('pointerdown',e=>{P.set(e.pointerId,[e.clientX,e.clientY]);try{c.svg.setPointerCapture(e.pointerId)}catch(x){}g={moved:0,target:e.target,x:e.clientX,y:e.clientY}});
 c.svg.addEventListener('pointermove',e=>{if(!P.has(e.pointerId)||!c.view)return;const old=P.get(e.pointerId);P.set(e.pointerId,[e.clientX,e.clientY]);
  if(P.size===1){const r=c.svg.getBoundingClientRect(),k=c.view.w/r.width;const dx=e.clientX-old[0],dy=e.clientY-old[1];g.moved+=Math.abs(dx)+Math.abs(dy);if(g.moved>6){c.mp.classList.add('panning');c.view.x-=dx*k;c.view.y-=dy*k;setView(c,true)}}
  else if(P.size===2){const ps=[...P.values()],d1=Math.hypot(ps[0][0]-ps[1][0],ps[0][1]-ps[1][1]),d0=Math.hypot(ps[0][0]-ps[1][0]-(e.clientX-old[0]),ps[0][1]-ps[1][1]-(e.clientY-old[1]));
   c.mp.classList.add('panning');if(d1>0&&d0>0)zoomAt(c,d0/d1,(ps[0][0]+ps[1][0])/2,(ps[0][1]+ps[1][1])/2,true);g.moved+=10}});
 const up=e=>{P.delete(e.pointerId);if(!P.size){c.mp.classList.remove('panning');settle(c)}
  if(e.type==='pointerup'&&g&&g.moved<=6&&!P.size)tapMap(c,g.target,e)};
 c.svg.addEventListener('pointerup',up);c.svg.addEventListener('pointercancel',up);
 c.svg.addEventListener('dblclick',e=>{e.preventDefault();zoomAt(c,0.5,e.clientX,e.clientY)});
 let wt=null;c.svg.addEventListener('wheel',e=>{e.preventDefault();c.mp.classList.add('panning');zoomAt(c,e.deltaY>0?1.2:1/1.2,e.clientX,e.clientY,true);
  clearTimeout(wt);wt=setTimeout(()=>{c.mp.classList.remove('panning');settle(c)},160)},{passive:false});
 c.redraw=()=>{if(c.d)R[c.w.type].update(c,c.d)};observe(c)}
function px(c,m){const v=c.view,r=c.svgR||c.svg.getBoundingClientRect(),k=r.width/v.w;return [((m[0]-c.org[0])-v.x)*k,((c.org[1]-m[1])-v.y)*k]}
function setView(c,quick){const v=c.view;c.svg.setAttribute('viewBox',v.x+' '+v.y+' '+v.w+' '+v.h);const r=c.svg.getBoundingClientRect();c.svgR=r;const k=v.w/Math.max(1,r.width);c.k=k;
 c.gl.querySelectorAll('circle').forEach(p=>p.setAttribute('r',(p.dataset.r||5)*k));c.hl.querySelectorAll('circle').forEach(p=>p.setAttribute('r',((p.dataset.r||5)+3)*k));
 scaleBar(c);if(!quick)settle(c)}
function settle(c){cancelAnimationFrame(c.lraf);c.lraf=requestAnimationFrame(()=>labels(c));if(c.bmOn){clearTimeout(c.bmT);c.bmT=setTimeout(()=>askBasemap(c),350)}}
function zoomAt(c,f,cx,cy,quick){if(!c.view)return;const v=c.view,r=c.svg.getBoundingClientRect();let fx=.5,fy=.5;
 if(cx!=null&&r.width){fx=(cx-r.left)/r.width;fy=(cy-r.top)/r.height}const ax=v.x+v.w*fx,ay=v.y+v.h*fy;v.w*=f;v.h*=f;v.x=ax-v.w*fx;v.y=ay-v.h*fy;setView(c,quick)}
function layout(c){const r=c.mp.getBoundingClientRect(),W=Math.max(50,r.width||300),H=Math.max(50,r.height||200);const b=c.bb;if(!b)return;
 const p0=merc(b[0],b[1]),p1=merc(b[2],b[3]);let w=Math.max(10,p1[0]-p0[0]),h=Math.max(10,p1[1]-p0[1]);w*=1.08;h*=1.08;
 if(w/h>W/H)h=w*H/W;else w=h*W/H;const cx=(p0[0]+p1[0])/2-c.org[0],cy=c.org[1]-(p0[1]+p1[1])/2;c.view={x:cx-w/2,y:cy-h/2,w:w,h:h};setView(c)}
function zoomBox(c,mb){if(!c.view)return;const r=c.mp.getBoundingClientRect(),W=Math.max(50,r.width),H=Math.max(50,r.height);
 let w=Math.max(30,(mb[2]-mb[0])*2.2),h=Math.max(30,(mb[3]-mb[1])*2.2);if(w/h>W/H)h=w*H/W;else w=h*W/H;
 const cx=(mb[0]+mb[2])/2-c.org[0],cy=c.org[1]-(mb[1]+mb[3])/2;c.view={x:cx-w/2,y:cy-h/2,w:w,h:h};setView(c)}
function scaleBar(c){const v=c.view,r=c.svgR;if(!v||!r||!r.width||!c.org)return;const lat=unmerc(c.org[0]+v.x+v.w/2,c.org[1]-(v.y+v.h/2))[1];
 const mpp=v.w/r.width*Math.cos(lat*Math.PI/180);let m=mpp*110;const p=Math.pow(10,Math.floor(Math.log10(m))),f=m/p;m=(f>=5?5:f>=2?2:1)*p;
 c.scaleB.style.width=Math.round(m/mpp)+'px';c.scaleT.textContent=m>=1000?nf(m/1000,m%1000?1:0)+' '+T.km:nf(m,0)+' '+T.m}
/* drawing */
function drawLayers(c,layers,opts){opts=opts||{};const bb=(c.d&&Array.isArray(c.d.bbox)&&c.d.bbox.length===4)?c.d.bbox:(()=>{let b=null;for(const L of layers){const x=bboxOf(L.geojson||{});if(x)b=b?[Math.min(b[0],x[0]),Math.min(b[1],x[1]),Math.max(b[2],x[2]),Math.max(b[3],x[3])]:x}return b})();
 if(!bb){c.bb=null;c.gl.textContent='';return}const first=!c.bb||!c.org;c.bb=bb;if(first)c.org=merc((bb[0]+bb[2])/2,(bb[1]+bb[3])/2);
 if(!c.L)c.L={};const seenL=new Set();const order={fill:0,mixed:1,line:2,point:3};
 layers.slice().sort((a,b)=>(order[a.kind]||0)-(order[b.kind]||0)).forEach(L=>{const lid=String(L.id);seenL.add(lid);
  let G=c.L[lid];if(!G){G=c.L[lid]={g:sv('g',{'data-l':lid},c.gl),f:{},on:true};if(L.kind==='line'||L.kind==='mixed')G.cas=sv('g',null,G.g),G.top=sv('g',null,G.g);else G.top=G.g}
  c.gl.appendChild(G.g);G.L=L;G.g.style.display=G.on?'':'none';const st=L.style||{},seen=new Set();
  if(L.hidden){G.g.textContent='';G.f={};if(G.cas){G.cas=sv('g',null,G.g);G.top=sv('g',null,G.g)}return}
  for(const f of ((L.geojson||{}).features||[])){if(!f||!f.geometry)continue;const p=f.properties||{},fid=String(f.id!=null?f.id:p.id!=null?p.id:Math.random());seen.add(fid);
   const gt=f.geometry.type,col=p._c||st.color||'#2f80ed';let F=G.f[fid];const sig=JSON.stringify(f.geometry.coordinates).length+':'+gt;
   if(F&&F.sig!==sig){F.node.remove();if(F.cas)F.cas.remove();delete G.f[fid];F=null}
   if(!F){F={sig:sig};if(/Point/.test(gt)){const q=geoPoints(f.geometry)[0]||[0,0],m=merc(q[0],q[1]),r=st.size||5;
      F.node=sv('circle',{cx:(m[0]-c.org[0]).toFixed(2),cy:(c.org[1]-m[1]).toFixed(2),r:r*(c.k||1),'data-r':r,class:'f'},G.top);F.node.style.vectorEffect='non-scaling-stroke';
      F.node.setAttribute('stroke','var(--surface)');F.node.setAttribute('stroke-width',1.6)}
    else{const line=/Line/.test(gt),d=geoPath(f.geometry,c.org);
     if(line){F.cas=sv('path',{d:d,fill:'none',stroke:'var(--surface)','stroke-width':(st.width||2.4)+2.6,'stroke-linecap':'round',opacity:.9},G.cas||G.top);
      F.node=sv('path',{d:d,fill:'none','stroke-width':st.width||2.4,'stroke-linecap':'round',class:'f'},G.top)}
     else F.node=sv('path',{d:d,'fill-rule':'evenodd','stroke-width':st.width&&L.kind==='fill'?st.width:1,class:'f'},G.top)}
    F.node.dataset.l=lid;F.node.dataset.f=fid;F.node.style.cursor='pointer';F.anchor=anchorOf(f.geometry);G.f[fid]=F}
   F.feat=f;const line=/Line/.test(gt),pt=/Point/.test(gt);
   if(line)F.node.setAttribute('stroke',col);else{F.node.setAttribute('fill',p.hidden&&opts.hatch?opts.hatch:col);F.node.setAttribute('fill-opacity',pt?1:(st.opacity!=null?st.opacity:.75));
    if(!pt)F.node.setAttribute('stroke',p._o||st.outline||darker(col,.38))}
   if(F.col&&F.col!==col&&!RM.matches){F.node.classList.remove('pulse');void F.node.getBoundingClientRect;F.node.classList.add('pulse');setTimeout(()=>F.node.classList.remove('pulse'),1500)}F.col=col;
   F.label=p._l!=null?String(p._l):(L.legacy&&p.label!=null?String(p.label):null);
   let t=F.node.querySelector('title');if(!t)t=sv('title',null,F.node);t.textContent=(F.label||L.name||'')}
  for(const k in G.f)if(!seen.has(k)){G.f[k].node.remove();if(G.f[k].cas)G.f[k].cas.remove();delete G.f[k]}});
 for(const k in c.L)if(!seenL.has(k)){c.L[k].g.remove();delete c.L[k]}
 legendOf(c,layers,opts);c.bmOn=!!(c.d&&c.d.basemap);
 if(first||!c.view)layout(c);else setView(c);applyXF()}
function legendOf(c,layers,opts){const body=c.legBody;body.textContent='';let any=false;
 if(opts.gradient){any=true;const gr=el('div','grad',null,body);gr.style.background='linear-gradient('+(RTL?'to left':'to right')+','+opts.gradient[0]+','+opts.gradient[1]+')';
  const rg=el('div','rng',null,body);rg.style.cssText='display:flex;justify-content:space-between;color:var(--muted);gap:12px';el('span',null,opts.gradient[2],rg);el('span',null,opts.gradient[3],rg)}
 else for(const L of layers){if(!L||L.hidden&&!L.name)continue;const leg=Array.isArray(L.legend)?L.legend:[];if(!leg.length&&layers.length<2)continue;any=true;
  const ly=el('div','ly',null,body);const h=el('label','lyh',null,ly);const cb=el('input',null,null,h);cb.type='checkbox';const G=c.L&&c.L[String(L.id)];cb.checked=!G||G.on;
  cb.onchange=()=>{if(G){G.on=cb.checked;G.g.style.display=G.on?'':'none';labels(c)}};el('span',null,L.name||'',h);
  for(const it of leg.slice(0,14)){const r=el('div','it',null,ly);const sw=el('i','sw'+(L.kind==='line'?' ln':L.kind==='point'?' pt':''),null,r);sw.style.background=it.color||'#999';el('span',null,String(it.label==null?'':it.label),r)}}
 c.leg.classList.toggle('hide',!any);if(c.legOpen==null){c.legOpen=(c.mp.getBoundingClientRect().width||600)>=520}c.leg.classList.toggle('closed',!c.legOpen)}
/* labels in screen pixels, largest first, never two on top of each other */
function labels(c){const s=c.lab;if(!c.view||!c.L){s.textContent='';return}const r=c.svgR||c.svg.getBoundingClientRect();s.setAttribute('viewBox','0 0 '+r.width+' '+r.height);
 s.textContent='';const boxes=[],k=r.width/c.view.w;const cand=[];
 for(const lid in c.L){const G=c.L[lid];if(!G.on)continue;for(const fid in G.f){const F=G.f[fid];if(!F.label||!F.anchor)continue;
  const p=px(c,F.anchor);if(p[0]<-20||p[1]<-20||p[0]>r.width+20||p[1]>r.height+20)continue;
  let size=0,big=false;const tw=Math.min(160,F.label.length*6.4+6);
  if(G.L&&G.L.kind==='fill'){const b=F.bb||(F.bb=fbox(F.feat.geometry));const bw=(b[2]-b[0])*k,bh=(b[3]-b[1])*k;size=Math.min(bw,bh);big=size>150;
   if(!big&&(bh<13||bw<tw*0.92))continue}
  let ang=0;if(G.L&&G.L.kind==='line'&&F.anchor.ang!=null){if(F.anchor.len*k<tw*1.15)continue;ang=-F.anchor.ang*180/Math.PI;if(ang>90)ang-=180;if(ang<-90)ang+=180}
  if(F.node.classList.contains('xoff'))continue;cand.push({F:F,p:p,size:size,big:big,ang:ang,pt:G.L&&G.L.kind==='point'})}}
 cand.sort((a,b)=>b.size-a.size);let n=0;
 for(const x of cand){if(n>=350)break;const t=x.F.label,w=Math.min(x.big?240:160,t.length*(x.big?8.6:6.4)+6),h=x.big?17:13;let cx=x.p[0],cy=x.p[1]+(x.pt?-10:4);
  const vert=Math.abs(x.ang)>45;const bx=vert?[cx-h/2,cy-w/2,cx+h/2,cy+w/2]:[cx-w/2,cy-h+2,cx+w/2,cy+3];if(boxes.some(o=>!(bx[2]<o[0]||bx[0]>o[2]||bx[3]<o[1]||bx[1]>o[3])))continue;boxes.push(bx);n++;
  const tx=sv('text',{x:cx.toFixed(1),y:cy.toFixed(1),'text-anchor':'middle',class:x.big?'big':x.pt?'pt':''},s);
  if(x.ang)tx.setAttribute('transform','rotate('+x.ang.toFixed(1)+' '+cx.toFixed(1)+' '+(cy-4).toFixed(1)+')');tx.textContent=t.length>26?t.slice(0,25)+'…':t}}
/* tap: popup, highlight, cross-filter */
function tapMap(c,target,e){const t=target&&target.closest&&target.closest('[data-f]');if(!t){closePop(c);if(S.xf&&S.xf.src===c.w.id)setXF(null);return}
 const G=c.L[t.dataset.l],F=G&&G.f[t.dataset.f];if(!F)return;const L=G.L,p=F.feat.properties||{};
 if(L.legacy&&c.w.type==='map'&&!(Array.isArray(L.popup)&&L.popup.length)){explain(c.w.id,t.dataset.f);return}
 if(c.w.type==='choropleth'){const key=p.key!=null?p.key:t.dataset.f;explain(c.w.id,key);return}
 hilite(c,F);const keys=p._k||{};let xf=null;for(const f in keys){if(chartsBy(f)){xf=[f,String(keys[f])];break}}
 if(xf)setXF(xf[0],xf[1],c.w.id);showPop(c,G,F,e)}
function hilite(c,F){c.hl.textContent='';if(!F)return;const n=F.node.cloneNode(false);n.removeAttribute('class');n.removeAttribute('data-f');if(n.tagName==='circle')n.setAttribute('r',((+n.dataset.r||5)+3)*(c.k||1));c.hl.appendChild(n)}
function showPop(c,G,F,e){const L=G.L,p=F.feat.properties||{},box=c.pop;box.textContent='';box.classList.remove('hide');
 const x=el('button','x','×',box);x.type='button';x.setAttribute('aria-label',T.close);x.onclick=()=>closePop(c);
 const h=el('h4',null,null,box);const sw=el('i',null,null,h);sw.style.background=F.col||'#999';el('span',null,F.label||L.name||'',h);
 if(F.label&&L.name)el('small',null,L.name,box);
 const pop=Array.isArray(L.popup)?L.popup:[];if(pop.length){const dl=el('dl',null,null,box);for(const q of pop){const v=p[q.key];el('dt',null,String(q.label||q.key),dl);
  el('dd',null,v==null||v===''?'–':(typeof v==='number'?nf(v):String(v)),dl)}}
 const acts=el('div','acts',null,box);const zb=el('button',null,T.zoomTo,acts);zb.type='button';zb.onclick=()=>{zoomBox(c,F.bb||(F.bb=fbox(F.feat.geometry)))};
 if(!L.legacy){const db=el('button',null,T.details,acts);db.type='button';db.onclick=()=>{closePop(c);explain(c.w.id,G.L.id+':'+F.node.dataset.f)}}
 const r=c.mp.getBoundingClientRect(),bw=Math.min(300,r.width*.85);let left=(e.clientX-r.left)+12,top=(e.clientY-r.top)+12;
 box.style.left='0px';box.style.top='0px';const bh=box.offsetHeight||120;if(left+bw>r.width-6)left=Math.max(6,(e.clientX-r.left)-bw-12);if(top+bh>r.height-6)top=Math.max(6,r.height-bh-6);
 box.style.left=left+'px';box.style.top=top+'px';box.style.insetInlineStart='auto'}
function closePop(c){c.pop.classList.add('hide');c.hl.textContent=''}
function chartsBy(field){for(const id in S.widgets){const x=S.widgets[id];if(x.d&&x.d.xf===field&&x.w.type!=='map')return true}return false}
function hasMaps(){for(const id in S.widgets){const x=S.widgets[id];if(x.w.type==='map'&&x.L)return true}return false}
function setXF(field,key,src){if(!field){S.xf=null}else if(S.xf&&S.xf.field===field&&S.xf.key===key&&src!==S.xf.src){S.xf=null}else S.xf={field:field,key:String(key),src:src||null};applyXF()}
function applyXF(){const xf=S.xf,pill=$('xpill');if(pill){pill.classList.toggle('on',!!xf);if(xf){let lab=xf.key;
  for(const id in S.widgets){const x=S.widgets[id];if(x.d&&x.d.xf===xf.field&&Array.isArray(x.d.items)){const it=x.d.items.find(i=>String(i.key)===xf.key);if(it&&it.label!=null){lab=String(it.label);break}}
   if(x.d&&x.d.xf===xf.field&&Array.isArray(x.d.options)){const it=x.d.options.find(i=>String(i.key)===xf.key);if(it&&it.label!=null){lab=String(it.label);break}}}
  $('xpillText').textContent=T.selected+': '+(lab===''?'—':lab);pill.title=T.clearSel;pill.setAttribute('aria-label',T.clearSel+' — '+lab)}}
 for(const id in S.widgets){const x=S.widgets[id];
  if(x.L){for(const lid in x.L){const G=x.L[lid];for(const fid in G.f){const F=G.f[fid],p=(F.feat&&F.feat.properties)||{};let on=null;
     if(xf){const ks=p._k;let v=ks&&ks[xf.field]!==undefined?ks[xf.field]:(G.L&&G.L.legacy&&p[xf.field]!==undefined?String(p[xf.field]):undefined);if(v!==undefined)on=String(v)===xf.key}
     F.node.classList.toggle('xoff',on===false);F.node.classList.toggle('xon',on===true)}}
   if(xf&&xf.src!==id)x.hl&&(x.hl.textContent='');cancelAnimationFrame(x.lraf);x.lraf=requestAnimationFrame(()=>labels(x))}
  else if(x.d){const match=xf&&x.d.xf===xf.field;x.el.querySelectorAll('[data-k]').forEach(n=>{n.classList.toggle('xsel',!!match&&n.dataset.k===xf.key);n.classList.toggle('xdim',!!match&&n.dataset.k!==xf.key)})}}}
/* the basemap the PC draws for the view */
function askBasemap(c){if(!c.view||!c.org||!c.bmOn)return;const v=c.view,r=c.svgR||c.svg.getBoundingClientRect();if(!r.width)return;
 const sw=unmerc(c.org[0]+v.x,c.org[1]-(v.y+v.h)),ne=unmerc(c.org[0]+v.x+v.w,c.org[1]-v.y);
 const bb=[+sw[0].toFixed(7),+sw[1].toFixed(7),+ne[0].toFixed(7),+ne[1].toFixed(7)];const key=bb.join(',')+'|'+Math.round(r.width)+'x'+Math.round(r.height);if(key===c.bmKey)return;c.bmKey=key;
 send({t:'basemap',widget:c.w.id,bbox:bb,w:Math.round(r.width),h:Math.round(r.height),d:Math.min(2,window.devicePixelRatio||1)})}
function onBasemap(m){const c=S.widgets[m.widget];if(!c||!c.org||!Array.isArray(m.bbox)||m.bbox.length!==4)return;if(!m.img){return}
 const a=merc(m.bbox[0],m.bbox[1]),b=merc(m.bbox[2],m.bbox[3]);const im=sv('image',{x:(a[0]-c.org[0]).toFixed(2),y:(c.org[1]-b[1]).toFixed(2),width:(b[0]-a[0]).toFixed(2),height:(b[1]-a[1]).toFixed(2),preserveAspectRatio:'none',class:'bm'},null);
 im.setAttribute('href',String(m.img));im.style.opacity='0';c.bmg.appendChild(im);const done=()=>{im.style.opacity='1';const old=[...c.bmg.querySelectorAll('image')].filter(x=>x!==im);setTimeout(()=>old.forEach(x=>x.remove()),450)};
 let fired=false;const once=()=>{if(!fired){fired=true;done()}};im.addEventListener("load",once);setTimeout(once,1500);c.mp.classList.add("hasbm")}
R.map={make(c){makeMap(c)},update(c,d){drawLayers(c,mapLayers(c,d),{})}};
R.choropleth={make(c){makeMap(c)},update(c,d){const st=c.w.style||{},lo=isFinite(d.min)?+d.min:0,hi=isFinite(d.max)&&+d.max>lo?+d.max:lo+1;
  const s0=okc(st.color0)||css('--seq0')||'#e3f1f6',s1=okc(st.color)||css('--seq1')||'#0b4f66',hid='url(#hatch'+c.w.id.replace(/\W/g,'')+')';const fc=d.geojson||{features:[]};
  for(const f of fc.features||[]){if(!f)continue;const p=f.properties||(f.properties={});const v=+p.value;p._c=p.hidden?hid:(!isFinite(v)?css('--surface2')||'#888':mix(s0,s1,Math.max(0,Math.min(1,(v-lo)/(hi-lo)))).replace(/^rgb\((\d+),(\d+),(\d+)\)$/,(m,r,g,b)=>'#'+[r,g,b].map(x=>(+x).toString(16).padStart(2,'0')).join('')));
   p._l=p.label!=null?String(p.label)+' · '+(p.hidden?T.hiddenItem:nf(v,st.decimals,true)):null;if(p.key!=null)f.id=f.id!=null?f.id:p.key}
  drawLayers(c,[{id:'L0',name:c.w.title||'',kind:'fill',legacy:true,geojson:fc,legend:[],style:{opacity:.88}}],{hatch:hid,gradient:[s0,s1,nf(lo,st.decimals,true),nf(hi,st.decimals,true)]})}};
R.feed={make(c){c.list=el('ul','feed',null,c.body);c.list.setAttribute('aria-live','polite');c.rows={};c.tick=()=>{for(const k in c.rows){const r=c.rows[k];r.t.textContent=rel(Date.now()/1000-r.ts)}}},
 update(c,d){const it=(Array.isArray(d.items)?d.items:[]).filter(i=>i&&typeof i==='object').slice().sort((a,b)=>(+b.ts||0)-(+a.ts||0)).slice(0,100);
  const first=!c.done,seen=new Set();c.done=true;let prev=null;
  for(const i of it){const k=[i.ts,i.who,i.what,i.layer].join('|');seen.add(k);let r=c.rows[k];
   if(!r){r={ts:+i.ts||0};r.el=el('li',first?null:'new');const who=String(i.who||'?');const av=el('span','av',(who.trim()[0]||'?').toUpperCase(),r.el);av.style.background=hashc(who);
    const w=el('div','what',null,r.el);el('b',null,who,w);el('span',null,' '+String(i.what||''),w);if(i.layer)el('span','lay',String(i.layer),w);
    r.t=el('time',null,'',r.el);r.t.dateTime=new Date(r.ts*1000).toISOString();r.t.title=dateText(r.ts,true);c.rows[k]=r}
   if(prev)prev.after(r.el);else c.list.prepend(r.el);prev=r.el;r.t.textContent=rel(Date.now()/1000-r.ts)}
  for(const k in c.rows)if(!seen.has(k)){c.rows[k].el.remove();delete c.rows[k]}
  if(!it.length&&!c.empty){c.empty=el('li','empty',T.nothing,c.list)}else if(it.length&&c.empty){c.empty.remove();c.empty=null}}};
R.text={make(c){c.box=el('div','txt',null,c.body)},update(c,d){c.box.textContent='';const t=String(d.text||'');
  for(const para of t.split(/\n{2,}/)){const p=el('p',null,null,c.box);para.split('\n').forEach((line,n)=>{if(n)p.appendChild(document.createElement('br'));
   line.split(/(\*\*[^*]+\*\*)/).forEach(part=>{if(/^\*\*[^*]+\*\*$/.test(part))el('strong',null,part.slice(2,-2),p);else if(part)p.appendChild(document.createTextNode(part))})})}}};
R.filter={make(c){c.box=el('div','chips',null,c.body);c.box.setAttribute('role','group');c.box.setAttribute('aria-label',c.w.title||'')},
 update(c,d){const fid=(c.w.options||{}).filter||((S.def.filters||[])[0]||{}).id||c.w.id,cur=new Set((Array.isArray(d.current)?d.current:[]).map(String));
  c.box.textContent='';const all=el('button','chip',T.all,c.box);all.type='button';all.setAttribute('aria-pressed',cur.size?'false':'true');
  all.onclick=()=>sendFilter(fid,null,c);
  for(const o of (Array.isArray(d.options)?d.options:[])){const k=String(o.key!=null?o.key:o.label);const b=el('button','chip',null,c.box);b.type='button';el('span',null,String(o.label!=null?o.label:k),b);
   if(o.count!=null)el('small',null,nf(+o.count,0),b);b.setAttribute('aria-pressed',cur.has(k)?'true':'false');b.dataset.k=k;
   b.onclick=()=>{const n=new Set(cur);if(n.has(k))n.delete(k);else n.add(k);sendFilter(fid,n.size?[...n]:null,c)}}}};
function sendFilter(fid,vals,c){S.filters[fid]=vals;const v={};v[fid]=vals;send({t:'filter',values:v});
 if(c&&c.d&&c.d.xf&&hasMaps()){if(vals&&vals.length===1)setXF(c.d.xf,String(vals[0]),c.w.id);else if(S.xf&&S.xf.field===c.d.xf)setXF(null)}
 if(c&&c.d){c.d=Object.assign({},c.d,{current:vals||[]});R.filter.update(c,c.d)}}
/* ---- what is behind a number, and its seal ---- */
function openSheet(title,body){$('sheetTitle').textContent=title;const b=$('sheetBody');b.textContent='';if(body)body(b);
 $('sheet').classList.remove('hide');$('scrim').classList.remove('hide');$('sheetClose').focus()}
function closeSheet(){$('sheet').classList.add('hide');$('scrim').classList.add('hide');S.explain=null;S.sealCode=null}
$('sheetClose').onclick=closeSheet;$('scrim').onclick=closeSheet;document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSheet()});
function explain(wid,key){const ctx=S.widgets[wid];S.explain={widget:wid,key:key==null?null:String(key)};
 openSheet(T.explain,b=>{el('p','sec',(ctx&&ctx.w.title||'')+(key!=null&&key!==''?' · '+key:''),b);el('p',null,T.loading,b).id='exLoad'});
 const m={t:'explain',widget:wid};if(key!=null)m.key=String(key);send(m)}
function onExplain(m){if(!S.explain||S.explain.widget!==m.widget||(S.explain.key||null)!==(m.key==null?null:String(m.key)))return;
 const b=$('sheetBody'),ld=$('exLoad');if(ld)ld.remove();const ctx=S.widgets[m.widget];
 el('p',null,fill(T.features,{n:nf(+m.count||0,0)}),b).style.fontWeight='650';
 const ul=el('ul','flist',null,b);for(const f of (Array.isArray(m.features)?m.features:[]).slice(0,200)){const li=el('li',null,null,ul);el('span',null,String(f.label!=null?f.label:f.id),li);
  const meta=[];if(f.by)meta.push(fill(T.byWho,{who:f.by}));if(f.when)meta.push(dateText(+f.when,true));el('small',null,meta.join(' · '),li);
  if(Array.isArray(f.fields)&&f.fields.length){const dl=el('dl','kvl',null,li);for(const q of f.fields){el('dt',null,String(q.label||''),dl);el('dd',null,q.value==null?'–':typeof q.value==='number'?nf(q.value):String(q.value),dl)}}}
 if(!m.features||!m.features.length)el('p','empty',T.nothing,b);if(m.more)el('p','tfoot',T.more,b)}
function openSeal(code){if(!code)return;S.sealCode=code;openSheet(T.sealTitle,b=>{el('p',null,null,b).appendChild(document.createTextNode(T.sealHint));
  const c=el('p','mono',code,b);c.style.cssText='font-size:20px;text-align:center;letter-spacing:.08em;font-weight:700';el('p',null,T.loading,b).id='sealLoad'});send({t:'seal',code:code})}
function onSeal(m){if(m.code!==S.sealCode)return;const b=$('sheetBody'),ld=$('sealLoad');if(ld)ld.remove();
 if(!m.statement){el('p','banner end',T.sealNo,b);return}const st=m.statement,dl=el('dl','kvl',null,b);
 const row=(k,v,mono)=>{if(v==null||v==='')return;el('dt',null,k,dl);const dd=el('dd',mono?'mono':null,typeof v==='object'?JSON.stringify(v):String(v),dl)};
 row(T.sDash,st.dash);row(T.sWidget,st.widget);row(T.sTitle,st.title);row(T.sValue,st.value);row(T.sFilters,st.filters&&Object.keys(st.filters).length?st.filters:null);
 row(T.sCard,st.card);row(T.sTs,isFinite(st.ts)?dateText(+st.ts,true):st.ts);row(T.sData,st.data,true);row(T.sSig,m.sig?String(m.sig).slice(0,32)+'…':null,true);
 const text=JSON.stringify({code:m.code,statement:st,sig:m.sig,pub:S.pub||undefined});const q=QR.svg(text);if(q){const d=el('div','qr',null,b);d.innerHTML=q}
 const cp=el('button','btn',T.copy,b);cp.type='button';cp.onclick=()=>{(navigator.clipboard?navigator.clipboard.writeText(text):Promise.reject()).then(()=>toast(T.copied)).catch(()=>{})}}
/* ---- settings ---- */
$('setBtn').onclick=()=>openSheet(T.settings,b=>{el('div','sec',T.theme,b);const g=el('div','opts',null,b);
 for(const th of THEMES){const o=el('button','opt',null,g);o.type='button';o.setAttribute('aria-pressed',S.theme===th?'true':'false');const sw=el('span','sw',null,o);
  const cs=getComputedStyle(document.documentElement);const probe=document.createElement('div');probe.dataset.theme=th;probe.style.display='none';document.body.appendChild(probe);
  const pc=getComputedStyle(probe);['--bg','--accent','--c2','--c3'].forEach(v=>{el('i',null,null,sw).style.background=pc.getPropertyValue(v)});probe.remove();
  el('span',null,T['th_'+th]||th,o);o.onclick=()=>{keep('biruni-dash-theme',th);setTheme(th);g.querySelectorAll('.opt').forEach(x=>x.setAttribute('aria-pressed','false'));o.setAttribute('aria-pressed','true')}}
 el('div','sec',T.language,b);const lg=el('div','opts',null,b);for(const k of Object.keys(NAMES)){if(!ALL[k])continue;const o=el('button','opt',NAMES[k],lg);o.type='button';o.lang=k.replace('_','-');
  o.setAttribute('aria-pressed',LANG===k?'true':'false');o.onclick=()=>{keep('biruni-lang',k);setLang(k);send({t:'hello',card:CARD,lang:LANG,again:true});closeSheet()}}
 if(LANG==='ar'||LANG==='fa'){const r=el('label','row2',null,b);el('span',null,T.digits,r);const cb=el('input',null,null,r);cb.type='checkbox';cb.checked=S.digits;
  cb.onchange=()=>{S.digits=cb.checked;keep('biruni-dash-digits',cb.checked?'1':'0');setLang(LANG)}}
 const r2=el('div','row2',null,b);const tvb=el('button','btn',T.tv,r2);tvb.type='button';tvb.onclick=()=>{closeSheet();setTV(true)};
 const pr=el('button','btn',T.print,r2);pr.type='button';pr.onclick=()=>{closeSheet();setTimeout(()=>window.print(),50)}});
let tvT=null;function setTV(on){S.tv=on;document.body.classList.toggle('tv',on);if(on){try{const r=document.documentElement.requestFullscreen&&document.documentElement.requestFullscreen();if(r&&r.catch)r.catch(()=>{})}catch(e){}tvCycle()}
 else{clearInterval(tvT);if(document.fullscreenElement&&document.exitFullscreen)document.exitFullscreen().catch(()=>{})}}
function tvCycle(){clearInterval(tvT);if(!S.tv)return;tvT=setInterval(()=>{const h=document.documentElement,max=h.scrollHeight-innerHeight;if(max<40)return;
 const next=scrollY+innerHeight*0.9;window.scrollTo({top:next>=max+5?0:next,behavior:RM.matches?'auto':'smooth'})},12000)}
$('tvBtn').onclick=()=>setTV(!S.tv);document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement&&S.tv)setTV(false)});
/* ---- messages from the PC ---- */
function onMessage(m){if(!m||typeof m!=='object')return;S.lastMsg=Date.now();
 if(S.conn!=='on'&&!S.ended)setConn('on');
 switch(m.t){
  case 'dash':S.def=m.def&&typeof m.def==='object'?m.def:{widgets:[]};S.card=m.card||null;S.pc=String(m.pc||'');S.pub=String(m.pub||'');
   if(!kept('biruni-dash-theme'))setTheme(S.def.theme||'biruni-light');$('wait').classList.add('hide');banner(null);build();fresh();for(const h of EXT.build){try{h(m)}catch(e){console.error(e)}}break;
  case 'data':{if(!S.def)return;const ws=m.widgets&&typeof m.widgets==='object'?m.widgets:{};S.rev=Math.max(S.rev,+m.rev||0);S.lastData=Date.now();
   for(const id in ws){S.data[id]=ws[id];const ctx=S.widgets[id];if(ctx)update(ctx,ws[id])}fresh();for(const h of EXT.data){try{h(m)}catch(e){console.error(e)}}break}
  case 'explain':onExplain(m);break;
  case 'seal':onSeal(m);break;
  case 'basemap':onBasemap(m);break;
  case 'error':{const why=String(m.why||'');const fatal=['expired','revoked','bad card','badcard'].includes(why);
   banner(why==='bad card'?'badcard':why,fatal,why);if(fatal){S.ended=true;setConn('off');$('wait').classList.add('hide');TR.stop()}break}
  case 'bye':S.ended=true;banner('ended',true);setConn('off');$('wait').classList.add('hide');TR.stop();break;
  default:{const hs=EXT.on[m.t];if(hs)for(const h of hs){try{h(m)}catch(e){console.error(e)}}break}
  case 'part':{if(!S.parts)S.parts={};const p=S.parts[m.id]||(S.parts[m.id]={n:+m.n||0,got:0,s:[]});if(p.s[m.i]==null){p.s[m.i]=String(m.s||'');p.got++}
   if(p.got>=p.n){delete S.parts[m.id];try{onMessage(JSON.parse(p.s.join('')))}catch(e){}}break}
 }}
/* ---- transports: connect(), send(obj), stop() ---- */
const EXT={on:{},data:[],build:[],lang:[]};
function send(o){S.sent.push(o);if(S.sent.length>50)S.sent.shift();try{TR.send(o)}catch(e){}}
const sleep2=ms=>new Promise(r=>setTimeout(r,ms));
const TR={send(){},stop(){},connect(){}};
if(TRANSPORT==='preview'){TR.send=o=>{if(typeof window.BiruniDash.onSend==='function')window.BiruniDash.onSend(o)};
 TR.connect=()=>{setConn('on');send({t:'hello',card:CARD,lang:LANG})}}
if(TRANSPORT==='lan'){const L={sid:null,n:0,stop:false,backoff:1000,
  base(){const p=location.pathname;return p.endsWith('/')?p:p+'/'},
  async post(m){const r=await fetch(this.base()+'m',{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({s:this.sid,m:m})});
   if(r.status===404||r.status===410){this.sid=null;throw new Error('gone')}if(!r.ok)throw new Error(String(r.status));const j=await r.json();if(j&&j.s)this.sid=j.s;
   for(const x of (j&&j.out)||[])onMessage(x)},
  async hello(){while(!this.stop){try{this.sid=null;this.n=0;await this.post({t:'hello',card:CARD,lang:LANG});this.backoff=1000;return true}
   catch(e){setConn(S.lastMsg?'wait':'wait');if(S.lastMsg&&Date.now()-S.lastMsg>60000)setConn('off');await sleep2(this.backoff);this.backoff=Math.min(this.backoff*2,30000)}}return false},
  async loop(){if(!await this.hello())return;while(!this.stop){try{const r=await fetch(this.base()+'ev?s='+encodeURIComponent(this.sid||'')+'&n='+this.n,{cache:'no-store'});
    if(r.status===404||r.status===410){if(!await this.hello())return;continue}if(!r.ok)throw new Error(String(r.status));
    const j=await r.json();if(j&&isFinite(j.n))this.n=+j.n;S.lastMsg=Date.now();if(S.conn!=='on'&&!S.ended)setConn('on');for(const x of (j&&j.out)||[])onMessage(x);this.backoff=1000}
   catch(e){if(this.stop)return;setConn(Date.now()-(S.lastMsg||0)>60000?'off':'wait');await sleep2(this.backoff);this.backoff=Math.min(this.backoff*2,30000)}}}};
 TR.send=o=>{if(o.t==='hello'&&!o.again)return;L.post(o).catch(()=>{})};TR.stop=()=>{L.stop=true};TR.connect=()=>{L.loop()}}
if(TRANSPORT==='bridge'){let pc=null,dc=null,sid=null,linking=false,retry=null,backoff=4000,done=false;
 const live=()=>!!(dc&&dc.readyState==='open');
 const drop=()=>{const p=pc;pc=null;dc=null;linking=false;if(p)try{p.close()}catch(e){}};
 const later=()=>{if(done)return;clearTimeout(retry);retry=setTimeout(connect,backoff);backoff=Math.min(backoff*2,60000)};
 async function connect(){if(done||!BK||linking||live())return;if(!SIG.up()){setConn('wait');return later()}
  linking=true;setConn('wait');const my=sid=rid();
  try{pc=new RTCPeerConnection({iceServers:BR.ice});const ch=dc=pc.createDataChannel('dash',{ordered:true}),me=pc;
   ch.onopen=()=>{linking=false;backoff=4000;setConn('on');banner(null);send({t:'hello',card:CARD,lang:LANG})};
   ch.onclose=()=>{if(pc===me){drop();if(!done){setConn('wait');banner('lost');later()}}};
   ch.onmessage=e=>{if(typeof e.data!=='string')return;let m;try{m=JSON.parse(e.data)}catch(x){return}onMessage(m)};
   pc.oniceconnectionstatechange=()=>{if(pc!==me)return;if(me.iceConnectionState==='failed'){drop();banner('p2pfail');later()}};
   await me.setLocalDescription(await me.createOffer());await gathered(me,3000);if(pc!==me)return;
   await SIG.send({t:'offer',sid:my,sdp:me.localDescription.sdp,z:canZ,app:'dash'},true);await sleep(20000);
   if(pc===me&&linking&&me.signalingState==='have-local-offer'){drop();banner('pcoff');setConn('off');later()}}catch(e){drop();setConn('wait');later()}}
 async function onSignal(o){if(o.t==='answer'&&o.sid===sid&&pc&&pc.signalingState==='have-local-offer'){try{await pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){drop();later()}}}
 TR.send=o=>{if(live())try{dc.send(JSON.stringify(o))}catch(e){}};
 TR.stop=()=>{done=true;clearTimeout(retry);drop()};
 TR.connect=async()=>{if(!KEYB||KEYB.length!==43){banner('nocard',true);$('wait').classList.add('hide');setConn('off');return}
  if(!window.isSecureContext||!window.RTCPeerConnection||!window.crypto||!crypto.subtle){banner('insecure',true);$('wait').classList.add('hide');setConn('off');return}
  await bridgeKey(KEYB);SIG.start(onSignal,n=>{if(n&&!linking&&!live()){backoff=4000;connect()}});
  addEventListener('online',()=>{backoff=4000;connect()});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!live()){backoff=4000;connect()}});
  setInterval(()=>{if(live())send({t:'ping'})},20000)}}
if(TRANSPORT==='lan')setInterval(()=>{if(S.conn==='on'&&!S.ended)fresh()},20000);
/* ---- the hook for Biruni (preview) and for tests ---- */
$('xpill').onclick=()=>setXF(null);
window.BiruniDash={version:2,setXF:(f,k)=>setXF(f,k,'api'),transport:TRANSPORT,feed:m=>onMessage(typeof m==='string'?JSON.parse(m):m),onSend:null,state:S,
 setTheme:t=>setTheme(t),setLang:l=>setLang(pick(l)||'en'),setTV:on=>setTV(!!on),themes:THEMES.slice(),explain:explain,openSeal:openSeal,close:closeSheet,
 send:o=>send(o),on:(t,fn)=>{(EXT.on[t]=EXT.on[t]||[]).push(fn)},onData:fn=>EXT.data.push(fn),onBuild:fn=>EXT.build.push(fn),onLang:fn=>EXT.lang.push(fn),
 t:k=>T[k]!==undefined?T[k]:k,lang:()=>LANG,rtl:()=>RTL,widgets:()=>S.widgets,sheet:(title,body)=>{$('sheetTitle').textContent=title;const b=$('sheetBody');b.textContent='';if(body)b.appendChild(body);$('sheet').classList.remove('hide');$('scrim').classList.remove('hide')},toast:m=>toast(m),button:(id,label,svgPath,fn)=>{const b=document.createElement('button');b.className='icon';b.type='button';b.id=id;b.title=label;b.setAttribute('aria-label',label);b.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="'+svgPath+'"/></svg>';b.onclick=fn;$('extBtns').appendChild(b);return b}};
setTheme(kept('biruni-dash-theme')||'biruni-light');
setLang(pick(kept('biruni-lang'))||pick(HASHL)||pick(DEFLANG)||pick(navigator.language)||'en');
if(S.tv)setTV(true);
if(TRANSPORT!=='preview'&&!CARD){banner('nocard',true);$('wait').classList.add('hide');setConn('off')}else TR.connect();
"""
