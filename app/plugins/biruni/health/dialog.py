"""System health dialog, account owners dialog, and 'Someone left…' dialog (no-loss P4)."""
import os
import time

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                                 QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
                                 QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from . import checks, owners, runbook

LEVEL_BADGE = {
    "ok": ("🟢", "OK", "#0f7b55"),
    "warn": ("🟡", "Attention", "#b86e00"),
    "bad": ("🔴", "Action needed", "#c93636"),
    "na": ("⚪", "Not reported", "#6c7680"),
}


def make_ctx(now=None):
    """Build a `checks.Ctx` reading live QgsSettings and Biruni paths."""
    from ..field_photos.dock import KEY as FP_KEY, default_folder

    s = QgsSettings()
    cert_folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
    photos_folder = s.value(FP_KEY + "folder", "") or default_folder()
    return checks.Ctx(
        get_setting=lambda k, d="": s.value(k, d),
        cert_folder=cert_folder,
        photos_folder=photos_folder,
        now=float(time.time() if now is None else now),
    )


def prompt_service_owner(parent, cert_folder, sid, service_label):
    """Ask for the owner's name and organisation email after Drive or Cloudflare setup, and save via `owners.set_owner`."""
    cur = (owners.load(cert_folder).get("services") or {}).get(sid) or {}
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("Account owner — {service}").format(service=service_label))
    v = QVBoxLayout(dlg)
    note = QLabel("<b>" + tr("Use the organisation's email for this account, not a personal one.") + "</b>")
    note.setWordWrap(True)
    v.addWidget(note)
    sub = QLabel(tr("Record who manages {service} so the office always knows who owns the account.").format(
        service=service_label
    ))
    sub.setWordWrap(True)
    sub.setObjectName("GpsHint")
    v.addWidget(sub)

    form = QFormLayout()
    name_edit = QLineEdit(cur.get("owner") or "")
    name_edit.setPlaceholderText(tr("Owner's full name"))
    email_edit = QLineEdit(cur.get("email") or "")
    email_edit.setPlaceholderText(tr("name@company.com"))
    form.addRow(tr("Owner name:"), name_edit)
    form.addRow(tr("Organisation email:"), email_edit)
    v.addLayout(form)

    warn_lbl = QLabel()
    warn_lbl.setWordWrap(True)
    warn_lbl.setStyleSheet("color: #b86e00; font-weight: 600;")
    warn_lbl.hide()
    v.addWidget(warn_lbl)

    def _check_email(txt):
        if owners.is_personal(txt):
            warn_lbl.setText(tr("This looks like a personal email address. Prefer an organisation email address."))
            warn_lbl.show()
        else:
            warn_lbl.hide()

    email_edit.textChanged.connect(_check_email)
    _check_email(email_edit.text())

    row = QHBoxLayout()
    row.addStretch(1)
    skip_btn = QPushButton(tr("Later"))
    save_btn = QPushButton(tr("Save"))
    save_btn.setObjectName("GpsPrimary")
    row.addWidget(skip_btn)
    row.addWidget(save_btn)
    v.addLayout(row)

    skip_btn.clicked.connect(dlg.reject)
    save_btn.clicked.connect(dlg.accept)
    dlg.resize(440, 0)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        owner_name = name_edit.text().strip()
        email = email_edit.text().strip()
        if owner_name or email:
            owners.set_owner(cert_folder, sid, owner_name, email)
            return True
    return False


