"""Legacy glyph fonts → Unicode. These are not code pages: the text is stored as Latin bytes and only shows as
Vietnamese, Bengali or Hindi in one particular font. GIS tools read them as gibberish such as "ViÖt Nam",
"evsjv‡`k" or "Hkkjr".

Tables are written from the published font layouts:
  * TCVN3 / ABC (.VnTime): Vietnamese standard TCVN 5712:1993, VN3 (lower-case font; the A1-A7 bytes carry
    the upper-case Ă Â Ê Ô Ơ Ư Đ).
  * VNI-Windows: base letter followed by a diacritic byte, as in the VNI typing layout.
  * Bijoy / SutonnyMJ (Bengali): the code positions of the SutonnyMJ font (pre-base vowel signs before the
    consonant, reph after the cluster).
  * Kruti Dev 010 (Hindi): the code positions of the Kruti Dev 010 font (i-matra before the consonant, reph
    'Z' after the syllable).
Bengali and Hindi ligature glyphs (pre-composed conjuncts) are only partly covered: conjuncts written with an
explicit virama/half-letter convert, single-glyph ligatures may stay as Latin characters (reported).
Kurdish Ali-K and Urdu InPage have no public, verifiable layout; they are not converted (documented).

Strings are taken as decoded with cp1252 (as GDAL/QGIS show them); bytes are accepted too."""
import unicodedata

# ---- Vietnamese TCVN3 -------------------------------------------------------------------------------------------
_TCVN3 = {
    0xA1: "Ă", 0xA2: "Â", 0xA3: "Ê", 0xA4: "Ô", 0xA5: "Ơ", 0xA6: "Ư", 0xA7: "Đ",
    0xA8: "ă", 0xA9: "â", 0xAA: "ê", 0xAB: "ô", 0xAC: "ơ", 0xAD: "ư", 0xAE: "đ",
    0xB5: "à", 0xB6: "ả", 0xB7: "ã", 0xB8: "á", 0xB9: "ạ",
    0xBB: "ằ", 0xBC: "ẳ", 0xBD: "ẵ", 0xBE: "ắ", 0xC6: "ặ",
    0xC7: "ầ", 0xC8: "ẩ", 0xC9: "ẫ", 0xCA: "ấ", 0xCB: "ậ",
    0xCC: "è", 0xCE: "ẻ", 0xCF: "ẽ", 0xD0: "é", 0xD1: "ẹ",
    0xD2: "ề", 0xD3: "ể", 0xD4: "ễ", 0xD5: "ế", 0xD6: "ệ",
    0xD7: "ì", 0xD8: "ỉ", 0xDC: "ĩ", 0xDD: "í", 0xDE: "ị",
    0xDF: "ò", 0xE1: "ỏ", 0xE2: "õ", 0xE3: "ó", 0xE4: "ọ",
    0xE5: "ồ", 0xE6: "ổ", 0xE7: "ỗ", 0xE8: "ố", 0xE9: "ộ",
    0xEA: "ờ", 0xEB: "ở", 0xEC: "ỡ", 0xED: "ớ", 0xEE: "ợ",
    0xEF: "ù", 0xF1: "ủ", 0xF2: "ũ", 0xF3: "ú", 0xF4: "ụ",
    0xF5: "ừ", 0xF6: "ử", 0xF7: "ữ", 0xF8: "ứ", 0xF9: "ự",
    0xFA: "ỳ", 0xFB: "ỷ", 0xFC: "ỹ", 0xFD: "ý", 0xFE: "ỵ",
}
_TCVN3_BACK = {v: k for k, v in _TCVN3.items()}


def _bytes_of(text):
    """The font byte behind each character of a cp1252-decoded string (or raw bytes)."""
    if isinstance(text, (bytes, bytearray)):
        return list(text)
    out = []
    for ch in text:
        try:
            out.append(ch.encode("cp1252")[0])
        except UnicodeEncodeError:
            out.append(ord(ch) if ord(ch) < 256 else -ord(ch))  # keep real Unicode as is (negative marker)
    return out


def tcvn3_to_unicode(text):
    out = []
    for b in _bytes_of(text):
        if b < 0:
            out.append(chr(-b))
        elif b in _TCVN3:
            out.append(_TCVN3[b])
        else:
            out.append(bytes([b]).decode("cp1252", "replace") if b >= 0x80 else chr(b))
    return "".join(out)


