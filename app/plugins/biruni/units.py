"""Area and length units of land records around the world, with exact factors.

Pure Python (no qgis). Every unit is stored by its size in square metres (area) or
metres (length). Units that differ by region have one id per variant
(``marla`` = 272.25 sq ft, ``marla_225``, ``marla_250`` …) so a choice is never
silently wrong.

    convert(5, "kanal", "m2")                   -> 2529.3...
    area_text(2783.0, ["kanal", "marla"], "en")   -> '5 kanal 1.15 marla'
    for_country("PK")                             -> ['kanal', 'marla']
"""
from dataclasses import dataclass, field

FT = 0.3048                       # international foot
US_FT = 1200 / 3937               # US survey foot
SQFT = FT * FT
GOLD_COAST_FT = 0.3047997101815088
CAPE_FT = 0.3148581
CHAIN = 66 * FT                   # Gunter's chain, 20.1168 m
QASABA = 3.55                     # Egyptian qasaba (feddan = 333 1/3 square qasaba)
FEDDAN = QASABA * QASABA * 1000 / 3


@dataclass(frozen=True)
class Unit:
    id: str
    kind: str                     # "area" | "length"
    size: float                   # m² or m
    names: dict = field(default_factory=dict)   # {lang: name}
    note: str = ""

    def name(self, lang="en"):
        lang = (lang or "en").lower()
        return (self.names.get(lang) or self.names.get(lang.split("-")[0]) or self.names.get("en") or self.id)


def _u(uid, kind, size, note="", **names):
    return Unit(uid, kind, size, {k.replace("_", "-"): v for k, v in names.items()}, note)


_ALL = [
    # ---- area: metric ----
    _u("m2", "area", 1.0, en="m²", ar="م²", fr="m²", es="m²", pt="m²", tr="m²", fa="متر مربع", ur="مربع میٹر",
       id="m²"),
    _u("are", "area", 100.0, en="are", fr="a", es="a", pt="a", ar="آر"),
    _u("ha", "area", 10_000.0, en="ha", ar="هكتار", fr="ha", es="ha", pt="ha", tr="ha", fa="هکتار", ur="ہیکٹر",
       id="ha"),
    _u("km2", "area", 1_000_000.0, en="km²", ar="كم²"),
    # ---- Egypt ----
    _u("feddan", "area", FEDDAN, "333 1/3 square qasaba (3.55 m)", en="feddan", ar="فدان"),
    _u("qirat", "area", FEDDAN / 24, "1/24 feddan", en="qirat", ar="قيراط"),
    _u("sahm", "area", FEDDAN / 24 / 24, "1/24 qirat", en="sahm", ar="سهم"),
    # ---- dunam ----
    _u("dunam", "area", 1000.0, "metric dunam (Jordan, Palestine, Israel, Turkey dönüm)", en="dunam", ar="دونم",
       tr="dönüm", he="דונם"),
    _u("dunam_iq", "area", 2500.0, "Iraqi dunam / mesharra", en="dunam (Iraq)", ar="دونم عراقي"),
    _u("dunam_ottoman", "area", 919.3, "old Ottoman dönüm (40 × 40 standard paces)", en="dunam (Ottoman)",
       ar="دونم عثماني", tr="eski dönüm"),
    # ---- South Asia ----
    _u("marla", "area", 272.25 * SQFT, "272.25 sq ft (revenue / Punjab)", en="marla", ur="مرلہ", ar="مرلة"),
    _u("marla_250", "area", 250 * SQFT, "250 sq ft", en="marla (250 sq ft)", ur="مرلہ"),
    _u("marla_225", "area", 225 * SQFT, "225 sq ft (Lahore, some housing schemes)", en="marla (225 sq ft)",
       ur="مرلہ"),
    _u("kanal", "area", 20 * 272.25 * SQFT, "20 marla of 272.25 sq ft", en="kanal", ur="کنال", ar="كنال"),
    _u("kanal_250", "area", 20 * 250 * SQFT, "20 marla of 250 sq ft", en="kanal (5000 sq ft)", ur="کنال"),
    _u("kanal_225", "area", 20 * 225 * SQFT, "20 marla of 225 sq ft", en="kanal (4500 sq ft)", ur="کنال"),
    _u("bigha", "area", 14_400 * SQFT, "Bangladesh / West Bengal / Assam: 14,400 sq ft", en="bigha", bn="বিঘা",
       hi="बीघा"),
    _u("bigha_rj", "area", 27_225 * SQFT, "Rajasthan pucca bigha: 27,225 sq ft", en="bigha (Rajasthan)",
       hi="बीघा"),
    _u("katha", "area", 720 * SQFT, "Bangladesh / West Bengal: 720 sq ft (1/20 bigha)", en="katha", bn="কাঠা",
       hi="कट्ठा"),
    _u("decimal", "area", 435.6 * SQFT, "1/100 acre", en="decimal", bn="শতাংশ"),
    _u("guntha", "area", 1089 * SQFT, "1/40 acre", en="guntha", hi="गुंठा"),
    _u("acre", "area", 4840 * 0.9144 ** 2, en="acre", ar="أكر", fr="acre", es="acre", ur="ایکڑ", bn="একর",
       hi="एकड़"),
    _u("sqft", "area", SQFT, en="sq ft", ar="قدم²", ur="مربع فٹ"),
    _u("sqft_us", "area", US_FT * US_FT, en="sq ft (US survey)"),
    _u("sqyd", "area", 0.9144 ** 2, en="sq yd", ur="مربع گز"),
    # ---- East / South-East Asia ----
    _u("pyeong", "area", 400 / 121, "Korea: 400/121 m²", en="pyeong", ko="평"),
    _u("tsubo", "area", 400 / 121, "Japan: 400/121 m²", en="tsubo", ja="坪"),
    _u("rai", "area", 1600.0, en="rai", th="ไร่"),
    _u("ngan", "area", 400.0, en="ngan", th="งาน"),
    _u("sqwa", "area", 4.0, en="square wa", th="ตารางวา"),
    _u("mu", "area", 2000 / 3, en="mu", zh="亩"),
    # ---- Latin America ----
    _u("alqueire_sp", "area", 24_200.0, "alqueire paulista", en="alqueire (paulista)", pt="alqueire paulista"),
    _u("alqueire_mg", "area", 48_400.0, "alqueire mineiro / geométrico / goiano", en="alqueire (mineiro)",
       pt="alqueire mineiro"),
    _u("alqueire_norte", "area", 27_225.0, "alqueire do norte", en="alqueire (norte)", pt="alqueire do norte"),
    _u("alqueire_ba", "area", 96_800.0, "alqueire baiano", en="alqueire (baiano)", pt="alqueire baiano"),
    _u("manzana", "area", 6987.37, "Guatemala / El Salvador / Honduras (Nicaragua ≈ 7,026)", en="manzana",
       es="manzana"),
    _u("cuerda", "area", 3930.395625, "Puerto Rico", en="cuerda", es="cuerda"),
    # ---- length ----
    _u("m", "length", 1.0, en="m", ar="م", fr="m", es="m", pt="m", tr="m", fa="متر", ur="میٹر", id="m"),
    _u("km", "length", 1000.0, en="km", ar="كم"),
    _u("ft", "length", FT, en="ft", ar="قدم", ur="فٹ"),
    _u("ft_us", "length", US_FT, en="US survey ft"),
    _u("ft_gold_coast", "length", GOLD_COAST_FT, "Ghana (Gold Coast foot)", en="Gold Coast ft"),
    _u("ft_cape", "length", CAPE_FT, "South Africa (Cape foot, old SG diagrams)", en="Cape ft"),
    _u("yd", "length", 0.9144, en="yd", ur="گز"),
    _u("chain", "length", CHAIN, "Gunter's chain (66 ft)", en="chain"),
    _u("link", "length", CHAIN / 100, en="link"),
    _u("rod", "length", CHAIN / 4, "rod / pole / perch (16.5 ft)", en="rod"),
    _u("pole", "length", CHAIN / 4, en="pole"),
    _u("perch", "length", CHAIN / 4, en="perch"),
    _u("vara", "length", 0.835905, "Castilian vara", en="vara", es="vara"),
    _u("vara_tx", "length", 0.8466667, "Texas / Mexico vara", en="vara (Texas)", es="vara"),
    _u("qasaba", "length", QASABA, "Egyptian qasaba", en="qasaba", ar="قصبة"),
]
UNITS = {u.id: u for u in _ALL}

