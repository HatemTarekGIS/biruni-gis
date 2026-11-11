"""Read ArcGIS ModelBuilder toolboxes and turn their models into Biruni models (.model3).

    .atbx  ArcGIS Pro 3.x toolbox - a ZIP: toolbox.content (JSON: toolsets and tool names), one folder per tool
           (<name>.tool/) holding tool.content (type, parameters), tool.content.rc (display texts) and, for a
           model, tool.model (JSON: "variables" and "processes"; each process names its "system_tool", e.g.
           "analysis.Buffer", and links its parameters to variables by "element_id"). Script tools keep their
           code in tool.script.execute.py. Password-protected models are stored encrypted (not JSON).
    .tbx   ArcMap / Pro 1.x-2.x toolbox - an OLE compound file with one "ToolN" stream per tool. The format is not
           published: the tools of each model are recognised by the run [ToolName, Display name, description,
           toolset, "<Toolbox> Tools"] written for every process. Only the list of tools is read.

Conversion maps the common ArcGIS tools to native algorithms, keeps literal parameter values, turns model
parameters and source data into model inputs and keeps every step it cannot convert as a visible note box (its
output becomes a model input, so the rest of the model still runs). Nothing here writes next to the toolbox."""
import json
import os
import re
import struct
import zipfile
from dataclasses import dataclass, field

from ..i18n import tr

TOOLBOX_EXTENSIONS = (".atbx", ".tbx")
MARKER = "biruni_arcgis_source"   # model variable naming the toolbox a model was imported from

# toolbox label written in .tbx files -> arcpy toolbox alias
TOOLBOX_ALIASES = {
    "data management tools": "management", "analysis tools": "analysis", "conversion tools": "conversion",
    "spatial analyst tools": "sa", "3d analyst tools": "3d", "cartography tools": "cartography",
    "editing tools": "edit", "linear referencing tools": "lr", "network analyst tools": "na",
    "spatial statistics tools": "stats", "geostatistical analyst tools": "ga", "coverage tools": "arc",
    "server tools": "server", "multidimension tools": "md", "parcel fabric tools": "fabric",
    "image analyst tools": "ia", "geocoding tools": "geocoding", "data interoperability tools": "interop",
    "topographic production tools": "topographic", "data reviewer tools": "reviewer",
}


class ToolboxError(Exception):
    """The toolbox cannot be read at all."""


@dataclass
class ToolInfo:
    name: str                      # tool folder / stream name (Model1)
    title: str                     # display name
    kind: str = "model"            # model | script | other
    tools: list = field(default_factory=list)   # ArcGIS tool ids of the steps, e.g. "analysis.Buffer"
    protected: bool = False        # password-protected model: its steps cannot be read
    graph: dict = None             # tool.model (atbx)
    content: dict = None           # tool.content (atbx)
    rc: dict = field(default_factory=dict)       # display texts
    script: str = ""               # embedded script tool code (atbx)
    script_path: str = ""          # external script path (tbx)


@dataclass
class Toolbox:
    path: str
    title: str
    kind: str                      # atbx | tbx
    tools: list = field(default_factory=list)

    @property
    def models(self):
        return [t for t in self.tools if t.kind == "model"]


# ---- reading --------------------------------------------------------------------------------------------------
def _json(z, name):
    try:
        return json.loads(z.read(name).decode("utf-8-sig"))
    except (KeyError, ValueError, UnicodeDecodeError):
        return None


def _rc_map(z, name):
    d = _json(z, name) or {}
    return {str(k).lower(): v for k, v in (d.get("map") or {}).items()}


def resolve(text, rc):
    """'$rc:model.element4' -> its display text."""
    if isinstance(text, str) and text.startswith("$rc:"):
        return rc.get(text[4:].lower(), text[4:])
    return text


def read_atbx(path):
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ToolboxError(str(e)) from e
    with z:
        names = z.namelist()
        tb = _json(z, "toolbox.content") or {}
        title = resolve(tb.get("displayname"), _rc_map(z, "toolbox.content.rc")) or \
            os.path.splitext(os.path.basename(path))[0]
        order = []
        for ts in (tb.get("toolsets") or {}).values():
            order += [t for t in (ts or {}).get("tools") or [] if t not in order]
        folders = []
        for n in names:
            head = n.split("/")[0]
            if head.endswith(".tool") and head[:-5] not in folders:
                folders.append(head[:-5])
        box = Toolbox(path=path, title=title, kind="atbx")
        for name in order + sorted(f for f in folders if f not in order):
            if name not in folders:
                continue
            base = f"{name}.tool/"
            content = _json(z, base + "tool.content") or {}
            rc = _rc_map(z, base + "tool.content.rc")
            info = ToolInfo(name=name, title=resolve(content.get("displayname"), rc) or name, content=content, rc=rc)
            typ = (content.get("type") or "").lower()
            if base + "tool.model" in names or typ == "modeltool":
                info.kind = "model"
                raw = z.read(base + "tool.model") if base + "tool.model" in names else b""
                if raw.lstrip()[:1] == b"{":
                    try:
                        info.graph = json.loads(raw.decode("utf-8-sig"))
                    except (ValueError, UnicodeDecodeError):
                        info.protected = True
                else:
                    info.protected = True
                if info.graph:
                    info.tools = [process_tool(p) for p in info.graph.get("processes") or []]
            elif "script" in typ or base + "tool.script.execute.py" in names:
                info.kind = "script"
                try:
                    info.script = z.read(base + "tool.script.execute.py").decode("utf-8", "replace")
                except KeyError:
                    info.script_path = content.get("script_path") or ""
            else:
                info.kind = "other"
            box.tools.append(info)
    return box


def process_tool(proc):
    """ArcGIS tool id of one model process ('analysis.Buffer'); custom tools by their name."""
    t = proc.get("system_tool")
    if t:
        return t
    path = proc.get("path") or ""
    if path:
        return "custom." + re.split(r"[\\/]", path.rstrip("\\/"))[-1]
    return "custom.?"


def _utf16_strings(b):
    """Runs of UTF-16 text (Latin or Arabic) in a binary stream. Greedy with no trailing context, so linear."""
    out = []
    for m in _UTF16_RUN.finditer(b):
        try:
            s = m.group().decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if all(c.isprintable() for c in s):
            out.append(s)
    return out


_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00|[\x00-\xff]\x06){2,}")


def read_tbx(path):
    from ..arcgis.mxd import MxdError, _streams

    try:
        with open(path, "rb") as f:
            data = f.read()
        streams = _streams(data)
    except (OSError, MxdError) as e:
        raise ToolboxError(str(e)) from e
    box = Toolbox(path=path, title=os.path.splitext(os.path.basename(path))[0], kind="tbx")
    keys = sorted((k for k in streams if re.fullmatch(r"Tool\d+", k)), key=lambda k: int(k[4:]))
    for key in keys:
        strs = _utf16_strings(streams[key])
        if not strs:
            continue
        info = ToolInfo(name=strs[0], title=strs[1] if len(strs) > 1 else strs[0], kind="other")
        for i, s in enumerate(strs):
            alias = TOOLBOX_ALIASES.get(s.strip().lower())
            if alias and i >= 4 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,80}", strs[i - 4]):
                info.tools.append(f"{alias}.{strs[i - 4]}")
        scripts = [s for s in strs if s.lower().endswith((".py", ".pyt")) and len(s) < 400]
        if info.tools or any("MdDlgContent" in s for s in strs[:6]):
            info.kind = "model"
        elif scripts:
            info.kind = "script"
            info.script_path = scripts[0]
        box.tools.append(info)
    return box


