"""«Dashboard of dashboards» (PC side): the link of a supervisor's view that brings several dashboards together
(see ext_multi.py for the page).

The link is any dashboard page's address with ``#m=<b64u(JSON)>``: ``{"t": title, "s": [{"n": name, "u": link}],
"map": {group: [titles]}}``. Every source keeps its own link - and so its own card, its own limits (widgets,
zone, hidden fields) and its own seals; this PC only writes the list. Links of other PCs' dashboards are pasted
in; this PC's issued cards are picked from a list.
"""
import base64
import json

from qgis.core import QgsProject

MAX_SOURCES = 12


def _b64u(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


def _unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


def is_dashboard_link(u):
    """A dashboard link carries its card after "#" (c=BDC1.…)."""
    u = (u or "").strip()
    return u.startswith("https://") and "#" in u and ("c=BDC1." in u.split("#", 1)[1] or "c=PREVIEW." in u)


def multi_link(base, sources, title="", mapping=None):
    """The view's address. base: a dashboard page address (its part before "#" is kept); sources: [(name, link)]."""
    srcs = []
    for name, link in sources:
        link = (link or "").strip()
        if not is_dashboard_link(link):
            raise ValueError("not a dashboard link with a card: " + link[:60])
        srcs.append({"n": (name or "").strip()[:80], "u": link})
    if not srcs:
        raise ValueError("no dashboards")
    if len(srcs) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} dashboards")
    doc = {"t": (title or "").strip()[:120], "s": srcs}
    if mapping:
        doc["map"] = {str(k): [str(t) for t in v] for k, v in mapping.items()}
    return base.split("#", 1)[0] + "#m=" + _b64u(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def parse_multi(link):
    """The view's {"t", "s", "map"} from its address, or None."""
    if "#" not in (link or ""):
        return None
    for part in link.split("#", 1)[1].split("&"):
        if part.startswith("m="):
            try:
                doc = json.loads(_unb64u(part[2:]))
            except (ValueError, UnicodeDecodeError):
                return None
            return doc if isinstance(doc, dict) and isinstance(doc.get("s"), list) else None
    return None


def _base_of(dock):
    """A page to host the view: this PC's page from anywhere when it is on, else its page on this network."""
    sh = getattr(dock, "sharing", None)
    if sh is not None and getattr(sh, "anywhere", None) is not None and sh.anywhere.running():
        from .host import PAGE_URL

        return PAGE_URL
    if sh is not None and sh.running():
        addrs = sh.addresses()
        return f"https://{addrs[0] if addrs else '127.0.0.1'}:{sh.lan.port}/d/{sh.lan.token}/"
    from .host import PAGE_URL

    return PAGE_URL


def collect_ui(plugin, dock=None):
    """ACTION: choose this PC's cards and paste other PCs' dashboard links -> the view's link and QR code."""
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QGuiApplication
    from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                     QMessageBox, QPlainTextEdit, QVBoxLayout)

    from ..i18n import tr
    from . import model
    from .dock import card_text, qr_pixmap

    dlg = QDialog(dock)
    dlg.setWindowTitle(tr("Dashboard of dashboards"))
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(tr("One view for a supervisor: several dashboards, each with its own card, live together.")))
    name = QLineEdit()
    name.setPlaceholderText(tr("Title of the view"))
    lay.addWidget(name)
    lay.addWidget(QLabel(tr("Cards of this computer:")))
    lst = QListWidget()
    titles = {d["id"]: d.get("title") or d["id"] for d in model.load_all(QgsProject.instance())}
    sh = getattr(dock, "sharing", None)
    for p in model.issued_cards(QgsProject.instance()):
        it = QListWidgetItem(f"{titles.get(p.get('dash'), p.get('dash'))} — {p.get('name') or p['id']}")
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, p)
        lst.addItem(it)
    lay.addWidget(lst)
    lay.addWidget(QLabel(tr("Links of dashboards on other computers (one per line):")))
    other = QPlainTextEdit()
    lay.addWidget(other)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    sources = []
    for i in range(lst.count()):
        it = lst.item(i)
        if it.checkState() != Qt.CheckState.Checked:
            continue
        p = it.data(Qt.ItemDataRole.UserRole)
        links = sh.links(card_text(p)) if sh is not None and sh.running() else {"lan": [], "anywhere": None}
        link = links.get("anywhere") or (links.get("lan") or [None])[0]
        if not link:
            QMessageBox.information(dock, tr("Dashboard of dashboards"),
                                    tr("Start sharing first, so this computer's dashboards have links."))
            return None
        sources.append((p.get("name") or titles.get(p.get("dash"), ""), link))
    for line in other.toPlainText().splitlines():
        if line.strip():
            sources.append(("", line.strip()))
    try:
        link = multi_link(_base_of(dock), sources, name.text())
    except ValueError as e:
        QMessageBox.warning(dock, tr("Dashboard of dashboards"), str(e))
        return None
    QGuiApplication.clipboard().setText(link)
    box = QDialog(dock)
    box.setWindowTitle(tr("Dashboard of dashboards"))
    v = QVBoxLayout(box)
    v.addWidget(QLabel(tr("The link is copied. Open it on a phone or scan the code:")))
    qr = QLabel()
    try:
        qr.setPixmap(qr_pixmap(link, 3))
    except Exception:  # noqa: BLE001 - a very long link does not fit a QR code; the copied link still works
        qr.setText(tr("Too long for a QR code - send the copied link."))
    v.addWidget(qr)
    ed = QLineEdit(link)
    ed.setReadOnly(True)
    v.addWidget(ed)
    b2 = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    b2.rejected.connect(box.reject)
    b2.accepted.connect(box.accept)
    v.addWidget(b2)
    box.exec()
    return link
