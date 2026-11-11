"""Small building blocks of the locked-safe dialogs: the Biruni look (theme tokens), cards, password fields
with a strength meter, icons."""
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                                 QToolButton, QVBoxLayout, QWidget)

from ..i18n import is_rtl, tr
from . import passwords


def tokens(plugin=None):
    theme = getattr(plugin, "theme", None)
    if theme is not None:
        try:
            return theme.tokens()
        except Exception:  # noqa: BLE001
            pass
    from ..theme import TOKENS

    return TOKENS["light"]


def qss(t):
    return f"""
QDialog#BiruniSecure {{ background: {t['window']}; }}
#BiruniSecure QFrame#SecCard {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 10px; }}
#BiruniSecure QFrame#SecWarn {{ background: {'#3A2A10' if t['window'].startswith('#1') else '#FFF6E5'};
    border: 1px solid {t['amber']}; border-radius: 10px; }}
#BiruniSecure QFrame#SecHonest {{ background: {t['accent_soft']}; border: none; border-radius: 10px; }}
#BiruniSecure QLabel#SecTitle {{ font-size: 15pt; font-weight: 700; color: {t['text']}; }}
#BiruniSecure QLabel#SecSub {{ font-size: 11pt; font-weight: 600; color: {t['text']}; }}
#BiruniSecure QLabel#SecHint {{ color: {t['text_dim']}; }}
#BiruniSecure QLabel#SecError {{ color: #D64545; font-weight: 600; }}
#BiruniSecure QLabel#SecOk {{ color: {t['accent']}; font-weight: 600; }}
#BiruniSecure QLabel#SecStep {{ color: {t['caption']}; padding: 2px 8px; }}
#BiruniSecure QLabel#SecStepOn {{ color: {t['accent_text']}; background: {t['accent']}; border-radius: 9px;
    padding: 2px 10px; font-weight: 600; }}
#BiruniSecure QLabel#SecCode {{ font-family: Consolas, "Courier New", monospace; font-size: 17pt; font-weight: 700;
    color: {t['text']}; background: {t['base']}; border: 1px dashed {t['mid']}; border-radius: 8px; padding: 10px; }}
#BiruniSecure QPushButton {{ padding: 6px 14px; border-radius: 6px; }}
#BiruniSecure QPushButton#SecPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    font-weight: 600; }}
#BiruniSecure QPushButton#SecPrimary:hover {{ background: {t['accent_hover']}; }}
#BiruniSecure QPushButton#SecPrimary:disabled {{ background: {t['mid']}; color: {t['text_disabled']}; }}
#BiruniSecure QPushButton#SecDanger {{ color: #C23B3B; }}
#BiruniSecure QToolButton#SecRight {{ border: 1px solid transparent; border-radius: 5px; padding: 2px; }}
#BiruniSecure QToolButton#SecRight:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; }}
#BiruniSecure QProgressBar {{ border: none; background: {t['midlight']}; border-radius: 3px; max-height: 6px; }}
"""


def icon(name, plugin=None, color=None):
    from .. import icons

    t = tokens(plugin)
    return icons.icon(name, color or t["icon"], t["icon_disabled"], t["accent"])


def style_dialog(dlg, plugin=None):
    dlg.setObjectName("BiruniSecure")
    dlg.setStyleSheet(qss(tokens(plugin)))
    dlg.setLayoutDirection(Qt.LayoutDirection.RightToLeft if is_rtl() else Qt.LayoutDirection.LeftToRight)


def card(kind="SecCard"):
    f = QFrame()
    f.setObjectName(kind)
    v = QVBoxLayout(f)
    v.setContentsMargins(14, 12, 14, 12)
    v.setSpacing(8)
    return f, v


def label(text, kind="SecHint", wrap=True):
    lb = QLabel(text)
    lb.setObjectName(kind)
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.TextFormat.PlainText if "<" not in text else Qt.TextFormat.RichText)
    return lb


def primary(text):
    b = QPushButton(text)
    b.setObjectName("SecPrimary")
    b.setDefault(True)
    return b


def header(title, subtitle, icon_name="lock", plugin=None):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    pic = QLabel()
    pic.setPixmap(icon(icon_name, plugin, tokens(plugin)["accent"]).pixmap(40, 40))
    pic.setFixedSize(46, 46)
    pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
    h.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
    v = QVBoxLayout()
    v.setSpacing(2)
    v.addWidget(label(title, "SecTitle"))
    if subtitle:
        v.addWidget(label(subtitle))
    h.addLayout(v, 1)
    return w


class PasswordField(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, placeholder="", plugin=None):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.eye = QToolButton()
        self.eye.setCheckable(True)
        self.eye.setIcon(icon("stops/eye", plugin))
        self.eye.setToolTip(tr("Show the password"))
        self.eye.setAutoRaise(True)
        self.eye.toggled.connect(lambda on: self.edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        h.addWidget(self.edit, 1)
        h.addWidget(self.eye)
        self.edit.textChanged.connect(self.changed.emit)

    def text(self):
        return self.edit.text()

    def setText(self, text):  # noqa: N802
        self.edit.setText(text)


class StrengthMeter(QWidget):
    """A bar and a sentence: how hard the password is to guess, and why it is refused if it is."""

    def __init__(self, minimum=passwords.MIN_USER):
        super().__init__()
        self.minimum = minimum
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        self.bar = QProgressBar()
        self.bar.setRange(0, 4)
        self.bar.setTextVisible(False)
        self.text = label("", "SecHint")
        v.addWidget(self.bar)
        v.addWidget(self.text)
        self.update_for("")

    def update_for(self, pw):
        s = passwords.score(pw) if pw else 0
        self.bar.setValue(max(s, 1 if pw else 0))
        color = ("#D64545", "#D64545", "#D97706", "#2E9E6B", "#0E7C74")[s]
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")
        word = (tr("Very weak"), tr("Weak"), tr("Fair"), tr("Good"), tr("Strong"))[s]
        why = passwords.problem(pw, self.minimum) if pw else ""
        if why:
            why = tr(why).format(n=self.minimum)
        self.text.setText(f"{tr('Strength')}: {word}" + (f" - {why}" if why else "") if pw else
                          tr("At least {n} characters. A sentence of several words is easy to remember and hard "
                             "to guess.").format(n=self.minimum))
        return not why


def ask_password(parent, title, text, plugin=None, minimum=0):
    """A small password prompt; returns the password or None."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    style_dialog(dlg, plugin)
    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 16, 18, 16)
    v.addWidget(header(title, text, "key", plugin))
    pf = PasswordField(tr("Password"), plugin)
    v.addWidget(pf)
    err = label("", "SecError")
    err.hide()
    v.addWidget(err)
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton(tr("Cancel"))
    ok = primary(tr("Open"))
    row.addWidget(cancel)
    row.addWidget(ok)
    v.addLayout(row)
    cancel.clicked.connect(dlg.reject)
    ok.clicked.connect(dlg.accept)
    dlg.resize(420, 0)
    dlg.error = err
    dlg.field = pf
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return pf.text()
