"""Acceptance checker for Biruni Link & Team Arabic training book.

Runs with system Python:
    python scripts\\check_linkteam_guide.py [--part <id>]
"""
import argparse
import html as html_lib
import json
import os
import re
import subprocess
import sys

from PIL import Image, ImageStat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE_DIR = os.path.join(ROOT, "docs", "guide")
SRC_HTML = os.path.join(GUIDE_DIR, "link-team-guide-ar.src.html")
OUT_HTML = os.path.join(GUIDE_DIR, "link-team-guide-ar.html")
OUT_PDF = os.path.join(GUIDE_DIR, "Biruni-Link-Team-Guide-ar.pdf")
BUILD_SCRIPT = os.path.join(GUIDE_DIR, "build_guide.py")
IMG_DIR = os.path.join(GUIDE_DIR, "images", "linkteam")
AR_JSON = os.path.join(ROOT, "app", "plugins", "biruni", "i18n", "ar.json")
SCRATCH_DIR = r"D:\Claude Code - Online\biruni\_scratch\linkteam_book"
FACTS_MD = os.path.join(SCRATCH_DIR, "FACTS.md")
UNCAPTURED_MD = os.path.join(SCRATCH_DIR, "UNCAPTURED.md")
PAGES_DIR = os.path.join(SCRATCH_DIR, "pages")

EXPECTED_SHOTS = {
    "front": [],
    "hub": [
        "hub-off", "hub-on", "hub-tools-mixed", "hub-busy-port",
        "door-changed", "door-unsigned", "ph-hub", "ph-hub-ask",
    ],
    "photos": [
        "fp-modes", "fp-local-qr", "fp-bridge-qr", "fp-own-tunnel", "fp-tunnel-help",
        "fp-qr-mode", "fp-surveyor-qr", "fp-form", "fp-team-list", "fp-team-menu",
        "fp-block", "fp-guide", "fp-indoor", "fp-room-codes", "fp-preview-list", "fp-daily-report",
        "ph-photo-start", "ph-name", "ph-shoot", "ph-kinds-form", "ph-avg", "ph-guide",
        "ph-room-scan", "ph-room-noted", "ph-relay-mode", "ph-via", "ph-incognito",
        "ph-blocked", "ph-hand-give", "ph-hand-take", "ph-pcoff",
    ],
    "drive": [
        "fd-off", "fd-confirm", "fd-on", "fd-key-nudge", "fd-key-password",
        "fd-key-backed", "fd-list-drive", "fd-status-checked", "fd-folder-missing",
        "pd-unlinked", "pd-pick", "pd-idle", "pd-up", "pd-own", "pd-ro",
        "pd-gone", "pd-full", "pd-auth", "pd-inapp", "pd-blocked", "pd-free",
    ],
    "gps": [
        "gps-bridge", "gps-serial", "gps-qr", "gps-receiving", "gps-catching-up",
        "gps-recording", "gps-saved", "gps-no-data",
        "pg-start", "pg-sending", "pg-kept", "pg-catch", "pg-via",
    ],
    "channels": [
        "org-empty", "org-token", "org-accounts", "org-deployed", "org-failed",
        "ch-relays-connected", "ch-tunnel-adding",
    ],
    "show": [
        "show-map-off", "show-map-on", "show-dash-share", "show-dash-card",
        "show-dash-links", "show-liveqr", "show-field-send", "show-field-back",
        "ps-map", "ps-dash", "ps-paper", "ps-seal-ok", "ps-seal-changed",
    ],
    "room": [
        "tm-start", "tm-open-room", "tm-join-net", "tm-dock-host", "tm-dock-member",
        "tm-qr", "tm-let-in", "tm-control-panel", "tm-pointer-map", "tm-host-lost",
        "tm-network-test", "tm-left-out-layers",
    ],
    "rules": [
        "tr-rights-menu", "tr-area-menu", "tr-area-map", "tr-locks", "tr-merge-rules",
        "tr-number-ranges", "tr-delete-guard", "tr-conflicts", "tr-activity", "tr-history",
        "tr-timemachine", "tr-take-back", "tr-new-code", "tr-refused-data",
    ],
    "teamdrive": [
        "td-wizard-create", "td-wizard-nodrive", "td-wizard-steps", "td-join-drive",
        "td-join-waiting", "td-status-synced", "td-behind-table", "td-warn-streamed",
        "td-folder-gone", "td-not-running", "td-host-right", "td-make-host-confirm",
        "td-peer-apply", "td-clock-ahead", "td-enter-new-code",
    ],
    "offline": [
        "off-pigeon", "off-pigeon-result", "off-light-send", "off-light-receive",
        "off-vault", "off-compute-share",
    ],
    "appendices": [],
}

