"""Labels that come out right in every writing system.

Measured in QGIS 4.2.3 by drawing every script offscreen (horizontal, parallel and layout text are
right everywhere; the problems are in curved labels, letter spacing, line wrapping and fonts):
  * Arabic script - Arabic, Persian, Urdu, Kurdish, Pashto, Sindhi: curved labels along lines and
    around polygons come out reversed and with the letters unjoined, because they are drawn one
    character at a time. shape_label()/arabic_label() return the text pre-shaped (joined presentation
    forms) in display order, which draws correctly. Some letters have no presentation forms in
    Unicode (Kurdish ڕ ڵ ێ, Pashto ټ ډ ړ ږ ښ ګ ڼ ۍ, …): they can never be drawn joined on a curve, so
    such layers are switched to straight labels along the line, which shape whole words.
  * Hebrew: curved labels are drawn reversed; shape_label() puts them in display order (numbers and
    Latin keep reading left to right, gershayim stay in place).
  * Devanagari, Bengali, Tamil and other Indic scripts, Thai, Chinese, Japanese, Korean: curved
    labels are already right.
  * Thai, Lao, Khmer and Myanmar have no spaces between words, so QGIS cannot wrap long labels;
    wrap_label() breaks Thai between words with a built-in dictionary (other scripts at spaces).
  * Fonts: a label font without the script's letters shows boxes; fix_layer() picks one that has them.
  * Chinese/Japanese/Korean vertical labels work (set_vertical()); digits are stacked upright one by one.

Expression functions (group "Biruni"): shape_label(text), arabic_label(text), arabic_digits(text),
latin_digits(text), native_digits(text, script), wrap_label(text, width), thai_words(text[, sep]),
unify_letters(text, 'persian'|'arabic').
"""
import re
import unicodedata

from qgis.core import (Qgis, QgsExpression, QgsFeatureRequest, QgsMessageLog, QgsPalLayerSettings, QgsProject,
                       QgsRuleBasedLabeling, QgsSettings, QgsVectorLayer, QgsVectorLayerSimpleLabeling, qgsfunction)
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QFont, QFontDatabase, QRawFont

from . import arabic_text as at
from .i18n import tr

GROUP = "Biruni"
AUTO_KEY = "biruni/arabic-labels/auto"  # kept from the Arabic-only version so users' choice carries over
WS = QFontDatabase.WritingSystem

# names shown to the user (collected for translation as TITLES)
TITLES = {
    "arabic": "Arabic script (Arabic, Persian, Urdu, Kurdish, Pashto)",
    "hebrew": "Hebrew",
    "thai": "Thai",
    "lao": "Lao",
    "khmer": "Khmer",
    "myanmar": "Myanmar",
    "devanagari": "Devanagari (Hindi, Marathi, Nepali)",
    "bengali": "Bengali",
    "gurmukhi": "Gurmukhi",
    "gujarati": "Gujarati",
    "oriya": "Odia",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "kannada": "Kannada",
    "malayalam": "Malayalam",
    "sinhala": "Sinhala",
    "cjk": "Chinese / Japanese",
    "korean": "Korean",
    "ethiopic": "Ethiopic (Amharic, Tigrinya)",
}

