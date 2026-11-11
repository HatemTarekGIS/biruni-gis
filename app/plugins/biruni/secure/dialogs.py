"""Dialogs of the locked safe: lock a database (wizard), sign in, users and rights, lock a project."""
import datetime as dt
import os

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QDate, QMarginsF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import (QColor, QFont, QGuiApplication, QImage, QPageLayout, QPageSize, QPainter, QPixmap,
                             QTextOption)
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDateEdit, QDialog, QFileDialog,
                                 QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTableWidget,
                                 QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from ..i18n import format_date, is_rtl, tr
from . import passwords
from .widgets import PasswordField, StrengthMeter, card, header, icon, label, primary, style_dialog, tokens

KEY = "biruni/secure/"


def _busy(fn, *a, **k):
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        return fn(*a, **k)
    finally:
        QApplication.restoreOverrideCursor()


def lock_error_text(e):
    code = getattr(e, "code", "")
    if code == "wrong_password":
        return tr("Wrong user name or password.")
    if code == "disabled":
        return tr("This user has been disabled by the administrator.")
    if code == "expired":
        return tr("Your access to this database ended on {date}.").format(date=getattr(e, "detail", ""))
    if code == "tampered":
        return tr("This file was changed outside Biruni after its administrator saved it. It is not opened, so "
                  "that you never work on altered data.")
    if code == "bad_recovery":
        return tr("This recovery key does not open this database.")
    if code == "not_locked":
        return tr("This is not a locked file.")
    if code == "changed_elsewhere":
        return tr("The file was saved by someone else since you opened it. Close it and open it again.")
    if code == "other_family":
        return tr("This file is not a copy of this database.")
    if code == "name_taken":
        return tr("There is already a user with this name.")
    return tr("The file is damaged or incomplete.")


def honest_note(admin=True):
    f, v = card("SecHonest")
    v.addWidget(label(tr("What the lock guarantees"), "SecSub"))
    v.addWidget(label(tr("Encryption: a user who may not see a layer or a field never receives its key, so no program "
                         "can open it from his copy. Every change is signed by its author, and a file changed by "
                         "hand is refused.")))
    v.addWidget(label(tr("Biruni's interface: export, print and copy, and refusing edits that are not allowed. "
                         "Nothing can stop a screenshot or someone retyping what he sees.")))
    if admin:
        v.addWidget(label(tr("A user can always change the bytes of the layers he can see in his own copy - but "
                             "changes he was not allowed to make are detected and refused when you merge his file.")))
    return f


def recovery_pdf(path, code, file_name, qr_png=None):
    """One printable page: the recovery key in large letters and as a QR code."""
    from qgis.PyQt.QtGui import QPdfWriter

    w = QPdfWriter(path)
    w.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    w.setPageMargins(QMarginsF(18, 18, 18, 18), QPageLayout.Unit.Millimeter)
    w.setResolution(150)
    w.setTitle("Biruni GIS - " + tr("Recovery key"))
    p = QPainter(w)
    width = w.width()
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    opt.setTextDirection(Qt.LayoutDirection.RightToLeft if is_rtl() else Qt.LayoutDirection.LeftToRight)
    opt.setAlignment(Qt.AlignmentFlag.AlignRight if is_rtl() else Qt.AlignmentFlag.AlignLeft)
    y = 0

    def text(t, size, bold=False, h=None, color="#14181D"):
        nonlocal y
        f = QFont("Segoe UI", size)
        f.setBold(bold)
        p.setFont(f)
        p.setPen(QColor(color))
        r = QRectF(0, y, width, h or size * 6)
        p.drawText(r, t, opt)
        y += h or size * 6

    text(tr("Recovery key"), 22, True, 90, "#0E7C74")
    text(tr("Database: {name}").format(name=file_name), 11, False, 50)
    text(tr("Made on {date}").format(date=format_date(dt.date.today())), 11, False, 60)
    f = QFont("Consolas", 22)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#14181D"))
    p.drawText(QRectF(0, y, width, 80), Qt.AlignmentFlag.AlignCenter, code)
    y += 110
    if qr_png:
        img = QImage.fromData(qr_png)
        side = 420
        p.drawImage(QRectF((width - side) / 2, y, side, side), img)
        y += side + 30
    text(tr("This key unlocks the administrator of the database if every password is lost. Keep this page in a "
            "safe place, away from the computer. Whoever has it can open everything in the database."), 11, False, 150)
    text(tr("Without this key and without the passwords, nobody - not even Biruni - can open the data."), 11, True, 90)
    p.end()


def qr_png(text):
    try:
        from ..gps.browser import qr_png as make
        from ..vendor import ensure_vendor

        ensure_vendor()
        return make(text, 6)
    except Exception:  # noqa: BLE001 - the key is still shown as text
        return None


