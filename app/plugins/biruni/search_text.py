"""One way to compare what the user types with names, for every search box of the program.

``fold`` makes a search key: lower case, no accents (é → e, ç → c, ş → s, ı → i), Arabic and Persian letters
unified (أ إ آ ٱ → ا, ى ی → ي, ة → ه, ؤ → و, ئ → ي, ک → ك), no tashkeel or tatweel, Arabic-Indic digits as ASCII,
punctuation as spaces. So «اضافة» finds «إضافة», "creer" finds "Créer" and "cizgi" finds "Çizgi".
"""
import re
import unicodedata

_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي",
                      "ک": "ك", "ۀ": "ه", "ı": "i", "ß": "ss",
                      "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8",
                      "٩": "9", "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4", "۵": "5", "۶": "6", "۷": "7",
                      "۸": "8", "۹": "9", "ـ": None, "ء": None, "‌": " ", "‏": None, "‎": None})
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES = re.compile(r"\s+")


def fold(text):
    s = str(text or "").translate(_MAP).casefold()
    # accents and Arabic marks are combining characters once decomposed (the Arabic letters above were
    # mapped first: NFKD would split آ into ا + mark anyway, but أ / إ must become plain ا, not ا + hamza)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def matches(query, *texts):
    """True when every word of ``query`` is in one of ``texts`` (folded)."""
    q = fold(query).split()
    if not q:
        return True
    hay = " ".join(fold(t) for t in texts if t)
    return all(w in hay for w in q)