STRUCTURED_PARTS = {"hub", "photos", "drive", "gps", "channels", "show", "room", "rules", "teamdrive", "offline"}

UNITS = (
    r"ثانية|ثوانٍ|ثوان|دقيقة|دقائق|ساعة|ساعات|يوم|أيام|يومان|شهر|أشهر|سنة|سنوات|"
    r"ميجابايت|كيلوبايت|جيجابايت|بايت|متر|أمتار|كم|كيلومتر|مللي\s+ثانية|مللي|"
    r"رسالة|رسائل|درجة|نقطة|معلم|معالم|صورة|صور|باكيت|حزمة|"
    r"MB|KB|GB|MiB|GiB|ms|Hz|px|dpi|mm|cm|m\b|s\b|h\b"
)
NUM_UNIT_RE = re.compile(rf"(?<![A-Za-z0-9_.\-])(\d+(?:[.,]\d+)?)\s*(?:{UNITS})")


def _strip_tags(s):
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s))


def _load_ar_values():
    with open(AR_JSON, encoding="utf-8") as f:
        data = json.load(f)
    exact = set()
    patterns = []
    for v in data.values():
        exact.add(v)
        norm = re.sub(r"\s+", " ", v).strip()
        exact.add(norm)
        for cand in {v, norm}:
            if "{" in cand and "}" in cand:
                parts = re.split(r"(\{[^}]+\})", cand)
                rx = "^" + "".join(".+?" if p.startswith("{") and p.endswith("}") else re.escape(p) for p in parts) + "$"
                patterns.append(re.compile(rx))
    keys_12 = [k for k, v in data.items() if len(k) >= 12 and v != k and re.search(r"[A-Za-z]", k)]
    return exact, patterns, keys_12


