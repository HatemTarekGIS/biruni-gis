"""Shared state of the arcpy shim: errors, environment settings, messages, tool results and script parameters.

Nothing here touches data; it only depends on the standard library so it can be imported first."""
import datetime
import functools
import os
import sys
import tempfile

NOT_YET = "not available in Biruni yet"


class ExecuteError(Exception):
    """Raised by tools exactly like arcpy.ExecuteError, so `except arcpy.ExecuteError:` keeps working."""


class ExecuteWarning(Exception):
    pass


class ExecuteAbort(Exception):
    pass


def unsupported(name, detail=""):
    """Raise the standard 'not available' error naming the arcpy function."""
    name = name if name.startswith("arcpy.") else "arcpy." + name
    msg = f"ERROR 999999: {name} is {NOT_YET}."
    if detail:
        msg += " " + detail
    _emit(2, msg, record_only=True)
    raise ExecuteError(msg)


def missing_callable(name):
    """A stand-in for an arcpy name the shim does not have: calling it raises ExecuteError."""
    def _missing(*_a, **_k):
        unsupported(name)
    _missing.__name__ = name.rsplit(".", 1)[-1]
    _missing._arcpy_missing = name
    return _missing


# ---------------------------------------------------------------- messages

_handlers = []        # callables (severity, text); severity 0 = message, 1 = warning, 2 = error
_progress_handlers = []
_script_messages = []  # every message of the script run (GetMessages)
_tool_messages = []    # messages of the tool that is running / ran last


def add_handler(fn):
    _handlers.append(fn)


def remove_handler(fn):
    if fn in _handlers:
        _handlers.remove(fn)


def add_progress_handler(fn):
    _progress_handlers.append(fn)


def remove_progress_handler(fn):
    if fn in _progress_handlers:
        _progress_handlers.remove(fn)


def _emit(severity, text, record_only=False):
    text = "" if text is None else str(text)
    _script_messages.append((severity, text))
    _tool_messages.append((severity, text))
    if record_only:
        return
    if _handlers:
        for h in list(_handlers):
            try:
                h(severity, text)
            except Exception:  # a broken listener must not break the script
                pass
    else:
        stream = sys.stdout if severity == 0 else sys.stderr
        print(text, file=stream)


def AddMessage(message):  # noqa: N802
    _emit(0, message)


def AddWarning(message):  # noqa: N802
    _emit(1, message)


def AddError(message):  # noqa: N802
    _emit(2, message)


def AddIDMessage(message_type, message_ID, add_argument1=None, add_argument2=None):  # noqa: N802
    sev = {"INFORMATIVE": 0, "WARNING": 1, "ERROR": 2}.get(str(message_type).upper(), 0)
    args = " ".join(str(a) for a in (add_argument1, add_argument2) if a is not None)
    _emit(sev, f"{'ERROR' if sev == 2 else 'WARNING' if sev == 1 else 'Message'} {int(message_ID):06d}: {args}".strip())


def AddReturnMessage(index):  # noqa: N802
    if 0 <= index < len(_tool_messages):
        sev, text = _tool_messages[index]
        _emit(sev, text)


def _select(msgs, severity):
    if severity is None:
        return [t for _s, t in msgs]
    return [t for s, t in msgs if s == int(severity)]


def GetMessages(severity=None):  # noqa: N802
    return "\n".join(_select(_tool_messages, severity))


def GetMessage(index):  # noqa: N802
    return _tool_messages[index][1] if 0 <= index < len(_tool_messages) else ""


def GetMessageCount():  # noqa: N802
    return len(_tool_messages)


def GetSeverity(index):  # noqa: N802
    return _tool_messages[index][0] if 0 <= index < len(_tool_messages) else 0


def GetMaxSeverity():  # noqa: N802
    return max([s for s, _t in _tool_messages] or [0])


def GetReturnCode(index):  # noqa: N802
    return 0


def _progress(kind, *args):
    for h in list(_progress_handlers):
        try:
            h(kind, *args)
        except Exception:
            pass