# =================================================================================================================
# Lock a database: what -> administrator -> recovery key -> done
# =================================================================================================================
class LockDatabaseWizard(QDialog):
    def __init__(self, plugin=None, parent=None, source=""):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle(tr("Lock a database"))
        style_dialog(self, plugin)
        self.code = None
        self.out_path = None
        self.admin_password = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)
        v.addWidget(header(tr("Lock a database with a password"),
                           tr("Each layer is encrypted with its own key. You are the administrator: you add users "
                              "and choose what each one may see and change."), "lock", plugin))
        steps = QHBoxLayout()
        self.step_labels = []
        for i, t in enumerate((tr("What to lock"), tr("Administrator"), tr("Recovery key"), tr("Done"))):
            lb = QLabel(f"{i + 1}  {t}")
            self.step_labels.append(lb)
            steps.addWidget(lb)
        steps.addStretch(1)
        v.addLayout(steps)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_source())
        self.stack.addWidget(self._page_admin())
        self.stack.addWidget(self._page_recovery())
        self.stack.addWidget(self._page_done())
        v.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        self.cancel = QPushButton(tr("Cancel"))
        self.back = QPushButton(tr("Back"))
        self.next = primary(tr("Next"))
        nav.addWidget(self.cancel)
        nav.addStretch(1)
        nav.addWidget(self.back)
        nav.addWidget(self.next)
        v.addLayout(nav)
        self.cancel.clicked.connect(self.reject)
        self.back.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.next.clicked.connect(self._next)
        self.resize(640, 600)
        if source:
            self.set_source(source)
        self._go(0)

    # ---- pages ---------------------------------------------------------------------------------------------------
    def _page_source(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        c, cv = card()
        cv.addWidget(label(tr("GeoPackage to lock"), "SecSub"))
        row = QHBoxLayout()
        self.src = QLineEdit()
        self.src.setReadOnly(True)
        self.src.setPlaceholderText(tr("Choose a GeoPackage (.gpkg)"))
        pick = QPushButton(tr("Choose…"))
        pick.clicked.connect(self._pick_source)
        row.addWidget(self.src, 1)
        row.addWidget(pick)
        cv.addLayout(row)
        cv.addWidget(label(tr("Layers to put in the locked database")))
        self.layers = QListWidget()
        self.layers.setMinimumHeight(150)
        cv.addWidget(self.layers, 1)
        cv.addWidget(label(tr("Locked file")))
        row = QHBoxLayout()
        self.out = QLineEdit()
        change = QPushButton(tr("Change…"))
        change.clicked.connect(self._pick_out)
        row.addWidget(self.out, 1)
        row.addWidget(change)
        cv.addLayout(row)
        cv.addWidget(label(tr("The original GeoPackage is not changed. Once the locked file opens, keep the original "
                              "somewhere safe or delete it yourself.")))
        v.addWidget(c, 1)
        return w

    def _page_admin(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        c, cv = card()
        form = QFormLayout()
        self.admin_name = QLineEdit(QgsSettings().value(KEY + "admin_name", "") or "")
        self.admin_name.setPlaceholderText(tr("e.g. your name"))
        self.pw1 = PasswordField(tr("Administrator password"), self.plugin)
        self.pw2 = PasswordField(tr("The same password again"), self.plugin)
        self.meter = StrengthMeter(passwords.MIN_ADMIN)
        self.pw1.changed.connect(self.meter.update_for)
        form.addRow(tr("Administrator name"), self.admin_name)
        form.addRow(tr("Password"), self.pw1)
        form.addRow("", self.meter)
        form.addRow(tr("Confirm"), self.pw2)
        cv.addLayout(form)
        cv.addWidget(label(tr("Every guess at a password costs about a third of a second and 256 MB of memory, so "
                              "guessing a good password would take centuries. Offline, there is no counter of "
                              "failed attempts that means anything: the file can be copied and tried elsewhere. "
                              "Only a good password protects it.")))
        v.addWidget(c)
        warn, wv = card("SecWarn")
        wv.addWidget(label(tr("If the passwords and the recovery key are lost, the data is lost"), "SecSub"))
        wv.addWidget(label(tr("Nobody can open a locked database without a password or its recovery key - not you, "
                              "not your users, not Biruni. There is no back door. On the next step you will get a "
                              "recovery key: print it or save it somewhere safe.")))
        v.addWidget(warn)
        v.addStretch(1)
        return w

    def _page_recovery(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        c, cv = card()
        cv.addWidget(label(tr("Your recovery key"), "SecSub"))
        cv.addWidget(label(tr("It opens the administrator of this database if every password is lost. It is shown "
                              "only now - it is not stored anywhere in clear.")))
        mid = QHBoxLayout()
        self.code_lbl = QLabel()
        self.code_lbl.setObjectName("SecCode")
        self.code_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_lbl.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.qr = QLabel()
        self.qr.setFixedSize(150, 150)
        self.qr.setScaledContents(True)
        mid.addWidget(self.code_lbl, 1)
        mid.addWidget(self.qr)
        cv.addLayout(mid)
        row = QHBoxLayout()
        copy = QPushButton(icon("copy", self.plugin), tr("Copy"))
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.code or ""))
        pdf = QPushButton(icon("file-type-pdf", self.plugin), tr("Save as PDF to print…"))
        pdf.clicked.connect(self._save_pdf)
        row.addWidget(copy)
        row.addWidget(pdf)
        row.addStretch(1)
        cv.addLayout(row)
        self.saved_it = QCheckBox(tr("I have saved the recovery key in a safe place"))
        self.saved_it.toggled.connect(lambda on: self.next.setEnabled(on))
        cv.addWidget(self.saved_it)
        v.addWidget(c)
        warn, wv = card("SecWarn")
        wv.addWidget(label(tr("Whoever has this key can open everything in the database. Do not keep it next to the "
                              "file, and do not send it with the file.")))
        v.addWidget(warn)
        v.addStretch(1)
        return w

    def _page_done(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        c, cv = card()
        self.done_title = label("", "SecOk")
        self.done_text = label("")
        cv.addWidget(self.done_title)
        cv.addWidget(self.done_text)
        row = QHBoxLayout()
        self.open_now = primary(tr("Open it now"))
        self.users_btn = QPushButton(icon("users", self.plugin), tr("Users and permissions…"))
        row.addWidget(self.open_now)
        row.addWidget(self.users_btn)
        row.addStretch(1)
        cv.addLayout(row)
        self.open_now.clicked.connect(lambda: self._finish("open"))
        self.users_btn.clicked.connect(lambda: self._finish("users"))
        v.addWidget(c)
        v.addWidget(honest_note())
        v.addStretch(1)
        self.action = None
        return w

    # ---- behaviour -------------------------------------------------------------------------------------------------
    def _go(self, i):
        i = max(0, min(3, i))
        self.stack.setCurrentIndex(i)
        for j, lb in enumerate(self.step_labels):
            lb.setObjectName("SecStepOn" if j == i else "SecStep")
            lb.style().unpolish(lb)
            lb.style().polish(lb)
        self.back.setVisible(0 < i < 3)
        self.next.setText(tr("Lock") if i == 2 else (tr("Close") if i == 3 else tr("Next")))
        self.next.setEnabled(i != 2 or self.saved_it.isChecked())
        self.cancel.setVisible(i < 3)
        if i == 2 and not self.code:
            from . import crypto

            self.code = crypto.new_recovery_code()
            self.code_lbl.setText(self.code[:19] + "\n" + self.code[20:])  # two lines of 4 groups
            png = qr_png(self.code)
            if png:
                pm = QPixmap()
                pm.loadFromData(png)
                self.qr.setPixmap(pm)

    def _pick_source(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("GeoPackage to lock"), os.path.dirname(self.src.text()),
                                              tr("GeoPackage") + " (*.gpkg)")
        if path:
            self.set_source(path)

    def set_source(self, path):
        from . import gpkg

        self.src.setText(path)
        self.layers.clear()
        try:
            found = gpkg.list_layers(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, tr("Lock a database"), str(e))
            return
        for name, geom, count, _ in found:
            it = QListWidgetItem(f"{name}   ·   {geom}   ·   " + tr("{n} features").format(n=count))
            it.setData(Qt.ItemDataRole.UserRole, name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.layers.addItem(it)
        if not self.out.text():
            self.out.setText(os.path.splitext(path)[0] + ".bgpkg")

    def _pick_out(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Locked file"), self.out.text(),
                                              tr("Locked database") + " (*.bgpkg)")
        if path:
            self.out.setText(path if path.lower().endswith(".bgpkg") else path + ".bgpkg")

    def chosen_layers(self):
        return [self.layers.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.layers.count())
                if self.layers.item(i).checkState() == Qt.CheckState.Checked]

    def _say(self, text):
        QMessageBox.information(self, tr("Lock a database"), text)

    def _next(self):
        i = self.stack.currentIndex()
        if i == 0:
            if not self.src.text() or not os.path.exists(self.src.text()):
                return self._say(tr("Choose the GeoPackage to lock."))
            if not self.chosen_layers():
                return self._say(tr("Choose at least one layer."))
            if not self.out.text().strip():
                return self._say(tr("Choose where to write the locked file."))
            if os.path.abspath(self.out.text()) == os.path.abspath(self.src.text()):
                return self._say(tr("The locked file must be a new file, not the GeoPackage itself."))
            return self._go(1)
        if i == 1:
            if not self.admin_name.text().strip():
                return self._say(tr("Write the administrator's name."))
            why = passwords.problem(self.pw1.text(), passwords.MIN_ADMIN)
            if why:
                return self._say(tr(why).format(n=passwords.MIN_ADMIN))
            if self.pw1.text() != self.pw2.text():
                return self._say(tr("The two passwords are not the same."))
            return self._go(2)
        if i == 2:
            return self._lock()
        self.accept()

    def _save_pdf(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Save as PDF to print…"),
                                              os.path.splitext(self.out.text())[0] + " - " + tr("Recovery key") + ".pdf",
                                              "PDF (*.pdf)")
        if path:
            recovery_pdf(path, self.code, os.path.basename(self.out.text()), qr_png(self.code))

    def _lock(self):
        from . import container

        out = os.path.abspath(self.out.text().strip())
        try:
            _busy(container.create, self.src.text(), self.chosen_layers(), out, self.admin_name.text(),
                  self.pw1.text(), self.code)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("Lock a database"), str(e))
            return
        QgsSettings().setValue(KEY + "admin_name", self.admin_name.text().strip())
        self.out_path = out
        self.admin_password = self.pw1.text()
        self.pw1.setText("")
        self.pw2.setText("")
        self.done_title.setText(tr("The database is locked."))
        self.done_text.setText(tr("{file} - {n} layers. Add users and give each one his rights in Users and "
                                  "permissions, then send each one his copy.").format(
            file=os.path.basename(out), n=len(self.chosen_layers())))
        self._go(3)

    def _finish(self, action):
        self.action = action
        self.accept()


# =================================================================================================================
# Sign in
# =================================================================================================================
class LoginDialog(QDialog):
    def __init__(self, path, plugin=None, parent=None):
        super().__init__(parent)
        self.path, self.plugin, self.session = path, plugin, None
        self.setWindowTitle(tr("Locked database"))
        style_dialog(self, plugin)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)
        v.addWidget(header(tr("Locked database"), os.path.basename(path), "lock", plugin))
        self.stack = QStackedWidget()
        # sign in
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(0, 0, 0, 0)
        self.user = QLineEdit(QgsSettings().value(KEY + "last_user", "") or "")
        self.user.setPlaceholderText(tr("User name"))
        self.pw = PasswordField(tr("Password"), plugin)
        f.addRow(tr("User"), self.user)
        f.addRow(tr("Password"), self.pw)
        self.stack.addWidget(w)
        # recovery
        w = QWidget()
        rv = QVBoxLayout(w)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(label(tr("Type the recovery key you saved when the database was locked, then choose a new "
                              "administrator password.")))
        self.code = QLineEdit()
        self.code.setPlaceholderText("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
        self.code.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.new1 = PasswordField(tr("New administrator password"), plugin)
        self.new2 = PasswordField(tr("The same password again"), plugin)
        self.meter = StrengthMeter(passwords.MIN_ADMIN)
        self.new1.changed.connect(self.meter.update_for)
        rf = QFormLayout()
        rf.addRow(tr("Recovery key"), self.code)
        rf.addRow(tr("New password"), self.new1)
        rf.addRow("", self.meter)
        rf.addRow(tr("Confirm"), self.new2)
        rv.addLayout(rf)
        self.stack.addWidget(w)
        v.addWidget(self.stack)
        self.error = label("", "SecError")
        self.error.hide()
        v.addWidget(self.error)
        self.switch = QPushButton(tr("Lost every password? Use the recovery key"))
        self.switch.setFlat(True)
        self.switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch.clicked.connect(self._toggle)
        v.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignLeading)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        self.ok = primary(tr("Open"))
        row.addWidget(cancel)
        row.addWidget(self.ok)
        v.addLayout(row)
        cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self._open)
        self.resize(460, 0)
        (self.pw.edit if self.user.text() else self.user).setFocus()

    def _toggle(self):
        rec = self.stack.currentIndex() == 0
        self.stack.setCurrentIndex(1 if rec else 0)
        self.switch.setText(tr("Back to signing in") if rec else tr("Lost every password? Use the recovery key"))
        self.ok.setText(tr("Unlock with the key") if rec else tr("Open"))
        self.error.hide()

    def show_error(self, text):
        self.error.setText(text)
        self.error.show()

    def _open(self):
        from . import container

        try:
            lf = container.LockedFile(self.path)
            if self.stack.currentIndex() == 0:
                s = _busy(lf.unlock, self.user.text(), self.pw.text())
                QgsSettings().setValue(KEY + "last_user", self.user.text().strip())
            else:
                why = passwords.problem(self.new1.text(), passwords.MIN_ADMIN)
                if why:
                    return self.show_error(tr(why).format(n=passwords.MIN_ADMIN))
                if self.new1.text() != self.new2.text():
                    return self.show_error(tr("The two passwords are not the same."))
                s = _busy(lf.recover, self.code.text())
                admin = next(u["uid"] for u in s.users() if u["role"] == "admin")
                _busy(s.reset_password, admin, self.new1.text())
                _busy(s.save)
                s.log("recovered")
        except container.LockError as e:
            self.pw.setText("")
            return self.show_error(lock_error_text(e))
        except OSError as e:
            return self.show_error(str(e))
        self.session = s
        self.accept()


