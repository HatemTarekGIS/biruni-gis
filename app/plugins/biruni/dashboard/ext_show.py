"""Dashboard extension "show": the phone wall, «اللوحة بتحكي» (narrative + voice), the expected finish dates on the
map (forecast), the project film and the meeting mode. See SPEC.md "Extensions" for how an extension plugs in.

PC side: the handlers below (messages show_*), the history kept by showkit, and two actions for the dashboard
panel (start a meeting, set up a phone wall). Page side: CSS, HTML, JS and TEXTS.

Widget conventions (no change to model.py; everything rides in ``style``, which reaches the page as it is):
* a forecast widget is a ``text`` widget with ``style.show = "forecast"`` and ``style.of = <progress widget id>``;
* a map plays its film from the team room's history, or from ``options.film_date`` (a date field, PC side only).
"""
import time

from . import film, forecast, meeting, narrative, showkit, wall
from .service import handler

showkit.observe()

_walls = {}       # (id(service), dash) -> wall.Wall
_meetings = {}    # (id(service), dash) -> meeting.Meeting


def _key(svc, s):
    return (id(svc), s.dash_id)


def _others(svc, s):
    return [x for x in list(svc.sessions) if x.ready and x is not s and x.dash_id == s.dash_id and not x.closed]


# ---- narrative ------------------------------------------------------------------------------------------------------
@handler("show_story")
def _story(svc, s, msg):
    lang = "ar" if str(msg.get("lang") or "") == "ar" else "en"
    cur, dfc, _raw = showkit.current(svc, s)
    prev = showkit.last_visit(s.dash_id, s.card)
    hist = showkit.history(s.dash_id, s.card)
    now = time.time()
    lines = narrative.story(prev and prev.get("w"), cur, narrative.titles_of(dfc), hist, lang, now,
                            prev and prev.get("ts"))
    showkit.record(s.dash_id, s.card, cur, now)
    showkit.visit(s.dash_id, s.card, cur, now)
    s._out({"t": "show_story", "lines": lines, "lang": lang, "first": prev is None})


# ---- forecast -------------------------------------------------------------------------------------------------------
@handler("show_forecast")
def _forecast(svc, s, msg):
    wid = str(msg.get("widget") or "")
    cur, dfc, _raw = showkit.current(svc, s)
    w = next((x for x in dfc.get("widgets") or [] if x.get("id") == wid), None)
    st = (w or {}).get("style") or {}
    of = st.get("of")
    if w is None or st.get("show") != "forecast" or not of or of not in {x.get("id") for x in dfc.get("widgets") or []}:
        s._out({"t": "show_err", "why": "no forecast"})
        return
    out = forecast.forecast_of(showkit.history(s.dash_id, s.card), of, cur.get(of), time.time())
    s._out(dict(out, t="show_forecast", widget=wid, of=of))


# ---- film -----------------------------------------------------------------------------------------------------------
@handler("show_film")
def _film(svc, s, msg):
    s._out(film.build(svc, s, str(msg.get("widget") or "")))


# ---- the phone wall -------------------------------------------------------------------------------------------------
def start_wall(svc, dash, rows=1, cols=3, bezel=16):
    w = _walls[(id(svc), dash)] = wall.Wall(dash, rows, cols, bezel)
    return w


def stop_wall(svc, dash):
    w = _walls.pop((id(svc), dash), None)
    if w:
        for m in list(w.members.values()):
            m["session"]._out({"t": "show_wall_end"})


def _wall_tell(w):
    for sess, m in w.messages():
        sess._out(m)


@handler("show_wall_join")
def _wall_join(svc, s, msg):
    w = _walls.get(_key(svc, s))
    if w is None:
        w = start_wall(svc, s.dash_id)
    try:
        slot = w.join(s, msg.get("w") or 400, msg.get("h") or 800, msg.get("dpr") or 1)
    except (TypeError, ValueError):
        slot = None
    if slot is None:
        s._out({"t": "show_err", "why": "wall full"})
        return
    _wall_tell(w)


@handler("show_wall_slot")
def _wall_slot(svc, s, msg):
    w = _walls.get(_key(svc, s))
    try:
        ok = w is not None and w.move(s, int(msg.get("slot")))
    except (TypeError, ValueError):
        ok = False
    if not ok:
        s._out({"t": "show_err", "why": "bad slot"})
        return
    _wall_tell(w)


@handler("show_wall_view")
def _wall_view(svc, s, msg):
    w = _walls.get(_key(svc, s))
    if w is None or s.id not in w.members:
        s._out({"t": "show_err", "why": "not on the wall"})
        return
    v = meeting.clean_view(msg.get("view"))
    if not v or not all(k in v for k in ("cx", "cy", "mpp")):
        return
    w.view = v
    for m in w.members.values():
        if m["session"] is not s:
            m["session"]._out({"t": "show_wall_view", "view": v, "from": w.members[s.id]["slot"]})


@handler("show_wall_leave")
def _wall_leave(svc, s, msg):
    w = _walls.get(_key(svc, s))
    if w is not None:
        w.leave(s)
        _wall_tell(w)
    s._out({"t": "show_wall_end"})


# ---- the meeting ----------------------------------------------------------------------------------------------------
def start_meeting(svc, dash):
    m = _meetings[(id(svc), dash)] = meeting.Meeting(dash)
    for x in list(svc.sessions):
        if x.ready and x.dash_id == dash:
            x._out(_meet_state(m, x))
    return m


def stop_meeting(svc, dash):
    _meetings.pop((id(svc), dash), None)
    for x in list(svc.sessions):
        if x.ready and x.dash_id == dash:
            x._out({"t": "show_meet_state", "active": False})


def _meet_state(m, s):
    return {"t": "show_meet_state", "active": True, "presenter": m.presenter_name if m.presenter else "",
            "you": m.is_presenter(s), "view": m.view}


@handler("show_meet_hello")
def _meet_hello(svc, s, msg):
    m = _meetings.get(_key(svc, s))
    s._out(_meet_state(m, s) if m else {"t": "show_meet_state", "active": False})


@handler("show_meet_claim")
def _meet_claim(svc, s, msg):
    m = _meetings.get(_key(svc, s))
    if m is None:
        s._out({"t": "show_err", "why": "no meeting"})
        return
    why = m.claim(s, msg.get("pin"))
    if why:
        s._out({"t": "show_err", "why": why})
        return
    for x in [s] + _others(svc, s):
        x._out(_meet_state(m, x))


@handler("show_meet_view")
def _meet_view(svc, s, msg):
    m = _meetings.get(_key(svc, s))
    if m is None or not m.is_presenter(s):
        s._out({"t": "show_err", "why": "not the presenter"})
        return
    v = meeting.clean_view(msg.get("view"))
    if v is None:
        return
    m.view = v
    for x in _others(svc, s):
        x._out({"t": "show_meet", "view": v, "presenter": m.presenter_name})


@handler("show_meet_laser")
def _meet_laser(svc, s, msg):
    m = _meetings.get(_key(svc, s))
    if m is None or not m.is_presenter(s):
        s._out({"t": "show_err", "why": "not the presenter"})
        return
    v = meeting.clean_laser(msg)
    if v is None:
        return
    for x in _others(svc, s):
        x._out(dict(v, t="show_meet_laser"))


@handler("show_meet_end")
def _meet_end(svc, s, msg):
    m = _meetings.get(_key(svc, s))
    if m is not None and m.release(s):
        for x in [s] + _others(svc, s):
            x._out(_meet_state(m, x))


# ---- the dashboard panel's actions ----------------------------------------------------------------------------------
def _meeting_action(plugin, dock):
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QFont
    from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

    from ..i18n import tr

    d = dock.current()
    if not d:
        return
    svc = dock.service()
    m = start_meeting(svc, d["id"])
    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Meeting mode"))
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(tr("The presenter types this PIN on their phone (Present button) to drive everyone's "
                            "dashboard:")))
    pin = QLabel(m.pin[:3] + " " + m.pin[3:])
    f = QFont()
    f.setPointSize(36)
    f.setBold(True)
    pin.setFont(f)
    pin.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(pin)
    bb = QDialogButtonBox()
    end = bb.addButton(tr("End the meeting"), QDialogButtonBox.ButtonRole.DestructiveRole)
    bb.addButton(QDialogButtonBox.StandardButton.Close)
    end.clicked.connect(lambda: (stop_meeting(svc, d["id"]), dlg.accept()))
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.show()
    dock._show_meeting_dialog = dlg


