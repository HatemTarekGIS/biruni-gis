"""Turn what I did into a script or a model.

Reads the Processing history (every tool run from a tool dialog is recorded there with its parameters and
results) and writes either a Python script that runs the same tools again, or a Graphical Modeler model
(.model3) that chains them. Files the user started from become variables / model inputs; when a later tool
used the result of an earlier one, the two are linked instead of pointing at the old output file.
"""
import datetime as dt
import keyword
import os
import re

from qgis.core import (QgsApplication, QgsProcessingDestinationParameter, QgsProcessingModelAlgorithm,
                       QgsProcessingModelChildAlgorithm, QgsProcessingModelChildParameterSource,
                       QgsProcessingModelOutput, QgsProcessingModelParameter)
from qgis.PyQt.QtCore import QDateTime, QPointF, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                                 QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)
from qgis.PyQt.QtCore import Qt

from .i18n import tr
from .version import APP_NAME

SIMPLE = (str, int, float, bool, type(None))


# ---- reading the history -----------------------------------------------------------------------------
def process_start():
    """When this program started (for 'this session'); midnight today if Windows can't tell."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        times = [wintypes.FILETIME() for _ in range(4)]
        if k32.GetProcessTimes(k32.GetCurrentProcess(), *[ctypes.byref(t) for t in times]):
            ft = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            utc = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=ft // 10)
            return utc.astimezone().replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        pass
    return dt.datetime.combine(dt.date.today(), dt.time())


def history_entries(start=None, end=None):
    """Processing runs recorded between ``start`` and ``end`` (datetimes), oldest first."""
    from qgis.gui import QgsGui

    reg = QgsGui.historyProviderRegistry()
    qs = QDateTime(start) if start else QDateTime()
    qe = QDateTime(end) if end else QDateTime()
    out = []
    for e in reg.queryEntries(qs, qe, "processing"):
        step = parse_entry(dict(e.entry), e.timestamp.toPyDateTime())
        if step:
            out.append(step)
    return sorted(out, key=lambda s: s["time"])


def parse_entry(entry, when=None):
    alg_id = entry.get("algorithm_id")
    if not alg_id:
        return None
    params = entry.get("parameters") or {}
    inputs = params.get("inputs", params) if isinstance(params, dict) else {}
    return {"time": when or dt.datetime.now(), "alg": alg_id, "inputs": dict(inputs or {}),
            "results": dict(entry.get("results") or {}), "python": entry.get("python_command", "")}


def algorithm(alg_id):
    return QgsApplication.processingRegistry().algorithmById(alg_id)


# ---- linking steps -----------------------------------------------------------------------------------
def _key(v):
    if not isinstance(v, str):
        return None
    s = v.strip().split("|")[0].replace("\\", "/")
    return s.lower() if s else None


def _looks_like_file(v):
    if not isinstance(v, str):
        return False
    p = v.split("|")[0].strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", p) or p.startswith("\\\\") or p.startswith("/"))


def var_name(text, used):
    base = re.sub(r"\W+", "_", os.path.splitext(os.path.basename(text.split("|")[0]))[0], flags=re.ASCII).strip("_")
    base = (base or "data").upper()
    if base[0].isdigit() or keyword.iskeyword(base.lower()):
        base = "DATA_" + base
    name, k = base, 2
    while name in used:
        name, k = f"{base}_{k}", k + 1
    used.add(name)
    return name


def analyse(steps):
    """Decide, for every parameter value of every step, whether it is a fixed value, one of the user's
    input files (a variable) or the output of an earlier step. Adds 'plan' to each step and returns the
    list of input variables [(var, value, step index, param name)]."""
    produced = {}   # value key -> (step index, output name)
    variables = {}  # value key -> var name
    inputs = []
    used = set()
    for i, st in enumerate(steps):
        alg = algorithm(st["alg"])
        dest = {d.name() for d in alg.destinationParameterDefinitions()} if alg else set()
        plan = {}
        for name, value in st["inputs"].items():
            if name in dest:
                plan[name] = ("output", value)
                continue
            k = _key(value)
            if k and k in produced:
                plan[name] = ("link", produced[k])
            elif _looks_like_file(value):
                if k not in variables:
                    variables[k] = var_name(value, used)
                    inputs.append((variables[k], value, i, name))
                plan[name] = ("var", variables[k])
            else:
                plan[name] = ("value", value)
        st["plan"] = plan
        for out_name, out_value in st["results"].items():
            k = _key(out_value)
            if k and out_name in dest | {o.name() for o in (alg.outputDefinitions() if alg else [])}:
                produced[k] = (i, out_name)
        for name in dest:  # the value asked for (a file path) is the same thing as the result
            k = _key(st["inputs"].get(name))
            if k and k not in ("temporary_output", "memory:") and k not in produced:
                produced[k] = (i, name)
    consumed = {p[1][0] for st in steps for p in st["plan"].values() if p[0] == "link"}
    for i, st in enumerate(steps):
        st["leaf"] = i not in consumed
    return inputs


def _literal(v):
    if isinstance(v, SIMPLE):
        return repr(v)
    if isinstance(v, (list, tuple)) and all(isinstance(x, SIMPLE) for x in v):
        return repr(list(v))
    return None


# ---- script ------------------------------------------------------------------------------------------
def make_script(steps, title=""):
    steps = [dict(s) for s in steps]
    inputs = analyse(steps)
    lines = ['"""' + (title or tr("Steps recorded in Biruni GIS")),
             tr("Created on {d}. Run it in the Python console of Biruni GIS (Plugins > Python Console > "
                "Show Editor), or change the input files below first.").format(
                 d=dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
             '"""', "import processing", ""]
    if inputs:
        lines.append("# " + tr("Input files"))
        for var, value, _i, _p in inputs:
            lines.append(f"{var} = {value!r}")
        lines.append("")
    for i, st in enumerate(steps):
        alg = algorithm(st["alg"])
        name = alg.displayName() if alg else st["alg"]
        lines.append(f"# {i + 1}. {name} ({st['alg']}) - {st['time']:%H:%M}")
        args, fallback = [], False
        for pname, (kind, val) in st["plan"].items():
            if kind == "var":
                txt = val
            elif kind == "link":
                txt = f"step{val[0] + 1}[{val[1]!r}]"
            elif kind == "output":
                txt = repr("TEMPORARY_OUTPUT") if not st["leaf"] and not _looks_like_file(val) else _literal(val)
                if txt is None:
                    txt = repr("TEMPORARY_OUTPUT")
            else:
                txt = _literal(val)
            if txt is None:
                fallback = True
                break
            args.append(f"    {pname!r}: {txt},")
        call = "processing.runAndLoadResults" if st["leaf"] else "processing.run"
        if fallback and st.get("python"):
            lines.append(f"step{i + 1} = " + st["python"].replace("processing.run(", call + "(", 1))
        else:
            lines.append(f"step{i + 1} = {call}({st['alg']!r}, {{")
            lines += args
            lines.append("})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---- model --------------------------------------------------------------------------------------------
def make_model(steps, name="Recorded steps", group="Biruni"):
    """A Graphical Modeler model chaining the steps; the user's input files become model inputs."""
    steps = [dict(s) for s in steps]
    inputs = analyse(steps)
    model = QgsProcessingModelAlgorithm(name, group)
    y = 60
    for var, value, i, pname in inputs:
        alg = algorithm(steps[i]["alg"])
        definition = alg.parameterDefinition(pname).clone() if alg and alg.parameterDefinition(pname) else None
        if definition is None:
            continue
        definition.setName(var.lower())
        definition.setDescription(os.path.basename(value.split("|")[0]) or var)
        definition.setDefaultValue(value)
        comp = QgsProcessingModelParameter(var.lower())
        comp.setPosition(QPointF(120, y))
        model.addModelParameter(definition, comp)
        y += 80
    child_ids = []
    for i, st in enumerate(steps):
        alg = algorithm(st["alg"])
        if alg is None:
            raise ValueError(tr("The tool {a} is not available.").format(a=st["alg"]))
        child = QgsProcessingModelChildAlgorithm(st["alg"])
        child.generateChildId(model)
        child.setDescription(f"{i + 1}. {alg.displayName()}")
        child.setPosition(QPointF(420, 60 + 110 * i))
        outputs = {}
        for pname, (kind, val) in st["plan"].items():
            if kind == "output":
                if st["leaf"]:
                    d = alg.parameterDefinition(pname)
                    oname = f"{alg.displayName()} - {d.description() if d else pname}"
                    out = QgsProcessingModelOutput(oname, oname)
                    out.setChildId(child.childId())
                    out.setChildOutputName(pname)
                    outputs[oname] = out
                continue
            if kind == "var":
                src = QgsProcessingModelChildParameterSource.fromModelParameter(val.lower())
            elif kind == "link":
                src = QgsProcessingModelChildParameterSource.fromChildOutput(child_ids[val[0]], val[1])
            else:
                src = QgsProcessingModelChildParameterSource.fromStaticValue(val)
            child.addParameterSources(pname, [src])
        if outputs:
            child.setModelOutputs(outputs)
        model.addChildAlgorithm(child)
        child_ids.append(child.childId())
    model.updateDestinationParameters()
    return model


def save_model(model, path):
    if not path.lower().endswith(".model3"):
        path += ".model3"
    if not model.toFile(path):
        raise OSError(tr("The model could not be written: {p}").format(p=path))
    return path


def is_destination(definition):
    return isinstance(definition, QgsProcessingDestinationParameter)


# ---- dialog -----------------------------------------------------------------------------------------
class SessionScriptDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin else None))
        self.plugin = plugin
        self.setWindowTitle(tr("Turn my steps into a script or a model"))
        self.resize(640, 480)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Tick the tools you ran, in order. Biruni writes a Python script that runs them again, "
                          "or a model for the Graphical Modeler where the result of one tool feeds the next."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Show")))
        self.range = QComboBox()
        for key, label in (("session", tr("This session")), ("today", tr("Today")),
                           ("week", tr("The last 7 days"))):
            self.range.addItem(label, key)
        self.range.currentIndexChanged.connect(self.reload)
        row.addWidget(self.range, 1)
        v.addLayout(row)
        self.list = QListWidget()
        v.addWidget(self.list, 1)
        bb = QDialogButtonBox()
        self.script_btn = QPushButton(tr("Save as Python script…"))
        self.model_btn = QPushButton(tr("Save as model…"))
        bb.addButton(self.script_btn, QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton(self.model_btn, QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        self.script_btn.clicked.connect(self.save_script)
        self.model_btn.clicked.connect(self.save_model)
        v.addWidget(bb)
        self.steps = []
        self.reload()

    def _start(self):
        key = self.range.currentData()
        now = dt.datetime.now()
        if key == "today":
            return dt.datetime.combine(now.date(), dt.time())
        if key == "week":
            return now - dt.timedelta(days=7)
        return process_start()

    def reload(self):
        self.list.clear()
        try:
            self.steps = history_entries(self._start())
        except Exception as e:  # noqa: BLE001
            self.steps = []
            QMessageBox.warning(self, APP_NAME, str(e))
        for st in self.steps:
            alg = algorithm(st["alg"])
            it = QListWidgetItem(f"{st['time']:%H:%M}  {alg.displayName() if alg else st['alg']}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if alg else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        empty = not self.steps
        if empty:
            self.list.addItem(tr("No tools were run in this period. Tools run from their dialogs are recorded."))
        self.script_btn.setEnabled(not empty)
        self.model_btn.setEnabled(not empty)

    def chosen(self):
        return [st for i, st in enumerate(self.steps)
                if self.list.item(i).checkState() == Qt.CheckState.Checked and algorithm(st["alg"])]

    def _folder(self):
        try:
            from .project import project_folder

            return project_folder() or os.path.expanduser("~")
        except Exception:  # noqa: BLE001
            return os.path.expanduser("~")

    def save_script(self):
        steps = self.chosen()
        if not steps:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save as Python script…"),
                                              os.path.join(self._folder(), "my_steps.py"), "Python (*.py)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(make_script(steps))
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        self.plugin.iface.messageBar().pushSuccess(APP_NAME, tr("Script saved: {p}").format(p=path))

    def save_model(self):
        steps = self.chosen()
        if not steps:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save as model…"),
                                              os.path.join(self._folder(), "my_steps.model3"), "Model (*.model3)")
        if not path:
            return
        try:
            model = make_model(steps, os.path.splitext(os.path.basename(path))[0])
            path = save_model(model, path)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        try:
            from processing.modeler.ModelerDialog import ModelerDialog

            dlg = ModelerDialog.create(model)
            dlg.show()
        except Exception:  # noqa: BLE001
            pass
        self.plugin.iface.messageBar().pushSuccess(APP_NAME, tr("Model saved: {p}").format(p=path))


def open_dialog(plugin):
    dlg = SessionScriptDialog(plugin)
    dlg.exec()
    return dlg
