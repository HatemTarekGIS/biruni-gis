"""Who is using this file? ("Cannot acquire a lock", "file is being used by another process").

Asks the Windows Restart Manager which programs hold open handles on the files of a dataset - a shapefile
and its sidecars, a GeoPackage with its -wal/-shm journal, every file of a File Geodatabase folder, an Excel
workbook - and reads the lock files programs leave next to the data (the ``*.lock`` files of a File
Geodatabase, Excel's ``~$`` owner file with the user name). Biruni never closes another program: it tells
the user which one to close and how.
"""
import ctypes
import glob
import os
import re
import sqlite3
from ctypes import wintypes

from qgis.core import QgsProviderRegistry
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                                 QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .i18n import tr
from .version import APP_NAME

MAX_FILES = 3000            # a File Geodatabase can hold thousands of files; the Restart Manager takes them all
SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
SQLITE_EXT = (".gpkg", ".sqlite", ".db", ".mbtiles")

# Restart Manager application types (codes; labels in kind_label)
APP_TYPES = {0: "unknown", 1: "window", 2: "program", 3: "service", 4: "explorer", 5: "console", 1000: "critical"}

# Program file names (lower case, without .exe) -> advice key (texts in advice_for)
PROGRAMS = [
    (("excel",), "excel"),
    (("arcgispro", "arcmap", "arccatalog", "arcscene", "arcglobe"), "arcgis"),
    (("acad", "acadlt", "bricscad", "zwcad", "gstarcad", "civil3d"), "cad"),
    (("qgis-bin", "qgis", "qgis-ltr-bin", "python", "python3", "pythonw"), "qgis"),
    (("onedrive", "dropbox", "googledrivesync", "googledrivefs", "nextcloud", "owncloud", "syncthing"), "sync"),
    (("explorer", "prevhost", "searchprotocolhost", "searchindexer", "dllhost"), "explorer"),
    (("msaccess",), "access"),
    (("globalmapper", "global_mapper"), "globalmapper"),
    (("googleearth", "googleearthpro"), "googleearth"),
    (("mapinfow", "mapinfopro"), "mapinfo"),
    (("winword", "powerpnt", "acrord32", "acrobat"), "office"),
    (("msmpeng", "avp", "avgnt", "mcshield", "ekrn", "bdservicehost"), "antivirus"),
]


def kind_label(kind):
    return {"window": tr("Program window"), "program": tr("Program"), "service": tr("Windows service"),
            "explorer": tr("Windows Explorer"), "console": tr("Console program"),
            "critical": tr("Critical system process")}.get(kind, tr("Unknown"))


def program_key(name):
    name = (name or "").lower()
    for names, key in PROGRAMS:
        if any(name == n or name.startswith(n) for n in names):
            return key
    return ""


# ---- datasets -------------------------------------------------------------------------------------
def dataset_files(path):
    """Every file that belongs to the dataset at ``path`` (a file, a layer source or a .gdb folder)."""
    path = (path or "").split("|")[0].strip().strip('"')
    if not path:
        return []
    path = os.path.abspath(path)
    if os.path.isdir(path):
        out = []
        for root, dirs, files in os.walk(path):
            for f in files:
                out.append(os.path.join(root, f))
                if len(out) >= MAX_FILES:
                    return out
        return out
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"):
        files = [f for f in glob.glob(os.path.join(glob.escape(folder), glob.escape(stem) + ".*"))
                 if os.path.splitext(f)[0].lower() == os.path.join(folder, stem).lower()]
        return sorted(set(files + ([path] if os.path.exists(path) else [])))
    files = [path] if os.path.exists(path) else []
    if ext in SQLITE_EXT:
        files += [path + s for s in SQLITE_SUFFIXES if os.path.exists(path + s)]
    if ext in (".tif", ".tiff", ".img", ".jp2", ".ecw", ".sid", ".vrt"):
        files += [f for f in (path + ".ovr", path + ".aux.xml", os.path.join(folder, stem + ".tfw"))
                  if os.path.exists(f)]
    return files


