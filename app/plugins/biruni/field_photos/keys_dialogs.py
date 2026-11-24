"""The office's keys (no-loss P3): backup PC, emergency key and the recipients list, as small dialogs for the
Field Photos dock. The work is done in recipients.py, emergency.py and handover.py; this file only asks and shows."""
import base64
import os
import time

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
                                 QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout)

from ..i18n import tr

PACK_FILE = "office-pack.bin"     # a backup PC's copy of the office pack (protected for this Windows user)


def _docs():
    d = os.path.join(os.path.expanduser("~"), "Documents")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def _hint(text):
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setObjectName("GpsHint")
    return lb


def _office(dock):
    from .handover import HandoverKey

    if dock.server is not None:
        return dock.server.handover
    return HandoverKey(dock._cert_folder())


def _changed(dock):
    if dock.server is not None:
        dock.server.refresh_recipients()
    dock._show_keys_state()


def scope_text(scope):
    names = {"photo": tr("photos"), "state": tr("checklist answers"), "pos": tr("positions")}
    return " · ".join(names[k] for k in ("photo", "state", "pos") if k in (scope or []))


def role_text(role):
    return {"office": tr("This computer (the office)"), "backup": tr("Backup computer"),
            "emergency": tr("Emergency key (3 of 5 holders)"), "sup": tr("Supervisor")}.get(role, role)


# ---- the recipients list --------------------------------------------------------------------------------------------
def recipients_dialog(dock):
    from . import recipients

    folder = dock._cert_folder()
    hk = _office(dock)
    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Recipients"))
    v = QVBoxLayout(dlg)
    v.addWidget(_hint(tr(
        "Each photo is sealed on the phone so that only the computers below can open it. Add a backup computer and "
        "an emergency key so that losing this computer never loses the field work.")))
    table = QTableWidget(0, 3)
    table.setHorizontalHeaderLabels([tr("Who"), tr("Opens"), tr("Added")])
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    v.addWidget(table)
    state = _hint("")
    v.addWidget(state)

    def fill():
        d = recipients.load(folder)
        rows = [{"p": hk.public, "role": "office", "scope": list(recipients.KINDS), "n": "", "at": ""}] + d["r"]
        table.setRowCount(len(rows))
        for i, e in enumerate(rows):
            who = role_text(e["role"]) + (" — " + e["n"] if e.get("n") else "")
            for j, t in enumerate((who, scope_text(e["scope"]), e.get("at") or "")):
                it = QTableWidgetItem(t)
                it.setData(Qt.ItemDataRole.UserRole, e["p"] if e["role"] != "office" else "")
                table.setItem(i, j, it)
        state.setText(tr("This office's id: {id}. Phones receive the list within seconds while Field Photos is "
                         "running.").format(id=hk.kid) if d["seq"] else
                      tr("No list yet: phones seal for this computer alone."))

    row = QHBoxLayout()
    rm = QPushButton(tr("Remove"))
    close = QPushButton(tr("Close"))
    row.addWidget(rm)
    row.addStretch(1)
    row.addWidget(close)
    v.addLayout(row)

    def remove():
        it = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        p = it.data(Qt.ItemDataRole.UserRole) if it else ""
        if not p:
            return
        if QMessageBox.question(dlg, tr("Recipients"), tr(
                "Remove this recipient? Photos already sealed for it stay openable by it; new photos will not be.")) \
                != QMessageBox.StandardButton.Yes:
            return
        recipients.remove(folder, p)
        _changed(dock)
        fill()

    rm.clicked.connect(remove)
    close.clicked.connect(dlg.accept)
    fill()
    dlg.resize(560, 320)
    dlg.exec()