# =================================================================================================================
# A user: name, password, end date, export / print / copy
# =================================================================================================================
class UserDialog(QDialog):
    def __init__(self, plugin=None, parent=None, title=None, name="", ask_name=True, user=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle(title or tr("Add a user"))
        style_dialog(self, plugin)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.addWidget(header(title or tr("Add a user"), tr("The user signs in with this name and password. Give him "
                                                         "the password yourself, not with the file."), "user", plugin))
        f = QFormLayout()
        self.name = QLineEdit(name)
        self.name.setEnabled(ask_name)
        self.pw1 = PasswordField(tr("Password"), plugin)
        self.pw2 = PasswordField(tr("The same password again"), plugin)
        self.meter = StrengthMeter(passwords.MIN_USER)
        self.pw1.changed.connect(self.meter.update_for)
        f.addRow(tr("Name"), self.name)
        f.addRow(tr("Password"), self.pw1)
        f.addRow("", self.meter)
        f.addRow(tr("Confirm"), self.pw2)
        self.has_end = QCheckBox(tr("Access ends on"))
        self.end = QDateEdit(QDate.currentDate().addMonths(3))
        self.end.setCalendarPopup(True)
        self.end.setEnabled(False)
        self.has_end.toggled.connect(self.end.setEnabled)
        row = QHBoxLayout()
        row.addWidget(self.has_end)
        row.addWidget(self.end)
        row.addStretch(1)
        self.show_extra = user is None and ask_name
        if self.show_extra:
            f.addRow("", row)
            self.export = QCheckBox(tr("May export"))
            self.print_ = QCheckBox(tr("May print"))
            self.print_.setChecked(True)
            self.copy = QCheckBox(tr("May copy features"))
            row2 = QHBoxLayout()
            for cb in (self.export, self.print_, self.copy):
                row2.addWidget(cb)
            row2.addStretch(1)
            f.addRow("", row2)
        v.addLayout(f)
        if self.show_extra:
            v.addWidget(label(tr("The end date is checked against this computer's clock, and export, print and copy "
                                 "by Biruni's interface: they are not encryption.")))
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        ok = primary(tr("OK"))
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self._ok)
        self.resize(480, 0)

    def _ok(self):
        if not self.name.text().strip():
            return QMessageBox.information(self, self.windowTitle(), tr("Write the user's name."))
        why = passwords.problem(self.pw1.text(), passwords.MIN_USER)
        if why:
            return QMessageBox.information(self, self.windowTitle(), tr(why).format(n=passwords.MIN_USER))
        if self.pw1.text() != self.pw2.text():
            return QMessageBox.information(self, self.windowTitle(), tr("The two passwords are not the same."))
        self.accept()

    def values(self):
        out = {"name": self.name.text().strip(), "password": self.pw1.text(),
               "expires": self.end.date().toString("yyyy-MM-dd") if self.has_end.isChecked() else ""}
        if self.show_extra:
            out["can"] = {"export": self.export.isChecked(), "print": self.print_.isChecked(),
                          "copy": self.copy.isChecked()}
        return out