_prog = {"min": 0, "max": 100, "pos": 0, "step": 1}


def SetProgressor(type="default", message="", min_range=0, max_range=100, step_value=1):  # noqa: N802,A002
    _prog.update(min=min_range, max=max_range, pos=min_range, step=step_value or 1)
    _progress("label", message)


def SetProgressorLabel(label):  # noqa: N802
    _progress("label", label)


def SetProgressorPosition(position=None):  # noqa: N802
    _prog["pos"] = _prog["pos"] + _prog["step"] if position is None else position
    span = (_prog["max"] - _prog["min"]) or 1
    _progress("percent", max(0.0, min(100.0, 100.0 * (_prog["pos"] - _prog["min"]) / span)))


def ResetProgressor():  # noqa: N802
    _prog.update(pos=_prog["min"])
    _progress("percent", 0.0)


# ---------------------------------------------------------------- environment

class _Env:
    """arcpy.env: the settings scripts set and read. Unknown settings are kept (and ignored) like arcpy does."""

    _DEFAULTS = {
        "workspace": None, "scratchWorkspace": None, "overwriteOutput": False, "outputCoordinateSystem": None,
        "extent": None, "cellSize": "MAXOF", "mask": None, "snapRaster": None, "outputZFlag": "Same As Input",
        "outputMFlag": "Same As Input", "XYTolerance": None, "XYResolution": None, "addOutputsToMap": True,
        "qualifiedFieldNames": True, "parallelProcessingFactor": None, "geographicTransformations": None,
        "transferDomains": False, "preserveGlobalIds": False, "maintainAttachments": True, "nodata": "NONE",
        "compression": None, "pyramid": None, "rasterStatistics": None, "autoCommit": 1000, "packageWorkspace": None,
        "outputZValue": None, "randomGenerator": None, "configKeyword": None, "tileSize": None, "resamplingMethod": None,
        "processingServerUser": None, "processingServerPassword": None, "processingServer": None,
        "terrainMemoryUsage": False, "transferGDBAttributeProperties": False, "maintainCurveSegments": False,
    }

    def __init__(self):
        object.__setattr__(self, "_values", dict(self._DEFAULTS))

    def __getattr__(self, name):
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        for k, v in values.items():
            if k.lower() == name.lower():
                return v
        raise AttributeError(name)

    def __setattr__(self, name, value):
        values = object.__getattribute__(self, "_values")
        for k in values:
            if k.lower() == name.lower():
                name = k
                break
        if name == "outputCoordinateSystem" and value is not None and value != "":
            from .geometry import SpatialReference
            value = value if isinstance(value, SpatialReference) else SpatialReference(value)
        if name in ("workspace", "scratchWorkspace") and value:
            value = os.path.normpath(os.fspath(value))
        values[name] = value

    def __getitem__(self, name):
        return getattr(self, name)

    def __setitem__(self, name, value):
        setattr(self, name, value)

    def __iter__(self):
        return iter(sorted(object.__getattribute__(self, "_values")))

    @property
    def scratchFolder(self):  # noqa: N802
        ws = self._values.get("scratchWorkspace")
        if ws and not ws.lower().endswith((".gdb", ".gpkg")) and os.path.isdir(ws):
            base = ws
        else:
            base = _temp_root()
        path = os.path.join(base, "scratch")
        os.makedirs(path, exist_ok=True)
        return path

    @property
    def scratchGDB(self):  # noqa: N802
        ws = self._values.get("scratchWorkspace")
        if ws and ws.lower().endswith(".gdb"):
            base = os.path.dirname(ws)
        else:
            base = os.path.dirname(self.scratchFolder)
        path = os.path.join(base, "scratch.gdb")
        if not os.path.isdir(path):
            from osgeo import ogr
            ogr.GetDriverByName("OpenFileGDB").CreateDataSource(path)
        return path

    def reset(self):
        object.__getattribute__(self, "_values").clear()
        object.__getattribute__(self, "_values").update(self._DEFAULTS)


env = _Env()
_TEMP = {"root": None}