def _load_facts_numbers():
    nums = set()
    if not os.path.exists(FACTS_MD):
        return nums
    with open(FACTS_MD, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "|" not in line or line.startswith("#") or line.startswith("value "):
                continue
            val = line.split("|", 1)[0].strip()
            for m in re.finditer(r"\d+(?:[.,]\d+)?", val):
                nums.add(m.group(0).replace(",", ""))
    return nums


def _load_uncaptured():
    items = set()
    if not os.path.exists(UNCAPTURED_MD):
        return items
    with open(UNCAPTURED_MD, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*[-*]\s*`?([a-z0-9-]+)`?\s*[:\-–—|]", line)
            if m:
                items.add(m.group(1))
    return items


def _extract_part(src_text, part_id):
    pat = re.compile(
        rf'<section\b[^>]*data-part="{re.escape(part_id)}"[^>]*>.*?</section>(?=\s*(?:<!--|</body|<section\b))',
        re.S,
    )
    # Use balanced <section> matching so nested <section class="cont"> inside a part works
    start_m = re.search(rf'<section\b[^>]*data-part="{re.escape(part_id)}"[^>]*>', src_text)
    if not start_m:
        return None
    pos = start_m.start()
    depth = 0
    for m in re.finditer(r"<(/?)section\b[^>]*>", src_text[pos:]):
        if m.group(1):
            depth -= 1
            if depth == 0:
                return src_text[pos:pos + m.end()]
        else:
            depth += 1
    return None


def _check_image_file(path, errors, ctx):
    if not os.path.exists(path):
        errors.append(f"[{ctx}] missing image: {path}")
        return
    sz = os.path.getsize(path)
    if sz <= 3072:
        errors.append(f"[{ctx}] image <= 3 KB ({sz} B): {path}")
        return
    try:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            st = ImageStat.Stat(rgb)
            if sum(st.stddev) < 1.0:
                errors.append(f"[{ctx}] image appears flat single colour: {path}")
    except Exception as exc:
        errors.append(f"[{ctx}] failed to read image {path}: {exc}")


def check_part_content(src_text, built_text, part_id, ar_exact, ar_patterns, keys_12, facts_nums, uncaptured,
                       errors, check_unused_pngs=False):
    ctx = part_id or "full"
    chunk = _extract_part(src_text, part_id) if part_id else src_text
    if chunk is None:
        errors.append(f"[{ctx}] section data-part='{part_id}' not found in {SRC_HTML}")
        return

    # 2. Images
    imgs = re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', chunk)
    used_linkteam = set()
    for rel in imgs:
        full = os.path.normpath(os.path.join(GUIDE_DIR, rel))
        _check_image_file(full, errors, ctx)
        if os.path.dirname(full).lower() == os.path.normpath(IMG_DIR).lower():
            used_linkteam.add(os.path.basename(full))

    if part_id:
        for shot in EXPECTED_SHOTS.get(part_id, []):
            fname = f"{shot}.png"
            fpath = os.path.join(IMG_DIR, fname)
            if not os.path.exists(fpath) and shot not in uncaptured:
                errors.append(f"[{ctx}] expected shot missing and not in UNCAPTURED.md: {fname}")
            if os.path.exists(fpath) and fname not in used_linkteam:
                errors.append(f"[{ctx}] shot exists but not used in section: {fname}")
    elif check_unused_pngs and os.path.isdir(IMG_DIR):
        all_pngs = {f for f in os.listdir(IMG_DIR) if f.lower().endswith(".png")}
        unused = sorted(all_pngs - used_linkteam)
        if unused:
            errors.append(f"[full] unused PNGs in images/linkteam: {unused}")
        for pid, shots in EXPECTED_SHOTS.items():
            for shot in shots:
                fpath = os.path.join(IMG_DIR, f"{shot}.png")
                if not os.path.exists(fpath) and shot not in uncaptured:
                    errors.append(f"[{pid}] expected shot missing and not in UNCAPTURED.md: {shot}.png")

    # 3. Labels (.ui)
    for m in re.finditer(r'<span\s+class="ui"[^>]*>(.*?)</span>', chunk, re.S):
        txt = _strip_tags(m.group(1)).strip()
        if txt in ar_exact or any(p.match(txt) for p in ar_patterns):
            continue
        errors.append(f"[{ctx}] .ui label not found in ar.json: {txt!r}")

    # 4. Structure
    parts_to_check = [part_id] if part_id else list(EXPECTED_SHOTS.keys())
    for pid in parts_to_check:
        sub = _extract_part(src_text, pid)
        if sub is None:
            errors.append(f"[{pid}] missing <section data-part='{pid}'>")
            continue
        if pid in STRUCTURED_PARTS:
            if not re.search(r'<table\b[^>]*class="[^"]*\bwho\b', sub):
                errors.append(f"[{pid}] missing table.who")
            if not re.search(r'<figure\b[^>]*class="[^"]*\bconcept\b[^>]*>.*?<svg\b', sub, re.S):
                errors.append(f"[{pid}] missing .concept SVG")
            if not re.search(r'class="[^"]*\bsees\b', sub):
                errors.append(f"[{pid}] missing .sees box")
            fix_m = re.search(r'<table\b[^>]*class="[^"]*\bfix\b[^>]*>(.*?)</table>', sub, re.S)
            if not fix_m:
                errors.append(f"[{pid}] missing table.fix")
            else:
                data_rows = [r for r in re.findall(r"<tr\b[^>]*>(.*?)</tr>", fix_m.group(1), re.S) if "<th" not in r]
                if len(data_rows) < 6:
                    errors.append(f"[{pid}] table.fix has {len(data_rows)} data rows (need >= 6)")
            if not re.search(r'class="[^"]*\bscenario\b', sub):
                errors.append(f"[{pid}] missing .scenario box")
            if not re.search(r"<h2\b", sub) and "<!-- PENDING -->" not in sub:
                errors.append(f"[{pid}] missing <h2> chapters or <!-- PENDING -->")

    # 5. Words and numbers
    prose = re.sub(r"<(style|script|svg)\b.*?</\1>", " ", chunk, flags=re.S | re.I)
    prose_no_code = re.sub(r"<(code|kbd)\b.*?</\1>", " ", prose, flags=re.S | re.I)
    visible = _strip_tags(prose)
    visible_no_code = _strip_tags(prose_no_code)
    for bad_rx, label in (
        (r"\bqgis\b", "qgis"),
        (r"\bECW\b", "ECW"),
        (r"\bAI\b", "AI"),
        (r"ذكاء اصطناعي", "ذكاء اصطناعي"),
        (r"المساعد الذكي", "المساعد الذكي"),
        (r"C:\\Users", r"C:\Users"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "@ address"),
    ):
        m = re.search(bad_rx, visible, re.I if label == "qgis" else 0)
        if m:
            errors.append(f"[{ctx}] forbidden text ({label}): {m.group(0)!r}")

    visible_no_gui = _strip_tags(re.sub(r'<span\s+class="gui"[^>]*>.*?</span>', " ", prose_no_code, flags=re.S))
    for k in keys_12:
        if k in visible_no_gui:
            errors.append(f"[{ctx}] untranslated ar.json key (>=12 chars): {k!r}")

    # Numbers with units (exclude .ui exact spans so UI strings like "20 ث" don't fail if already from UI, or require them in FACTS too - actually requiring all numbers with units in FACTS is safer, let's check all visible_no_code)
    for m in NUM_UNIT_RE.finditer(visible_no_code):
        num = m.group(1).replace(",", "")
        if num not in facts_nums:
            errors.append(f"[{ctx}] number with unit not in FACTS.md: {m.group(0)!r}")

    # 6. SVG checks in built HTML
    built_chunk = _extract_part(built_text, part_id) if part_id else built_text
    if built_chunk:
        for svg_m in re.finditer(r"<svg\b.*?</svg>", built_chunk, re.S):
            svg = svg_m.group(0)
            if "<bdi" in svg.lower():
                errors.append(f"[{ctx}] <bdi inside <svg> in built HTML")
            svg_txt = _strip_tags(svg)
            bad_waw = re.search(r"و[A-Za-z]", svg_txt)
            if bad_waw:
                errors.append(f"[{ctx}] 'و' directly before Latin letter in SVG: {bad_waw.group(0)!r}")


def check_pdf_and_render(src_text, errors):
    import fitz

    if not os.path.exists(OUT_PDF):
        errors.append(f"[pdf] missing {OUT_PDF}")
        return
    sz = os.path.getsize(OUT_PDF)
    if sz >= 60 * 1024 * 1024:
        errors.append(f"[pdf] size {sz} >= 60 MB")
    doc = fitz.open(OUT_PDF)
    n = len(doc)
    if not (90 <= n <= 170):
        errors.append(f"[pdf] page count {n} outside 90-170")
    fonts = set()
    for i, page in enumerate(doc):
        r = page.rect
        if abs(r.width - 595.28) > 4 or abs(r.height - 841.89) > 4:
            errors.append(f"[pdf] page {i + 1} not A4: {r.width:.1f}x{r.height:.1f}")
            break
        for f in page.get_fonts():
            fonts.add(f[3])
    font_str = " ".join(fonts)
    if "Cairo" not in font_str:
        errors.append(f"[pdf] Cairo font missing; found: {sorted(fonts)}")
    if any("Times" in f or "Arial" in f for f in fonts):
        errors.append(f"[pdf] forbidden font (Times/Arial) found: {sorted(fonts)}")
    p1_txt = doc[0].get_text("text").strip()
    if re.search(r"(?:^|\n)\s*1\s*(?:\n|$)", p1_txt):
        errors.append("[pdf] page 1 appears to contain page number '1'")

    # 8. Render first page of every part + last page at 80 dpi
    os.makedirs(PAGES_DIR, exist_ok=True)
    part_titles = {}
    for pid in EXPECTED_SHOTS:
        sub = _extract_part(src_text, pid)
        if sub:
            h = re.search(r"<h[12]\b[^>]*>(.*?)</h[12]>", sub, re.S)
            if h:
                part_titles[pid] = _strip_tags(h.group(1)).strip()[:30]

    rendered = {}
    rendered["front"] = 0
    for i, page in enumerate(doc):
        txt = page.get_text("text")
        for pid, title in part_titles.items():
            if pid not in rendered and title and title in txt:
                rendered[pid] = i
    rendered["last"] = n - 1
    for label, pno in rendered.items():
        pix = doc[pno].get_pixmap(dpi=80)
        out = os.path.join(PAGES_DIR, f"{label}_p{pno + 1}.png")
        pix.save(out)
        im = Image.open(out).convert("RGB")
        st = ImageStat.Stat(im)
        if sum(st.stddev) < 1.0:
            errors.append(f"[pdf] rendered page {pno + 1} ({label}) appears blank")
    doc.close()


def check_regression(errors):
    res = subprocess.run([sys.executable, BUILD_SCRIPT, "--html-only"], capture_output=True, text=True)
    if res.returncode != 0:
        errors.append(f"[regression] build_guide --html-only failed: {res.stderr}")
        return
    diff = subprocess.run(["git", "-C", ROOT, "diff", "--", "docs/guide/guide-ar.html"],
                          capture_output=True, text=True)
    if diff.stdout.strip():
        errors.append("[regression] git diff docs/guide/guide-ar.html is not empty")


def main():
    ap = argparse.ArgumentParser(description="Check Biruni Link & Team Arabic guide.")
    ap.add_argument("--part", help="Check only this part id (runs checks 1-6)")
    args = ap.parse_args()

    errors = []
    # 1. Build
    res = subprocess.run(
        [sys.executable, BUILD_SCRIPT, "--src", "link-team-guide-ar.src.html",
         "--pdf", "Biruni-Link-Team-Guide-ar.pdf"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        errors.append(f"[build] build_guide.py failed: {res.stderr or res.stdout}")

    if not os.path.exists(SRC_HTML):
        errors.append(f"[build] missing {SRC_HTML}")
        for e in errors:
            print("FAIL:", e)
        raise SystemExit(1)

    src_text = open(SRC_HTML, encoding="utf-8").read()
    built_text = open(OUT_HTML, encoding="utf-8").read() if os.path.exists(OUT_HTML) else ""
    ar_exact, ar_patterns, keys_12 = _load_ar_values()
    facts_nums = _load_facts_numbers()
    uncaptured = _load_uncaptured()

    check_part_content(src_text, built_text, args.part, ar_exact, ar_patterns, keys_12,
                       facts_nums, uncaptured, errors, check_unused_pngs=(args.part is None))

    if args.part is None:
        check_pdf_and_render(src_text, errors)
        check_regression(errors)

    if errors:
        for e in errors:
            print("FAIL:", e)
        raise SystemExit(1)
    print(f"PASS ({args.part or 'full'})")


if __name__ == "__main__":
    main()