# =================================================================================================================
# Users and permissions (administrator)
# =================================================================================================================
_RIGHT_ICONS = (("v", "stops/eye", "View"), ("a", "square-plus", "Add features"), ("e", "pencil", "Edit features"),
                ("d", "trash", "Delete features"))


class RightsCell(QWidget):
    """Four toggles: view, add, edit, delete. Adding, editing or deleting turns view on; view off turns all off."""

    def __init__(self, rights, on_change, plugin=None):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 0, 4, 0)
        h.setSpacing(2)
        self.on_change = on_change
        self.buttons = {}
        for key, ic, tip in _RIGHT_ICONS:
            b = QToolButton()
            b.setObjectName("SecRight")
            b.setCheckable(True)
            b.setIcon(icon(ic, plugin))
            b.setIconSize(QSize(16, 16))
            b.setToolTip(tr(tip))
            b.setAutoRaise(True)
            b.toggled.connect(lambda on, k=key: self._toggled(k, on))
            h.addWidget(b)
            self.buttons[key] = b
        h.addStretch(1)
        self.set(rights)

    def set(self, rights):
        for k, b in self.buttons.items():
            b.blockSignals(True)
            b.setChecked(k in rights)
            b.blockSignals(False)

    def rights(self):
        return "".join(k for k, b in self.buttons.items() if b.isChecked())

    def _toggled(self, key, on):
        r = set(self.rights())
        if key == "v" and not on:
            r = set()
        elif key != "v" and on:
            r.add("v")
        self.set("".join(r))
        self.on_change(self.rights())


_ACTIONS = {"create": "Locked the database", "open": "Opened", "save": "Saved changes",
            "merge": "Merged a returned file", "send": "Wrote a copy for users", "recovered": "Used the recovery key",
            "admin_save": "Saved a new version", "unreadable": "Unreadable entry"}


def _why(reason):
    return {"no_add": tr("no right to add"), "no_edit": tr("no right to edit"), "no_delete": tr("no right to delete"),
            "no_view": tr("no right to see this layer"), "hidden_field": tr("it touches a hidden field"),
            "user_removed": tr("this user was removed or disabled"),
            "no_layer": tr("the layer is no longer in the database"), "unreadable": tr("unreadable"),
            "signature": tr("forged or damaged"), "schema": tr("only the administrator changes fields")}.get(reason, reason)


