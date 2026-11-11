"""Reading numbers on scanned plans with the OCR engine built into Windows
(Windows.Media.Ocr, the one behind the Snipping Tool's "copy text").

No extra software: Windows PowerShell calls the WinRT engine for every image
tile and returns the words with their boxes as JSON. Arabic needs the Arabic
OCR language (it comes with the Arabic language pack). Windows OCR gives no
confidence values, so callers judge the words themselves."""
import json
import math
import os
import re
import subprocess
import tempfile

import numpy as np

_SCRIPT = r"""param([string]$List, [string]$Out, [string]$Langs = 'ar-SA,en-US')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$engines = [ordered]@{}
foreach ($tag in $Langs.Split(',')) {
    if (-not $tag) { continue }
    $lang = New-Object Windows.Globalization.Language $tag
    if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
        $engines[$tag] = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    }
}
$words = New-Object System.Collections.ArrayList
$i = 0
foreach ($path in [IO.File]::ReadAllLines($List, [Text.Encoding]::UTF8)) {
    if (-not $path) { continue }
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    foreach ($tag in $engines.Keys) {
        $res = Await ($engines[$tag].RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $n = 0
        foreach ($line in $res.Lines) {
            $n++
            foreach ($w in $line.Words) {
                $r = $w.BoundingRect
                [void]$words.Add(@{ i = $i; l = $tag; n = $n; t = $w.Text; x = $r.X; y = $r.Y; w = $r.Width; h = $r.Height })
            }
        }
    }
    $stream.Dispose()
    $i++
}
$json = ConvertTo-Json -InputObject @{ langs = @($engines.Keys); words = $words.ToArray() } -Depth 4 -Compress
[IO.File]::WriteAllText($Out, $json, (New-Object Text.UTF8Encoding $false))
"""

MAX_DIMENSION = 9000  # Windows.Media.Ocr refuses images larger than 10000 px
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_cache = {}


class OcrUnavailable(Exception):
    pass


def normalise_digits(text):
    return (text or "").translate(_DIGITS)