# Units used on deeds by default, per ISO country code (largest first; a list is written as "2 kanal 5 marla")
_COUNTRY_AREA = {
    "EG": ["feddan", "qirat", "sahm"], "SD": ["feddan"],
    "PK": ["kanal", "marla"], "IN": ["acre", "guntha"], "BD": ["bigha", "katha"], "NP": ["bigha"],
    "IQ": ["dunam_iq"], "JO": ["dunam"], "PS": ["dunam"], "IL": ["dunam"], "LB": ["m2"], "SY": ["m2"],
    "KR": ["pyeong"], "JP": ["tsubo"], "TH": ["rai", "ngan", "sqwa"], "CN": ["mu"], "TW": ["m2"],
    "US": ["acre"], "GB": ["acre"], "LR": ["acre"],
    "GT": ["manzana"], "SV": ["manzana"], "HN": ["manzana"], "NI": ["manzana"], "PR": ["cuerda"],
    "BR": ["ha"], "AR": ["ha"], "UY": ["ha"], "PY": ["ha"], "CO": ["ha"], "MX": ["ha"],
    "FR": ["ha", "are", "m2"], "BE": ["ha", "are", "m2"],
}
_COUNTRY_LENGTH = {"US": "ft_us", "LR": "ft", "GH": "m", "ZA": "m"}


def get(uid):
    try:
        return UNITS[uid]
    except KeyError:
        raise ValueError(f"unknown unit: {uid}") from None


def convert(value, from_unit, to_unit):
    a, b = get(from_unit), get(to_unit)
    if a.kind != b.kind:
        raise ValueError(f"cannot convert {a.kind} to {b.kind}")
    return value * a.size / b.size


def for_country(cc):
    """Default area units for deeds in a country (ISO 3166 alpha-2); m² when nothing special is used."""
    return list(_COUNTRY_AREA.get((cc or "").upper(), ["m2"]))


def length_for_country(cc):
    return _COUNTRY_LENGTH.get((cc or "").upper(), "m")


def area_text(m2, units=None, lang="en", decimals=2):
    """An area in mixed units, largest first: '2 kanal 5.25 marla', '1 rai 2 ngan 30.5 square wa'.
    The last unit carries the remainder with ``decimals`` decimals; zero leading parts are left out."""
    units = units or ["m2"]
    us = sorted((get(u) for u in units), key=lambda u: -u.size)
    rest, parts = float(m2), []
    for i, u in enumerate(us):
        last = i == len(us) - 1
        if last:
            v = round(rest / u.size, decimals)
            txt = f"{v:,.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{v:,.0f}"
            if v or not parts:
                parts.append(f"{txt} {u.name(lang)}")
        else:
            n = int((rest + 1e-9) // u.size)
            rest -= n * u.size
            if n or parts:
                parts.append(f"{n:,} {u.name(lang)}")
    return " ".join(parts)


def length_text(m, unit="m", lang="en", decimals=2):
    u = get(unit)
    return f"{m / u.size:,.{decimals}f} {u.name(lang)}"
