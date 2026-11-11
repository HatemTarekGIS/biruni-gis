"""Windows for ArcGIS services: the services browser (connections, folders, services, layers -> map or
offline copy), the connection editor and the sync window."""
import os
import webbrowser

from qgis.core import Qgis, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                 QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                                 QRadioButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from . import connections, layers, offline
from .rest import (AUTH_APIKEY, AUTH_NONE, AUTH_OAUTH, AUTH_TOKEN, Client, Connection, RestError, edit_rights,
                   split_service_url)

ROLE_KIND, ROLE_URL, ROLE_LOADED, ROLE_ROOT = (Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1,
                                              Qt.ItemDataRole.UserRole + 2, Qt.ItemDataRole.UserRole + 3)
PROP_OFFLINE = "biruni/arcgis-offline-of"
_AUTH = ((AUTH_NONE, "Anonymous (no sign-in)"), (AUTH_TOKEN, "User name and password"),
         (AUTH_APIKEY, "API key"), (AUTH_OAUTH, "Sign in with an ArcGIS account (Portal / ArcGIS Online)"))


def _busy(fn):
    def wrap(*a, **k):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return fn(*a, **k)
        finally:
            QApplication.restoreOverrideCursor()
    return wrap


# ---- a connection ---------------------------------------------------------------------------------------------------
class ConnectionDialog(QDialog):
    def __init__(self, parent=None, conn=None):
        super().__init__(parent)
        self.setWindowTitle(tr("ArcGIS server connection"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://gis.company.com/arcgis   ·   https://www.arcgis.com")
        self.auth = QComboBox()
        for key, text in _AUTH:
            self.auth.addItem(tr(text), key)
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox(tr("Remember the password (kept encrypted for your Windows account)"))
        self.remember.setChecked(connections.can_store_secrets())
        self.remember.setEnabled(connections.can_store_secrets())
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_id = QLineEdit()
        self.client_id.setPlaceholderText(tr("Application id registered on your portal"))
        self.referer = QLineEdit()
        self.verify = QCheckBox(tr("Check the server's security certificate"))
        self.verify.setChecked(True)
        form.addRow(tr("Name"), self.name)
        form.addRow(tr("Address"), self.url)
        form.addRow(tr("Sign-in"), self.auth)
        self._rows = {}
        for key, label, w in ((AUTH_TOKEN, tr("User name"), self.user), (AUTH_TOKEN, tr("Password"), self.password),
                              (AUTH_TOKEN, "", self.remember), (AUTH_APIKEY, tr("API key"), self.api_key),
                              (AUTH_OAUTH, tr("Application id"), self.client_id)):
            form.addRow(label, w)
            self._rows.setdefault(key, []).append((form.labelForField(w), w))
        form.addRow(tr("Referer (optional)"), self.referer)
        form.addRow("", self.verify)
        lay.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.test = QPushButton(tr("Test connection"))
        self.test.clicked.connect(self.run_test)
        self.signin = QPushButton(tr("Sign in…"))
        self.signin.clicked.connect(self.run_signin)
        row.addWidget(self.test)
        row.addWidget(self.signin)
        row.addStretch(1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.auth.currentIndexChanged.connect(self._auth_changed)
        if conn is not None:
            self.name.setText(conn.name)
            self.url.setText(conn.url)
            self.auth.setCurrentIndex(max(0, self.auth.findData(conn.auth)))
            self.user.setText(conn.username)
            self.password.setText(conn.password)
            self.api_key.setText(conn.api_key)
            self.client_id.setText(conn.client_id)
            self.referer.setText(conn.referer)
            self.verify.setChecked(conn.verify_ssl)
            self._token = (conn.token, conn.token_expires, conn.refresh_token)
        else:
            self._token = ("", 0.0, "")
        self._auth_changed()

    def _auth_changed(self):
        cur = self.auth.currentData()
        for key, rows in self._rows.items():
            for label, w in rows:
                for x in (label, w):
                    if x is not None:
                        x.setVisible(key == cur)
        self.signin.setVisible(cur == AUTH_OAUTH)

    def connection(self):
        c = Connection(name=self.name.text().strip() or self.url.text().strip(), url=self.url.text().strip(),
                       auth=self.auth.currentData(), username=self.user.text().strip(),
                       password=self.password.text(), api_key=self.api_key.text().strip(),
                       client_id=self.client_id.text().strip(), referer=self.referer.text().strip(),
                       verify_ssl=self.verify.isChecked())
        c.token, c.token_expires, c.refresh_token = self._token
        return c

    @_busy
    def run_test(self):
        c = self.connection()
        try:
            client = Client(c)
            if c.is_portal:
                servers = client.portal_servers()
                text = tr("Connected. {n} servers found on the portal.").format(n=len(servers))
            else:
                cat = client.catalog()
                text = tr("Connected: {f} folders and {s} services.").format(f=len(cat["folders"]),
                                                                           s=len(cat["services"]))
            self._token = (c.token, c.token_expires, c.refresh_token)
            self.status.setText(text)
            return True
        except RestError as e:
            self.status.setText(tr("Not connected: {e}").format(e=tr(str(e))))
            return False

    def run_signin(self):
        c = self.connection()
        try:
            Client(c).oauth_login(webbrowser.open)
            self._token = (c.token, c.token_expires, c.refresh_token)
            self.status.setText(tr("Signed in as {user}.").format(user=c.username or "?"))
        except RestError as e:
            self.status.setText(tr("Not signed in: {e}").format(e=e))

    def _accept(self):
        if not self.url.text().strip():
            self.status.setText(tr("Enter the address of the server or portal."))
            return
        connections.save(self.connection(), keep_password=self.remember.isChecked())
        self.accept()


# ---- the browser ----------------------------------------------------------------------------------------------------
class ServicesBrowser(QDialog):
    """Saved connections -> folders -> services -> layers; Add to Map, Take Offline."""

    def __init__(self, parent=None, project=None, notify=None):
        super().__init__(parent)
        self.setWindowTitle(tr("ArcGIS Server and Portal"))
        self.resize(620, 560)
        self.project = project or QgsProject.instance()
        self.notify = notify
        self.client = None
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.conns = QComboBox()
        self.conns.currentIndexChanged.connect(self.load_connection)
        top.addWidget(QLabel(tr("Connection")))
        top.addWidget(self.conns, 1)
        for text, fn in ((tr("New…"), self.new_connection), (tr("Edit…"), self.edit_connection),
                         (tr("Remove"), self.remove_connection)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        lay.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Name"), tr("Type")])
        self.tree.setColumnWidth(0, 360)
        self.tree.itemExpanded.connect(self.expand)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.add_selected())
        self.tree.currentItemChanged.connect(self.selected)
        lay.addWidget(self.tree, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("Add to Map"))
        self.add_btn.clicked.connect(self.add_selected)
        self.offline_btn = QPushButton(tr("Take Offline…"))
        self.offline_btn.clicked.connect(self.take_offline)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        row.addWidget(self.add_btn)
        row.addWidget(self.offline_btn)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.fill_connections()

    # -- connections --
    def fill_connections(self, select=None):
        self.conns.blockSignals(True)
        self.conns.clear()
        for n in connections.names():
            self.conns.addItem(n, n)
        self.conns.blockSignals(False)
        if select:
            self.conns.setCurrentIndex(max(0, self.conns.findData(select)))
        self.load_connection()

    def new_connection(self):
        d = ConnectionDialog(self)
        if d.exec():
            self.fill_connections(d.connection().name)

    def edit_connection(self):
        c = connections.load(self.conns.currentData() or "")
        if c is None:
            return self.new_connection()
        d = ConnectionDialog(self, c)
        if d.exec():
            if d.connection().name != c.name:
                connections.delete(c.name)
            self.fill_connections(d.connection().name)

    def remove_connection(self):
        n = self.conns.currentData()
        if n and QMessageBox.question(self, self.windowTitle(),
                                      tr("Remove the connection {name}?").format(name=n)) == \
                QMessageBox.StandardButton.Yes:
            connections.delete(n)
            self.fill_connections()

    def use(self, conn):
        """Browse this connection (also used without saving it, e.g. by tests)."""
        self.client = Client(conn)
        self.tree.clear()
        self.info.setText("")
        roots = [self.client.root] if not conn.is_portal else []
        if conn.is_portal:
            try:
                roots = self.client.portal_servers()
            except RestError as e:
                self.info.setText(tr(str(e)))
        for r in roots:
            it = QTreeWidgetItem([r.split("//", 1)[-1], tr("Server")])
            it.setData(0, ROLE_KIND, "root")
            it.setData(0, ROLE_URL, r)
            it.setData(0, ROLE_ROOT, r)
            it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            self.tree.addTopLevelItem(it)
            if len(roots) == 1:
                it.setExpanded(True)

    def load_connection(self):
        c = connections.load(self.conns.currentData() or "")
        if c is None:
            self.client = None
            self.tree.clear()
            self.info.setText(tr("Add a connection to your company's ArcGIS Server or Portal with New…"))
            return
        self.use(c)

    # -- tree --
    @_busy
    def expand(self, item):
        if item.data(0, ROLE_LOADED) or self.client is None:
            return
        item.setData(0, ROLE_LOADED, True)
        kind, url = item.data(0, ROLE_KIND), item.data(0, ROLE_URL)
        try:
            if kind in ("root", "folder"):
                root = item.data(0, ROLE_ROOT)
                d = self.client.request(url)
                for f in d.get("folders") or []:
                    self._child(item, f.rsplit("/", 1)[-1], tr("Folder"), "folder", f"{root}/{f}", True, root)
                for sv in d.get("services") or []:
                    if sv.get("type") not in ("FeatureServer", "MapServer", "ImageServer"):
                        continue
                    surl = f"{root}/{sv['name']}/{sv['type']}"
                    self._child(item, sv["name"].rsplit("/", 1)[-1], sv["type"], "service", surl, True, root)
            elif kind == "service":
                found, _info = self.client.layers(url)
                if url.endswith("/MapServer") or url.endswith("/ImageServer"):
                    self._child(item, tr("Whole map (as an image)"), tr("Map image"), "image", url, False)
                for lyr in found:
                    t = tr("Table") if lyr["table"] else (lyr["geometryType"] or "").replace("esriGeometry", "")
                    self._child(item, lyr["name"], t, "layer", f"{url}/{lyr['id']}", False)
                if not found and url.endswith("/FeatureServer"):
                    self._child(item, tr("(no layers)"), "", "error", "", False)
        except RestError as e:
            self._child(item, tr(str(e)), tr("Error"), "error", "", False)

    @staticmethod
    def _child(parent, name, typ, kind, url, expandable, root=None):
        it = QTreeWidgetItem([name, typ])
        it.setData(0, ROLE_KIND, kind)
        it.setData(0, ROLE_URL, url)
        it.setData(0, ROLE_ROOT, root if root is not None else parent.data(0, ROLE_ROOT))
        if expandable:
            it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        parent.addChild(it)
        return it

    def items(self, kind=None):
        out, stack = [], [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop(0)
            if kind is None or it.data(0, ROLE_KIND) == kind:
                out.append(it)
            stack += [it.child(i) for i in range(it.childCount())]
        return out

    @_busy
    def selected(self, item, _prev=None):
        if item is None:
            return
        kind = item.data(0, ROLE_KIND)
        self.add_btn.setEnabled(kind in ("layer", "image"))
        self.offline_btn.setEnabled(kind == "layer" and "/FeatureServer/" in (item.data(0, ROLE_URL) or ""))
        if kind == "layer" and self.client is not None:
            try:
                info = self.client.info(item.data(0, ROLE_URL))
                self.info.setText(layers.describe_rights(edit_rights(info))
                                  if "/FeatureServer/" in item.data(0, ROLE_URL)
                                  else tr("Map service layer: read-only."))
            except RestError as e:
                self.info.setText(tr(str(e)))

    # -- actions --
    @_busy
    def add_selected(self, item=None):
        item = item or self.tree.currentItem()
        if item is None or item.data(0, ROLE_KIND) not in ("layer", "image") or self.client is None:
            return None
        try:
            lyr = layers.add_to_project(self.client, item.data(0, ROLE_URL), self.project, item.text(0)
                                        if item.data(0, ROLE_KIND) == "layer" else None)
        except RestError as e:
            self.info.setText(tr(str(e)))
            return None
        connections.remember_token(self.client.conn)
        if self.notify is not None and hasattr(lyr, "editingStarted"):
            layers.guard(lyr, self.notify)
        self.info.setText(tr("{name} was added to the map.").format(name=lyr.name()))
        return lyr

    def take_offline(self, item=None, path=None):
        item = item or self.tree.currentItem()
        if item is None or item.data(0, ROLE_KIND) != "layer" or self.client is None:
            return None
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, tr("Take offline to a GeoPackage"),
                                                  os.path.join(os.path.expanduser("~"), item.text(0) + ".gpkg"),
                                                  "GeoPackage (*.gpkg)")
            if not path:
                return None
        return take_offline_to_map(self.client, item.data(0, ROLE_URL), path, self.project, item.text(0),
                                   self.info.setText)


@_busy
def take_offline_to_map(client, url, path, project=None, name=None, say=None):
    """Download the layer into the GeoPackage and add the copy to the map."""
    try:
        table = offline.take_offline(client, url, path)
    except (RestError, offline.OfflineError) as e:
        if say:
            say(tr("The layer could not be taken offline: {e}").format(e=tr(str(e))))
        return None
    lyr = QgsVectorLayer(f"{path}|layername={table}", (name or table) + " " + tr("(offline)"), "ogr")
    meta = offline.offline_layers(path).get(table) or {}
    idx = lyr.fields().indexOf(meta.get("oid_field", ""))
    if idx >= 0:   # the server's object id links each feature to the service: not to be edited by hand
        cfg = lyr.editFormConfig()
        cfg.setReadOnly(idx, True)
        lyr.setEditFormConfig(cfg)
    lyr.setCustomProperty(PROP_OFFLINE, url)
    (project or QgsProject.instance()).addMapLayer(lyr)
    if say:
        say(tr("{n} features copied. Edit the offline layer, then use Sync to send your edits back.")
            .format(n=lyr.featureCount()))
    return lyr


# ---- sync -----------------------------------------------------------------------------------------------------------
def offline_source(lyr):
    """(GeoPackage path, table) of an offline copy layer, or None."""
    if lyr is None or lyr.providerType() != "ogr":
        return None
    src = lyr.source()
    path, _, rest = src.partition("|")
    table = next((p.split("=", 1)[1] for p in rest.split("|") if p.startswith("layername=")), "")
    if not table or table not in offline.offline_layers(path):
        return None
    return path, table


class SyncDialog(QDialog):
    def __init__(self, parent, path, table, conn=None):
        super().__init__(parent)
        self.path, self.table = path, table
        self.meta = offline.offline_layers(path)[table]
        self.conn = conn or connections.load(self.meta.get("connection") or "") or \
            connections.find_for_url(self.meta["url"])
        self.setWindowTitle(tr("Sync with the service"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        _root, svc, _t, lid = split_service_url(self.meta["url"])
        lay.addWidget(QLabel(tr("Offline copy of {svc} (layer {id})").format(svc=svc, id=lid)))
        self.counts = QLabel("")
        lay.addWidget(self.counts)
        lay.addWidget(QLabel(tr("When a feature was also changed on the server:")))
        self.policy = QButtonGroup(self)
        for k, (key, text) in enumerate(((offline.SKIP, tr("Keep it for later and list it (safest)")),
                                         (offline.MINE, tr("Send my version (overwrite the server)")),
                                         (offline.THEIRS, tr("Take the server's version (drop my edit)")))):
            rb = QRadioButton(text)
            rb.setProperty("policy", key)
            rb.setChecked(k == 0)
            self.policy.addButton(rb, k)
            lay.addWidget(rb)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.result)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.go = bb.addButton(tr("Sync Now"), QDialogButtonBox.ButtonRole.ActionRole)
        self.go.clicked.connect(self.run)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.refresh_counts()

    def refresh_counts(self):
        p = offline.pending(self.path, self.table)
        self.counts.setText(tr("Not sent yet: {a} added, {c} changed, {d} deleted.").format(
            a=len(p.adds), c=len(p.updates), d=len(p.deletes)))
        return p

    @_busy
    def run(self):
        if self.conn is None:
            self.result.setText(tr("No saved connection for this service: add one in the ArcGIS services browser."))
            return None
        policy = self.policy.checkedButton().property("policy")
        try:
            rep = offline.sync(Client(self.conn), self.path, self.table, policy=policy)
        except (RestError, offline.OfflineError) as e:
            self.result.setText(tr("Sync failed: {e}").format(e=tr(str(e))))
            return None
        connections.remember_token(self.conn)
        lines = [rep.text(tr)]
        for fid, oid, why in rep.conflicts[:20]:
            lines.append("• " + tr("Object {oid}: {why}").format(oid=oid, why=tr(why)))
        for what, msg in rep.errors[:20]:
            lines.append("• " + f"{what}: {msg}")
        self.result.setText("\n".join(lines))
        self.refresh_counts()
        for lyr in QgsProject.instance().mapLayers().values():
            if offline_source(lyr) == (self.path, self.table):
                lyr.reload()
                lyr.triggerRepaint()
        return rep


# ---- entry points for the ribbon ------------------------------------------------------------------------------------
def _parent(plugin):
    iface = getattr(plugin, "iface", None)
    return iface.mainWindow() if iface is not None else None


def _notify(plugin):
    iface = getattr(plugin, "iface", None)
    if iface is None:
        return None

    def say(text, level):
        iface.messageBar().pushMessage(tr("ArcGIS service"), text,
                                       Qgis.MessageLevel.Info if level == 0 else Qgis.MessageLevel.Warning, 10)
    return say


def open_browser(plugin):
    d = ServicesBrowser(_parent(plugin), notify=_notify(plugin))
    d.exec()


def sync_active_layer(plugin):
    iface = getattr(plugin, "iface", None)
    lyr = iface.activeLayer() if iface is not None else None
    src = offline_source(lyr)
    if src is None:
        QMessageBox.information(_parent(plugin), tr("Sync with the service"),
                                tr("Select an offline copy of a service layer in the Layers panel first "
                                   "(made with Take Offline in the ArcGIS services browser)."))
        return
    if lyr.isEditable() and lyr.isModified():
        lyr.commitChanges()
    SyncDialog(_parent(plugin), *src).exec()


def refresh_service_tokens(plugin=None):
    """Sign service layers in again (e.g. after opening a project)."""
    return layers.refresh_tokens()
