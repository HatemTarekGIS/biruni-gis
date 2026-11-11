"""Paper sizes and title blocks of the sheet standards used around the world.

A preset gives the page (ISO, ANSI or a national format), its margins (ABNT
NBR 10068 keeps 25 mm on the binding side), the title-block fields in the
language of the sheet, the scale-bar unit, the default grid style and the
countries that use it. ``for_country`` returns the presets a region offers
first; the map sheet builds the page from a preset id.
"""
from .i18n import tr

ISO = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420), "A4": (210, 297)}
ANSI = {"ANSI A (Letter)": (215.9, 279.4), "ANSI B (Tabloid)": (279.4, 431.8), "ANSI C": (431.8, 558.8),
        "ANSI D": (558.8, 863.6), "ANSI E": (863.6, 1117.6), "Legal": (215.9, 355.6),
        "Folio (8.5 × 13 in)": (215.9, 330.2)}

# title-block field labels per language (en, ar, pt, es, fr)
FIELDS = {
    "owner": {"en": "Owner", "ar": "المالك", "pt": "Proprietário", "es": "Propietario", "fr": "Propriétaire"},
    "client": {"en": "Client", "ar": "العميل", "pt": "Interessado", "es": "Solicitante", "fr": "Client"},
    "deed_no": {"en": "Deed no.", "ar": "رقم الصك", "pt": "Matrícula", "es": "Partida registral",
                "fr": "Référence de l'acte"},
    "parcel_no": {"en": "Parcel no.", "ar": "رقم القطعة", "pt": "Lote", "es": "Lote", "fr": "Parcelle"},
    "plan_no": {"en": "Plan no.", "ar": "رقم المخطط", "pt": "Loteamento", "es": "Plano", "fr": "Plan"},
    "district": {"en": "District", "ar": "الحي", "pt": "Bairro", "es": "Distrito", "fr": "Lieu-dit"},
    "city": {"en": "City", "ar": "المدينة", "pt": "Município", "es": "Provincia", "fr": "Commune"},
    "basin": {"en": "Basin (hod)", "ar": "الحوض", "pt": "Gleba", "es": "Sector", "fr": "Section"},
    "village": {"en": "Village", "ar": "الناحية", "pt": "Distrito", "es": "Localidad", "fr": "Village"},
    "markaz": {"en": "Markaz", "ar": "المركز", "pt": "Comarca", "es": "Municipio", "fr": "Canton"},
    "governorate": {"en": "Governorate", "ar": "المحافظة", "pt": "Estado", "es": "Departamento",
                    "fr": "Département"},
    "location": {"en": "Location", "ar": "الموقع", "pt": "Localização", "es": "Ubicación", "fr": "Situation"},
    "section": {"en": "Section", "ar": "القسم", "pt": "Setor", "es": "Sección", "fr": "Section"},
    "lot_no": {"en": "Lot no.", "ar": "رقم القطعة", "pt": "Lote", "es": "Lote", "fr": "Lot"},
    "survey_no": {"en": "Survey no.", "ar": "رقم الرفع", "pt": "Processo", "es": "Expediente",
                  "fr": "Dossier"},
    "plan_number": {"en": "Plan number", "ar": "رقم اللوحة", "pt": "Prancha", "es": "Lámina", "fr": "Planche"},
    "origin": {"en": "Origin / datum", "ar": "المرجع الجيوديسي", "pt": "Datum", "es": "Datum",
               "fr": "Système de référence"},
    "area": {"en": "Area", "ar": "المساحة", "pt": "Área", "es": "Área", "fr": "Contenance"},
    "perimeter": {"en": "Perimeter", "ar": "المحيط", "pt": "Perímetro", "es": "Perímetro", "fr": "Périmètre"},
    "scale": {"en": "Scale", "ar": "مقياس الرسم", "pt": "Escala", "es": "Escala", "fr": "Échelle"},
    "date": {"en": "Date", "ar": "التاريخ", "pt": "Data", "es": "Fecha", "fr": "Date"},
    "surveyor": {"en": "Surveyor", "ar": "المهندس المساح", "pt": "Responsável técnico", "es": "Profesional",
                 "fr": "Géomètre-expert"},
    "licence": {"en": "Licence no.", "ar": "رقم الترخيص", "pt": "CREA / CFT", "es": "Colegiatura (CIP)",
                "fr": "N° d'inscription"},
    "office": {"en": "Engineering office", "ar": "المكتب الهندسي", "pt": "Escritório", "es": "Oficina",
               "fr": "Cabinet"},
    "sheet": {"en": "Sheet", "ar": "اللوحة", "pt": "Folha", "es": "Hoja", "fr": "Feuille"},
}