# ---- scripts --------------------------------------------------------------------------------------
# id, name, sample, ranges, writing system (font check), recommended fonts (Windows first, then Noto)
SCRIPTS = [
    {"id": "arabic", "name": TITLES["arabic"],
     "sample": "شارع خیابان شاہراہ شەقام", "rtl": True, "ws": WS.Arabic,
     "ranges": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)),
     "fonts": ("Segoe UI", "Tahoma", "Arial", "Noto Naskh Arabic", "Noto Sans Arabic", "Traditional Arabic")},
    {"id": "hebrew", "name": TITLES["hebrew"], "sample": "רחוב הרצל 12", "rtl": True, "ws": WS.Hebrew,
     "ranges": ((0x0590, 0x05FF), (0xFB1D, 0xFB4F)), "fonts": ("Segoe UI", "Arial", "David", "Noto Sans Hebrew")},
    {"id": "thai", "name": TITLES["thai"], "sample": "ถนนสุขุมวิท", "ws": WS.Thai, "ranges": ((0x0E00, 0x0E7F),),
     "fonts": ("Leelawadee UI", "Tahoma", "Noto Sans Thai", "Cordia New")},
    {"id": "lao", "name": TITLES["lao"], "sample": "ວຽງຈັນ", "ws": WS.Lao, "ranges": ((0x0E80, 0x0EFF),),
     "fonts": ("Leelawadee UI", "Lao UI", "Noto Sans Lao")},
    {"id": "khmer", "name": TITLES["khmer"], "sample": "ភ្នំពេញ", "ws": WS.Khmer, "ranges": ((0x1780, 0x17FF),),
     "fonts": ("Leelawadee UI", "Khmer UI", "Noto Sans Khmer")},
    {"id": "myanmar", "name": TITLES["myanmar"], "sample": "ရန်ကုန်", "ws": WS.Myanmar, "ranges": ((0x1000, 0x109F),),
     "fonts": ("Myanmar Text", "Noto Sans Myanmar")},
    {"id": "devanagari", "name": TITLES["devanagari"], "sample": "महात्मा गांधी मार्ग",
     "ws": WS.Devanagari, "ranges": ((0x0900, 0x097F),), "fonts": ("Nirmala UI", "Mangal", "Noto Sans Devanagari")},
    {"id": "bengali", "name": TITLES["bengali"], "sample": "শহীদ মিনার সড়ক", "ws": WS.Bengali,
     "ranges": ((0x0980, 0x09FF),), "fonts": ("Nirmala UI", "Vrinda", "Noto Sans Bengali")},
    {"id": "gurmukhi", "name": TITLES["gurmukhi"], "sample": "ਸੜਕ", "ws": WS.Gurmukhi, "ranges": ((0x0A00, 0x0A7F),),
     "fonts": ("Nirmala UI", "Raavi", "Noto Sans Gurmukhi")},
    {"id": "gujarati", "name": TITLES["gujarati"], "sample": "રસ્તો", "ws": WS.Gujarati, "ranges": ((0x0A80, 0x0AFF),),
     "fonts": ("Nirmala UI", "Shruti", "Noto Sans Gujarati")},
    {"id": "oriya", "name": TITLES["oriya"], "sample": "ରାସ୍ତା", "ws": WS.Oriya, "ranges": ((0x0B00, 0x0B7F),),
     "fonts": ("Nirmala UI", "Kalinga", "Noto Sans Oriya")},
    {"id": "tamil", "name": TITLES["tamil"], "sample": "அண்ணா சாலை", "ws": WS.Tamil, "ranges": ((0x0B80, 0x0BFF),),
     "fonts": ("Nirmala UI", "Latha", "Noto Sans Tamil")},
    {"id": "telugu", "name": TITLES["telugu"], "sample": "రహదారి", "ws": WS.Telugu, "ranges": ((0x0C00, 0x0C7F),),
     "fonts": ("Nirmala UI", "Gautami", "Noto Sans Telugu")},
    {"id": "kannada", "name": TITLES["kannada"], "sample": "ರಸ್ತೆ", "ws": WS.Kannada, "ranges": ((0x0C80, 0x0CFF),),
     "fonts": ("Nirmala UI", "Tunga", "Noto Sans Kannada")},
    {"id": "malayalam", "name": TITLES["malayalam"], "sample": "റോഡ്", "ws": WS.Malayalam, "ranges": ((0x0D00, 0x0D7F),),
     "fonts": ("Nirmala UI", "Kartika", "Noto Sans Malayalam")},
    {"id": "sinhala", "name": TITLES["sinhala"], "sample": "මාර්ගය", "ws": WS.Sinhala, "ranges": ((0x0D80, 0x0DFF),),
     "fonts": ("Nirmala UI", "Iskoola Pota", "Noto Sans Sinhala")},
    {"id": "cjk", "name": TITLES["cjk"], "sample": "中山路 新宿通り", "ws": WS.SimplifiedChinese,
     "ranges": ((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)),
     "fonts": ("Microsoft YaHei", "Yu Gothic", "Meiryo", "SimSun", "MS Gothic", "Noto Sans CJK SC")},
    {"id": "korean", "name": TITLES["korean"], "sample": "세종대로", "ws": WS.Korean,
     "ranges": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)), "fonts": ("Malgun Gothic", "Noto Sans KR")},
    {"id": "ethiopic", "name": TITLES["ethiopic"], "sample": "አዲስ አበባ", "ws": None,
     "ranges": ((0x1200, 0x139F),), "probe": "ሀ", "fonts": ("Ebrima", "Nyala", "Noto Sans Ethiopic")},
]
BY_ID = {s["id"]: s for s in SCRIPTS}
NO_SPACE_SCRIPTS = ("thai", "lao", "khmer", "myanmar")


def script_of(ch):
    o = ord(ch)
    for s in SCRIPTS:
        for a, b in s["ranges"]:
            if a <= o <= b:
                return s["id"]
    return None


def detect_scripts(texts):
    """{script id: number of letters} over some texts (Latin, digits and punctuation are not counted)."""
    if isinstance(texts, str):
        texts = [texts]
    counts = {}
    for t in texts:
        for ch in str(t or ""):
            if not unicodedata.category(ch).startswith(("L", "M")):
                continue
            sid = script_of(ch)
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
    return counts


def main_script(texts):
    counts = detect_scripts(texts)
    return max(counts, key=counts.get) if counts else None


# ---- Arabic-script letters that cannot be shaped character by character ------------------------------
def unshapeable_letters(text):
    """Arabic-script letters in text that have no presentation forms (so they cannot be drawn joined when
    a renderer places one character at a time)."""
    bad = set()
    for ch in at.unshape(str(text or "")):
        if script_of(ch) == "arabic" and unicodedata.category(ch) == "Lo" and ch not in at._FORMS:
            bad.add(ch)
    return bad