def _run(paths, langs, timeout):
    if os.name != "nt":
        raise OcrUnavailable("Windows OCR is only available on Windows")
    work = tempfile.mkdtemp(prefix="biruni_ocr_")
    script = os.path.join(work, "ocr.ps1")
    listing = os.path.join(work, "tiles.txt")
    out = os.path.join(work, "words.json")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(_SCRIPT)
    with open(listing, "w", encoding="utf-8") as f:
        f.write("\n".join(paths))
    # the full path: the engine's environment does not keep the Windows folders on PATH
    shell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                         "powershell.exe")
    cmd = [shell if os.path.exists(shell) else "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
           "-List", listing, "-Out", out, "-Langs", ",".join(langs)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OcrUnavailable(str(e)) from e
    if proc.returncode != 0 or not os.path.exists(out):
        raise OcrUnavailable(proc.stderr.decode("utf-8", "replace").strip()[-400:] or "OCR failed")
    with open(out, encoding="utf-8") as f:
        data = json.load(f)
    for p in (script, listing, out):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(work)
    except OSError:
        pass
    return data


def available_languages(wanted=("ar-SA", "en-US")):
    """The wanted OCR languages that this Windows can read ([] when none or no Windows OCR)."""
    key = tuple(wanted)
    if key not in _cache:
        try:
            _cache[key] = list(_run([], wanted, 60).get("langs") or [])
        except OcrUnavailable:
            _cache[key] = []
    return _cache[key]


def read_words(tiles, langs, timeout=None):
    """OCR image files. tiles: [(path, x0, y0, scale)] - each word box is mapped
    back to page pixels with x0 + x / scale. Returns dicts with the page box,
    the text, the language and the tile/line it came from."""
    if not tiles:
        return []
    data = _run([t[0] for t in tiles], langs, timeout or 60 + 15 * len(tiles))
    words = []
    for w in data.get("words") or []:
        path, x0, y0, s = tiles[int(w["i"])]
        words.append({"text": w["t"], "lang": w["l"], "tile": int(w["i"]), "line": int(w["n"]),
                      "x": x0 + float(w["x"]) / s, "y": y0 + float(w["y"]) / s,
                      "w": float(w["w"]) / s, "h": float(w["h"]) / s})
    return words


NUMBER = re.compile(r"^\d+([./\-]\d+)?[A-Za-zء-ي]?$")
INTEGER = re.compile(r"^\d+[A-Za-zء-ي]?$")


def numbers(words):
    """Number-like words only, with neighbouring digit groups of one OCR line
    joined (Arabic numbers often come back split: "٤ ٨" -> "48")."""
    out = []
    groups = {}
    for w in words:
        t = normalise_digits(w["text"]).strip("()[]،,:;")
        if not any(c.isdigit() for c in t):
            continue
        groups.setdefault((w["tile"], w["lang"], w["line"]), []).append(dict(w, norm=t))
    for ws in groups.values():
        ws.sort(key=lambda w: w["x"])
        cur = None
        for w in ws:
            if (cur is not None and cur["norm"].isdigit() and w["norm"].isdigit()
                    and w["x"] - (cur["x"] + cur["w"]) < 0.6 * max(cur["h"], w["h"])
                    and abs((w["y"] + w["h"] / 2) - (cur["y"] + cur["h"] / 2)) < 0.5 * max(cur["h"], w["h"])):
                right = max(cur["x"] + cur["w"], w["x"] + w["w"])
                bottom = max(cur["y"] + cur["h"], w["y"] + w["h"])
                cur["y"] = min(cur["y"], w["y"])
                cur["norm"] += w["norm"]
                cur["text"] += w["text"]
                cur["w"], cur["h"] = right - cur["x"], bottom - cur["y"]
                continue
            if cur is not None:
                out.append(cur)
            cur = dict(w)
        if cur is not None:
            out.append(cur)
    for w in out:
        w["is_number"] = bool(NUMBER.match(w["norm"]))
        w["is_integer"] = bool(INTEGER.match(w["norm"]))
        w["cx"], w["cy"] = w["x"] + w["w"] / 2, w["y"] + w["h"] / 2
    return [w for w in out if w["is_number"]]


def deduplicate(nums):
    """The same number read by two languages (or two overlapping tiles) once:
    the reading with more digits wins; disagreeing readings are marked."""
    nums = sorted(nums, key=lambda w: (-len(w["norm"]), -w["h"]))
    kept = []
    for w in nums:
        twin = None
        for k in kept:
            if abs(k["cx"] - w["cx"]) < 0.6 * max(k["w"], w["w"]) and abs(k["cy"] - w["cy"]) < 0.6 * max(k["h"], w["h"]):
                twin = k
                break
        if twin is None:
            kept.append(dict(w, readings={w["norm"]}))
        else:
            twin["readings"].add(w["norm"])
    for k in kept:
        k["disputed"] = len(k["readings"]) > 1 and not all(k["norm"].endswith(r) or r in k["norm"]
                                                            for r in k["readings"])
    return kept


# ---- printed digits read by shape ---------------------------------------------------------------------------
# Windows OCR hardly reads isolated Arabic-Indic numbers (and sometimes returns a wrong one), so parcel numbers
# are also read glyph by glyph against digits rendered in the fonts of this computer. Each digit of a printed
# number is one blob of ink; a number is a row of such blobs.

_FONTS = ["arial", "tahoma", "times", "segoeui", "trado", "simpo", "majalla", "arabtype", "andlso", "calibri",
          "cour", "verdana", "micross", "aldhabi", "sakkalmajalla"]
_ARABIC = "٠١٢٣٤٥٦٧٨٩"
GRID = 20


def glyph_vector(mask):
    """A binary glyph as a 20×20 picture (aspect ratio kept), blurred a little,
    zero-mean and unit length, so a dot product is a correlation."""
    from scipy import ndimage

    h, w = mask.shape
    s = (GRID - 2) / max(h, w)
    zoom = ndimage.zoom(mask.astype(np.float32), (max(s * h, 1) / h, max(s * w, 1) / w), order=1)
    canvas = np.zeros((GRID, GRID), np.float32)
    zh, zw = min(zoom.shape[0], GRID), min(zoom.shape[1], GRID)
    y0, x0 = (GRID - zh) // 2, (GRID - zw) // 2
    canvas[y0:y0 + zh, x0:x0 + zw] = zoom[:zh, :zw]
    v = ndimage.gaussian_filter(canvas, 0.8).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n else v


class DigitTemplates:
    """Latin and Arabic-Indic digits rendered in the fonts found on this computer."""

    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        from PIL import Image, ImageDraw, ImageFont

        folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        vecs, labels = [], []
        for name in _FONTS:
            path = os.path.join(folder, name + ".ttf")
            if not os.path.exists(path):
                continue
            for size in (40, 64):
                font = ImageFont.truetype(path, size)
                for script, chars in (("latin", "0123456789"), ("arabic", _ARABIC)):
                    masks = []
                    for ch in chars:
                        img = Image.new("L", (size * 3, size * 3), 0)
                        ImageDraw.Draw(img).text((size, size), ch, font=font, fill=255)
                        a = np.asarray(img) > 128
                        if not a.any():
                            masks.append(None)
                            continue
                        ys, xs = np.nonzero(a)
                        masks.append(a[ys.min():ys.max() + 1, xs.min():xs.max() + 1])
                    shapes = [m.tobytes() + bytes(m.shape) for m in masks if m is not None]
                    if len(set(shapes)) < 8:  # the font has no such digits (empty boxes)
                        continue
                    tall = max(m.shape[0] for m in masks if m is not None)
                    for d, m in enumerate(masks):
                        if m is None:
                            continue
                        vecs.append(glyph_vector(m))
                        labels.append((script, d, m.shape[0] / tall, m.shape[0] / m.shape[1]))
        self.vecs = np.array(vecs) if vecs else np.zeros((0, GRID * GRID))
        self.labels = labels

    def classify(self, mask, rel_h):
        """(script, digit, score, margin) of the best digit for a glyph, or None."""
        if not self.labels:
            return None
        aspect = mask.shape[0] / max(mask.shape[1], 1)
        scores = self.vecs @ glyph_vector(mask)
        best = {}
        for (script, d, th, ta), s in zip(self.labels, scores):
            # the size against the other digits of the number and the shape of the box matter too:
            # a dot (Arabic zero) or a bar (one) are only told apart from specks and lines that way
            s = s - 0.6 * abs(math.log(max(rel_h, 0.05) / th)) - 0.25 * abs(math.log(aspect / ta))
            key = (script, d)
            if s > best.get(key, -9):
                best[key] = s
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        (script, d), s = ranked[0]
        other = next((v for (sc, dd), v in ranked[1:] if dd != d), -1.0)
        return script, d, float(s), float(s - other)


def glyph_numbers(ink, min_px=2, max_px=200):
    """Rows of digit-sized blobs in a binary picture, read by shape.
    Returns dicts like numbers(): norm, x, y, w, h, cx, cy, plus score and the
    blob count; only rows whose every blob is a clear digit of one script.
    A dot on the baseline between digits makes a decimal (a length, not a
    parcel number)."""
    from scipy import ndimage

    lab, n = ndimage.label(ink, structure=np.ones((3, 3), bool))
    blobs = []
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if max(h, w) > max_px or max(h, w) < min_px:
            continue
        blobs.append({"y": sl[0].start, "x": sl[1].start, "h": h, "w": w, "mask": lab[sl] == i})
    blobs.sort(key=lambda b: b["x"])
    groups = []
    for b in blobs:
        for g in groups:
            last = g[-1]
            top, bottom = min(x["y"] for x in g), max(x["y"] + x["h"] for x in g)
            gh = bottom - top
            cy = b["y"] + b["h"] / 2
            if (top - 0.15 * gh <= cy <= bottom + 0.15 * gh and b["h"] <= 1.6 * gh
                    and b["x"] - (last["x"] + last["w"]) <= 0.9 * max(gh, b["h"])):
                g.append(b)
                break
        else:
            groups.append([b])
    templates = DigitTemplates.get()
    out = []
    for g in groups:
        top, bottom = min(b["y"] for b in g), max(b["y"] + b["h"] for b in g)
        tall = max(b["h"] for b in g)
        # decimal points: small blobs low on the line (an Arabic zero is a dot too, but in the middle)
        dots = [b for b in g if max(b["h"], b["w"]) <= 0.3 * tall and b["y"] + b["h"] / 2 > top + 0.7 * tall]
        g = [b for b in g if b not in dots]
        if not g or len(g) > 6 or tall < 8:
            continue
        reads = [templates.classify(b["mask"], b["h"] / tall) for b in g]

        def clear(r):
            return r is not None and r[2] >= 0.65 and r[3] >= 0.08

        def speck(b):
            return max(b["h"], b["w"]) < 0.45 * tall

        # dust next to a number: drop small unclear blobs at either end, never inside
        while len(g) > 1 and not clear(reads[-1]) and speck(g[-1]):
            g, reads = g[:-1], reads[:-1]
        while len(g) > 1 and not clear(reads[0]) and speck(g[0]):
            g, reads = g[1:], reads[1:]
        decimal = None
        for d in dots:
            cx = d["x"] + d["w"] / 2
            left = sum(1 for b in g if b["x"] + b["w"] / 2 < cx)
            if 0 < left < len(g):
                decimal = left
        top, bottom = min(b["y"] for b in g), max(b["y"] + b["h"] for b in g)
        if any(r is None for r in reads) or len({r[0] for r in reads}) > 1:
            continue
        if reads[0][1] == 0 and len(reads) > 1:  # a number does not start with zero
            continue
        score = min(r[2] for r in reads)
        margin = min(r[3] for r in reads)
        # measured on fonts left out of the templates: these keep ~90% of the digits and no wrong ones
        if score < 0.65 or margin < 0.08:
            continue
        x0, x1 = g[0]["x"], max(b["x"] + b["w"] for b in g)
        text = "".join(str(r[1]) for r in reads)
        if decimal is not None:
            text = text[:decimal] + "." + text[decimal:]
        out.append({"norm": text, "text": text if reads[0][0] == "latin" else text.translate(
            str.maketrans("0123456789", _ARABIC)), "x": x0, "y": top, "w": x1 - x0, "h": bottom - top,
            "cx": (x0 + x1) / 2, "cy": (top + bottom) / 2, "score": score, "margin": margin, "blobs": len(g),
            "is_integer": decimal is None, "is_number": True, "disputed": False,
            "arabic": reads[0][0] == "arabic"})
    return out


def count_blobs(ink, box):
    """Digit-sized blobs whose middle lies in box (x, y, w, h) of a binary picture."""
    from scipy import ndimage

    x, y, w, h = box
    pad = 0.3 * h
    y0, y1 = max(0, int(y - pad)), int(y + h + pad) + 1
    x0, x1 = max(0, int(x - pad)), int(x + w + pad) + 1
    sub = ink[y0:y1, x0:x1]
    if sub.size == 0:
        return 0
    lab, n = ndimage.label(sub, structure=np.ones((3, 3), bool))
    count = 0
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        bh, bw = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        cy, cx = y0 + (sl[0].start + sl[0].stop) / 2, x0 + (sl[1].start + sl[1].stop) / 2
        if max(bh, bw) <= 1.6 * h and max(bh, bw) >= 2 and y <= cy <= y + h and x <= cx <= x + w:
            count += 1
    return count