class Preset:
    def __init__(self, pid, name, paper, landscape, margins, fields, countries=(), lang="en", unit="m",
                 grid="projected", corners=False, note=""):
        self.id = pid
        self.name = name                    # English; shown through tr()
        self.paper = paper                  # (width, height) in mm, portrait
        self.landscape = landscape
        self.margins = margins              # (left, top, right, bottom) mm
        self.fields = list(fields)          # title-block field keys
        self.countries = tuple(countries)   # ISO 3166 alpha-2
        self.lang = lang                    # language of the title block
        self.unit = unit                    # scale bar: "m" or "ft"
        self.grid = grid                    # map_grid style
        self.corners = corners              # corner coordinates written outside the map frame
        self.note = note

    def page_size(self):
        w, h = self.paper
        return (h, w) if self.landscape else (w, h)


_SURVEY = ["owner", "location", "area", "perimeter", "scale", "date", "surveyor", "licence"]
_M10 = (10, 10, 10, 10)


def _abnt_margins(size):
    # NBR 10068: 25 mm binding margin on the left; 10 mm (A0, A1) or 7 mm (A2-A4) elsewhere
    m = 10 if size in ("A0", "A1") else 7
    return (25, m, m, m)


PRESETS = {}


def _add(p):
    PRESETS[p.id] = p


for _size in ("A4", "A3", "A2", "A1", "A0"):
    for _land in (True, False):
        _add(Preset(f"iso_{_size.lower()}_{'landscape' if _land else 'portrait'}",
                    f"ISO {_size} {'landscape' if _land else 'portrait'}", ISO[_size], _land, _M10,
                    ["owner", "location", "scale", "date", "surveyor"]))
for _name, _dims in ANSI.items():
    _add(Preset("ansi_" + _name.split(" (")[0].lower().replace("ansi ", "").replace(" ", "_"), _name, _dims, True,
                (12.7, 12.7, 12.7, 12.7), ["owner", "location", "scale", "date", "surveyor"],
                countries=("US", "CA"), unit="ft"))
for _size in ("A4", "A3", "A2", "A1", "A0"):
    _add(Preset(f"abnt_{_size.lower()}", f"ABNT NBR 10068 {_size}", ISO[_size], _size != "A4", _abnt_margins(_size),
                ["owner", "deed_no", "location", "city", "area", "perimeter", "origin", "scale", "date",
                 "surveyor", "licence", "plan_number"],
                countries=("BR",), lang="pt", grid="both", corners=True,
                note="Brazilian technical drawing sheet (binding margin on the left)"))
_add(Preset("pe_perimetrico_a3", "Plano perimétrico A3 (Peru)", ISO["A3"], True, _M10,
            ["owner", "location", "district", "city", "area", "perimeter", "origin", "scale", "date", "surveyor",
             "licence", "plan_number"], countries=("PE", "BO", "EC"), lang="es", grid="both", corners=True,
            note="Perimeter plan with UTM coordinates table for SUNARP/COFOPRI"))
_add(Preset("sa_kroki_a4", "Kroki A4 (Saudi Arabia)", ISO["A4"], False, _M10,
            ["owner", "deed_no", "parcel_no", "plan_no", "district", "city", "area", "date", "office", "licence"],
            countries=("SA", "AE", "KW", "QA", "BH", "OM"), lang="ar", grid="projected", corners=True,
            note="Survey sketch for deed and permit filings"))