def _temp_root():
    if not _TEMP["root"] or not os.path.isdir(_TEMP["root"]):
        _TEMP["root"] = tempfile.mkdtemp(prefix="biruni_arcpy_")
    return _TEMP["root"]


def temp_path(suffix=""):
    fd, path = tempfile.mkstemp(suffix=suffix, dir=_temp_root())
    os.close(fd)
    os.remove(path)
    return path


def ResetEnvironments():  # noqa: N802
    env.reset()


def ClearEnvironment(environment_name):  # noqa: N802
    if environment_name in _Env._DEFAULTS:
        setattr(env, environment_name, _Env._DEFAULTS[environment_name])


def ListEnvironments(wild_card=None):  # noqa: N802
    import fnmatch
    names = list(_Env._DEFAULTS) + ["scratchGDB", "scratchFolder"]
    return sorted(n for n in names if not wild_card or fnmatch.fnmatch(n.lower(), wild_card.lower()))


def GetSystemEnvironment(environment):  # noqa: N802
    return os.environ.get(environment)


class EnvManager:
    """`with arcpy.EnvManager(workspace=..., overwriteOutput=True):`"""

    def __init__(self, **kwargs):
        self._new = kwargs
        self._old = {}

    def __enter__(self):
        for k, v in self._new.items():
            try:
                self._old[k] = getattr(env, k)
            except AttributeError:
                self._old[k] = None
            setattr(env, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            object.__getattribute__(env, "_values")[k] = v
        return False

    def reset(self):
        self.__exit__(None, None, None)


# ---------------------------------------------------------------- script parameters

_params = {"values": None, "outputs": {}}


def set_script_parameters(values):
    """The runner gives the script arguments here; None means 'read sys.argv' (standalone behaviour)."""
    _params["values"] = None if values is None else list(values)
    _params["outputs"] = {}


def script_outputs():
    return dict(_params["outputs"])


def _param_list():
    if _params["values"] is not None:
        return _params["values"]
    return list(sys.argv[1:])


def GetParameterAsText(index):  # noqa: N802
    vals = _param_list()
    if 0 <= index < len(vals):
        v = vals[index]
        if v is None or v == "#":
            return ""
        return str(v)
    return ""


def GetParameter(index):  # noqa: N802
    vals = _param_list()
    if 0 <= index < len(vals):
        v = vals[index]
        if isinstance(v, str):
            if v in ("", "#"):
                return None
            low = v.strip().lower()
            if low in ("true", "false"):
                return low == "true"
            try:
                return int(v) if low.lstrip("-").isdigit() else float(v) if _floaty(low) else v
            except ValueError:
                return v
        return v
    return None


def _floaty(s):
    try:
        float(s)
        return any(c in s for c in ".e")
    except ValueError:
        return False


def GetArgumentCount():  # noqa: N802
    return len(_param_list())


GetParameterCount = None  # replaced below (signature differs: takes a tool name)


def _get_parameter_count(tool_name=None):
    return len(_param_list())


GetParameterCount = _get_parameter_count


def SetParameterAsText(index, text):  # noqa: N802
    _params["outputs"][index] = "" if text is None else str(text)


def SetParameter(index, value):  # noqa: N802
    _params["outputs"][index] = value


def GetParameterInfo(tool_name=None):  # noqa: N802
    unsupported("GetParameterInfo")


# ---------------------------------------------------------------- results and the tool wrapper

class Result:
    """What every geoprocessing tool returns."""

    def __init__(self, outputs, messages, tool):
        self._outputs = list(outputs)
        self._messages = list(messages)
        self._tool = tool
        self.status = 4
        self.resultID = ""

    def getOutput(self, index):  # noqa: N802
        return self._outputs[index]

    def getInput(self, index):  # noqa: N802
        return None

    def getMessages(self, severity=None):  # noqa: N802
        return "\n".join(_select(self._messages, severity))

    def getMessage(self, index):  # noqa: N802
        return self._messages[index][1]

    def getSeverity(self, index):  # noqa: N802
        return self._messages[index][0]

    def getMapImageURL(self, *a):  # noqa: N802
        return ""

    def cancel(self):
        pass

    @property
    def outputCount(self):  # noqa: N802
        return len(self._outputs)

    @property
    def messageCount(self):  # noqa: N802
        return len(self._messages)

    @property
    def maxSeverity(self):  # noqa: N802
        return max([s for s, _t in self._messages] or [0])

    @property
    def inputCount(self):  # noqa: N802
        return 0

    def __getitem__(self, index):
        return self._outputs[index]

    def __iter__(self):
        return iter(self._outputs)

    def __len__(self):
        return len(self._outputs)

    def __str__(self):
        return str(self._outputs[0]) if self._outputs else ""

    def __fspath__(self):
        return str(self)

    def __repr__(self):
        return f"<Result '{self}'>"


CREATED = []  # outputs written by tools during a run, so the runner can add them to the map


def note_output(path):
    if path and isinstance(path, str) and path not in CREATED:
        CREATED.append(path)


def tool(qualname, *outputs_are_created):
    """Decorator for geoprocessing tools: arcpy-like messages, ExecuteError on failure, a Result back."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _tool_messages.clear()
            short = qualname.rsplit(".", 1)[-1]
            start = datetime.datetime.now()
            _emit(0, f"Start Time: {start:%A, %B %d, %Y %I:%M:%S %p}", record_only=True)
            try:
                out = fn(*args, **kwargs)
            except ExecuteError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:  # anything else becomes an ExecuteError, like arcpy
                msg = f"ERROR 999999: {short} failed: {e}"
                _emit(2, msg, record_only=True)
                raise ExecuteError(msg + f"\nFailed to execute ({short}).") from e
            outs = list(out) if isinstance(out, (list, tuple)) else [out]
            end = datetime.datetime.now()
            _emit(0, f"Succeeded at {end:%A, %B %d, %Y %I:%M:%S %p} (Elapsed Time: "
                     f"{(end - start).total_seconds():.2f} seconds)", record_only=True)
            return Result(outs, list(_tool_messages), qualname)
        wrapper._arcpy_name = qualname
        return wrapper
    return deco


def tool_warning(text):
    _emit(1, text)


def fail(code, text):
    msg = f"ERROR {code:06d}: {text}"
    _emit(2, msg, record_only=True)
    raise ExecuteError(msg)


# ---------------------------------------------------------------- licensing stubs

def CheckExtension(extension):  # noqa: N802
    return "Available"


def CheckOutExtension(extension):  # noqa: N802
    return "CheckedOut"


def CheckInExtension(extension):  # noqa: N802
    return "CheckedIn"


def CheckProduct(product):  # noqa: N802
    return "AlreadyInitialized"


def SetProduct(product):  # noqa: N802
    return "CheckedOut"


def ProductInfo():  # noqa: N802
    return "ArcInfo"


def GetInstallInfo(product=None):  # noqa: N802
    return {"ProductName": "ArcGISPro", "Version": "3.3", "InstallDir": "", "Installer": "Biruni GIS",
            "BuildNumber": "0", "LicenseLevel": "Advanced", "SourceDir": ""}


def GetSigninToken():  # noqa: N802
    """Biruni is never signed in to an ArcGIS portal: None, as arcpy returns when signed out."""
    return None


def GetActivePortalURL():  # noqa: N802
    return ""


def SetLogHistory(log_history):  # noqa: N802
    pass


def GetLogHistory():  # noqa: N802
    return False


def SetLogMetadata(log_metadata):  # noqa: N802
    pass


def RefreshActiveView():  # noqa: N802
    pass


def RefreshTOC():  # noqa: N802
    pass


def RefreshCatalog(dataset=None):  # noqa: N802
    pass


def Usage(tool_name):  # noqa: N802
    return tool_name


def ImportToolbox(input_file, module_name=None):  # noqa: N802
    unsupported("ImportToolbox", "Custom .tbx/.atbx toolboxes cannot be loaded.")
