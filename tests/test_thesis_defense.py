r"""Thesis defence presentation (thesis/defense.py): PowerPoint, the HTML page for phones, the PDF, the dialog.

Run:  engine\bin\python-qgis.bat tests\test_thesis_defense.py

BIRUNI_DEFENSE_OUT=<folder> keeps the outputs there (and PNG sketches of the slides) to look at them.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout,
                       QgsProject, QgsRectangle, QgsUnitTypes, QgsVectorLayer)
from qgis.PyQt.QtGui import QImage  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

# the methodology module of another tool may or may not exist: first run as if it does not
sys.modules["biruni.thesis.methodology"] = None
from biruni.thesis import defense  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


KEEP = os.environ.get("BIRUNI_DEFENSE_OUT", "")
work = KEEP or tempfile.mkdtemp()
os.makedirs(work, exist_ok=True)

# ---- a small project: parcels, roads, wells; three print layouts ----------------------------------------------
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:32638"))
project.setEllipsoid("EPSG:7030")
X0, Y0 = 670000, 2730000
parcels = QgsVectorLayer("Polygon?crs=EPSG:32638&field=name:string&field=use:string", "Parcels", "memory")
feats = []
for i in range(4):
    for j in range(3):
        f = QgsFeature(parcels.fields())
        x, y = X0 + i * 120, Y0 + j * 90
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x, y, x + 100, y + 80)))
        f.setAttributes([f"P{i}{j}", "سكني" if (i + j) % 2 else "تجاري"])
        feats.append(f)
parcels.dataProvider().addFeatures(feats)
roads = QgsVectorLayer("LineString?crs=EPSG:32638&field=name:string", "Roads", "memory")
r = QgsFeature(roads.fields())
r.setGeometry(QgsGeometry.fromWkt(f"LineString({X0 - 20} {Y0 + 85}, {X0 + 500} {Y0 + 85})"))
r.setAttributes(["شارع الملك فهد"])
roads.dataProvider().addFeatures([r])
wells = QgsVectorLayer("Point?crs=EPSG:32638&field=id:integer", "Wells", "memory")
pts = []
for k in range(5):
    p = QgsFeature(wells.fields())
    p.setGeometry(QgsGeometry.fromWkt(f"Point({X0 + 50 + k * 90} {Y0 + 230})"))
    p.setAttributes([k])
    pts.append(p)
wells.dataProvider().addFeatures(pts)
project.addMapLayers([wells, roads, parcels])


def make_layout(name, figure=None):
    lay = QgsPrintLayout(project)
    lay.initializeDefaults()
    lay.setName(name)
    mp = QgsLayoutItemMap(lay)
    mp.attemptMove(QgsLayoutPoint(10, 20, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    mp.attemptResize(QgsLayoutSize(277, 180, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    mp.setLayers([wells, roads, parcels])
    mp.setExtent(QgsRectangle(X0 - 50, Y0 - 30, X0 + 520, Y0 + 260))
    lay.addLayoutItem(mp)
    lbl = QgsLayoutItemLabel(lay)
    lbl.setText(name)
    lbl.attemptMove(QgsLayoutPoint(10, 5, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    lbl.attemptResize(QgsLayoutSize(200, 12, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    lay.addLayoutItem(lbl)
    if figure is not None:
        lay.setCustomProperty(defense.FIGURE_PROP, json.dumps(figure, ensure_ascii=False))
    project.layoutManager().addLayout(lay)
    return lay


make_layout("Land use map", {"chapter": 3, "order": 2, "caption": "استعمالات الأراضي في منطقة الدراسة",
                             "caption_en": "Land use in the study area"})
make_layout("Plain layout without a figure key")
make_layout("Parcels map", {"chapter": 3, "order": 1, "caption": "توزيع قطع الأراضي",
                            "caption_en": "Distribution of the parcels"})

# ---- figures and numbers ----------------------------------------------------------------------------------------
figs = defense.thesis_figures(project)
check([f["layout"] for f in figs] == ["Parcels map", "Land use map", "Plain layout without a figure key"],
      "figures in thesis order: tagged by chapter/order, then untagged in project order")
check(figs[0]["number"] == ("شكل (3-1)", "Figure 3.1") and figs[1]["number"] == ("شكل (3-2)", "Figure 3.2"),
      "tagged figures are numbered «شكل (3-2)» / Figure 3.2")
check(figs[1]["caption"][1] == "Land use in the study area", "the English caption comes from caption_en")
check(figs[2]["number"] == ("شكل (1)", "Figure 1") and figs[2]["caption"][0] == "Plain layout without a figure key",
      "an untagged layout uses its name and a plain number")
stats = defense.key_stats(project)
by = {(s["layer"], s["label"][1].split(" ")[0]): s for s in stats}
check(any(s["layer"] == "Parcels" and s["text"] == "12" for s in stats), "feature count of Parcels = 12")
area = [s for s in stats if s["layer"] == "Parcels" and s["unit"][1] == "ha"]
check(bool(area) and abs(area[0]["value"] - 9.6) < 0.2, f"area of the parcels ≈ 9.6 ha ({area and area[0]['value']})")
length = [s for s in stats if s["layer"] == "Roads" and s["unit"][1] in ("m", "km")]
check(bool(length) and abs(length[0]["value"] - 520) < 3, "length of the road ≈ 520 m")
check(len(stats) <= 6, "at most six big numbers")
check(defense.summarise(["الجملة الأولى. والثانية.", "First sentence here. Second."]) ==
      ["الجملة الأولى.", "First sentence here."], "paragraphs summarised to their first sentence")
check(defense.methodology_bullets(project) is None and not defense.has_workflow(),
      "no methodology module: no methodology bullets, no workflow slide")

# ---- export in Arabic, without the methodology module --------------------------------------------------------
OPTS = {"title": "تحليل استعمالات الأراضي في حي النخيل باستخدام نظم المعلومات الجغرافية",
        "title_en": "Land use analysis of Al-Nakheel district with GIS",
        "students": ["أحمد محمد", "سارة علي"], "supervisor": "د. خالد إبراهيم", "university": "جامعة الملك سعود",
        "faculty": "كلية الآداب - قسم الجغرافيا", "date": "2026-10-30", "lang": "ar", "theme": "light",
        "accent": "#0E7C74"}
slides = defense.build_slides(project, OPTS)
kinds = [s["kind"] for s in slides]
check(kinds == ["title", "outline", "study_area", "data_method", "figure", "figure", "figure", "webmap", "results",
                "conclusions", "thanks"], f"default slide list ({kinds})")
out_ar = os.path.join(work, "ar")
steps = []
res = defense.export_presentation(out_ar, slides, OPTS, project, outputs=("pptx", "html", "pdf", "png"),
                                  feedback=lambda i, n, t: steps.append((i, n)))
check(not res["warnings"], f"no warnings ({res['warnings']})")
check(len(res["png"]) == 10, "a picture of every slide")
check(steps and steps[-1][0] == steps[-1][1], "progress reaches the end")
N = 10  # all slides but the web map, which is in the HTML page only
check(res["slides"] == N and res["html_slides"] == N + 1, f"{N} slides in the files, {N + 1} in the HTML page")
check(len(res["figures"]) == 3 and all(os.path.exists(p) for p in res["figures"]), "three figure images")
big = sorted(os.listdir(os.path.join(out_ar, "figures")))
img = QImage(os.path.join(out_ar, "figures", big[0]))
check(len(big) == 3 and img.width() >= 3400, f"figures/ holds the 300 dpi renders ({img.width()} px wide)")


def pptx_checks(path, n_expected, rtl_expected, label):
    check(os.path.exists(path), f"{label}: pptx written")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        bad = z.testzip()
        check(bad is None, f"{label}: the zip is sound")
        slides_xml = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                            key=lambda n: int(re.findall(r"\d+", n)[0]))
        check(len(slides_xml) == n_expected, f"{label}: {len(slides_xml)} slides (expected {n_expected})")
        media = [n for n in names if n.startswith("ppt/media/")]
        check(len(media) >= 4, f"{label}: pictures in the file ({len(media)})")
        from lxml import etree
        ns = {"a": defense.NS_A, "p": defense.NS_P, "r": defense.NS_R}
        arabic_paras = rtl_ok = pics = mixed_runs = 0
        for n in slides_xml:
            root = etree.fromstring(z.read(n))
            pics += len(root.findall(".//p:pic", ns))
            for r in root.iter("{%s}r" % defense.NS_A):
                rpr = r.find("a:rPr", ns)
                t = r.findtext("a:t", "", ns)
                if rpr is not None and rpr.get("lang") == "ar-SA" and re.search("[A-Za-z0-9]", t):
                    mixed_runs += 1
            for p in root.iter("{%s}p" % defense.NS_A):
                text = "".join(t.text or "" for t in p.iter("{%s}t" % defense.NS_A))
                if re.search("[\u0600-\u06FF]", text):
                    arabic_paras += 1
                    ppr = p.find("a:pPr", ns)
                    if ppr is not None and ppr.get("rtl") == "1":
                        rtl_ok += 1
        if rtl_expected:
            check(arabic_paras > 10 and rtl_ok == arabic_paras,
                  f"{label}: every Arabic paragraph is right-to-left ({rtl_ok}/{arabic_paras})")
        check(pics >= 4, f"{label}: picture shapes on the slides ({pics})")
        check(mixed_runs == 0, f"{label}: Latin text is never inside an Arabic run ({mixed_runs})")
        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                etree.fromstring(z.read(n))  # raises on broken XML
        rels = z.read("ppt/slides/_rels/slide5.xml.rels").decode()
        check("notesSlide5.xml" in rels and "../media/" in rels, f"{label}: a figure slide has its notes and picture")
        notes = "".join(z.read(f"ppt/notesSlides/notesSlide{i}.xml").decode("utf-8")
                        for i in range(1, n_expected + 1))
        return notes
    return ""


notes = pptx_checks(res["pptx"], N, True, "Arabic")
check("شكل (3-1): توزيع قطع الأراضي" in notes and "المصدر" in notes,
      "speaker notes carry the figure caption and its source")
try:
    import pptx  # noqa: F401 - only when python-pptx happens to be installed
    prs = pptx.Presentation(res["pptx"])
    check(len(prs.slides) == N, "python-pptx reads the file")
    check("شكل (3-1)" in prs.slides[4].notes_slide.notes_text_frame.text, "python-pptx finds the notes")
except ImportError:
    print("SKIP python-pptx is not installed (the file is checked as XML above)")

# HTML
page_path = res["html"]
page = open(page_path, encoding="utf-8").read()
check(page.count('<section class="slide"') == N + 1, "the HTML page has every slide and the map slide")
check('dir="rtl"' in page[:200] and 'lang="ar"' in page[:200], "the HTML page is right-to-left Arabic")
m = re.search(r'<script type="application/json" id="webmap-src">(.*?)</script>', page, re.S)
outside = page.replace(m.group(0), "") if m else page
ext_refs = re.findall(r'(?:src|href)\s*=\s*["\']?(https?:|//)', outside)
check(not ext_refs and "<link" not in outside and "@import" not in outside,
      "the HTML page loads nothing from the internet (only the embedded map's tiles)")
wm = json.loads(m.group(1)) if m else ""
check("leaflet" in wm.lower() and "Parcels" in wm, "the interactive web map is embedded")
check(os.path.exists(os.path.join(out_ar, "webmap.html")), "webmap.html is next to the page")
check(page.count("data:image/") >= 4, "the pictures are inside the HTML page")
check("شكل (3-2): استعمالات الأراضي في منطقة الدراسة" in page, "the figure caption is on its slide")
scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
if os.path.exists(node):
    js = os.path.join(work, "deck.js")
    with open(js, "w", encoding="utf-8") as f:
        f.write(scripts[-1])
    p = subprocess.run([node, "--check", js], capture_output=True, text=True)
    check(p.returncode == 0, "the page's script passes node --check " + p.stderr[:300])
else:
    print("SKIP node not found")

# PDF
pdf = open(res["pdf"], "rb").read()
pages = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", pdf))
check(pdf.startswith(b"%PDF") and pages == N, f"the PDF has {pages} pages (expected {N})")

# ---- with a methodology module, English, dark, a QR code ------------------------------------------------------
meth = types.ModuleType("biruni.thesis.methodology")


def methodology_text(lang):
    if lang == "ar":
        return "المنهجية", ["جُمعت البيانات من الأمانة. ثم نُظّفت.", "رُقمنت الخرائط الورقية بدقة عالية.",
                            "حُللت استعمالات الأراضي بالتصنيف الموجه.", "قورنت النتائج بالمسح الميداني."]
    return "Methodology", ["Data were collected from the municipality. Then cleaned.",
                           "Paper maps were digitised accurately.", "Land use was analysed by classification.",
                           "Results were compared with the field survey."]


def workflow_image(path, lang):
    im = QImage(1600, 600, QImage.Format.Format_ARGB32)
    im.fill(0xFF3A7BD5)
    im.save(path)
    return path


meth.methodology_text = methodology_text
meth.workflow_image = workflow_image
sys.modules["biruni.thesis.methodology"] = meth
import biruni.thesis as thesis_pkg  # noqa: E402
thesis_pkg.methodology = meth

opts_en = dict(OPTS, lang="en", theme="dark", accent="#1F4E79", url="https://example.org/defense/")
slides_en = defense.build_slides(project, opts_en)
kinds = [s["kind"] for s in slides_en]
check("workflow" in kinds and kinds.index("workflow") == kinds.index("data_method") + 1,
      "with the methodology module: a workflow slide after data & methodology")
dm = slides_en[kinds.index("data_method")]
check(dm["method"] and len(dm["method"][1]) == 4 and dm["method"][1][0][1] ==
      "Data were collected from the municipality.", "methodology summarised into 3-5 bullets")
out_en = os.path.join(work, "en")
res_en = defense.export_presentation(out_en, slides_en, opts_en, project)
check(res_en["slides"] == N + 1 and os.path.exists(os.path.join(out_en, "media", "workflow.png")),
      "English: the workflow picture is a slide")
check(os.path.exists(os.path.join(out_en, "media", "qr.png")), "a QR code for the published address")
notes_en = pptx_checks(res_en["pptx"], N + 1, False, "English")
check("Figure 3.1: Distribution of the parcels" in notes_en, "English notes")
page_en = open(res_en["html"], encoding="utf-8").read()
check('dir="ltr"' in page_en[:200] and 'class="dark"' in page_en, "English page: left-to-right, dark theme")

# both languages, light; only the HTML (no python-pptx needed, no PDF)
opts_both = dict(OPTS, lang="both")
res_b = defense.export_presentation(os.path.join(work, "both"), None, opts_both, project, outputs=("html",))
page_b = open(res_b["html"], encoding="utf-8").read()
check("Land use in the study area" in page_b and "استعمالات الأراضي في منطقة الدراسة" in page_b
      and not res_b.get("pptx") and not res_b.get("pdf"), "both languages on the slides; only what was asked")

# palette contrast: a dark accent stays readable on the dark theme
pal = defense.palette("dark", "#14365A")
check(defense._contrast(pal["accent_text"], pal["bg"]) >= 4.5, "accent text keeps 4.5:1 contrast on dark")

# ---- the dialog (built, never shown) ---------------------------------------------------------------------------
sys.modules["biruni.thesis.methodology"] = None
del thesis_pkg.methodology
from biruni.thesis import defense_dialog  # noqa: E402


class _Iface:
    def mainWindow(self):
        return None


class _Plugin:
    iface = _Iface()


dlg = defense_dialog.DefenseDialog(_Plugin(), project)
check(dlg.list.count() == 11, f"the dialog lists the slides ({dlg.list.count()})")
dlg.list.item(4).setCheckState(defense_dialog.Qt.CheckState.Unchecked)
dlg.list.setCurrentRow(6)
dlg._move(-1)
chosen = dlg.chosen_slides()
check(len(chosen) == 10 and chosen[4]["kind"] == "figure" and chosen[4]["layout"] == "Plain layout without a figure key",
      "untick and reorder in the dialog")
dlg.lang.setCurrentIndex(dlg.lang.findData("en"))
check(dlg.list.item(0).text() and "Figure" in dlg.list.item(5).text(), "labels follow the language")
dlg._preview(5)
check(dlg.preview.pixmap() is not None and not dlg.preview.pixmap().isNull(), "a preview of the slide")
dlg.title.setText("T")
dlg._save()
again = defense_dialog.DefenseDialog(_Plugin(), project)
check(again.title.text() == "T" and again.lang.currentData() == "en", "the inputs are kept in the project")

# pictures of the slides, to look at them
if KEEP:
    defense.export_presentation(os.path.join(work, "png"), None, OPTS, project, outputs=("png",))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
if not KEEP:
    shutil.rmtree(work, ignore_errors=True)
app.exitQgis()
sys.exit(1 if failures else 0)