def _wall_action(plugin, dock):
    from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox)

    from ..i18n import tr

    d = dock.current()
    if not d:
        return
    svc = dock.service()
    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Phone wall"))
    lay = QFormLayout(dlg)
    lay.addRow(QLabel(tr("Phones side by side show one big map. Lay them out, then tap «Phone wall» on each.")))
    rows, cols, bez = QSpinBox(), QSpinBox(), QSpinBox()
    rows.setRange(1, 4)
    cols.setRange(1, 6)
    bez.setRange(0, 80)
    rows.setValue(1)
    cols.setValue(3)
    bez.setValue(16)
    lay.addRow(tr("Rows"), rows)
    lay.addRow(tr("Columns"), cols)
    lay.addRow(tr("Gap between screens (px)"), bez)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addRow(bb)
    if dlg.exec():
        old = _walls.get((id(svc), d["id"]))
        w = start_wall(svc, d["id"], rows.value(), cols.value(), bez.value())
        if old:   # the phones already on the wall stay
            for m in old.members.values():
                w.join(m["session"], m["w"], m["h"], m["dpr"])
            _wall_tell(w)


ACTIONS = [
    ("show_meeting", "Start a meeting (presenter PIN)…", "presentation", _meeting_action),
    ("show_wall", "Phone wall…", "layout-grid", _wall_action),
]