def shape_text(text):
    """Text for renderers that draw one character at a time (curved labels): Arabic script shaped and in
    display order, Hebrew in display order; other text unchanged. Arabic-script text with letters that
    have no presentation forms is returned unchanged (it cannot be fixed this way)."""
    if text is None:
        return None
    text = str(text)
    scripts = detect_scripts(text)
    if "arabic" in scripts:
        return text if unshapeable_letters(text) else at.label_text(text)
    if "hebrew" in scripts:
        return at.visual(text)
    return text


# ---- digits and letters --------------------------------------------------------------------------
DIGIT_ZERO = {"arabic": 0x0660, "persian": 0x06F0, "urdu": 0x06F0, "devanagari": 0x0966, "bengali": 0x09E6,
              "gurmukhi": 0x0A66, "gujarati": 0x0AE6, "oriya": 0x0B66, "tamil": 0x0BE6, "telugu": 0x0C66,
              "kannada": 0x0CE6, "malayalam": 0x0D66, "thai": 0x0E50, "lao": 0x0ED0, "khmer": 0x17E0,
              "myanmar": 0x1040}


def to_native_digits(text, script="arabic"):
    """0-9 written with the digits of a script ('arabic', 'persian', 'devanagari', 'bengali', 'thai' …)."""
    if text is None:
        return None
    zero = DIGIT_ZERO.get(script)
    if zero is None:
        return str(text)
    s = "".join(chr(zero + ord(c) - 48) if "0" <= c <= "9" else c for c in str(text))
    if script in ("arabic", "persian", "urdu"):
        s = re.sub(r"(?<=[٠-٩۰-۹])\.(?=[٠-٩۰-۹])", "٫", s)
    return s