def report_line(r):
    u, n, layer = r["user"], r["count"], r["layer"]
    if r["ok"]:
        return {"add": tr("{user}: {n} features added to {layer}"), "edit": tr("{user}: {n} features edited in {layer}"),
                "delete": tr("{user}: {n} features deleted from {layer}")}.get(r["op"], "{user}").format(
            user=u, n=n, layer=layer)
    if r["op"] == "all" and r["reason"] == "signature":
        return tr("A change set in the name of {user} for {layer} has a signature that does not match - refused "
                  "(forged or damaged)").format(user=u, layer=layer)
    if r["op"] == "schema":
        return tr("{user} tried to change the fields of {layer} - refused (only the administrator changes fields)").format(
            user=u, layer=layer)
    if r["op"] == "missing":
        return tr("{user}: {n} changes to {layer} concern features that no longer exist - skipped").format(
            user=u, n=n, layer=layer)
    text = {"add": tr("{user} tried to add {n} features to {layer} - refused ({why})"),
            "edit": tr("{user} tried to edit {n} features of {layer} - refused ({why})"),
            "delete": tr("{user} tried to delete {n} features of {layer} - refused ({why})"),
            "all": tr("{user}: a change set for {layer} was refused ({why})")}.get(r["op"], "{user}")
    return text.format(user=u, n=n, layer=layer, why=_why(r["reason"]))


def audit_details(e):
    d = e.get("details") or {}
    a = e.get("action")
    if a == "save":
        parts = []
        for x in d.get("layers", []):
            parts.append(tr("{layer}: +{a} ~{e} -{d}").format(layer=x.get("layer"), a=x.get("adds", 0),
                                                             e=x.get("edits", 0), d=x.get("deletes", 0)))
        return "; ".join(parts)
    if a == "merge":
        return tr("{file}: {ok} applied, {bad} refused").format(file=d.get("file", ""), ok=d.get("applied", 0),
                                                                bad=d.get("refused", 0))
    if a == "send":
        return tr("for {names} ({file})").format(names=", ".join(d.get("to", [])), file=d.get("file", ""))
    if a == "create":
        return ", ".join(d.get("layers", []))
    return ""


