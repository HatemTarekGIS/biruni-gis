"""Light mode for weak computers.

``apply(iface, True)`` switches QGIS to lighter rendering settings and keeps the
previous values, so ``apply(iface, False)`` puts every one of them back exactly
(a setting that did not exist before is removed again). The map canvas follows
at once; vector simplification applies to layers added afterwards.

``optimise_layers`` adds the spatial indexes that are missing, finds big
rasters without overviews (and builds them when asked) and lists layers that
are too big to draw comfortably. ``system_check`` looks at memory, processor
and free disk to suggest light mode on the first run.
"""
import ctypes
import json
import os
import shutil

from qgis.core import (QgsApplication, QgsFeatureSource, QgsProject, QgsRasterLayer, QgsSettings,
                       QgsVectorDataProvider, QgsVectorLayer)

from .i18n import tr

ON_KEY = "biruni/lightmode/on"
SAVED_KEY = "biruni/lightmode/previous"
SUGGESTED_KEY = "biruni/lightmode/suggested"
BIG_LAYER = 500_000           # features
BIG_RASTER = 4000             # pixels on the longer side
LOW_RAM_GB = 8
LOW_CORES = 4
LOW_DISK_GB = 5


def light_settings(cores=None):
    """The QGIS settings light mode uses (key -> value)."""
    cores = cores or os.cpu_count() or 2
    return {
        "qgis/enable_render_caching": True,          # redraw from cache when nothing changed
        "qgis/parallel_rendering": True,
        "qgis/max_threads": max(1, min(cores - 1, 2)),  # leave cores for the interface and other programs
        "qgis/map_update_interval": 500,             # fewer partial redraws while drawing
        "qgis/enable_anti_aliasing": False,
        "qgis/simplifyDrawingHints": 1,              # simplify geometries while drawing
        "qgis/simplifyAlgorithm": 0,                 # distance (the fastest)
        "qgis/simplifyDrawingTol": 1.5,              # pixels
        "qgis/simplifyLocal": True,                  # simplify on this computer, not on the server
        "qgis/simplifyMaxScale": 1.0,                # at every scale
        "qgis/enableMapTips": False,
        # the Browser panel: no watching of folders, no looking inside zip files, files known by their
        # extension only (opening every file to see what it is costs processor and disk)
        "qgis/monitorDirectoriesInBrowser": False,
        "qgis/scanZipInBrowser2": "no",
        "qgis/scanItemsInBrowser2": "extension",
    }


# ---- what light mode changes in Biruni itself (kept for this session; set again at each start) -------
LIGHT_GDAL_CACHE_MB = 128       # the raster block cache (GDAL's default is 5% of the computer's memory)
LIGHT_AUTOSAVE_MS = 120_000     # autosave looks for changes every 2 minutes instead of 30 s
_runtime = {"gdal_cache": None, "autosave_ms": None, "meter": None}


def apply_runtime(plugin, on):
    """The parts of light mode that are not QGIS settings: the raster cache, how often autosave looks,
    and the memory gauge in the status bar. Called when the mode is switched and when Biruni starts."""
    try:
        from osgeo import gdal

        if on:
            if _runtime["gdal_cache"] is None:
                _runtime["gdal_cache"] = gdal.GetCacheMax()
            gdal.SetCacheMax(min(_runtime["gdal_cache"], LIGHT_GDAL_CACHE_MB * 1024 * 1024))
        elif _runtime["gdal_cache"] is not None:
            gdal.SetCacheMax(_runtime["gdal_cache"])
            _runtime["gdal_cache"] = None
    except Exception:  # noqa: BLE001 - only an optimisation
        pass
    saver = getattr(plugin, "autosave", None)
    timer = getattr(saver, "timer", None)
    if timer is not None:
        if on:
            if _runtime["autosave_ms"] is None:
                _runtime["autosave_ms"] = timer.interval()
            timer.setInterval(max(timer.interval(), LIGHT_AUTOSAVE_MS))
        elif _runtime["autosave_ms"] is not None:
            timer.setInterval(_runtime["autosave_ms"])
            _runtime["autosave_ms"] = None
    _meter(plugin, on)


# ---- memory: how much Biruni uses, and giving back what it can -----------------------------------
def _this_process():
    """This process's handle, at the full pointer width (a 32-bit int would be cut on 64-bit Windows)."""
    f = ctypes.windll.kernel32.GetCurrentProcess
    f.restype = ctypes.c_void_p
    return f()


