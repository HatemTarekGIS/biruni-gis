"""One map, many outputs: the print PDF, an image at the purpose's resolution, a 16:9 slide, social posts in their
sizes (each re-laid out, not just cropped) and a small "map card" with what the map is, in one click."""
import datetime as dt
import json
import os
import re

from qgis.core import QgsLayoutExporter, QgsProject

from ..i18n import tr
from . import auto_layout, intents

# output id -> (intent used to re-lay out the map or None for the main layout, file kind)
PLAN = {
    "pdf": (None, "pdf"),
    "png": (None, "png"),
    "slide": ("slide", "png"),
    "social_square": ("social_square", "png"),
    "social_portrait": ("social_portrait", "png"),
    "story": ("story", "png"),
    "card": (None, "json"),
}


def safe_name(text):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(text or "")).strip(" .") or "map"
    return name[:80]


def export_layout(layout, path, dpi=300, lean=True):
    """PDF (lean, links clickable) or PNG/JPG at ``dpi``; returns True on success."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.lower().endswith(".pdf"):
        from ..cartography import export_pdf

        ok, _n = export_pdf(layout, path, lean=lean, links=True)
        return bool(ok)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = dpi
    return QgsLayoutExporter(layout).exportToImage(path, settings) == QgsLayoutExporter.ExportResult.Success


def map_card(layout, files=()):
    """What the map is, for the folder it is exported to (title, purpose, scale, CRS, extent, sources, date)."""
    mp = layout.itemById("map") or next((i for i in layout.items() if i.__class__.__name__ == "QgsLayoutItemMap"),
                                        None)
    project = layout.project() or QgsProject.instance()
    layers = mp.layers() if mp is not None else []
    card = {
        "title": layout.customProperty(auto_layout.PROP + "title") or layout.name(),
        "purpose": layout.customProperty(auto_layout.PROP + "purpose") or "",
        "scale": f"1:{round(mp.scale()):,}" if mp is not None and mp.scale() > 0 else "",
        "crs": mp.crs().authid() if mp is not None else project.crs().authid(),
        "crs_name": mp.crs().description() if mp is not None else project.crs().description(),
        "extent": mp.extent().toString(3) if mp is not None else "",
        "layers": [l.name() for l in layers],
        "sources": auto_layout.credits(layers),
        "page_mm": [round(layout.pageCollection().page(0).pageSize().width(), 2),
                    round(layout.pageCollection().page(0).pageSize().height(), 2)],
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "files": [os.path.basename(f) for f in files],
        "made_with": "Biruni GIS",
    }
    return card


def export_all(layout, folder, outputs=("pdf", "png"), basename=None, feedback=None):
    """Export ``layout`` in every requested output (ids of PLAN) into ``folder``.
    Returns {output id: path or None when it failed}."""
    project = layout.project() or QgsProject.instance()
    purpose = intents.get(layout.customProperty(auto_layout.PROP + "purpose") or "report")
    base = safe_name(basename or layout.customProperty(auto_layout.PROP + "title") or layout.name())
    os.makedirs(folder, exist_ok=True)
    done = {}
    todo = [o for o in outputs if o in PLAN and o != "card"]
    for k, out in enumerate(todo):
        if feedback is not None:
            feedback(k, len(todo), out)
        target, kind = PLAN[out]
        if target is None:
            path = os.path.join(folder, f"{base}.{kind}")
            ok = export_layout(layout, path, dpi=purpose.dpi)
        else:
            intent = intents.get(target)
            other = _rebuild(layout, target, project)
            path = os.path.join(folder, f"{base} - {target}.{kind}")
            ok = export_layout(other, path, dpi=intent.dpi)
        done[out] = path if ok else None
    if "card" in outputs:
        path = os.path.join(folder, f"{base} - map card.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(map_card(layout, [p for p in done.values() if p]), f, ensure_ascii=False, indent=1)
        done["card"] = path
    return done


def _rebuild(layout, purpose, project):
    """The same map (layers, extent, title, language) laid out for another purpose, outside the layout manager."""
    get = layout.customProperty
    ids = [i for i in (get(auto_layout.PROP + "layers") or "").split(",") if i]
    layers = [project.mapLayer(i) for i in ids if project.mapLayer(i) is not None] or None
    from qgis.core import QgsGeometry

    wkt = get(auto_layout.PROP + "extent") or ""
    ext = QgsGeometry.fromWkt(wkt).boundingBox() if wkt else None
    return auto_layout.build(project, purpose, layers, ext, get(auto_layout.PROP + "title"),
                             get(auto_layout.PROP + "lang"), get(auto_layout.PROP + "country"), add_to_manager=False)


def summary(done):
    """One line per output for a message to the user."""
    ok = [os.path.basename(p) for p in done.values() if p]
    bad = [k for k, p in done.items() if not p]
    text = tr("{n} files written: {files}").format(n=len(ok), files=", ".join(ok))
    if bad:
        text += "\n" + tr("Not written: {what}").format(what=", ".join(bad))
    return text