def unicode_to_tcvn3(text, as_bytes=False):
    """Unicode Vietnamese → TCVN3 (for legacy systems that still need it). Capital toned letters have no TCVN3
    code in the lower-case font and are written in lower case."""
    out = bytearray()
    for ch in unicodedata.normalize("NFC", text or ""):
        if ch in _TCVN3_BACK:
            out.append(_TCVN3_BACK[ch])
        elif ch.lower() in _TCVN3_BACK and ch not in "ĂÂÊÔƠƯĐ":
            out.append(_TCVN3_BACK[ch.lower()])
        else:
            out += ch.encode("cp1252", "replace")
    return bytes(out) if as_bytes else out.decode("cp1252", "replace")


# ---- Vietnamese VNI-Windows -------------------------------------------------------------------------------------
# lower-case: base + mark; upper-case uses the upper-case mark bytes
_VNI_MARKS = {  # tone/shape → (lower mark, upper mark)
    "grave": ("ø", "Ø"), "acute": ("ù", "Ù"), "hook": ("û", "Û"), "tilde": ("õ", "Õ"), "dot": ("ï", "Ï"),
}
_VNI_CIRC = {"": ("â", "Â"), "grave": ("à", "À"), "acute": ("á", "Á"), "hook": ("å", "Å"), "tilde": ("ã", "Ã"),
             "dot": ("ä", "Ä")}
_VNI_BREVE = {"": ("ê", "Ê"), "grave": ("è", "È"), "acute": ("é", "É"), "hook": ("ú", "Ú"), "tilde": ("ü", "Ü"),
              "dot": ("ë", "Ë")}
_TONE_OF = {"̀": "grave", "́": "acute", "̉": "hook", "̃": "tilde", "̣": "dot"}


def _build_vni():
    table = {}  # unicode letter → VNI string
    for base in "aeiouy":
        for tone, (lo, up) in _VNI_MARKS.items():
            cp = {"grave": "̀", "acute": "́", "hook": "̉", "tilde": "̃", "dot": "̣"}[tone]
            u = unicodedata.normalize("NFC", base + cp)
            table[u] = base + lo
            table[u.upper()] = base.upper() + up
    for tone, (lo, up) in _VNI_CIRC.items():  # â ê ô and their tones
        cp = {"": "", "grave": "̀", "acute": "́", "hook": "̉", "tilde": "̃", "dot": "̣"}[tone]
        for base in "aeo":
            u = unicodedata.normalize("NFC", base + "̂" + cp)
            table[u] = base + lo
            table[u.upper()] = base.upper() + up
    for tone, (lo, up) in _VNI_BREVE.items():  # ă and its tones
        cp = {"": "", "grave": "̀", "acute": "́", "hook": "̉", "tilde": "̃", "dot": "̣"}[tone]
        u = unicodedata.normalize("NFC", "ă" + cp)
        table[u] = "a" + lo
        table[u.upper()] = "A" + up
    # ơ = ô, ư = ö (single glyphs) + tones
    for base, lo_g, up_g in (("o", "ô", "Ô"), ("u", "ö", "Ö")):
        for tone, (lo, up) in [("", ("", ""))] + list(_VNI_MARKS.items()):
            cp = {"": "", "grave": "̀", "acute": "́", "hook": "̉", "tilde": "̃",
                  "dot": "̣"}[tone]
            u = unicodedata.normalize("NFC", base + "̛" + cp)
            table[u] = lo_g + lo
            table[u.upper()] = up_g + up
    # i and y have their own single glyphs for some tones
    table.update({"ì": "ì", "í": "í", "ỉ": "æ", "ĩ": "ó", "ị": "ò", "Ì": "Ì", "Í": "Í", "Ỉ": "Æ", "Ĩ": "Ó", "Ị": "Ò",
                  "ỵ": "î", "Ỵ": "Î", "đ": "ñ", "Đ": "Ñ"})
    return table


_VNI = _build_vni()
_VNI_BACK = sorted(((v, k) for k, v in _VNI.items()), key=lambda p: -len(p[0]))
_VNI_BACK_MAP = {}
for _v, _k in _VNI_BACK:
    _VNI_BACK_MAP.setdefault(_v, _k)


