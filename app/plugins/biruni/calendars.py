"""Calendars used on maps and in land records around the world.

Gregorian; Hijri (Umm al-Qura, Saudi Arabia - tables in arabic_text); Solar Hijri / Jalali (Iran and
Afghanistan - exact arithmetic, the algorithm of jalaali-js / Borkowski, valid for years 1-3177);
Thai Buddhist era (Gregorian year + 543); Ethiopian (13 months, exact arithmetic).

Dates are datetime.date on the Gregorian side and (year, month, day) tuples in the other calendars.

Expression functions (group "Biruni"): to_hijri(date[, pattern]), from_hijri(text),
to_jalali(date[, pattern]), from_jalali(text), to_calendar(date, calendar[, pattern]),
from_calendar(text, calendar).
"""
import datetime
import re
import unicodedata

from qgis.PyQt.QtCore import QDate, QDateTime

from . import arabic_text as at
from .i18n import tr
from .script_labels import FunctionSet, GROUP

CALENDARS = ("gregorian", "hijri", "jalali", "buddhist", "ethiopian")
NAMES = {"gregorian": "Gregorian", "hijri": "Hijri (Umm al-Qura)", "jalali": "Solar Hijri (Jalali, Iran)",
         "buddhist": "Thai Buddhist era", "ethiopian": "Ethiopian"}
_COUNTRY = {"SA": ["hijri"], "YE": ["hijri"], "IR": ["jalali"], "AF": ["jalali", "hijri"], "TH": ["buddhist"],
            "ET": ["ethiopian"], "ER": ["ethiopian"]}

MONTHS = {
    "hijri": ("محرم", "صفر", "ربيع الأول", "ربيع الآخر", "جمادى الأولى", "جمادى الآخرة", "رجب", "شعبان",
              "رمضان", "شوال", "ذو القعدة", "ذو الحجة"),
    "jalali": ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن",
               "اسفند"),
    "buddhist": ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม",
                 "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"),
    "ethiopian": ("መስከረም", "ጥቅምት", "ኅዳር", "ታኅሣሥ", "ጥር", "የካቲት", "መጋቢት", "ሚያዝያ", "ግንቦት", "ሰኔ", "ሐምሌ",
                  "ነሐሴ", "ጳጉሜን"),
    "gregorian": ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                  "October", "November", "December"),
}
SUFFIX = {"hijri": "هـ", "jalali": "ه‍.ش", "buddhist": "พ.ศ.", "ethiopian": "ዓ.ም", "gregorian": ""}
_NATIVE_ZERO = {"hijri": 0x0660, "jalali": 0x06F0, "buddhist": 0x0E50}


def for_country(cc):
    """Calendars (besides Gregorian) people use in a country, by ISO code."""
    return list(_COUNTRY.get((cc or "").upper(), []))


# ---- Jalali (Solar Hijri) ---------------------------------------------------------------------------
_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394,
           2456, 3178)


def _div(a, b):  # integer division truncating toward zero, as in the reference (JavaScript) algorithm
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _mod(a, b):
    return a - _div(a, b) * b


def _jal_cal(jy):
    """(leap: years since the last leap year (0 = leap year), gy, march: day of March of 1 Farvardin) -
    jalaali-js jalCal."""
    if jy < _BREAKS[0] or jy >= _BREAKS[-1]:
        raise ValueError(f"Jalali year {jy} out of range")
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    jump = 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def jalali_leap(jy):
    return _jal_cal(jy)[0] == 0


def jalali_month_length(jy, jm):
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if jalali_leap(jy) else 29


