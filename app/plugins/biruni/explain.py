"""«What does this error mean?» - known error messages explained in plain words.

Warnings and errors written to the QGIS message log (QGIS, GDAL, PROJ, GEOS,
Processing, GRASS, Python) and failed Processing runs are matched against
explain_rules.RULES. A match shows a quiet message-bar item with a button
that opens the explanation, what to do, and a button that runs the fix. The
same kind of error is shown at most once a minute; the recent ones are kept
for the Learn panel (recent()).

    explainer = explain.install(plugin)   # in initGui
    explain.uninstall()                   # in unload
"""
import html
import os
import re
import time
from collections import deque

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import QObject, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QApplication

from .explain_rules import ACTION_LABELS, PATH, RULES
from .i18n import tr

APP_NAME = "Biruni GIS"
REPEAT_SECONDS = 60      # the same kind of error is announced once a minute
BAR_GAP_SECONDS = 5      # and at most one announcement every few seconds
MAX_TEXT = 4000

_COMPILED = [(r, [re.compile(p, re.IGNORECASE | re.DOTALL) for p in r["patterns"]]) for r in RULES]
_PATH = re.compile(PATH)
_TAGS = re.compile(r"<[^>]+>")
_instance = None


def clean(text):
    """Plain text of a log message (QGIS sometimes logs HTML)."""
    s = html.unescape(_TAGS.sub(" ", str(text or "")))
    return s.strip()[:MAX_TEXT]


def find(text):
    """The first rule matching the message, or None."""
    s = clean(text)
    if not s:
        return None
    for rule, patterns in _COMPILED:
        if any(p.search(s) for p in patterns):
            return rule
    return None


def path_in(text):
    """The first Windows file path named in the message, or ''."""
    for m in _PATH.finditer(clean(text)):
        p = (m.group("path") or m.group("path2") or "").rstrip(".,;:)")
        if p:
            return p
    return ""


def explain_text(text):
    """The explanation of a message in the interface language, or None."""
    rule = find(text)
    if rule is None:
        return None
    return {"id": rule["id"], "title": tr(rule["title"]), "cause": tr(rule["cause"]), "fix": tr(rule["fix"]),
            "action": rule["action"], "path": path_in(text)}