def vni_to_unicode(text):
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("cp1252", "replace")
    out, i = [], 0
    while i < len(text):
        two, one = text[i:i + 2], text[i]
        if len(two) == 2 and two in _VNI_BACK_MAP:
            out.append(_VNI_BACK_MAP[two])
            i += 2
        elif one in _VNI_BACK_MAP:
            out.append(_VNI_BACK_MAP[one])
            i += 1
        else:
            out.append(one)
            i += 1
    return "".join(out)


def unicode_to_vni(text):
    return "".join(_VNI.get(ch, ch) for ch in unicodedata.normalize("NFC", text or ""))


# ---- Bengali Bijoy / SutonnyMJ ----------------------------------------------------------------------------------
_BIJOY = {
    "Av": "আ", "A": "অ", "B": "ই", "C": "ঈ", "D": "উ", "E": "ঊ", "F": "ঋ", "G": "এ", "H": "ঐ", "I": "ও", "J": "ঔ",
    "K": "ক", "L": "খ", "M": "গ", "N": "ঘ", "O": "ঙ", "P": "চ", "Q": "ছ", "R": "জ", "S": "ঝ", "T": "ঞ", "U": "ট",
    "V": "ঠ", "W": "ড", "X": "ঢ", "Y": "ণ", "Z": "ত", "_": "থ", "`": "দ", "a": "ধ", "b": "ন", "c": "প", "d": "ফ",
    "e": "ব", "f": "ভ", "g": "ম", "h": "য", "i": "র", "j": "ল", "k": "শ", "l": "ষ", "m": "স", "n": "হ", "o": "ড়",
    "p": "ঢ়", "q": "য়", "r": "ৎ", "s": "ং", "t": "ঃ", "u": "ঁ",
    "v": "া", "x": "ী", "y": "ু", "~": "ূ", "…": "ৃ", "&": "্", "|": "।", "ª": "্র", "Ö": "্র", "¨": "্য",
    "Š": "ৗ",
    "0": "০", "1": "১", "2": "২", "3": "৩", "4": "৪", "5": "৫", "6": "৬", "7": "৭", "8": "৮", "9": "৯",
}
_BIJOY_PRE = {"w": "ি", "‡": "ে", "‰": "ৈ"}  # written before the consonant in Bijoy, after it in Unicode
_BIJOY_REPH = "©"  # reph: after the cluster in Bijoy, "র্" before it in Unicode
_BN_CONS = set("কখগঘঙচছজঝঞটঠডঢণতথদধনপফবভমযরলশষসহড়ঢ়য়ৎ")


def _bijoy_tokens(text):
    out, i = [], 0
    while i < len(text):
        if text[i:i + 2] in _BIJOY:
            out.append(_BIJOY[text[i:i + 2]])
            i += 2
            continue
        ch = text[i]
        if ch in _BIJOY_PRE:
            out.append(("PRE", _BIJOY_PRE[ch]))
        elif ch == _BIJOY_REPH:
            out.append(("REPH", "র্"))
        else:
            out.append(_BIJOY.get(ch, ch))
        i += 1
    return out


