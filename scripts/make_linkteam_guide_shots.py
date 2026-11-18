"""Capture desktop and phone screenshots for the Biruni link & team Arabic guide.

Run with the engine's Python:
    engine\\bin\\python-qgis.bat scripts\\make_linkteam_guide_shots.py [--only <part>] [--group desktop|phone]
"""
import argparse
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "app", "plugins", "biruni")
OUT_DIR = os.path.join(ROOT, "docs", "guide", "images", "linkteam")
SCRATCH_DIR = r"D:\Claude Code - Online\biruni\_scratch\linkteam_book"
SHOTS_JSON = os.path.join(SCRATCH_DIR, "SHOTS.json")
MISSING_AR_TXT = os.path.join(SCRATCH_DIR, "missing_ar.txt")
AR_JSON = os.path.join(PLUGIN_DIR, "i18n", "ar.json")

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(SCRATCH_DIR, exist_ok=True)


def free_port():
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        try:
            with socket.socket() as a:
                a.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port")


def _update_shots_json(new_entries, only_part=None):
    data = {}
    if os.path.exists(SHOTS_JSON):
        try:
            with open(SHOTS_JSON, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    if only_part:
        for k, v in list(data.items()):
            if v.get("part") == only_part:
                del data[k]
    data.update(new_entries)
    with open(SHOTS_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def _append_missing_ar(lines):
    if not lines:
        return
    existing = set()
    if os.path.exists(MISSING_AR_TXT):
        with open(MISSING_AR_TXT, encoding="utf-8") as f:
            existing = {ln.strip() for ln in f if ln.strip()}
    added = [ln for ln in lines if ln not in existing]
    if added:
        with open(MISSING_AR_TXT, "a", encoding="utf-8", newline="\n") as f:
            for ln in added:
                f.write(ln + "\n")


# ---------------------------------------------------------------------------
# Desktop worker (QgsApplication)
# ---------------------------------------------------------------------------
def _run_desktop(only_part, platform):
    os.environ["QT_QPA_PLATFORM"] = platform
    os.environ["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    settings_dir = tempfile.mkdtemp(prefix="biruni_qgs_")
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = settings_dir
    os.environ["QT_SCALE_FACTOR"] = "2"

    # Block network via env vars before importing plugin modules
    dead_port = free_port()
    os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{dead_port}"
    os.environ["BIRUNI_BRIDGE_MQTT"] = f"ws://127.0.0.1:{dead_port}"
    os.environ["BIRUNI_BRIDGE_NTFY"] = f"https://127.0.0.1:{dead_port}/"
    os.environ["BIRUNI_BRIDGE_OWN"] = f"wss://127.0.0.1:{dead_port}/"
    os.environ["BIRUNI_CF_API"] = f"http://127.0.0.1:{dead_port}/"

    sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
    sys.path.insert(0, os.path.join(ROOT, "app", "python"))

    from qgis.core import QgsApplication, QgsProject, QgsSettings
    app = QgsApplication([], True)
    app.initQgis()

    from biruni import i18n, theme
    i18n.init("ar")

    from qgis.PyQt.QtCore import QTranslator, Qt
    from qgis.PyQt.QtGui import QFont, QPixmap
    from qgis.PyQt.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGroupBox, QInputDialog, QLabel, QLineEdit,
                                     QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QTreeWidget)
    from qgis.gui import QgsMapCanvas

    _qt_tr = QTranslator(app)
    if _qt_tr.load("qtbase_ar", os.path.join(ROOT, "engine", "apps", "Qt6", "translations")):
        app.installTranslator(_qt_tr)

    tokens = theme.TOKENS["light"]
    app.setPalette(theme.build_palette(tokens))
    app.setStyleSheet(theme.app_qss(tokens))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setFont(QFont("Segoe UI", 9))
    QgsSettings().setValue("locale/overrideFlag", False)

    grab_queue = []

    def _next_modal_grab(widget, default_width=None):
        if not grab_queue:
            return
        name, part, w, temp_paths = grab_queue.pop(0)
        save_grab(widget, name, part, width=w or default_width, temp_paths=temp_paths)

    def _patch_modals():
        def _msg_exec(self):
            _next_modal_grab(self, 440)
            return QDialog.DialogCode.Rejected

        def _dlg_exec(self):
            _next_modal_grab(self, 460)
            return QDialog.DialogCode.Rejected

        def _menu_exec(self, *_a, **_kw):
            _next_modal_grab(self, None)
            return None

        QMessageBox.exec = _msg_exec
        QDialog.exec = _dlg_exec
        QMenu.exec = _menu_exec
        QInputDialog.getText = classmethod(lambda cls, *_a, **_kw: ("", False))
        QInputDialog.getItem = classmethod(lambda cls, *_a, **_kw: ("", False))
        QFileDialog.getOpenFileName = classmethod(lambda cls, *_a, **_kw: ("", ""))
        QFileDialog.getSaveFileName = classmethod(lambda cls, *_a, **_kw: ("", ""))
        QFileDialog.getExistingDirectory = classmethod(lambda cls, *_a, **_kw: "")

    _patch_modals()

    with open(AR_JSON, encoding="utf-8") as f:
        ar_keys = set(json.load(f).keys())
    user_name = getpass.getuser()

    from biruni.field_photos import drive
    drive.suggest_roots = lambda: [r"G:\My Drive"]

    class Bar:
        def pushWarning(self, *_a):
            pass

        def pushMessage(self, *_a, **_kw):
            pass

        def pushSuccess(self, *_a, **_kw):
            pass

        def pushInfo(self, *_a, **_kw):
            pass

    class Iface:
        def __init__(self):
            self.mw = QMainWindow()
            self.canvas = QgsMapCanvas(self.mw)
            self.mw.setCentralWidget(self.canvas)

        def mainWindow(self):
            return self.mw

        def mapCanvas(self):
            return self.canvas

        def messageBar(self):
            return Bar()

    class ThemeStub:
        def tokens(self):
            return dict(tokens)

    class ActivityStub:
        def __init__(self):
            self.logged = []

        def log(self, *a):
            self.logged.append(a)

    class PluginStub:
        def __init__(self):
            self.iface = Iface()
            self.theme = ThemeStub()
            self.activity = ActivityStub()
            self.toolbox = None
            self.runner = None

    LATIN_WORDS = re.compile(r"[A-Za-z]{2,}(?:\s+[A-Za-z]{2,})+")
    PRODUCT_ALLOW = {"Biruni GIS", "Google Drive", "Cloudflare", "Cloudflare Workers", "Zero Trust",
                     "Google Chrome", "Safari", "Android", "iOS", "Windows", "Excel", "GeoPackage",
                     "QR", "GPS", "GPX", "COM", "USB", "Wi-Fi", "HTTPS", "WebRTC", "JSON", "PDF",
                     "CSV", "HTML", "Editor", "Available offline", "My Drive", "Shared with me",
                     "Add shortcut", "No TLS Verify", "Nostr", "ntfy", "MQTT"}

    missing_lines = []
    captured = {}

    def _scrub(txt, temp_paths):
        for tp in temp_paths:
            if tp:
                rep = r"G:\My Drive" if "drive" in tp.lower() else r"D:\Projects\مشروع حي النخيل"
                txt = re.sub(re.escape(tp), lambda _m, r=rep: r, txt, flags=re.I)
        if settings_dir:
            txt = re.sub(re.escape(settings_dir), lambda _m: r"D:\Projects\مشروع حي النخيل", txt, flags=re.I)
        return txt

    def sanitize_widget(w, temp_paths=()):
        for lbl in w.findChildren(QLabel):
            lbl.setText(_scrub(lbl.text(), temp_paths))
        for le in w.findChildren(QLineEdit):
            le.setText(_scrub(le.text(), temp_paths))
        for lw in w.findChildren(QListWidget):
            for i in range(lw.count()):
                it = lw.item(i)
                it.setText(_scrub(it.text(), temp_paths))
        for tw in w.findChildren(QTreeWidget):
            def _walk(it):
                for c in range(tw.columnCount()):
                    it.setText(c, _scrub(it.text(c), temp_paths))
                for j in range(it.childCount()):
                    _walk(it.child(j))
            for i in range(tw.topLevelItemCount()):
                _walk(tw.topLevelItem(i))

    def collect_texts(w):
        texts = []
        for cls in (QLabel, QLineEdit):
            for ch in w.findChildren(cls):
                t = ch.text().strip()
                if t:
                    texts.append(t)
        from qgis.PyQt.QtWidgets import QPushButton, QCheckBox, QRadioButton, QGroupBox
        for cls in (QPushButton, QCheckBox, QRadioButton):
            for ch in w.findChildren(cls):
                t = ch.text().strip()
                if t:
                    texts.append(t)
        for ch in w.findChildren(QGroupBox):
            t = ch.title().strip()
            if t:
                texts.append(t)
        for lw in w.findChildren(QListWidget):
            for i in range(lw.count()):
                t = lw.item(i).text().strip()
                if t:
                    texts.append(t)
        for tw in w.findChildren(QTreeWidget):
            def walk(it):
                for c in range(tw.columnCount()):
                    t = it.text(c).strip()
                    if t:
                        texts.append(t)
                for j in range(it.childCount()):
                    walk(it.child(j))
            for i in range(tw.topLevelItemCount()):
                walk(tw.topLevelItem(i))
        return texts

    def save_grab(widget, name, part, width=420, rect=None, temp_paths=()):
        sanitize_widget(widget, temp_paths)
        widget.ensurePolished()
        if isinstance(widget, (QDialog, QMessageBox)):
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            widget.show()
            if width and not isinstance(widget, QMessageBox):
                widget.setFixedWidth(width)
            widget.adjustSize()
        elif width and not isinstance(widget, QMainWindow):
            widget.setFixedWidth(width)
            widget.adjustSize()
        app.processEvents()
        texts = collect_texts(widget)
        for t in texts:
            low = t.lower()
            if "qgis" in low or r"c:\users" in low or (user_name and len(user_name) > 2 and user_name.lower() in low):
                if "biruni" not in low or "qgis" in low or r"c:\users" in low:
                    raise RuntimeError(f"Forbidden text in {name}: {t!r}")
        for t in texts:
            if "[WinError" in t or t in PRODUCT_ALLOW:
                continue
            if t in ar_keys and len(t) >= 4:
                line = f"UNTRANSLATED: {t}"
                print(line, flush=True)
                missing_lines.append(line)
            elif LATIN_WORDS.search(t) and not any(p in t for p in PRODUCT_ALLOW) and "://" not in t and "\\" not in t:
                if not re.match(r"^[A-Z0-9]{4}(-[A-Z0-9]{4})+$", t):
                    line = f"UNTRANSLATED: {t}"
                    print(line, flush=True)
                    missing_lines.append(line)
        pm = widget.grab(rect) if rect is not None else widget.grab()
        if isinstance(widget, (QDialog, QMessageBox)):
            from qgis.PyQt.QtWidgets import QWidget as _QW
            _QW.hide(widget)
        out_path = os.path.join(OUT_DIR, f"{name}.png")
        pm.save(out_path)
        sz = os.path.getsize(out_path)
        print(f"SHOT desktop {part} {name}.png {sz}B", flush=True)
        captured[name] = {"part": part, "size": sz}

    # --- HUB & PROBE ---
    if only_part in ("probe", "hub", "all"):
        import socket as _sock
        from biruni.phone_hub import dock as hd, features as hf
        from biruni.secure import door as _door
        _door.check_door = lambda u, **k: {"state": "ok", "page": _door.rel_of(u), "why": "", "version": "test"}

        class FakeFeature(hf.Feature):
            def __init__(self, fid, title_txt, avail, on, link):
                self.id = fid
                self._title = title_txt
                self.icon = "camera" if fid == "photos" else ("device-mobile-pin" if fid == "gps" else "antenna")
                self._avail = avail
                self.on = on
                self.link = link

            def title(self):
                return self._title

            def available(self, _plugin):
                return self._avail

            def state(self, _plugin):
                return self.on, self.link

            def start(self, _plugin):
                self.on = True

            def stop(self, _plugin):
                self.on = False

            def show(self, _plugin):
                pass

        plugin = PluginStub()
        fakes = [
            FakeFeature("gps", i18n.tr("Phone GPS"), True, True, {"port": 10114, "path": "/g/"}),
            FakeFeature("photos", i18n.tr("Field Photos"), True, True, {"port": 10112, "path": "/p/"}),
            FakeFeature("map", i18n.tr("See My Map"), True, False, None),
        ]
        d = hd.open_dock(plugin, features=fakes, port=free_port(), bind="127.0.0.1", addresses=["192.168.1.25"])
        d.url.setText(f"https://192.168.1.25:10115/{d.server.token}/")
        d.refresh()
        save_grab(d.widget(), "hub-on", "hub" if only_part != "probe" else "probe", width=420)
        if only_part in ("hub", "all"):
            d.toggle()
            save_grab(d.widget(), "hub-off", "hub", width=420)

            # Busy port state
            blocker = _sock.socket()
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_p = blocker.getsockname()[1]
            d.port = busy_p
            d.start()
            save_grab(d.widget(), "hub-busy-port", "hub", width=420)
            blocker.close()
            hd.close_dock(plugin)

            # Mixed 4 tool states
            mixed = [
                FakeFeature("gps", i18n.tr("Phone GPS"), True, True, {"port": 10114, "path": "/g/"}),
                FakeFeature("photos", i18n.tr("Field Photos"), True, True, None),
                FakeFeature("map", i18n.tr("See My Map"), True, False, None),
                FakeFeature("gps_off", i18n.tr("Phone GPS"), False, False, None),
            ]
            d2 = hd.open_dock(plugin, features=mixed, port=free_port(), bind="127.0.0.1", addresses=["192.168.1.25"])
            d2.url.setText(f"https://192.168.1.25:10115/{d2.server.token}/")
            d2.refresh()
            save_grab(d2.widget(), "hub-tools-mixed", "hub", width=420)
            hd.close_dock(plugin)

            # Door checks
            grab_queue.append(("door-changed", "hub", None, ()))
            _door.ask_owner(None, {"state": "changed", "page": "p/index.html", "why": ""})
            grab_queue.append(("door-unsigned", "hub", None, ()))
            _door.ask_owner(None, {"state": "unsigned", "page": "p/index.html",
                                   "why": "the release list is not signed by Biruni's key"})

    # --- PHOTOS ---
    if only_part in ("photos", "all"):
        import io
        import tempfile as _tf
        from qgis.core import QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer
        from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QRect
        from qgis.PyQt.QtGui import QColor, QImage
        from biruni.field_photos import bridge as _fp_bridge, dock as fd, form_dialog as fdiag, server as fs
        from biruni.gps.browser import qr_png

        fd.open_layer = lambda folder, add=True: None  # avoid GPKG side effects in temp grabs
        p_work = _tf.mkdtemp(prefix="fp_book_")
        p_folder = os.path.join(p_work, "Field photos")
        os.makedirs(p_folder, exist_ok=True)
        QgsSettings().setValue(fd.KEY + "folder", p_folder)
        QgsSettings().setValue(fd.KEY + "mode", "local")
        QgsSettings().setValue(fd.KEY + "kinds", "عمود، لوحة، كسر، تفتيش")
        QgsSettings().setValue(fd.KEY + "form", json.dumps(fdiag.example_form(), ensure_ascii=False))
        QgsSettings().setValue(fd.KEY + "qr-mode", "shared")

        plugin = PluginStub()
        # Fictional parcel layer "قطع الأراضي"
        parcels = QgsVectorLayer("Polygon?crs=EPSG:4326&field=parcel_no:string&field=owner:string&field=area:double",
                                 "قطع الأراضي", "memory")
        pr = parcels.dataProvider()
        for idx_p, (pno, own, dx) in enumerate([("101", "أحمد سالم", 0.0), ("102", "فهد العتيبي", 0.002),
                                                ("103", "خالد الحربي", 0.004)]):
            f = QgsFeature(parcels.fields())
            x0, y0 = 46.675 + dx, 24.713
            f.setGeometry(QgsGeometry.fromRect(__import__("qgis.core", fromlist=["QgsRectangle"]).QgsRectangle(
                x0, y0, x0 + 0.0015, y0 + 0.0015)))
            f["parcel_no"], f["owner"], f["area"] = pno, own, 450.0 + idx_p * 50
            pr.addFeatures([f])
        parcels.updateExtents()
        QgsProject.instance().addMapLayer(parcels)
        plugin.iface.setActiveLayer = lambda _l: None
        plugin.iface.activeLayer = lambda: parcels

        d = fd.open_dock(plugin)
        d._poll.stop()
        d._team_tick.stop()
        d.folder.setText(r"D:\Projects\مشروع حي النخيل\الصور الميدانية")

        def _fake_qr(widget_dock, url_str):
            pm = QPixmap()
            pm.loadFromData(qr_png(url_str, 5))
            widget_dock.qr.setPixmap(pm)

        # 1. fp-modes (top card showing mode selector & popup list or card)
        d.mode.setCurrentIndex(d.mode.findData("local"))
        d._show_hint()
        save_grab(d.mode.parentWidget(), "fp-modes", "photos", width=420, temp_paths=(p_work, p_folder))

        # 2. fp-local-qr (started local + sample photo received)
        _fake_qr(d, "https://192.168.1.25:10112/k9QmX2aP7wRt/")
        d.url.setText("https://192.168.1.25:10112/k9QmX2aP7wRt/")
        d.start_btn.setText(i18n.tr("Stop"))
        d.state.setText(i18n.tr("Scan the code with the phone's camera."))
        d.count.setText(i18n.tr("Photos received: {n}").format(n=12))
        thumb_img = QImage(320, 180, QImage.Format.Format_RGB32)
        thumb_img.fill(QColor("#d9ece9"))
        from qgis.PyQt.QtGui import QPainter, QPen
        painter = QPainter(thumb_img)
        painter.setPen(QPen(QColor("#0f8f80"), 3))
        painter.drawRect(30, 30, 260, 120)
        painter.setPen(QColor("#0b1f2a"))
        painter.drawText(thumb_img.rect(), int(Qt.AlignmentFlag.AlignCenter), "IMG_012 · عمود إنارة · 24.7136°N 46.6753°E")
        painter.end()
        d.thumb.setPixmap(QPixmap.fromImage(thumb_img))
        d.last_info.setText("سارة · عمود · ± 4 m · حي النخيل")
        save_grab(d.widget(), "fp-local-qr", "photos", width=420, temp_paths=(p_work, p_folder))

        # 3. fp-bridge-qr
        d.mode.setCurrentIndex(d.mode.findData("bridge"))
        d._show_hint()
        _fake_qr(d, "https://photos.biruni-gis.com/p/#k=ExampleBridgeKey000000000000000000000000000&l=ar")
        d.url.setText("photos.biruni-gis.com/p/")
        d.state.setText(i18n.tr("Ready. Scan the code with the phone's camera - it works on mobile data too."))
        save_grab(d.qr.parentWidget().parentWidget(), "fp-bridge-qr", "photos", width=420, temp_paths=(p_work, p_folder))

        # 4. fp-own-tunnel
        d.mode.setCurrentIndex(d.mode.findData("own"))
        d._show_hint()
        d.own_token.setText("eyJhIjoiZXhhbXBsZS10b2tlbiJ9")
        d.own_host.setText("photos.example.org")
        d.state.setText(i18n.tr("Not started"))
        d.start_btn.setText(i18n.tr("Start"))
        save_grab(d.mode.parentWidget(), "fp-own-tunnel", "photos", width=420, temp_paths=(p_work, p_folder))

        # 5. fp-tunnel-help
        grab_queue.append(("fp-tunnel-help", "photos", 480, (p_work, p_folder)))
        d._own_help()

        # 6. fp-qr-mode & 7. fp-surveyor-qr
        d.mode.setCurrentIndex(d.mode.findData("bridge"))
        d._show_hint()
        d.set_qr_mode("per_surveyor")
        d._on_qr_mode_changed()
        save_grab(d.mode.parentWidget(), "fp-qr-mode", "photos", width=420, temp_paths=(p_work, p_folder))

        # Issue personal QR for Sara
        d._base_qr_url = "https://photos.biruni-gis.com/p/#k=ExampleBridgeKey000000000000000000000000000&l=ar"
        _store = fs.PhotoStore(p_folder, fdiag.example_form())
        _store.records = d._photo_records
        d.server = type("SrvStub", (), {
            "store": _store,
            "set_guide": lambda self, items: len(items),
            "set_indoor": lambda self, payload: len(payload.get("units", [])),
            "stop": lambda self: None,
        })()
        s_info = d.issue_surveyor_qr("سارة")
        d.state.setText(i18n.tr("QR code ready for {who}.").format(who=s_info["name"]))
        save_grab(d.qr.parentWidget(), "fp-surveyor-qr", "photos", width=420, temp_paths=(p_work, p_folder))

        # 8. fp-form
        fdlg = fdiag.FormDialog(fdiag.example_form())
        fdlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        fdlg.show()
        fdlg.resize(740, 430)
        app.processEvents()
        pm_f = fdlg.grab()
        fdlg.hide()
        f_out = os.path.join(OUT_DIR, "fp-form.png")
        pm_f.save(f_out)
        f_sz = os.path.getsize(f_out)
        print(f"SHOT desktop photos fp-form.png {f_sz}B", flush=True)
        captured["fp-form"] = {"part": "photos", "size": f_sz}

        # 9. fp-team-list (5 Riyadh surveyors)
        d.set_qr_mode("shared")
        sk_file = os.path.join(p_folder, fs.SURVEYOR_KEYS_NAME)
        if os.path.exists(sk_file):
            os.remove(sk_file)
        now_t = time.time()
        sample_team = {
            "dev_sara": {"pos": {"id": "dev_sara", "name": "سارة", "lat": 24.7136, "lon": 46.6753, "acc": 4,
                                 "net": "cellular", "eff": "4g", "ping": 110, "bat": 0.84, "chg": False, "route": "direct"}, "at": now_t - 5},
            "dev_yusuf": {"pos": {"id": "dev_yusuf", "name": "يوسف", "lat": 24.7140, "lon": 46.6760, "acc": 6,
                                  "net": "cellular", "eff": "4g", "ping": 310, "bat": 0.62, "chg": True, "route": "relay"}, "at": now_t - 12},
            "dev_huda": {"pos": {"id": "dev_huda", "name": "هدى", "lat": 24.7128, "lon": 46.6741, "acc": 5,
                                 "net": "wifi", "eff": "", "ping": 75, "bat": 0.91, "chg": False, "route": "lan"}, "at": now_t - 4},
            "dev_omar": {"pos": {"id": "dev_omar", "name": "عمر", "lat": 24.7152, "lon": 46.6772, "acc": 9,
                                 "net": "cellular", "eff": "3g", "ping": 740, "bat": 0.28, "chg": False, "route": "peer"}, "at": now_t - 22},
            "dev_layla": {"pos": {"id": "dev_layla", "name": "ليلى", "lat": 24.7119, "lon": 46.6730, "acc": 5,
                                  "net": "cellular", "eff": "4g", "ping": 190, "bat": 0.55, "chg": False, "route": "direct"}, "at": now_t - 190},
        }
        d.team = sample_team
        today_iso = __import__("datetime").datetime.now().astimezone().strftime("%Y-%m-%dT10:15:00+03:00")
        d._photo_records = {
            "p1": {"id": "p1", "surveyor": "سارة", "device_id": "dev_sara", "taken": today_iso, "acc": 4, "route": "direct", "preview": False},
            "p2": {"id": "p2", "surveyor": "سارة", "device_id": "dev_sara", "taken": today_iso, "acc": 5, "route": "direct", "preview": False},
            "p3": {"id": "p3", "surveyor": "يوسف", "device_id": "dev_yusuf", "taken": today_iso, "acc": 6, "route": "relay", "preview": True},
            "p4": {"id": "p4", "surveyor": "يوسف", "device_id": "dev_yusuf", "taken": today_iso, "acc": 7, "route": "relay", "preview": True},
            "p5": {"id": "p5", "surveyor": "هدى", "device_id": "dev_huda", "taken": today_iso, "acc": 5, "route": "lan", "preview": False},
            "p6": {"id": "p6", "surveyor": "عمر", "device_id": "dev_omar", "taken": today_iso, "acc": 9, "route": "peer", "preview": False},
        }
        _store.records = d._photo_records
        d._show_team(include_disk=True)
        save_grab(d.team_info.parentWidget(), "fp-team-list", "photos", width=860, temp_paths=(p_work, p_folder))

        # 10. fp-team-menu
        d.team_list.setCurrentItem(d.team_list.topLevelItem(0))
        grab_queue.append(("fp-team-menu", "photos", None, (p_work, p_folder)))
        d._team_menu(QPoint(20, 20))

        # 11. fp-block (block Omar's device)
        d.server.store.block_device("dev_omar", "عمر", i18n.tr("Lost phone or left the team"))
        d._show_team(include_disk=True)
        d.state.setText(i18n.tr("Blocked device {d} ({who}). Existing photos are kept.").format(d="dev_omar", who="عمر"))
        save_grab(d.team_info.parentWidget(), "fp-block", "photos", width=860, temp_paths=(p_work, p_folder))

        # 12. fp-guide
        parcels.selectAll()
        d._send_guide()
        save_grab(d.guide_btn.parentWidget(), "fp-guide", "photos", width=480, temp_paths=(p_work, p_folder))

        # 13. fp-indoor & 14. fp-room-codes
        units = QgsVectorLayer(
            "Polygon?crs=EPSG:4326&field=unit_id:string&field=level_id:integer&field=name:string&field=number:string",
            "غرف المبنى", "memory")
        upr = units.dataProvider()
        for uid_s, lv_i, nm_s, num_s, dx in [
            ("U-101", 1, "الاستقبال", "101", 0.0),
            ("U-102", 1, "مكتب المساحة", "102", 0.0001),
            ("U-201", 2, "الأرشيف", "201", 0.0),
            ("U-202", 2, "غرفة الخرائط", "202", 0.0001),
        ]:
            uf = QgsFeature(units.fields())
            uf.setGeometry(QgsGeometry.fromRect(__import__("qgis.core", fromlist=["QgsRectangle"]).QgsRectangle(
                46.675 + dx, 24.713, 46.675 + dx + 0.00008, 24.71308)))
            uf["unit_id"], uf["level_id"], uf["name"], uf["number"] = uid_s, lv_i, nm_s, num_s
            upr.addFeatures([uf])
        units.updateExtents()
        QgsProject.instance().addMapLayer(units)
        d._send_indoor()
        save_grab(d.indoor_btn.parentWidget(), "fp-indoor", "photos", width=480, temp_paths=(p_work, p_folder))

        pdf_rooms = os.path.join(p_work, "room_codes.pdf")
        fd.room_labels_pdf(units, pdf_rooms)
        rc_path = os.path.join(OUT_DIR, "fp-room-codes.png")
        from PyQt6.QtPdf import QPdfDocument
        from qgis.PyQt.QtCore import QSize
        pdoc = QPdfDocument(None)
        pdoc.load(pdf_rooms)
        pimg = pdoc.render(0, QSize(1100, 1556))
        pimg.save(rc_path)
        pdoc.close()
        rc_sz = os.path.getsize(rc_path)
        print(f"SHOT desktop photos fp-room-codes.png {rc_sz}B", flush=True)
        captured["fp-room-codes"] = {"part": "photos", "size": rc_sz}

        # 15. fp-preview-list
        d.server.store.unblock_device("dev_omar")
        d.team = {
            "dev_yusuf": sample_team["dev_yusuf"],
            "dev_omar": {"pos": {"id": "dev_omar", "name": "عمر", "lat": 24.7152, "lon": 46.6772, "acc": 8,
                                 "net": "cellular", "eff": "4g", "ping": 480, "bat": 0.44, "chg": False, "route": "relay"}, "at": now_t - 8},
        }
        d._photo_records["p6"] = {"id": "p6", "surveyor": "عمر", "device_id": "dev_omar", "taken": today_iso, "acc": 8, "route": "relay", "preview": True}
        d._show_team(include_disk=True)
        d.state.setText(i18n.tr("A phone is connected through the relays (previews now, originals on Wi-Fi)."))
        save_grab(d.team_info.parentWidget(), "fp-preview-list", "photos", width=860, temp_paths=(p_work, p_folder))

        # 16. fp-daily-report (write HTML report and render via QTextDocument / or let phone worker render HTML? Wait - we have QTextBrowser or QWebEngine in phone worker; let's render HTML via QTextDocument on QImage? No, QWebEngine in phone worker renders modern CSS cleanly! Or let's pass daily report path to phone worker or write a temp HTML and render in phone worker? Let's write daily report to p_folder/daily_report.html? Actually `write_daily_report` can be called directly in phone worker too since `field_photos.dock` imports cleanly in phone worker or we just write the HTML in desktop worker to `SCRATCH_DIR/scratch/daily_report.html` and render it in phone worker!)
        rep_html = os.path.join(SCRATCH_DIR, "scratch", "daily_report.html")
        rep_recs = [
            {"id": "p1", "surveyor": "سارة", "device_id": "dev_sara", "taken": today_iso, "acc": 4, "preview": False},
            {"id": "p2", "surveyor": "سارة", "device_id": "dev_sara", "taken": today_iso, "acc": 5, "preview": False},
            {"id": "p3", "surveyor": "يوسف", "device_id": "dev_yusuf", "taken": today_iso, "acc": 6, "preview": True},
            {"id": "p4", "surveyor": "يوسف", "device_id": "dev_yusuf", "taken": today_iso, "acc": 7, "preview": True},
            {"id": "p5", "surveyor": "هدى", "device_id": "dev_huda", "taken": today_iso, "acc": 5, "preview": False},
            {"id": "p6", "surveyor": "عمر", "device_id": "dev_omar", "taken": today_iso, "acc": 8, "preview": True},
        ]
        fd.write_daily_report(rep_html, rep_recs)

        d.server = None
        fd.close_dock(plugin)

    # --- DRIVE ---
    if only_part in ("drive", "all"):
        import tempfile as _tf
        from biruni.field_photos import bridge as _fp_bridge, dock as fd, drive as drv_mod, handover as ho_mod

        fd.open_layer = lambda folder, add=True: None
        d_work = _tf.mkdtemp(prefix="fd_book_")
        d_folder = os.path.join(d_work, "Field photos")
        d_root = os.path.join(d_work, "My Drive")
        os.makedirs(d_folder, exist_ok=True)
        os.makedirs(d_root, exist_ok=True)
        drv_mod.suggest_roots = lambda: [d_root]

        QgsSettings().setValue(fd.KEY + "folder", d_folder)
        QgsSettings().setValue(fd.KEY + "mode", "bridge")
        QgsSettings().setValue(fd.DRIVE_ROOTS, json.dumps([d_root]))
        QgsSettings().setValue(fd.DRIVE_MADE, "[]")
        QgsSettings().setValue(fd.KEY_BACKUP, "")
        QgsProject.instance().removeEntry(fd.P_SCOPE, fd.P_DRIVE)
        QgsProject.instance().removeEntry(fd.P_SCOPE, fd.P_NOTICE)
        QgsProject.instance().removeEntry(fd.P_SCOPE, fd.P_CLEAN)

        plugin = PluginStub()
        d = fd.open_dock(plugin)
        d._poll.stop()
        d._team_tick.stop()
        d.folder.setText(r"D:\Projects\مشروع حي النخيل\الصور الميدانية")

        # 1. fd-off
        save_grab(d.mode.parentWidget(), "fd-off", "drive", width=430, temp_paths=(d_work, d_folder, d_root))

        # 2. fd-confirm
        grab_queue.append(("fd-confirm", "drive", None, (d_work, d_folder, d_root)))
        d._confirm_drive()

        # 3. fd-on (enable Drive without modal confirmation, create office folder)
        QgsProject.instance().writeEntryBool(fd.P_SCOPE, fd.P_DRIVE, True)
        QgsProject.instance().writeEntryBool(fd.P_SCOPE, fd.P_NOTICE, True)
        k_bridge = _fp_bridge.load_key(QgsSettings())
        office_path, _ = drv_mod.ensure_office_folder([d_root], k_bridge)
        d._sync_drive()
        d.drive_list.clear()
        d.drive_list.addItem(r"G:\My Drive")
        d.drive_share.setText(d.drive_share.text().replace(d_root, r"G:\My Drive"))
        save_grab(d.mode.parentWidget(), "fd-on", "drive", width=440, temp_paths=(d_root, d_folder, d_work))

        # 4. fd-key-nudge
        grab_queue.append(("fd-key-nudge", "drive", None, (d_root, d_folder, d_work)))
        d._nudge_backup()

        # 5. fd-key-password
        grab_queue.append(("fd-key-password", "drive", 440, (d_root, d_folder, d_work)))
        d._ask_backup_password()

        # 6. fd-key-backed
        kid = d._pc_kid()
        d._record_key_backup(kid, "2026-11-18")
        d._show_key_state()
        d.drive_state.setText(i18n.tr("Key backed up."))
        save_grab(d.mode.parentWidget(), "fd-key-backed", "drive", width=440, temp_paths=(d_root, d_folder, d_work))

        # 7. fd-list-drive
        now_t = time.time()
        today_iso = __import__("datetime").datetime.now().astimezone().strftime("%Y-%m-%dT10:25:00+03:00")
        d.team = {
            "dev_sara": {"pos": {"id": "dev_sara", "name": "سارة", "lat": 24.7136, "lon": 46.6753, "acc": 4,
                                 "net": "cellular", "eff": "4g", "ping": 115, "bat": 0.80, "chg": False, "route": "drive"}, "at": now_t - 4},
            "dev_yusuf": {"pos": {"id": "dev_yusuf", "name": "يوسف", "lat": 24.7140, "lon": 46.6760, "acc": 5,
                                  "net": "cellular", "eff": "4g", "ping": 210, "bat": 0.72, "chg": False, "route": "drive"}, "at": now_t - 9},
        }
        d._photo_records = {
            "dp1": {"id": "dp1", "surveyor": "سارة", "device_id": "dev_sara", "taken": today_iso, "acc": 4, "route": "drive", "preview": False},
            "dp2": {"id": "dp2", "surveyor": "سارة", "device_id": "dev_sara", "taken": today_iso, "acc": 4, "route": "drive", "preview": False},
            "dp3": {"id": "dp3", "surveyor": "يوسف", "device_id": "dev_yusuf", "taken": today_iso, "acc": 5, "route": "drive", "preview": True},
        }
        d._show_team(include_disk=True)
        save_grab(d.team_info.parentWidget(), "fd-list-drive", "drive", width=860, temp_paths=(d_root, d_folder, d_work))

        # 8. fd-status-checked
        d._drive_last = {"new": 0, "refused": 0, "reasons": []}
        d._on_drive({"new": 4, "refused": [], "folders": 1, "at": now_t})
        save_grab(d.mode.parentWidget(), "fd-status-checked", "drive", width=440, temp_paths=(d_root, d_folder, d_work))

        # 9. fd-folder-missing
        import shutil as _shutil
        if office_path and os.path.isdir(office_path):
            _shutil.rmtree(office_path, ignore_errors=True)
        d._office_folder_state(create=False)
        d._on_drive({"new": 0, "refused": [], "folders": 0, "at": now_t})
        save_grab(d.mode.parentWidget(), "fd-folder-missing", "drive", width=440, temp_paths=(d_root, d_folder, d_work))

        fd.close_dock(plugin)

    # --- GPS ---
    if only_part in ("gps", "all"):
        import datetime as _dt
        from biruni import tracking
        from biruni.gps.browser import qr_png
        from biruni.gps.nmea import Fix

        QgsSettings().setValue(tracking.KEY + "mode", "bridge")
        QgsSettings().setValue(tracking.KEY + "detail", "normal")
        QgsSettings().setValue(tracking.KEY + "drive", False)
        plugin = PluginStub()
        plugin.tracking = tracking.TrackingDock(plugin)
        td = plugin.tracking
        td.refresh_theme()
        td._clock.stop()
        td._dr_timer.stop()

        # 1. gps-bridge
        save_grab(td.widget(), "gps-bridge", "gps", width=420)

        # 2. gps-serial
        td.mode.setCurrentIndex(td.mode.findData("serial"))
        td.com.clear()
        td.com.addItem("COM4 - Bluetooth GNSS Receiver", "COM4")
        save_grab(td.mode.parentWidget(), "gps-serial", "gps", width=420)

        # 3. gps-qr
        td.mode.setCurrentIndex(td.mode.findData("bridge"))
        pm = QPixmap()
        pm.loadFromData(qr_png("https://photos.biruni-gis.com/p/#k=ExampleGpsKey00000000000000000000000000000&m=gps&l=ar", 5))
        td.qr.setPixmap(pm)
        td.qr_url.setText("photos.biruni-gis.com/p/")
        td.connect_btn.setText(i18n.tr("Disconnect"))
        td.state.setText(i18n.tr("Waiting for the phone…"))
        save_grab(td.mode.parentWidget(), "gps-qr", "gps", width=420)

        # 4. gps-receiving
        fix1 = Fix(time=_dt.datetime.now(_dt.timezone.utc), lat=24.713642, lon=46.675318, alt=612.0,
                   speed_kmh=5.4, course=62.0, hdop=0.8, satellites=11, quality=1, valid=True, sentences={"browser"})
        td.source.is_active = lambda: True
        td._on_fix(fix1)
        td._last_data = _dt.datetime.now()
        td._refresh_clock()
        td._on_link({"via": "direct", "backlog": 0})
        save_grab(td.widget(), "gps-receiving", "gps", width=420)

        # 5. gps-catching-up
        td._on_link({"via": "own", "backlog": 48, "hosts": ["relay.biruni-gis.com"]})
        td.widget().setFixedWidth(420)
        td.widget().adjustSize()
        app.processEvents()
        save_grab(td.mode.parentWidget(), "gps-catching-up", "gps", width=420)

        # 6. gps-recording
        td.toggle_recording()
        for k in range(1, 15):
            fk = Fix(time=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=k * 3),
                     lat=24.713642 + k * 0.00018, lon=46.675318 + k * 0.00014, alt=612.5,
                     speed_kmh=5.2, course=42.0, hdop=0.8, satellites=11, quality=1, valid=True, sentences={"browser"})
            td.recorder.add(fk)
        td._refresh_record_info()
        save_grab(td.record_btn.parentWidget(), "gps-recording", "gps", width=420)

        # 7. gps-saved
        td.toggle_recording()
        td.rec_info.setText(i18n.tr("Saved: {name} - {summary}").format(
            name=td.track_name.text(),
            summary=i18n.tr("{km} km in {min} min, {stops} stops").format(km=0.35, min=1.2, stops=1),
        ))
        save_grab(td.record_btn.parentWidget(), "gps-saved", "gps", width=420)

        # 8. gps-no-data
        td._last_data = _dt.datetime.now() - _dt.timedelta(seconds=18)
        td._refresh_clock()
        save_grab(td.widget(), "gps-no-data", "gps", width=420)
        td.disconnect_source()

    if only_part in ("channels", "all"):
        from biruni.field_photos import bridge as _br
        from biruni.field_photos.dock import FieldPhotosDock as _FPD
        from biruni.field_photos.org_relay_dialog import OrgRelayDialog as _ORD
        from qgis.PyQt.QtWidgets import QInputDialog as _QID

        old_org = _br.org_host()
        try:
            _br.set_org_host("")
            dlg = _ORD()
            save_grab(dlg, "org-empty", "channels", width=520)

            dlg.token_edit.setText("cfat_dummy_token_placeholder")
            dlg.status_lbl.setText(i18n.tr("Deploying relay…"))
            save_grab(dlg, "org-token", "channels", width=520)

            qid = _QID()
            qid.setWindowTitle(i18n.tr("Choose Cloudflare account"))
            qid.setLabelText(i18n.tr("Account:"))
            qid.setComboBoxItems(["Survey Authority (acc-1a2b)", "Field Operations (acc-3c4d)"])
            qid.setComboBoxEditable(False)
            save_grab(qid, "org-accounts", "channels", width=380)

            _br.set_org_host("biruni-relay.survey-org.workers.dev")
            dlg._refresh_addr()
            dlg.token_edit.clear()
            dlg.status_lbl.setText(i18n.tr("Relay deployed and active: {host}").format(
                host="biruni-relay.survey-org.workers.dev"
            ))
            save_grab(dlg, "org-deployed", "channels", width=520)

            _br.set_org_host("")
            dlg._refresh_addr()
            dlg.status_lbl.setText(i18n.tr("Deployment failed."))
            save_grab(dlg, "org-failed", "channels", width=520)
            dlg.close()

            d_ch = tempfile.mkdtemp(prefix="biruni-ch-")
            plug_ch = PluginStub()
            fpd = _FPD(plug_ch)
            fpd.refresh_theme()
            fpd.folder.setText(r"D:\Projects\مشروع حي النخيل\الصور الميدانية")
            fpd.mode.setCurrentIndex(max(0, fpd.mode.findData("bridge")))
            fpd._show_hint()
            fpd._relays_up = 0
            fpd.start_btn.setText(i18n.tr("Stop"))
            fpd._on_bridge({
                "type": "relays",
                "up": 5,
                "hosts": ["nos.lol", "relay.primal.net", "broker.hivemq.com", "ntfy.sh", "relay.biruni-gis.com"],
            })
            card_ch = fpd.mode.parentWidget()
            for _ in range(3):
                card_ch.layout().activate()
                fpd.widget().setFixedWidth(440)
                fpd.widget().adjustSize()
                app.processEvents()
            card_ch.setFixedHeight(card_ch.sizeHint().height() + 6)
            save_grab(card_ch, "ch-relays-connected", "channels", width=440, temp_paths=[d_ch])

            card_ch.setMinimumHeight(0)
            card_ch.setMaximumHeight(16777215)
            fpd.close()

            from biruni import tracking as _trk
            QgsSettings().setValue(_trk.KEY + "mode", "bridge")
            plug_ch2 = PluginStub()
            td_ch = _trk.TrackingDock(plug_ch2)
            td_ch.refresh_theme()
            td_ch._clock.stop()
            td_ch._dr_timer.stop()
            td_ch.connect_btn.setText(i18n.tr("Disconnect"))
            td_ch.state.setText(i18n.tr("Waiting for the phone…"))
            td_ch._on_link({
                "relays_up": 5,
                "hosts": ["nos.lol", "relay.primal.net", "broker.hivemq.com", "ntfy.sh", "relay.biruni-gis.com"],
                "tunnel_adding": True,
            })
            card_t = td_ch.mode.parentWidget()
            td_ch.hint4.setMinimumHeight(70)
            td_ch.link_lbl.setMinimumHeight(42)
            card_t.layout().activate()
            td_ch.widget().setFixedWidth(420)
            td_ch.widget().adjustSize()
            app.processEvents()
            card_t.setFixedHeight(card_t.sizeHint().height() + 4)
            save_grab(card_t, "ch-tunnel-adding", "channels", width=420)
            td_ch.close()
        finally:
            _br.set_org_host(old_org)

    if only_part in ("show", "all"):
        from biruni.gps.browser import qr_png
        from biruni.live_share.dock import LiveShareDock
        from biruni.dashboard import cards as _dcards, dock as _ddock, model as _dmodel, seals as _dseals
        from biruni.share import qfield_dialog as _qfd

        # 1. show-map-off, 2. show-map-on
        plug_ls = PluginStub()
        lsd = LiveShareDock(plug_ls)
        lsd.refresh_theme()
        save_grab(lsd.widget(), "show-map-off", "show", width=420)

        sample_url = "https://biruni-gis.com/s/#k=ExampleLiveShareKey0000000000000000000000&l=ar&p=2"
        pm_ls = QPixmap()
        pm_ls.loadFromData(qr_png(sample_url, 5))
        lsd.link = sample_url
        lsd.qr.setPixmap(pm_ls)
        lsd.url.setText(sample_url)
        lsd.viewers.setText(i18n.tr("Watching now: {n}").format(n=2))
        lsd.qr_card.setVisible(True)
        lsd.start_btn.setText(i18n.tr("Stop sharing"))
        lsd.state.setText(i18n.tr("Ready. Send the link, or let someone scan the code."))
        save_grab(lsd.widget(), "show-map-on", "show", width=440)
        lsd.close()

        # 3. show-dash-dock, 4. show-dash-card, 5. show-dash-links, 6. show-seal-pc
        tmp_dash = tempfile.mkdtemp(prefix="biruni-show-")
        old_seed = list(_dcards._TEST_SEED)
        old_sp = list(_dseals._PATH)
        proj = QgsProject.instance()
        old_dash_prop = proj.readEntry("biruni", "dashboards", "")[0]
        old_card_prop = proj.readEntry("biruni", "dashboard_cards", "")[0]
        old_rev_prop = proj.readEntry("biruni", "dashboard_revoked", "")[0]
        try:
            _dcards._TEST_SEED[:] = [b"\x17" * 32]
            _dseals._PATH[:] = [os.path.join(tmp_dash, "seals.sqlite")]
            _dseals._CON.clear()
            d1 = {
                "id": "d_poles",
                "title": "متابعة أعمدة الإنارة",
                "sources": {"s1": {"filter": "owner_phone IS NOT NULL AND status <> ''"}},
                "widgets": [
                    {"id": "w1", "type": "kpi", "title": "إجمالي الأعمدة", "source": "s1", "measure": {"field": "pole_id"}},
                    {"id": "w2", "type": "bar", "title": "الأعمدة حسب الحي", "source": "s1", "group": {"field": "district"}},
                    {"id": "w3", "type": "map", "title": "خريطة التوزيع", "source": "s1"},
                ],
                "filters": [{"id": "f_dist", "label": "الحي", "field": "district", "layer": "poles"}],
            }
            d2 = {"id": "d_cov", "title": "تغطية الأحياء اليومية", "widgets": [{"id": "w4", "type": "kpi", "title": "نسبة الإنجاز"}]}
            _dmodel.save(proj, d1)
            _dmodel.save(proj, d2)
            _dcards.issue(d1["id"], "مشرف شمال الرياض", ["w1", "w2", "w3"], ["owner_phone"],
                          "POLYGON((46.65 24.70, 46.70 24.70, 46.70 24.75, 46.65 24.75, 46.65 24.70))",
                          {"f_dist": ["النخيل"]}, days=30, project=proj)
            _dcards.issue(d1["id"], "مراقب سابق", "*", (), days=-2, project=proj)

            plug_d = PluginStub()
            dd = _ddock.DashboardDock(plug_d)
            dd.timer.stop()
            dd.on_lan.blockSignals(True)
            dd.on_any.blockSignals(True)
            dd.on_lan.setChecked(True)
            dd.on_any.setChecked(True)
            dd.on_lan.blockSignals(False)
            dd.on_any.blockSignals(False)
            dd.refresh()
            dd.state.setText(i18n.tr("Shared {where}. Viewers now: {n}.").format(
                where=i18n.tr("on this network") + " + " + i18n.tr("from anywhere"), n=3
            ))
            save_grab(dd.widget(), "show-dash-share", "show", width=460)

            cdlg = _ddock.CardDialog(d1, plug_d)
            cdlg.name.setText("مشرف شمال الرياض")
            for fkey, le in cdlg.forced.items():
                le.setText("النخيل")
            for i in range(cdlg.hide.count()):
                if cdlg.hide.item(i).text() == "owner_phone":
                    cdlg.hide.item(i).setCheckState(Qt.CheckState.Checked)
            save_grab(cdlg, "show-dash-card", "show", width=480)
            cdlg.close()

            ldlg = _ddock.LinksDialog({
                "lan": ["https://192.168.1.25:10116/d/xK9pQ2/#c=BDC1.example"],
                "anywhere": "https://biruni-gis.com/d/#k=ExampleDashKey000000000000000000000000&c=BDC1.example",
            }, "مشرف شمال الرياض")
            save_grab(ldlg, "show-dash-links", "show", width=440)
            ldlg.close()

            seal_code = _dseals.make(
                "متابعة أعمدة الإنارة", "w1", "إجمالي الأعمدة", {"value": 142},
                {"الحي": "النخيل"}, {"id": "c1", "name": "مشرف شمال الرياض"}, "sha256demo",
            )
            sd = _ddock.SealDialog()
            sd.input.setPlainText(seal_code)
            sd.check()
            save_grab(sd, "show-seal-pc", "show", width=560)
            sd.close()
            dd.close()
        finally:
            _dcards._TEST_SEED[:] = old_seed
            _dseals._PATH[:] = old_sp
            _dseals._CON.clear()
            proj.writeEntry("biruni", "dashboards", old_dash_prop)
            proj.writeEntry("biruni", "dashboard_cards", old_card_prop)
            proj.writeEntry("biruni", "dashboard_revoked", old_rev_prop)

        # show-liveqr (print layout corner with real Living Paper QR code)
        from qgis.core import (QgsCoordinateReferenceSystem, QgsLayoutExporter, QgsLayoutItemLabel,
                               QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout, QgsRectangle)
        from biruni.living_paper import layout as _lp_lay
        lyt = QgsPrintLayout(proj)
        lyt.initializeDefaults()
        mp = QgsLayoutItemMap(lyt)
        mp.attemptMove(QgsLayoutPoint(12, 18))
        mp.attemptResize(QgsLayoutSize(273, 175))
        mp.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        mp.setExtent(QgsRectangle(46.670, 24.710, 46.682, 24.718))
        mp.setFrameEnabled(True)
        lyt.addLayoutItem(mp)
        ttl_lbl = QgsLayoutItemLabel(lyt)
        ttl_lbl.setText("مخطط حي النخيل - شبكة الإنارة")
        ttl_lbl.attemptMove(QgsLayoutPoint(12, 5))
        ttl_lbl.attemptResize(QgsLayoutSize(180, 12))
        lyt.addLayoutItem(ttl_lbl)
        _lp_lay.add_live_qr(lyt, title="مخطط حي النخيل")
        exp = QgsLayoutExporter(lyt)
        from qgis.PyQt.QtCore import QSize as _QS
        pg_w = lyt.pageCollection().page(0).pageSize().width()
        pg_h = lyt.pageCollection().page(0).pageSize().height()
        scale_px = 120.0 / 25.4
        qimg = exp.renderPageToImage(0, _QS(round(pg_w * scale_px), round(pg_h * scale_px)))
        qr_item = lyt.itemById(_lp_lay.QR_ID)
        cap_item = lyt.itemById(_lp_lay.CAPTION_ID)
        px = qr_item.positionWithUnits().x()
        py = qr_item.positionWithUnits().y()
        pw = qr_item.sizeWithUnits().width()
        ph = qr_item.sizeWithUnits().height() + cap_item.sizeWithUnits().height()
        pad_px = int(14 * scale_px)
        rx = max(0, int(px * scale_px) - pad_px)
        ry = max(0, int(py * scale_px) - pad_px)
        rw = int((pw + 28) * scale_px)
        rh = int((ph + 28) * scale_px)
        cropped = qimg.copy(rx, ry, min(rw, qimg.width() - rx), min(rh, qimg.height() - ry))
        out_liveqr = os.path.join(OUT_DIR, "show-liveqr.png")
        cropped.save(out_liveqr)
        sz_lq = os.path.getsize(out_liveqr)
        print(f"SHOT desktop show show-liveqr.png {sz_lq}B", flush=True)
        captured["show-liveqr"] = {"part": "show", "size": sz_lq}

        # 7. show-field-send, 8. show-field-back
        from qgis.PyQt.QtWidgets import QLineEdit as _QLE, QTableWidgetItem as _QTWI

        plug_qf = PluginStub()
        sf = _qfd.SendToFieldDialog(plug_qf)
        pe_edit = sf.out.findChild(_QLE)
        if pe_edit is not None:
            pe_edit.setText(r"D:\Projects\حي النخيل\QField")
        sf.basemap.setEnabled(True)
        sf.basemap.setChecked(True)
        sf.estimate.setText(i18n.tr("≈ {n} tiles, {mb} MB").format(n=420, mb="6.2"))
        sf.zip.setChecked(True)
        sf.setMinimumHeight(sf.sizeHint().height() + 28)
        save_grab(sf, "show-field-send", "show", width=580)
        sf.close()

        bb = _qfd.BringBackDialog(plug_qf)
        bb.path.setText(r"D:\Projects\حي النخيل\QField_ميدان")
        rows_qf = [("أعمدة الإنارة", "5", "2", "0", "1"), ("خطوط التغذية", "1", "3", "1", "0")]
        bb.table.setRowCount(len(rows_qf))
        for r_i, vals in enumerate(rows_qf):
            for c_i, v_s in enumerate(vals):
                it_q = _QTWI(v_s)
                if c_i:
                    it_q.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                bb.table.setItem(r_i, c_i, it_q)
        bb.table.setMinimumHeight(115)
        bb.note.setText(i18n.tr(
            "{n} features were also changed in the office after the copy was made - "
            "they are left as they are unless you tick the box below."
        ).format(n=1))
        bb.force.setEnabled(True)
        bb.apply_btn.setEnabled(True)
        bb.setMinimumHeight(bb.sizeHint().height() + 24)
        save_grab(bb, "show-field-back", "show", width=620)
        bb.close()

    if only_part in ("room", "all"):
        from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle, QgsVectorLayer
        from qgis.gui import QgsMessageBar
        from qgis.PyQt.QtWidgets import QTextEdit as _QTE, QTreeWidgetItem, QVBoxLayout as _QVBL, QWidget as _QW
        from biruni.team import dock as _tdock, session as _tsess
        from biruni.team.code import RoomKeys as _RKeys

        proj_rm = QgsProject.instance()
        proj_rm.setTitle("مشروع حي النخيل")
        _tsess.set_my_name("خالد")

        tmp_rm = tempfile.mkdtemp(prefix="biruni-rm-")
        lyr_gpkg_path = os.path.join(tmp_rm, "poles.gpkg")
        from qgis.core import QgsFields, QgsField, QgsVectorFileWriter, QgsWkbTypes
        from qgis.PyQt.QtCore import QMetaType
        mem_p = QgsVectorLayer("Point?crs=EPSG:4326", "أعمدة الإنارة", "memory")
        mem_p.dataProvider().addAttributes([QgsField("name", QMetaType.Type.QString)])
        mem_p.updateFields()
        feats_p = []
        for idx_p, (px_p, py_p, nm_p) in enumerate((
            (46.672, 24.712, "P-101"), (46.675, 24.714, "P-102"),
            (46.678, 24.715, "P-103"), (46.674, 24.716, "P-104"),
        )):
            ft = QgsFeature(mem_p.fields())
            ft.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(px_p, py_p)))
            ft.setAttributes([nm_p])
            feats_p.append(ft)
        mem_p.dataProvider().addFeatures(feats_p)
        opts_w = QgsVectorFileWriter.SaveVectorOptions()
        opts_w.driverName = "GPKG"
        opts_w.layerName = "poles"
        QgsVectorFileWriter.writeAsVectorFormatV3(mem_p, lyr_gpkg_path, proj_rm.transformContext(), opts_w)
        gpkg_lyr = QgsVectorLayer(lyr_gpkg_path + "|layername=poles", "أعمدة الإنارة", "ogr")
        roads_lyr = QgsVectorLayer("LineString?crs=EPSG:4326", "شبكة الشوارع", "memory")
        ft_r1 = QgsFeature()
        ft_r1.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(46.670, 24.713), QgsPointXY(46.680, 24.715)]))
        ft_r2 = QgsFeature()
        ft_r2.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(46.675, 24.710), QgsPointXY(46.675, 24.718)]))
        roads_lyr.dataProvider().addFeatures([ft_r1, ft_r2])
        proj_rm.addMapLayers([gpkg_lyr, roads_lyr])

        plug_rm = PluginStub()
        td_rm = _tdock.TeamDock(plug_rm)
        td_rm.refresh_theme()
        td_rm._live.stop()
        td_rm._pending_timer.stop()

        # 1. tm-start
        save_grab(td_rm.widget(), "tm-start", "room", width=440)

        # 2. tm-open-room
        odlg = _tdock.OpenDialog(plug_rm.iface.mainWindow())
        odlg.name.setText("مشروع حي النخيل")
        odlg.me.setText("خالد")
        odlg.outside.setChecked(True)
        save_grab(odlg, "tm-open-room", "room", width=460)
        odlg.close()

        # 3. tm-join-net
        jdlg = _tdock.JoinDialog(plug_rm.iface.mainWindow())
        jdlg.code.setText("K7QM-2D9X-HR4P-8WZT")
        jdlg.me.setText("سارة")
        jdlg.folder.setText(r"D:\Projects\غرف البيروني")
        jdlg.state.setText(i18n.tr("Found. Waiting for the host to let you in…"))
        save_grab(jdlg, "tm-join-net", "room", width=480)
        jdlg.close()

        # Build lightweight stub Hub/Store/Node for clean screenshots of host/member/control panel/let-in
        now_ts = time.time()
        rk_rm = _RKeys("K7QM-2D9X-HR4P-8WZT")

        class _StoreStub:
            def __init__(self):
                self._d = {
                    "name": "مشروع حي النخيل",
                    "code": _tsess._protect(rk_rm.code),
                    "room_code": rk_rm.code,
                    "external": True,
                }
            def get(self, k, default=None):
                return self._d.get(k, default)
            def open_conflicts(self):
                return []
            def recent_log(self, limit=40):
                return [
                    {"seq": 18, "ts": now_ts - 90, "name": "سارة", "layer": "poles", "kind": "add", "fid": 1000012},
                    {"seq": 19, "ts": now_ts - 40, "name": "عمر", "layer": "poles", "kind": "edit", "fid": 1000004},
                ]
            def members(self):
                return []

        class _HubStub:
            store = _StoreStub()
            port = 10420
            open_door = True
            auto_accept = False
            frozen = False
            presence = {
                "m2": {"c": [46.6738, 24.7145], "name": "سارة", "ts": now_ts - 2},
                "m3": {"c": [46.6768, 24.7132], "name": "عمر", "ts": now_ts - 4},
            }
            def numbers(self):
                per = [0]*18 + [2, 5, 4, 8, 12, 9, 14, 11, 16, 10, 15, 14]
                return {
                    "online": ["m2", "m3"],
                    "last_min": 14,
                    "today": 186,
                    "merged": 12,
                    "conflicts": 0,
                    "apply_ms": 6.4,
                    "apply_max": 18.2,
                    "queue": 0,
                    "bytes_in": 48200,
                    "bytes_out": 194500,
                    "requests": 312,
                    "uptime": 5420,
                    "size": 1450000,
                    "epoch": 1,
                    "per_min": per,
                    "by": {"m1": 52, "m2": 74, "m3": 42, "m4": 18},
                    "waiting": [{"id": "m5", "name": "يوسف", "fingerprint": "8F2C 19A4 D70B 33E1"}],
                    "errors": [],
                }

        class _LinkStub:
            def members(self):
                return 1

        class _NodeStub:
            def __init__(self, role):
                self.role = role
                self.me_id = "m1" if role == "host" else "m2"
                self.keys = rk_rm
                self.store = _StoreStub()
                self.hub = _HubStub() if role == "host" else None
                self.link = _LinkStub() if role == "host" else None
                self.external_url = "link:" if role == "host" else None
                self.layers = {"poles": gpkg_lyr}
                self.roster = [
                    {"id": "m1", "name": "خالد", "host": True, "rank": 0, "state": "online", "seen": now_ts - 1, "edits": 52},
                    {"id": "m2", "name": "سارة", "host": False, "rank": 1, "state": "editing", "seen": now_ts - 2, "edits": 74},
                    {"id": "m3", "name": "عمر", "host": False, "rank": 2, "state": "online", "seen": now_ts - 4, "edits": 42},
                    {"id": "m4", "name": "طارق", "host": False, "rank": 3, "state": "away", "seen": now_ts - 240, "edits": 18},
                ]
                self.presence = {
                    "m1": {"c": [46.6750, 24.7155], "name": "خالد", "ts": now_ts - 1},
                    "m3": {"c": [46.6768, 24.7132], "name": "عمر", "ts": now_ts - 4},
                }
                self.state = "online"
                self.member = {"id": "m2", "name": "سارة", "rights": {"mode": "edit"}}
                self._areas_zones = {
                    "m2": (QgsRectangle(46.6725, 24.7130, 46.6750, 24.7152), "سارة"),
                    "m3": (QgsRectangle(46.6758, 24.7120, 46.6782, 24.7142), "عمر"),
                }
            def my_rank(self):
                return 0 if self.role == "host" else 1
            def zones(self):
                return self._areas_zones
            def key_of(self, lyr):
                return "poles" if lyr == gpkg_lyr else None

        td_rm.attach = lambda _n: None
        td_rm.node = _NodeStub("host")
        td_rm.pages.setCurrentIndex(1)
        td_rm.tabs.setTabVisible(1, True)
        td_rm.title.setText("مشروع حي النخيل")
        td_rm.badge.setText(i18n.tr("Host (server)"))
        td_rm.state.setText(i18n.tr("This computer is the room's server. Keep it on while the team works."))
        td_rm.code.setText(rk_rm.code)
        td_rm.members.clear()
        for mem in td_rm.node.roster:
            it_m = QTreeWidgetItem([
                mem["name"] + (" (" + i18n.tr("you") + ")" if mem["id"] == "m1" else ""),
                i18n.tr(mem["state"]),
                str(mem["edits"]),
                i18n.tr("yes") if mem["rank"] <= 2 else "",
            ])
            td_rm.members.addTopLevelItem(it_m)
        td_rm.tabs.setCurrentIndex(0)
        td_rm.members.setMinimumHeight(112)
        td_rm.members.setMaximumHeight(135)
        # 4. tm-dock-host
        save_grab(td_rm.widget(), "tm-dock-host", "room", width=440)

        # 5. tm-qr
        grab_queue.append(("tm-qr", "room", 340, ()))
        td_rm._show_qr()

        # 6. tm-control-panel + 7. tm-let-in
        td_rm.tabs.setCurrentIndex(1)
        td_rm._panel_numbers()
        for cb_r in (td_rm.door, td_rm.approve, td_rm.outside):
            cb_r.blockSignals(True)
            cb_r.setChecked(True)
            cb_r.blockSignals(False)
        td_rm.addresses.setText(
            i18n.tr("The room's addresses (the code finds them by itself):")
            + "\nhttp://192.168.1.25:10420\n"
            + i18n.tr("Outside this network: a direct encrypted link ({n} connected through it)").format(n=1)
        )
        td_rm.panel.setFixedWidth(420)
        td_rm.panel.adjustSize()
        app.processEvents()
        save_grab(td_rm.panel, "tm-control-panel", "room", width=420)

        save_grab(td_rm.waiting_box, "tm-let-in", "room", width=420)

        # 8. tm-pointer-map (map canvas with pointers and work zones overlay)
        cnv = plug_rm.iface.mapCanvas()
        cnv.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cnv.show()
        cnv.resize(520, 340)
        cnv.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        cnv.setLayers([gpkg_lyr, roads_lyr])
        cnv.setExtent(QgsRectangle(46.670, 24.7105, 46.6805, 24.7175))
        cnv.refresh()
        cnv.waitWhileRendering()
        td_rm.overlay.update(td_rm.node.hub.presence, td_rm.node.zones(), "m1")
        app.processEvents()
        save_grab(cnv, "tm-pointer-map", "room", width=None)
        td_rm.overlay.clear()
        cnv.hide()

        # 9. tm-dock-member + 10. tm-host-lost
        td_rm.node = _NodeStub("member")
        td_rm.tabs.setTabVisible(1, False)
        td_rm.tabs.setCurrentIndex(0)
        td_rm.badge.setText(i18n.tr("online"))
        td_rm.state.setText(i18n.tr("Connected - edits are shared as you work."))
        td_rm.members.clear()
        for mem in td_rm.node.roster:
            it_m = QTreeWidgetItem([
                mem["name"] + (" (" + i18n.tr("you") + ")" if mem["id"] == "m2" else ""),
                i18n.tr("host") if mem["host"] else i18n.tr(mem["state"]),
                str(mem["edits"]),
                "",
            ])
            td_rm.members.addTopLevelItem(it_m)
        save_grab(td_rm.widget(), "tm-dock-member", "room", width=440)

        td_rm.badge.setText(i18n.tr("away"))
        td_rm.state.setText(i18n.tr(
            "The host cannot be reached. Keep working: your edits wait and are sent later. "
            "If the host does not come back, a member takes over."
        ))
        save_grab(td_rm.code.parentWidget(), "tm-host-lost", "room", width=440)

        # 11. tm-network-test + 12. tm-left-out-layers using real QgsMessageBar
        bar_w1 = _QW()
        bl1 = _QVBL(bar_w1)
        bl1.setContentsMargins(6, 6, 6, 6)
        bl1.setSpacing(6)
        mb1a = QgsMessageBar(bar_w1)
        mb1a.setMinimumHeight(52)
        mb1a.pushMessage(
            i18n.tr("Team room"),
            i18n.tr("The room answers on this network ({ms} ms). Members here can find it by the code.").format(ms=12),
            Qgis.MessageLevel.Success, 0,
        )
        mb1b = QgsMessageBar(bar_w1)
        mb1b.pushMessage(
            i18n.tr("Team room"),
            i18n.tr("The room does not answer on this network: Windows Firewall may be blocking Biruni GIS. "
                    "Allow it on private networks."),
            Qgis.MessageLevel.Warning, 0,
        )
        for _tb in mb1b.findChildren(_QTE):
            _tb.setMinimumHeight(46)
        bl1.addWidget(mb1a)
        bl1.addWidget(mb1b)
        save_grab(bar_w1, "tm-network-test", "room", width=880)

        bar_w2 = _QW()
        bl2 = _QVBL(bar_w2)
        bl2.setContentsMargins(6, 6, 6, 6)
        mb2 = QgsMessageBar(bar_w2)
        bl2.addWidget(mb2)
        gone_str = ", ".join([
            f"صور_جوية_القطاع ({i18n.tr('raster')})",
            f"خريطة_الرفع_الكبيرة ({i18n.tr('too large')})",
        ])
        mb2.pushMessage(
            i18n.tr("Team room"),
            i18n.tr("You are in the room. Edit as usual: your work is shared as you go.") + " "
            + i18n.tr("Some layers are files only the host has: {names}").format(names=gone_str),
            Qgis.MessageLevel.Success, 0,
        )
        for _tb in mb2.findChildren(_QTE):
            _tb.setMinimumHeight(52)
        save_grab(bar_w2, "tm-left-out-layers", "room", width=880)
        proj_rm.removeAllMapLayers()
        td_rm.node = None
        td_rm.close()

    if only_part in ("rules", "all"):
        import datetime as _dt
        import secrets as _sec
        import sqlite3 as _sql3
        import threading as _thr
        from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsGeometry,
                               QgsPointXY, QgsRectangle, QgsVectorFileWriter, QgsVectorLayer)
        from qgis.gui import QgsMessageBar
        from qgis.PyQt.QtCore import QMetaType
        from qgis.PyQt.QtWidgets import (QListWidgetItem as _QLWI, QMenu as _QM, QPushButton as _QPB,
                                         QTextEdit as _QTE, QVBoxLayout as _QVBL, QWidget as _QW)
        from biruni.team import area as _tarea, dock as _tdock, rules as _trules, rules_ui as _trui, timemachine as _ttm
        from biruni.team.code import RoomKeys as _RKeys
        from biruni.secure import crypto as _tcrypto

        proj_rl = QgsProject.instance()
        tmp_rl = tempfile.mkdtemp(prefix="biruni-rl-")
        gpath = os.path.join(tmp_rl, "parcels.gpkg")
        mem_par = QgsVectorLayer("Polygon?crs=EPSG:4326", "قطع الأراضي", "memory")
        mem_par.dataProvider().addAttributes([
            QgsField("parcel_no", QMetaType.Type.Int),
            QgsField("owner_name", QMetaType.Type.QString),
            QgsField("area_m2", QMetaType.Type.Double),
            QgsField("status", QMetaType.Type.QString),
        ])
        mem_par.updateFields()
        poly1 = QgsGeometry.fromPolygonXY([[
            QgsPointXY(46.671, 24.711), QgsPointXY(46.674, 24.711),
            QgsPointXY(46.674, 24.714), QgsPointXY(46.671, 24.714), QgsPointXY(46.671, 24.711),
        ]])
        poly2 = QgsGeometry.fromPolygonXY([[
            QgsPointXY(46.675, 24.712), QgsPointXY(46.678, 24.712),
            QgsPointXY(46.678, 24.715), QgsPointXY(46.675, 24.715), QgsPointXY(46.675, 24.712),
        ]])
        ft1 = QgsFeature(mem_par.fields())
        ft1.setGeometry(poly1)
        ft1.setAttributes([1004, "عبدالله السالم", 620.5, "معتمد"])
        ft2 = QgsFeature(mem_par.fields())
        ft2.setGeometry(poly2)
        ft2.setAttributes([1102, "فهد الدوسري", 540.0, "مراجعة"])
        mem_par.dataProvider().addFeatures([ft1, ft2])
        ow = QgsVectorFileWriter.SaveVectorOptions()
        ow.driverName = "GPKG"
        ow.layerName = "parcels"
        QgsVectorFileWriter.writeAsVectorFormatV3(mem_par, gpath, proj_rl.transformContext(), ow)
        par_lyr = QgsVectorLayer(gpath + "|layername=parcels", "قطع الأراضي", "ogr")
        proj_rl.addMapLayer(par_lyr)

        rk_rl = _RKeys("K7QM-2D9X-HR4P-8WZT")
        seed0 = _sec.token_bytes(32)
        pub0 = _tcrypto.public_key(seed0).hex()
        seed0_hex = seed0.hex()
        now_r = time.time()

        class _StoreRules:
            def __init__(self):
                self._lock = _thr.Lock()
                self.con = _sql3.connect(":memory:", check_same_thread=False)
                self.con.execute(
                    "CREATE TABLE ops (seq INTEGER PRIMARY KEY, uid TEXT, epoch INTEGER, author TEXT, name TEXT, "
                    "ts REAL, layer TEXT, fid INTEGER, kind TEXT, state TEXT, prev TEXT, ver INTEGER, sig TEXT, "
                    "note TEXT, src TEXT, lww TEXT)"
                )
                self._d = {"name": "مخطط النخيل", "code": rk_rl.code}
                self._members = [
                    {"id": "m1", "name": "خالد", "host": True, "rank": 0, "state": "online", "pub": pub0},
                    {"id": "m2", "name": "سارة", "host": False, "rank": 1, "state": "online",
                     "rights": {"_area": {"wkt": poly1.asWkt(6), "crs": "EPSG:4326"}}},
                    {"id": "m3", "name": "عمر", "host": False, "rank": 2, "state": "online",
                     "rights": {"_area": {"wkt": poly2.asWkt(6), "crs": "EPSG:4326"}}},
                    {"id": "m4", "name": "طارق", "host": False, "rank": 3, "state": "online"},
                ]
                self._conflicts = [
                    {"layer": "parcels", "fid": 1004, "other": "عمر",
                     "mine": {"a": {"owner_name": "عبدالله السالم"}, "g": bytes(poly1.asWkb()).hex()},
                     "theirs": {"a": {"owner_name": "عبدالله م. السالم"}, "g": bytes(poly1.asWkb()).hex()},
                     "items": [
                         {"what": "field", "field": "owner_name", "mine": "عبدالله السالم", "theirs": "عبدالله م. السالم"},
                         {"what": "geometry", "nodes": [2]},
                     ]},
                    {"layer": "parcels", "fid": 1102, "other": "سارة",
                     "mine": {"a": {"status": "معتمد"}},
                     "theirs": {"a": {"status": "مؤجل"}},
                     "items": [{"what": "field", "field": "status", "mine": "معتمد", "theirs": "مؤجل"}]},
                ]
                s1 = json.dumps({"g": bytes(poly1.asWkb()).hex(), "a": {"parcel_no": 1004, "owner_name": "عبدالله السالم", "status": "جديد"}})
                s2 = json.dumps({"g": bytes(poly1.asWkb()).hex(), "a": {"parcel_no": 1004, "owner_name": "عبدالله السالم", "status": "معتمد"}})
                for row_s in (
                    (1, "u1", 1, "m2", "سارة", now_r - 3600, "parcels", 1004, "put", s1, None, 1, "", "", "{}", "{}"),
                    (2, "u2", 1, "m3", "عمر", now_r - 1800, "parcels", 1004, "put", s2, s1, 2, "", "merged", "{}", "{}"),
                    (3, "u3", 1, "m1", "خالد", now_r - 600, "parcels", 1102, "put", s2, None, 1, "", "", "{}", "{}"),
                ):
                    self.con.execute("INSERT INTO ops VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row_s)
                self.con.commit()

            def get(self, k, default=None):
                return self._d.get(k, default)

            def put(self, k, v):
                self._d[k] = v

            def members(self):
                return list(self._members)

            def open_conflicts(self):
                return list(self._conflicts)

            def recent_log(self, limit=40):
                return [
                    {"seq": 1, "ts": now_r - 3600, "name": "سارة", "layer": "parcels", "kind": "put", "fid": 1004, "note": ""},
                    {"seq": 2, "ts": now_r - 1800, "name": "عمر", "layer": "parcels", "kind": "put", "fid": 1004, "note": "merged"},
                    {"seq": 3, "ts": now_r - 600, "name": "خالد", "layer": "parcels", "kind": "put", "fid": 1102, "note": ""},
                ]

            def feature_history(self, layer_key, fid):
                rows = self._rows(
                    "SELECT seq, uid, epoch, author, name, ts, layer, fid, kind, state, prev, ver, sig, note, src, lww "
                    "FROM ops WHERE layer=? AND fid=? ORDER BY seq", (layer_key, fid)
                )
                return rows

            def _rows(self, sql, params=()):
                cur = self.con.execute(sql, params)
                cols = [d[0] for d in cur.description]
                out = []
                for r in cur.fetchall():
                    d = dict(zip(cols, r))
                    for jf in ("prev", "state", "src", "lww"):
                        if isinstance(d.get(jf), str):
                            d[jf] = json.loads(d[jf])
                    out.append(d)
                return out

        st_rl = _StoreRules()
        st_rl.put(_trules.CKEY, _trules.make_constitution(
            seed0_hex, "m1", {"parcels": {"area_m2": "sum", "status": "authority", "parcel_no": "ask"}}, 1
        ))
        st_rl.put(_trules.GKEY, [
            _trules.make_grant(seed0_hex, "m1", "parcels", "parcel_no", "m2", 1000, 1099),
            _trules.make_grant(seed0_hex, "m1", "parcels", "parcel_no", "m3", 1100, 1199),
        ])
        st_rl.put(_trules.FKEY, {"parcels": {
            "doc": {"kind": "locks", "layer": "parcels", "fields": ["parcel_no", "area_m2"], "by": "m1", "ts": now_r},
            "sig": _tcrypto.sign(bytes.fromhex(seed0_hex), _tcrypto.canonical(
                {"kind": "locks", "layer": "parcels", "fields": ["parcel_no", "area_m2"], "by": "m1", "ts": now_r}
            )).hex(),
        }})

        class _NodeRules:
            me_id = "m1"
            role = "host"
            ident = {"id": "m1", "name": "خالد", "seed": seed0_hex}
            store = st_rl
            layers = {"parcels": par_lyr}
            roster = st_rl._members
            class _HubR:
                store = st_rl
                _settings = (0.0, {})
                def _bump(self):
                    pass
            hub = _HubR()
            def my_rank(self):
                return 0
            def lookup_member(self, mid):
                return next((m for m in st_rl._members if m["id"] == mid), None)

        n_rl = _NodeRules()
        plug_rl = PluginStub()
        td_rl = _tdock.TeamDock(plug_rl)
        td_rl.refresh_theme()
        td_rl._live.stop()
        td_rl._pending_timer.stop()
        td_rl.attach = lambda _n: None
        td_rl.node = n_rl
        td_rl.pages.setCurrentIndex(1)

        # 1. tr-rights-menu, 2. tr-area-menu
        m_rights = _QM()
        for r_key, r_lbl in (("edit", i18n.tr("Can edit")), ("nodelete", i18n.tr("Can edit, not delete")),
                             ("data", i18n.tr("Data entry only")), ("view", i18n.tr("Can only view"))):
            act_r = m_rights.addAction(r_lbl)
            act_r.setCheckable(True)
            act_r.setChecked(r_key == "nodelete")
        m_rights.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        m_rights.show()
        m_rights.adjustSize()
        save_grab(m_rights, "tr-rights-menu", "rules", width=None)
        m_rights.hide()

        m_area = _QM()
        m_area.addAction(i18n.tr("The selected features"))
        act_any = m_area.addAction(i18n.tr("Anywhere"))
        act_any.setCheckable(True)
        act_any.setChecked(False)
        m_area.addSeparator()
        m_area.addAction(i18n.tr("Take back this member's work"))
        m_area.addAction(i18n.tr("Remove from the room"))
        m_area.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        m_area.show()
        m_area.adjustSize()
        save_grab(m_area, "tr-area-menu", "rules", width=None)
        m_area.hide()

        # 3. tr-area-map
        cnv_rl = plug_rl.iface.mapCanvas()
        cnv_rl.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cnv_rl.show()
        cnv_rl.resize(520, 320)
        cnv_rl.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        cnv_rl.setLayers([par_lyr])
        cnv_rl.setExtent(QgsRectangle(46.670, 24.710, 46.679, 24.716))
        cnv_rl.refresh()
        cnv_rl.waitWhileRendering()
        td_rl._show_areas()
        app.processEvents()
        save_grab(cnv_rl, "tr-area-map", "rules", width=None)
        td_rl._clear_areas()
        cnv_rl.hide()

        # 4. tr-locks, 5. tr-merge-rules, 6. tr-number-ranges
        ld_rl = _trui.LocksDialog(n_rl)
        save_grab(ld_rl, "tr-locks", "rules", width=460)
        ld_rl.close()

        rd_rl = _trui.RulesDialog(n_rl)
        rd_rl.table.setColumnWidth(1, 270)
        rd_rl.table.setMinimumHeight(195)
        save_grab(rd_rl, "tr-merge-rules", "rules", width=560)
        rd_rl.close()

        nd_rl = _trui.NumbersDialog(n_rl)
        save_grab(nd_rl, "tr-number-ranges", "rules", width=640)
        nd_rl.close()

        # 7. tr-delete-guard (QgsMessageBar with Allow/Refuse)
        w_dg = _QW()
        l_dg = _QVBL(w_dg)
        l_dg.setContentsMargins(6, 6, 6, 6)
        mb_dg = QgsMessageBar(w_dg)
        it_dg = mb_dg.createMessage(
            i18n.tr("Team room"),
            i18n.tr("{name} is deleting many features at once. Their deletions wait for you.").format(name="طارق"),
        )
        it_dg.layout().addWidget(_QPB(i18n.tr("Allow")))
        it_dg.layout().addWidget(_QPB(i18n.tr("Refuse")))
        mb_dg.pushWidget(it_dg, Qgis.MessageLevel.Warning, 0)
        l_dg.addWidget(mb_dg)
        save_grab(w_dg, "tr-delete-guard", "rules", width=720)

        # 8. tr-conflicts, 9. tr-activity
        td_rl.conflicts.clear()
        for cf in st_rl.open_conflicts():
            p_cf = []
            for it_c in cf["items"]:
                if it_c["what"] == "field":
                    p_cf.append(i18n.tr("{f}: yours «{a}», {o}'s «{b}»").format(
                        f=it_c["field"], a=it_c["mine"], b=it_c["theirs"], o=cf["other"]))
                else:
                    p_cf.append(i18n.tr("the shape: common corner moved two ways ({n} node)").format(n=1))
            td_rl.conflicts.addItem(_QLWI(f"قطع الأراضي · #{cf['fid']}\n" + "\n".join(p_cf)))
        td_rl.tabs.setTabText(2, i18n.tr("Conflicts") + " (2)")
        td_rl.tabs.setCurrentIndex(2)
        save_grab(td_rl.tabs, "tr-conflicts", "rules", width=440)

        td_rl.feed.clear()
        for ev_r in st_rl.recent_log():
            ts_s = _dt.datetime.fromtimestamp(ev_r["ts"]).strftime("%H:%M:%S")
            what_s = i18n.tr("edited a feature in {l}").format(l="قطع الأراضي")
            if ev_r["note"] == "merged":
                what_s += " · " + i18n.tr("joined with another edit")
            td_rl.feed.addItem(f"{ts_s}  {ev_r['name']} — {what_s} (#{ev_r['fid']})")
        td_rl.tabs.setCurrentIndex(3)
        save_grab(td_rl.tabs, "tr-activity", "rules", width=440)

        # 10. tr-history, 11. tr-timemachine
        hd_rl = _tdock.HistoryDialog(plug_rl.iface.mainWindow(), n_rl, "parcels", 1004, cnv_rl)
        save_grab(hd_rl, "tr-history", "rules", width=520)
        hd_rl.done(0)

        tm_rl = _ttm.TimeMachine(plug_rl.iface.mainWindow(), n_rl, plug_rl.iface)
        tm_rl.slider.setValue(500)
        tm_rl.show_moment()
        save_grab(tm_rl, "tr-timemachine", "rules", width=480)
        tm_rl.close()

        # 12. tr-take-back, 13. tr-new-code
        mb_tb = QMessageBox(
            QMessageBox.Icon.Question,
            i18n.tr("Take back this member's work"),
            i18n.tr(
                "Put the {n} features {name} changed back as they were before? What others changed in them "
                "afterwards is kept. Everyone gets the result; each feature can still be brought back from its history."
            ).format(n=6, name="طارق"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            plug_rl.iface.mainWindow(),
        )
        save_grab(mb_tb, "tr-take-back", "rules", width=460)
        mb_nc = QMessageBox(
            QMessageBox.Icon.Information,
            i18n.tr("Change the room code"),
            i18n.tr(
                "The new code is:\n\n{c}\n\nThe room switches to it in 20 seconds. Give it to members who were not connected."
            ).format(c="M9RP-4F2T-QX8L-3VWN"),
            QMessageBox.StandardButton.Ok,
            plug_rl.iface.mainWindow(),
        )
        save_grab(mb_nc, "tr-new-code", "rules", width=460)

        # 14. tr-refused-data
        w_rf = _QW()
        l_rf = _QVBL(w_rf)
        l_rf.setContentsMargins(6, 6, 6, 6)
        l_rf.setSpacing(6)
        for msg_rf in (
            i18n.tr("You only have data-entry rights on this layer; the shape change was not kept."),
            i18n.tr("The field {f} is locked by the room's owner; your change was not kept.").format(f="parcel_no"),
            i18n.tr("That feature is outside your area of work; your change was not kept."),
        ):
            mb_rf = QgsMessageBar(w_rf)
            mb_rf.pushMessage(i18n.tr("Team room"), msg_rf, Qgis.MessageLevel.Warning, 0)
            for _tb in mb_rf.findChildren(_QTE):
                _tb.setMinimumHeight(44)
            l_rf.addWidget(mb_rf)
        save_grab(w_rf, "tr-refused-data", "rules", width=760)

        proj_rl.removeAllMapLayers()
        td_rl.node = None
        td_rl.close()

    # ---------- PART: teamdrive ----------
    if only_part in ("all", "teamdrive"):
        from biruni.team import dock as _tdock, drive_ui as _dui, folder as _tfolder, rules as _trules, session as _tsess
        from biruni.secure import crypto as _tcrypto
        from qgis.core import Qgis, QgsVectorLayer as _QVL, QgsField as _QFL, QgsFeature as _QFT, QgsGeometry as _QGM, QgsPointXY as _QPXY
        from qgis.gui import QgsMessageBar
        from qgis.PyQt.QtCore import QMetaType as _QMT, pyqtSignal as _pqs, QObject as _QO
        from qgis.PyQt.QtWidgets import (QInputDialog as _QID, QMessageBox as _QMB, QTextEdit as _QTE,
                                         QVBoxLayout as _QVBL, QWidget as _QW)

        plug_td = PluginStub()
        td_dir = tempfile.mkdtemp(prefix="lt_td_")
        proj_td = QgsProject.instance()
        proj_td.removeAllMapLayers()
        proj_td.setFileName(os.path.join(td_dir, "east_riyadh.qgz"))

        lyr_td = _QVL("Polygon?crs=EPSG:32638", "قطع الأراضي", "memory")
        lyr_td.dataProvider().addAttributes([
            _QFL("parcel_no", _QMT.Type.Int),
            _QFL("owner_name", _QMT.Type.QString),
        ])
        lyr_td.updateFields()
        f1 = _QFT(lyr_td.fields())
        f1.setAttributes([1001, "عبدالله السالم"])
        f1.setGeometry(_QGM.fromPolygonXY([[_QPXY(670100, 2734100), _QPXY(670160, 2734100), _QPXY(670160, 2734160), _QPXY(670100, 2734160)]]))
        lyr_td.dataProvider().addFeatures([f1])
        proj_td.addMapLayer(lyr_td)

        now_td = time.time()
        seed_h = os.urandom(32)
        pub_h = _tcrypto.public_key(seed_h)
        pub_2 = _tcrypto.public_key(os.urandom(32))
        pub_3 = _tcrypto.public_key(os.urandom(32))
        grant_mem2 = _trules.make_host_grant(seed_h.hex(), "m1", "mem-2", True)
        peer_doc = {"kind": "peer_apply", "on": True, "by": "m1", "ts": now_td}
        peer_signed = {"doc": peer_doc, "sig": _tcrypto.sign(seed_h, _tcrypto.canonical(peer_doc)).hex()}

        class _FakeStore:
            def __init__(self, initial=None):
                self._d = dict(initial or {})
            def get(self, k, default=None):
                return self._d.get(k, default)
            def put(self, k, v):
                self._d[k] = v
            def outbox_count(self):
                return int(self._d.get("_outbox_n", 0))
            def members(self):
                return list(self._d.get("_members", []))
            def is_frozen(self):
                return False
            def counts(self):
                return {"m1": 48, "mem-2": 19, "mem-3": 12}
            def open_conflicts(self):
                return []
            def recent_log(self, limit=40):
                return []
            def head_seq(self):
                return 50

        class _FakeHub:
            def __init__(self, st):
                self.store = st
                self.open_door = True
                self.auto_accept = False
                self.frozen = False
                self.presence = {}
                self.waiting = {}
                self.port = 10118
            def base_left_out(self):
                return list(self.store.get("left_out_layers") or [])
            def numbers(self):
                return {
                    "online": ["m1"],
                    "last_min": 0,
                    "today": 31,
                    "merged": 4,
                    "conflicts": 0,
                    "apply_ms": 11.4,
                    "apply_max": 24.0,
                    "queue": 0,
                    "bytes_in": 18400,
                    "bytes_out": 24600,
                    "requests": 42,
                    "uptime": 3720,
                    "size": 1420000,
                    "epoch": 1,
                    "per_min": [0] * 30,
                    "by": {"m1": 16, "mem-2": 9, "mem-3": 6},
                    "waiting": [],
                    "errors": [],
                }
            def backup(self):
                return os.path.join(td_dir, "backup.zip")

        class _FakeNode(_QO):
            changed = _pqs()
            def __init__(self, role="host", room_name="مسح شرق الرياض"):
                super().__init__()
                self.role = role
                self.me_id = "m1" if role == "host" else "mem-2"
                self.state = "connected"
                self.presence = {}
                self.layers = {"parcels": lyr_td}
                self.code = "H7QK-4N2M-W9XP-6DVT"
                self.roster = [
                    {"id": "m1", "name": "خالد", "rank": 0, "state": "in", "pub": pub_h.hex(), "host": True},
                    {"id": "mem-2", "name": "سارة", "rank": 1, "state": "in", "pub": pub_2.hex(), "host": False},
                    {"id": "mem-3", "name": "عمر", "rank": 1, "state": "in", "pub": pub_3.hex(), "host": False},
                ]
                self.store = _FakeStore({
                    "room_name": room_name,
                    "room_id": "rm-east-riyadh",
                    "room_key": os.urandom(32).hex(),
                    "me": self.me_id,
                    "my_name": "خالد" if role == "host" else "سارة",
                    "_members": self.roster,
                    "left_out_layers": [{"name": "مرئية جوية 2026", "why": "raster"}],
                    _trules.HKEY: [grant_mem2],
                    _trules.PKEY: peer_signed,
                })
                self.hub = _FakeHub(self.store) if role == "host" else None
                self.host_grants_set = {"mem-2"}
            def my_rank(self):
                return 0 if self.role == "host" else 1
            def lookup_member(self, mid):
                return next((m for m in self.roster if m["id"] == mid), None)
            def areas(self):
                return {}
            def zones(self):
                return {}
            def can_make_host(self):
                return self.role != "host" and "mem-2" in self.host_grants_set
            def set_peer_apply(self, on):
                doc = {"kind": "peer_apply", "on": bool(on), "by": "m1", "ts": time.time()}
                self.store.put(_trules.PKEY, {"doc": doc, "sig": _tcrypto.sign(seed_h, _tcrypto.canonical(doc)).hex()})
            def set_host_right(self, mid, allow):
                g = _trules.make_host_grant(seed_h.hex(), "m1", mid, allow)
                self.store.put(_trules.HKEY, [g])
            def stop(self):
                pass
            def close(self):
                pass

        _orig_session_room_code = _tsess.room_code
        _tsess.room_code = lambda node: "H7QK-4N2M-W9XP-6DVT"

        class _FakeCarrier:
            def __init__(self, node, status, rows=()):
                self.node = node
                self.status = status
                self._rows = list(rows)
            def members_view(self):
                return list(self._rows)

        _active_carrier = [None]
        _orig_carrier_for = _tfolder.carrier_for
        _tfolder.carrier_for = lambda node: _active_carrier[0]
        td_dk = None

        try:
            # 1. td-wizard-create
            n_td = _FakeNode(role="host")
            dlg_td = _dui.DriveRoomDialog(n_td, plug_td.iface.mainWindow())
            dlg_td.resize(520, 260)
            save_grab(dlg_td, "td-wizard-create", "teamdrive", width=540)

            # 2. td-wizard-nodrive
            dlg_td.no_drive_label.setVisible(True)
            dlg_td.choose_btn.setVisible(True)
            dlg_td.resize(520, 320)
            save_grab(dlg_td, "td-wizard-nodrive", "teamdrive", width=540)

            # 3. td-wizard-steps
            fake_room_folder = os.path.join(td_dir, "Biruni Team - مسح شرق الرياض")
            os.makedirs(fake_room_folder, exist_ok=True)
            n_td.store.put("folder_carrier", fake_room_folder)
            dlg_td.no_drive_label.setVisible(False)
            dlg_td.choose_btn.setVisible(False)
            dlg_td.refresh()
            dlg_td.resize(540, 360)
            save_grab(dlg_td, "td-wizard-steps", "teamdrive", width=540)
            dlg_td.close()

            # 4. td-join-drive
            jd = _tdock.JoinDialog(plug_td.iface.mainWindow())
            jd.mode_drive.setChecked(True)
            jd.code.setText("H7QK-4N2M-W9XP-6DVT")
            jd.me.setText("سارة")
            jd.folder.setText(r"D:\Biruni-Rooms\East-Riyadh")
            jd.offline_cb.setChecked(True)
            jd.drive_folder.setText(r"G:\My Drive\Biruni Team - مسح شرق الرياض")
            jd.drive_steps.setMinimumHeight(86)
            jd.drive_box.setMinimumHeight(145)
            save_grab(jd, "td-join-drive", "teamdrive", width=560)
            jd.close()

            # 5. td-join-waiting
            w_wait = _QW()
            l_wait = _QVBL(w_wait)
            l_wait.setContentsMargins(6, 6, 6, 6)
            l_wait.setSpacing(6)
            for _msg_w in (
                i18n.tr("Request placed in Google Drive. Waiting for the host to let you in…"),
                i18n.tr("Waiting for the host to let you in: {room}").format(room="مسح شرق الرياض"),
            ):
                mb_w = QgsMessageBar(w_wait)
                mb_w.pushMessage(i18n.tr("Team room"), _msg_w, Qgis.MessageLevel.Info, 0)
                for _tb in mb_w.findChildren(_QTE):
                    _tb.setMinimumHeight(44)
                l_wait.addWidget(mb_w)
            save_grab(w_wait, "td-join-waiting", "teamdrive", width=760)

            # Create TeamDock bound cleanly to fake host node for dock panels & tables
            td_dk = _tdock.TeamDock(plug_td)
            td_dk.refresh_theme()
            td_dk._live.stop()
            td_dk._pending_timer.stop()
            td_dk.attach = lambda _n: None
            td_dk.node = n_td
            td_dk.pages.setCurrentIndex(1)

            _active_carrier[0] = _FakeCarrier(
                n_td,
                {"state": "ok", "last": now_td - 40, "devices": 2, "streamed": False},
                rows=[
                    {"name": "سارة", "seen": 50, "head": 50, "last": now_td - 480},
                    {"name": "عمر", "seen": 42, "head": 50, "last": now_td - 5400},
                ],
            )
            td_dk.refresh()
            td_dk.refresh_folder()

            # 6. td-status-synced
            save_grab(td_dk.folder_state, "td-status-synced", "teamdrive", width=540)

            # 7. td-behind-table
            td_dk.folder_table.setMinimumHeight(110)
            for _ci in range(3):
                td_dk.folder_table.resizeColumnToContents(_ci)
            save_grab(td_dk.folder_table, "td-behind-table", "teamdrive", width=560)

            # 8. td-warn-streamed
            n_td_stream = _FakeNode(role="member")
            n_td_stream.store.put("folder_carrier", r"G:\My Drive\Biruni Team - مسح شرق الرياض")
            _active_carrier[0] = _FakeCarrier(
                n_td_stream,
                {"state": "ok", "streamed": True, "placeholders": 3, "last": now_td - 20},
            )
            dlg_warn = _dui.DriveRoomDialog(n_td_stream, plug_td.iface.mainWindow())
            dlg_warn.resize(540, 340)
            save_grab(dlg_warn, "td-warn-streamed", "teamdrive", width=540)
            dlg_warn.close()

            # 9. td-folder-gone
            w_gone = _QW()
            l_gone = _QVBL(w_gone)
            l_gone.setContentsMargins(6, 6, 6, 6)
            mb_gone = QgsMessageBar(w_gone)
            mb_gone.pushMessage(
                i18n.tr("Team room"),
                i18n.tr("The room's folder is gone from this computer - nothing is sent. Ask the host."),
                Qgis.MessageLevel.Warning,
                0,
            )
            for _tb in mb_gone.findChildren(_QTE):
                _tb.setMinimumHeight(44)
            l_gone.addWidget(mb_gone)
            save_grab(w_gone, "td-folder-gone", "teamdrive", width=760)

            # 10. td-not-running
            w_nrun = _QW()
            l_nrun = _QVBL(w_nrun)
            l_nrun.setContentsMargins(6, 6, 6, 6)
            mb_nrun = QgsMessageBar(w_nrun)
            mb_nrun.pushMessage(
                i18n.tr("Team room"),
                i18n.tr("Google Drive for desktop is not running on this computer."),
                Qgis.MessageLevel.Warning,
                0,
            )
            for _tb in mb_nrun.findChildren(_QTE):
                _tb.setMinimumHeight(44)
            l_nrun.addWidget(mb_nrun)
            save_grab(w_nrun, "td-not-running", "teamdrive", width=760)

            # 11. td-host-right
            td_dk.tabs.setCurrentIndex(0)
            td_dk.members.setMinimumHeight(140)
            save_grab(td_dk.members, "td-host-right", "teamdrive", width=560)

            # 12. td-make-host-confirm
            mb_mh = _QMB(
                _QMB.Icon.Question,
                i18n.tr("Make This Computer the Host"),
                i18n.tr("Use this only when the host's computer is gone. Changes only that computer had are lost."),
                _QMB.StandardButton.Yes | _QMB.StandardButton.No,
                plug_td.iface.mainWindow(),
            )
            save_grab(mb_mh, "td-make-host-confirm", "teamdrive", width=480)

            # 13. td-peer-apply
            _active_carrier[0] = _FakeCarrier(
                n_td,
                {"state": "ok", "last": now_td - 12, "devices": 2, "peer_applied": 14},
                rows=[{"name": "سارة", "seen": 50, "head": 50, "last": now_td - 120}],
            )
            td_dk.refresh()
            td_dk.refresh_folder()
            w_pa = _QW()
            l_pa = _QVBL(w_pa)
            l_pa.setContentsMargins(10, 10, 10, 10)
            l_pa.setSpacing(8)
            from qgis.PyQt.QtWidgets import QLabel as _QL
            l_pa.addWidget(_QL("<b>" + i18n.tr("Control panel") + "</b>"))
            l_pa.addWidget(td_dk.peer_apply)
            l_pa.addWidget(td_dk.folder_state)
            save_grab(w_pa, "td-peer-apply", "teamdrive", width=560)

            # 14. td-clock-ahead
            w_clk = _QW()
            l_clk = _QVBL(w_clk)
            l_clk.setContentsMargins(6, 6, 6, 6)
            l_clk.setSpacing(6)
            for _msg_c in (
                i18n.tr("This computer's clock is ahead. Fix the date and time; your edits wait until then."),
                i18n.tr("{0} waiting: a teammate's clock is ahead").format(2),
            ):
                mb_c = QgsMessageBar(w_clk)
                mb_c.pushMessage(i18n.tr("Team room"), _msg_c, Qgis.MessageLevel.Warning, 0)
                for _tb in mb_c.findChildren(_QTE):
                    _tb.setMinimumHeight(44)
                l_clk.addWidget(mb_c)
            save_grab(w_clk, "td-clock-ahead", "teamdrive", width=760)

            # 15. td-enter-new-code
            id_nc = _QID(plug_td.iface.mainWindow())
            id_nc.setWindowTitle(i18n.tr("Enter the Room's New Code"))
            id_nc.setLabelText(i18n.tr("New room code"))
            id_nc.setTextValue("R7WK-9C3N-LM4P-2VXT")
            save_grab(id_nc, "td-enter-new-code", "teamdrive", width=440)
        finally:
            _tsess.room_code = _orig_session_room_code
            _tfolder.carrier_for = _orig_carrier_for
            proj_td.removeAllMapLayers()
            if td_dk is not None:
                td_dk.node = None
                td_dk.close()

    # ---------- PART: offline ----------
    if only_part in ("all", "offline"):
        from biruni.pigeon import core as _pcore, ui as _pui
        from biruni.lightsync import ui as _lui
        from biruni.team_compute import ui as _tcui, worker as _tcw
        from biruni.vault import dock as _vdock
        from qgis.core import QgsVectorLayer as _QVL, QgsVectorFileWriter as _QVFW, QgsField as _QFL, QgsFeature as _QFT, QgsGeometry as _QGM, QgsPointXY as _QPXY
        from qgis.PyQt.QtCore import QMetaType as _QMT

        plug_off = PluginStub()
        proj_off = QgsProject.instance()
        proj_off.removeAllMapLayers()

        class _NodeOff:
            role = "member"
            me_id = "mem-off-1"
            code = "K4PT-8M2N-W7QX-3RZV"

        _orig_pcounts = _pcore.counts
        _pcore.counts = lambda _n: {"role": "member", "outbox": 7, "carried": 3, "waiting": 1}
        try:
            # 1. off-pigeon
            pd = _pui.PigeonDialog(plug_off, _NodeOff())
            save_grab(pd, "off-pigeon", "offline", width=600)

            # 2. off-pigeon-result
            rep_sample = {
                "packs": 4,
                "in": 0,
                "applied": 18,
                "carried": 2,
                "waiting": 0,
                "conflicts": 1,
                "dups": 1,
                "refused": ["expired"],
            }
            pd.result.setText("\n".join(_pui.report_lines(rep_sample)))
            save_grab(pd, "off-pigeon-result", "offline", width=600)
            pd.close()
        finally:
            _pcore.counts = _orig_pcounts

        # 3. off-light-send
        sample_bytes = os.urandom(48 * 1024)
        sd_ls = _lui.SendDialog(plug_off.iface.mainWindow(), sample_bytes, title=i18n.tr("Sending «{name}» ({size})").format(name="east_riyadh.bpigeon", size="48.0 كيلوبايت"))
        sd_ls.timer.stop()
        sd_ls.tick()
        sd_ls.resize(560, 620)
        save_grab(sd_ls, "off-light-send", "offline", width=560)
        qr_pm = sd_ls.code.pixmap()
        sd_ls.close()

        # 4. off-light-receive
        rd_lr = _lui.ReceiveDialog(plug_off.iface.mainWindow(), camera=False)
        rd_lr.cams.setEnabled(True)
        rd_lr.cams.addItem("Integrated Camera (1920x1080)")
        if qr_pm is not None and not qr_pm.isNull():
            rd_lr.preview.setPixmap(qr_pm.scaled(260, 260, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        rd_lr.bar.setValue(780)
        rd_lr.state.setText(i18n.tr("{known} of {k} blocks · {n} frames read ({rate:.1f} a second)").format(known=39, k=50, n=44, rate=9.6))
        rd_lr.resize(560, 560)
        save_grab(rd_lr, "off-light-receive", "offline", width=560)
        rd_lr.close()

        # 5. off-vault
        vdir = tempfile.mkdtemp(prefix="lt_vault_")
        gpkg_v = os.path.join(vdir, "parcels.gpkg")
        mem_v = _QVL("Polygon?crs=EPSG:32638", "قطع الأراضي", "memory")
        mem_v.dataProvider().addAttributes([_QFL("parcel_no", _QMT.Type.Int)])
        mem_v.updateFields()
        fv = _QFT(mem_v.fields())
        fv.setAttributes([1001])
        fv.setGeometry(_QGM.fromPolygonXY([[_QPXY(670100, 2734100), _QPXY(670150, 2734100), _QPXY(670150, 2734150), _QPXY(670100, 2734150)]]))
        mem_v.dataProvider().addFeatures([fv])
        ow_v = _QVFW.SaveVectorOptions()
        ow_v.driverName = "GPKG"
        ow_v.layerName = "parcels"
        _QVFW.writeAsVectorFormatV3(mem_v, gpkg_v, proj_off.transformContext(), ow_v)
        lyr_v = _QVL(gpkg_v + "|layername=parcels", "قطع الأراضي", "ogr")
        proj_off.setFileName(os.path.join(vdir, "east_riyadh_survey.qgz"))
        proj_off.addMapLayer(lyr_v)
        vd = _vdock.VaultDock(plug_off)
        vd._files = []
        vd.rebuild()
        vd._tick.stop()
        vd._later_rescan.stop()
        vd._later_rebuild.stop()
        vd.resize(440, 520)
        save_grab(vd, "off-vault", "offline", width=440)
        vd.worker.stop()
        vd.close()
        vd.deleteLater()
        app.processEvents()
        proj_off.removeAllMapLayers()

        # 6. off-compute-share
        _orig_rid = _tcw.room_id
        _orig_lsh = _tcw.load_share
        _orig_shg = _tcw.sharing
        class _FakeWorker:
            done = 28
            failed = 0
        _tcw.room_id = lambda _n: "rm-off-compute"
        _tcw.load_share = lambda _r: (True, "half")
        _tcw.sharing = lambda _n: _FakeWorker()
        try:
            cs_dlg = _tcui.ShareDialog(plug_off, _NodeOff())
            save_grab(cs_dlg, "off-compute-share", "offline", width=480)
            cs_dlg.close()
        finally:
            _tcw.room_id = _orig_rid
            _tcw.load_share = _orig_lsh
            _tcw.sharing = _orig_shg

    _append_missing_ar(missing_lines)
    _update_shots_json(captured, None if only_part == "probe" else only_part)
    app.exitQgis()


# ---------------------------------------------------------------------------
# Phone worker (QApplication + QWebEngineView)
# ---------------------------------------------------------------------------
def _run_phone(only_part, platform):
    os.environ["QT_QPA_PLATFORM"] = platform
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

    sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
    sys.path.insert(0, os.path.join(ROOT, "app", "python"))

    from PyQt6.QtCore import QCoreApplication, Qt, QUrl
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                       QWebEngineUrlRequestInterceptor)
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    app = QApplication(sys.argv[:1])
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    if hasattr(app, "styleHints") and hasattr(app.styleHints(), "setColorScheme"):
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)

    class LocalOnlyInterceptor(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            host = info.requestUrl().host()
            if host and host not in ("127.0.0.1", "localhost"):
                info.block(True)

    profile = QWebEngineProfile("linkteam_guide_shots")
    interceptor = LocalOnlyInterceptor()
    profile.setUrlRequestInterceptor(interceptor)

    view = QWebEngineView()
    page = QWebEnginePage(profile, view)
    view.setPage(page)
    view.setFixedSize(390, 844)
    view.setZoomFactor(1.0)
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    view.show()

    import time

    def wait_js(expr, timeout=5.0):
        res = [None]
        done = [False]
        def cb(val):
            res[0] = val
            done[0] = True
        page.runJavaScript(expr, cb)
        end = time.monotonic() + timeout
        while not done[0] and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        return res[0]

    def load_and_wait(url, ready_js="document.readyState === 'complete'", timeout=8.0):
        loaded = [False]
        page.loadFinished.connect(lambda _ok: loaded.__setitem__(0, True))
        view.load(QUrl(url))
        end = time.monotonic() + timeout
        while not loaded[0] and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        end2 = time.monotonic() + 3.0
        while time.monotonic() < end2:
            app.processEvents()
            if wait_js(ready_js):
                break
            time.sleep(0.05)
        for _ in range(15):
            app.processEvents()
            time.sleep(0.02)

    def grab_phone(name, part):
        time.sleep(0.32)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.02)
        pm = view.grab()
        out_path = os.path.join(OUT_DIR, f"{name}.png")
        pm.save(out_path)
        sz = os.path.getsize(out_path)
        print(f"SHOT phone {part} {name}.png {sz}B", flush=True)
        return {"part": part, "size": sz}

    captured = {}
    if only_part in ("probe", "hub", "all"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from biruni.phone_hub import server as hs

        texts = hs.all_phone_texts()
        html_bytes = hs.render_page(texts, "ar")
        state_obj = {
            "v": 2,
            "features": [
                {"id": "gps", "on": True, "link": {"port": 10114, "path": "/g/"}},
                {"id": "photos", "on": True, "link": {"port": 10112, "path": "/p/"}},
                {"id": "map", "on": False, "link": None},
            ],
        }

        class HubStub(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                if self.path.endswith("/state"):
                    body = json.dumps(state_obj).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html_bytes)))
                    self.end_headers()
                    self.wfile.write(html_bytes)

        port = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), HubStub)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            load_and_wait(f"http://127.0.0.1:{port}/", "!!document.querySelector('.card')")
            captured["ph-hub"] = grab_phone("ph-hub", "hub" if only_part != "probe" else "probe")
            if only_part in ("hub", "all"):
                state_obj["v"] = 3
                state_obj["features"] = [
                    {"id": "gps", "on": False, "link": None},
                    {"id": "photos", "on": True, "link": None},
                    {"id": "map", "on": False, "link": None},
                ]
                wait_js("STATE = {v:3, features:[{id:'gps',on:false,link:null},{id:'photos',on:true,link:null},{id:'map',on:false,link:null}]}; OK = false; draw(); chip(); true;")
                captured["ph-hub-ask"] = grab_phone("ph-hub-ask", "hub")
        finally:
            httpd.shutdown()
            httpd.server_close()

    if only_part in ("photos", "all"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from biruni.field_photos import bridge as _fp_bridge, room_page, server as fs

        texts_all = fs.all_phone_texts()
        example_form = [
            {"name": "q1", "label": "رقم العمود", "type": "text", "req": True},
            {"name": "q2", "label": "الحالة", "type": "choice", "req": True, "choices": ["سليم", "محتاج صيانة", "مكسور"]},
            {"name": "q3", "label": "المشاكل", "type": "multi", "choices": ["صدأ", "مايل", "الكشاف مطفي"]},
            {"name": "q4", "label": "الارتفاع (م)", "type": "number", "min": 0, "max": 40},
            {"name": "q5", "label": "هل الوصول إليه آمن؟", "type": "yesno"},
        ]
        raw_page = _fp_bridge.phone_page(texts_all)
        raw_page = raw_page.replace("KINDS=[]", "KINDS=['عمود','لوحة','كسر','تفتيش']", 1)
        raw_page = raw_page.replace("FORM=[]", "FORM=" + json.dumps(example_form, ensure_ascii=False), 1)
        page_bytes = _fp_bridge.with_csp(raw_page).encode("utf-8")
        room_bytes = room_page.room_page().encode("utf-8")
        rep_path = os.path.join(SCRATCH_DIR, "scratch", "daily_report.html")
        rep_bytes = open(rep_path, "rb").read() if os.path.exists(rep_path) else b"<html><body>Report</body></html>"

        class PhotosStub(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/r" or p.startswith("/r/"):
                    body = room_bytes
                elif p.startswith("/report"):
                    body = rep_bytes
                else:
                    body = page_bytes
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        port = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), PhotosStub)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            # Render daily report wide first
            view.setFixedSize(720, 560)
            load_and_wait(f"http://127.0.0.1:{port}/report", "!!document.querySelector('table')")
            captured["fp-daily-report"] = grab_phone("fp-daily-report", "photos")

            # Now switch to phone size 390x844
            view.setFixedSize(390, 844)
            base_url = f"http://127.0.0.1:{port}/p/#k=ExampleBridgeKey000000000000000000000000000&i=ExamplePin000000000000&l=ar"
            load_and_wait(base_url, "!!document.getElementById('go') && !!T.hero")
            captured["ph-photo-start"] = grab_phone("ph-photo-start", "photos")

            # Enter work mode
            wait_js("""
                $('intro').classList.add('hide');
                $('work').classList.remove('hide');
                $('bar').classList.remove('hide');
                started = true;
                fix = {lat: 24.713642, lon: 46.675318, acc: 4.2, alt: 612, heading: 45, t: Date.now()};
                compass = 48;
                $('pos').textContent = '24.713642, 46.675318';
                $('acc').textContent = '± 4 m';
                $('accBar').style.width = '92%';
                $('deg').textContent = '48°';
                $('arrow').style.transform = 'rotate(48deg)';
                $('shot').disabled = false;
                $('shotHint').textContent = T.shoot;
                status(true, 'connected');
                $('me').value = '';
                $('meCard').scrollIntoView({block:'center'});
                true;
            """)
            captured["ph-name"] = grab_phone("ph-name", "photos")

            # ph-shoot: name filled, kind selected, recent photos, stats
            wait_js("""
                $('me').value = 'سارة';
                kind = 'عمود';
                drawKinds();
                $('note').value = 'عمود إنارة شمال التقاطع';
                sent = 4;
                $('nSent').textContent = '4';
                $('nWait').textContent = '0';
                $('list').innerHTML = '';
                for (let i = 1; i <= 3; i++) {
                    const c = document.createElement('div');
                    c.className = 'thumb';
                    c.innerHTML = '<div style="width:100%;height:100%;background:linear-gradient(135deg,#ccebe6,#92d5cb);display:grid;place-items:center;color:#0b1f2a;font-size:11px;font-weight:700">#' + i + '</div><span class="badge ok">✓</span>';
                    $('list').appendChild(c);
                }
                window.scrollTo(0, 0);
                true;
            """)
            captured["ph-shoot"] = grab_phone("ph-shoot", "photos")

            # ph-kinds-form
            wait_js("""
                vals = {q1: 'P-408', q2: 'محتاج صيانة', q3: ['صدأ', 'مايل'], q4: '12.5', q5: true};
                drawForm();
                document.querySelector('#form').scrollIntoView({block:'center'});
                true;
            """)
            captured["ph-kinds-form"] = grab_phone("ph-kinds-form", "photos")

            # ph-avg
            wait_js("""
                window.scrollTo(0, 0);
                $('avgBar').classList.remove('hide');
                $('avgFill').style.width = '100%';
                $('avgBtn').textContent = T.avg;
                $('avgInfo').textContent = T.averaged.replace('{n}', 14).replace('{s}', '1.8');
                $('avgBtn').scrollIntoView({block:'center'});
                true;
            """)
            captured["ph-avg"] = grab_phone("ph-avg", "photos")

            # ph-guide
            wait_js("""
                $('avgBar').classList.add('hide');
                $('avgInfo').textContent = '';
                const gItems = [
                    {k:'poly', n:'قطعة 101', c:[[[46.6750,24.7133],[46.6757,24.7133],[46.6757,24.7139],[46.6750,24.7139],[46.6750,24.7133]]]},
                    {k:'pt', n:'نقطة A', c:[46.6759,24.7141]}
                ];
                setGuide({v: 2, items: gItems});
                $('gtext').textContent = T.target.replace('{d}', '28 m').replace('{name}', 'قطعة 101');
                $('guideCard').scrollIntoView({block:'start'});
                true;
            """)
            captured["ph-guide"] = grab_phone("ph-guide", "photos")

            # ph-room-scan (indoor room sheet open)
            wait_js("""
                $('guideCard').classList.add('hide');
                INDOOR = {
                    levels: [{id:1, name:'الأرضي'}, {id:2, name:'الثاني'}],
                    units: [
                        {id:'U-101', lv:1, p:[46.6752,24.7134], r:5, n:'الاستقبال', num:'101', c:[[[46.6751,24.7133],[46.6753,24.7133],[46.6753,24.7135],[46.6751,24.7135],[46.6751,24.7133]]]},
                        {id:'U-102', lv:1, p:[46.6755,24.7134], r:5, n:'مكتب المساحة', num:'102', c:[[[46.6754,24.7133],[46.6756,24.7133],[46.6756,24.7135],[46.6754,24.7135],[46.6754,24.7133]]]},
                        {id:'U-201', lv:2, p:[46.6752,24.7134], r:5, n:'الأرشيف', num:'201', c:[[[46.6751,24.7133],[46.6753,24.7133],[46.6753,24.7135],[46.6751,24.7135],[46.6751,24.7133]]]},
                        {id:'U-202', lv:2, p:[46.6755,24.7134], r:5, n:'غرفة الخرائط', num:'202', c:[[[46.6754,24.7133],[46.6756,24.7133],[46.6756,24.7135],[46.6754,24.7135],[46.6754,24.7133]]]}
                    ]
                };
                $('roomRow').classList.remove('hide');
                $('scanSheet').classList.remove('hide');
                $('scanBox').classList.add('hide');
                $('lPick').textContent = T.pickRoom;
                $('roomFind').placeholder = T.findRoom;
                const rl = $('roomList'); rl.textContent = '';
                INDOOR.units.forEach(u => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.innerHTML = '<b>' + u.num + '</b><span>' + u.n + ' · ' + T.level.replace('{n}', u.lv) + '</span>';
                    rl.appendChild(b);
                });
                true;
            """)
            captured["ph-room-scan"] = grab_phone("ph-room-scan", "photos")

            # ph-relay-mode, ph-via, ph-incognito, ph-blocked, ph-pcoff
            wait_js("""
                $('scanSheet').classList.add('hide');
                $('roomRow').classList.add('hide');
                window.scrollTo(0, 0);
                status(false, 'relayMode');
                $('nWait').textContent = '2';
                $('lWait').textContent = T.previewSent.replace('{n}', 2);
                $('waitBox').classList.add('warn');
                $('waitPill').textContent = T.previewSent.replace('{n}', 2);
                $('waitPill').classList.remove('hide');
                true;
            """)
            captured["ph-relay-mode"] = grab_phone("ph-relay-mode", "photos")

            wait_js("""
                status(true, 'connected');
                $('waitPill').classList.add('hide');
                $('nWait').textContent = '0';
                $('lWait').textContent = bare(T.queued);
                $('waitBox').classList.remove('warn');
                toast(T.via);
                true;
            """)
            captured["ph-via"] = grab_phone("ph-via", "photos")

            wait_js("""
                $('toast').classList.remove('show');
                setPrivate(true);
                true;
            """)
            captured["ph-incognito"] = grab_phone("ph-incognito", "photos")

            wait_js("""
                setPrivate(false);
                status(false, 'blocked');
                true;
            """)
            captured["ph-blocked"] = grab_phone("ph-blocked", "photos")

            wait_js("""
                status(false, 'pcoff');
                $('nWait').textContent = '3';
                $('lWait').textContent = T.waitingPhotos.replace('{n}', 3);
                $('waitBox').classList.add('warn');
                true;
            """)
            captured["ph-pcoff"] = grab_phone("ph-pcoff", "photos")

            # ph-hand-give & ph-hand-take
            wait_js("""
                $('handSheet').classList.remove('hide');
                $('handStep').textContent = T.handGive1;
                const hc = $('handQr'); hc.classList.remove('hide');
                hc.width = 200; hc.height = 200;
                const g = hc.getContext('2d');
                g.fillStyle = '#ffffff'; g.fillRect(0, 0, 200, 200);
                g.fillStyle = '#111111';
                for (let r = 0; r < 15; r++) for (let c = 0; c < 15; c++) {
                    if ((r < 4 && c < 4) || (r < 4 && c > 10) || (r > 10 && c < 4) || ((r * 7 + c * 13) % 3 === 0))
                        g.fillRect(15 + c * 11, 15 + r * 11, 10, 10);
                }
                true;
            """)
            captured["ph-hand-give"] = grab_phone("ph-hand-give", "photos")

            wait_js("""
                $('handQr').classList.add('hide');
                $('handStep').textContent = T.handTook.replace('{n}', 3);
                true;
            """)
            captured["ph-hand-take"] = grab_phone("ph-hand-take", "photos")

            # ph-room-noted (/r/ page)
            load_and_wait(f"http://127.0.0.1:{port}/r/#u=U-102&n=%D9%85%D9%83%D8%AA%D8%A8%20%D8%A7%D9%84%D9%85%D8%B3%D8%A7%D8%AD%D8%A9%20102&l=1",
                          "!!document.getElementById('t').textContent")
            wait_js("localStorage.setItem('biruni-lang', 'ar'); document.documentElement.dir='rtl'; document.documentElement.lang='ar'; "
                    "const T2=ALL.ar; document.getElementById('l').textContent=T2.level.replace('{n}',1); document.getElementById('m').textContent=T2.noted; true;")
            captured["ph-room-noted"] = grab_phone("ph-room-noted", "photos")
        finally:
            httpd.shutdown()
            httpd.server_close()

    if only_part in ("drive", "all"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from biruni.field_photos import bridge as _fp_bridge, drive_page, server as fs

        port = free_port()
        helper_url = f"http://127.0.0.1:{port}"
        os.environ["BIRUNI_DRIVE_HELPER"] = helper_url
        os.environ["BIRUNI_DRIVE_PARENT"] = helper_url
        texts_all = fs.all_phone_texts()
        raw_page = _fp_bridge.phone_page(texts_all)
        raw_page = raw_page.replace(drive_page.HELPER, helper_url)
        page_bytes = raw_page.encode("utf-8")
        helper_bytes = (
            '<!doctype html><html dir="rtl"><body><button style="width:100%;min-height:42px;border-radius:8px;'
            'background:#0f766e;color:#fff;border:0;font:600 15px system-ui,Segoe UI,Tahoma">ربط Google Drive'
            '</button></body></html>'
        ).encode("utf-8")

        class DriveStub(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                p = self.path.split("?")[0]
                body = helper_bytes if p.startswith("/link") else page_bytes
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", port), DriveStub)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            view.setFixedSize(390, 844)
            fake_pk = "BE" + ("A" * 85)
            u = (f"{helper_url}/p/#k=ExampleBridgeKey000000000000000000000000000"
                 f"&i=ExamplePin000000000000&d=1&pk={fake_pk}&l=ar")
            load_and_wait(u, "!!document.getElementById('driveCard') && !!T.driveTitle")
            wait_js("""
                $('intro').classList.add('hide');
                $('work').classList.remove('hide');
                $('bar').classList.remove('hide');
                started = true;
                fix = {lat: 24.713642, lon: 46.675318, acc: 4.0, t: Date.now()};
                $('pos').textContent = '24.713642, 46.675318';
                $('acc').textContent = '± 4 m';
                $('accBar').style.width = '92%';
                $('me').value = 'سارة';
                DRV.tag = 'a1b2c3d4e5f60718';
                status(true, 'connected');
                window.scrollTo(0, 0);
                true;
            """)
            captured["pd-unlinked"] = grab_phone("pd-unlinked", "drive")

            states = [
                ("pd-pick", "DRV.tok='tok'; DRV.exp=Date.now()+3600000; DRV.fold=null; DRV.n=0; "
                            "if(DRV.frame) DRV.frame.style.display='none'; driveSay('pick'); true;"),
                ("pd-idle", "DRV.fold={t:DRV.tag,r:'f1',k:1,own:false}; DRV.n=5; driveSay('idle'); true;"),
                ("pd-up", "DRV.n=5; driveSay('up'); true;"),
                ("pd-own", "DRV.fold={t:DRV.tag,r:'f1',k:1,own:true}; DRV.n=2; driveSay('idle'); true;"),
                ("pd-ro", "DRV.fold={t:DRV.tag,r:'f1',k:1,own:false}; driveSay('ro'); true;"),
                ("pd-gone", "DRV.fold=null; driveSay('gone'); true;"),
                ("pd-full", "DRV.fold={t:DRV.tag,r:'f1',k:1,own:false}; driveSay('full'); true;"),
                ("pd-auth", "DRV.tok=''; DRV.exp=0; DRV.w=3; driveSay('auth'); "
                            "document.querySelector('.dpop').classList.remove('hide'); true;"),
                ("pd-inapp", "DRV.tok=''; DRV.exp=0; document.querySelector('.dpop').classList.add('hide'); "
                             "driveSay('inapp'); true;"),
                ("pd-blocked", "driveSay('blocked'); true;"),
                ("pd-free", "dshowFree = async () => {}; DRV.tok='tok'; DRV.exp=Date.now()+3600000; "
                            "DRV.fold={t:DRV.tag,r:'f1',k:1,own:false}; DRV.clean='manual'; DRV.n=4; "
                            "driveSay('idle'); status(true, 'connected'); "
                            "const fb=document.querySelector('.dfree'); fb.textContent=T.driveFree.replace('{n}',4); "
                            "fb.classList.remove('hide'); true;"),
            ]
            for sname, js_code in states:
                wait_js(js_code)
                captured[sname] = grab_phone(sname, "drive")
        finally:
            httpd.shutdown()
            httpd.server_close()

    if only_part in ("gps", "all"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from biruni.field_photos import bridge as _fp_bridge, drive_page, server as fs

        port = free_port()
        helper_url = f"http://127.0.0.1:{port}"
        texts_all = fs.all_phone_texts()
        raw_page = _fp_bridge.phone_page(texts_all)
        raw_page = raw_page.replace(drive_page.HELPER, helper_url)
        page_bytes = raw_page.encode("utf-8")

        class GpsStub(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)

        httpd = ThreadingHTTPServer(("127.0.0.1", port), GpsStub)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            view.setFixedSize(390, 844)
            u = (f"{helper_url}/p/#k=ExampleGpsKey00000000000000000000000000000"
                 "&i=ExamplePin000000000000&m=gps&g=1&l=ar")
            load_and_wait(u, "!!document.getElementById('go') && !!T.hero")
            captured["pg-start"] = grab_phone("pg-start", "gps")

            wait_js("""
                $('intro').classList.add('hide');
                $('work').classList.remove('hide');
                $('bar').classList.remove('hide');
                started = true;
                fix = {lat: 24.713642, lon: 46.675318, acc: 4.0, alt: 612, speed: 1.5, heading: 62, t: Date.now()};
                $('pos').textContent = '24.713642, 46.675318';
                $('acc').textContent = '± 4 m';
                $('accBar').style.width = '92%';
                $('deg').textContent = '62°';
                $('arrow').style.transform = 'rotate(62deg)';
                $('nSent').textContent = '34';
                status(true, 'connected');
                if (typeof DRV === 'object' && DRV.card) {
                    DRV.tok = 'tok'; DRV.exp = Date.now() + 3600000; DRV.tag = 'a1b2c3d4e5f60718';
                    DRV.fold = {t: DRV.tag, r: 'f1', k: 1, own: false};
                    if (DRV.frame) DRV.frame.style.display = 'none';
                    driveSay('idle');
                }
                window.scrollTo(0, 0);
                true;
            """)
            captured["pg-sending"] = grab_phone("pg-sending", "gps")

            wait_js("""
                status(false, 'lost');
                $('waitPill').textContent = T.ladKeep.replace('{n}', 12);
                $('waitPill').classList.remove('hide');
                true;
            """)
            captured["pg-kept"] = grab_phone("pg-kept", "gps")

            wait_js("""
                status(true, 'connected');
                $('waitPill').textContent = T.ladCatch.replace('{n}', 12);
                $('waitPill').classList.remove('hide');
                true;
            """)
            captured["pg-catch"] = grab_phone("pg-catch", "gps")

            wait_js("""
                $('waitPill').classList.add('hide');
                const sb = $('st');
                sb.textContent = T.ladVia.replace('{name}', T.chOrg || 'مرحل مؤسستك');
                sb.classList.remove('hide');
                chip('off');
                true;
            """)
            captured["pg-via"] = grab_phone("pg-via", "gps")
        finally:
            httpd.shutdown()
            httpd.server_close()

    if only_part in ("show", "all"):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from biruni.dashboard import page as _dash_page
        from biruni.live_share import viewer as _ls_view
        from biruni.living_paper import codec as _lp_codec, page as _lp_page
        from biruni.seal import core as _seal_core, page as _seal_page

        ls_html = _ls_view.public_page().encode("utf-8")
        ds_html = _dash_page.page_html("ar", "preview").encode("utf-8")
        lp_html = _lp_page.public_page().encode("utf-8")
        sl_html = _seal_page.public_page().encode("utf-8")

        class ShowStub(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                p = self.path.split("?", 1)[0]
                if p.startswith("/s"):
                    body = ls_html
                elif p.startswith("/d"):
                    body = ds_html
                elif p.startswith("/m"):
                    body = lp_html
                elif p.startswith("/v"):
                    body = sl_html
                else:
                    body = b""
                if not body:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        port = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), ShowStub)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            view.setFixedSize(390, 844)
            u_s = f"http://127.0.0.1:{port}/s/#k=ExampleLiveShareKey0000000000000000000000&l=ar&p=2"
            load_and_wait(u_s, "typeof onInfo === 'function' && !!T.title")
            wait_js("""
                (async () => {
                    onInfo({
                        title: 'شبكة أعمدة حي النخيل',
                        bg: '#edf2ee',
                        extent: [5194000, 2839000, 5197500, 2843000],
                        layers: ['أعمدة الإنارة', 'الشوارع الرئيسية', 'حدود الأحياء'],
                        attrs: true
                    });
                    const cv = document.createElement('canvas');
                    cv.width = 390; cv.height = 844;
                    const g = cv.getContext('2d');
                    g.fillStyle = '#eef2ee'; g.fillRect(0, 0, 390, 844);
                    g.strokeStyle = '#cbd5d0'; g.lineWidth = 10;
                    g.beginPath(); g.moveTo(40, 140); g.lineTo(350, 260); g.lineTo(310, 700); g.stroke();
                    g.strokeStyle = '#d8dfd9'; g.lineWidth = 7;
                    g.beginPath(); g.moveTo(30, 460); g.lineTo(370, 390); g.stroke();
                    g.beginPath(); g.moveTo(190, 80); g.lineTo(180, 740); g.stroke();
                    g.fillStyle = '#e4f0ec'; g.strokeStyle = '#0f8f80'; g.lineWidth = 2;
                    g.fillRect(85, 250, 220, 250); g.strokeRect(85, 250, 220, 250);
                    [[120,290],[195,310],[270,330],[140,420],[225,440],[260,395]].forEach(([x,y])=>{
                        g.fillStyle='#e8590c'; g.beginPath(); g.arc(x,y,7,0,Math.PI*2); g.fill();
                        g.strokeStyle='#ffffff'; g.lineWidth=2; g.stroke();
                    });
                    const blob = await new Promise(r => cv.toBlob(r, 'image/png'));
                    const buf = await blob.arrayBuffer();
                    const q = extentOf(390, 844);
                    pend.set(1, {w: 390, h: 844, ext: q, pw: 390, ph: 844});
                    await show({id: 1, size: buf.byteLength, type: 'image/png', parts: [buf], ext: q});
                    say('connected');
                })();
                true;
            """)
            captured["ps-map"] = grab_phone("ps-map", "show")

            wait_js("""
                PIN = extentOf(390, 844);
                PIN = [(PIN[0]+PIN[2])/2, (PIN[1]+PIN[3])/2];
                $('pin').classList.remove('hide');
                place();
                onIdent({
                    features: [{
                        layer: 'أعمدة الإنارة',
                        title: 'عمود P-104',
                        attrs: [
                            ['النوع', 'عمود مزدوج 12 م'],
                            ['الحالة', 'يعمل بشكل طبيعي'],
                            ['الحي', 'النخيل - قطاع ب'],
                            ['المسّاح', 'عمر الحربي']
                        ]
                    }],
                    more: false
                });
                true;
            """)
            captured["ps-map-tap"] = grab_phone("ps-map-tap", "show")

            wait_js("""
                closeSheet($('identSheet'));
                say('lost');
                true;
            """)
            captured["ps-map-offline"] = grab_phone("ps-map-offline", "show")

            u_lp, _rep = _lp_codec.encode(
                "مخطط حي النخيل",
                [(46.670, 24.710), (46.682, 24.710), (46.682, 24.718), (46.670, 24.718)],
                "1:2,500",
                [(46.675, 24.714, "عمود P-104")],
                [],
                lang="ar",
                base=f"http://127.0.0.1:{port}/m/",
            )
            load_and_wait(u_lp, "typeof update === 'function' && !!T.locate")
            wait_js("""
                setLang('ar');
                $('noBase').classList.add('hide');
                $('map').classList.remove('hide');
                $('map').innerHTML = `
                    <svg viewBox="0 0 390 480" width="100%" height="100%" style="background:#edf2ee">
                      <line x1="20" y1="130" x2="370" y2="210" stroke="#d5ddd8" stroke-width="10"/>
                      <line x1="175" y1="20" x2="150" y2="460" stroke="#d5ddd8" stroke-width="8"/>
                      <rect x="75" y="110" width="240" height="240" fill="rgba(18,165,148,0.08)" stroke="#12A594" stroke-width="3" stroke-dasharray="10 6"/>
                      <circle cx="210" cy="215" r="7" fill="#E8590C" stroke="#fff" stroke-width="2"/>
                      <circle cx="165" cy="245" r="22" fill="rgba(43,124,246,0.16)" stroke="#2B7CF6" stroke-width="1"/>
                      <circle cx="165" cy="245" r="7" fill="#2B7CF6" stroke="#fff" stroke-width="2"/>
                    </svg>`;
                fix = {lat: 24.7142, lon: 46.6761, acc: 4};
                started = true;
                $('go').classList.add('hide');
                $('acts').classList.remove('hide');
                update();
                true;
            """)
            captured["ps-paper"] = grab_phone("ps-paper", "show")

            tmp_sl = tempfile.mkdtemp(prefix="biruni-sl-")
            sample_gpkg = os.path.join(tmp_sl, "نخيل_التسليم.zip")
            with open(sample_gpkg, "wb") as _sf:
                _sf.write(b"PK\x03\x04BiruniSampleDeliveredData" * 40)
            sl_dict = _seal_core.make_seal(sample_gpkg, office_name="مكتب الرياض للمساحة")
            u_sl = f"http://127.0.0.1:{port}/v/#" + sl_dict["url"].split("#", 1)[1]
            load_and_wait(u_sl, "typeof result === 'function' && !!T.ok")
            wait_js("""
                setLang('ar');
                result('ok');
                true;
            """)
            captured["ps-seal-ok"] = grab_phone("ps-seal-ok", "show")

            wait_js("""
                result('changed', 1450);
                true;
            """)
            captured["ps-seal-changed"] = grab_phone("ps-seal-changed", "show")

            u_d = f"http://127.0.0.1:{port}/d/#l=ar"
            load_and_wait(u_d, "typeof window.BiruniDash === 'object' && !!window.BiruniDash.feed")
            wait_js("""
                window.BiruniDash.setLang('ar');
                window.BiruniDash.feed({
                    t: 'dash',
                    pc: 'مكتب الرياض للمساحة',
                    card: {name: 'مشرف شمال الرياض', exp: 1800000000, zone: true},
                    def: {
                        title: 'متابعة أعمدة الإنارة',
                        theme: 'biruni-light',
                        widgets: [
                            {id: 'w1', type: 'kpi', title: 'إجمالي الأعمدة الممسوحة', x: 0, y: 0, w: 6, h: 2,
                             style: {unit: 'عمود', target: 200}},
                            {id: 'w2', type: 'gauge', title: 'نسبة الإنجاز', x: 6, y: 0, w: 6, h: 2,
                             style: {unit: '%'}},
                            {id: 'w3', type: 'bar', title: 'الأعمدة حسب الحي', x: 0, y: 2, w: 12, h: 3}
                        ]
                    }
                });
                window.BiruniDash.feed({
                    t: 'data',
                    rev: 1,
                    widgets: {
                        w1: {value: 142, delta: 18, seal: '4GCC-QB8S'},
                        w2: {value: 71, min: 0, max: 100, seal: '9MTR-2V5K'},
                        w3: {items: [
                            {key: 'النخيل', label: 'النخيل', value: 64},
                            {key: 'العقيق', label: 'العقيق', value: 48},
                            {key: 'الملقا', label: 'الملقا', value: 30}
                        ], seal: '7PWL-3N4X'}
                    }
                });
                true;
            """)
            import time as _tm
            _t_end = _tm.monotonic() + 1.15
            while _tm.monotonic() < _t_end:
                app.processEvents()
                _tm.sleep(0.02)
            captured["ps-dash"] = grab_phone("ps-dash", "show")
        finally:
            httpd.shutdown()
            httpd.server_close()

    _update_shots_json(captured, None)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Capture screenshots for Biruni link & team guide.")
    ap.add_argument("--only", default="all", help="Part id (or 'probe' or 'all')")
    ap.add_argument("--group", choices=["desktop", "phone", "both"], default="both")
    ap.add_argument("--worker", choices=["desktop", "phone"], help=argparse.SUPPRESS)
    ap.add_argument("--platform", default="offscreen", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker == "desktop":
        _run_desktop(args.only, args.platform)
        return
    if args.worker == "phone":
        _run_phone(args.only, args.platform)
        return

    groups = ["desktop", "phone"] if args.group == "both" else [args.group]
    for grp in groups:
        cmd = [sys.executable, os.path.abspath(__file__), "--worker", grp, "--only", args.only,
               "--platform", args.platform]
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            raise SystemExit(f"{grp} worker failed with code {res.returncode}")


if __name__ == "__main__":
    main()
