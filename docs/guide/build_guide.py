"""Builds the Arabic user guide: guide-ar.src.html -> guide-ar.html -> Biruni-GIS-Guide-ar.pdf.

Every Latin run in the text (ArcGIS, Excel / CSV, ...) is wrapped in
<bdi dir="ltr"> so that lists of Latin words and the Arabic "و" glued to a
Latin word keep their order in the right-to-left text. The PDF is printed by
headless Chrome (Cairo font from Google Fonts, so the first build needs the
internet).

    python docs/guide/build_guide.py [path to chrome.exe]
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP = {"style", "title", "code", "kbd", "bdi", "script", "svg"}
WAW = '<span style="font-family:\'Segoe UI\',Tahoma,sans-serif">و</span>'
LATIN = re.compile(r"\.?[A-Za-z][A-Za-z0-9 .,:;/+&()\-_'’°×%·]*[A-Za-z0-9)]")


def _resolve(path):
    if os.path.isabs(path) or os.path.dirname(path):
        return os.path.abspath(path)
    return os.path.join(HERE, path)


def _out_html(src_path):
    if src_path.endswith(".src.html"):
        return src_path[:-9] + ".html"
    return os.path.splitext(src_path)[0] + ".html"


def wrap(text):
    def one(m):
        run = m.group(0)
        tail = ""
        if run.endswith(")") and run.count("(") < run.count(")"):
            run, tail = run[:-1], ")"
        return f'<bdi dir="ltr">{run}</bdi>{tail}'
    # "وGDAL" reads like "GDALو" next to Latin words: keep the "و" apart, and draw
    # it in Segoe UI - Cairo's lone "و" looks like a Latin "g" between Latin words
    text = re.sub(r"(^|(?<=[\s(]))و(?=[A-Za-z])", "\ue000 ", text)
    text = text.replace("\u200e", "")  # the isolates do the job of the old LRMs
    return LATIN.sub(one, text).replace("\ue000", WAW)


def build_html(src_path, out_path):
    src = open(src_path, encoding="utf-8").read()
    head, body = src.split("<body>", 1)
    parts = re.split(r"(<[^>]+>)", body)
    stack, out = [], []
    for part in parts:
        if part.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", part)
            if m and not part.endswith("/>"):
                name = m.group(2).lower()
                if m.group(1):
                    if stack and stack[-1] == name:
                        stack.pop()
                elif name in SKIP:
                    stack.append(name)
            out.append(part)
        else:
            out.append(part if stack else wrap(part))
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + "<body>" + "".join(out))


def find_chrome():
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    raise SystemExit("Chrome or Edge not found - pass its path")


def build_pdf(out_path, pdf_path, chrome):
    url = "file:///" + out_path.replace("\\", "/")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--virtual-time-budget=60000", "--run-all-compositor-stages-before-draw",
                    f"--print-to-pdf={pdf_path}", url], check=True, capture_output=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build Biruni Arabic guide HTML and PDF.")
    ap.add_argument("chrome", nargs="?", help="Optional path to chrome.exe or msedge.exe")
    ap.add_argument("--src", default="guide-ar.src.html", help="Source HTML path")
    ap.add_argument("--pdf", default="Biruni-GIS-Guide-ar.pdf", help="Output PDF path")
    ap.add_argument("--html-only", action="store_true", help="Only build HTML, skip PDF")
    args = ap.parse_args(argv)
    src_path = _resolve(args.src)
    out_path = _out_html(src_path)
    pdf_path = _resolve(args.pdf)
    build_html(src_path, out_path)
    if args.html_only:
        print(out_path)
        return
    build_pdf(out_path, pdf_path, args.chrome or find_chrome())
    print(pdf_path)


if __name__ == "__main__":
    main()