def _reorder(tokens, cons, virama, phalas=(), signs=(), merges=None):
    """Indic visual order → Unicode logical order.

    tokens are strings (letters, signs, half-letters ending in the virama) or ("PRE", sign) for a vowel sign
    written before its consonant cluster, or ("REPH", "র্"/"र्") for a reph written after its cluster."""

    def is_cons(t):
        return isinstance(t, str) and t[:1] in cons and not t.endswith(virama)

    def is_half(t):
        return isinstance(t, str) and len(t) >= 2 and t[:1] in cons and t.endswith(virama)

    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if isinstance(t, tuple) and t[0] == "PRE":
            j, cluster = i + 1, []
            while j < len(tokens) and is_half(tokens[j]):
                cluster.append(tokens[j])
                j += 1
            if j < len(tokens) and is_cons(tokens[j]):
                cluster.append(tokens[j])
                j += 1
                while j < len(tokens):
                    if tokens[j] == virama and j + 1 < len(tokens) and is_cons(tokens[j + 1]):
                        cluster += [tokens[j], tokens[j + 1]]
                        j += 2
                    elif tokens[j] in phalas:
                        cluster.append(tokens[j])
                        j += 1
                    else:
                        break
            sign = t[1]
            if merges and j < len(tokens) and (sign, tokens[j]) in merges:  # ে + া = ো, ে + ৗ = ৌ
                sign = merges[(sign, tokens[j])]
                j += 1
            out += cluster + [sign]
            i = j
            continue
        if isinstance(t, tuple) and t[0] == "REPH":
            k = len(out)
            while k > 0 and out[k - 1] in signs:
                k -= 1
            if k > 0 and is_cons(out[k - 1]):
                k -= 1
                while k >= 2 and out[k - 1] == virama and is_cons(out[k - 2]):
                    k -= 2
                while k > 0 and is_half(out[k - 1]):
                    k -= 1
            out.insert(k, t[1])
            i += 1
            continue
        out.append(t)
        i += 1
    return "".join(x if isinstance(x, str) else x[1] for x in out)


_BN_SIGNS = set("ািীুূৃেৈোৌংঃঁ")


def bijoy_to_unicode(text):
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("cp1252", "replace")
    merged = _reorder(_bijoy_tokens(text), _BN_CONS, "্", phalas=("্র", "্য"), signs=_BN_SIGNS,
                      merges={("ে", "া"): "ো", ("ে", "ৗ"): "ৌ"})
    return unicodedata.normalize("NFC", merged)


# ---- Hindi Kruti Dev 010 ----------------------------------------------------------------------------------------
_KRUTI = [  # longest first
    ("vkS", "औ"), ("vks", "ओ"), ("vk", "आ"), ("bZ", "ई"), (",s", "ऐ"), ("[k", "ख"), ("?k", "घ"), (".k", "ण"),
    ("Fk", "थ"), ("/k", "ध"), ("Hk", "भ"), ("'k", "श"), ('"k', "ष"), ("{k", "क्ष"), ("=k", "त्र"), ("ks", "ो"),
    ("kS", "ौ"),
    ("v", "अ"), ("b", "इ"), ("m", "उ"), ("Å", "ऊ"), ("_", "ऋ"), (",", "ए"),
    ("d", "क"), ("x", "ग"), ("p", "च"), ("N", "छ"), ("t", "ज"), (">", "झ"), ("V", "ट"), ("B", "ठ"), ("M", "ड"),
    ("<", "ढ"), ("r", "त"), ("n", "द"), ("u", "न"), ("i", "प"), ("Q", "फ"), ("c", "ब"), ("e", "म"), (";", "य"),
    ("j", "र"), ("y", "ल"), ("o", "व"), ("l", "स"), ("g", "ह"), ("K", "ज्ञ"),
    ("D", "क्"), ("[", "ख्"), ("X", "ग्"), ("?", "घ्"), ("P", "च्"), ("T", "ज्"), ("R", "त्"), ("F", "थ्"),
    ("/", "ध्"), ("U", "न्"), ("I", "प्"), ("C", "ब्"), ("H", "भ्"), ("E", "म्"), ("¸", "य्"), ("Y", "ल्"),
    ("O", "व्"), ("'", "श्"), ('"', "ष्"), ("L", "स्"),
    ("k", "ा"), ("h", "ी"), ("q", "ु"), ("w", "ू"), ("`", "ृ"), ("s", "े"), ("S", "ै"), ("a", "ं"), ("¡", "ँ"),
    ("%", "ः"), ("~", "्"), ("z", "्र"), ("A", "।"),
]
_KRUTI_I = "f"  # ि before the consonant
_KRUTI_REPH = "Z"  # र् written after the syllable
_HI_CONS = set("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहक्षत्रज्ञ")
_HI_SIGNS = set("ािीुूृेैोौंँः")


def krutidev_to_unicode(text):
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("cp1252", "replace")
    toks, i = [], 0
    while i < len(text):
        for k, v in _KRUTI:
            if text.startswith(k, i):
                toks.append(v)
                i += len(k)
                break
        else:
            ch = text[i]
            toks.append({_KRUTI_I: ("PRE", "ि"), _KRUTI_REPH: ("REPH", "र्")}.get(ch, ch))
            i += 1
    merged = _reorder(toks, _HI_CONS, "्", phalas=("्र",), signs=_HI_SIGNS)
    return unicodedata.normalize("NFC", merged)


