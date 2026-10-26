"""Builds the Arabic user guide: guide-ar.src.html -> guide-ar.html -> Biruni-GIS-Guide-ar.pdf.

Every Latin run in the text (ArcGIS, Excel / CSV, ...) is wrapped in
<bdi dir="ltr"> so that lists of Latin words and the Arabic "و" glued to a
Latin word keep their order in the right-to-left text. The PDF is printed by
headless Chrome (Cairo font from Google Fonts, so the first build needs the
internet).

    python docs/guide/build_guide.py [path to chrome.exe]
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "guide-ar.src.html")
OUT = os.path.join(HERE, "guide-ar.html")
PDF = os.path.join(HERE, "Biruni-GIS-Guide-ar.pdf")
SKIP = {"style", "title", "code", "kbd", "bdi", "script"}
WAW = '<span style="font-family:\'Segoe UI\',Tahoma,sans-serif">و</span>'
LATIN = re.compile(r"\.?[A-Za-z][A-Za-z0-9 .,:;/+&()\-_'’°×%·]*[A-Za-z0-9)]")


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


def build_html():
    src = open(SRC, encoding="utf-8").read()
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
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + "<body>" + "".join(out))


def find_chrome():
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            return p
    raise SystemExit("Chrome or Edge not found - pass its path")


def build_pdf(chrome):
    url = "file:///" + OUT.replace("\\", "/")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw",
                    f"--print-to-pdf={PDF}", url], check=True, capture_output=True)


if __name__ == "__main__":
    build_html()
    build_pdf(sys.argv[1] if len(sys.argv) > 1 else find_chrome())
    print(PDF)