def owners_dialog(parent, folder, focus_sid=None):
    """Dialog to view and edit service account owners, organisation name, and key backup holder/location."""
    doc = owners.load(folder)
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("Account owners and key holders"))
    v = QVBoxLayout(dlg)

    banner = QLabel("<b>" + tr("Use the organisation's email for each account, not a personal one.") + "</b>")
    banner.setWordWrap(True)
    v.addWidget(banner)

    org_box = QGroupBox(tr("Organisation"))
    org_form = QFormLayout(org_box)
    org_edit = QLineEdit(doc.get("org") or "")
    org_edit.setPlaceholderText(tr("Engineering office or organisation name"))
    org_form.addRow(tr("Organisation name:"), org_edit)
    v.addWidget(org_box)

    srv_box = QGroupBox(tr("Cloud and domain accounts"))
    g = QGridLayout(srv_box)
    g.addWidget(QLabel("<b>" + tr("Service") + "</b>"), 0, 0)
    g.addWidget(QLabel("<b>" + tr("Owner name") + "</b>"), 0, 1)
    g.addWidget(QLabel("<b>" + tr("Organisation email") + "</b>"), 0, 2)

    labels = {
        "drive": tr("Google Drive"),
        "cloudflare": tr("Cloudflare (Worker / Tunnel)"),
        "domain": tr("Domain"),
    }
    edits = {}
    for row_idx, sid in enumerate(owners.SERVICES, start=1):
        info = (doc.get("services") or {}).get(sid) or {}
        g.addWidget(QLabel(labels.get(sid, sid)), row_idx, 0)
        ne = QLineEdit(info.get("owner") or "")
        ne.setPlaceholderText(tr("Owner's full name"))
        ee = QLineEdit(info.get("email") or "")
        ee.setPlaceholderText(tr("name@company.com"))
        g.addWidget(ne, row_idx, 1)
        g.addWidget(ee, row_idx, 2)
        edits[sid] = (ne, ee)
    v.addWidget(srv_box)

    kb_box = QGroupBox(tr("Office key backup (.biruni-key)"))
    kb_form = QFormLayout(kb_box)
    kb = doc.get("key_backup") or {}
    kb_holder = QLineEdit(kb.get("holder") or "")
    kb_holder.setPlaceholderText(tr("Who holds the password-locked key backup"))
    kb_where = QLineEdit(kb.get("where") or "")
    kb_where.setPlaceholderText(tr("e.g. Office safe — USB #1 and manager's vault"))
    kb_form.addRow(tr("Key backup holder:"), kb_holder)
    kb_form.addRow(tr("Where it is kept:"), kb_where)
    v.addWidget(kb_box)

    if focus_sid in edits:
        edits[focus_sid][0].setFocus()

    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    cancel_btn = QPushButton(tr("Cancel"))
    save_btn = QPushButton(tr("Save"))
    save_btn.setObjectName("GpsPrimary")
    btn_row.addWidget(cancel_btn)
    btn_row.addWidget(save_btn)
    v.addLayout(btn_row)

    def do_save():
        cur = owners.load(folder)
        cur["org"] = org_edit.text().strip()
        today = time.strftime("%Y-%m-%d")
        for sid, (ne, ee) in edits.items():
            prev = (cur.get("services") or {}).get(sid) or {}
            oname = ne.text().strip()
            oemail = ee.text().strip()
            changed = (oname != (prev.get("owner") or "")) or (oemail != (prev.get("email") or ""))
            cur["services"][sid] = {
                "owner": oname,
                "email": oemail,
                "at": today if (changed and (oname or oemail)) else (prev.get("at") or (today if (oname or oemail) else "")),
            }
            if oname and owners.has_left(cur, oname) and changed:
                # If user explicitly re-saved or replaced, only clear if it's a new person; keep if same unless they cleared left
                pass
        cur["key_backup"] = {
            "holder": kb_holder.text().strip(),
            "where": kb_where.text().strip(),
        }
        owners.save(folder, cur)
        if cur["org"]:
            try:
                from ..seal.core import set_office

                set_office(cur["org"])
            except Exception:  # noqa: BLE001
                pass
        dlg.accept()

    cancel_btn.clicked.connect(dlg.reject)
    save_btn.clicked.connect(do_save)
    dlg.resize(560, 0)
    return dlg.exec() == QDialog.DialogCode.Accepted