def layer_path(layer):
    """The file (or folder) behind a layer, or None for databases and services."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception:  # noqa: BLE001
        return None
    p = parts.get("path") or ""
    return p or None


# ---- Windows Restart Manager ------------------------------------------------------------------------
class _UniqueProcess(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("Process", _UniqueProcess), ("strAppName", wintypes.WCHAR * 256),
                ("strServiceShortName", wintypes.WCHAR * 64), ("ApplicationType", ctypes.c_int),
                ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]


ERROR_MORE_DATA = 234


def _exe_path(pid):
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return ""


def processes_using(files):
    """Programs that hold any of ``files`` open: a list of dicts (pid, name, exe, kind, service, is_self).
    Returns None when the Restart Manager is not available (not Windows)."""
    files = [f for f in files if f]
    if not files:
        return []
    try:
        rm = ctypes.WinDLL("rstrtmgr")
    except (OSError, AttributeError):
        return None
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return None
    try:
        arr = (wintypes.LPCWSTR * len(files))(*files)
        if rm.RmRegisterResources(session, len(files), arr, 0, None, 0, None) != 0:
            return None
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
        infos = None
        for _ in range(5):
            infos = (_ProcessInfo * max(needed.value, 1))()
            count = wintypes.UINT(len(infos))
            res = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
            if res == 0:
                break
            if res != ERROR_MORE_DATA:
                return None
        else:
            return None
        out, me = [], os.getpid()
        for i in range(count.value):
            p = infos[i]
            pid = int(p.Process.dwProcessId)
            exe = _exe_path(pid)
            name = os.path.splitext(os.path.basename(exe))[0] if exe else ""
            out.append({"pid": pid, "app": p.strAppName or name, "name": name or p.strAppName, "exe": exe,
                        "kind": APP_TYPES.get(int(p.ApplicationType), "unknown"),
                        "service": p.strServiceShortName, "is_self": pid == me})
        return out
    finally:
        rm.RmEndSession(session)


# ---- lock files left next to the data -----------------------------------------------------------------
FGDB_LOCK = re.compile(r"^(?P<table>.*?)\.(?P<machine>[^.]+)\.(?P<pid>\d+)\.(?P<thread>\d+)\.(?P<kind>[a-z]+)\.lock$",
                       re.IGNORECASE)
FGDB_KINDS = {"sr": "reading", "rd": "reading", "ed": "editing", "wr": "writing", "sch": "schema", "es": "editing"}


def lock_kind_label(kind):
    return {"reading": tr("reading"), "editing": tr("editing"), "writing": tr("writing"),
            "schema": tr("changing the structure"), "open": tr("open")}.get(kind, kind)


def excel_owner(owner_file):
    """User name stored in Excel's ``~$book.xlsx`` owner file (Unicode copy preferred), or ''."""
    try:
        with open(owner_file, "rb") as f:
            data = f.read(512)
    except OSError:
        return ""
    if not data:
        return ""
    if len(data) >= 56:  # the Unicode copy of the name: length at byte 54, UTF-16 from byte 56
        n = data[54]
        raw = data[56:56 + 2 * n]
        if 0 < n <= 54 and len(raw) == 2 * n:
            try:
                name = raw.decode("utf-16-le").strip("\x00 ").strip()
                if name and name.isprintable():
                    return name
            except UnicodeDecodeError:
                pass
    n = data[0]
    if 0 < n < 54:
        for enc in ("mbcs", "cp1256", "latin-1"):
            try:
                return data[1:1 + n].decode(enc).strip()
            except (UnicodeDecodeError, LookupError):
                continue
    return ""


def lock_files(path):
    """Lock files that other programs leave next to the dataset: list of dicts (path, program, kind, who)."""
    path = (path or "").split("|")[0].strip()
    if not path:
        return []
    out = []
    path = os.path.abspath(path)
    gdb = path if path.lower().endswith(".gdb") else None
    if gdb is None and os.path.basename(os.path.dirname(path)).lower().endswith(".gdb"):
        gdb = os.path.dirname(path)
    if gdb and os.path.isdir(gdb):
        for f in sorted(os.listdir(gdb)):
            if not f.lower().endswith(".lock"):
                continue
            m = FGDB_LOCK.match(f)
            if m:
                who = tr("computer {m}, process {p}").format(m=m.group("machine"), p=m.group("pid"))
                kind = FGDB_KINDS.get(m.group("kind").lower(), m.group("kind"))
            else:
                who, kind = "", "lock"
            out.append({"path": os.path.join(gdb, f), "program": "ArcGIS", "kind": kind, "who": who})
    folder, name = os.path.split(path)
    if name.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv", ".docx")):
        # Excel names its owner file ~$<book>; Word-style owners drop the first two letters of the name
        cands = {os.path.join(folder, "~$" + name)}
        if len(name) > 6:
            cands.add(os.path.join(folder, "~$" + name[2:]))
        for cand in sorted(cands):
            if os.path.exists(cand):
                out.append({"path": cand, "program": "Excel", "kind": "open", "who": excel_owner(cand)})
    if name.lower().endswith(SQLITE_EXT) and os.path.exists(path):
        if sqlite_busy(path):
            out.append({"path": path, "program": "", "kind": "writing", "who": ""})
    return out