# ---- the page ---------------------------------------------------------------------------------------------------------
TEXTS = {
    "en": {"show_since": "Since your last visit", "show_first": "Welcome", "show_listen": "Listen", "show_stop": "Stop",
           "show_close": "Close", "show_noVoice": "This browser has no voice for this language.",
           "show_fcTitle": "Expected finish", "show_fcAll": "Whole job", "show_fcBetween": "between {a} and {b}",
           "show_fcDone": "Finished", "show_fcSlow": "No progress yet", "show_fcRate": "{r} a day",
           "show_fcColour": "Colour the map by finish week", "show_fcOff": "Back to the map's colours",
           "show_fcWeek": "Week of {d}", "show_fcLate": "Later", "show_fcLegend": "Expected finish",
           "show_film": "Project film", "show_play": "Play", "show_pause": "Pause", "show_speed": "Speed",
           "show_rec": "Record video", "show_recStop": "Stop recording", "show_recNo": "This browser cannot record video.",
           "show_recSaved": "The video is ready.", "show_filmNo": "There is no history for this map yet.",
           "show_changed": "{n} changes",
           "show_wall": "Phone wall", "show_wallJoin": "Join the phone wall", "show_wallHelp":
               "Lay the phones side by side, open this dashboard on each, and tap Join on each. Each phone shows its "
               "part of one big map.", "show_wallSlot": "Screen {n} of {m}", "show_wallStart": "Start",
           "show_wallLeave": "Leave the wall", "show_wallLeft": "Move left", "show_wallRight": "Move right",
           "show_wallUp": "Move up", "show_wallDown": "Move down", "show_wallFull": "The wall is full.",
           "show_meet": "Meeting", "show_meetNone": "No meeting is running on the computer.",
           "show_meetPin": "PIN shown on the computer", "show_meetPresent": "Present",
           "show_meetYou": "You are presenting. Everyone follows your map.", "show_meetBy": "Following {name}",
           "show_meetStop": "Stop following", "show_meetFollow": "Follow again", "show_meetLaser": "Laser pointer",
           "show_meetEnd": "Stop presenting", "show_meetWrong": "Wrong PIN.", "show_meetMany": "Too many tries.",
           "show_meetFollowing": "Following the presenter"},
    "ar": {"show_since": "منذ زيارتك الأخيرة", "show_first": "أهلًا بك", "show_listen": "استمع", "show_stop": "إيقاف",
           "show_close": "إغلاق", "show_noVoice": "لا يتوفر في هذا المتصفح صوت لهذه اللغة.",
           "show_fcTitle": "موعد الانتهاء المتوقع", "show_fcAll": "المشروع كله", "show_fcBetween": "بين {a} و{b}",
           "show_fcDone": "اكتمل", "show_fcSlow": "لا تقدّم بعد", "show_fcRate": "{r} يوميًا",
           "show_fcColour": "لوّن الخريطة بأسبوع الانتهاء", "show_fcOff": "عودة إلى ألوان الخريطة",
           "show_fcWeek": "أسبوع {d}", "show_fcLate": "لاحقًا", "show_fcLegend": "الانتهاء المتوقع",
           "show_film": "فيلم المشروع", "show_play": "تشغيل", "show_pause": "إيقاف مؤقت", "show_speed": "السرعة",
           "show_rec": "تسجيل فيديو", "show_recStop": "إيقاف التسجيل", "show_recNo": "لا يستطيع هذا المتصفح تسجيل الفيديو.",
           "show_recSaved": "الفيديو جاهز.", "show_filmNo": "لا يوجد تاريخ لهذه الخريطة بعد.",
           "show_changed": "{n} تغييرًا",
           "show_wall": "جدار الهواتف", "show_wallJoin": "انضم إلى جدار الهواتف", "show_wallHelp":
               "ضع الهواتف جنبًا إلى جنب، وافتح هذه اللوحة على كل منها، ثم اضغط «انضم» على كل هاتف. يعرض كل هاتف "
               "جزءه من خريطة واحدة كبيرة.", "show_wallSlot": "الشاشة {n} من {m}", "show_wallStart": "ابدأ",
           "show_wallLeave": "مغادرة الجدار", "show_wallLeft": "إلى اليسار", "show_wallRight": "إلى اليمين",
           "show_wallUp": "إلى الأعلى", "show_wallDown": "إلى الأسفل", "show_wallFull": "الجدار ممتلئ.",
           "show_meet": "الاجتماع", "show_meetNone": "لا يوجد اجتماع جارٍ على الحاسوب.",
           "show_meetPin": "الرمز الظاهر على الحاسوب", "show_meetPresent": "قدّم العرض",
           "show_meetYou": "أنت المقدِّم. الجميع يتابعون خريطتك.", "show_meetBy": "تتابع {name}",
           "show_meetStop": "أوقف المتابعة", "show_meetFollow": "تابع مجددًا", "show_meetLaser": "مؤشر الليزر",
           "show_meetEnd": "إنهاء التقديم", "show_meetWrong": "الرمز غير صحيح.", "show_meetMany": "محاولات كثيرة.",
           "show_meetFollowing": "تتابع المقدِّم"},
    "es": {"show_since": "Desde tu última visita", "show_first": "Bienvenido", "show_listen": "Escuchar",
           "show_stop": "Detener", "show_close": "Cerrar", "show_noVoice": "Este navegador no tiene voz para este idioma.",
           "show_fcTitle": "Fin previsto", "show_fcAll": "Todo el trabajo", "show_fcBetween": "entre {a} y {b}",
           "show_fcDone": "Terminado", "show_fcSlow": "Sin avance aún", "show_fcRate": "{r} al día",
           "show_fcColour": "Colorear el mapa por semana de fin", "show_fcOff": "Volver a los colores del mapa",
           "show_fcWeek": "Semana del {d}", "show_fcLate": "Más tarde", "show_fcLegend": "Fin previsto",
           "show_film": "Película del proyecto", "show_play": "Reproducir", "show_pause": "Pausa",
           "show_speed": "Velocidad", "show_rec": "Grabar vídeo", "show_recStop": "Detener grabación",
           "show_recNo": "Este navegador no puede grabar vídeo.", "show_recSaved": "El vídeo está listo.",
           "show_filmNo": "Aún no hay historial para este mapa.", "show_changed": "{n} cambios",
           "show_wall": "Muro de teléfonos", "show_wallJoin": "Unirse al muro", "show_wallHelp":
               "Coloca los teléfonos uno junto a otro, abre este panel en cada uno y pulsa Unirse. Cada teléfono "
               "muestra su parte de un gran mapa.", "show_wallSlot": "Pantalla {n} de {m}", "show_wallStart": "Empezar",
           "show_wallLeave": "Salir del muro", "show_wallLeft": "A la izquierda", "show_wallRight": "A la derecha",
           "show_wallUp": "Arriba", "show_wallDown": "Abajo", "show_wallFull": "El muro está completo.",
           "show_meet": "Reunión", "show_meetNone": "No hay ninguna reunión en el ordenador.",
           "show_meetPin": "PIN mostrado en el ordenador", "show_meetPresent": "Presentar",
           "show_meetYou": "Estás presentando. Todos siguen tu mapa.", "show_meetBy": "Siguiendo a {name}",
           "show_meetStop": "Dejar de seguir", "show_meetFollow": "Seguir de nuevo", "show_meetLaser": "Puntero láser",
           "show_meetEnd": "Dejar de presentar", "show_meetWrong": "PIN incorrecto.", "show_meetMany": "Demasiados intentos.",
           "show_meetFollowing": "Siguiendo al presentador"},
    "fr": {"show_since": "Depuis votre dernière visite", "show_first": "Bienvenue", "show_listen": "Écouter",
           "show_stop": "Arrêter", "show_close": "Fermer", "show_noVoice": "Ce navigateur n'a pas de voix pour cette langue.",
           "show_fcTitle": "Fin prévue", "show_fcAll": "Tout le chantier", "show_fcBetween": "entre {a} et {b}",
           "show_fcDone": "Terminé", "show_fcSlow": "Pas encore d'avancement", "show_fcRate": "{r} par jour",
           "show_fcColour": "Colorer la carte par semaine de fin", "show_fcOff": "Revenir aux couleurs de la carte",
           "show_fcWeek": "Semaine du {d}", "show_fcLate": "Plus tard", "show_fcLegend": "Fin prévue",
           "show_film": "Film du projet", "show_play": "Lire", "show_pause": "Pause", "show_speed": "Vitesse",
           "show_rec": "Enregistrer une vidéo", "show_recStop": "Arrêter l'enregistrement",
           "show_recNo": "Ce navigateur ne peut pas enregistrer de vidéo.", "show_recSaved": "La vidéo est prête.",
           "show_filmNo": "Pas encore d'historique pour cette carte.", "show_changed": "{n} changements",
           "show_wall": "Mur de téléphones", "show_wallJoin": "Rejoindre le mur", "show_wallHelp":
               "Posez les téléphones côte à côte, ouvrez ce tableau sur chacun et touchez Rejoindre. Chaque téléphone "
               "montre sa part d'une grande carte.", "show_wallSlot": "Écran {n} sur {m}", "show_wallStart": "Démarrer",
           "show_wallLeave": "Quitter le mur", "show_wallLeft": "Vers la gauche", "show_wallRight": "Vers la droite",
           "show_wallUp": "Vers le haut", "show_wallDown": "Vers le bas", "show_wallFull": "Le mur est complet.",
           "show_meet": "Réunion", "show_meetNone": "Aucune réunion en cours sur l'ordinateur.",
           "show_meetPin": "Code affiché sur l'ordinateur", "show_meetPresent": "Présenter",
           "show_meetYou": "Vous présentez. Tout le monde suit votre carte.", "show_meetBy": "Vous suivez {name}",
           "show_meetStop": "Ne plus suivre", "show_meetFollow": "Suivre à nouveau", "show_meetLaser": "Pointeur laser",
           "show_meetEnd": "Arrêter de présenter", "show_meetWrong": "Code incorrect.", "show_meetMany": "Trop d'essais.",
           "show_meetFollowing": "Vous suivez le présentateur"},
    "pt_BR": {"show_since": "Desde sua última visita", "show_first": "Bem-vindo", "show_listen": "Ouvir",
              "show_stop": "Parar", "show_close": "Fechar", "show_noVoice": "Este navegador não tem voz para este idioma.",
              "show_fcTitle": "Término previsto", "show_fcAll": "Trabalho inteiro", "show_fcBetween": "entre {a} e {b}",
              "show_fcDone": "Concluído", "show_fcSlow": "Sem avanço ainda", "show_fcRate": "{r} por dia",
              "show_fcColour": "Colorir o mapa pela semana de término", "show_fcOff": "Voltar às cores do mapa",
              "show_fcWeek": "Semana de {d}", "show_fcLate": "Mais tarde", "show_fcLegend": "Término previsto",
              "show_film": "Filme do projeto", "show_play": "Reproduzir", "show_pause": "Pausar", "show_speed": "Velocidade",
              "show_rec": "Gravar vídeo", "show_recStop": "Parar gravação",
              "show_recNo": "Este navegador não consegue gravar vídeo.", "show_recSaved": "O vídeo está pronto.",
              "show_filmNo": "Ainda não há histórico para este mapa.", "show_changed": "{n} mudanças",
              "show_wall": "Parede de celulares", "show_wallJoin": "Entrar na parede", "show_wallHelp":
                  "Coloque os celulares lado a lado, abra este painel em cada um e toque em Entrar. Cada celular mostra "
                  "sua parte de um grande mapa.", "show_wallSlot": "Tela {n} de {m}", "show_wallStart": "Começar",
              "show_wallLeave": "Sair da parede", "show_wallLeft": "Para a esquerda", "show_wallRight": "Para a direita",
              "show_wallUp": "Para cima", "show_wallDown": "Para baixo", "show_wallFull": "A parede está cheia.",
              "show_meet": "Reunião", "show_meetNone": "Não há reunião em andamento no computador.",
              "show_meetPin": "PIN mostrado no computador", "show_meetPresent": "Apresentar",
              "show_meetYou": "Você está apresentando. Todos seguem seu mapa.", "show_meetBy": "Seguindo {name}",
              "show_meetStop": "Parar de seguir", "show_meetFollow": "Seguir de novo", "show_meetLaser": "Ponteiro laser",
              "show_meetEnd": "Parar de apresentar", "show_meetWrong": "PIN incorreto.", "show_meetMany": "Tentativas demais.",
              "show_meetFollowing": "Seguindo o apresentador"},
    "tr": {"show_since": "Son ziyaretinizden beri", "show_first": "Hoş geldiniz", "show_listen": "Dinle",
           "show_stop": "Durdur", "show_close": "Kapat", "show_noVoice": "Bu tarayıcıda bu dil için ses yok.",
           "show_fcTitle": "Beklenen bitiş", "show_fcAll": "Tüm iş", "show_fcBetween": "{a} ile {b} arasında",
           "show_fcDone": "Bitti", "show_fcSlow": "Henüz ilerleme yok", "show_fcRate": "günde {r}",
           "show_fcColour": "Haritayı bitiş haftasına göre renklendir", "show_fcOff": "Haritanın renklerine dön",
           "show_fcWeek": "{d} haftası", "show_fcLate": "Daha sonra", "show_fcLegend": "Beklenen bitiş",
           "show_film": "Proje filmi", "show_play": "Oynat", "show_pause": "Duraklat", "show_speed": "Hız",
           "show_rec": "Video kaydet", "show_recStop": "Kaydı durdur", "show_recNo": "Bu tarayıcı video kaydedemiyor.",
           "show_recSaved": "Video hazır.", "show_filmNo": "Bu harita için henüz geçmiş yok.", "show_changed": "{n} değişiklik",
           "show_wall": "Telefon duvarı", "show_wallJoin": "Duvara katıl", "show_wallHelp":
               "Telefonları yan yana koyun, bu paneli her birinde açın ve Katıl'a dokunun. Her telefon büyük bir "
               "haritanın kendi parçasını gösterir.", "show_wallSlot": "Ekran {n} / {m}", "show_wallStart": "Başla",
           "show_wallLeave": "Duvardan ayrıl", "show_wallLeft": "Sola", "show_wallRight": "Sağa",
           "show_wallUp": "Yukarı", "show_wallDown": "Aşağı", "show_wallFull": "Duvar dolu.",
           "show_meet": "Toplantı", "show_meetNone": "Bilgisayarda süren bir toplantı yok.",
           "show_meetPin": "Bilgisayarda görünen PIN", "show_meetPresent": "Sun",
           "show_meetYou": "Sunuyorsunuz. Herkes haritanızı izliyor.", "show_meetBy": "{name} izleniyor",
           "show_meetStop": "İzlemeyi bırak", "show_meetFollow": "Yeniden izle", "show_meetLaser": "Lazer işaretçi",
           "show_meetEnd": "Sunmayı bırak", "show_meetWrong": "Yanlış PIN.", "show_meetMany": "Çok fazla deneme.",
           "show_meetFollowing": "Sunucu izleniyor"},
    "fa": {"show_since": "از آخرین بازدید شما", "show_first": "خوش آمدید", "show_listen": "گوش دهید",
           "show_stop": "توقف", "show_close": "بستن", "show_noVoice": "این مرورگر صدایی برای این زبان ندارد.",
           "show_fcTitle": "پایان پیش‌بینی‌شده", "show_fcAll": "کل کار", "show_fcBetween": "بین {a} و {b}",
           "show_fcDone": "تمام شد", "show_fcSlow": "هنوز پیشرفتی نیست", "show_fcRate": "{r} در روز",
           "show_fcColour": "رنگ‌آمیزی نقشه با هفتهٔ پایان", "show_fcOff": "بازگشت به رنگ‌های نقشه",
           "show_fcWeek": "هفتهٔ {d}", "show_fcLate": "بعدتر", "show_fcLegend": "پایان پیش‌بینی‌شده",
           "show_film": "فیلم پروژه", "show_play": "پخش", "show_pause": "مکث", "show_speed": "سرعت",
           "show_rec": "ضبط ویدیو", "show_recStop": "توقف ضبط", "show_recNo": "این مرورگر نمی‌تواند ویدیو ضبط کند.",
           "show_recSaved": "ویدیو آماده است.", "show_filmNo": "هنوز تاریخچه‌ای برای این نقشه نیست.",
           "show_changed": "{n} تغییر",
           "show_wall": "دیوار گوشی‌ها", "show_wallJoin": "پیوستن به دیوار", "show_wallHelp":
               "گوشی‌ها را کنار هم بگذارید، این داشبورد را روی هرکدام باز کنید و «پیوستن» را بزنید. هر گوشی بخش خودش "
               "از یک نقشهٔ بزرگ را نشان می‌دهد.", "show_wallSlot": "صفحهٔ {n} از {m}", "show_wallStart": "شروع",
           "show_wallLeave": "ترک دیوار", "show_wallLeft": "به چپ", "show_wallRight": "به راست",
           "show_wallUp": "به بالا", "show_wallDown": "به پایین", "show_wallFull": "دیوار پر است.",
           "show_meet": "جلسه", "show_meetNone": "جلسه‌ای روی رایانه در جریان نیست.",
           "show_meetPin": "رمز نمایش‌داده‌شده روی رایانه", "show_meetPresent": "ارائه",
           "show_meetYou": "شما ارائه می‌دهید. همه نقشهٔ شما را دنبال می‌کنند.", "show_meetBy": "در حال دنبال کردن {name}",
           "show_meetStop": "توقف دنبال کردن", "show_meetFollow": "دنبال کردن دوباره", "show_meetLaser": "نشانگر لیزری",
           "show_meetEnd": "پایان ارائه", "show_meetWrong": "رمز نادرست است.", "show_meetMany": "تلاش‌های زیاد.",
           "show_meetFollowing": "در حال دنبال کردن ارائه‌دهنده"},
}