def jalali_to_gregorian(jy, jm, jd):
    if not (1 <= jm <= 12 and 1 <= jd <= jalali_month_length(jy, jm)):
        raise ValueError(f"no such Jalali date {jy}/{jm}/{jd}")
    _leap, gy, march = _jal_cal(jy)
    start = datetime.date(gy, 3, march).toordinal()
    days = (jm - 1) * 31 - (jm // 7) * (jm - 7) + jd - 1
    return datetime.date.fromordinal(start + days)


def gregorian_to_jalali(date):
    gy = date.year
    jy = gy - 621
    leap, _gy, march = _jal_cal(jy)
    k = date.toordinal() - datetime.date(gy, 3, march).toordinal()
    if k >= 0:
        if k <= 185:
            return jy, 1 + k // 31, k % 31 + 1
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:  # the year before was a leap year: its Esfand has 30 days
            k += 1
    return jy, 7 + k // 30, k % 30 + 1


# ---- Ethiopian ----------------------------------------------------------------------------------
_ETH_EPOCH = datetime.date(8, 8, 27).toordinal()  # 1 Meskerem 1 = 29 Aug 8 (Julian) = 27 Aug 8 (proleptic Gregorian)


def ethiopian_to_gregorian(y, m, d):
    if not (1 <= m <= 13 and 1 <= d <= (30 if m < 13 else (6 if y % 4 == 3 else 5))):
        raise ValueError(f"no such Ethiopian date {y}/{m}/{d}")
    return datetime.date.fromordinal(_ETH_EPOCH - 1 + 365 * (y - 1) + y // 4 + 30 * (m - 1) + d)


def gregorian_to_ethiopian(date):
    n = date.toordinal()
    y = (4 * (n - _ETH_EPOCH) + 1463) // 1461
    first = ethiopian_to_gregorian(y, 1, 1).toordinal()
    m = (n - first) // 30 + 1
    d = n - ethiopian_to_gregorian(y, m, 1).toordinal() + 1
    return y, m, d


# ---- one interface for all ------------------------------------------------------------------------
def from_gregorian(date, calendar):
    """(year, month, day) of a Gregorian date in `calendar`; exact is False only for Hijri dates outside
    the Umm al-Qura table. Returns ((y, m, d), exact)."""
    if calendar == "gregorian":
        return (date.year, date.month, date.day), True
    if calendar == "hijri":
        return at.gregorian_to_hijri(date)
    if calendar == "jalali":
        return gregorian_to_jalali(date), True
    if calendar == "buddhist":
        return (date.year + 543, date.month, date.day), True
    if calendar == "ethiopian":
        return gregorian_to_ethiopian(date), True
    raise ValueError(calendar)


def to_gregorian(calendar, y, m, d):
    """Gregorian date of a date in `calendar`: (date, exact). ValueError for impossible dates."""
    if calendar == "gregorian":
        return datetime.date(y, m, d), True
    if calendar == "hijri":
        return at.hijri_to_gregorian(y, m, d)
    if calendar == "jalali":
        return jalali_to_gregorian(y, m, d), True
    if calendar == "buddhist":
        return datetime.date(y - 543, m, d), True
    if calendar == "ethiopian":
        return ethiopian_to_gregorian(y, m, d), True
    raise ValueError(calendar)


def native_digits(text, calendar):
    zero = _NATIVE_ZERO.get(calendar)
    if zero is None or text is None:
        return text
    return "".join(chr(zero + int(c)) if "0" <= c <= "9" else c for c in str(text))


def latin_digits(text):
    """Every decimal digit (Arabic-Indic, Persian, Thai, Devanagari …) as 0-9."""
    if text is None:
        return None
    out = []
    for c in str(text):
        if c.isdigit() and not ("0" <= c <= "9"):
            try:
                out.append(str(unicodedata.digit(c)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(c)
    return "".join(out).replace("٫", ".").replace("٬", ",")


def format_date(calendar, ymd, pattern="yyyy/mm/dd", digits="latin", month_names=False, suffix=True):
    """Text of a date: pattern with yyyy, mm, dd (and MMMM for the month name)."""
    y, m, d = ymd
    text = pattern.replace("yyyy", f"{y:04d}").replace("mm", f"{m:02d}").replace("dd", f"{d:02d}")
    if "MMMM" in text or month_names:
        name = MONTHS[calendar][m - 1]
        text = text.replace("MMMM", name) if "MMMM" in text else f"{d} {name} {y}"
    if digits == "native":
        text = native_digits(text, calendar)
    if suffix and SUFFIX.get(calendar):
        text = f"{text} {SUFFIX[calendar]}"
    return text


_PARTS = re.compile(r"(\d{1,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,4})")
_MARKS = (("jalali", re.compile(r"ه‍?\.?\s*ش|هجری\s*شمسی|شمسی|\bSH\b|solar", re.I)),
          ("hijri", re.compile(r"هـ|هجري|\bAH\b|\bH\b(?!\w)|\d\s*h\b", re.I)),
          ("buddhist", re.compile(r"พ\.?\s*ศ\.?|\bBE\b|B\.E\.", re.I)),
          ("ethiopian", re.compile(r"ዓ\.?\s*ም|\bEC\b|E\.C\.", re.I)))


def detect_calendar(text):
    """The calendar a date text says it is in (suffix, digits, month names), or None."""
    if text is None:
        return None
    s = str(text)
    for cal, rx in _MARKS:
        if rx.search(s):
            return cal
    if any("۰" <= c <= "۹" for c in s) or any(name in s for name in MONTHS["jalali"]):
        return "jalali"
    if any("๐" <= c <= "๙" for c in s) or any(name in s for name in MONTHS["buddhist"]):
        return "buddhist"
    if any("ሀ" <= c <= "፿" for c in s):
        return "ethiopian"
    if any("٠" <= c <= "٩" for c in s):
        return "hijri"
    return None


def guess_calendar(year):
    """A plain year number's likely calendar when nothing else says so."""
    if year >= 2400:
        return "buddhist"
    if 1800 <= year <= 2300:
        return "gregorian"
    if 1300 <= year <= 1500:
        return "hijri"
    if 1200 <= year < 1300:
        return "jalali"
    return None


def parse_date(text, calendar=None, day_first=True):
    """(calendar, (y, m, d)) from a date text in any of the calendars; calendar=None detects it.
    Year first or last; with the year last, day_first picks dd/mm/yyyy over mm/dd/yyyy."""
    if text is None:
        return None
    if isinstance(text, QDateTime):
        text = text.date()
    if isinstance(text, QDate):
        return ("gregorian", (text.year(), text.month(), text.day())) if text.isValid() else None
    if isinstance(text, datetime.datetime):
        text = text.date()
    if isinstance(text, datetime.date):
        return "gregorian", (text.year, text.month, text.day)
    s = latin_digits(str(text))
    cal = calendar or detect_calendar(str(text))
    names = [(c, i + 1, n) for c in ("jalali", "buddhist", "ethiopian", "hijri") for i, n in enumerate(MONTHS[c])]
    for c, mi, name in sorted(names, key=lambda t: -len(t[2])):
        if name in s and (cal in (None, c)):
            nums = [int(x) for x in re.findall(r"\d+", s)]
            if len(nums) >= 2:
                d, y = (nums[0], nums[1]) if nums[0] <= 31 else (nums[1], nums[0])
                return (calendar or c), (y, mi, d)
    m = _PARTS.search(s)
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    if len(m.group(1)) >= 3:
        y, mo, d = a, b, c
    elif len(m.group(3)) >= 3:
        y = c
        mo, d = (b, a) if day_first else (a, b)
        if mo > 12 and d <= 12:
            mo, d = d, mo
    else:
        return None
    cal = cal or guess_calendar(y)
    if cal is None:
        return None
    return cal, (y, mo, d)


def convert(value, target, source=None, pattern="yyyy/mm/dd", digits="latin", suffix=True, day_first=True):
    """Convert a date (QDate/date or text in any calendar) to `target`: returns (text or date, exact, source)
    where the result is a datetime.date for target 'gregorian' and text otherwise. None when it can't."""
    parsed = parse_date(value, source, day_first)
    if parsed is None:
        return None
    src, (y, m, d) = parsed
    try:
        g, exact = to_gregorian(src, y, m, d)
    except (ValueError, OverflowError):
        return None
    if target == "gregorian":
        return g, exact, src
    ymd, ex2 = from_gregorian(g, target)
    return format_date(target, ymd, pattern, digits, suffix=suffix), exact and ex2, src


# ---- expression functions ------------------------------------------------------------------------
FUNCTIONS = FunctionSet()


def _as_date(v):
    p = parse_date(v, None)
    if p is None:
        return None
    cal, (y, m, d) = p
    try:
        return to_gregorian(cal, y, m, d)[0]
    except (ValueError, OverflowError):
        return None


def _to(values, cal):
    args = list(values)
    d = _as_date(args[0] if args else None)
    if d is None:
        return None
    pattern = str(args[1]) if len(args) > 1 and args[1] else "yyyy/mm/dd"
    ymd, _exact = from_gregorian(d, cal)
    return format_date(cal, ymd, pattern)


def _from(value, cal):
    r = convert(value, "gregorian", cal)
    if r is None:
        return None
    g = r[0]
    return QDate(g.year, g.month, g.day)


@FUNCTIONS.function(args=-1)
def to_hijri(values, feature, parent):
    """<h3>to_hijri(date[, pattern])</h3>The Hijri (Umm al-Qura) date of a Gregorian date as text.
    pattern uses yyyy, mm and dd (default 'yyyy/mm/dd').<br>
    Example: <code>to_hijri(to_date('2024-03-11'))</code> → '1445/09/01 هـ'"""
    return _to(values, "hijri")


@FUNCTIONS.function()
def from_hijri(value, feature, parent):
    """<h3>from_hijri(text)</h3>The Gregorian date of a Hijri (Umm al-Qura) date written as text, in any
    common form ('١٤٤٥/٠٩/٠١ هـ', '1-9-1445', '1445/9/1H').<br>
    Example: <code>from_hijri('1445/9/1')</code> → 2024-03-11"""
    return _from(value, "hijri")


@FUNCTIONS.function(args=-1)
def to_jalali(values, feature, parent):
    """<h3>to_jalali(date[, pattern])</h3>The Solar Hijri (Jalali, Iranian) date of a Gregorian date as
    text; pattern uses yyyy, mm, dd and MMMM (month name).<br>
    Example: <code>to_jalali(to_date('2024-08-10'))</code> → '1403/05/20 ه‍.ش'"""
    return _to(values, "jalali")


@FUNCTIONS.function()
def from_jalali(value, feature, parent):
    """<h3>from_jalali(text)</h3>The Gregorian date of a Solar Hijri (Jalali) date written as text, with
    Persian or Latin digits ('۱۴۰۳/۰۵/۲۰', '1403-5-20').<br>
    Example: <code>from_jalali('1403/05/20')</code> → 2024-08-10"""
    return _from(value, "jalali")


@FUNCTIONS.function(args=-1)
def to_calendar(values, feature, parent):
    """<h3>to_calendar(date, calendar[, pattern])</h3>A date written in another calendar: 'hijri', 'jalali',
    'buddhist' (Thai) or 'ethiopian'.<br>
    Example: <code>to_calendar(to_date('2024-09-11'), 'ethiopian')</code> → '2017/01/01 ዓ.ም'"""
    args = list(values)
    if len(args) < 2 or str(args[1]) not in CALENDARS:
        return None
    return _to([args[0]] + args[2:], str(args[1]))


@FUNCTIONS.function(args=-1)
def from_calendar(values, feature, parent):
    """<h3>from_calendar(text, calendar)</h3>The Gregorian date of a date written in another calendar
    ('hijri', 'jalali', 'buddhist', 'ethiopian').<br>
    Example: <code>from_calendar('2567/01/15', 'buddhist')</code> → 2024-01-15"""
    args = list(values)
    if len(args) < 2 or str(args[1]) not in CALENDARS:
        return None
    return _from(args[0], str(args[1]))


def register_functions():
    FUNCTIONS.register()


def unregister_functions():
    FUNCTIONS.unregister()


def calendar_names():
    return [(c, tr(NAMES[c])) for c in CALENDARS]


_ = GROUP  # the functions are grouped with the label functions