def someone_left_dialog(parent, cert_folder):
    """Record that a team member left and immediately show which accounts or keys need to be rotated."""
    doc = owners.load(cert_folder)
    _, holders = checks._load_emergency_holders(cert_folder)
    known = []
    for sid in owners.SERVICES:
        n = ((doc.get("services") or {}).get(sid) or {}).get("owner") or ""
        if n and n not in known:
            known.append(n)
    kb_h = (doc.get("key_backup") or {}).get("holder") or ""
    if kb_h and kb_h not in known:
        known.append(kb_h)
    for h in holders:
        if h and h not in known:
            known.append(h)

    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("Someone left…"))
    v = QVBoxLayout(dlg)
    hint = QLabel(tr(
        "Choose or type the name of the person who left the organisation. System Health will flag every service "
        "account or emergency key share they held so you can replace it."
    ))
    hint.setWordWrap(True)
    v.addWidget(hint)

    combo = QComboBox()
    combo.setEditable(True)
    for n in known:
        combo.addItem(n)
    combo.setCurrentText("")
    if combo.lineEdit() is not None:
        combo.lineEdit().setPlaceholderText(tr("Full name of the person who left"))
    v.addWidget(combo)

    left_map = doc.get("left") or {}
    if left_map:
        prev_txt = "، ".join(f"{k} ({d})" for k, d in left_map.items())
        prev_lbl = QLabel(tr("Previously marked as left: {names}").format(names=prev_txt))
        prev_lbl.setWordWrap(True)
        prev_lbl.setObjectName("GpsHint")
        v.addWidget(prev_lbl)

    row = QHBoxLayout()
    clear_btn = QPushButton(tr("Clear list"))
    clear_btn.setVisible(bool(left_map))
    row.addWidget(clear_btn)
    row.addStretch(1)
    cancel_btn = QPushButton(tr("Cancel"))
    ok_btn = QPushButton(tr("Mark as left"))
    ok_btn.setObjectName("GpsPrimary")
    row.addWidget(cancel_btn)
    row.addWidget(ok_btn)
    v.addLayout(row)

    def do_clear():
        cur = owners.load(cert_folder)
        cur["left"] = {}
        owners.save(cert_folder, cur)
        dlg.accept()

    def do_mark():
        who = combo.currentText().strip()
        if not who:
            return
        owners.mark_left(cert_folder, who)
        dlg.accept()

    clear_btn.clicked.connect(do_clear)
    cancel_btn.clicked.connect(dlg.reject)
    ok_btn.clicked.connect(do_mark)
    dlg.resize(440, 0)
    return dlg.exec() == QDialog.DialogCode.Accepted


