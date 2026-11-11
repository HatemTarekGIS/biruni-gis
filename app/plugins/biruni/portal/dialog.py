"""The portal window: create or open a portal folder, add items (a file, the selected layer, a link), manage groups
and their members (by member card, or by making a key for someone without Biruni), follow other portals, publish,
and this user's own member card and key file."""
import os
import tempfile
import time
import urllib.request

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings, QgsVectorFileWriter, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout,
                                 QWidget)

from ..i18n import tr
from . import core
from . import crypto as C
from . import page

LAST = "biruni/portal/last_folder"
BASE = "biruni/portal/base_url"


def _fetch(url):
    if url.lower().startswith(("http://", "https://")):
        with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310 - the user's own followed addresses
            return r.read(4 * 1024 * 1024)
    path = url[7:] if url.lower().startswith("file://") else url
    with open(path, "rb") as f:
        return f.read()


class ItemDialog(QDialog):
    """Title, tags, groups, expiry, scope of a new item."""

    def __init__(self, parent, portal, title="", link=False):
        super().__init__(parent)
        self.setWindowTitle(tr("New portal item"))
        f = QFormLayout(self)
        self.title = QLineEdit(title)
        f.addRow(tr("Title"), self.title)
        self.url = QLineEdit()
        self.kind = QComboBox()
        if link:
            f.addRow(tr("Address"), self.url)
            for k, label in (("link", tr("Link")), ("dashboard", tr("Dashboard link")),
                             ("mapservice", tr("Map service folder"))):
                self.kind.addItem(label, k)
            f.addRow(tr("Type"), self.kind)
        self.desc = QPlainTextEdit()
        self.desc.setFixedHeight(70)
        f.addRow(tr("Description"), self.desc)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText(tr("comma separated"))
        f.addRow(tr("Tags"), self.tags)
        self.groups = QListWidget()
        self.groups.setFixedHeight(90)
        for g in portal.index["groups"]:
            it = QListWidgetItem(g["name"])
            it.setData(Qt.ItemDataRole.UserRole, g["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.groups.addItem(it)
        f.addRow(tr("Shared with"), self.groups)
        f.addRow("", QLabel(tr("No group checked = public: anyone with the address sees it.")))
        self.days = QDoubleSpinBox()
        self.days.setRange(0, 3650)
        self.days.setDecimals(1)
        self.days.setSpecialValueText(tr("Never"))
        f.addRow(tr("Expires after (days)"), self.days)
        self.rotate = QCheckBox(tr("Auto-rotate: on expiry, take it out and replace its groups' keys"))
        f.addRow("", self.rotate)
        self.scope = QLineEdit()
        self.scope.setPlaceholderText(tr("e.g. for review only, not for construction"))
        f.addRow(tr("Scope of use"), self.scope)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        groups = [self.groups.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.groups.count())
                  if self.groups.item(i).checkState() == Qt.CheckState.Checked]
        return {"title": self.title.text().strip(), "desc": self.desc.toPlainText().strip(),
                "tags": [t for t in self.tags.text().replace("،", ",").split(",") if t.strip()], "groups": groups,
                "expires_days": self.days.value() or None, "rotate": self.rotate.isChecked(),
                "scope": self.scope.text().strip()}


class PortalDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin else None))
        self.plugin = plugin
        self.portal = None
        self.setWindowTitle(tr("Signed portal"))
        self.resize(760, 560)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.folder = QLineEdit(QgsSettings().value(LAST, "") or "")
        top.addWidget(QLabel(tr("Portal folder")))
        top.addWidget(self.folder, 1)
        b = QPushButton(tr("Browse…"))
        b.clicked.connect(self._browse)
        top.addWidget(b)
        b = QPushButton(tr("Open"))
        b.clicked.connect(self._open)
        top.addWidget(b)
        b = QPushButton(tr("Create here…"))
        b.clicked.connect(self._create)
        top.addWidget(b)
        v.addLayout(top)
        self.info = QLabel(tr("Choose a folder: a new portal, or one you made before."))
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.info)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        self._items_tab()
        self._groups_tab()
        self._follow_tab()
        self._publish_tab()
        self._key_tab()
        self._refresh()
        if self.folder.text() and os.path.isfile(os.path.join(self.folder.text(), core.INDEX)):
            self._open(quiet=True)

    # -- helpers -----------------------------------------------------------------------------------------------------
    def _warn(self, text):
        QMessageBox.warning(self, tr("Signed portal"), text)

    def _need(self):
        if not self.portal:
            self._warn(tr("Open or create a portal first."))
            return False
        return True

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr("Portal folder"), self.folder.text())
        if d:
            self.folder.setText(d)

    def _open(self, quiet=False):
        try:
            self.portal = core.Portal.open(self.folder.text())
            QgsSettings().setValue(LAST, self.folder.text())
        except Exception as e:  # noqa: BLE001
            self.portal = None
            if not quiet:
                self._warn(tr("This folder has no portal of yours: {0}").format(e))
        self._refresh()

    def _create(self):
        folder = self.folder.text().strip()
        if not folder:
            self._browse()
            folder = self.folder.text().strip()
        if not folder:
            return
        name, ok = QInputDialog.getText(self, tr("New portal"), tr("Portal name (e.g. the office or municipality)"))
        if not ok or not name.strip():
            return
        owner, ok = QInputDialog.getText(self, tr("New portal"), tr("Your name or the publisher's name"))
        if not ok:
            return
        try:
            os.makedirs(folder, exist_ok=True)
            self.portal = core.Portal.create(folder, name.strip(), owner.strip())
            QgsSettings().setValue(LAST, folder)
        except Exception as e:  # noqa: BLE001
            self._warn(str(e))
        self._refresh()

    def _refresh(self):
        p = self.portal
        if p:
            idx = p.index
            self.info.setText(tr("{0} — signed by {1}, key {2} · {3} items · {4} groups · version {5}").format(
                idx["name"], idx["owner"]["name"] or "…", C.key_print(p.pub), len(idx["items"]), len(idx["groups"]),
                idx["seq"]))
        self.items.clear()
        self.glist.clear()
        self.flist.clear()
        if not p:
            return
        names = {g["id"]: g["name"] for g in p.index["groups"]}
        for m in p.items():
            bits = [m["title"], tr(m["type"])]
            bits.append(", ".join(names.get(g, g) for g in m["groups"]) or tr("public"))
            if m["expires"]:
                bits.append((tr("expired") if core.is_expired(m) else tr("expires")) + " " +
                            time.strftime("%Y-%m-%d", time.localtime(m["expires"])))
            it = QListWidgetItem(" · ".join(bits))
            it.setData(Qt.ItemDataRole.UserRole, m["id"])
            self.items.addItem(it)
        for g in p.index["groups"]:
            it = QListWidgetItem(f"{g['name']}  ({len(g['members'])}) · v{g['ver']}")
            it.setData(Qt.ItemDataRole.UserRole, g["id"])
            self.glist.addItem(it)
        for f in p.index["follows"]:
            it = QListWidgetItem(f"{f['name'] or f['url']} — {C.key_print(f['pub'])}")
            it.setData(Qt.ItemDataRole.UserRole, f["url"])
            self.flist.addItem(it)
        self._members()

    # -- items ---------------------------------------------------------------------------------------------------------
    def _items_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.items = QListWidget()
        v.addWidget(self.items, 1)
        row = QHBoxLayout()
        for label, fn in ((tr("Add a file…"), self._add_file), (tr("Add the selected layer"), self._add_layer),
                          (tr("Add a link…"), self._add_link), (tr("Remove"), self._remove_item),
                          (tr("Sweep expired"), self._sweep)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self.tabs.addTab(w, tr("Items"))

    def _ask(self, title="", link=False):
        d = ItemDialog(self, self.portal, title, link)
        if d.exec() != QDialog.DialogCode.Accepted:
            return None, d
        return d.values(), d

    def _add_file(self, path=None):
        if not self._need():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, tr("Add a file"))
        if not path:
            return
        vals, _ = self._ask(os.path.basename(path))
        if vals is None:
            return
        try:
            self.portal.add_file(path, **vals)
        except Exception as e:  # noqa: BLE001
            self._warn(str(e))
        self._refresh()

    def _add_layer(self):
        if not self._need():
            return
        lyr = self.plugin.iface.activeLayer() if self.plugin else None
        if lyr is None:
            self._warn(tr("Select a layer in the Layers panel first."))
            return
        if isinstance(lyr, QgsVectorLayer):
            tmp = os.path.join(tempfile.mkdtemp(prefix="biruni_portal_"), _safe(lyr.name()) + ".gpkg")
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.layerName = _safe(lyr.name())
            err = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, tmp, QgsProject.instance().transformContext(), opts)
            if err[0] != QgsVectorFileWriter.WriterError.NoError:
                self._warn(tr("The layer could not be written: {0}").format(err[1]))
                return
            self._add_file(tmp)
        elif isinstance(lyr, QgsRasterLayer) and os.path.isfile(lyr.source()):
            self._add_file(lyr.source())
        else:
            self._warn(tr("This layer is not a file; export it first."))

    def _add_link(self):
        if not self._need():
            return
        vals, d = self._ask("", link=True)
        if vals is None:
            return
        try:
            self.portal.add_link(d.url.text().strip(), kind=d.kind.currentData(), **vals)
        except Exception as e:  # noqa: BLE001
            self._warn(str(e))
        self._refresh()

    def _remove_item(self):
        it = self.items.currentItem()
        if not it or not self._need():
            return
        if QMessageBox.question(self, tr("Signed portal"), tr("Remove this item from the portal?")) != \
                QMessageBox.StandardButton.Yes:
            return
        self.portal.remove_item(it.data(Qt.ItemDataRole.UserRole))
        self._refresh()

    def _sweep(self):
        if not self._need():
            return
        gone = self.portal.sweep()
        QMessageBox.information(self, tr("Signed portal"), tr("{0} expired items taken out.").format(len(gone)))
        self._refresh()

    # -- groups -------------------------------------------------------------------------------------------------------
    def _groups_tab(self):
        w = QWidget()
        h = QHBoxLayout(w)
        left = QVBoxLayout()
        self.glist = QListWidget()
        self.glist.currentRowChanged.connect(lambda *_: self._members())
        left.addWidget(QLabel(tr("Groups (each group is a key)")))
        left.addWidget(self.glist, 1)
        for label, fn in ((tr("New group…"), self._new_group), (tr("Delete group"), self._del_group),
                          (tr("Replace the group key"), self._rotate)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            left.addWidget(b)
        h.addLayout(left, 1)
        right = QVBoxLayout()
        self.mlist = QListWidget()
        right.addWidget(QLabel(tr("Members")))
        right.addWidget(self.mlist, 1)
        for label, fn in ((tr("Add by member card…"), self._add_card), (tr("Make a key for a member…"), self._make_key),
                          (tr("Remove member"), self._remove_member)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            right.addWidget(b)
        h.addLayout(right, 1)
        self.tabs.addTab(w, tr("Groups"))

    def _gid(self):
        it = self.glist.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _members(self):
        self.mlist.clear()
        if not self.portal or not self._gid():
            return
        for m in self.portal.group(self._gid())["members"]:
            it = QListWidgetItem(f"{m['name'] or '…'} — {C.key_print(m['box'])}" + (f" ({tr('owner')})" if m.get("owner") else ""))
            it.setData(Qt.ItemDataRole.UserRole, m["box"])
            self.mlist.addItem(it)

    def _new_group(self):
        if not self._need():
            return
        name, ok = QInputDialog.getText(self, tr("New group"), tr("Group name (e.g. Survey team, Municipality)"))
        if ok and name.strip():
            self.portal.add_group(name.strip())
            self._refresh()

    def _del_group(self):
        gid = self._gid()
        if not gid:
            return
        if QMessageBox.question(self, tr("Signed portal"), tr(
                "Delete this group? Its items that belong to no other group are removed too.")) == \
                QMessageBox.StandardButton.Yes:
            self.portal.remove_group(gid)
            self._refresh()

    def _rotate(self):
        gid = self._gid()
        if gid:
            self.portal.rotate_group(gid)
            self._refresh()

    def _add_card(self):
        gid = self._gid()
        if not gid:
            self._warn(tr("Choose a group first."))
            return
        text, ok = QInputDialog.getMultiLineText(self, tr("Add a member"), tr(
            "Paste the member card (BPM1…), or leave empty to choose a card file:"))
        if not ok:
            return
        if not text.strip():
            path, _ = QFileDialog.getOpenFileName(self, tr("Member card"), "", "Biruni (*.bpm *.txt)")
            if not path:
                return
            with open(path, encoding="utf-8") as f:
                text = f.read()
        try:
            card = C.parse_card(text)
        except ValueError:
            self._warn(tr("This is not a member card."))
            return
        if QMessageBox.question(self, tr("Add a member"), tr(
                "Add {0}? Check the key with them by phone: {1}").format(card["name"], C.key_print(card["box"]))) != \
                QMessageBox.StandardButton.Yes:
            return
        self.portal.add_member(gid, card=text.strip())
        self._refresh()

    def _make_key(self):
        """For someone without Biruni: a key file made here, handed over (with its passphrase said separately)."""
        gid = self._gid()
        if not gid:
            self._warn(tr("Choose a group first."))
            return
        name, ok = QInputDialog.getText(self, tr("Make a member key"), tr("Member name"))
        if not ok or not name.strip():
            return
        pw, ok = QInputDialog.getText(self, tr("Make a member key"), tr(
            "A passphrase for the key file (tell it to them separately; at least 8 characters)"),
            QLineEdit.EchoMode.Password)
        if not ok or len(pw) < 8:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save the member key"), _safe(name) + ".biruni-key.json",
                                              "JSON (*.json)")
        if not path:
            return
        priv, card = C.new_member_key(name.strip())
        with open(path, "w", encoding="utf-8") as f:
            f.write(C.key_file(name.strip(), priv, pw))
        self.portal.add_member(gid, card=card)
        self._refresh()

    def _remove_member(self):
        gid, it = self._gid(), self.mlist.currentItem()
        if not gid or not it:
            return
        if QMessageBox.question(self, tr("Signed portal"), tr(
                "Remove this member? The group key is replaced and the group's files are sealed again. "
                "What they already downloaded stays with them.")) != QMessageBox.StandardButton.Yes:
            return
        if not self.portal.remove_member(gid, it.data(Qt.ItemDataRole.UserRole)):
            self._warn(tr("The owner cannot be removed."))
        self._refresh()

    # -- following -----------------------------------------------------------------------------------------------------
    def _follow_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(tr("Portals this portal follows: their signed lists show in your viewer, each checked "
                              "against the key you pin here.")))
        self.flist = QListWidget()
        v.addWidget(self.flist, 1)
        self.fstatus = QLabel()
        self.fstatus.setWordWrap(True)
        v.addWidget(self.fstatus)
        row = QHBoxLayout()
        for label, fn in ((tr("Follow a portal…"), self._follow), (tr("Stop following"), self._unfollow),
                          (tr("Check now"), self._check)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self.tabs.addTab(w, tr("Following"))

    def _follow(self):
        if not self._need():
            return
        url, ok = QInputDialog.getText(self, tr("Follow a portal"), tr("Portal address (folder URL or path)"))
        if not ok or not url.strip():
            return
        pub, name = "", ""
        try:
            rep = core.verify_doc(core.read_doc(_fetch(core.portal_url(url.strip()))))
            if rep["index"]:
                pub = rep["index"]["owner"]["pub"]
                name = rep["index"]["name"]
        except Exception:  # noqa: BLE001 - unreachable now: the key can still be pinned by hand
            pass
        pub, ok = QInputDialog.getText(self, tr("Follow a portal"), tr(
            "The owner's key to pin (check its fingerprint {0} with them)").format(C.key_print(pub) if pub else "?"),
            QLineEdit.EchoMode.Normal, pub)
        if not ok or not pub.strip():
            return
        try:
            self.portal.follow(url.strip(), pub.strip(), name)
        except Exception as e:  # noqa: BLE001
            self._warn(str(e))
        self._refresh()

    def _unfollow(self):
        it = self.flist.currentItem()
        if it and self._need():
            self.portal.unfollow(it.data(Qt.ItemDataRole.UserRole))
            self._refresh()

    def _check(self):
        if not self._need():
            return
        rows = core.aggregate(self.portal.index, _fetch)
        lines = []
        for r in rows:
            n = len(r["report"]["items"]) if r["report"] else 0
            lines.append(f"{r['follow']['name'] or r['follow']['url']}: {tr(r['status'])} ({n})")
        self.fstatus.setText("\n".join(lines) or tr("No followed portals."))

    # -- publishing ----------------------------------------------------------------------------------------------------
    def _publish_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.base = QLineEdit(QgsSettings().value(BASE, "") or "")
        self.base.setPlaceholderText("https://<user>.github.io/<repo>/portal-office")
        f.addRow(tr("Address where it will be hosted"), self.base)
        row = QHBoxLayout()
        for label, fn in ((tr("Open the viewer"), self._view), (tr("Publish to a folder…"), self._publish),
                          (tr("Copy the portal link"), self._copy_link), (tr("Build the hosted viewer"), self._bridge)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addRow(row)
        note = QLabel(tr("The portal folder is the whole portal: put it on GitHub Pages, a shared network folder or a "
                         "USB stick. Public items are readable by anyone with the address; items shared with groups "
                         "are encrypted - but their titles and tags are visible to all."))
        note.setWordWrap(True)
        f.addRow(note)
        self.tabs.addTab(w, tr("Publish"))

    def _view(self):
        if self._need():
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.join(self.portal.folder, "index.html")))

    def _publish(self):
        if not self._need():
            return
        d = QFileDialog.getExistingDirectory(self, tr("Publish to a folder"))
        if d:
            self.portal.publish(d)
            QMessageBox.information(self, tr("Signed portal"), tr("Published to {0}").format(d))

    def _copy_link(self):
        if not self._need():
            return
        base = self.base.text().strip()
        if not base:
            self._warn(tr("Write the address where the portal will be hosted."))
            return
        QgsSettings().setValue(BASE, base)
        QGuiApplication.clipboard().setText(self.portal.address(base))

    def _bridge(self):
        path = page.build_bridge_viewer()
        QMessageBox.information(self, tr("Signed portal"), tr(
            "The hosted viewer is ready: {0}\nIt opens any portal by its address: …/portal/#u=ADDRESS&k=KEY").format(path))

    # -- my key --------------------------------------------------------------------------------------------------------
    def _key_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(tr("Your member card: give it to a portal owner so they can add you to a group.")))
        self.myname = QLineEdit(QgsSettings().value("biruni/portal/my_name", "") or "")
        self.myname.setPlaceholderText(tr("Your name"))
        v.addWidget(self.myname)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.qr, 1)
        self.card = QLineEdit()
        self.card.setReadOnly(True)
        v.addWidget(self.card)
        row = QHBoxLayout()
        for label, fn in ((tr("Show my card"), self._show_card), (tr("Save my card…"), self._save_card),
                          (tr("Export my key file…"), self._export_key)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self.tabs.addTab(w, tr("My key"))

    def _show_card(self):
        name = self.myname.text().strip()
        QgsSettings().setValue("biruni/portal/my_name", name)
        text = core.my_card(name)
        self.card.setText(text)
        try:
            from ..dashboard.dock import qr_pixmap

            self.qr.setPixmap(qr_pixmap(text, 5))
        except Exception:  # noqa: BLE001 - no QR library: the text is enough
            self.qr.setText(C.key_print(C.parse_card(text)["box"]))
        return text

    def _save_card(self):
        text = self._show_card()
        path, _ = QFileDialog.getSaveFileName(self, tr("Save my card"), "my-card.bpm", "Biruni (*.bpm)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    def _export_key(self):
        pw, ok = QInputDialog.getText(self, tr("Export my key file"), tr(
            "A passphrase to protect the key file (at least 8 characters)"), QLineEdit.EchoMode.Password)
        if not ok or len(pw) < 8:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Export my key file"), "my.biruni-key.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(core.my_key_file(self.myname.text().strip(), pw))


def _safe(name):
    s = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name or "layer"))
    return s.strip("_")[:60] or "layer"


_OPEN = []


def open_dialog(plugin):
    d = PortalDialog(plugin)
    _OPEN[:] = [d]
    d.show()
    return d