class Explainer(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.history = deque(maxlen=50)
        self._last_rule = {}       # rule id -> time it was last announced
        self._last_bar = 0.0
        self._hooked_dialogs = set()
        self._connected = False

    # ---- hooks ---------------------------------------------------------------------------------
    def install(self):
        # Messages arrive from worker threads too: queue them to this (main-thread) object.
        self._signal().connect(self._on_message, Qt.ConnectionType.QueuedConnection)
        app = QApplication.instance()
        if hasattr(app, "focusChanged"):
            app.focusChanged.connect(self._on_focus)
        self._connected = True
        return self

    def uninstall(self):
        if not self._connected:
            return
        try:
            self._signal().disconnect(self._on_message)
        except (TypeError, RuntimeError):
            pass
        try:
            QApplication.instance().focusChanged.disconnect(self._on_focus)
        except (TypeError, RuntimeError, AttributeError):
            pass
        self._connected = False

    @staticmethod
    def _signal():
        """QGIS 4 emits messageReceivedWithFormat for logged messages (the three-argument
        messageReceived stays silent)."""
        log = QgsApplication.messageLog()
        if hasattr(log, "messageReceivedWithFormat"):
            return log.messageReceivedWithFormat
        return log.messageReceived[str, str, Qgis.MessageLevel]

    def _on_message(self, message, tag, level, _format=None):
        try:
            if level not in (Qgis.MessageLevel.Warning, Qgis.MessageLevel.Critical):
                return
            self.handle(message, tag)
        except Exception:  # noqa: BLE001 - the explainer must never break the log
            pass

    def _on_focus(self, _old, new):
        """Failed runs in a Processing tool window: hook each window once."""
        try:
            if new is None:
                return
            win = new.window()
            if id(win) in self._hooked_dialogs or not hasattr(win, "algorithmFinished"):
                return
            self._hooked_dialogs.add(id(win))
            win.algorithmFinished.connect(lambda ok, _res, w=win: self._on_alg_finished(ok, w))
            win.destroyed.connect(lambda _=None, k=id(win): self._hooked_dialogs.discard(k))
        except Exception:  # noqa: BLE001
            pass

    def _on_alg_finished(self, ok, win):
        if ok:
            return
        try:
            from qgis.PyQt.QtWidgets import QTextEdit

            log = win.findChild(QTextEdit, "txtLog")
            text = log.toPlainText() if log is not None else ""
            if text:
                self.handle(text[-MAX_TEXT:], "Processing")
        except Exception:  # noqa: BLE001
            pass

    # ---- matching ------------------------------------------------------------------------------
    def handle(self, message, tag=""):
        """Explain one message: record it and announce it unless it was announced a moment ago.
        Returns the history entry, or None when nothing is known about the message."""
        rule = find(message)
        if rule is None:
            return None
        text = clean(message)
        now = time.time()
        entry = next((e for e in self.history if e["rule"]["id"] == rule["id"] and e["message"] == text), None)
        if entry is not None:
            entry["count"] += 1
            entry["time"] = now
            self.history.remove(entry)
        else:
            entry = {"rule": rule, "message": text, "tag": tag or "", "time": now, "count": 1,
                     "path": path_in(text)}
        self.history.appendleft(entry)
        last = self._last_rule.get(rule["id"])
        if last is not None and now - last < REPEAT_SECONDS:
            return entry
        if now - self._last_bar < BAR_GAP_SECONDS:
            return entry
        self._last_rule[rule["id"]] = now
        self._last_bar = now
        entry["announced"] = True
        self._announce(entry)
        return entry

    def recent(self, n=20):
        """The latest explained problems, newest first, for the Learn panel."""
        out = []
        for e in list(self.history)[:n]:
            r = e["rule"]
            out.append({"id": r["id"], "title": tr(r["title"]), "cause": tr(r["cause"]), "fix": tr(r["fix"]),
                        "action": r["action"], "message": e["message"], "time": e["time"], "count": e["count"],
                        "entry": e})
        return out

    # ---- interface -----------------------------------------------------------------------------
    def _announce(self, entry):
        iface = getattr(self.plugin, "iface", None)
        if iface is None:
            return
        from qgis.PyQt.QtWidgets import QPushButton

        bar = iface.messageBar()
        item = bar.createMessage(tr(entry["rule"]["title"]), "")
        button = QPushButton(tr("See why and how to fix"))
        button.clicked.connect(lambda _=False, e=entry: self.show_dialog(e))
        item.layout().addWidget(button)
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 12)

    def action_label(self, action):
        if not action:
            return ""
        if action in ACTION_LABELS:
            return tr(ACTION_LABELS[action])
        from .commands import CATALOG

        if action in CATALOG:
            return tr(CATALOG[action].label).replace("\n", " ")
        alg = QgsApplication.processingRegistry().algorithmById(action)
        return alg.displayName() if alg else ""

    def run_action(self, entry):
        action = entry["rule"]["action"]
        if action == "folder":
            path = entry.get("path") or ""
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            if folder and os.path.isdir(folder):
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            return
        from .commands import CATALOG

        runner = getattr(self.plugin, "runner", None)
        if runner is None:
            return
        if action in CATALOG:
            runner.execute(CATALOG[action])
        elif QgsApplication.processingRegistry().algorithmById(action) is not None:
            runner.open_algorithm(action)

    def show_dialog(self, entry):
        from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton,
                                         QVBoxLayout)

        rule = entry["rule"]
        dlg = QDialog(getattr(self.plugin, "mw", None))
        dlg.setWindowTitle(tr("What does this error mean?"))
        dlg.setMinimumWidth(520)
        lay = QVBoxLayout(dlg)
        title = QLabel(f"<b>{html.escape(tr(rule['title']))}</b>")
        lay.addWidget(title)
        for head, body in ((tr("What happened"), tr(rule["cause"])), (tr("What to do"), tr(rule["fix"]))):
            lab = QLabel(f"<p><b>{html.escape(head)}</b><br>{html.escape(body)}</p>")
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(lab)
        lay.addWidget(QLabel(tr("The original message:")))
        details = QPlainTextEdit(entry["message"])
        details.setReadOnly(True)
        details.setMaximumHeight(110)
        details.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        lay.addWidget(details)
        buttons = QDialogButtonBox()
        label = self.action_label(rule["action"])
        if label and not (rule["action"] == "folder" and not entry.get("path")):
            fix = QPushButton(label)
            fix.clicked.connect(lambda _=False: (dlg.accept(), self.run_action(entry)))
            buttons.addButton(fix, QDialogButtonBox.ButtonRole.AcceptRole)
        copy = QPushButton(tr("Copy details"))
        copy.clicked.connect(lambda _=False: QApplication.clipboard().setText(
            f"{tr(rule['title'])}\n\n{entry['message']}"))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        dlg.show()
        return dlg


def install(plugin):
    global _instance
    if _instance is None:
        _instance = Explainer(plugin).install()
    return _instance


def uninstall():
    global _instance
    if _instance is not None:
        _instance.uninstall()
        _instance = None


def recent(n=20):
    return _instance.recent(n) if _instance is not None else []