CSS = r"""
.show-story{margin:12px 16px 0;padding:14px 16px;border-radius:14px;background:var(--surface);border:1px solid var(--line);
 box-shadow:0 1px 0 rgba(0,0,0,.04);position:relative;animation:showIn .4s ease}
.show-story h3{margin:0 0 6px;font-size:15px;display:flex;align-items:center;gap:8px}
.show-story h3 i{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 25%,transparent)}
.show-story ul{margin:0;padding-inline-start:18px;line-height:1.75;font-size:14px}
.show-filming .gleg,.show-filming .mscale,.show-filming .mnorth{display:none!important}
.show-story .acts{display:flex;gap:8px;margin-top:8px}
.show-story button,.show-btn{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--surface2);color:var(--text);
 border-radius:999px;padding:5px 12px;cursor:pointer}
.show-story button:hover,.show-btn:hover{border-color:var(--accent)}
@keyframes showIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.show-fc .body{display:none}
.show-fcbox{display:flex;flex-direction:column;gap:8px;padding:2px 14px 12px;height:calc(100% - 44px);overflow:auto}
.show-fcall{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.show-fcall b{font-size:26px;font-weight:750;color:var(--accent)}
.show-fcall span{color:var(--muted);font-size:13px}
.show-fcz{display:grid;grid-template-columns:1fr auto;gap:2px 10px;font-size:13px}
.show-fcz .lb{color:var(--text)}.show-fcz .d{font-weight:650;text-align:end}.show-fcz .r{grid-column:1/-1;color:var(--muted);font-size:12px;margin-bottom:4px}
.show-fcsvg{width:100%;height:150px;flex:none}
.show-fcsvg .cone{fill:var(--accent);opacity:.18}.show-fcsvg .hist{fill:none;stroke:var(--accent);stroke-width:2.4}
.show-fcsvg .mid{fill:none;stroke:var(--accent);stroke-width:1.6;stroke-dasharray:5 4}
.show-fcsvg .tot{stroke:var(--muted);stroke-dasharray:2 4}.show-fcsvg text{fill:var(--muted);font-size:10px}
.show-fleg{position:absolute;inset-inline-start:10px;top:10px;z-index:5;background:color-mix(in srgb,var(--surface) 92%,transparent);
 border-radius:10px;padding:8px 10px;font-size:12px;box-shadow:0 2px 10px rgba(0,0,0,.15)}
.show-fleg b{display:block;margin-bottom:4px}.show-fleg div{display:flex;align-items:center;gap:6px}.show-fleg i{width:12px;height:12px;border-radius:3px}
.show-filmb{margin-inline-start:auto;font-size:12px;padding:3px 10px}
.show-player{position:absolute;inset-inline:10px;bottom:10px;z-index:6;display:flex;align-items:center;gap:8px;padding:8px 10px;
 border-radius:12px;background:color-mix(in srgb,var(--surface) 94%,transparent);box-shadow:0 4px 18px rgba(0,0,0,.22);font-size:13px}
.show-player input[type=range]{flex:1;accent-color:var(--accent)}
.show-player select{font:inherit;font-size:12px;background:var(--surface2);color:var(--text);border:1px solid var(--line);border-radius:8px}
.show-date{position:absolute;top:12px;left:50%;transform:translateX(-50%);z-index:6;padding:6px 16px;border-radius:12px;
 background:color-mix(in srgb,var(--surface) 92%,transparent);font-size:clamp(16px,2.4vw,26px);font-weight:750;
 box-shadow:0 4px 18px rgba(0,0,0,.2);text-align:center;line-height:1.25}
.show-date small{display:block;font-size:.55em;font-weight:500;color:var(--muted)}
.map .f.show-pending{fill:var(--surface2)!important;opacity:.35}
.map .f.show-pulse{animation:showPulse .9s ease}
@keyframes showPulse{0%{opacity:.2}40%{opacity:1;filter:drop-shadow(0 0 6px var(--accent))}100%{opacity:1}}
.show-laser{position:absolute;z-index:9;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;pointer-events:none;
 background:radial-gradient(circle,#ff3b3b 0 35%,rgba(255,59,59,.45) 36% 60%,rgba(255,59,59,0) 61%);
 box-shadow:0 0 18px 6px rgba(255,59,59,.55);transition:left .06s linear,top .06s linear}
.show-laserpad{position:absolute;inset:0;z-index:8;cursor:crosshair;touch-action:none;outline:3px solid #ff3b3b;outline-offset:-3px}
.show-pill{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:40;display:flex;gap:8px;align-items:center;
 padding:8px 12px;border-radius:999px;background:var(--surface);box-shadow:0 6px 24px rgba(0,0,0,.25);font-size:13px}
.show-pill i{width:9px;height:9px;border-radius:50%;background:#ff3b3b;animation:showBlink 1.2s infinite}
@keyframes showBlink{50%{opacity:.25}}
.show-sheet{display:flex;flex-direction:column;gap:10px}
.show-sheet input{font:inherit;font-size:28px;letter-spacing:.3em;text-align:center;padding:8px;border-radius:10px;border:1px solid var(--line);
 background:var(--surface2);color:var(--text)}
body.show-wallmode .top,body.show-wallmode .foot,body.show-wallmode .show-story{display:none!important}
body.show-wallmode .w{display:none!important}
body.show-wallmode .w.show-hero{display:block!important;position:fixed!important;inset:0!important;z-index:30;border-radius:0!important;margin:0!important}
body.show-wallmode .w.show-hero>header{display:none}
body.show-wallmode .w.show-hero .body{height:100vh!important}
body.show-wallmode .w.show-hero .map{height:100vh!important;border-radius:0!important}
body.show-wallmode .gleg,body.show-wallmode .mapbtn,body.show-wallmode .mscale,body.show-wallmode .mnorth{display:none!important}
.show-cal{position:fixed;inset:0;z-index:50;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;
 background:color-mix(in srgb,var(--bg) 88%,transparent);text-align:center;padding:20px}
.show-cal .n{font-size:min(38vw,240px);font-weight:800;line-height:1;color:var(--accent)}
.show-cal .grid{display:grid;gap:6px}
.show-cal .grid i{width:34px;height:52px;border-radius:6px;border:2px solid var(--line);display:block}
.show-cal .grid i.me{background:var(--accent);border-color:var(--accent)}
.show-cal .grid i.on{background:color-mix(in srgb,var(--accent) 30%,transparent)}
.show-cal .row{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.show-wallx{position:fixed;top:8px;inset-inline-end:8px;z-index:45;opacity:.35}
.show-wallx:hover{opacity:1}
"""