def to_latin_digits(text):
    """Every decimal digit (Arabic-Indic, Persian, Devanagari, Bengali, Thai …) written as 0-9."""
    if text is None:
        return None
    out = []
    for c in str(text):
        if c.isdigit() and not "0" <= c <= "9":
            try:
                out.append(str(unicodedata.digit(c)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(c)
    return re.sub(r"(?<=\d)٫(?=\d)", ".", "".join(out)).replace("٬", ",")


_TO_PERSIAN = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه"})
_TO_ARABIC = str.maketrans({"ی": "ي", "ک": "ك"})


def unify(text, target="persian"):
    """Arabic ي/ك and Persian ی/ک look alike but are different letters, so searches and joins miss;
    'persian' writes ی/ک everywhere, 'arabic' writes ي/ك."""
    if text is None:
        return None
    return str(text).translate(_TO_PERSIAN if target == "persian" else _TO_ARABIC)


# ---- Thai words ----------------------------------------------------------------------------------
# Common Thai words (places, roads, administration, numbers, everyday words). Text between known words
# is kept in syllable clusters, so a word that is not in the list is never cut inside a syllable.
THAI_WORDS = set("""
ถนน ซอย ตรอก แยก ทาง หลวง ทางหลวง สาย ถนนสาย ตำบล อำเภอ จังหวัด เขต แขวง หมู่ หมู่บ้าน บ้าน เลขที่ เมือง เทศบาล
นคร กรุงเทพ กรุงเทพมหานคร มหานคร กรุง เทพ ชุมชน ประเทศ ไทย ประเทศไทย ภาค เหนือ ใต้ ตะวันออก ตะวันตก กลาง
ตะวันออกเฉียงเหนือ แม่น้ำ แม่ น้ำ คลอง ห้วย หนอง บึง ทะเล สาบ ทะเลสาบ อ่าว เกาะ หาด แหลม ภูเขา ภู เขา ดอย ป่า
อุทยาน แห่งชาติ แห่ง ชาติ วัด โบสถ์ มัสยิด โรงเรียน โรง เรียน โรงพยาบาล พยาบาล มหาวิทยาลัย วิทยาลัย สถาบัน
ตลาด สถานี ตำรวจ ไปรษณีย์ ท่า เรือ ท่าเรือ สนาม บิน สนามบิน ท่าอากาศยาน อากาศยาน สะพาน อุโมงค์ ทางด่วน
รถไฟ รถ ไฟ ศูนย์ การค้า ศูนย์การค้า ห้าง สวน สาธารณะ สวนสาธารณะ สำนักงาน ที่ว่าการ ศาลา กลาง ศาลากลาง
นิคม อุตสาหกรรม โรงงาน ฟาร์ม นา ไร่ สวนยาง ยาง ข้าว ที่ดิน โฉนด แปลง เลข ระวาง หน้า สำรวจ เนื้อที่ งาน วา ตาราง
ใหม่ เก่า ใหญ่ น้อย เล็ก ยาว สูง ต่ำ บน ล่าง ใน นอก ข้าง หน้า หลัง ซ้าย ขวา ตรง และ กับ ของ ที่ ซึ่ง ไป มา
สุขุมวิท พหลโยธิน วิภาวดี รังสิต เพชรบุรี เพชรเกษม ราชดำเนิน สีลม สาทร พระราม เจริญ กรุง เยาวราช รัชดาภิเษก
ลาดพร้าว รามคำแหง บางนา บาง กะปิ บางกะปิ จตุจักร ปทุมวัน ดุสิต สามเสน ประชาชื่น นนทบุรี ปทุมธานี สมุทรปราการ
เชียงใหม่ เชียงราย ภูเก็ต ขอนแก่น นครราชสีมา อุดรธานี หาดใหญ่ สงขลา พัทยา ชลบุรี ระยอง อยุธยา สุโขทัย
หนึ่ง สอง สาม สี่ ห้า หก เจ็ด แปด เก้า สิบ เอ็ด สิบเอ็ด ยี่สิบ ยี่สิบเอ็ด ร้อย พัน หมื่น แสน ล้าน ที่หนึ่ง
พระ ราช พระราช สมเด็จ เจ้า พระยา เจ้าพระยา มหา ราชา ราชินี วังหลวง วัง ปราสาท อนุสาวรีย์ ประชาธิปไตย ชัยสมรภูมิ
ศาล จังหวัดศาล อาคาร ตึก คอนโด หอ พัก หอพัก โรงแรม ร้าน อาหาร ร้านอาหาร ปั๊ม น้ำมัน ธนาคาร
""".split())
_THAI_LEAD = set("เแโใไ")
_THAI_FOLLOW = set("ะาำๅๆฯ")
_THAI_MAX = max(len(w) for w in THAI_WORDS)


def _thai_mark(ch):
    return unicodedata.category(ch) in ("Mn", "Me") or ch in "ัิีึืฺุู"


def _thai_clusters(text):
    """Split a Thai run into clusters that may never be cut: a leading vowel stays with the next
    consonant, marks and following vowels with the one before, a silent consonant (with ์) with the
    syllable before it."""
    clusters = []
    i, n = 0, len(text)
    while i < n:
        j = i
        while j < n and text[j] in _THAI_LEAD:
            j += 1
        j += 1  # the base character
        while j < n and (_thai_mark(text[j]) or text[j] in _THAI_FOLLOW):
            j += 1
        # a consonant followed by the silencing mark ์ (and its own marks) belongs to this cluster
        while j + 1 < n and text[j + 1] == "์":
            j += 2
            while j < n and _thai_mark(text[j]):
                j += 1
        clusters.append(text[i:min(j, n)])
        i = min(j, n)
    return clusters


def split_thai(text):
    """Words of a Thai text (spaces and other text kept as their own tokens): dictionary maximal matching
    over syllable clusters; unknown parts stay together."""
    tokens = []
    for run in re.split(r"([฀-๿]+)", str(text or "")):
        if not run:
            continue
        if not ("฀" <= run[0] <= "๿"):
            tokens.append(run)
            continue
        cl = _thai_clusters(run)
        n = len(cl)
        best = [None] * (n + 1)  # (unknown clusters, words) cost and back pointer
        best[0] = (0, 0, -1, True)
        for i in range(n):
            if best[i] is None:
                continue
            u, w, _p, _k = best[i]
            s = ""
            for j in range(i, n):
                s += cl[j]
                if len(s) > _THAI_MAX:
                    break
                if s in THAI_WORDS:
                    cand = (u, w + 1, i, True)
                    if best[j + 1] is None or cand[:2] < best[j + 1][:2]:
                        best[j + 1] = cand
            cand = (u + 1, w + 1, i, False)  # one unknown cluster
            if best[i + 1] is None or cand[:2] < best[i + 1][:2]:
                best[i + 1] = cand
        parts, k = [], n
        while k > 0:
            _u, _w, p, known = best[k]
            parts.append(("".join(cl[p:k]), known))
            k = p
        parts.reverse()
        merged = []
        for word, known in parts:  # unknown clusters next to each other form one word
            if merged and not known and not merged[-1][1]:
                merged[-1] = (merged[-1][0] + word, False)
            else:
                merged.append((word, known))
        tokens.extend(w for w, _k in merged)
    return tokens


def _display_len(s):
    return sum(1 for c in s if not unicodedata.combining(c) and unicodedata.category(c) not in ("Mn", "Me", "Cf"))


def wrap_text(text, width=20):
    """Text broken into lines of at most `width` characters between words; Thai words are found with the
    dictionary, Chinese/Japanese may break between any two characters."""
    if text is None:
        return None
    width = max(1, int(width or 20))
    tokens = []
    for part in re.split(r"(\s+)", str(text)):
        if not part:
            continue
        if part.isspace():
            tokens.append(" ")
        elif detect_scripts(part).get("thai"):
            tokens.extend(split_thai(part))
        elif detect_scripts(part).get("cjk"):
            tokens.extend(list(part))
        else:
            tokens.append(part)
    lines, cur = [], ""
    for tok in tokens:
        if tok == " ":
            if cur:
                cur += " "
            continue
        if cur.strip() and _display_len(cur.rstrip()) + (1 if cur.endswith(" ") else 0) + _display_len(tok) > width:
            lines.append(cur.rstrip())
            cur = tok
        else:
            cur += tok
    if cur.strip():
        lines.append(cur.rstrip())
    return "\n".join(lines)


# ---- expression functions (registered safely) --------------------------------------------------------
class FunctionSet:
    """Expression functions defined once in Python. QGIS deletes a function when it is unregistered, so
    every Python wrapper ever created is kept for the life of the program - letting Python free one
    after that freed it twice and crashed QGIS on exit. Registering again creates fresh wrappers."""

    def __init__(self):
        self.specs = []
        self._keep = []

    def _make(self, fn, args):
        w = qgsfunction(args=args, group=GROUP, referenced_columns=[])(fn)
        if w is not None:
            self._keep.append(w)

    def function(self, args="auto"):
        def deco(fn):
            self.specs.append((fn, args))
            self._make(fn, args)
            return fn
        return deco

    def names(self):
        return [fn.__name__ for fn, _a in self.specs]

    def register(self):
        for fn, args in self.specs:
            if not QgsExpression.isFunctionName(fn.__name__):
                self._make(fn, args)

    def unregister(self):
        for name in self.names():
            if QgsExpression.isFunctionName(name):
                QgsExpression.unregisterFunction(name)


FUNCTIONS = FunctionSet()


def _text(v):
    return None if v is None or (hasattr(v, "isNull") and v.isNull()) else str(v)


@FUNCTIONS.function()
def arabic_label(value, feature, parent):
    """<h3>arabic_label(text)</h3>Arabic-script text ready for curved labels (along lines and around
    polygons): joined letter forms in display order. Other text is returned unchanged.<br>
    Example: <code>arabic_label("name")</code>"""
    t = _text(value)
    return at.label_text(t) if t is not None else None


@FUNCTIONS.function()
def shape_label(value, feature, parent):
    """<h3>shape_label(text)</h3>Text ready for curved labels in any script: Arabic, Persian, Urdu and
    Hebrew in display order with joined letters; other scripts unchanged.<br>
    Example: <code>shape_label("name")</code>"""
    return shape_text(_text(value))


@FUNCTIONS.function()
def arabic_digits(value, feature, parent):
    """<h3>arabic_digits(text)</h3>Digits 0-9 written as Arabic-Indic digits ٠-٩ (and the decimal point
    as ٫).<br>Example: <code>arabic_digits('12.5 م')</code> → '١٢٫٥ م'"""
    t = _text(value)
    return at.to_arabic_digits(t) if t is not None else None


@FUNCTIONS.function()
def latin_digits(value, feature, parent):
    """<h3>latin_digits(text)</h3>Digits of any script (Arabic-Indic, Persian, Devanagari, Bengali,
    Thai …) written as 0-9.<br>Example: <code>latin_digits('١٢٫٥')</code> → '12.5'"""
    return to_latin_digits(_text(value))


@FUNCTIONS.function(args=-1)
def native_digits(values, feature, parent):
    """<h3>native_digits(text[, script])</h3>Digits 0-9 written with the digits of a script: 'arabic'
    (default), 'persian', 'urdu', 'devanagari', 'bengali', 'tamil', 'thai', 'lao', 'khmer', 'myanmar' …<br>
    Example: <code>native_digits('1403', 'persian')</code> → '۱۴۰۳'"""
    args = list(values)
    t = _text(args[0] if args else None)
    return to_native_digits(t, str(args[1]) if len(args) > 1 and args[1] else "arabic")


@FUNCTIONS.function(args=-1)
def wrap_label(values, feature, parent):
    """<h3>wrap_label(text, width)</h3>The text broken into lines of at most `width` characters between
    words - also Thai, which has no spaces between words.<br>
    Example: <code>wrap_label("name", 12)</code>"""
    args = list(values)
    t = _text(args[0] if args else None)
    return wrap_text(t, args[1] if len(args) > 1 and args[1] else 20)


@FUNCTIONS.function(args=-1)
def thai_words(values, feature, parent):
    """<h3>thai_words(text[, separator])</h3>Thai text with a separator between its words (default a
    zero-width space, which lets other programs wrap it).<br>
    Example: <code>thai_words('ถนนสุขุมวิท', ' ')</code> → 'ถนน สุขุมวิท'"""
    args = list(values)
    t = _text(args[0] if args else None)
    if t is None:
        return None
    sep = str(args[1]) if len(args) > 1 and args[1] is not None else "​"
    return sep.join(w for w in split_thai(t) if not w.isspace()) if detect_scripts(t).get("thai") else t


@FUNCTIONS.function(args=-1)
def unify_letters(values, feature, parent):
    """<h3>unify_letters(text[, 'persian' | 'arabic'])</h3>Arabic ي/ك and Persian ی/ک written one way, so
    searches and joins find both.<br>Example: <code>unify_letters('كيان', 'persian')</code> → 'کیان'"""
    args = list(values)
    return unify(_text(args[0] if args else None), str(args[1]) if len(args) > 1 and args[1]
                              else "persian")


def register_functions():
    FUNCTIONS.register()


def unregister_functions():
    FUNCTIONS.unregister()


# ---- fonts ---------------------------------------------------------------------------------------
def _probe_letters(spec):
    letters = [c for c in spec.get("probe", spec["sample"]) if unicodedata.category(c).startswith("L")]
    return letters[:3] or [spec["sample"][0]]


def font_covers(font, script):
    """Does the font itself (not a fallback font) have the letters of the script?"""
    spec = BY_ID.get(script)
    if spec is None:
        return True
    fam = font.family() if isinstance(font, QFont) else str(font)
    if fam not in QFontDatabase.families():
        return False
    systems = QFontDatabase.writingSystems(fam)
    if systems and spec["ws"] is not None:
        return spec["ws"] in systems
    raw = QRawFont.fromFont(QFont(fam))
    return raw.isValid() and all(raw.supportsCharacter(ord(c)) for c in _probe_letters(spec))


def pick_font(script, prefer=None):
    """A font family installed here that has the script's letters, preferring `prefer`."""
    spec = BY_ID.get(script)
    families = set(QFontDatabase.families())
    for fam in ([prefer] if prefer else []) + list(spec["fonts"] if spec else ()):
        if fam and fam in families and font_covers(fam, script):
            return fam
    if spec:
        for fam in sorted(families):
            if font_covers(fam, script):
                return fam
    return None


def font_has_arabic(font):
    return font_covers(font, "arabic")


def arabic_font(prefer=None):
    return pick_font("arabic", prefer)


# ---- layers --------------------------------------------------------------------------------------
CURVED = (Qgis.LabelPlacement.Curved, Qgis.LabelPlacement.PerimeterCurved)
_WRAPPED = re.compile(r"^\s*(arabic_label|shape_label)\s*\((.*)\)\s*$", re.S)
_WRAPPED_WIDTH = re.compile(r"^\s*wrap_label\s*\((.*),\s*(\d+)\s*\)\s*$", re.S)


def unwrap(expr):
    """(function name, inner expression) when the label expression is one of ours, else (None, expr)."""
    expr = expr or ""
    m = _WRAPPED.match(expr)
    if m:
        return m.group(1), m.group(2).strip()
    m = _WRAPPED_WIDTH.match(expr)
    if m:
        return "wrap_label", m.group(1).strip()
    return None, expr


def _settings_list(layer):
    """[(settings, setter)] for simple and rule-based labeling; setter(new_settings) writes back."""
    lab = layer.labeling()
    out = []
    if isinstance(lab, QgsVectorLayerSimpleLabeling):
        out.append((lab.settings(), lambda s, lab=lab: lab.setSettings(QgsPalLayerSettings(s))))
    elif isinstance(lab, QgsRuleBasedLabeling):
        def walk(rule):
            for r in rule.children():
                if r.settings() is not None:
                    out.append((r.settings(), lambda s, r=r: r.setSettings(QgsPalLayerSettings(s))))
                walk(r)
        walk(lab.rootRule())
    return out


def _expression(settings):
    return settings.fieldName if settings.isExpression else QgsExpression.quotedColumnRef(settings.fieldName)


def _label_values(layer, settings, n=40):
    """A sample of the texts the labels would show (before our own functions are applied)."""
    _fn, expr = unwrap(_expression(settings))
    e = QgsExpression(expr)
    if e.hasParserError():
        return []
    from qgis.core import QgsExpressionContext, QgsExpressionContextUtils

    ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    e.prepare(ctx)
    vals = []
    for f in layer.getFeatures(QgsFeatureRequest().setLimit(n)):
        ctx.setFeature(f)
        v = e.evaluate(ctx)
        if v is not None and not (hasattr(v, "isNull") and v.isNull()):
            vals.append(str(v))
    return vals


def _set_expression(settings, expr):
    """Label text from expr: a plain quoted field name becomes the field again."""
    m = re.fullmatch(r'"((?:[^"]|"")+)"', expr.strip())
    if m:
        settings.fieldName, settings.isExpression = m.group(1).replace('""', '"'), False
    else:
        settings.fieldName, settings.isExpression = expr, True


def _spacing(font):
    return abs(font.letterSpacing()) > 1e-9 and not (font.letterSpacingType() == QFont.SpacingType.PercentageSpacing
                                                    and abs(font.letterSpacing() - 100) < 1e-9)


def problems(layer):
    """What would draw labels wrongly on this layer: [(rule index, problem)], problem being
    'curved'   - right-to-left text on a curved placement, fixable by shaping,
    'straight' - Arabic-script letters that cannot be shaped on a curved placement,
    'unneeded' - our curved-label function on a placement that is no longer curved,
    'wrap'     - Thai/Lao/Khmer/Myanmar text with automatic wrapping (QGIS cannot find their words),
    'spacing'  - letter spacing on joined (Arabic-script) letters,
    'font'     - a font without the letters of the text."""
    if not isinstance(layer, QgsVectorLayer) or not layer.labelsEnabled() or layer.labeling() is None:
        return []
    found = []
    for i, (s, _set) in enumerate(_settings_list(layer)):
        values = _label_values(layer, s)
        scripts = detect_scripts(values)
        if not scripts:
            continue
        fn, _inner = unwrap(s.fieldName if s.isExpression else "")
        curved = s.placement in CURVED
        rtl = "arabic" in scripts or "hebrew" in scripts
        bad = set().union(*(unshapeable_letters(v) for v in values)) if "arabic" in scripts else set()
        if curved and bad:
            found.append((i, "straight"))
        elif curved and rtl and fn not in ("arabic_label", "shape_label"):
            found.append((i, "curved"))
        elif not curved and fn in ("arabic_label", "shape_label"):
            found.append((i, "unneeded"))
        if any(sc in scripts for sc in NO_SPACE_SCRIPTS) and s.autoWrapLength > 0 and fn != "wrap_label":
            found.append((i, "wrap"))
        font = s.format().font()
        if "arabic" in scripts and _spacing(font):
            found.append((i, "spacing"))
        main = max(scripts, key=scripts.get)
        if not font_covers(font, main):
            found.append((i, "font"))
    return found


def fix_layer(layer):
    """Apply the fixes; returns a list of human-readable changes (empty when nothing was needed)."""
    todo = problems(layer)
    if not todo:
        return []
    by_rule = {}
    for i, what in todo:
        by_rule.setdefault(i, set()).add(what)
    changes = []
    for i, (s, setter) in enumerate(_settings_list(layer)):
        what = by_rule.get(i)
        if not what:
            continue
        values = _label_values(layer, s)
        scripts = detect_scripts(values)
        main = max(scripts, key=scripts.get) if scripts else None
        s = QgsPalLayerSettings(s)
        fmt = s.format()
        _fn, inner = unwrap(_expression(s))
        if "curved" in what:
            if "arabic" in scripts:
                s.fieldName = f"arabic_label({inner})"
                changes.append(tr("curved labels: Arabic letters joined and in the right order"))
            else:
                s.fieldName = f"shape_label({inner})"
                changes.append(tr("curved labels: right-to-left text in the right order"))
            s.isExpression = True
        if "straight" in what:
            bad = "".join(sorted(set().union(*(unshapeable_letters(v) for v in values))))
            s.placement = Qgis.LabelPlacement.Line
            _set_expression(s, inner)  # straight labels shape whole words: our curved-label text would be wrong
            changes.append(tr("curved labels changed to straight labels along the line: the letters {letters} "
                              "cannot be drawn joined on a curve").format(letters=" ".join(bad)))
        if "unneeded" in what:
            _set_expression(s, inner)
            changes.append(tr("label text put back as it is (the labels are no longer curved)"))
        if "wrap" in what:
            n = int(s.autoWrapLength)
            _fn2, cur = unwrap(_expression(s))
            s.fieldName = f"wrap_label({cur}, {n})"
            s.isExpression = True
            s.autoWrapLength = 0
            changes.append(tr("long labels wrapped between words ({n} characters a line)").format(n=n))
        if "spacing" in what:
            f = fmt.font()
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0)
            fmt.setFont(f)
            changes.append(tr("letter spacing removed (it separates Arabic letters)"))
        if "font" in what and main:
            fam = pick_font(main)
            if fam:
                f = fmt.font()
                f.setFamily(fam)
                fmt.setFont(f)
                fmt.setNamedStyle("")
                if main == "arabic":
                    changes.append(tr("font changed to {f} (the old one has no Arabic letters)").format(f=fam))
                else:
                    changes.append(tr("font changed to {f} (the old one has no {script} letters)").format(
                        f=fam, script=tr(BY_ID[main]["name"])))
        s.setFormat(fmt)
        setter(s)
    layer.triggerRepaint()
    return changes


def unfix_layer(layer):
    """Take our label functions off the label expressions (spacing, font and placement stay)."""
    done = 0
    for s, setter in _settings_list(layer):
        fn, inner = unwrap(s.fieldName if s.isExpression else "")
        if fn:
            s = QgsPalLayerSettings(s)
            s.fieldName = inner
            setter(s)
            done += 1
    if done:
        layer.triggerRepaint()
    return done


def set_vertical(layer, on=True):
    """Chinese/Japanese/Korean vertical labels (characters stacked top to bottom); returns the number of
    label rules changed."""
    n = 0
    for s, setter in _settings_list(layer):
        s = QgsPalLayerSettings(s)
        fmt = s.format()
        fmt.setOrientation(Qgis.TextOrientation.Vertical if on else Qgis.TextOrientation.Horizontal)
        s.setFormat(fmt)
        setter(s)
        n += 1
    if n:
        layer.triggerRepaint()
    return n


def _already_visual(text):
    return any("ﭐ" <= c <= "﻿" for c in text)


def fix_annotations(layer):
    """Text along a line (annotation layers) has the same problem as curved labels: its text is stored
    shaped and in display order. Returns the number of items changed."""
    from qgis.core import QgsAnnotationLayer, QgsAnnotationLineTextItem

    if not isinstance(layer, QgsAnnotationLayer):
        return 0
    n = 0
    for item_id, item in layer.items().items():
        if not isinstance(item, QgsAnnotationLineTextItem):
            continue
        text = item.text()
        scripts = detect_scripts(text)
        if "arabic" in scripts and not _already_visual(text) and not unshapeable_letters(text):
            new_text = at.label_text(text)
        elif "hebrew" in scripts and "arabic" not in scripts and not item.text().startswith("‎"):
            new_text = "‎" + at.visual(text)  # the mark tells us next time that it is done
        else:
            continue
        new = item.clone()
        new.setText(new_text)
        layer.replaceItem(item_id, new)
        n += 1
    if n:
        layer.triggerRepaint()
    return n


def fix_project(project=None):
    """Fix every layer of the project: {layer name: [changes]}."""
    from qgis.core import QgsAnnotationLayer

    project = project or QgsProject.instance()
    report = {}
    for lyr in project.mapLayers().values():
        ch = fix_layer(lyr) if isinstance(lyr, QgsVectorLayer) else []
        if isinstance(lyr, QgsAnnotationLayer) and fix_annotations(lyr):
            ch = [tr("text along lines: letters joined and in the right order")]
        if ch:
            report[lyr.name()] = ch
    return report


# ---- automatic -----------------------------------------------------------------------------------
_state = {"installed": False, "seen": {}, "iface": None, "timer": None}


def is_auto():
    return QgsSettings().value(AUTO_KEY, True, type=bool)


def set_auto(on):
    QgsSettings().setValue(AUTO_KEY, bool(on))


def _signature(layer):
    if layer.labeling() is None or not layer.labelsEnabled():
        return None
    parts = []
    for s, _set in _settings_list(layer):
        f = s.format().font()
        parts.append((s.fieldName, int(s.placement), f.family(), round(f.letterSpacing(), 3), s.autoWrapLength))
    return tuple(parts)


def _check_layers(layers, announce=True):
    if not is_auto():
        return
    fixed = {}
    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer):
            continue
        sig = _signature(lyr)
        if sig is None or _state["seen"].get(lyr.id()) == sig:
            continue
        try:
            ch = fix_layer(lyr)
        except Exception as e:  # noqa: BLE001 - never break drawing
            QgsMessageLog.logMessage(f"script labels: {e}", "Biruni GIS")
            ch = []
        _state["seen"][lyr.id()] = _signature(lyr)
        if ch:
            fixed[lyr.name()] = ch
    iface = _state["iface"]
    if fixed and announce and iface is not None:
        from .version import APP_NAME

        names = "، ".join(fixed) if any(at.has_arabic(n) for n in fixed) else ", ".join(fixed)
        iface.messageBar().pushInfo(APP_NAME, tr("Labels fixed on {layers}: {what}.").format(
            layers=names, what="; ".join(sorted({c for v in fixed.values() for c in v}))))