class UsersDialog(QDialog):
    def __init__(self, session, plugin=None, parent=None, on_saved=None):
        super().__init__(parent)
        self.s, self.plugin, self.on_saved = session, plugin, on_saved
        self.setWindowTitle(tr("Users and permissions"))
        style_dialog(self, plugin)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        v.setSpacing(10)
        v.addWidget(header(tr("Users and permissions"),
                           tr("{file} - you are the administrator ({name})").format(
                               file=os.path.basename(session.path), name=session.name), "users", plugin))
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_users(), icon("user", plugin), tr("Users"))
        self.tabs.addTab(self._tab_rights(), icon("shield", plugin), tr("Permissions"))
        self.tabs.addTab(self._tab_send(), icon("send", plugin), tr("Send and merge"))
        self.tabs.addTab(self._tab_audit(), icon("history", plugin), tr("Activity"))
        v.addWidget(self.tabs, 1)
        v.addWidget(honest_note())
        row = QHBoxLayout()
        self.state = label("")
        row.addWidget(self.state, 1)
        close = QPushButton(tr("Close"))
        self.save_btn = primary(tr("Save"))
        row.addWidget(close)
        row.addWidget(self.save_btn)
        v.addLayout(row)
        close.clicked.connect(self.close)
        self.save_btn.clicked.connect(self.save)
        self.resize(900, 680)
        self.refresh()

    # ---- users -------------------------------------------------------------------------------------------------------
    def _tab_users(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.utable = QTableWidget(0, 7)
        self.utable.setHorizontalHeaderLabels([tr("Name"), tr("Role"), tr("Status"), tr("Access ends"), tr("Export"),
                                               tr("Print"), tr("Copy")])
        self.utable.verticalHeader().hide()
        self.utable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.utable.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.utable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.utable.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.utable, 1)
        row = QHBoxLayout()
        add = primary(tr("Add a user…"))
        add.setIcon(icon("user", self.plugin, "#FFFFFF"))
        reset = QPushButton(icon("key", self.plugin), tr("Reset password…"))
        ends = QPushButton(icon("calendar", self.plugin), tr("Access ends…"))
        dis = QPushButton(tr("Disable"))
        rem = QPushButton(icon("trash", self.plugin), tr("Remove"))
        rem.setObjectName("SecDanger")
        for b in (add, reset, ends, dis, rem):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(label(tr("A disabled or removed user loses his keys in the next version of the file. A copy he "
                             "already has still opens with his old password - send everyone a new copy.")))
        add.clicked.connect(self._add)
        reset.clicked.connect(self._reset)
        ends.clicked.connect(self._ends)
        dis.clicked.connect(self._disable)
        rem.clicked.connect(self._remove)
        return w

    def _current_user(self):
        r = self.utable.currentRow()
        if r < 0:
            QMessageBox.information(self, self.windowTitle(), tr("Choose a user in the list."))
            return None
        return self.utable.item(r, 0).data(Qt.ItemDataRole.UserRole)

    def _add(self):
        d = UserDialog(self.plugin, self)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        val = d.values()
        from .container import LockError

        try:
            _busy(self.s.add_user, val["name"], val["password"], {}, val.get("can"), val["expires"])
        except LockError as e:
            return QMessageBox.warning(self, self.windowTitle(), lock_error_text(e))
        self.refresh()
        self.tabs.setCurrentIndex(1)

    def _reset(self):
        uid = self._current_user()
        if not uid:
            return
        u = next(x for x in self.s.users() if x["uid"] == uid)
        d = UserDialog(self.plugin, self, tr("New password for {name}").format(name=u["name"]), u["name"], False, u)
        d.has_end.hide()
        d.end.hide()
        if d.exec() == QDialog.DialogCode.Accepted:
            _busy(self.s.reset_password, uid, d.values()["password"])
            self.refresh()

    def _ends(self):
        uid = self._current_user()
        if not uid:
            return
        u = next(x for x in self.s.users() if x["uid"] == uid)
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Access ends"))
        style_dialog(dlg, self.plugin)
        lay = QVBoxLayout(dlg)
        cb = QCheckBox(tr("Access ends on"))
        de = QDateEdit(QDate.fromString(u["expires"], "yyyy-MM-dd") if u["expires"] else QDate.currentDate().addMonths(3))
        de.setCalendarPopup(True)
        cb.setChecked(bool(u["expires"]))
        lay.addWidget(label(u["name"], "SecSub"))
        row = QHBoxLayout()
        row.addWidget(cb)
        row.addWidget(de)
        lay.addLayout(row)
        ok = primary(tr("OK"))
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.s.set_expiry(uid, de.date().toString("yyyy-MM-dd") if cb.isChecked() else "")
            self.refresh()

    def _disable(self):
        uid = self._current_user()
        if uid:
            u = next(x for x in self.s.users() if x["uid"] == uid)
            if u["role"] == "admin":
                return
            if u["disabled"]:
                return self._reset()
            self.s.disable(uid)
            self.refresh()

    def _remove(self):
        uid = self._current_user()
        if not uid:
            return
        u = next(x for x in self.s.users() if x["uid"] == uid)
        if u["role"] == "admin":
            return
        if QMessageBox.question(self, self.windowTitle(), tr("Remove {name}? His changes not yet merged will be "
                                                             "refused.").format(name=u["name"])) \
                == QMessageBox.StandardButton.Yes:
            self.s.remove_user(uid)
            self.refresh()

    # ---- rights --------------------------------------------------------------------------------------------------------
    def _tab_rights(self):
        w = QWidget()
        v = QVBoxLayout(w)
        legend = QHBoxLayout()
        for _, ic, tip in _RIGHT_ICONS:
            pic = QLabel()
            pic.setPixmap(icon(ic, self.plugin).pixmap(16, 16))
            legend.addWidget(pic)
            legend.addWidget(label(tr(tip), "SecHint", False))
            legend.addSpacing(10)
        legend.addStretch(1)
        v.addLayout(legend)
        split = QSplitter()
        self.grid = QTableWidget(0, 0)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.currentCellChanged.connect(lambda r, c, *_: self._show_fields(r, c))
        split.addWidget(self.grid)
        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(6, 0, 0, 0)
        self.fields_title = label(tr("Hidden fields"), "SecSub")
        self.fields_hint = label(tr("Choose a cell: the fields ticked here are hidden from that user. Their values are "
                                    "encrypted with keys he does not receive."))
        self.fields = QListWidget()
        self.fields.itemChanged.connect(self._fields_changed)
        sv.addWidget(self.fields_title)
        sv.addWidget(self.fields_hint)
        sv.addWidget(self.fields, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        v.addWidget(split, 1)
        return w

    def _grid_users(self):
        return [u for u in self.s.users() if u["role"] != "admin" and not u["disabled"]]

    def _fill_grid(self):
        users, layers = self._grid_users(), self.s.layers()
        self.grid.blockSignals(True)
        self.grid.clear()
        self.grid.setRowCount(len(users))
        self.grid.setColumnCount(len(layers))
        self.grid.setHorizontalHeaderLabels([x["name"] for x in layers])
        self.grid.setVerticalHeaderLabels([u["name"] for u in users])
        for r, u in enumerate(users):
            for c, x in enumerate(layers):
                cell = RightsCell(u["rights"].get(x["id"], ""), lambda rights, uid=u["uid"], lid=x["id"]:
                                  self._rights_changed(uid, lid, rights), self.plugin)
                self.grid.setCellWidget(r, c, cell)
                hid = u["hidden"].get(x["id"], [])
                item = QTableWidgetItem()
                item.setToolTip(tr("Hidden fields: {names}").format(names=", ".join(hid)) if hid else "")
                self.grid.setItem(r, c, item)
        self.grid.resizeColumnsToContents()
        for c in range(self.grid.columnCount()):
            self.grid.setColumnWidth(c, max(self.grid.columnWidth(c), 150))
        self.grid.verticalHeader().setDefaultSectionSize(34)
        self.grid.blockSignals(False)
        self._show_fields(self.grid.currentRow(), self.grid.currentColumn())

    def _rights_changed(self, uid, lid, rights):
        self.s.set_rights(uid, lid, rights)
        self._dirty()

    def _show_fields(self, r, c):
        users, layers = self._grid_users(), self.s.layers()
        self.fields.blockSignals(True)
        self.fields.clear()
        if r < 0 or c < 0 or r >= len(users) or c >= len(layers):
            self.fields_title.setText(tr("Hidden fields"))
            self.fields.blockSignals(False)
            return
        u, x = users[r], layers[c]
        self._field_cell = (u["uid"], x["id"])
        self.fields_title.setText(tr("Fields of {layer} hidden from {user}").format(layer=x["name"], user=u["name"]))
        hidden = set(u["hidden"].get(x["id"], []))
        for n in x["fields"]:
            it = QListWidgetItem(icon("eye-off", self.plugin) if n in hidden else icon("stops/eye", self.plugin), n)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if n in hidden else Qt.CheckState.Unchecked)
            self.fields.addItem(it)
        self.fields.blockSignals(False)

    def _fields_changed(self, _item):
        uid, lid = self._field_cell
        names = [self.fields.item(i).text() for i in range(self.fields.count())
                 if self.fields.item(i).checkState() == Qt.CheckState.Checked]
        self.s.set_hidden(uid, lid, names)
        self.fields.blockSignals(True)
        for i in range(self.fields.count()):
            it = self.fields.item(i)
            it.setIcon(icon("eye-off" if it.checkState() == Qt.CheckState.Checked else "stops/eye", self.plugin))
        self.fields.blockSignals(False)
        self._dirty()

    # ---- send and merge -------------------------------------------------------------------------------------------------
    def _tab_send(self):
        w = QWidget()
        v = QVBoxLayout(w)
        c, cv = card()
        cv.addWidget(label(tr("Send a copy"), "SecSub"))
        cv.addWidget(label(tr("Writes a copy of the saved database for the users you tick. Each one opens it with his "
                              "own name and password and sees only what his rights allow.")))
        self.send_users = QListWidget()
        self.send_users.setMaximumHeight(120)
        cv.addWidget(self.send_users)
        self.only_visible = QCheckBox(tr("Only what they can see (the other layers are not in the copy at all)"))
        self.only_visible.setChecked(True)
        cv.addWidget(self.only_visible)
        b = primary(tr("Write the copy…"))
        b.setIcon(icon("send", self.plugin, "#FFFFFF"))
        b.clicked.connect(self._send)
        cv.addWidget(b, 0, Qt.AlignmentFlag.AlignLeading)
        v.addWidget(c)
        c, cv = card()
        cv.addWidget(label(tr("Merge a returned file"), "SecSub"))
        cv.addWidget(label(tr("Each change in the returned copy is checked against the signature of the user who made it "
                              "and against his rights. Allowed changes are applied; the others are refused and listed "
                              "here and in the activity log.")))
        b = QPushButton(icon("file-diff", self.plugin), tr("Choose the returned file…"))
        b.clicked.connect(self._merge)
        cv.addWidget(b, 0, Qt.AlignmentFlag.AlignLeading)
        self.report = QListWidget()
        cv.addWidget(self.report, 1)
        v.addWidget(c, 1)
        return w

    def _send(self):
        uids = [self.send_users.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.send_users.count())
                if self.send_users.item(i).checkState() == Qt.CheckState.Checked]
        if not uids:
            return QMessageBox.information(self, self.windowTitle(), tr("Tick the users the copy is for."))
        names = [u["name"] for u in self.s.users() if u["uid"] in uids]
        default = os.path.join(os.path.dirname(self.s.path), os.path.splitext(os.path.basename(self.s.path))[0]
                               + " - " + ", ".join(names) + ".bgpkg")
        path, _ = QFileDialog.getSaveFileName(self, tr("Write the copy…"), default, tr("Locked database") + " (*.bgpkg)")
        if not path:
            return
        _busy(self.s.send_to, uids, path, self.only_visible.isChecked())
        self.state.setText(tr("Copy written: {file}").format(file=os.path.basename(path)))
        self.refresh()

    def _merge(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose the returned file…"), os.path.dirname(self.s.path),
                                              tr("Locked database") + " (*.bgpkg)")
        if path:
            self.merge_file(path)

    def merge_file(self, path):
        from .container import LockError

        try:
            rep = _busy(self.s.merge, path)
            _busy(self.s.save)
        except LockError as e:
            return QMessageBox.warning(self, self.windowTitle(), lock_error_text(e))
        self.show_report(rep)
        if self.on_saved:
            self.on_saved()
        self.refresh()

    def show_report(self, rep):
        t = tokens(self.plugin)
        self.report.clear()
        if not rep:
            self.report.addItem(tr("Nothing new to merge in this file."))
        for r in rep:
            it = QListWidgetItem(icon("shield-check" if r["ok"] else "shield", self.plugin,
                                      t["accent"] if r["ok"] else "#D64545"), report_line(r))
            self.report.addItem(it)

    # ---- activity ---------------------------------------------------------------------------------------------------------
    def _tab_audit(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.atable = QTableWidget(0, 5)
        self.atable.setHorizontalHeaderLabels([tr("When"), tr("Who"), tr("What"), tr("Details"), tr("Signature")])
        self.atable.verticalHeader().hide()
        self.atable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.atable.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.atable, 1)
        v.addWidget(label(tr("Each entry is signed by the person who made it and only you can read it. An entry "
                             "removed from the middle of the file is noticed; entries cut from the end of a copy "
                             "cannot be noticed offline.")))
        return w

    def _fill_audit(self):
        log = list(reversed(self.s.read_audit()))
        self.atable.setRowCount(len(log))
        t = tokens(self.plugin)
        for r, e in enumerate(log):
            when = dt.datetime.fromtimestamp(e.get("t", 0)).strftime("%Y-%m-%d %H:%M") if e.get("t") else ""
            sig_ok = e.get("signed") and e.get("chain", True)
            sig = tr("Signed") if sig_ok else (tr("Signed - but an earlier record was removed") if e.get("signed")
                                               else tr("NOT verified"))
            vals = (when, e.get("user", "?"), tr(_ACTIONS.get(e.get("action"), e.get("action") or "")),
                    audit_details(e), sig)
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 4:
                    it.setIcon(icon("shield-check" if sig_ok else "shield", self.plugin,
                                    t["accent"] if sig_ok else "#D64545"))
                self.atable.setItem(r, c, it)
        self.atable.resizeColumnsToContents()
        self.atable.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    # ---- state -------------------------------------------------------------------------------------------------------------
    def refresh(self):
        users = self.s.users()
        self.utable.setRowCount(len(users))
        t = tokens(self.plugin)
        for r, u in enumerate(users):
            name = QTableWidgetItem(icon("key" if u["role"] == "admin" else "user", self.plugin), u["name"])
            name.setData(Qt.ItemDataRole.UserRole, u["uid"])
            name.setToolTip(tr("Key fingerprint: {fp}").format(fp=u["fingerprint"]))
            role = tr("Administrator") if u["role"] == "admin" else tr("User")
            status = tr("Disabled") if u["disabled"] else tr("Active")
            if u["expires"] and u["expires"] < dt.date.today().isoformat():
                status = tr("Access ended")
            ends = format_date(u["expires"]) if u["expires"] else "—"
            self.utable.setItem(r, 0, name)
            for c, val in ((1, role), (2, status), (3, ends)):
                self.utable.setItem(r, c, QTableWidgetItem(val))
            for c, key in ((4, "export"), (5, "print"), (6, "copy")):
                cb = QCheckBox()
                cb.setChecked(u["role"] == "admin" or bool(u["can"].get(key)))
                cb.setEnabled(u["role"] != "admin" and not u["disabled"])
                cb.toggled.connect(lambda on, uid=u["uid"], k=key: (self.s.set_can(uid, **{k: on}), self._dirty()))
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                hl.addWidget(cb)
                self.utable.setCellWidget(r, c, holder)
        self.utable.resizeColumnsToContents()
        self.utable.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._fill_grid()
        self.send_users.clear()
        for u in users:
            if u["role"] == "admin" or u["disabled"]:
                continue
            it = QListWidgetItem(icon("user", self.plugin), u["name"])
            it.setData(Qt.ItemDataRole.UserRole, u["uid"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.send_users.addItem(it)
        self._fill_audit()
        self._dirty()

    def _dirty(self):
        d = self.s.model_dirty
        self.save_btn.setEnabled(True)
        self.state.setText(tr("Changes not saved yet - users get them in the next copy you send.") if d else
                           tr("Version {n}, saved.").format(n=self.s.m.get("version", 1)))

    def save(self):
        from .container import LockError

        try:
            rep = _busy(self.s.save)
        except LockError as e:
            return QMessageBox.warning(self, self.windowTitle(), lock_error_text(e))
        if rep:
            self.show_report(rep)
            self.tabs.setCurrentIndex(2)
        if self.on_saved:
            self.on_saved()
        self.refresh()

    def closeEvent(self, e):  # noqa: N802
        if self.s.model_dirty and not self.s.closed:
            ans = QMessageBox.question(self, self.windowTitle(), tr("Save the changes to users and permissions?"),
                                       QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                       | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel:
                e.ignore()
                return
            if ans == QMessageBox.StandardButton.Save:
                self.save()
        super().closeEvent(e)


# =================================================================================================================
# Lock this project / remove the lock
# =================================================================================================================
class LockProjectDialog(QDialog):
    def __init__(self, project_path, plugin=None, parent=None):
        super().__init__(parent)
        from . import project_lock

        self.plugin, self.project_path = plugin, project_path
        self.setWindowTitle(tr("Lock this project with a password"))
        style_dialog(self, plugin)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)
        v.addWidget(header(tr("Lock this project with a password"),
                           tr("The project and the data files you tick are encrypted into one file. It opens only "
                              "with the password, and its data stays in memory while you work."), "lock", plugin))
        c, cv = card()
        cv.addWidget(label(tr("Data files to lock with the project"), "SecSub"))
        self.files = QListWidget()
        self.files.setMinimumHeight(110)
        for path, inside in project_lock.project_data_files(project_path):
            it = QListWidgetItem(icon("database", plugin), os.path.relpath(path, os.path.dirname(project_path))
                                 if inside else path)
            it.setData(Qt.ItemDataRole.UserRole, path)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if inside else Qt.CheckState.Unchecked)
            if not inside:
                it.setToolTip(tr("Outside the project's folder"))
            self.files.addItem(it)
        cv.addWidget(self.files)
        cv.addWidget(label(tr("Layers whose files are not ticked keep reading their files, unlocked.")))
        f = QFormLayout()
        self.out = QLineEdit(os.path.splitext(project_path)[0] + ".bproj")
        self.pw1 = PasswordField(tr("Password"), plugin)
        self.pw2 = PasswordField(tr("The same password again"), plugin)
        self.meter = StrengthMeter(passwords.MIN_ADMIN)
        self.pw1.changed.connect(self.meter.update_for)
        f.addRow(tr("Locked file"), self.out)
        f.addRow(tr("Password"), self.pw1)
        f.addRow("", self.meter)
        f.addRow(tr("Confirm"), self.pw2)
        cv.addLayout(f)
        self.open_after = QCheckBox(tr("Open the locked project now"))
        self.open_after.setChecked(True)
        cv.addWidget(self.open_after)
        v.addWidget(c)
        warn, wv = card("SecWarn")
        wv.addWidget(label(tr("There is no recovery key for a locked project"), "SecSub"))
        wv.addWidget(label(tr("If you forget this password, nobody can open the project - not even Biruni. The "
                              "original project and files are not changed or deleted: delete them yourself once you "
                              "have checked that the locked project opens.")))
        v.addWidget(warn)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        ok = primary(tr("Lock"))
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self._lock)
        self.resize(600, 0)
        self.password = ""

    def chosen(self):
        return [self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())
                if self.files.item(i).checkState() == Qt.CheckState.Checked]

    def _lock(self):
        from . import project_lock

        why = passwords.problem(self.pw1.text(), passwords.MIN_ADMIN)
        if why:
            return QMessageBox.information(self, self.windowTitle(), tr(why).format(n=passwords.MIN_ADMIN))
        if self.pw1.text() != self.pw2.text():
            return QMessageBox.information(self, self.windowTitle(), tr("The two passwords are not the same."))
        out = self.out.text().strip()
        if not out.lower().endswith(".bproj"):
            out += ".bproj"
        try:
            _busy(project_lock.lock, self.project_path, self.chosen(), out, self.pw1.text())
        except Exception as e:  # noqa: BLE001
            return QMessageBox.critical(self, self.windowTitle(), str(e))
        self.out_path = out
        self.password = self.pw1.text()
        self.accept()


