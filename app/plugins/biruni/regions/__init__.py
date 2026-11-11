"""The region registry: one place that knows, per country, what Biruni offers there - deed language and units,
calendars, text encodings, coordinate systems, map-sheet presets, file formats, official cadastre services and
the tools that matter - so the interface can show "My country" instead of everything to everyone.

Each topic lives in its own engine module (units, calendars, regional_crs …) with a for_country(cc) function;
the registry only gathers them. A missing engine simply leaves its part empty."""
import importlib

from ..i18n import lang, tr
from . import detect
from .countries import COUNTRIES, NATIONAL_TOOLS, PENDING_TOOLS

_ENGINES = {}


def _engine(name):
    """An engine module of the plugin, or None when it is not there (yet)."""
    if name not in _ENGINES:
        try:
            _ENGINES[name] = importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{name}")
        except Exception:  # noqa: BLE001 - an engine that fails to import must not hide the others
            _ENGINES[name] = None
    return _ENGINES[name]


def _ask(name, cc, default):
    mod = _engine(name)
    fn = getattr(mod, "for_country", None) if mod is not None else None
    if fn is None:
        return default
    try:
        return fn(cc)
    except Exception:  # noqa: BLE001
        return default


class Region:
    """Everything Biruni knows about working in one country."""

    def __init__(self, cc):
        self.cc = (cc or "").upper()
        c = COUNTRIES.get(self.cc)
        self.known = c is not None
        self.name_en = c.en if c else self.cc
        self.name_ar = c.ar if c else self.cc
        self.native = c.native if c else ""
        self.ui_language = c.ui if c else "en"
        self.agency = c.agency if c else ""
        self.link = c.link if c else ""
        self.crs_hints = list(c.crs) if c else []
        self.area_units = _ask("units", self.cc, ["m2"])
        units = _engine("units")
        self.length_unit = units.length_for_country(self.cc) if units and hasattr(units, "length_for_country") \
            else "m"
        self.bearing_notation = _ask("bearings", self.cc, "azimuth")
        self.deed_language = _ask("deed_languages", self.cc, "en")
        self.calendars = _ask("calendars", self.cc, [])
        self.encodings = _ask("text_encoding", self.cc, [])
        self.crs = _ask("regional_crs", self.cc, [])
        self.sheet_presets = _ask("sheet_presets", self.cc, [])
        formats = _engine("formats")
        self.formats = formats.for_country(self.cc) if formats and hasattr(formats, "for_country") else []
        from ..algorithms.cadastre_lookup import connectors_for

        self.cadastre = connectors_for(self.cc)

    @property
    def name(self):
        """The country name in the interface language."""
        return self.name_ar if lang() == "ar" else self.name_en

    def __repr__(self):
        return f"<Region {self.cc} {self.name_en}>"


def countries():
    """All countries of the registry as (code, display name), sorted by display name."""
    ar = lang() == "ar"
    return sorted(((cc, c.ar if ar else c.en) for cc, c in COUNTRIES.items()), key=lambda t: t[1])


def region(cc=None):
    """The Region of a country (the current one when cc is None)."""
    return Region(cc if cc is not None else current_country())


def current_country(project=None):
    return detect.detect(project)[0]


def set_country(cc):
    """Remember the user's country; None or '' goes back to automatic detection."""
    from qgis.core import QgsSettings

    if cc:
        QgsSettings().setValue(detect.SETTING, cc.upper())
    else:
        QgsSettings().remove(detect.SETTING)


def _registered(tool_id):
    from qgis.core import QgsApplication

    return QgsApplication.processingRegistry().algorithmById(tool_id) is not None


def tool_ids(cc):
    """Every tool id the registry recommends for a country (registered or still pending), most useful first."""
    r = region(cc)
    c = COUNTRIES.get(r.cc)
    out = []

    def add(*ids):
        out.extend(i for i in ids if i not in out)

    if r.cadastre and any(con.available for con in r.cadastre):
        add("biruni:cadastrelookup")
    if c is not None:
        add(*c.tools)
    if r.deed_language != "en" or (c is not None and c.ui in ("en", "sw")):
        add("biruni:deedtoparcel", "biruni:parceldescription", "biruni:deedvssurvey")
    if r.calendars:
        add("biruni:convertdates")
        if "hijri" in r.calendars:
            add("biruni:hijridates")
    add("biruni:guesscrs", "biruni:wrongplace", "biruni:localization")
    add(*NATIONAL_TOOLS.get(r.cc, ()))
    add(*PENDING_TOOLS.get(r.cc, ()))
    return out


def toolbox_entries(cc=None, include_pending=False):
    """Tool ids for a dynamic "My country" toolbox category: only those registered in Processing, unless
    include_pending. Empty when the country is unknown."""
    cc = cc if cc is not None else current_country()
    if not cc:
        return []
    ids = tool_ids(cc)
    return ids if include_pending else [i for i in ids if _registered(i)]


def ribbon_menu(plugin, menu):
    """Fill a "My country" ribbon menu: the country (opens the region window) and its tools."""
    from qgis.core import QgsApplication

    from .. import icons

    menu.clear()
    cc = current_country()
    r = region(cc) if cc else None
    title = (tr("My country: {c}").format(c=r.name) if r else tr("Choose my country…"))
    menu.addAction(icons.icon("world-pin", plugin.theme.tokens()["icon"]), title, lambda: open_dialog(plugin))
    if r is None:
        return
    menu.addSeparator()
    reg = QgsApplication.processingRegistry()
    for tid in toolbox_entries(cc):
        alg = reg.algorithmById(tid)
        menu.addAction(alg.icon(), alg.displayName(), lambda _=False, t=tid: plugin.runner.open_algorithm(t))


def open_dialog(plugin):
    from .region_dialog import RegionDialog

    dlg = RegionDialog(plugin)
    if dlg.exec() and plugin is not None and getattr(plugin, "toolbox", None) is not None:
        plugin.toolbox.load()  # the "My country" category follows the new choice
    return dlg