def read_toolbox(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".atbx":
        return read_atbx(path)
    if ext == ".tbx":
        if zipfile.is_zipfile(path):   # Pro sometimes writes the new format under the old name
            return read_atbx(path)
        return read_tbx(path)
    raise ToolboxError(tr("This is not an ArcGIS toolbox."))


# ---- tool mapping ---------------------------------------------------------------------------------------------
def normalise_tool(name):
    """'analysis.Buffer', 'arcpy.analysis.Buffer', 'Buffer_analysis' -> 'analysis.buffer'."""
    n = (name or "").strip()
    if n.lower().startswith("arcpy."):
        n = n[6:]
    if "." not in n:
        m = re.fullmatch(r"(\w+?)_(\w+)", n)
        if m:
            n = f"{m.group(2)}.{m.group(1)}"
    return n.lower()


# ArcGIS tool -> (algorithm id shown in reports, handler name)
_MAPPED = {
    "analysis.buffer": "native:buffer", "analysis.pairwisebuffer": "native:buffer",
    "analysis.clip": "native:clip", "analysis.pairwiseclip": "native:clip",
    "analysis.intersect": "native:intersection", "analysis.pairwiseintersect": "native:intersection",
    "management.dissolve": "native:dissolve", "analysis.pairwisedissolve": "native:dissolve",
    "analysis.select": "native:extractbyexpression",
    "management.project": "native:reprojectlayer",
    "management.merge": "native:mergevectorlayers",
    "analysis.union": "native:union",
    "analysis.erase": "native:difference", "analysis.pairwiseerase": "native:difference",
    "analysis.spatialjoin": "native:joinattributesbylocation",
    "management.calculatefield": "native:fieldcalculator",
    "management.addfield": "native:addfieldtoattributestable",
    "management.copyfeatures": "native:savefeatures", "conversion.exportfeatures": "native:savefeatures",
    "conversion.featureclasstofeatureclass": "native:savefeatures", "conversion.exporttable": "native:savefeatures",
    "conversion.tabletotable": "native:savefeatures", "management.copyrows": "native:savefeatures",
    "management.featuretopoint": "native:centroids",
    "management.makefeaturelayer": "native:extractbyexpression", "management.maketableview": "native:extractbyexpression",
    "management.selectlayerbyattribute": "native:extractbyexpression",
    "management.selectlayerbylocation": "native:extractbylocation",
    "management.deletefield": "native:deletecolumn",
}

# what to do instead, for frequent tools without an equivalent step
_ADVICE = {
    "management.append": "Append adds rows to an existing layer in place: run Merge, or paste the features while editing.",
    "management.delete": "Deleting data is not done inside models: remove the files by hand when needed.",
    "management.deletefeatures": "Deleting features in place: use Extract by expression with the opposite condition.",
    "analysis.near": "Use 'Join attributes by nearest' (distance to the nearest feature).",
    "analysis.statistics": "Use 'Statistics by categories' or 'Aggregate'.",
    "management.joinfield": "Use 'Join attributes by field value'.",
    "management.addxy": "Use the field calculator with $x and $y.",
    "management.getcount": "Feature counts are shown in the layer properties; models have no count step.",
    "ia.rastercalculator": "Use the 'Raster calculator' tool.",
    "sa.rastercalculator": "Use the 'Raster calculator' tool.",
}


def tool_status(name):
    """'mapped' when the tool is converted to a native step, 'model_only' for ModelBuilder-only tools
    (iterators, Get Field Value...), otherwise 'unmapped'."""
    n = normalise_tool(name)
    if n in _MAPPED:
        return "mapped"
    if n.startswith("mb."):
        return "model_only"
    return "unmapped"


def mapped_algorithm(name):
    return _MAPPED.get(normalise_tool(name))


def advice(name):
    n = normalise_tool(name)
    if n.startswith("mb.iterate"):
        return tr("Iterators are not converted: run the model on each input with the batch (green arrow) option.")
    if n.startswith("mb."):
        return tr("ModelBuilder-only tool: rebuild this logic with model inputs or expressions.")
    text = _ADVICE.get(n)
    return tr(text) if text else ""


# ---- value helpers --------------------------------------------------------------------------------------------
_DATA_TYPES = {"gpfeaturelayer", "defeatureclass", "deshapefile", "gplayer", "gptableview", "detable",
               "dedbasetable", "gprasterlayer", "derasterdataset", "derasterband", "gpmosaiclayer", "defile",
               "defolder", "deworkspace", "defeaturedataset", "gprasterdatalayer", "detextfile",
               "decaddrawingdataset", "gpinternaltable", "delayer", "gpfeaturerecordsetlayer", "gprecordset",
               "gpcomposite", "demosaicdataset", "gpgalayer", "detin", "gptinlayer", "delasdataset"}
_RASTER_TYPES = {"gprasterlayer", "derasterdataset", "derasterband", "gprasterdatalayer", "gpmosaiclayer",
                 "demosaicdataset"}
_TABLE_TYPES = {"gptableview", "detable", "dedbasetable", "detextfile", "gpinternaltable", "gprecordset"}
_FOLDER_TYPES = {"defolder", "deworkspace", "defeaturedataset"}

_UNITS = {"meters": 1.0, "meter": 1.0, "kilometers": 1000.0, "kilometer": 1000.0, "centimeters": 0.01,
          "millimeters": 0.001, "decimeters": 0.1, "feet": 0.3048, "foot": 0.3048, "intfeet": 0.3048,
          "ussurveyfeet": 1200 / 3937, "yards": 0.9144, "miles": 1609.344, "nauticalmiles": 1852.0,
          "inches": 0.0254}


def parse_distance(text):
    """'400 Meters' -> (400.0, 'meters'); a number alone -> (value, ''); else None."""
    m = re.fullmatch(r"\s*([-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*([A-Za-z_]*)\s*", str(text or ""))
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), m.group(2).lower()


def sql_to_expression(where):
    """An ArcGIS SQL where clause as an expression (field names in [] become "")."""
    return re.sub(r"\[([^\]]+)\]", r'"\1"', (where or "").strip())