# ---- a backup PC ----------------------------------------------------------------------------------------------------
def backup_pc_dialog(dock):
    from . import recipients

    folder = dock._cert_folder()
    hk = _office(dock)
    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Backup PC"))
    v = QVBoxLayout(dlg)
    v.addWidget(QLabel("<b>" + tr("Add a backup computer to this office") + "</b>"))
    v.addWidget(_hint(tr(
        "On the backup computer open this same window and save its public key, then bring the file here. From then "
        "on the phones seal every photo for it too.")))
    g = QGridLayout()
    g.addWidget(QLabel(tr("Name")), 0, 0)
    name = QLineEdit()
    name.setPlaceholderText(tr("e.g. the branch computer"))
    g.addWidget(name, 0, 1, 1, 2)
    g.addWidget(QLabel(tr("Public key")), 1, 0)
    pub = QLineEdit()
    pub.setPlaceholderText(tr("Paste the key, or open its file"))
    g.addWidget(pub, 1, 1)
    openk = QPushButton(tr("Open file…"))
    g.addWidget(openk, 1, 2)
    v.addLayout(g)
    addb = QPushButton(tr("Add as backup computer"))
    v.addWidget(addb)
    v.addWidget(QLabel("<b>" + tr("This computer as a backup") + "</b>"))
    v.addWidget(_hint(tr("This computer's public key (it opens nothing by itself; it can be sent by e-mail):")))
    mine = QLineEdit(hk.public)
    mine.setReadOnly(True)
    v.addWidget(mine)
    r2 = QHBoxLayout()
    copyb = QPushButton(tr("Copy"))
    saveb = QPushButton(tr("Save to file…"))
    packb = QPushButton(tr("Open the office pack…"))
    filesb = QPushButton(tr("Open sealed files…"))
    for b in (copyb, saveb, packb, filesb):
        r2.addWidget(b)
    v.addLayout(r2)
    state = _hint("")
    v.addWidget(state)
    close = QPushButton(tr("Close"))
    close.clicked.connect(dlg.accept)
    v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    def open_key():
        path, _ = QFileDialog.getOpenFileName(dlg, tr("Public key"), _docs(),
                                              tr("Biruni public key (*.biruni-pub);;All files (*)"))
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    pub.setText(f.read().strip())
            except OSError as e:
                state.setText(tr("It could not be done: {e}").format(e=e))

    def add():
        try:
            recipients.add(folder, pub.text(), "backup", name.text().strip() or tr("Backup computer"),
                           office_pub=hk.public)
        except ValueError as e:
            state.setText(tr("It could not be done: {e}").format(e=tr(str(e))))
            return
        _changed(dock)
        state.setText(tr("Added. Phones seal for it from the next photo."))
        if QMessageBox.question(dlg, tr("Backup PC"), tr(
                "Save the office pack for this backup computer? It lets that computer read this office's Google Drive "
                "folder and positions. It opens only on that computer.")) == QMessageBox.StandardButton.Yes:
            save_pack(pub.text().strip())
        pub.clear()
        name.clear()

    def save_pack(bpub):
        from . import bridge
        from .handover import BRIDGE_KEYS
        from .server import read_surveyor_keys
        from .tunnel import unprotect

        st = QgsSettings()
        keys = {n: unprotect(st.value(n, "") or "") for n in BRIDGE_KEYS}
        keys = {n: k for n, k in keys.items() if len(k) == 43}
        if not keys:
            keys = {BRIDGE_KEYS[0]: bridge.load_key(st)}
        path, _ = QFileDialog.getSaveFileName(dlg, tr("Backup PC"), os.path.join(
            _docs(), "biruni-office-pack-" + recipients.kid_hex(bpub) + ".biruni-pack"),
            tr("Biruni office pack (*.biruni-pack)"))
        if not path:
            return
        blob = recipients.office_pack(bpub, keys, read_surveyor_keys(dock._active_folder()), office_pub=hk.public)
        with open(path, "wb") as f:
            f.write(blob)
        state.setText(tr("Saved. Take the file to the backup computer and open it there."))

    def copy():
        QApplication.clipboard().setText(hk.public)
        state.setText(tr("Copied."))

    def save_pub():
        path, _ = QFileDialog.getSaveFileName(dlg, tr("Save to file…"), os.path.join(
            _docs(), "biruni-backup-pc-" + hk.kid + ".biruni-pub"), tr("Biruni public key (*.biruni-pub)"))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(hk.public + "\n")
            state.setText(tr("Saved."))

    def open_pack():
        from .tunnel import protect

        path, _ = QFileDialog.getOpenFileName(dlg, tr("Open the office pack…"), _docs(),
                                              tr("Biruni office pack (*.biruni-pack);;All files (*)"))
        if not path:
            return
        try:
            with open(path, "rb") as f:
                blob = f.read()
            meta = recipients.open_office_pack(hk, blob)
        except (OSError, ValueError) as e:
            state.setText(tr("It could not be done: {e}").format(e=tr(str(e))))
            return
        if QMessageBox.question(dlg, tr("Open the office pack…"), tr(
                "This pack says it is from the office whose id is {id}. Compare it with the id shown on the office "
                "computer (Recipients) and keep it only if they are the same.").format(
                id=meta.get("office") or "?")) != QMessageBox.StandardButton.Yes:
            return
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, PACK_FILE), "w", encoding="ascii") as f:
            f.write(protect(base64.b64encode(blob).decode("ascii")))
        state.setText(tr("The office pack is kept on this computer ({n} codes).").format(n=len(meta["keys"])))

    def open_files():
        from .tunnel import unprotect

        try:
            with open(os.path.join(folder, PACK_FILE), encoding="ascii") as f:
                pack = recipients.open_office_pack(hk, base64.b64decode(unprotect(f.read().strip()) or ""))
        except (OSError, ValueError):
            pack = None
        paths, _ = QFileDialog.getOpenFileNames(dlg, tr("Open sealed files…"), _docs(),
                                                tr("Sealed photos (*.bfp *.bfx);;All files (*)"))
        if not paths:
            return
        store = dock.server.store if dock.server is not None else None
        if store is None:
            from .server import PhotoStore

            store = PhotoStore(dock._active_folder(), ())
        res = recipients.import_files(paths, store, hk, pack)
        state.setText(tr("Opened: {n}. Already here: {s}. Refused: {r}.").format(
            n=res["imported"], s=res["skipped"], r=len(res["refused"])))
        state.setToolTip("\n".join(f"{os.path.basename(p)}: {why}" for p, why in res["refused"][:10]))

    openk.clicked.connect(open_key)
    addb.clicked.connect(add)
    copyb.clicked.connect(copy)
    saveb.clicked.connect(save_pub)
    packb.clicked.connect(open_pack)
    filesb.clicked.connect(open_files)
    dlg.resize(600, 0)
    dlg.exec()