class HealthDialog(QDialog):
    """System health window (🟢🟡🔴): checks keys, copies, surveyor queues and account owners."""

    def __init__(self, plugin, parent=None, ctx_factory=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin and getattr(plugin, "iface", None) else None))
        self.plugin = plugin
        self._ctx_factory = ctx_factory or make_ctx
        self.rows = []
        self.setObjectName("BiruniSystemHealth")
        self.setWindowTitle(tr("System health"))
        self.setMinimumSize(QSize(680, 460))

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr("State"), tr("Item"), tr("Detail"), ""])
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.owners_btn = QPushButton(tr("Owners…"))
        self.owners_btn.clicked.connect(self.open_owners)
        self.left_btn = QPushButton(tr("Someone left…"))
        self.left_btn.clicked.connect(self.open_someone_left)
        self.print_btn = QPushButton(tr("Print emergency sheet…"))
        self.print_btn.clicked.connect(self.print_runbook)
        self.refresh_btn = QPushButton(tr("Refresh"))
        self.refresh_btn.clicked.connect(self.refresh)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.owners_btn)
        row.addWidget(self.left_btn)
        row.addWidget(self.print_btn)
        row.addStretch(1)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.close_btn)
        v.addLayout(row)

        self.refresh()

    def _ctx(self):
        return self._ctx_factory()

    def refresh(self):
        ctx = self._ctx()
        self.rows = checks.run(ctx)
        w = checks.worst(self.rows)
        counts = {"bad": 0, "warn": 0, "ok": 0, "na": 0}
        for r in self.rows:
            counts[r["level"]] = counts.get(r["level"], 0) + 1
        badge, _lbl, color = LEVEL_BADGE.get(w, LEVEL_BADGE["na"])
        if w == "bad":
            headline = tr("Action needed: {b} critical issue(s), {w} warning(s).").format(
                b=counts["bad"], w=counts["warn"]
            )
        elif w == "warn":
            headline = tr("Attention needed: {w} warning(s), no critical issues.").format(w=counts["warn"])
        elif w == "ok":
            headline = tr("All system health checks passed ({n} OK).").format(n=counts["ok"])
        else:
            headline = tr("System health checked ({ok} OK, {na} not reported yet).").format(
                ok=counts["ok"], na=counts["na"]
            )
        self.summary.setText(f"<span style='color:{color};font-weight:700'>{badge} {headline}</span>")

        self.tree.clear()
        for r in self.rows:
            b_ico, b_txt, b_col = LEVEL_BADGE.get(r["level"], LEVEL_BADGE["na"])
            state_str = f"{b_ico} {tr(b_txt)}"
            it = QTreeWidgetItem([state_str, r["title"], r["text"], ""])
            it.setForeground(0, QColor(b_col))
            it.setToolTip(1, r["group"])
            it.setToolTip(2, r["text"])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            self.tree.addTopLevelItem(it)
            if r.get("fix") and r["level"] in ("bad", "warn"):
                btn = QPushButton(tr("Fix…"))
                btn.clicked.connect(lambda _=False, row=r: self.run_fix(row))
                self.tree.setItemWidget(it, 3, btn)
        return self.rows

    def run_fix(self, row):
        fix = row.get("fix") if isinstance(row, dict) else row
        if callable(fix):
            fix()
            self.refresh()
            return
        fix_str = str(fix or "")
        ctx = self._ctx()
        if fix_str == "owners":
            self.open_owners()
            return
        if fix_str.startswith("owner:"):
            sid = fix_str.split(":", 1)[1]
            if owners_dialog(self, ctx.cert_folder, focus_sid=sid):
                self.refresh()
            return
        if self.plugin is None:
            return
        if fix_str == "backup":
            from ..backup import dialog as backup_dialog

            backup_dialog.open_dialog(self.plugin)
            return
        from ..field_photos import dock as fp_dock

        d = fp_dock.open_dock(self.plugin)
        if fix_str == "key_backup":
            d._backup_key()
        elif fix_str == "second_copy":
            d._pick_mirror_folder()
        elif fix_str == "emergency_key":
            d._emergency_dialog()
        elif fix_str == "org_relay":
            d._open_org_relay()
        self.refresh()

    def open_owners(self):
        ctx = self._ctx()
        if owners_dialog(self, ctx.cert_folder):
            self.refresh()

    def open_someone_left(self):
        ctx = self._ctx()
        if someone_left_dialog(self, ctx.cert_folder):
            self.refresh()

    def print_runbook(self):
        ctx = self._ctx()
        model = runbook.runbook_model(ctx)
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        base_dir = docs if os.path.isdir(docs) else os.path.expanduser("~")
        default_path = os.path.join(base_dir, f"biruni-emergency-sheet-{model['made']}.pdf")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Print emergency sheet…"), default_path, tr("PDF file (*.pdf)")
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            runbook.write_runbook(path, model)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, tr("Print emergency sheet…"), tr("It could not be done: {e}").format(e=e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def open_health_dialog(plugin):
    """Open or raise the System Health dialog (non-blocking show)."""
    dlg = getattr(plugin, "_system_health_dialog", None)
    if dlg is None:
        dlg = HealthDialog(plugin)
        if plugin is not None:
            plugin._system_health_dialog = dlg
    else:
        dlg.refresh()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


ALERT_SETTING = "biruni/health/last-alert"


def check_on_startup(plugin, ctx=None, today=None):
    """Run health checks once after startup; if worst is `bad`, push a messageBar alert at most once a day."""
    try:
        ctx = ctx or make_ctx()
        rows = checks.run(ctx)
        if checks.worst(rows) != "bad":
            return False
        s = QgsSettings()
        day = str(today or time.strftime("%Y-%m-%d", time.localtime(ctx.now)))
        if str(s.value(ALERT_SETTING, "") or "") == day:
            return False
        s.setValue(ALERT_SETTING, day)
        iface = getattr(plugin, "iface", None)
        bar = iface.messageBar() if iface is not None and hasattr(iface, "messageBar") else None
        if bar is None:
            return False
        from qgis.core import Qgis
        from ..version import APP_NAME

        bad_rows = [r for r in rows if r.get("level") == "bad"]
        first_txt = bad_rows[0]["text"] if bad_rows else tr("One or more system health checks need attention.")
        msg = tr("System health: {text}").format(text=first_txt)
        item = bar.createMessage(APP_NAME, msg)
        btn = QPushButton(tr("Open system health"))
        btn.clicked.connect(lambda: open_health_dialog(plugin))
        item.layout().addWidget(btn)
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)
        return True
    except Exception:  # noqa: BLE001 - startup health check never throws
        return False