def python_to_expression(expr):
    """An ArcGIS Python field calculation as an expression: !field! -> "field", "text" -> 'text'.
    Returns (expression, exact)."""
    out, i, s = [], 0, expr or ""
    exact = True
    while i < len(s):
        c = s[i]
        if c == "!":
            j = s.find("!", i + 1)
            if j > i:
                out.append('"' + s[i + 1:j] + '"')
                i = j + 1
                continue
        if c in "\"'":
            j = i + 1
            while j < len(s) and s[j] != c:
                j += 2 if s[j] == "\\" else 1
            body = s[i + 1:j].replace("\\" + c, c).replace("'", "''")
            out.append("'" + body + "'")
            i = j + 1
            continue
        out.append(c)
        i += 1
    text = "".join(out)
    text = re.sub(r"\bNone\b", "NULL", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    # a + between texts stays (|| would be clearer, + also concatenates); function calls / methods need a look
    if re.search(r"[A-Za-z_]\w*\s*\(|\)\s*\.|\.\w+\(|\[|\bif\b|\bfor\b|\blambda\b", re.sub(r"'[^']*'|\"[^\"]*\"", "", text)):
        exact = False
    return text, exact


def arcade_to_expression(expr):
    text = re.sub(r"\$feature\.(\w+)", r'"\1"', expr or "")
    text = re.sub(r"\$feature\[\s*[\"']([^\"']+)[\"']\s*\]", r'"\1"', text)
    exact = not re.search(r"[A-Za-z_]\w*\s*\(|\bvar\b|;|\breturn\b", text)
    return text, exact


def gis_path(value, base):
    """An ArcGIS dataset path as a data source: 'x.gdb\\fds\\fc' -> 'x.gdb|layername=fc' (relative to base)."""
    v = (value or "").strip().strip('"')
    if not v:
        return v
    if re.match(r"^\.{1,2}[\\/]", v):
        v = os.path.normpath(os.path.join(base, v))
    m = re.match(r"(?i)^(.*?\.gdb)[\\/](?:[^\\/]+[\\/])?([^\\/]+)$", v)
    if m:
        return f"{m.group(1)}|layername={m.group(2)}"
    m = re.match(r"(?i)^(.*?\.(?:gpkg|sqlite))[\\/](?:main\.)?([^\\/]+)$", v)
    if m:
        return f"{m.group(1)}|layername={m.group(2)}"
    return v


def _catalog_path(var):
    xml = var.get("value_xml") or ""
    m = re.search(r"<CatalogPath>(.*?)</CatalogPath>", xml)
    if m:
        return m.group(1).replace("&amp;", "&")
    try:
        cp = json.loads(var.get("value_json") or "{}").get("catalogPath")
        if cp:
            return cp
    except ValueError:
        pass
    return var.get("value") or ""


def _ids(entry):
    e = entry.get("element_id") if isinstance(entry, dict) else None
    if e is None:
        return []
    return [str(x) for x in (e if isinstance(e, list) else [e]) if str(x) not in ("", "0")]


def _type_of(var, params):
    dt = var.get("value_datatype") or var.get("datatype") or {}
    if not dt and var.get("param_name") in params:
        dt = params[var["param_name"]].get("datatype") or {}
    t = (dt.get("type") or "")
    if t == "GPComposite" and dt.get("datatypes"):
        t = dt["datatypes"][0].get("type") or t
    return t.lower()


def _param_name(text, used):
    base = re.sub(r"[^0-9a-z]+", "_", (text or "").lower(), flags=re.ASCII).strip("_") or "input"
    if base[0].isdigit():
        base = "p_" + base
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


# ---- conversion -----------------------------------------------------------------------------------------------
@dataclass
class Step:
    title: str
    tool: str
    algorithm: str = ""            # native id, "" when not converted
    notes: list = field(default_factory=list)


@dataclass
class Conversion:
    toolbox: str
    model: str
    title: str
    steps: list = field(default_factory=list)
    inputs: list = field(default_factory=list)      # (name, description, default)
    outputs: list = field(default_factory=list)
    unmapped: list = field(default_factory=list)    # ArcGIS tool ids
    warnings: list = field(default_factory=list)
    error: str = ""
    file: str = ""
    algorithm: object = None                        # QgsProcessingModelAlgorithm

    @property
    def converted(self):
        return sum(1 for s in self.steps if s.algorithm)

    @property
    def complete(self):
        return not self.error and not self.unmapped and bool(self.steps)


class _Converter:
    def __init__(self, info, toolbox_path, group):
        from qgis.core import QgsProcessingModelAlgorithm

        self.info = info
        self.base = os.path.dirname(os.path.abspath(toolbox_path))
        self.graph = info.graph or {}
        self.rc = info.rc
        self.params = (info.content or {}).get("params") or {}
        if not isinstance(self.params, dict):
            self.params = {}
        self.vars = {str(v.get("id")): v for v in self.graph.get("variables") or []}
        self.procs = [p for p in self.graph.get("processes") or [] if isinstance(p, dict)]
        self.model = QgsProcessingModelAlgorithm(info.title, group, "")
        self.conv = Conversion(toolbox=toolbox_path, model=info.name, title=info.title)
        self.used_names = set()
        self.inputs = {}        # var id -> model parameter name
        self.outputs = {}       # (process id, arc param) -> (child id, output name)
        self.aliases = {}       # var id -> var id it stands for (Make Feature Layer without a query)
        self.producer = {}      # var id -> (process id, arc param)
        self.consumed = set()
        self.child_ids = set()
        self.by_title = {}
        for vid, v in self.vars.items():
            self.by_title[str(self.title(v)).lower()] = vid
        for p in self.procs:
            for pname, pe in (p.get("params") or {}).items():
                if not isinstance(pe, dict):
                    continue
                for eid in _ids(pe):
                    if pe.get("direction") == "out":
                        self.producer[eid] = (str(p.get("id")), pname)
                    else:
                        self.consumed.add(eid)
            for eid in _as_list(p.get("precondition_id")):
                self.consumed.add(str(eid))

    def title(self, v):
        return resolve(v.get("title"), self.rc) or v.get("value") or v.get("id")

    # -- model inputs --
    def make_inputs(self):
        from qgis.core import QgsProcessingModelParameter
        from qgis.PyQt.QtCore import QPointF

        y = 60
        for vid, v in self.vars.items():
            if vid in self.producer:
                continue
            t = _type_of(v, self.params)
            is_param = bool(v.get("param_name"))
            if not is_param and (vid not in self.consumed or t not in _DATA_TYPES):
                continue
            title = str(self.title(v))
            name = _param_name(v.get("param_name") or title, self.used_names)
            default = self._default(v, t)
            pdef = _parameter(name, title, t, default)
            comp = QgsProcessingModelParameter(name)
            comp.setPosition(QPointF(120, y))
            y += 70
            self.model.addModelParameter(pdef, comp)
            self.inputs[vid] = name
            self.conv.inputs.append((name, title, "" if default is None else str(default)))

    def _default(self, v, t):
        value = v.get("value")
        pv = self.params.get(v.get("param_name") or "") or {}
        if value in (None, "") and isinstance(pv, dict):
            value = pv.get("value")
        if t in _DATA_TYPES:
            path = _catalog_path(v) or value or ""
            return gis_path(path, self.base) or None
        if t in ("gplong", "gpdouble", "gplinearunit"):
            d = parse_distance(value)
            return d[0] if d else None
        if t == "gpboolean":
            return str(value).lower() in ("true", "1")
        return value if value not in ("",) else None

    def input_for_output(self, vid, tool):
        """An output of a step that was not converted becomes a model input, so later steps stay connected."""
        if vid in self.inputs:
            return self.inputs[vid]
        from qgis.core import QgsProcessingModelParameter
        from qgis.PyQt.QtCore import QPointF

        v = self.vars.get(vid, {})
        title = tr("{name} (from the step that was not converted: {tool})").format(name=self.title(v), tool=tool)
        name = _param_name(str(self.title(v)), self.used_names)
        t = _type_of(v, self.params)
        pdef = _parameter(name, title, t if t in _DATA_TYPES else "gpfeaturelayer", None)
        comp = QgsProcessingModelParameter(name)
        comp.setPosition(QPointF(120, 60 + 70 * len(self.inputs)))
        self.model.addModelParameter(pdef, comp)
        self.inputs[vid] = name
        self.conv.inputs.append((name, title, ""))
        return name

    # -- sources --
    def source_for(self, vid, expression=False):
        from qgis.core import QgsProcessingModelChildParameterSource as Src

        seen = set()
        while vid in self.aliases and vid not in seen:
            seen.add(vid)
            vid = self.aliases[vid]
        if vid in self.producer:
            key = self.producer[vid]
            if key in self.outputs:
                cid, out = self.outputs[key]
                return Src.fromChildOutput(cid, out)
            tool = next((process_tool(p) for p in self.procs if str(p.get("id")) == key[0]), "?")
            return Src.fromModelParameter(self.input_for_output(vid, tool))
        if vid in self.inputs:
            return Src.fromModelParameter(self.inputs[vid])
        v = self.vars.get(vid, {})
        return self.static(v.get("value"), expression)

    def static(self, value, expression=False):
        """A literal value; %Variable% inline substitutions become model variables (@name)."""
        from qgis.core import QgsExpression
        from qgis.core import QgsProcessingModelChildParameterSource as Src

        text = value if isinstance(value, str) else None
        if text is None or "%" not in text:
            return Src.fromStaticValue(value)
        parts = re.split(r"%([^%]+)%", text)
        if len(parts) == 1:
            return Src.fromStaticValue(value)
        if expression:
            out = []
            for i, part in enumerate(parts):
                out.append(part if i % 2 == 0 else self._inline(part, as_expression=True))
            return Src.fromStaticValue("".join(out))
        if len(parts) == 3 and parts[0] == "" and parts[2] == "":
            vid = self.by_title.get(parts[1].lower())
            if vid in self.inputs:
                return Src.fromModelParameter(self.inputs[vid])
        pieces = []
        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part:
                    pieces.append(QgsExpression.quotedString(part))
            else:
                pieces.append(self._inline(part, as_expression=True, quoted=True))
        return Src.fromExpression(" || ".join(pieces) or "''")

    def _inline(self, name, as_expression=True, quoted=False):
        vid = self.by_title.get(name.lower())
        if vid in self.inputs:
            return "@" + self.inputs[vid]
        if vid is not None and vid not in self.producer:
            val = str(self.vars[vid].get("value") or "")
            if quoted:
                from qgis.core import QgsExpression

                return QgsExpression.quotedString(val)
            return val
        self.warn(tr("the inline variable %{name}% could not be converted").format(name=name))
        return "''" if quoted else f"%{name}%"

    def warn(self, text):
        if self._step is not None:
            self._step.notes.append(text)
        else:
            self.conv.warnings.append(text)

    _step = None

    # -- processes --
    def order(self):
        ids = {str(p.get("id")): p for p in self.procs}
        deps = {}
        for pid, p in ids.items():
            d = set()
            for pe in (p.get("params") or {}).values():
                if isinstance(pe, dict) and pe.get("direction") != "out":
                    for eid in _ids(pe):
                        if eid in self.producer:
                            d.add(self.producer[eid][0])
            for eid in _as_list(p.get("precondition_id")):
                if str(eid) in self.producer:
                    d.add(self.producer[str(eid)][0])
            d.discard(pid)
            deps[pid] = d
        done, out, level = set(), [], {}
        while len(out) < len(ids):
            ready = [pid for pid in ids if pid not in done and deps[pid] <= done]
            if not ready:   # a loop (iterator feedback): take the rest in file order
                ready = [pid for pid in ids if pid not in done][:1]
            for pid in ready:
                level[pid] = 1 + max([level.get(d, 0) for d in deps[pid]] or [0])
                done.add(pid)
                out.append(ids[pid])
        return out, level

    def run(self):
        from qgis.PyQt.QtCore import QPointF

        self.make_inputs()
        procs, level = self.order()
        rows = {}
        for p in procs:
            pid = str(p.get("id"))
            tool = process_tool(p)
            title = str(resolve(p.get("title"), self.rc) or tool)
            step = Step(title=title, tool=tool)
            self._step = step
            lv = level.get(pid, 1)
            rows[lv] = rows.get(lv, 0) + 1
            pos = QPointF(120 + 260 * lv, 60 + 120 * (rows[lv] - 1))
            handler = _HANDLERS.get(normalise_tool(tool))
            ok = False
            if handler is not None:
                try:
                    ok = handler(_Ctx(self, p, step, pos)) is not False
                except Exception as e:  # noqa: BLE001 - one odd step must not stop the model
                    step.notes.append(tr("not converted: {e}").format(e=e))
                    ok = False
            if not ok:
                step.algorithm = ""
                self.conv.unmapped.append(tool)
                self._placeholder(p, step, pos)
            self.conv.steps.append(step)
            self._step = None
        self.model.setVariables({MARKER: self.conv.toolbox})
        self.model.setHelpContent({"ALG_DESC": self.help_text(), "ALG_CREATOR": "ArcGIS ModelBuilder",
                                   "ALG_VERSION": ""})
        self.conv.algorithm = self.model
        return self.conv

    def _placeholder(self, p, step, pos):
        from qgis.core import QgsProcessingModelGroupBox
        from qgis.PyQt.QtCore import QSizeF

        lines = [tr("Not converted: {title} ({tool})").format(title=step.title, tool=step.tool)]
        for pname, pe in (p.get("params") or {}).items():
            if not isinstance(pe, dict):
                continue
            if _ids(pe):
                val = ", ".join(str(self.title(self.vars.get(e, {"id": e}))) for e in _ids(pe))
            else:
                val = pe.get("value")
            if val not in (None, ""):
                lines.append(f"{pname} = {str(val)[:120]}")
        hint = advice(step.tool)
        if hint:
            lines.append(hint)
            step.notes.append(hint)
        box = QgsProcessingModelGroupBox("\n".join(lines))
        box.setPosition(pos)
        box.setSize(QSizeF(230, 90))
        box.setColor(_qcolor("#fde68a"))
        self.model.addGroupBox(box)

    def help_text(self):
        c = self.conv
        lines = [tr("Imported from the ArcGIS toolbox {file}.").format(file=os.path.basename(c.toolbox)),
                 tr("{done} of {total} steps were converted.").format(done=c.converted, total=len(c.steps))]
        for s in c.steps:
            if not s.algorithm:
                lines.append("- " + tr("Not converted: {title} ({tool})").format(title=s.title, tool=s.tool))
            for n in s.notes:
                lines.append(f"- {s.title}: {n}")
        return "\n".join(lines)


def _as_list(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _qcolor(hex_):
    from qgis.PyQt.QtGui import QColor

    return QColor(hex_)


_OUT_CACHE = {}


def _alg_outputs(alg_id):
    if alg_id not in _OUT_CACHE:
        from qgis.core import QgsApplication, QgsProcessingOutputVectorLayer

        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        if alg is None:   # native algorithms not loaded (Processing not started): every mapped tool has OUTPUT
            return [QgsProcessingOutputVectorLayer("OUTPUT")]
        _OUT_CACHE[alg_id] = list(alg.outputDefinitions())
    return _OUT_CACHE[alg_id]


def _parameter(name, title, t, default):
    from qgis.core import (Qgis, QgsProcessingParameterBoolean, QgsProcessingParameterCrs,
                           QgsProcessingParameterFile, QgsProcessingParameterNumber,
                           QgsProcessingParameterRasterLayer, QgsProcessingParameterString,
                           QgsProcessingParameterVectorLayer)

    if t in _RASTER_TYPES:
        return QgsProcessingParameterRasterLayer(name, title, default)
    if t in _FOLDER_TYPES:
        return QgsProcessingParameterFile(name, title, Qgis.ProcessingFileParameterBehavior.Folder,
                                          defaultValue=default)
    if t == "defile":
        return QgsProcessingParameterFile(name, title, defaultValue=default)
    if t in _TABLE_TYPES:
        return QgsProcessingParameterVectorLayer(name, title, [Qgis.ProcessingSourceType.Vector], default)
    if t in _DATA_TYPES:
        return QgsProcessingParameterVectorLayer(name, title, [Qgis.ProcessingSourceType.Vector], default)
    if t == "gplong":
        return QgsProcessingParameterNumber(name, title, Qgis.ProcessingNumberParameterType.Integer, default)
    if t in ("gpdouble", "gplinearunit"):
        return QgsProcessingParameterNumber(name, title, Qgis.ProcessingNumberParameterType.Double, default)
    if t == "gpboolean":
        return QgsProcessingParameterBoolean(name, title, bool(default))
    if t in ("gpcoordinatesystem", "gpspatialreference"):
        return QgsProcessingParameterCrs(name, title, default)
    return QgsProcessingParameterString(name, title, "" if default is None else str(default))


class _Ctx:
    """One process being converted: reads its ArcGIS parameters and builds the child step."""

    def __init__(self, conv, proc, step, pos):
        self.conv = conv
        self.proc = proc
        self.step = step
        self.pos = pos
        self.params = {k: v for k, v in (proc.get("params") or {}).items() if isinstance(v, dict)}
        self.child = None

    # reading
    def entry(self, *names):
        for n in names:
            if n in self.params:
                return self.params[n]
        return None

    def dynamic(self, *names):
        """True when the value comes from a model input or an earlier step."""
        e = self.entry(*names)
        if e is None:
            return False
        for eid in _ids(e):
            if eid in self.conv.inputs or eid in self.conv.producer or eid in self.conv.aliases:
                return True
        return False

    def raw(self, *names, default=""):
        e = self.entry(*names)
        if e is None:
            return default
        ids = _ids(e)
        if ids:
            v = self.conv.vars.get(ids[0], {})
            val = v.get("value")
            return default if val in (None, "") else val
        val = e.get("value")
        return default if val in (None, "") else val

    def sources(self, *names, expression=False):
        e = self.entry(*names)
        if e is None:
            return []
        ids = _ids(e)
        if ids:
            return [self.conv.source_for(i, expression) for i in ids]
        if e.get("value") not in (None, ""):
            return [self.conv.static(e.get("value"), expression)]
        return []

    def note(self, text):
        self.step.notes.append(text)

    # building
    def make(self, alg_id):
        from qgis.core import QgsProcessingModelChildAlgorithm

        short = alg_id.split(":")[-1]
        n = 1
        while f"{short}_{n}" in self.conv.child_ids:
            n += 1
        cid = f"{short}_{n}"
        self.conv.child_ids.add(cid)
        child = QgsProcessingModelChildAlgorithm(alg_id)
        child.setChildId(cid)
        child.setDescription(self.step.title)
        child.setPosition(self.pos)
        self.child = child
        self.step.algorithm = alg_id
        return child

    def set(self, qname, sources):
        from qgis.core import QgsProcessingModelChildParameterSource as Src

        if not (isinstance(sources, list) and all(isinstance(s, Src) for s in sources)):
            sources = [Src.fromStaticValue(sources)]
        if sources:
            self.child.addParameterSources(qname, sources)

    def link(self, qname, *arc_names, expression=False):
        self.set(qname, self.sources(*arc_names, expression=expression))

    def out(self, arc_name, qname="OUTPUT"):
        if arc_name in self.params:
            self.conv.outputs[(str(self.proc.get("id")), arc_name)] = (self.child.childId(), qname)

    def alias(self, arc_out, *arc_in):
        e_out, e_in = self.entry(arc_out), self.entry(*arc_in)
        if e_out is None or e_in is None:
            return
        src = _ids(e_in)
        for eid in _ids(e_out):
            if src:
                self.conv.aliases[eid] = src[0]

    def finish(self):
        from qgis.core import QgsProcessingModelChildDependency
        from qgis.PyQt.QtCore import QPointF

        deps = []
        for eid in _as_list(self.proc.get("precondition_id")):
            key = self.conv.producer.get(str(eid))
            if key and key in self.conv.outputs:
                deps.append(QgsProcessingModelChildDependency(self.conv.outputs[key][0]))
        if deps:
            self.child.setDependencies(deps)
            self.note(tr("the step waits for its precondition; the true/false test itself is not converted"))
        self._model_outputs()
        if self.step.notes:
            c = self.child.comment()
            c.setDescription("\n".join(self.step.notes))
            c.setPosition(self.pos + QPointF(0, 70))
        self.conv.model.addChildAlgorithm(self.child)
        return True

    def _model_outputs(self):
        """Outputs that are model parameters, or final results nothing else reads, become model outputs."""
        from qgis.core import QgsProcessingModelOutput

        pid = str(self.proc.get("id"))
        names = [o.name() for o in _alg_outputs(self.child.algorithmId())]
        outs = {}
        for vid, key in self.conv.producer.items():
            if key[0] != pid or self.conv.outputs.get(key, (None,))[0] != self.child.childId():
                continue
            v = self.conv.vars.get(vid, {})
            final = vid not in self.conv.consumed and str(v.get("intermediate")).lower() != "true"
            if not (v.get("param_name") or final):
                continue
            qname = self.conv.outputs[key][1]
            if qname not in names or any(o.childOutputName() == qname for o in outs.values()):
                continue
            name = str(self.conv.title(v))
            mo = QgsProcessingModelOutput(name, name)
            mo.setChildId(self.child.childId())
            mo.setChildOutputName(qname)
            outs[name] = mo
            self.conv.conv.outputs.append(name)
        if outs:
            self.child.setModelOutputs(outs)

    def expression(self, *names, kind="sql"):
        """A where clause / calculation of this step as an expression with %variables% resolved."""
        e = self.entry(*names)
        if e is None:
            return ""
        if _ids(e) and self.dynamic(*names):
            return None
        text = str(self.conv.static(str(self.raw(*names)), expression=True).staticValue())
        if kind == "python":
            text, exact = python_to_expression(text)
        elif kind == "arcade":
            text, exact = arcade_to_expression(text)
        else:
            text, exact = sql_to_expression(text), True
        if not exact:
            self.note(tr("check the converted expression: {e}").format(e=text))
        return text


# ---- handlers: one per ArcGIS tool (return False to keep the step as a placeholder) -------------------------------
def _distance(ctx, *names):
    if ctx.dynamic(*names):
        return ctx.sources(*names)
    val = ctx.raw(*names)
    d = parse_distance(val)
    if d is None:
        if val:
            from qgis.core import QgsProperty

            ctx.note(tr("the distance comes from the field {f}").format(f=val))
            return QgsProperty.fromField(str(val))
        return 0.0
    num, unit = d
    if unit and unit not in ("meters", "meter", "unknown", "decimaldegrees"):
        if unit in _UNITS:
            ctx.note(tr("{v} {u} written as {m} meters: the layer must use meters").format(
                v=num, u=unit, m=round(num * _UNITS[unit], 6)))
            num = num * _UNITS[unit]
        else:
            ctx.note(tr("distance unit {u} not converted: the number is in the layer's units").format(u=unit))
    return num


def _h_buffer(ctx):
    ctx.make("native:buffer")
    ctx.link("INPUT", "in_features")
    ctx.set("DISTANCE", _distance(ctx, "buffer_distance_or_field"))
    ctx.set("SEGMENTS", 8)
    end = str(ctx.raw("line_end_type", default="ROUND")).upper()
    ctx.set("END_CAP_STYLE", 1 if end == "FLAT" else 0)
    diss = str(ctx.raw("dissolve_option", default="NONE")).upper()
    ctx.set("DISSOLVE", diss == "ALL")
    if diss == "LIST":
        ctx.note(tr("dissolving by fields is not kept: run Dissolve on the result"))
    if str(ctx.raw("line_side", default="FULL")).upper() not in ("FULL", ""):
        ctx.note(tr("one-sided buffers are not kept: use 'Single sided buffer'"))
    ctx.out("out_feature_class")
    return ctx.finish()


def _h_overlay(alg, second):
    def h(ctx):
        ctx.make(alg)
        ctx.link("INPUT", "in_features")
        ctx.link("OVERLAY", second)
        ctx.out("out_feature_class")
        return ctx.finish()
    return h


def _h_intersect(ctx):
    srcs = ctx.sources("in_features")
    if len(srcs) < 2:
        return False
    if len(srcs) == 2:
        ctx.make("native:intersection")
        ctx.set("INPUT", srcs[:1])
        ctx.set("OVERLAY", srcs[1:])
    else:
        ctx.make("native:multiintersection")
        ctx.set("INPUT", srcs[:1])
        ctx.set("OVERLAYS", srcs[1:])
    if str(ctx.raw("join_attributes", default="ALL")).upper() not in ("ALL", ""):
        ctx.note(tr("all attributes are kept (the attribute option is not converted)"))
    if str(ctx.raw("output_type", default="INPUT")).upper() not in ("INPUT", ""):
        ctx.note(tr("the output geometry type option is not converted"))
    ctx.out("out_feature_class")
    return ctx.finish()


def _h_union(ctx):
    srcs = ctx.sources("in_features")
    if not srcs:
        return False
    ctx.make("native:union")
    ctx.set("INPUT", srcs[:1])
    if len(srcs) > 1:
        ctx.set("OVERLAY", srcs[1:2])
    if len(srcs) > 2:
        ctx.note(tr("only the first two layers are combined: chain more Union steps for the others"))
    ctx.out("out_feature_class")
    return ctx.finish()


def _h_dissolve(ctx):
    ctx.make("native:dissolve")
    ctx.link("INPUT", "in_features")
    fields = ctx.raw("dissolve_field")
    if fields and not ctx.dynamic("dissolve_field"):
        ctx.set("FIELD", [f.strip() for f in re.split(r"[;,]", str(fields)) if f.strip()])
    if ctx.raw("statistics_fields"):
        ctx.note(tr("summary statistics are not kept: use 'Aggregate' for them"))
    if str(ctx.raw("multi_part", default="MULTI_PART")).upper() == "SINGLE_PART":
        ctx.set("SEPARATE_DISJOINT", True)
    ctx.out("out_feature_class")
    return ctx.finish()


def _h_select(ctx):
    expr = ctx.expression("where_clause")
    ctx.make("native:extractbyexpression")
    ctx.link("INPUT", "in_features")
    if expr is None:
        ctx.link("EXPRESSION", "where_clause")
    else:
        ctx.set("EXPRESSION", expr or "TRUE")
    ctx.out("out_feature_class")
    return ctx.finish()


def _h_project(ctx):
    from qgis.core import QgsCoordinateReferenceSystem

    ctx.make("native:reprojectlayer")
    ctx.link("INPUT", "in_dataset")
    if ctx.dynamic("out_coor_system"):
        ctx.link("TARGET_CRS", "out_coor_system")
    else:
        text = str(ctx.raw("out_coor_system"))
        crs = QgsCoordinateReferenceSystem.fromWkt(text) if "[" in text else QgsCoordinateReferenceSystem(text)
        if not crs.isValid() and text.strip().isdigit():
            crs = QgsCoordinateReferenceSystem(f"EPSG:{text.strip()}")
        if not crs.isValid():
            return False
        ctx.set("TARGET_CRS", crs.authid() or crs.toWkt())
    if ctx.raw("transform_method"):
        ctx.note(tr("the datum transformation is chosen automatically"))
    ctx.out("out_dataset")
    return ctx.finish()


def _h_merge(ctx):
    srcs = ctx.sources("inputs")
    if not srcs:
        return False
    ctx.make("native:mergevectorlayers")
    ctx.set("LAYERS", srcs)
    if ctx.raw("field_mappings"):
        ctx.note(tr("field mapping is not kept: fields with the same name are merged"))
    ctx.set("ADD_SOURCE_FIELDS", str(ctx.raw("add_source", default="false")).lower() in ("true", "add_source_info"))
    ctx.out("output")
    return ctx.finish()


_JOIN_PREDICATES = {"INTERSECT": 0, "INTERSECT_3D": 0, "CONTAINS": 1, "COMPLETELY_CONTAINS": 1,
                    "CONTAINS_CLEMENTINI": 1, "WITHIN": 5, "COMPLETELY_WITHIN": 5, "WITHIN_CLEMENTINI": 5,
                    "ARE_IDENTICAL_TO": 2, "BOUNDARY_TOUCHES": 3, "SHARE_A_LINE_SEGMENT_WITH": 3,
                    "CROSSED_BY_THE_OUTLINE_OF": 6}


def _h_spatial_join(ctx):
    ctx.make("native:joinattributesbylocation")
    ctx.link("INPUT", "target_features")
    ctx.link("JOIN", "join_features")
    match = str(ctx.raw("match_option", default="INTERSECT")).upper()
    if match not in _JOIN_PREDICATES:
        ctx.note(tr("match option {m} is joined as 'intersects'").format(m=match))
    ctx.set("PREDICATE", [_JOIN_PREDICATES.get(match, 0)])
    op = str(ctx.raw("join_operation", default="JOIN_ONE_TO_ONE")).upper()
    ctx.set("METHOD", 0 if op == "JOIN_ONE_TO_MANY" else 1)
    keep = str(ctx.raw("join_type", default="KEEP_ALL")).upper()
    ctx.set("DISCARD_NONMATCHING", keep in ("KEEP_COMMON", "FALSE"))
    if ctx.raw("field_mapping"):
        ctx.note(tr("field mapping is not kept: all fields of the joined layer are added"))
    if op != "JOIN_ONE_TO_MANY":
        ctx.note(tr("the Join_Count field is not added"))
    if parse_distance(ctx.raw("search_radius")) not in (None, (0.0, ""), (0.0, "meters")):
        ctx.note(tr("the search radius is not kept"))
    ctx.out("out_feature_class")
    return ctx.finish()


_CALC_TYPES = {"DOUBLE": 0, "FLOAT": 0, "SHORT": 1, "LONG": 1, "BIGINTEGER": 1, "TEXT": 2, "GUID": 2,
               "DATE": 5, "DATEONLY": 3, "TIMEONLY": 4}
_ADD_TYPES = {"SHORT": 0, "LONG": 0, "BIGINTEGER": 0, "FLOAT": 1, "DOUBLE": 1, "TEXT": 2, "GUID": 2,
              "DATE": 6, "DATEONLY": 4, "TIMEONLY": 5, "BLOB": 7}


def _h_calculate(ctx):
    kind = str(ctx.raw("expression_type", default="PYTHON3")).upper()
    expr = ctx.expression("expression", kind="arcade" if kind == "ARCADE" else
                          ("sql" if kind == "SQL" else "python"))
    ctx.make("native:fieldcalculator")
    ctx.link("INPUT", "in_table")
    ctx.link("FIELD_NAME", "field")
    if expr is None:
        ctx.link("FORMULA", "expression")
    else:
        ctx.set("FORMULA", expr)
    ftype = str(ctx.raw("field_type", default="")).upper()
    if ftype in _CALC_TYPES:
        ctx.set("FIELD_TYPE", _CALC_TYPES[ftype])
    else:
        numeric = expr is not None and re.fullmatch(r"[\s\d.+\-*/()\"\w]*", expr or "") and "'" not in (expr or "")
        ctx.set("FIELD_TYPE", 0 if numeric else 2)
        ctx.note(tr("the field type was guessed ({t}): check it").format(
            t=tr("number") if numeric else tr("text")))
    ctx.set("FIELD_LENGTH", 0)
    ctx.set("FIELD_PRECISION", 0)
    if ctx.raw("code_block"):
        ctx.note(tr("the code block is not converted: rewrite it as an expression"))
    ctx.out("out_table")
    return ctx.finish()


def _h_add_field(ctx):
    ctx.make("native:addfieldtoattributestable")
    ctx.link("INPUT", "in_table")
    ctx.link("FIELD_NAME", "field_name")
    ftype = str(ctx.raw("field_type", default="TEXT")).upper()
    ctx.set("FIELD_TYPE", _ADD_TYPES.get(ftype, 2))
    try:
        length = int(float(ctx.raw("field_length", default=0) or 0))
    except ValueError:
        length = 0
    ctx.set("FIELD_LENGTH", length or (255 if ftype in ("TEXT", "GUID") else 10))
    try:
        ctx.set("FIELD_PRECISION", int(float(ctx.raw("field_scale", default=0) or 0)))
    except ValueError:
        ctx.set("FIELD_PRECISION", 0)
    if ctx.raw("field_alias"):
        ctx.link("FIELD_ALIAS", "field_alias")
    ctx.out("out_table")
    return ctx.finish()


def _h_copy(ctx):
    where = ctx.expression("where_clause") if ctx.entry("where_clause") else ""
    if where:
        ctx.make("native:extractbyexpression")
        ctx.link("INPUT", "in_features", "in_table", "in_rows")
        ctx.set("EXPRESSION", where)
    else:
        ctx.make("native:savefeatures")
        ctx.link("INPUT", "in_features", "in_table", "in_rows")
    for name in ("out_feature_class", "out_features", "out_table", "out_rows", "out_path"):
        ctx.out(name)
    if ctx.raw("field_mapping"):
        ctx.note(tr("field mapping is not kept: the fields are copied as they are"))
    return ctx.finish()


def _h_feature_to_point(ctx):
    inside = str(ctx.raw("point_location", default="CENTROID")).upper() == "INSIDE"
    ctx.make("native:pointonsurface" if inside else "native:centroids")
    ctx.link("INPUT", "in_features")
    ctx.set("ALL_PARTS", False)
    ctx.out("out_feature_class")
    return ctx.finish()


def _h_make_layer(ctx):
    where = ctx.expression("where_clause") if ctx.entry("where_clause") else ""
    if not where and where is not None:
        # a layer made from the data: later steps read the data itself
        ctx.alias("out_layer", "in_features")
        ctx.alias("out_view", "in_table")
        ctx.step.algorithm = "-"
        ctx.note(tr("layers made from data are read directly by the next steps"))
        return True
    ctx.make("native:extractbyexpression")
    ctx.link("INPUT", "in_features", "in_table")
    if where is None:
        ctx.link("EXPRESSION", "where_clause")
    else:
        ctx.set("EXPRESSION", where)
    ctx.out("out_layer")
    ctx.out("out_view")
    return ctx.finish()


def _h_select_attribute(ctx):
    sel = str(ctx.raw("selection_type", default="NEW_SELECTION")).upper()
    if sel == "CLEAR_SELECTION":
        ctx.alias("out_layer_or_view", "in_layer_or_view")
        ctx.step.algorithm = "-"
        ctx.note(tr("clearing the selection: the next steps use all features"))
        return True
    expr = ctx.expression("where_clause")
    ctx.make("native:extractbyexpression")
    ctx.link("INPUT", "in_layer_or_view")
    if expr is None:
        ctx.link("EXPRESSION", "where_clause")
    else:
        expr = expr or "TRUE"
        if str(ctx.raw("invert_where_clause", default="")).upper() in ("INVERT", "TRUE"):
            expr = f"NOT ({expr})"
        ctx.set("EXPRESSION", expr)
    if sel != "NEW_SELECTION":
        ctx.note(tr("selection type {s} is done as a new selection").format(s=sel))
    ctx.note(tr("the selection becomes a new layer with the selected features"))
    ctx.out("out_layer_or_view")
    return ctx.finish()


_LOC_PREDICATES = {"INTERSECT": 0, "INTERSECT_3D": 0, "CONTAINS": 1, "COMPLETELY_CONTAINS": 1,
                   "CONTAINS_CLEMENTINI": 1, "ARE_IDENTICAL_TO": 3, "BOUNDARY_TOUCHES": 4,
                   "SHARE_A_LINE_SEGMENT_WITH": 4, "WITHIN": 6, "COMPLETELY_WITHIN": 6, "WITHIN_CLEMENTINI": 6,
                   "CROSSED_BY_THE_OUTLINE_OF": 7, "HAVE_THEIR_CENTER_IN": 6}


def _h_select_location(ctx):
    overlap = str(ctx.raw("overlap_type", default="INTERSECT")).upper()
    dist = parse_distance(ctx.raw("search_distance"))
    srcs = ctx.sources("in_layer")
    if not srcs:
        return False
    if overlap in ("WITHIN_A_DISTANCE", "WITHIN_A_DISTANCE_GEODESIC", "WITHIN_A_DISTANCE_3D") or (
            dist and dist[0] > 0 and overlap.startswith("INTERSECT")):
        ctx.make("native:extractwithindistance")
        ctx.set("INPUT", srcs[:1])
        ctx.link("REFERENCE", "select_features")
        ctx.set("DISTANCE", _distance(ctx, "search_distance"))
    else:
        if overlap not in _LOC_PREDICATES:
            ctx.note(tr("relationship {m} is selected as 'intersects'").format(m=overlap))
        pred = _LOC_PREDICATES.get(overlap, 0)
        if str(ctx.raw("invert_spatial_relationship", default="")).upper() in ("INVERT", "TRUE"):
            if pred != 0:
                ctx.note(tr("the inverted relationship is done as 'disjoint'"))
            pred = 2
        ctx.make("native:extractbylocation")
        ctx.set("INPUT", srcs[:1])
        ctx.link("INTERSECT", "select_features")
        ctx.set("PREDICATE", [pred])
    if len(srcs) > 1:
        ctx.note(tr("only the first input layer is selected from"))
    sel = str(ctx.raw("selection_type", default="NEW_SELECTION")).upper()
    if sel != "NEW_SELECTION":
        ctx.note(tr("selection type {s} is done as a new selection").format(s=sel))
    ctx.note(tr("the selection becomes a new layer with the selected features"))
    ctx.out("out_layer_or_view")
    ctx.out("out_layers_or_views")
    return ctx.finish()


def _h_delete_field(ctx):
    fields = ctx.raw("drop_field")
    if not fields or ctx.dynamic("drop_field"):
        return False
    names = [f.strip() for f in re.split(r"[;,]", str(fields)) if f.strip()]
    keep = str(ctx.raw("method", default="DELETE_FIELDS")).upper() == "KEEP_FIELDS"
    ctx.make("native:retainfields" if keep else "native:deletecolumn")
    ctx.link("INPUT", "in_table")
    ctx.set("FIELDS" if keep else "COLUMN", names)
    ctx.out("out_table")
    return ctx.finish()


_HANDLERS = {
    "analysis.buffer": _h_buffer, "analysis.pairwisebuffer": _h_buffer,
    "analysis.clip": _h_overlay("native:clip", "clip_features"),
    "analysis.pairwiseclip": _h_overlay("native:clip", "clip_features"),
    "analysis.intersect": _h_intersect, "analysis.pairwiseintersect": _h_intersect,
    "management.dissolve": _h_dissolve, "analysis.pairwisedissolve": _h_dissolve,
    "analysis.select": _h_select,
    "management.project": _h_project,
    "management.merge": _h_merge,
    "analysis.union": _h_union,
    "analysis.erase": _h_overlay("native:difference", "erase_features"),
    "analysis.pairwiseerase": _h_overlay("native:difference", "erase_features"),
    "analysis.spatialjoin": _h_spatial_join,
    "management.calculatefield": _h_calculate,
    "management.addfield": _h_add_field,
    "management.copyfeatures": _h_copy, "conversion.exportfeatures": _h_copy,
    "conversion.featureclasstofeatureclass": _h_copy, "conversion.exporttable": _h_copy,
    "conversion.tabletotable": _h_copy, "management.copyrows": _h_copy,
    "management.featuretopoint": _h_feature_to_point,
    "management.makefeaturelayer": _h_make_layer, "management.maketableview": _h_make_layer,
    "management.selectlayerbyattribute": _h_select_attribute,
    "management.selectlayerbylocation": _h_select_location,
    "management.deletefield": _h_delete_field,
}


# ---- public API ---------------------------------------------------------------------------------------------------
def convert_model(info, toolbox_path, group=None):
    """One ModelBuilder model -> Conversion (with .algorithm, a QgsProcessingModelAlgorithm, unless error)."""
    group = group or tr("Imported from ArcGIS")
    if info.kind != "model":
        return Conversion(toolbox=toolbox_path, model=info.name, title=info.title,
                          error=tr("not a model"))
    if info.protected:
        return Conversion(toolbox=toolbox_path, model=info.name, title=info.title,
                          error=tr("the model is password-protected: remove the password in ArcGIS Pro first"))
    if not info.graph:
        conv = Conversion(toolbox=toolbox_path, model=info.name, title=info.title,
                          error=tr("ArcMap toolbox: only the tool list can be read. Open it in ArcGIS Pro and "
                                   "save it as an .atbx toolbox to convert the model."))
        conv.steps = [Step(title=t, tool=t, algorithm=mapped_algorithm(t) or "") for t in info.tools]
        conv.unmapped = [t for t in info.tools if tool_status(t) != "mapped"]
        return conv
    return _Converter(info, toolbox_path, group).run()


def models_folder():
    """The folder whose .model3 files appear in the toolbox under Models."""
    try:
        from processing.modeler.ModelerUtils import ModelerUtils

        folders = ModelerUtils.modelsFolders()
        if folders:
            return folders[0]
    except Exception:  # noqa: BLE001 - Processing not loaded (tests)
        pass
    from qgis.core import QgsApplication

    return os.path.join(QgsApplication.qgisSettingsDirPath(), "processing", "models")


def _safe_file_name(text):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .")
    return name[:120] or "model"


def save_model(conv, folder):
    """Write conv.algorithm as <folder>/<toolbox> - <model>.model3; an earlier import of the same model is
    replaced, any other file with that name is kept (a number is added)."""
    os.makedirs(folder, exist_ok=True)
    stem = _safe_file_name(f"{os.path.splitext(os.path.basename(conv.toolbox))[0]} - {conv.title}")
    path, n = os.path.join(folder, stem + ".model3"), 2
    while os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                ours = MARKER in f.read() and os.path.abspath(conv.toolbox) in _model_source(path)
        except OSError:
            ours = False
        if ours:
            break
        path, n = os.path.join(folder, f"{stem} ({n}).model3"), n + 1
    if not conv.algorithm.toFile(path):
        raise OSError(tr("could not write {p}").format(p=path))
    conv.file = path
    return path


def _model_source(path):
    from qgis.core import QgsProcessingModelAlgorithm

    m = QgsProcessingModelAlgorithm()
    if not m.fromFile(path):
        return ""
    return os.path.abspath(str((m.variables() or {}).get(MARKER) or ""))


def refresh_models():
    """Show newly saved models in the toolbox."""
    from qgis.core import QgsApplication

    prov = QgsApplication.processingRegistry().providerById("model")
    if prov is not None:
        prov.refreshAlgorithms()


def convert_toolbox(path, folder=None, save=True):
    """Every model of a toolbox -> [Conversion]; saved to folder (default: the user's models folder)."""
    box = read_toolbox(path)
    out = []
    for info in box.models:
        conv = convert_model(info, path, group=tr("Imported from ArcGIS") + " - " + box.title)
        if save and conv.algorithm is not None:
            save_model(conv, folder or models_folder())
        out.append(conv)
    if save and folder is None:
        refresh_models()
    return out


def report_html(conversions, title=None):
    """A conversion report (HTML text) for a list of Conversions."""
    from .report import conversion_html

    return conversion_html(conversions, title)
