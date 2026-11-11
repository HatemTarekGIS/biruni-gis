"""Arabic text helpers (pure Python, no QGIS): normalisation for matching
names, similarity scores, and numbers / lengths written with Arabic-Indic
digits, the Arabic decimal mark or in words (deeds write "عشرون متراً").
"""
import difflib
import re
import unicodedata

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
# One character in, one character out, so positions survive (see light_normalise).
_LETTERS = str.maketrans("أإآٱىةؤ", "اااايهو")
# The letters of Persian, Urdu and other scripts typed on their keyboards in Arabic names (keheh, Farsi yeh,
# heh goal…), and the rarer alef forms: one Arabic letter each (as in the normalisations of CAMeL Tools).
_OTHER_LETTERS = str.maketrans({
    "ک": "ك", "ڪ": "ك", "ګ": "ك", "ػ": "ك", "ؼ": "ك",      # keheh, swash kaf…
    "ی": "ي", "ې": "ي", "ے": "ي", "ۓ": "ي", "ؽ": "ي",      # Farsi yeh, yeh barree…
    "ؾ": "ي", "ؿ": "ي", "ؠ": "ي", "ۍ": "ي",
    "ہ": "ه", "ۂ": "ه", "ۀ": "ه", "ە": "ه", "ھ": "ه", "ۿ": "ه",   # heh goal…
    "ۃ": "ه",                                                                     # teh marbuta goal
    "ٲ": "ا", "ٳ": "ا", "ٵ": "ا", "ٱ": "ا",                       # wavy hamza alefs
    "ٶ": "و", "ٷ": "و", "ۄ": "و", "ۆ": "و", "ۇ": "و", "ۈ": "و", "ۋ": "و",
    "ٸ": "ي",
})
# Marks that do not show but stop two names from matching: zero-width (non-)joiners, direction marks, BOM.
_INVISIBLE = {"​", "‌", "‍", "‎", "‏", "‪", "‫", "‬", "‭",
              "‮", "⁦", "⁧", "⁨", "⁩", "؜", "﻿", "­"}
_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES = re.compile(r"\s+")
_ABD = re.compile(r"\bعبد\s+ال")
_PREFIX_WORDS = {"حي", "شارع", "مخطط", "طريق", "ميدان", "منطقه", "قريه", "مدينه", "محافظه", "مركز", "حاره"}
_TASHKEEL_CHARS = set(chr(c) for c in list(range(0x610, 0x61B)) + list(range(0x64B, 0x660)) + [0x670]
                      + list(range(0x6D6, 0x6EE)) + [0x640])


def light_normalise(text):
    """Digits to ASCII, alef/ya/ta-marbuta forms unified, tashkeel and tatweel
    removed - nothing else. Returns (text, index) where index[i] is the
    position in the original text of character i, so a match found in the
    normalised text can be cut from the original."""
    out, index = [], []
    for i, ch0 in enumerate(str(text or "")):
        o = ord(ch0)
        # the presentation forms of old PDFs and CAD texts (ﻻ, ﺍﻟ…): back to the letters they show
        parts = unicodedata.normalize("NFKC", ch0) if 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF else ch0
        for ch in parts:
            if ch in _TASHKEEL_CHARS or ch in _INVISIBLE:
                continue
            if ch == "ئ":
                ch = "ي"
            elif ch == "ء":
                continue
            out.append(ch.translate(_DIGITS).translate(_OTHER_LETTERS).translate(_LETTERS))
            index.append(i)
    return "".join(out), index


def normalise(text, strip_prefixes=False):
    """A matching key: lower case, unified letters and digits, no tashkeel or
    punctuation, single spaces, "عبد ال…" written as one word. With
    strip_prefixes also drops words like "حي" / "شارع" and the article "ال"."""
    if text is None:
        return ""
    s, _ = light_normalise(str(text))
    s = _PUNCT.sub(" ", s.lower())
    s = _SPACES.sub(" ", s).strip()
    s = _ABD.sub("عبدال", s)
    if strip_prefixes:
        words = [w for w in s.split(" ") if w not in _PREFIX_WORDS]
        if not words:
            words = s.split(" ")
        s = " ".join(_strip_article(w) for w in words)
    return s


def _strip_article(word):
    for art in ("وال", "بال", "لل", "ال"):
        if word.startswith(art) and len(word) - len(art) >= 2:
            return word[len(art):]
    return word


def tokens(text):
    s = normalise(text, strip_prefixes=True)
    return s.split(" ") if s else []