def process_memory_mb():
    """(working set, private) of this process in MB - what Task Manager shows - or (None, None)."""
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    try:
        c = Counters()
        c.cb = ctypes.sizeof(Counters)
        get = ctypes.windll.kernel32.K32GetProcessMemoryInfo
        get.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        if get(_this_process(), ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 1024 ** 2, c.PrivateUsage / 1024 ** 2
    except Exception:  # noqa: BLE001 - not Windows
        pass
    return None, None


def free_memory(iface=None):
    """Give back memory that can be rebuilt: cached map images, icon pictures, the raster block cache,
    Python's garbage, then let Windows take the unused pages back. Returns (before MB, after MB).
    Nothing the user made is touched; the map redraws what it needs."""
    import gc

    from qgis.PyQt.QtGui import QPixmapCache

    before = process_memory_mb()[0]
    if iface is not None:
        try:
            iface.mapCanvas().clearCache()
        except Exception:  # noqa: BLE001
            pass
    QPixmapCache.clear()
    from . import icons

    icons.forget_pictures()
    try:
        from osgeo import gdal

        cap = gdal.GetCacheMax()
        gdal.SetCacheMax(0)  # empties the raster block cache
        gdal.SetCacheMax(cap)
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    try:
        empty = ctypes.windll.kernel32.K32EmptyWorkingSet
        empty.argtypes = [ctypes.c_void_p]
        empty(_this_process())
    except Exception:  # noqa: BLE001
        pass
    return before, process_memory_mb()[0]


def free_memory_command(plugin):
    from .version import APP_NAME

    before, after = free_memory(plugin.iface)
    if before is None:
        return
    plugin.iface.messageBar().pushSuccess(APP_NAME, tr("Memory in use: {a:,.0f} MB (was {b:,.0f} MB).").format(
        a=after, b=before))


def _meter(plugin, on):
    """A small memory gauge in the status bar while light mode is on; a click frees memory."""
    from qgis.PyQt.QtCore import QTimer
    from qgis.PyQt.QtWidgets import QToolButton

    m = _runtime["meter"]
    if not on:
        if m is not None:
            m["timer"].stop()
            plugin.iface.mainWindow().statusBar().removeWidget(m["button"])
            m["button"].deleteLater()
            _runtime["meter"] = None
        return
    if m is not None or process_memory_mb()[0] is None:
        return
    btn = QToolButton()
    btn.setAutoRaise(True)
    btn.setToolTip(tr("Memory used by Biruni GIS. Click to free what can be rebuilt (map and icon pictures)."))
    btn.clicked.connect(lambda: (free_memory_command(plugin), update()))
    timer = QTimer(btn)
    timer.setInterval(5000)

    def update():
        ws = process_memory_mb()[0]
        if ws is not None:
            btn.setText(tr("Memory: {n:,.0f} MB").format(n=ws))

    timer.timeout.connect(update)
    plugin.iface.mainWindow().statusBar().addPermanentWidget(btn)
    update()
    timer.start()
    _runtime["meter"] = {"button": btn, "timer": timer}


def is_on():
    return QgsSettings().value(ON_KEY, False, type=bool)


def _snapshot(keys):
    s = QgsSettings()
    return {k: [s.contains(k), s.value(k)] for k in keys}


def _restore(saved):
    s = QgsSettings()
    for k, (present, value) in saved.items():
        if present:
            s.setValue(k, value)
        else:
            s.remove(k)


def apply(iface, on):
    """Switch light mode on or off. Returns the settings that were changed."""
    s = QgsSettings()
    settings = light_settings()
    if on:
        if not is_on():  # keep the user's own values only once
            s.setValue(SAVED_KEY, json.dumps(_snapshot(settings)))
        for k, v in settings.items():
            s.setValue(k, v)
        s.setValue(ON_KEY, True)
    else:
        raw = s.value(SAVED_KEY, "")
        if raw:
            try:
                _restore(json.loads(raw))
            except ValueError:
                pass
        s.remove(SAVED_KEY)
        s.setValue(ON_KEY, False)
    _apply_live(iface)
    return list(settings)


def _apply_live(iface):
    """Make the running application follow the settings without a restart."""
    s = QgsSettings()
    threads = s.value("qgis/max_threads", -1, type=int)
    try:
        QgsApplication.setMaxThreads(threads)
    except Exception:
        pass
    if iface is None:
        return
    canvas = iface.mapCanvas()
    canvas.setCachingEnabled(s.value("qgis/enable_render_caching", True, type=bool))
    canvas.setParallelRenderingEnabled(s.value("qgis/parallel_rendering", True, type=bool))
    canvas.setMapUpdateInterval(s.value("qgis/map_update_interval", 250, type=int))
    canvas.enableAntiAliasing(s.value("qgis/enable_anti_aliasing", True, type=bool))
    canvas.refresh()


def toggle(plugin):
    """The ribbon button: switch light mode and tidy the layers of the project when switching it on."""
    from .version import APP_NAME

    on = not is_on()
    apply(plugin.iface, on)
    apply_runtime(plugin, on)
    if on:
        free_memory(plugin.iface)
    bar = plugin.iface.messageBar()
    if on:
        report = optimise_layers()
        bar.pushSuccess(APP_NAME, tr("Light mode is on: lighter drawing for this computer.") + "\n" +
                        report_text(report))
    else:
        bar.pushInfo(APP_NAME, tr("Light mode is off: your previous drawing settings are back."))
    return on


def suggest_on_first_run(plugin):
    """Call once after start-up: on a weak computer, offer light mode with a button."""
    if not should_suggest():
        return
    from qgis.PyQt.QtWidgets import QPushButton
    from qgis.core import Qgis

    from .version import APP_NAME

    info = system_check()
    bar = plugin.iface.messageBar()
    item = bar.createMessage(APP_NAME, tr("This computer has {what}. Light mode makes the map draw faster.").format(
        what=", ".join(info["reasons"])))
    btn = QPushButton(tr("Turn on light mode"))
    btn.clicked.connect(lambda: (bar.popWidget(item), toggle(plugin)))
    item.layout().addWidget(btn)
    bar.pushWidget(item, Qgis.MessageLevel.Info, 0)


# ---- layers -------------------------------------------------------------
def _raster_path(layer):
    path = layer.source().split("|")[0]
    return path if os.path.isfile(path) else None


def _has_overviews(path):
    from osgeo import gdal

    ds = gdal.Open(path)
    if ds is None:
        return True  # cannot tell: leave it alone
    try:
        return ds.RasterCount == 0 or ds.GetRasterBand(1).GetOverviewCount() > 0
    finally:
        ds = None


def build_overviews(path):
    """External overviews (.ovr) next to the raster; the raster itself is not changed."""
    from osgeo import gdal

    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        return False
    try:
        side = max(ds.RasterXSize, ds.RasterYSize)
        levels, f = [], 2
        while side / f >= 256:
            levels.append(f)
            f *= 2
        if not levels:
            return False
        gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
        return ds.BuildOverviews("AVERAGE", levels) == 0
    finally:
        ds = None


def optimise_layers(layers=None, build_raster_overviews=False, big_layer=BIG_LAYER):
    """Spatial indexes where missing, overviews for big rasters, and a list of heavy layers.

    Returns a report: {"indexed": [names], "index_failed": [names], "overviews": [names],
    "need_overviews": [names], "big": [(name, count)]}."""
    layers = list(QgsProject.instance().mapLayers().values()) if layers is None else layers
    report = {"indexed": [], "index_failed": [], "overviews": [], "need_overviews": [], "big": []}
    for lyr in layers:
        if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial():
            prov = lyr.dataProvider()
            if prov.name() != "memory" and \
                    prov.hasSpatialIndex() == QgsFeatureSource.SpatialIndexPresence.SpatialIndexNotPresent:
                if prov.capabilities() & QgsVectorDataProvider.Capability.CreateSpatialIndex and \
                        prov.createSpatialIndex():
                    report["indexed"].append(lyr.name())
                else:
                    report["index_failed"].append(lyr.name())
            n = lyr.featureCount()
            if n > big_layer:
                report["big"].append((lyr.name(), n))
        elif isinstance(lyr, QgsRasterLayer) and lyr.isValid() and lyr.providerType() == "gdal":
            path = _raster_path(lyr)
            if not path or max(lyr.width(), lyr.height()) < BIG_RASTER or _has_overviews(path):
                continue
            if build_raster_overviews and build_overviews(path):
                report["overviews"].append(lyr.name())
                lyr.dataProvider().reloadData()
            else:
                report["need_overviews"].append(lyr.name())
    return report


def report_text(report):
    lines = []
    if report["indexed"]:
        lines.append(tr("Spatial index added: {names}").format(names=", ".join(report["indexed"])))
    if report["index_failed"]:
        lines.append(tr("No spatial index could be added to: {names}").format(names=", ".join(report["index_failed"])))
    if report["overviews"]:
        lines.append(tr("Overviews built for: {names}").format(names=", ".join(report["overviews"])))
    if report["need_overviews"]:
        lines.append(tr("Big rasters without overviews (slow to draw): {names}").format(
            names=", ".join(report["need_overviews"])))
    for name, n in report["big"]:
        lines.append(tr("{name} has {n:,} features: show it only when zoomed in (scale-dependent visibility)"
                        ).format(name=name, n=n))
    return "\n".join(lines) or tr("All layers are already in good shape.")


# ---- the computer ---------------------------------------------------------
def total_ram_gb():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        st = MemoryStatus()
        st.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys / 1024 ** 3
    except Exception:
        pass
    return None


def system_check(path=None):
    """Memory, processor and free disk, and whether light mode is worth suggesting.

    Returns {"ram_gb", "cores", "free_disk_gb", "suggest": bool, "reasons": [texts]}."""
    ram = total_ram_gb()
    cores = os.cpu_count() or 1
    try:
        free = shutil.disk_usage(path or os.path.expanduser("~")).free / 1024 ** 3
    except OSError:
        free = None
    reasons = []
    if ram is not None and ram < LOW_RAM_GB:
        reasons.append(tr("{n:.0f} GB of memory").format(n=ram))
    if cores < LOW_CORES:
        reasons.append(tr("{n} processor cores").format(n=cores))
    if free is not None and free < LOW_DISK_GB:
        reasons.append(tr("{n:.1f} GB free on the disk").format(n=free))
    return {"ram_gb": ram, "cores": cores, "free_disk_gb": free, "suggest": bool(reasons), "reasons": reasons}


def should_suggest():
    """True once, on the first run of a computer that would benefit from light mode."""
    s = QgsSettings()
    if is_on() or s.value(SUGGESTED_KEY, False, type=bool):
        return False
    s.setValue(SUGGESTED_KEY, True)
    return system_check()["suggest"]