def sqlite_busy(path):
    """True when another connection is writing to the GeoPackage/SQLite file right now."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=0.2)
    except sqlite3.Error:
        return False
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return False
    except sqlite3.OperationalError as e:
        return "locked" in str(e).lower() or "busy" in str(e).lower()
    finally:
        con.close()


# ---- the report ------------------------------------------------------------------------------------
def advice_for(proc):
    if proc.get("is_self"):
        return tr("This Biruni window has the file open: remove the layer from the map, or stop editing it, "
                  "then try again.")
    key = program_key(proc.get("name") or proc.get("app"))
    texts = {
        "excel": tr("Close the workbook in Excel (check for hidden Excel windows too)."),
        "arcgis": tr("Remove the layer from the ArcGIS map, stop editing, or close the ArcGIS project."),
        "cad": tr("Close the drawing in the CAD program."),
        "qgis": tr("Another Biruni window, another GIS program or a script has the file open: remove the layer there or "
                   "close that window."),
        "sync": tr("A sync program is copying the file: wait until it finishes, or pause syncing."),
        "explorer": tr("Windows Explorer (the preview pane or the search index) is reading the file: close the "
                       "folder window or turn off the preview pane."),
        "access": tr("Close the database in Microsoft Access."),
        "globalmapper": tr("Close the layer in Global Mapper."),
        "googleearth": tr("Close the file in Google Earth."),
        "mapinfo": tr("Close the table in MapInfo."),
        "office": tr("Close the document in that program."),
        "antivirus": tr("An antivirus is scanning the file: wait a moment and try again."),
    }
    return texts.get(key, tr("Close the file in this program, or close the program, then try again."))


def who_is_using(path):
    """Everything known about who holds ``path``: dict with files, processes (or None when unknown),
    lock_files and a list of plain-language advice lines."""
    files = dataset_files(path)
    procs = processes_using(files) if files else []
    locks = lock_files(path)
    advice = []
    for p in procs or []:
        line = advice_for(p)
        if line not in advice:
            advice.append(line)
    for lk in locks:
        if lk["program"] == "Excel":
            who = lk["who"] or tr("someone")
            line = tr("Excel says the workbook is open by {who}. If nobody has it open, Excel crashed: delete "
                      "the hidden file {f}.").format(who=who, f=os.path.basename(lk["path"]))
        elif lk["program"] == "ArcGIS":
            line = tr("ArcGIS left lock files in the geodatabase ({k}, {w}). Close ArcGIS on that computer; "
                      "lock files of programs that are no longer running can be deleted.").format(
                k=lock_kind_label(lk["kind"]), w=lk["who"] or "?")
        else:
            line = tr("Another program is writing to the database right now: wait for it to finish.")
        if line not in advice:
            advice.append(line)
    if procs is not None and not procs and not locks:
        advice.append(tr("No program has the file open now. If it still fails, the folder may be read-only or "
                         "a sync program may be busy."))
    return {"files": files, "processes": procs, "lock_files": locks, "advice": advice}


# ---- dialog -----------------------------------------------------------------------------------------
class LocksDialog(QDialog):
    """Pick a file or layer and see which programs are using it."""

    def __init__(self, plugin, path=None, parent=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin else None))
        self.plugin = plugin
        self.setWindowTitle(tr("Who is using this file?"))
        self.resize(720, 460)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.path = QLineEdit(path or self._active_layer_path())
        self.path.setPlaceholderText(tr("A file, a layer or a .gdb folder"))
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        check = QPushButton(tr("Check"))
        check.setDefault(True)
        check.clicked.connect(self.refresh)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        row.addWidget(check)
        v.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Program"), tr("Process"), tr("Type"), tr("What to do")])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.tree, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.note)
        hint = QLabel(tr("Biruni never closes other programs for you: close the file there, then press Check."))
        hint.setWordWrap(True)
        v.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        if self.path.text():
            self.refresh()

    def _active_layer_path(self):
        try:
            lyr = self.plugin.iface.activeLayer()
        except Exception:  # noqa: BLE001
            return ""
        return (layer_path(lyr) or "") if lyr else ""

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose the file"), self.path.text())
        if path:
            self.path.setText(path)
            self.refresh()

    def refresh(self):
        self.tree.clear()
        rep = who_is_using(self.path.text())
        if not rep["files"] and not rep["lock_files"]:
            self.note.setText(tr("The file was not found."))
            return
        for p in rep["processes"] or []:
            name = p["app"] or p["name"] or "?"
            if p["is_self"]:
                name = f"{APP_NAME} ({tr('this window')})"
            it = QTreeWidgetItem([name, str(p["pid"]), kind_label(p["kind"]), advice_for(p)])
            it.setToolTip(0, p["exe"])
            self.tree.addTopLevelItem(it)
        for lk in rep["lock_files"]:
            it = QTreeWidgetItem([lk["program"] or tr("Database"), "", tr("lock file"),
                                  os.path.basename(lk["path"]) + (f" - {lk['who']}" if lk["who"] else "")])
            it.setToolTip(3, lk["path"])
            self.tree.addTopLevelItem(it)
        lines = []
        if rep["processes"] is None:
            lines.append(tr("Windows could not tell which programs use the file."))
        lines += rep["advice"]
        self.note.setText("\n".join(lines))


def open_dialog(plugin, path=None):
    dlg = LocksDialog(plugin, path)
    dlg.exec()
    return dlg