def trigrams(text, normalised=False):
    s = (text if normalised else normalise(text, strip_prefixes=True)).replace(" ", "")
    if not s:
        return set()
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _dice(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def _soft_tokens(ta, tb):
    """Token overlap where a word with a one-letter slip still counts (mostly)."""
    if not ta or not tb:
        return 0.0

    def best(x, others):
        if x in others:
            return 1.0
        r = max(difflib.SequenceMatcher(None, x, y, autojunk=False).ratio() for y in others)
        return r if r >= 0.75 else 0.0

    return (sum(best(x, tb) for x in ta) + sum(best(y, ta) for y in tb)) / (len(ta) + len(tb))


def similarity(a, b):
    """0..1: 1 for the same name however it is written (hamza, ta marbuta,
    "حي", digits, spacing), high for small spelling differences."""
    return similarity_keys(normalise(a, True), normalise(b, True))


def similarity_keys(na, nb, tri_a=None, tri_b=None):
    """similarity() of two keys already made by normalise(text, True); their
    trigram sets can be passed when they are kept."""
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if ca == cb:
        return 0.98
    ta, tb = set(na.split(" ")), set(nb.split(" "))
    tri = _dice(tri_a if tri_a is not None else trigrams(na, True), tri_b if tri_b is not None else trigrams(nb, True))
    seq = difflib.SequenceMatcher(None, ca, cb, autojunk=False).ratio()
    score = 0.5 * tri + 0.3 * seq + 0.2 * _soft_tokens(ta, tb)
    if ta == tb:
        score = max(score, 0.95)
    return round(min(score, 0.97), 4)


# ---- Arabic and Latin spellings of one name -----------------------------------------------
_AR_SKELETON = {
    "ب": "b", "پ": "b", "ت": "t", "ث": "t", "ط": "t", "ج": "j", "چ": "j", "غ": "j", "گ": "j", "ح": "h", "ه": "h",
    "خ": "k", "ك": "k", "ق": "k", "ک": "k", "د": "d", "ذ": "d", "ض": "d", "ر": "r", "ز": "z", "ظ": "z", "س": "s",
    "ش": "s", "ص": "s", "ف": "f", "ڤ": "f", "ل": "l", "م": "m", "ن": "n",
}
_LATIN_DIGRAPHS = (("kh", "k"), ("sh", "s"), ("ch", "s"), ("th", "t"), ("dh", "d"), ("gh", "j"), ("ph", "f"),
                   ("ck", "k"))
_LATIN_SKELETON = str.maketrans({"q": "k", "c": "k", "g": "j", "v": "f", "p": "b", "x": "k"})
_ARABIC_LETTER = re.compile("[ء-ي]")
_LATIN_LETTER = re.compile("[A-Za-z]")


def script(text):
    """"arabic", "latin", "mixed" or "" for the letters of a text."""
    s = str(text or "")
    a, latin = bool(_ARABIC_LETTER.search(s)), bool(_LATIN_LETTER.search(s))
    return "mixed" if a and latin else "arabic" if a else "latin" if latin else ""


def latin_skeleton(text):
    """The consonants of a name as they sound, the same for «الملك فهد» and "Al Malik Fahd",
    «محمد» and "Mohammed", «عبدالله» and "Abdullah": vowels, ع / ة / long vowels and a
    word-final h dropped, close sounds merged (ق/ك/خ → k, ص/س/ش → s, ج/غ/g → j …),
    doubled letters written once. Empty for text in neither script."""
    s = str(text or "")
    words = []
    if script(s) == "arabic":
        s, _ = light_normalise(s)
        for w in re.split(r"[\s\-_/]+", s):
            w = re.sub(r"[ةه]$", "", w)
            words.append("".join(_AR_SKELETON.get(ch, "") for ch in w))
    elif script(s) == "latin":
        for w in re.split(r"[\s\-_/']+", s.lower()):
            w = re.sub(r"[^a-z]", "", w)
            for a, b in _LATIN_DIGRAPHS:
                w = w.replace(a, b)
            w = re.sub(r"(?<=[aeiouy])h$", "", w)
            w = re.sub(r"[aeiouyw]", "", w.translate(_LATIN_SKELETON))
            words.append(w)
    else:
        return ""
    return re.sub(r"(.)\1+", r"\1", "".join(words))


# ---- numbers ------------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?")


def parse_number(value):
    """The first number in a value: 20.50, ٢٠٫٥, 20,5 (decimal comma), 1,250.75;
    None when there is none."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s, _ = light_normalise(str(value))
    m = _NUM_RE.search(s)
    if not m:
        return None
    return number_from_match(m.group(0))


def number_from_match(t):
    if "," in t:
        head, _, tail = t.partition(",")
        if "." in t or (len(tail) == 3 and "," not in tail and len(head) <= 3 and head != "0"):
            t = t.replace(",", "")            # thousands separators
        else:
            t = t.replace(",", ".")           # decimal comma
    try:
        return float(t)
    except ValueError:
        return None


_UNITS = {
    "واحد": 1, "واحده": 1, "احد": 1, "احدي": 1, "اثنان": 2, "اثنين": 2, "اثنا": 2, "اثني": 2, "اثنتان": 2,
    "اثنتين": 2, "اثنتا": 2, "اثنتي": 2, "ثلاث": 3, "ثلاثه": 3, "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5,
    "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7, "ثمان": 8, "ثماني": 8, "ثمانيه": 8, "تسع": 9, "تسعه": 9,
    "عشر": 10, "عشره": 10,
}
_TENS = {
    "عشرون": 20, "عشرين": 20, "ثلاثون": 30, "ثلاثين": 30, "اربعون": 40, "اربعين": 40, "خمسون": 50, "خمسين": 50,
    "ستون": 60, "ستين": 60, "سبعون": 70, "سبعين": 70, "ثمانون": 80, "ثمانين": 80, "تسعون": 90, "تسعين": 90,
}
_HUNDRED = ("مايه", "ميه", "مايت", "ميت")
_FRACTIONS = {"نصف": 0.5, "ربع": 0.25, "ثلث": 1 / 3, "ارباع": 0.75}
_THOUSAND = {"الف": 1000, "الفا": 1000, "الاف": 1000, "الفان": 2000, "الفين": 2000}
_FILLER = {"و", "فقط", "لاغير", "لا", "غير", "قدره", "قدرها", "وقدره", "وقدرها", "مقداره", "مقدارها", "حوالي"}


def _word_value(w):
    """(kind, value) of one number word."""
    if w in _UNITS:
        return "unit", _UNITS[w]
    if w in _TENS:
        return "tens", _TENS[w]
    if w in _THOUSAND:
        return "thousand", _THOUSAND[w]
    if w in _FRACTIONS:
        return "fraction", _FRACTIONS[w]
    if w in ("مايتان", "مايتين", "ميتان", "ميتين"):
        return "hundreds", 200
    if w in _HUNDRED:
        return "hundred", 100
    for h in ("مايه", "ميه"):
        if w.endswith(h) and w[:-len(h)] in _UNITS:
            return "hundreds", _UNITS[w[:-len(h)]] * 100
    return None, None


def words_to_number(text):
    """Arabic number words to a number: "ستمائة وخمسة عشر" -> 615,
    "عشرون ونصف" -> 20.5. None when the text has no number words."""
    s = normalise(text)
    words = []
    for w in s.split(" "):
        if not w:
            continue
        kind, _ = _word_value(w)
        if kind is None and w.startswith("و") and len(w) > 2:
            if _word_value(w[1:])[0] is not None:
                w = w[1:]
        words.append(w)
    total, current, fraction, seen = 0, 0, 0.0, False
    i = 0
    while i < len(words):
        w = words[i]
        kind, val = _word_value(w)
        if kind is None:
            if w in _FILLER:
                i += 1
                continue
            if seen:
                break
            i += 1
            continue
        seen = True
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if kind == "unit":
            if nxt in ("عشر", "عشره") and val < 10:
                current += val + 10
                i += 1
            elif nxt in _HUNDRED:
                current += val * 100
                i += 1
            elif nxt == "ارباع":
                fraction += 0.25 * val
                i += 1
            else:
                current += val
        elif kind == "tens":
            current += val
        elif kind == "hundred":
            current = (current or 1) * 100 if current < 100 else current + 100
        elif kind == "hundreds":
            current += val
        elif kind == "thousand":
            if w in ("الاف", "الف", "الفا"):
                total += (current or 1) * 1000
            else:
                total += val
            current = 0
        elif kind == "fraction":
            fraction += val
        i += 1
    if not seen:
        return None
    return total + current + fraction


_M_UNIT = r"(?<![ء-يA-Za-z])(?:امتار|مترا|متر|م|m)(?![ء-ي\w²2])"
_CM_UNIT = r"(?<![ء-يA-Za-z])(?:سنتيمترات|سنتيمترا|سنتيمتر|سنتي|سم|cm)(?![ء-ي\w])"
_UNIT_SPLIT = re.compile(f"({_CM_UNIT}|{_M_UNIT})")


def parse_length(text):
    """Metres from a written length: "20.50م", "٢٠٫٥ متر", "20 م و 50 سم",
    "عشرون متراً وخمسون سنتيمتراً", "ثلاثون مترا ونصف". None when none."""
    s, _ = light_normalise(str(text or ""))
    s = s.replace("ـ", "")
    parts = _UNIT_SPLIT.split(s)
    metres, found = 0.0, False
    if len(parts) == 1:
        v = _value(parts[0])
        return v
    for chunk, unit in zip(parts[0::2], parts[1::2]):
        v = _value(chunk)
        if v is None:
            continue
        found = True
        metres += v / 100 if re.fullmatch(_CM_UNIT, unit) else v
    tail = _value(parts[-1]) if len(parts) % 2 == 1 and found else None
    if tail is not None and re.search(r"(نصف|ربع)", normalise(parts[-1])):
        metres += tail
    return metres if found else None


def _value(chunk):
    m = list(_NUM_RE.finditer(chunk))
    if m:
        return number_from_match(m[-1].group(0))
    return words_to_number(chunk)


# ---- shaping and visual order ------------------------------------------------------------------
# Some renderers draw a text one character at a time in the order it is stored (QGIS curved and
# perimeter labels): Arabic then comes out reversed and with its letters unjoined. shape() picks
# the joined letter form of every letter (presentation forms) and visual() puts the characters in
# display order, so drawing them one by one from left to right gives correct Arabic.

# letter: (isolated, final, initial, medial); None where the letter has no such form
_FORMS = {
    "ء": (0xFE80, None, None, None), "آ": (0xFE81, 0xFE82, None, None), "أ": (0xFE83, 0xFE84, None, None),
    "ؤ": (0xFE85, 0xFE86, None, None), "إ": (0xFE87, 0xFE88, None, None),
    "ئ": (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C), "ا": (0xFE8D, 0xFE8E, None, None),
    "ب": (0xFE8F, 0xFE90, 0xFE91, 0xFE92), "ة": (0xFE93, 0xFE94, None, None),
    "ت": (0xFE95, 0xFE96, 0xFE97, 0xFE98), "ث": (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),
    "ج": (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0), "ح": (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4),
    "خ": (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8), "د": (0xFEA9, 0xFEAA, None, None), "ذ": (0xFEAB, 0xFEAC, None, None),
    "ر": (0xFEAD, 0xFEAE, None, None), "ز": (0xFEAF, 0xFEB0, None, None),
    "س": (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4), "ش": (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8),
    "ص": (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC), "ض": (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0),
    "ط": (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4), "ظ": (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),
    "ع": (0xFEC9, 0xFECA, 0xFECB, 0xFECC), "غ": (0xFECD, 0xFECE, 0xFECF, 0xFED0),
    "ف": (0xFED1, 0xFED2, 0xFED3, 0xFED4), "ق": (0xFED5, 0xFED6, 0xFED7, 0xFED8),
    "ك": (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC), "ل": (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),
    "م": (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4), "ن": (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8),
    "ه": (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC), "و": (0xFEED, 0xFEEE, None, None), "ى": (0xFEEF, 0xFEF0, None, None),
    "ي": (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),
    "پ": (0xFB56, 0xFB57, 0xFB58, 0xFB59), "چ": (0xFB7A, 0xFB7B, 0xFB7C, 0xFB7D), "ژ": (0xFB8A, 0xFB8B, None, None),
    "ک": (0xFB8E, 0xFB8F, 0xFB90, 0xFB91), "گ": (0xFB92, 0xFB93, 0xFB94, 0xFB95),
    "ی": (0xFBFC, 0xFBFD, 0xFBFE, 0xFBFF),
    # Urdu, Sindhi, Kurdish, Pashto and other Arabic-script letters that have presentation forms
    "ٹ": (0xFB66, 0xFB67, 0xFB68, 0xFB69), "ٺ": (0xFB5E, 0xFB5F, 0xFB60, 0xFB61),
    "ٻ": (0xFB52, 0xFB53, 0xFB54, 0xFB55), "ڀ": (0xFB5A, 0xFB5B, 0xFB5C, 0xFB5D),
    "ٿ": (0xFB62, 0xFB63, 0xFB64, 0xFB65), "ڃ": (0xFB76, 0xFB77, 0xFB78, 0xFB79),
    "ڄ": (0xFB72, 0xFB73, 0xFB74, 0xFB75), "ڇ": (0xFB7E, 0xFB7F, 0xFB80, 0xFB81),
    "ڈ": (0xFB88, 0xFB89, None, None), "ڌ": (0xFB84, 0xFB85, None, None), "ڍ": (0xFB82, 0xFB83, None, None),
    "ڎ": (0xFB86, 0xFB87, None, None), "ڑ": (0xFB8C, 0xFB8D, None, None),
    "ڤ": (0xFB6A, 0xFB6B, 0xFB6C, 0xFB6D), "ڦ": (0xFB6E, 0xFB6F, 0xFB70, 0xFB71),
    "ڭ": (0xFBD3, 0xFBD4, 0xFBD5, 0xFBD6), "ڱ": (0xFB9A, 0xFB9B, 0xFB9C, 0xFB9D),
    "ڳ": (0xFB96, 0xFB97, 0xFB98, 0xFB99), "ں": (0xFB9E, 0xFB9F, None, None),
    "ڻ": (0xFBA0, 0xFBA1, 0xFBA2, 0xFBA3), "ھ": (0xFBAA, 0xFBAB, 0xFBAC, 0xFBAD),
    "ہ": (0xFBA6, 0xFBA7, 0xFBA8, 0xFBA9), "ۀ": (0xFBA4, 0xFBA5, None, None),
    "ۅ": (0xFBE0, 0xFBE1, None, None), "ۆ": (0xFBD9, 0xFBDA, None, None), "ۇ": (0xFBD7, 0xFBD8, None, None),
    "ۈ": (0xFBDB, 0xFBDC, None, None), "ۉ": (0xFBE2, 0xFBE3, None, None), "ۋ": (0xFBDE, 0xFBDF, None, None),
    "ې": (0xFBE4, 0xFBE5, 0xFBE6, 0xFBE7), "ے": (0xFBAE, 0xFBAF, None, None), "ۓ": (0xFBB0, 0xFBB1, None, None),
    # Kurdish ە (ae) has no presentation forms of its own; it joins only to the letter before it and is
    # drawn like heh in that position
    "ە": (0xFEE9, 0xFEEA, None, None),
}
_LAM_ALEF = {"آ": (0xFEF5, 0xFEF6), "أ": (0xFEF7, 0xFEF8), "إ": (0xFEF9, 0xFEFA), "ا": (0xFEFB, 0xFEFC)}
_JOIN_CAUSING = {"ـ", "‍"}  # tatweel, zero-width joiner


def _is_mark(ch):
    return ch in _TASHKEEL_CHARS and ch != "ـ"


def _joins_left(ch):
    """Can ch connect to the letter after it (in reading order)?"""
    if ch in _JOIN_CAUSING:
        return True
    f = _FORMS.get(ch)
    return f is not None and f[2] is not None


def _joins_right(ch):
    """Can ch connect to the letter before it?"""
    return ch in _JOIN_CAUSING or (ch in _FORMS and _FORMS[ch][1] is not None)


def shape(text):
    """Arabic letters replaced by their joined forms (initial, medial, final, isolated) and lam-alef
    ligatures. Marks (tashkeel) stay after their letter."""
    if not text:
        return text
    chars = list(text)
    n = len(chars)

    def neighbour(i, step):
        j = i + step
        while 0 <= j < n and _is_mark(chars[j]):
            j += step
        return chars[j] if 0 <= j < n else ""

    out = []
    i = 0
    while i < n:
        ch = chars[i]
        if ch not in _FORMS:
            out.append(ch)
            i += 1
            continue
        prev, nxt = neighbour(i, -1), neighbour(i, 1)
        joined_before = bool(prev) and _joins_left(prev) and _joins_right(ch)
        if ch == "ل" and nxt in _LAM_ALEF:
            # skip the marks between lam and alef; keep them after the ligature
            j = i + 1
            marks = []
            while j < n and _is_mark(chars[j]):
                marks.append(chars[j])
                j += 1
            iso, fin = _LAM_ALEF[nxt]
            out.append(chr(fin if joined_before else iso))
            out.extend(marks)
            i = j + 1
            continue
        joined_after = bool(nxt) and _joins_left(ch) and _joins_right(nxt)
        iso, fin, ini, med = _FORMS[ch]
        if joined_before and joined_after and med:
            code = med
        elif joined_before and fin:
            code = fin
        elif joined_after and ini:
            code = ini
        else:
            code = iso
        out.append(chr(code))
        i += 1
    return "".join(out)


def unshape(text):
    """Presentation forms (and lam-alef ligatures) back to ordinary Arabic letters."""
    import unicodedata

    if not text:
        return text
    return "".join(unicodedata.normalize("NFKC", c) if "ﭐ" <= c <= "﷿" or "ﹰ" <= c <= "﻿"
                   else c for c in text)


_MIRROR = str.maketrans("()[]{}<>«»", ")(][}{><»«")


def _strong(ch):
    """'R' for Arabic/Hebrew letters, 'L' for other letters, 'N' for digits (they read left to right),
    None for neutrals (spaces, punctuation)."""
    import unicodedata

    if ch.isdigit():
        return "N"
    bidi = unicodedata.bidirectional(ch)
    if bidi in ("R", "AL"):
        return "R"
    if bidi == "L":
        return "L"
    if bidi in ("EN", "AN"):
        return "N"
    return None


def _clusters(text):
    out = []
    for ch in text:
        if out and _is_mark(ch):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def visual(text, base=None):
    """Characters in display order (Unicode bidi rule L2, simplified for one-line labels): the order
    in which they appear from left to right. Marks stay with their letter; brackets are mirrored in
    right-to-left runs. base: 'R' or 'L'; by default the first strong letter decides."""
    if not text:
        return text
    cl = _clusters(text)
    kinds = [_strong(c[0]) for c in cl]
    if base is None:
        base = next((k for k in kinds if k in ("R", "L")), "L")
    base_level = 1 if base == "R" else 0
    levels = []
    for k in kinds:
        if k == "R":
            levels.append(1)
        elif k in ("L", "N"):
            levels.append(2 if base_level == 1 else 0)
        else:
            levels.append(None)
    # bracket pairs (bidi rule N0): numbers count as right-to-left inside them, so "(12)" in Arabic
    # text keeps its brackets with the Arabic around it
    stack = []
    for i, c in enumerate(cl):
        if c in "([{«":
            stack.append(i)
        elif c in ")]}»" and stack:
            o = stack.pop()
            inside = kinds[o + 1:i]
            if base_level == 1 and ("R" in inside or "N" in inside):
                levels[o] = levels[i] = 1
            elif "L" in inside and "R" not in inside:
                levels[o] = levels[i] = 2 if base_level == 1 else 0
    # neutrals take the level of the strong text around them when both sides agree, else the base level
    for i, lv in enumerate(levels):
        if lv is not None:
            continue
        left = next((levels[j] for j in range(i - 1, -1, -1) if levels[j] is not None), base_level)
        right = next((levels[j] for j in range(i + 1, len(levels)) if levels[j] is not None), base_level)
        levels[i] = left if left == right else base_level
    order = list(range(len(cl)))
    for level in range(max(levels), 0, -1):
        i = 0
        while i < len(order):
            if levels[order[i]] >= level:
                j = i
                while j < len(order) and levels[order[j]] >= level:
                    j += 1
                order[i:j] = order[i:j][::-1]
                i = j
            else:
                i += 1
    out = []
    for idx in order:
        c = cl[idx]
        out.append(c.translate(_MIRROR) if levels[idx] % 2 == 1 else c)
    return "".join(out)


def has_arabic(text):
    return bool(text) and any("؀" <= c <= "ۿ" or "ﭐ" <= c <= "﻿" for c in str(text))


def label_text(text):
    """Text for renderers that draw characters one by one (curved labels): shaped, in visual order."""
    if not has_arabic(text):
        return text
    return visual(shape(unshape(text)))


# ---- digits --------------------------------------------------------------------------------------
_TO_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_TO_LATIN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def to_arabic_digits(text, decimal=True):
    """0-9 → ٠-٩; with decimal, a dot between digits becomes the Arabic decimal mark ٫."""
    if text is None:
        return None
    s = str(text).translate(_TO_ARABIC_DIGITS)
    return re.sub(r"(?<=[٠-٩])\.(?=[٠-٩])", "٫", s) if decimal else s


def to_latin_digits(text):
    if text is None:
        return None
    s = str(text).translate(_TO_LATIN_DIGITS)
    return re.sub(r"(?<=\d)٫(?=\d)", ".", s).replace("٬", ",")


def remove_tatweel(text):
    return text.replace("ـ", "") if text else text


# ---- repair ----------------------------------------------------------------------------------------
_COMMON_WORDS = {"شارع", "حي", "طريق", "قطعه", "قطعة", "مخطط", "رقم", "مدينه", "مدينة", "منطقه", "منطقة", "مركز",
                 "الملك", "الامير", "الأمير", "عبد", "عبدالله", "محمد", "بن", "ابن", "مسجد", "مدرسه", "مدرسة",
                 "سكني", "تجاري", "حديقه", "حديقة", "ميدان", "جامع", "بلديه", "بلدية", "محافظه", "محافظة", "قريه",
                 "قرية", "شمال", "جنوب", "شرق", "غرب", "الشمالي", "الجنوبي", "الشرقي", "الغربي", "ارض", "أرض",
                 "مبنى", "عماره", "عمارة", "فيلا", "مستشفى", "مكتب", "سوق", "وادي", "جبل", "بئر", "مزرعه", "مزرعة"}
_FINAL_ONLY = set("ةى")


def _order_score(text):
    """How much a plain-letter Arabic text looks like normal reading order (higher = more natural)."""
    score = 0.0
    for w in re.findall(r"[ء-ي]+", text):
        if len(w) >= 3 and w.startswith("ال"):
            score += 1
        if len(w) >= 3 and w.endswith("لا"):
            score -= 1
        if w[0] in _FINAL_ONLY:
            score -= 2
        if w[-1] in _FINAL_ONLY:
            score += 1
        if w in _COMMON_WORDS or (w.startswith("ال") and w[2:] in _COMMON_WORDS):
            score += 2
        if w[::-1] in _COMMON_WORDS:
            score -= 2
    return score


def _forms_order(text):
    """+1 when presentation forms say the text is in reading order (a word starts with an initial or
    isolated form), -1 when they say it is reversed (it starts with a final form), 0 when unknown."""
    final = {f[1] for f in _FORMS.values() if f[1]} | {v[1] for v in _LAM_ALEF.values()}
    initial = {f[2] for f in _FORMS.values() if f[2]}
    vote = 0
    for w in re.findall(r"[ﭐ-﷿ﹰ-﻿]{2,}", text):
        first, last = ord(w[0]), ord(w[-1])
        if first in initial or last in final:
            vote += 1
        if first in final or last in initial:
            vote -= 1
    return (vote > 0) - (vote < 0)


def is_reversed(text):
    """True when Arabic text is stored in display order (reversed), as old CAD and DOS systems did."""
    if not has_arabic(text):
        return False
    vote = _forms_order(text)
    if vote:
        return vote < 0
    plain = unshape(text)
    return _order_score(visual(plain)) > _order_score(plain) + 0.5


def fix_reversed(text):
    """Text stored in display order back to reading order (visual order is its own inverse)."""
    if not text:
        return text
    return visual(unshape(text))


_MOJIBAKE_SOURCES = ("cp1252", "latin-1")
_CANDIDATE_ENCODINGS = ("utf-8", "cp1256", "cp720", "iso8859-6", "cp864")


def arabic_quality(text):
    """0..1: how much a text looks like valid Arabic (letters in the Arabic block, no stray Latin-1
    accented letters, no replacement characters)."""
    if not text:
        return 0.0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.5
    good = sum(1 for c in letters if "ء" <= c <= "ي" or c in "پچژکگی")
    bad = sum(1 for c in text if "À" <= c <= "ÿ" or c in "�?" or "\u0080" <= c <= "\u009F")
    return max(0.0, (good - 2 * bad) / len(letters))


def fix_mojibake(text):
    """Arabic that was decoded with the wrong code page ("ÔÇÑÚ" for cp1256 read as Latin-1, "Ø´Ø§Ø±Ø¹" for
    UTF-8 read as Latin-1) back to Arabic. Returns (text, how) - how is '' when nothing was changed."""
    if not text or has_arabic(text) and arabic_quality(text) > 0.9:
        return text, ""
    best, best_q, how = text, arabic_quality(text), ""
    for src in _MOJIBAKE_SOURCES:
        try:
            raw = text.encode(src)
        except UnicodeEncodeError:
            continue
        for enc in _CANDIDATE_ENCODINGS:
            try:
                cand = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            q = arabic_quality(cand)
            if q >= 0.6 and q > best_q + 0.2:  # a few Arabic letters inside Latin words are not a repair
                best, best_q, how = cand, q, f"{src}→{enc}"
    return best, how


def guess_encoding(samples):
    """The code page that decodes raw byte strings into the most valid Arabic, with its score."""
    samples = [s for s in samples if s and any(b >= 0x80 for b in s)]
    if not samples:
        return None, 0.0
    best, best_q = None, -1.0
    for enc in _CANDIDATE_ENCODINGS:
        total, ok = 0.0, True
        for s in samples:
            try:
                total += arabic_quality(s.decode(enc))
            except UnicodeDecodeError:
                ok = False
                break
        q = total / len(samples) if ok else -1.0
        if q > best_q + 1e-9:
            best, best_q = enc, q
    return best, best_q


# ---- Hijri (Umm al-Qura) ↔ Gregorian -------------------------------------------------------------
# Month lengths of the Umm al-Qura calendar, 1318-1500 AH: one number per year, 12 bits from
# Muharram (most significant) to Dhu al-Hijjah, 1 = 30 days, 0 = 29 days. 1 Muharram 1318 =
# 30 April 1900. Generated from the Umm al-Qura calendar of Windows (.NET UmAlQuraCalendar).
_UQ_FIRST_YEAR = 1318
_UQ_YEARS = (
    1396, 2422, 1207, 599, 1323, 1685, 1738, 2773, 1371, 605, 2349, 3221, 3402, 3749, 1746, 2773, 1370, 2731, 1355,
    1701, 1874, 2985, 884, 2742, 1366, 2730, 3410, 3497, 1492, 2794, 1245, 622, 2350, 2726, 3412, 3498, 1461, 694,
    2359, 1179, 2635, 2853, 2900, 2922, 1389, 1197, 2645, 3365, 3730, 3785, 1748, 2794, 1387, 1195, 1685, 2889,
    2980, 2994, 1461, 698, 2395, 1195, 1365, 1714, 1753, 748, 2414, 1198, 2646, 3370, 3413, 1450, 2741, 1211, 603,
    2347, 2709, 2890, 2981, 1450, 2741, 1366, 2710, 3402, 3749, 1874, 1769, 874, 2733, 1365, 2725, 2898, 2985,
    1460, 2490, 1243, 605, 1325, 2725, 2772, 2794, 1389, 1213, 573, 2333, 2709, 2890, 2906, 1389, 694, 2363, 1179,
    1621, 1705, 1876, 2922, 1388, 2733, 1365, 2857, 2962, 2985, 1492, 2778, 1370, 2731, 1429, 1865, 1892, 2986,
    1461, 694, 2646, 3370, 3733, 1834, 1877, 858, 2397, 1179, 2637, 3366, 3411, 1450, 2733, 1206, 2647, 1319,
    2709, 2890, 2901, 876, 2478, 1206, 2710, 2890, 3493, 1490, 1497, 732, 2413, 1197, 1621, 1746, 2921, 884, 2486,
    1239, 683, 1355, 1701, 1874, 2921, 1387, 685, 2381, 3221, 3402, 3749, 1738, 2773, 1366, 3223)
_UQ_LAST_YEAR = _UQ_FIRST_YEAR + len(_UQ_YEARS) - 1
_UQ_EPOCH = None  # ordinal (datetime.date.toordinal) of 1 Muharram 1318
_UQ_MONTH_STARTS = []  # ordinal of the first day of every month, then one past the end


def _uq_table():
    global _UQ_EPOCH
    if not _UQ_MONTH_STARTS:
        import datetime

        _UQ_EPOCH = datetime.date(1900, 4, 30).toordinal()
        day = _UQ_EPOCH
        for bits in _UQ_YEARS:
            for m in range(12):
                _UQ_MONTH_STARTS.append(day)
                day += 30 if bits >> (11 - m) & 1 else 29
        _UQ_MONTH_STARTS.append(day)
    return _UQ_MONTH_STARTS


# 1343-1500 AH (1 August 1924 - 16 November 2077) come from hijridate (MIT, bundled in <app>/python): the
# official Umm al-Qura calendar, checked against the archived Umm al-Qura newspaper and the KACST tables. The
# table above (from Windows) agrees with it from 1402 AH on, but is a day off on about 9,300 days of 1343-1401 AH,
# the years of many old title deeds; it is kept for 1318-1342 AH, which hijridate does not cover.
_HD = []


def _hijridate():
    """The hijridate module, or None when it cannot be loaded (the tables are used then)."""
    if not _HD:
        try:
            from .vendor import ensure_vendor

            ensure_vendor()
            import hijridate

            _HD.append(hijridate)
        except Exception:  # noqa: BLE001 - outside the app, or the package missing: the table is used
            _HD.append(None)
    return _HD[0]


_HD_FIRST, _HD_LAST = 1343, 1500


def _civil_to_ordinal(y, m, d):
    """Tabular (arithmetical) Islamic calendar, used outside the Umm al-Qura table."""
    import math

    jdn = d + math.ceil(29.5 * (m - 1)) + (y - 1) * 354 + (3 + 11 * y) // 30 + 1948439
    return jdn - 1721425  # Julian day number → proleptic Gregorian ordinal


def hijri_to_gregorian(y, m, d):
    """(datetime.date, exact). exact is False outside 1318-1500 AH (tabular calendar used, ±1 day)."""
    import datetime

    if not (1 <= m <= 12 and 1 <= d <= 31):
        raise ValueError(f"not a Hijri date: {y}/{m}/{d}")
    hd = _hijridate() if _HD_FIRST <= y <= _HD_LAST else None     # it knows its months (1345/5 has 31 days)
    if hd is None and d > 30:
        raise ValueError(f"not a Hijri date: {y}/{m}/{d}")
    if hd is not None:
        try:
            g = hd.Hijri(y, m, d).to_gregorian()
        except (ValueError, OverflowError) as e:
            raise ValueError(f"{y}/{m}/{d}: {e}") from e
        return datetime.date(g.year, g.month, g.day), True
    if _UQ_FIRST_YEAR <= y <= _UQ_LAST_YEAR:
        starts = _uq_table()
        k = (y - _UQ_FIRST_YEAR) * 12 + (m - 1)
        length = starts[k + 1] - starts[k]
        if d > length:
            raise ValueError(f"{y}/{m} has {length} days")
        return datetime.date.fromordinal(starts[k] + d - 1), True
    return datetime.date.fromordinal(_civil_to_ordinal(y, m, d)), False


def gregorian_to_hijri(date):
    """((year, month, day), exact) for a datetime.date."""
    import bisect

    hd = _hijridate()
    if hd is not None:
        try:
            h = hd.Gregorian(date.year, date.month, date.day).to_hijri()
            return (h.year, h.month, h.day), True
        except (ValueError, OverflowError):
            pass       # outside 1343-1500 AH
    starts = _uq_table()
    o = date.toordinal()
    if starts[0] <= o < starts[-1]:
        k = bisect.bisect_right(starts, o) - 1
        return (_UQ_FIRST_YEAR + k // 12, k % 12 + 1, o - starts[k] + 1), True
    # tabular calendar outside the table
    y = max(1, int((o - _civil_to_ordinal(1, 1, 1)) / 354.36667))
    while _civil_to_ordinal(y + 1, 1, 1) <= o:
        y += 1
    while y > 1 and _civil_to_ordinal(y, 1, 1) > o:
        y -= 1
    m = 1
    while m < 12 and _civil_to_ordinal(y, m + 1, 1) <= o:
        m += 1
    return (y, m, o - _civil_to_ordinal(y, m, 1) + 1), False


_HIJRI_MARK = re.compile(r"\s*(هـ|ه\.?|هجري[ةه]?|h\.?|ah)\s*$", re.IGNORECASE)
_DATE_PARTS = re.compile(r"(\d{1,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,4})")


def parse_hijri(text):
    """(year, month, day) from '١٤٤٥/٠٣/١٢ هـ', '12-3-1445', '1445/3/12H'…; the four-digit part (or the
    part above 31) is the year. None when no date is found."""
    if text is None:
        return None
    s = to_latin_digits(str(text)).strip()
    s = _HIJRI_MARK.sub("", s)
    m = _DATE_PARTS.search(s)
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    if a > 31:
        y, mo, d = a, b, c
    elif c > 31:
        y, mo, d = c, b, a
    else:
        return None
    if not (1 <= mo <= 12 and 1 <= d <= (31 if (y, mo) == (1345, 5) else 30)):   # the one 31-day month
        return None
    return y, mo, d


def parse_gregorian(text):
    """datetime.date from '2024-03-11', '11/03/2024', '١١-٣-٢٠٢٤' (day first when the year is last);
    None for anything else, including Hijri years."""
    import datetime

    if text is None:
        return None
    s = to_latin_digits(str(text)).strip()
    m = _DATE_PARTS.search(s)
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    y, mo, d = (a, b, c) if a > 31 else (c, b, a) if c > 31 else (None, None, None)
    if y is None or not (1700 <= y <= 2300):
        return None
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def format_hijri(ymd, pattern="yyyy/mm/dd", arabic_digits=False, suffix=True):
    y, m, d = ymd
    s = pattern.replace("yyyy", f"{y:04d}").replace("mm", f"{m:02d}").replace("dd", f"{d:02d}")
    if suffix:
        s += " هـ"
    return to_arabic_digits(s, decimal=False) if arabic_digits else s