HTML = ""

JS = r"""
const BD=window.BiruniDash;if(!BD)return;
const t=(k,v)=>{let s=BD.t('show_'+k);if(v)for(const x in v)s=s.split('{'+x+'}').join(v[x]);return s};
const $=id=>document.getElementById(id);
const mk=(tag,cls,text,parent)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;if(parent)parent.appendChild(e);return e};
const NS='http://www.w3.org/2000/svg';
const sv=(tag,attrs,parent)=>{const e=document.createElementNS(NS,tag);for(const k in attrs||{})e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e};
const MK=20037508.34;
function merc(lon,lat){lat=Math.max(-85,Math.min(85,lat));return [lon*MK/180,Math.log(Math.tan((90+lat)*Math.PI/360))*MK/Math.PI]}
function unmerc(x,y){return [x*180/MK,360/Math.PI*Math.atan(Math.exp(y*Math.PI/MK))-90]}
const S=BD.state;
const X={story:false,fc:{},fcAt:0,film:null,meet:{active:false,you:false,follow:true},wall:null,muteUntil:0,lastView:'',pop:null};
window.BiruniShow=X;
/* ---- the map's view, through the map's own code (a tiny drag that ends where it starts) ---- */
function maps(){const ws=BD.widgets(),out=[];for(const id in ws)if(ws[id].w.type==='map')out.push(ws[id]);return out}
function hero(){const ms=maps();if(!ms.length)return null;return ms.slice().sort((a,b)=>((b.w.w||0)*(b.w.h||0))-((a.w.w||0)*(a.w.h||0)))[0]}
function viewOf(c){if(!c||!c.view||!c.org||!c.svg)return null;const r=c.svg.getBoundingClientRect();if(!r.width)return null;
 return {cx:c.org[0]+c.view.x+c.view.w/2,cy:c.org[1]-(c.view.y+c.view.h/2),mpp:c.view.w/r.width}}
function nudge(c){const r=c.svg.getBoundingClientRect(),x=r.left+4,y=r.top+4;const o={pointerId:4097,bubbles:true,cancelable:true,isPrimary:true,pointerType:'mouse',button:0};
 const ev=(type,dx)=>c.svg.dispatchEvent(new PointerEvent(type,Object.assign({},o,{clientX:x+dx,clientY:y})));
 ev('pointerdown',0);ev('pointermove',8);ev('pointermove',0);ev('pointerup',0)}
function applyView(c,v){if(!c||!c.org||!c.svg||!v||!isFinite(v.cx)||!isFinite(v.mpp))return;const r=c.svg.getBoundingClientRect();if(!r.width)return;
 const w=v.mpp*r.width,h=v.mpp*r.height;c.view={x:v.cx-c.org[0]-w/2,y:(c.org[1]-v.cy)-h/2,w:w,h:h};X.muteUntil=Date.now()+500;nudge(c);X.lastView=key(viewOf(c))}
const key=v=>v?[v.cx.toFixed(1),v.cy.toFixed(1),v.mpp.toFixed(4)].join(','):'';
/* ---- dates (Gregorian and/or Hijri, as the dashboard says) ---- */
function fmtDate(ts,opt){if(!ts)return '';const d=new Date(ts*1000),L=BD.lang().replace('_','-'),o=opt||{day:'numeric',month:'short'};
 const mode=(S.def&&S.def.dates)||'both';let g='',h='';
 try{g=new Intl.DateTimeFormat(L,o).format(d)}catch(e){g=d.toISOString().slice(0,10)}
 try{h=new Intl.DateTimeFormat(L+'-u-ca-islamic-umalqura',o).format(d)}catch(e){h=''}
 if(mode==='hijri'&&h)return h;if(mode==='greg'||!h)return g;return g+' · '+h}
const nf=(v,dec)=>{try{return new Intl.NumberFormat(BD.lang().replace('_','-'),{maximumFractionDigits:dec==null?1:dec}).format(v)}catch(e){return String(v)}};

/* ======================= 1. «Since your last visit» + voice ======================= */
BD.on('show_story',m=>{const old=$('show_story');if(old)old.remove();const lines=Array.isArray(m.lines)?m.lines:[];if(!lines.length)return;
 const box=mk('section','show-story');box.id='show_story';box.setAttribute('role','status');
 const h=mk('h3',null,null,box);mk('i',null,null,h);mk('span',null,String(lines[0]).replace(/[:：]\s*$/,''),h);
 const ul=mk('ul',null,null,box);for(const s of lines.slice(1))mk('li',null,String(s),ul);
 const acts=mk('div','acts',null,box);const lb=mk('button',null,t('listen'),acts);lb.type='button';
 lb.onclick=()=>speak(lines,m.lang||BD.lang(),lb);const cb=mk('button',null,t('close'),acts);cb.type='button';cb.onclick=()=>{try{speechSynthesis.cancel()}catch(e){}box.remove()};
 const g=$('grid');g.parentNode.insertBefore(box,g)});
function speak(lines,lang,btn){const ss=window.speechSynthesis;if(!ss||!window.SpeechSynthesisUtterance){BD.toast(t('noVoice'));return}
 if(ss.speaking){ss.cancel();btn.textContent=t('listen');return}
 const vs=ss.getVoices()||[];const want=lang==='ar'?'ar':String(lang||'en').slice(0,2);const v=vs.find(x=>(x.lang||'').toLowerCase().startsWith(want));
 if(!v&&vs.length){BD.toast(t('noVoice'));return}
 const u=new SpeechSynthesisUtterance(lines.join(' '));u.lang=want==='ar'?'ar-SA':want;if(v)u.voice=v;u.rate=1;
 u.onend=()=>{btn.textContent=t('listen')};btn.textContent=t('stop');ss.speak(u)}

/* ======================= 2. forecast widgets ======================= */
function fcWidgets(){const ws=BD.widgets(),out=[];for(const id in ws){const st=ws[id].w.style||{};if(ws[id].w.type==='text'&&st.show==='forecast')out.push(ws[id])}return out}
function askForecasts(){X.fcAt=Date.now();for(const c of fcWidgets())BD.send({t:'show_forecast',widget:c.w.id})}
BD.on('show_forecast',m=>{const c=BD.widgets()[m.widget];if(!c)return;X.fc[m.widget]=m;c.el.classList.add('show-fc');
 let box=c.el.querySelector('.show-fcbox');if(!box){box=mk('div','show-fcbox',null,c.el)}box.textContent='';
 const o=m.overall||{};const top=mk('div','show-fcall',null,box);
 mk('b',null,o.finished?t('fcDone'):o.eta?fmtDate(o.eta):t('fcSlow'),top);
 if(o.eta&&!o.finished)mk('span',null,t('fcBetween',{a:fmtDate(o.lo),b:fmtDate(o.hi)})+' · '+t('fcRate',{r:nf(o.rate)}),top);
 chart(box,o);
 const z=mk('div','show-fcz',null,box);
 for(const it of (m.zones||[]).slice().sort((a,b)=>(a.eta||9e12)-(b.eta||9e12))){mk('span','lb',String(it.label),z);
  mk('span','d',it.finished?t('fcDone'):it.eta?fmtDate(it.eta):t('fcSlow'),z);
  mk('span','r',(it.eta&&!it.finished?t('fcBetween',{a:fmtDate(it.lo),b:fmtDate(it.hi)})+' · ':'')+nf(it.done,0)+' / '+nf(it.total,0),z)}
 if(m.xf&&maps().length){const b=mk('button','show-btn',X.fcOn===m.widget?t('fcOff'):t('fcColour'),box);b.type='button';
  b.onclick=()=>{if(X.fcOn===m.widget){colourOff();b.textContent=t('fcColour')}else{colourByWeek(m);b.textContent=t('fcOff')}}}
 if(X.fcOn===m.widget)colourByWeek(m)});
function chart(box,o){const s=o.series||[],cn=o.cone||[];if(s.length<2)return;const W=600,H=150,P=24;
 const svg=sv('svg',{class:'show-fcsvg',viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',role:'img','aria-label':t('fcTitle'),style:'direction:ltr'},box);
 const ts=s.map(p=>p[0]).concat(cn.map(p=>p[0])),t0=Math.min(...ts),t1=Math.max(...ts),ymax=Math.max(o.total||0,...s.map(p=>p[1]))||1;
 const rtl=false;const X_=x=>{const f=(x-t0)/Math.max(1,t1-t0);return P+(rtl?1-f:f)*(W-2*P)},Y_=y=>H-P-(y/ymax)*(H-2*P);
 sv('line',{class:'tot',x1:P,x2:W-P,y1:Y_(o.total||0),y2:Y_(o.total||0)},svg);
 if(cn.length){const up=cn.map(p=>X_(p[0])+','+Y_(p[3])).join(' '),dn=cn.slice().reverse().map(p=>X_(p[0])+','+Y_(p[1])).join(' ');
  sv('polygon',{class:'cone',points:up+' '+dn},svg);sv('polyline',{class:'mid',points:cn.map(p=>X_(p[0])+','+Y_(p[2])).join(' ')},svg)}
 sv('polyline',{class:'hist',points:s.map(p=>X_(p[0])+','+Y_(p[1])).join(' ')},svg);
 const a=sv('text',{x:rtl?W-P:P,y:H-6,'text-anchor':rtl?'end':'start'},svg);a.textContent=fmtDate(t0,{day:'numeric',month:'short'}).split(' · ')[0];
 const b=sv('text',{x:rtl?P:W-P,y:H-6,'text-anchor':rtl?'start':'end'},svg);b.textContent=fmtDate(t1,{day:'numeric',month:'short'}).split(' · ')[0]}
const WEEK=['#1a9850','#66bd63','#a6d96a','#fee08b','#fdae61','#f46d43','#d73027'];
function colourByWeek(m){colourOff(true);X.fcOn=m.widget;const now=Date.now()/1000,zs={};
 for(const z of m.zones||[]){const wk=z.finished?0:z.eta?Math.max(0,Math.floor((z.eta-now)/(7*86400))):99;zs[String(z.key)]={wk:wk,z:z}}
 const used=new Set();
 for(const c of maps()){if(!c.L)continue;for(const lid in c.L){const G=c.L[lid];for(const fid in G.f){const F=G.f[fid],p=(F.feat&&F.feat.properties)||{};
   const k=p._k&&p._k[m.xf];if(k==null||!zs[String(k)])continue;const wk=zs[String(k)].wk;used.add(wk);F.node.style.fill=WEEK[Math.min(WEEK.length-1,wk)];
   if(/^(path)$/i.test(F.node.tagName)&&F.node.getAttribute('fill')==='none')F.node.style.stroke=WEEK[Math.min(WEEK.length-1,wk)];F.node.dataset.showFc='1'}}
  let lg=c.mp.querySelector('.show-fleg');if(!lg)lg=mk('div','show-fleg',null,c.mp);lg.textContent='';mk('b',null,t('fcLegend'),lg);
  for(const wk of [...used].sort((a,b)=>a-b).slice(0,7)){const row=mk('div',null,null,lg);const i=mk('i',null,null,row);i.style.background=WEEK[Math.min(WEEK.length-1,wk)];
   mk('span',null,wk===0?t('fcDone'):wk>=WEEK.length-1?t('fcLate'):t('fcWeek',{d:fmtDate(now+wk*7*86400).split(' · ')[0]}),row)}}}
function colourOff(keep){for(const c of maps()){if(!c.L)continue;for(const lid in c.L){const G=c.L[lid];for(const fid in G.f){const n=G.f[fid].node;if(n.dataset.showFc){n.style.fill='';n.style.stroke='';delete n.dataset.showFc}}}
 const lg=c.mp&&c.mp.querySelector('.show-fleg');if(lg)lg.remove()}if(!keep)X.fcOn=null}

/* ======================= 3. the project film ======================= */
function filmButtons(){for(const c of maps()){const hd=c.el.querySelector('header');if(!hd||hd.querySelector('.show-filmb'))continue;
 const b=mk('button','show-btn show-filmb','▶ '+t('film'),hd);b.type='button';b.onclick=e=>{e.stopPropagation();BD.send({t:'show_film',widget:c.w.id})}}}
BD.on('show_film',m=>{const c=BD.widgets()[m.widget];if(!c||!Array.isArray(m.frames)||!m.frames.length){BD.toast(t('filmNo'));return}filmOpen(c,m)});
function filmNodes(c,m){const out={};for(const f of m.frames)for(const ch of f.ch){const G=c.L&&c.L[ch[0]],F=G&&G.f[ch[1]];if(F)out[ch[0]+':'+ch[1]]=F}return out}
function filmOpen(c,m){filmClose();const F=filmNodes(c,m);const P={c:c,m:m,F:F,i:-1,speed:1,timer:null,count:0};X.film=P;
 for(const k in F){F[k].node.classList.add('show-pending');F[k].node.style.fill=''}
 const date=mk('div','show-date',null,c.mp);P.date=date;c.mp.classList.add('show-filming');
 const bar=mk('div','show-player',null,c.mp);P.bar=bar;
 const pb=mk('button','show-btn','▶',bar);pb.type='button';pb.title=t('play');pb.onclick=()=>P.timer?pause():play();P.pb=pb;
 const rg=mk('input',null,null,bar);rg.type='range';rg.min=0;rg.max=m.frames.length-1;rg.value=0;rg.oninput=()=>seek(+rg.value);P.rg=rg;
 const sp=mk('select',null,null,bar);sp.title=t('speed');for(const v of [1,2,4,8]){const o=mk('option',null,v+'×',sp);o.value=v}sp.onchange=()=>{P.speed=+sp.value;if(P.timer){pause();play()}};
 const cnt=mk('span',null,'',bar);P.cnt=cnt;
 const rec=mk('button','show-btn','● '+t('rec'),bar);rec.type='button';rec.onclick=()=>P.rec?recStop():recStart();P.recB=rec;
 const x=mk('button','show-btn','×',bar);x.type='button';x.title=t('close');x.onclick=filmClose;
 seek(0)}
function stepTo(i){const P=X.film;if(!P)return;const fr=P.m.frames;i=Math.max(0,Math.min(fr.length-1,i));
 if(i<P.i){for(const k in P.F){const n=P.F[k].node;n.classList.add('show-pending');n.style.fill='';n.style.opacity=''}P.i=-1;P.count=0}
 for(let j=P.i+1;j<=i;j++){for(const ch of fr[j].ch){const n=P.F[ch[0]+':'+ch[1]];if(!n)continue;const nd=n.node;nd.classList.remove('show-pending');
   if(ch[2]==null){nd.style.opacity='.1'}else{nd.style.opacity='';nd.style.fill=ch[2]}
   if(j===i){nd.classList.remove('show-pulse');void nd.getBoundingClientRect();nd.classList.add('show-pulse')}P.count++}}
 P.i=i;P.rg.value=i;P.date.innerHTML='';const big=mk('span',null,fmtDate(fr[i].t,{day:'numeric',month:'long',year:'numeric'}).split(' · ')[0],P.date);
 const h=fmtDate(fr[i].t,{day:'numeric',month:'long',year:'numeric'}).split(' · ')[1];if(h)mk('small',null,h,P.date);P.cnt.textContent=changes(P.count);
 if(P.rec)recDraw()}
function seek(i){stepTo(i)}
/* «n changes» with Arabic number agreement: تغيير واحد، تغييران، 3-10 تغييرات، 11-99 تغييرًا، 100 تغيير… */
function changes(n){if(BD.lang()!=='ar')return t('changed',{n:nf(n,0)});const r=n%100,s=nf(n,0);
 if(n===1)return 'تغيير واحد';if(n===2)return 'تغييران';if(r>=3&&r<=10)return s+' تغييرات';if(r>=11&&r<=99)return s+' تغييرًا';return s+' تغيير'}
function play(){const P=X.film;if(!P)return;if(P.i>=P.m.frames.length-1)stepTo(0);P.pb.textContent='❚❚';P.pb.title=t('pause');
 P.timer=setInterval(()=>{if(P.i>=P.m.frames.length-1){pause();if(P.rec)setTimeout(recStop,800);return}stepTo(P.i+1)},Math.max(60,700/P.speed))}
function pause(){const P=X.film;if(!P)return;clearInterval(P.timer);P.timer=null;P.pb.textContent='▶';P.pb.title=t('play')}
function filmClose(){const P=X.film;if(!P)return;pause();if(P.rec)recStop();for(const k in P.F){const n=P.F[k].node;n.classList.remove('show-pending','show-pulse');n.style.fill='';n.style.opacity=''}
 P.date.remove();P.bar.remove();P.c.mp.classList.remove('show-filming');X.film=null}
/* recording: each frame of the map (SVG with its computed colours) drawn on a canvas, captured by MediaRecorder */
function recStart(){const P=X.film;if(!P)return;const cv=document.createElement('canvas');const r=P.c.svg.getBoundingClientRect();
 if(!cv.captureStream||!window.MediaRecorder){BD.toast(t('recNo'));return}cv.width=Math.round(r.width*Math.min(2,devicePixelRatio||1));cv.height=Math.round(r.height*Math.min(2,devicePixelRatio||1));
 let mr;try{mr=new MediaRecorder(cv.captureStream(24),{mimeType:MediaRecorder.isTypeSupported('video/webm;codecs=vp9')?'video/webm;codecs=vp9':'video/webm'})}catch(e){BD.toast(t('recNo'));return}
 P.rec={cv:cv,mr:mr,parts:[]};mr.ondataavailable=e=>{if(e.data&&e.data.size)P.rec.parts.push(e.data)};
 mr.onstop=()=>{const blob=new Blob(P.rec?P.rec.parts:[],{type:'video/webm'});X.lastVideo=blob;const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=((S.def&&S.def.title)||'biruni')+'.webm';document.body.appendChild(a);try{a.click()}catch(e){}setTimeout(()=>a.remove(),2000);BD.toast(t('recSaved'))};
 mr.start(250);P.recB.textContent='■ '+t('recStop');stepTo(0);recDraw();play()}
function recStop(){const P=X.film;if(!P||!P.rec)return;try{P.rec.mr.stop()}catch(e){}P.recB.textContent='● '+t('rec');setTimeout(()=>{if(X.film)X.film.rec=null},50)}
const PROPS=['fill','stroke','stroke-width','opacity','fill-opacity','stroke-opacity','stroke-dasharray','font-size','font-weight','font-family','paint-order'];
function recDraw(){const P=X.film;if(!P||!P.rec)return;const src=P.c.svg,cl=src.cloneNode(true),a=src.querySelectorAll('*'),b=cl.querySelectorAll('*');
 for(let i=0;i<a.length;i++){const cs=getComputedStyle(a[i]);let st='';for(const p of PROPS){const v=cs.getPropertyValue(p);if(v)st+=p+':'+v+';'}b[i].style.cssText=st}
 const r=src.getBoundingClientRect();cl.setAttribute('width',r.width);cl.setAttribute('height',r.height);
 const xml=new XMLSerializer().serializeToString(cl);const img=new Image();const cv=P.rec.cv,g=cv.getContext('2d');
 img.onload=()=>{const bg=getComputedStyle(P.c.mp).backgroundColor||'#fff';g.fillStyle=bg;g.fillRect(0,0,cv.width,cv.height);g.drawImage(img,0,0,cv.width,cv.height);
  const k=cv.width/r.width;g.font='700 '+Math.round(22*k)+'px system-ui,Segoe UI,Tahoma,sans-serif';g.textAlign='center';g.fillStyle='rgba(0,0,0,.55)';
  const txt=P.date.textContent;const w=g.measureText(txt).width+24*k;g.fillRect(cv.width/2-w/2,10*k,w,38*k);g.fillStyle='#fff';g.fillText(txt,cv.width/2,36*k)};
 img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(xml)}

/* ======================= 4. meeting mode ======================= */
function meetButton(){if($('show_meetBtn'))return;BD.button('show_meetBtn',t('meet'),'M3 4h18M5 4v9h14V4M12 13v3M8 21l4-5 4 5M15 8l-3 2-2-1',meetSheet)}
function meetSheet(){const M=X.meet,box=mk('div','show-sheet');
 if(!M.active){mk('p',null,t('meetNone'),box);BD.sheet(t('meet'),box);return}
 if(M.you){mk('p',null,t('meetYou'),box);const l=mk('button','show-btn',t('meetLaser'),box);l.type='button';l.onclick=()=>{BD.close();laserPad(true)};
  const e=mk('button','show-btn',t('meetEnd'),box);e.type='button';e.onclick=()=>{BD.send({t:'show_meet_end'});laserPad(false);BD.close()}}
 else{mk('label',null,t('meetPin'),box);const i=mk('input',null,null,box);i.inputMode='numeric';i.maxLength=6;i.autocomplete='off';
  const b=mk('button','show-btn',t('meetPresent'),box);b.type='button';b.onclick=()=>BD.send({t:'show_meet_claim',pin:i.value.replace(/\D/g,'')});
  if(M.presenter){const f=mk('button','show-btn',M.follow?t('meetStop'):t('meetFollow'),box);f.type='button';f.onclick=()=>{M.follow=!M.follow;pill();BD.close()}}}
 BD.sheet(t('meet'),box)}
BD.on('show_meet_state',m=>{const M=X.meet;const was=M.you;M.active=!!m.active;M.you=!!m.you;M.presenter=m.presenter||'';
 if(M.you&&!was){BD.close();BD.toast(t('meetYou'))}if(!M.you)laserPad(false);if(!M.active)M.follow=true;pill();
 if(m.view&&!M.you&&M.follow)follow(m.view)});
BD.on('show_meet',m=>{const M=X.meet;M.presenter=m.presenter||M.presenter;M.active=true;pill();if(!M.you&&M.follow)follow(m.view)});
BD.on('show_meet_laser',m=>{if(X.meet.you||!X.meet.follow)return;laserShow(m)});
BD.on('show_err',m=>{const w=String(m.why||'');if(w==='wrong pin')BD.toast(t('meetWrong'));else if(w==='too many tries')BD.toast(t('meetMany'));else if(w==='wall full')BD.toast(t('wallFull'));else if(w==='no history'||w==='no film')BD.toast(t('filmNo'))});
function pill(){let p=$('show_pill');const M=X.meet;const on=M.active&&M.presenter&&(M.you||M.follow);
 if(!on){if(p)p.remove();return}if(!p){p=mk('div','show-pill',null,document.body);p.id='show_pill'}p.textContent='';mk('i',null,null,p);
 mk('span',null,M.you?t('meetYou'):t('meetBy',{name:M.presenter}),p);
 const b=mk('button','show-btn',M.you?t('meetEnd'):t('meetStop'),p);b.type='button';b.onclick=()=>{if(M.you){BD.send({t:'show_meet_end'});laserPad(false)}else{M.follow=false;pill();laserHide()}}}
function follow(v){if(!v)return;const c=(v.wid&&BD.widgets()[v.wid])||hero();
 if(c&&v.mpp)applyView(c,v);
 const xf=S.xf;if(v.xf===null&&xf)BD.setXF(null);else if(Array.isArray(v.xf)&&(!xf||xf.field!==v.xf[0]||xf.key!==v.xf[1]))BD.setXF(v.xf[0],v.xf[1]);
 if(isFinite(v.scroll)){const max=document.documentElement.scrollHeight-innerHeight;window.scrollTo({top:Math.max(0,v.scroll*max),behavior:'smooth'})}
 if(c&&Array.isArray(v.pop)){const G=c.L&&c.L[v.pop[0]],F=G&&G.f[v.pop[1]];if(F&&(!X.pop||X.pop.join()!==v.pop.join())){tapNode(c,F.node)}X.pop=v.pop}
 else if(c&&X.pop&&!v.pop){const x=c.pop&&c.pop.querySelector('button.x');if(x)x.click();X.pop=null}}
function tapNode(c,node){const r=node.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;const o={pointerId:4098,bubbles:true,cancelable:true,isPrimary:true,pointerType:'mouse',clientX:x,clientY:y};
 X.muteUntil=Date.now()+400;node.dispatchEvent(new PointerEvent('pointerdown',o));node.dispatchEvent(new PointerEvent('pointerup',o))}
/* the presenter's page reports its view (polled: whatever moved it - finger, wheel, a button) */
function myPop(c){if(!c||!c.pop||c.pop.classList.contains('hide'))return null;return X.myPop||null}
document.addEventListener('pointerup',e=>{const n=e.target&&e.target.closest&&e.target.closest('.map [data-f]');if(n)X.myPop=[n.dataset.l,n.dataset.f]},true);
let lastSent='';
setInterval(()=>{const M=X.meet;
 if(M.you){const c=hero();const v=viewOf(c);if(!v)return;const max=document.documentElement.scrollHeight-innerHeight;
  const xf=S.xf?[S.xf.field,S.xf.key]:null;const out={cx:v.cx,cy:v.cy,mpp:v.mpp,wid:c.w.id,xf:xf,scroll:max>0?scrollY/max:0,pop:myPop(c)};
  const k=JSON.stringify(out);if(k!==lastSent){lastSent=k;BD.send({t:'show_meet_view',view:out})}}
 if(X.wall&&X.wall.started&&Date.now()>X.muteUntil){const c=hero(),v=viewOf(c);const k=key(v);if(v&&k!==X.lastView){X.lastView=k;const sh=sharedFrom(v);
  const xf=S.xf?[S.xf.field,S.xf.key]:null;BD.send({t:'show_wall_view',view:{cx:sh.cx,cy:sh.cy,mpp:sh.mpp,xf:xf}})}}},200);
function laserPad(on){const c=hero();let pad=c&&c.mp.querySelector('.show-laserpad');if(!on){if(pad)pad.remove();if(X.meet.you)BD.send({t:'show_meet_laser'});return}
 if(!c||pad)return;pad=mk('div','show-laserpad',null,c.mp);let last=0;
 const mv=e=>{const now=Date.now();if(now-last<50)return;last=now;const r=c.svg.getBoundingClientRect(),v=c.view;if(!v)return;
  const x=c.org[0]+v.x+(e.clientX-r.left)/r.width*v.w,y=c.org[1]-(v.y+(e.clientY-r.top)/r.height*v.h);const ll=unmerc(x,y);
  BD.send({t:'show_meet_laser',wid:c.w.id,lon:ll[0],lat:ll[1]});e.preventDefault()};
 pad.addEventListener('pointermove',mv);pad.addEventListener('pointerdown',mv);pad.addEventListener('dblclick',()=>laserPad(false))}
function laserShow(m){const c=(m.wid&&BD.widgets()[m.wid])||hero();if(!c||!c.view)return;let dot=c.mp.querySelector('.show-laser');
 if(m.off){if(dot)dot.remove();return}if(!dot)dot=mk('div','show-laser',null,c.mp);const p=merc(m.lon,m.lat),r=c.svg.getBoundingClientRect(),v=c.view;
 const mr=c.mp.getBoundingClientRect();dot.style.left=((p[0]-c.org[0]-v.x)/v.w*r.width+(r.left-mr.left))+'px';dot.style.top=((c.org[1]-p[1]-v.y)/v.h*r.height+(r.top-mr.top))+'px';
 clearTimeout(X.laserT);X.laserT=setTimeout(()=>{const d=c.mp.querySelector('.show-laser');if(d)d.remove()},4000)}
function laserHide(){for(const c of maps()){const d=c.mp.querySelector('.show-laser');if(d)d.remove()}}

/* ======================= 5. the phone wall ======================= */
function wallButton(){if($('show_wallBtn'))return;BD.button('show_wallBtn',t('wall'),'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',()=>{
 const box=mk('div','show-sheet');mk('p',null,t('wallHelp'),box);const b=mk('button','show-btn',t('wallJoin'),box);b.type='button';
 b.onclick=()=>{BD.close();BD.send({t:'show_wall_join',w:innerWidth,h:innerHeight,dpr:devicePixelRatio||1})};BD.sheet(t('wall'),box)})}
BD.on('show_wall',m=>{const first=!X.wall;X.wall=Object.assign(X.wall||{},{id:m.wall,slot:m.slot,rows:m.rows,cols:m.cols,tile:m.tile,virtual:m.virtual,n:m.n});
 const c=hero();if(!c)return;document.body.classList.add('show-wallmode');for(const x of maps())x.el.classList.toggle('show-hero',x===c);
 if(first){if(!$('show_wallx')){const b=mk('button','show-btn show-wallx','×',document.body);b.id='show_wallx';b.title=t('wallLeave');b.onclick=()=>BD.send({t:'show_wall_leave'})}}
 calibrate();setTimeout(()=>{window.dispatchEvent(new Event('resize'));if(m.view)wallApply(m.view)},120)});
BD.on('show_wall_view',m=>wallApply(m.view));
BD.on('show_wall_end',()=>{X.wall=null;document.body.classList.remove('show-wallmode');for(const x of maps())x.el.classList.remove('show-hero');
 const b=$('show_wallx');if(b)b.remove();const cal=$('show_cal');if(cal)cal.remove();setTimeout(()=>window.dispatchEvent(new Event('resize')),60)});
function sharedFrom(v){const W=X.wall,tl=W.tile,vr=W.virtual;return {cx:v.cx-(tl[0]+tl[2]/2-vr[0]/2)*v.mpp,cy:v.cy+(tl[1]+tl[3]/2-vr[1]/2)*v.mpp,mpp:v.mpp}}
function phoneFrom(v){const W=X.wall,tl=W.tile,vr=W.virtual;return {cx:v.cx+(tl[0]+tl[2]/2-vr[0]/2)*v.mpp,cy:v.cy-(tl[1]+tl[3]/2-vr[1]/2)*v.mpp,mpp:v.mpp}}
function wallApply(v){if(!X.wall||!v)return;const c=hero();if(!c)return;applyView(c,phoneFrom(v));
 const xf=S.xf;if(v.xf===null&&xf)BD.setXF(null);else if(Array.isArray(v.xf)&&(!xf||xf.field!==v.xf[0]||xf.key!==v.xf[1]))BD.setXF(v.xf[0],v.xf[1])}
function calibrate(){const W=X.wall;let cal=$('show_cal');if(W.started&&cal){cal.remove();return}if(W.started)return;
 if(!cal){cal=mk('div','show-cal',null,document.body);cal.id='show_cal'}cal.textContent='';
 mk('div','n',String(W.slot+1),cal);mk('div',null,t('wallSlot',{n:W.slot+1,m:W.rows*W.cols}),cal);
 const g=mk('div','grid',null,cal);g.style.gridTemplateColumns='repeat('+W.cols+',34px)';g.dir='ltr';
 for(let i=0;i<W.rows*W.cols;i++){const c=mk('i',i===W.slot?'me':i<W.n?'on':null,null,g)}
 const row=mk('div','row',null,cal);const mv=(d,lbl)=>{const b=mk('button','show-btn',lbl,row);b.type='button';b.onclick=()=>BD.send({t:'show_wall_slot',slot:W.slot+d})};
 if(W.slot%W.cols>0)mv(-1,'← '+t('wallLeft'));if(W.slot%W.cols<W.cols-1)mv(1,t('wallRight')+' →');
 if(W.slot>=W.cols)mv(-W.cols,'↑ '+t('wallUp'));if(W.slot+W.cols<W.rows*W.cols)mv(W.cols,'↓ '+t('wallDown'));
 const go=mk('button','show-btn',t('wallStart'),cal);go.type='button';go.onclick=()=>{W.started=true;cal.remove();
  setTimeout(()=>{const c=hero();const v=viewOf(c);if(W.view)wallApply(W.view);else if(v){X.lastView='';}},150)}}

/* ======================= wiring ======================= */
BD.onBuild(()=>{filmButtons();meetButton();wallButton();
 if(!X.story){X.story=true;BD.send({t:'show_story',lang:BD.lang()})}
 askForecasts();BD.send({t:'show_meet_hello'})});
BD.onData(()=>{filmButtons();if(Date.now()-X.fcAt>60000)askForecasts();if(X.fcOn&&X.fc[X.fcOn])setTimeout(()=>colourByWeek(X.fc[X.fcOn]),50)});
"""