def _on_render(*_):
    timer = _state["timer"]
    if timer is not None and not timer.isActive():
        timer.start()


def _deferred():
    iface = _state["iface"]
    if iface is not None:
        _check_layers(iface.mapCanvas().layers())


def _on_layers_added(layers):
    _check_layers(layers)


def install(iface):
    """Fix labels by themselves: on layers added to the project, and on visible layers whenever their
    labels change (checked when the map is drawn). Also makes the calendar functions available."""
    if _state["installed"]:
        return
    register_functions()
    from . import calendars

    calendars.register_functions()
    _state.update(installed=True, iface=iface, seen={})
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setInterval(0)
    timer.timeout.connect(_deferred)
    _state["timer"] = timer
    QgsProject.instance().layersAdded.connect(_on_layers_added)
    iface.mapCanvas().renderStarting.connect(_on_render)


def uninstall():
    if not _state["installed"]:
        return
    iface = _state["iface"]
    try:
        QgsProject.instance().layersAdded.disconnect(_on_layers_added)
    except TypeError:
        pass
    if iface is not None:
        try:
            iface.mapCanvas().renderStarting.disconnect(_on_render)
        except TypeError:
            pass
    if _state["timer"] is not None:
        _state["timer"].stop()
    _state.update(installed=False, iface=None, seen={}, timer=None)
    # the functions stay registered: saved projects may use them in label expressions