class RemoveLockDialog(QDialog):
    def __init__(self, path="", plugin=None, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle(tr("Remove the lock"))
        style_dialog(self, plugin)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.addWidget(header(tr("Remove the lock"), tr("Writes the project and its data, not encrypted, into a folder. "
                                                    "The locked file is kept."), "key", plugin))
        f = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit(path)
        pick = QPushButton(tr("Choose…"))
        pick.clicked.connect(self._pick)
        row.addWidget(self.path, 1)
        row.addWidget(pick)
        self.pw = PasswordField(tr("Password"), plugin)
        self.folder = QLineEdit(os.path.splitext(path)[0] + " - " + tr("unlocked") if path else "")
        f.addRow(tr("Locked project"), row)
        f.addRow(tr("Password"), self.pw)
        f.addRow(tr("Into the folder"), self.folder)
        v.addLayout(f)
        warn, wv = card("SecWarn")
        wv.addWidget(label(tr("The files written are not protected: anyone with access to this folder can read them.")))
        v.addWidget(warn)
        self.error = label("", "SecError")
        self.error.hide()
        v.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        ok = primary(tr("Remove the lock"))
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self._go)
        self.resize(560, 0)
        self.result_project = None

    def _pick(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Locked project"), self.path.text(),
                                           tr("Locked project") + " (*.bproj)")
        if p:
            self.path.setText(p)
            if not self.folder.text():
                self.folder.setText(os.path.splitext(p)[0] + " - " + tr("unlocked"))

    def _go(self):
        from . import project_lock

        try:
            lp = _busy(project_lock.LockedProject, self.path.text(), self.pw.text())
        except project_lock.ProjectLockError:
            self.error.setText(tr("Wrong password."))
            self.error.show()
            return
        except OSError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        try:
            self.result_project = _busy(lp.unlock_to, self.folder.text())
        finally:
            lp.close()
        self.accept()
