"""Floor plans -> indoor.gpkg + indoor_imdf.zip + indoor_report.txt (no UI: used by the Processing tool and
by the tests)."""
import os
import re

from ..i18n import tr
from . import cad, graph, store
from .recognize import Options, recognize


def parse_plans(text, base_dir=""):
    """'plan_gf.dxf=0; plan_1.dxf=1:First floor' (also one per line) -> [(path, level, name)]."""
    out = []
    for chunk in re.split(r"[;\n]+", text or ""):
        chunk = chunk.strip().strip('"')
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(chunk)
        path, rest = chunk.rsplit("=", 1)
        path = path.strip().strip('"')
        level, _, name = rest.partition(":")
        level = int(level.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩−", "0123456789-")))
        if base_dir and not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        out.append((path, level, name.strip()))
    return out


def run(plans, crs, out_dir, opts=None, min_door_width=0.9, storey=3.5, units=0.0, encoding="",
        venue_name="", feedback=None):
    """plans: [(path, level_id, name)]. Returns dict(gpkg, imdf, imdf_skipped, report, report_path, levels,
    nodes, edges)."""
    opts = opts or Options()
    seen = set()
    levels = []
    extra = []
    for n, (path, level, name) in enumerate(plans):
        if level in seen:
            raise ValueError(tr("Level {l} is given twice.").format(l=level))
        seen.add(level)
        if feedback:
            feedback.pushInfo(tr("Reading {p} as level {l}…").format(p=os.path.basename(path), l=level))
        plan = cad.read_plan(path, units, encoding, feedback)
        if feedback and plan.scale != 1.0:
            feedback.pushInfo(tr("Drawing units: {s} m per unit.").format(s=f"{plan.scale:g}"))
        res = recognize(plan, opts)
        res.warnings = list(plan.warnings) + res.warnings
        store.assign_ids(level, res)
        levels.append((level, name or store.default_level_name(level), level * storey, res))
        if feedback:
            feedback.setProgress(10 + 60 * (n + 1) / max(len(plans), 1))
    levels.sort(key=lambda t: t[0])
    nodes, edges = graph.build([(lv, el, r) for lv, _n, el, r in levels], min_door_width, storey)
    os.makedirs(out_dir, exist_ok=True)
    gpkg = store.write_gpkg(os.path.join(out_dir, "indoor.gpkg"), crs, levels, nodes, edges)
    imdf = os.path.join(out_dir, "indoor_imdf.zip")
    venue = venue_name or os.path.splitext(os.path.basename(plans[0][0]))[0]
    why = store.write_imdf(imdf, crs, levels, venue)
    if why:
        extra.append(tr("IMDF was not written: {why}.").format(why=why))
        imdf = ""
    text = store.report(levels, nodes, edges, min_door_width, extra)
    report_path = os.path.join(out_dir, "indoor_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"gpkg": gpkg, "imdf": imdf, "imdf_skipped": why, "report": text, "report_path": report_path,
            "levels": levels, "nodes": nodes, "edges": edges}