# ---- the emergency key ----------------------------------------------------------------------------------------------
def emergency_dialog(dock):
    from . import emergency, recipients

    folder = dock._cert_folder()
    hk = _office(dock)
    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Emergency key"))
    v = QVBoxLayout(dlg)
    cur = recipients.load(folder).get("emergency") or {}
    v.addWidget(_hint(tr(
        "The emergency key opens the photos when this computer and its key backup are both lost. It is split into 5 "
        "parts, one for each person below; any 3 of them together can open the photos, and 2 can do nothing. The "
        "whole key is never saved anywhere.")))
    if cur:
        v.addWidget(_hint(tr("An emergency key made on {date} is in use. Making a new one replaces it.")
                          .format(date=cur.get("made") or "")))
    g = QGridLayout()
    names = []
    for i in range(emergency.N):
        g.addWidget(QLabel(tr("Holder {n}").format(n=i + 1)), i, 0)
        e = QLineEdit((cur.get("holders") or [""] * 5)[i] if len(cur.get("holders") or []) == 5 else "")
        names.append(e)
        g.addWidget(e, i, 1)
    v.addLayout(g)
    state = _hint("")
    v.addWidget(state)
    row = QHBoxLayout()
    make = QPushButton(tr("Make the emergency key…"))
    use = QPushButton(tr("Open photos with 3 parts…"))
    close = QPushButton(tr("Close"))
    row.addWidget(make)
    row.addWidget(use)
    row.addStretch(1)
    row.addWidget(close)
    v.addLayout(row)

    def do_make():
        holders = [e.text().strip() for e in names]
        if not all(holders):
            state.setText(tr("Write the five names first."))
            return
        out = QFileDialog.getExistingDirectory(dlg, tr("Where to save the 5 parts (then give each to its holder)"),
                                               _docs())
        if not out:
            return
        try:
            pub, paths = emergency.create(out, holders)
            recipients.add(folder, pub, "emergency", tr("Emergency key"), office_pub=hk.public)
            d = recipients.load(folder)
            d["emergency"] = {"made": time.strftime("%Y-%m-%d"), "holders": holders}
            recipients.save(folder, d)
        except (OSError, ValueError) as e:
            state.setText(tr("It could not be done: {e}").format(e=tr(str(e))))
            return
        _changed(dock)
        state.setText(tr("Done: 5 files were saved. Give each file to its holder, on a USB stick or by hand, and "
                         "delete them from this computer."))

    def do_use():
        shares, _ = QFileDialog.getOpenFileNames(dlg, tr("Choose 3 parts of the emergency key"), _docs(),
                                                 tr("Emergency key part (*.bek)"))
        if len(shares) < 3:
            return
        files, _ = QFileDialog.getOpenFileNames(dlg, tr("Choose the sealed photos"), _docs(),
                                                tr("Sealed photos (*.bfp);;All files (*)"))
        if not files:
            return
        out = QFileDialog.getExistingDirectory(dlg, tr("Where to save the opened photos"), _docs())
        if not out:
            return
        try:
            res = emergency.open_files(shares, files, out)
        except ValueError as e:
            state.setText(tr("It could not be done: {e}").format(e=tr(str(e))))
            return
        state.setText(tr("Opened: {n}. Refused: {r}.").format(n=len(res["opened"]), r=len(res["refused"])))

    make.clicked.connect(do_make)
    use.clicked.connect(do_use)
    close.clicked.connect(dlg.accept)
    dlg.resize(520, 0)
    dlg.exec()