# ---- detection -------------------------------------------------------------------------------------------------
CONVERTERS = {"tcvn3": tcvn3_to_unicode, "vni": vni_to_unicode, "bijoy": bijoy_to_unicode,
              "krutidev": krutidev_to_unicode}
LABELS = {"tcvn3": "Vietnamese TCVN3 (.VnTime, ABC)", "vni": "Vietnamese VNI-Windows",
          "bijoy": "Bengali Bijoy / SutonnyMJ", "krutidev": "Hindi Kruti Dev 010"}
FONT_HINTS = {".vn": "tcvn3", "vntime": "tcvn3", "vnarial": "tcvn3", "abc": "tcvn3", "vni-": "vni", "vni ": "vni",
              "sutonny": "bijoy", "bijoy": "bijoy", "kruti": "krutidev"}


def font_hint(font_name):
    """The legacy font family a font or text-style name points to (DXF styles, layer settings), or None."""
    n = (font_name or "").lower()
    for key, fam in FONT_HINTS.items():
        if key in n:
            return fam
    return None


_TCVN3_DISTINCTIVE = set("¡¢£¤¥¦§¨©ª«¬\xad®µ¶·¸¹»¼½¾ÆÇÈÉÊËÌÎÏÑ")
_VI_LETTERS = set("ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ")


_TONE_MARKS = set("̣̀́̃̉")


def _vi_score(text):
    """How Vietnamese a text looks: its letters, minus odd non-ASCII characters, and only if the words are real
    syllables - one tone mark at most per syllable, which random bytes 'converted' never respect."""
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return 0.0
    vi = sum(1 for c in letters if c in _VI_LETTERS)
    odd = sum(1 for c in text if ord(c) >= 0x80 and c.lower() not in _VI_LETTERS)
    words = [w for w in "".join(c if c.isalpha() else " " for c in text).split() if any(ord(c) > 0x7F for c in w)]
    bad_words = sum(1 for w in words
                    if sum(1 for c in w if _TONE_MARKS & set(unicodedata.normalize("NFD", c))) > 1 or len(w) > 7)
    if words and bad_words / len(words) > 0.1:
        return 0.0
    return (vi - 2 * odd) / len(letters)


def detect(texts, allow=("tcvn3", "vni"), hint=None):
    """The legacy font the texts are written in, or None. Vietnamese fonts are recognised by themselves; Bengali
    and Hindi fonts look like plain ASCII, so they are only used when allowed (country hint / user choice) and
    their conversion produces mostly Bengali/Devanagari."""
    texts = [t for t in texts if t]
    if not texts:
        return None
    if hint in CONVERTERS:
        allow = tuple(allow) + (hint,)
    joined = " ".join(texts[:300])
    best, best_s = None, 0.0
    for fam in allow:
        conv = CONVERTERS[fam](joined)
        if conv == joined:
            continue
        if fam in ("tcvn3", "vni"):
            s = _vi_score(conv) - max(0.0, _vi_score(joined))
            if fam == "tcvn3":
                # TCVN3 text always uses some bytes that no Western language writes (µ for à, ¸ for á, ­ for ư,
                # ® for đ …); "Café Müller" has none and must not be "converted"
                high = [c for c in joined if 0x80 <= ord(c) <= 0xFF]
                distinctive = sum(1 for c in high if c in _TCVN3_DISTINCTIVE)
                if not high or distinctive / len(high) < 0.2:
                    s = 0.0
        else:
            rng = (0x0980, 0x09FF) if fam == "bijoy" else (0x0900, 0x097F)
            letters = [c for c in conv if c.isalpha() or unicodedata.category(c).startswith("M")]
            s = sum(1 for c in letters if rng[0] <= ord(c) <= rng[1]) / max(len(letters), 1)
            s = s if s >= 0.8 else 0.0
        if hint == fam:
            s += 0.05
        if s > best_s + 1e-9:
            best, best_s = fam, s
    return best if best_s >= 0.15 else None


def convert(text, family):
    return CONVERTERS[family](text) if text and family in CONVERTERS else text