_add(Preset("eg_kroki_a4", "Kroki A4 (Egypt)", ISO["A4"], False, _M10,
            ["owner", "basin", "parcel_no", "village", "markaz", "governorate", "area", "date", "surveyor"],
            countries=("EG", "SD"), lang="ar", grid="projected", corners=True,
            note="Survey sketch for the Real Estate Registry"))
_add(Preset("ph_lot_plan", "Lot plan (Philippines, 8.5 × 13 in)", ANSI["Folio (8.5 × 13 in)"], False,
            (12.7, 12.7, 12.7, 12.7), ["owner", "lot_no", "survey_no", "location", "area", "origin", "scale",
                                       "date", "surveyor", "licence"],
            countries=("PH",), lang="en", grid="projected", corners=True,
            note="Lot plan with technical description and tie line to a BLLM"))
_add(Preset("ng_survey_plan_a3", "Survey plan A3 (Nigeria)", ISO["A3"], False, _M10,
            ["owner", "plan_number", "location", "area", "origin", "scale", "date", "surveyor", "licence"],
            countries=("NG", "GH", "KE", "UG", "TZ"), lang="en", grid="projected", corners=True,
            note="Beacon schedule and bearings/distances for C of O processing"))
_add(Preset("fr_bornage_a4", "Plan de bornage A4 (France)", ISO["A4"], False, _M10,
            ["owner", "city", "section", "parcel_no", "area", "origin", "scale", "date", "surveyor", "licence"],
            countries=("FR", "BE", "MA", "DZ", "TN", "SN", "CI", "CM"), lang="fr", grid="projected", corners=True,
            note="Boundary-marking plan (plan de bornage / procès-verbal)"))

_COUNTRY_ORDER = {
    "SA": ["sa_kroki_a4", "iso_a4_portrait", "iso_a3_landscape"],
    "EG": ["eg_kroki_a4", "iso_a4_portrait", "iso_a3_landscape"],
    "BR": ["abnt_a3", "abnt_a4", "abnt_a2", "abnt_a1"],
    "PE": ["pe_perimetrico_a3", "iso_a3_landscape", "iso_a4_portrait"],
    "PH": ["ph_lot_plan", "ansi_a", "iso_a4_portrait"],
    "NG": ["ng_survey_plan_a3", "iso_a3_portrait", "iso_a4_portrait"],
    "FR": ["fr_bornage_a4", "iso_a4_portrait", "iso_a3_landscape"],
    "US": ["ansi_a", "ansi_b", "ansi_d"],
}


def for_country(cc):
    """Preset ids a country gets first (ISO 3166 alpha-2), most useful first."""
    cc = (cc or "").upper()
    if cc in _COUNTRY_ORDER:
        return list(_COUNTRY_ORDER[cc])
    found = [p.id for p in PRESETS.values() if cc and cc in p.countries]
    return found + [i for i in ("iso_a4_landscape", "iso_a3_landscape", "iso_a4_portrait") if i not in found]


def get(pid):
    return PRESETS.get(pid)


def field_label(key, lang):
    labels = FIELDS.get(key, {})
    return labels.get((lang or "en").split("_")[0], labels.get("en", key))


def title_rows(preset, values, lang=None):
    """[(label, value)] of a preset's title block in its language; missing values stay blank."""
    lang = lang or preset.lang
    return [(field_label(k, lang), str(values.get(k, "") or "")) for k in preset.fields]


def preset_label(pid):
    """Name shown in menus (national standards keep their own names)."""
    p = PRESETS.get(pid)
    if p is None:
        return pid
    if pid.startswith("iso_"):
        size = pid.split("_")[1].upper()
        return (tr("ISO {s} landscape") if p.landscape else tr("ISO {s} portrait")).format(s=size)
    return p.name
