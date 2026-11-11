"""Country table of the region registry: what a user in each country needs Biruni to know.

Languages, units, calendars, bearings and CRS catalogues come from their own engines (units.py, calendars.py,
deed_languages.py, regional_crs.py …); this table only holds what no engine owns: names, the national
mapping/cadastre agency, typical EPSG codes to fall back on, the official cadastre connectors and the extra
tools that make sense there. Codes are ISO 3166-1 alpha-2."""


class Country:
    __slots__ = ("cc", "en", "ar", "native", "ui", "crs", "cadastre", "agency", "link", "tools")

    def __init__(self, cc, en, ar, native, ui, crs=(), cadastre=(), agency="", link="", tools=()):
        self.cc, self.en, self.ar, self.native, self.ui = cc, en, ar, native, ui
        self.crs, self.cadastre, self.agency, self.link, self.tools = tuple(crs), tuple(cadastre), agency, link, \
            tuple(tools)


_SA_TOOLS = ("biruni:parcelbundle", "biruni:linestoplots", "biruni:setbackcheck")
_ARAB = ("biruni:arabictextdoctor",)

_ROWS = [
    # ---- Arab world -------------------------------------------------------------------------------------
    ("SA", "Saudi Arabia", "السعودية", "السعودية", "ar", ("EPSG:9356", "EPSG:9357", "EPSG:9358", "EPSG:20437",
                                                         "EPSG:20438"), (), "General Authority for Survey and "
     "Geospatial Information (GEOSA) · Real Estate General Authority (REGA)", "https://www.geosa.gov.sa",
     _SA_TOOLS + _ARAB),
    ("EG", "Egypt", "مصر", "مصر", "ar", ("EPSG:22992", "EPSG:22993", "EPSG:22994", "EPSG:32636"), (),
     "Egyptian Survey Authority (ESA)", "https://www.esa.gov.eg", ("biruni:linestoplots",) + _ARAB),
    ("AE", "United Arab Emirates", "الإمارات", "الإمارات", "ar", ("EPSG:32640", "EPSG:3997"), (),
     "Dubai Land Department · Abu Dhabi Municipality", "https://dubailand.gov.ae", _ARAB),
    ("KW", "Kuwait", "الكويت", "الكويت", "ar", ("EPSG:32639", "EPSG:20439"), (), "Kuwait Municipality", "", _ARAB),
    ("QA", "Qatar", "قطر", "قطر", "ar", ("EPSG:2932", "EPSG:32639"), (), "Ministry of Municipality", "", _ARAB),
    ("BH", "Bahrain", "البحرين", "البحرين", "ar", ("EPSG:20439", "EPSG:32639"), (), "Survey and Land Registration "
     "Bureau", "https://www.slrb.gov.bh", _ARAB),
    ("OM", "Oman", "عُمان", "عُمان", "ar", ("EPSG:32640", "EPSG:32639"), (), "National Survey Authority", "", _ARAB),
    ("YE", "Yemen", "اليمن", "اليمن", "ar", ("EPSG:32638", "EPSG:32639"), (), "", "", _ARAB),
    ("JO", "Jordan", "الأردن", "الأردن", "ar", ("EPSG:3066", "EPSG:32636"), (), "Department of Lands and Survey",
     "https://dls.gov.jo", _ARAB),
    ("PS", "Palestine", "فلسطين", "فلسطين", "ar", ("EPSG:28193", "EPSG:2039"), (), "", "", _ARAB),
    ("LB", "Lebanon", "لبنان", "لبنان", "ar", ("EPSG:22770", "EPSG:32636"), (), "", "", _ARAB),
    ("SY", "Syria", "سوريا", "سوريا", "ar", ("EPSG:22770", "EPSG:32637"), (), "", "", _ARAB),
    ("IQ", "Iraq", "العراق", "العراق", "ar", ("EPSG:3893", "EPSG:32638"), (), "", "", _ARAB),
    ("LY", "Libya", "ليبيا", "ليبيا", "ar", ("EPSG:2068", "EPSG:32633"), (), "", "", _ARAB),
    ("SD", "Sudan", "السودان", "السودان", "ar", ("EPSG:20136", "EPSG:32636"), (), "", "", _ARAB),
    ("MA", "Morocco", "المغرب", "المغرب", "ar", ("EPSG:26191", "EPSG:26192", "EPSG:32629"), (),
     "ANCFCC", "https://www.ancfcc.gov.ma", _ARAB),
    ("DZ", "Algeria", "الجزائر", "الجزائر", "ar", ("EPSG:30791", "EPSG:30792", "EPSG:32631"), (), "INCT", "", _ARAB),
    ("TN", "Tunisia", "تونس", "تونس", "ar", ("EPSG:22391", "EPSG:22392", "EPSG:32632"), (), "OTC", "", _ARAB),
    ("MR", "Mauritania", "موريتانيا", "موريتانيا", "ar", ("EPSG:32628",), (), "", "", _ARAB),
    # ---- Middle East and Asia -----------------------------------------------------------------------------
    ("IR", "Iran", "إيران", "ایران", "fa", ("EPSG:32638", "EPSG:32639", "EPSG:32640", "EPSG:32641"), (),
     "National Cartographic Center", "https://www.ncc.gov.ir", _ARAB),
    ("AF", "Afghanistan", "أفغانستان", "افغانستان", "fa", ("EPSG:32641", "EPSG:32642"), (), "", "", _ARAB),
    ("PK", "Pakistan", "باكستان", "پاکستان", "ur", ("EPSG:24312", "EPSG:24313", "EPSG:32642", "EPSG:32643"), (),
     "Survey of Pakistan · Punjab Land Records Authority", "https://www.punjab-zameen.gov.pk", _ARAB),
    ("IN", "India", "الهند", "भारत", "hi", ("EPSG:7755", "EPSG:32643", "EPSG:32644"), (), "Survey of India",
     "https://surveyofindia.gov.in", ()),
    ("BD", "Bangladesh", "بنغلاديش", "বাংলাদেশ", "bn", ("EPSG:9678", "EPSG:3106"), (), "Department of Land Records "
     "and Surveys", "https://dlrs.gov.bd", ()),
    ("NP", "Nepal", "نيبال", "नेपाल", "ne", ("EPSG:6207", "EPSG:32645"), (), "", "", ()),
    ("TR", "Türkiye", "تركيا", "Türkiye", "tr", ("EPSG:5253", "EPSG:5254", "EPSG:5255", "EPSG:2320"), (),
     "Tapu ve Kadastro Genel Müdürlüğü (TKGM)", "https://parselsorgu.tkgm.gov.tr", ()),
    ("IL", "Israel", "إسرائيل", "ישראל", "he", ("EPSG:2039", "EPSG:28193"), (), "Survey of Israel", "", ()),
    ("AZ", "Azerbaijan", "أذربيجان", "Azərbaycan", "az", ("EPSG:32638", "EPSG:32639"), (), "", "", ()),
    ("KZ", "Kazakhstan", "كازاخستان", "Қазақстан", "kk", ("EPSG:32642", "EPSG:28412"), (), "", "", ()),
    ("RU", "Russia", "روسيا", "Россия", "ru", ("EPSG:28407", "EPSG:20007", "EPSG:7683"), (), "Rosreestr",
     "https://nspd.gov.ru", ()),
    ("CN", "China", "الصين", "中国", "zh", ("EPSG:4490", "EPSG:4549", "EPSG:4527"), (), "", "", ()),
    ("JP", "Japan", "اليابان", "日本", "ja", ("EPSG:6669", "EPSG:6677", "EPSG:6668"), (), "Geospatial Information "
     "Authority of Japan (GSI)", "https://www.gsi.go.jp", ()),
    ("KR", "South Korea", "كوريا الجنوبية", "대한민국", "ko", ("EPSG:5186", "EPSG:5174", "EPSG:5179"), (),
     "NGII · V-World", "https://www.vworld.kr", ()),
    ("TW", "Taiwan", "تايوان", "臺灣", "zh", ("EPSG:3826",), (), "", "", ()),
    ("VN", "Vietnam", "فيتنام", "Việt Nam", "vi", ("EPSG:3405", "EPSG:9208"), (), "", "", ()),
    ("TH", "Thailand", "تايلاند", "ประเทศไทย", "th", ("EPSG:24047", "EPSG:24048", "EPSG:32647"), (),
     "Department of Lands", "https://landsmaps.dol.go.th", ()),
    ("MY", "Malaysia", "ماليزيا", "Malaysia", "ms", ("EPSG:3375", "EPSG:3376"), (), "JUPEM", "", ()),
    ("ID", "Indonesia", "إندونيسيا", "Indonesia", "id", ("EPSG:23830", "EPSG:9470", "EPSG:32749"), (),
     "Badan Pertanahan Nasional (BPN) · BIG", "https://bhumi.atrbpn.go.id", ()),
    ("PH", "Philippines", "الفلبين", "Pilipinas", "en", ("EPSG:3121", "EPSG:3123", "EPSG:32651"), (),
     "NAMRIA · Land Management Bureau", "https://www.namria.gov.ph", ()),
    # ---- Europe -------------------------------------------------------------------------------------------
    ("GB", "United Kingdom", "المملكة المتحدة", "United Kingdom", "en", ("EPSG:27700",), (), "Ordnance Survey · HM "
     "Land Registry", "https://www.gov.uk/search-property-information-land-registry", ()),
    ("IE", "Ireland", "أيرلندا", "Éire", "en", ("EPSG:2157", "EPSG:29902"), (), "Tailte Éireann", "", ()),
    ("FR", "France", "فرنسا", "France", "fr", ("EPSG:2154", "EPSG:3942", "EPSG:3949"), ("fr_apicarto",),
     "IGN · DGFiP (cadastre)", "https://cadastre.data.gouv.fr", ()),
    ("BE", "Belgium", "بلجيكا", "België / Belgique", "fr", ("EPSG:31370", "EPSG:3812"), (), "", "", ()),
    ("NL", "Netherlands", "هولندا", "Nederland", "nl", ("EPSG:28992",), ("nl_pdok",), "Kadaster (PDOK)",
     "https://www.pdok.nl", ()),
    ("DE", "Germany", "ألمانيا", "Deutschland", "de", ("EPSG:25832", "EPSG:25833", "EPSG:31467"), (),
     "Landesvermessungsämter (ALKIS)", "https://www.adv-online.de", ()),
    ("AT", "Austria", "النمسا", "Österreich", "de", ("EPSG:31256", "EPSG:31255", "EPSG:31287"), (), "BEV",
     "https://www.bev.gv.at", ()),
    ("CH", "Switzerland", "سويسرا", "Schweiz / Suisse", "de", ("EPSG:2056",), (), "swisstopo",
     "https://www.swisstopo.admin.ch", ()),
    ("IT", "Italy", "إيطاليا", "Italia", "it", ("EPSG:6706", "EPSG:7791", "EPSG:3003"), ("it_ade",),
     "Agenzia delle Entrate (catasto)", "https://geoportale.cartografia.agenziaentrate.gov.it", ()),
    ("ES", "Spain", "إسبانيا", "España", "es", ("EPSG:25830", "EPSG:25829", "EPSG:25831"), ("es_catastro",),
     "Dirección General del Catastro", "https://www.sedecatastro.gob.es", ()),
    ("PT", "Portugal", "البرتغال", "Portugal", "pt", ("EPSG:3763",), (), "DGT", "", ()),
    ("PL", "Poland", "بولندا", "Polska", "pl", ("EPSG:2180", "EPSG:2176", "EPSG:2177", "EPSG:2178", "EPSG:2179"),
     ("pl_uldk",), "GUGiK", "https://uldk.gugik.gov.pl", ()),
    ("CZ", "Czechia", "التشيك", "Česko", "cs", ("EPSG:5514",), (), "ČÚZK", "https://cuzk.cz", ()),
    ("GR", "Greece", "اليونان", "Ελλάδα", "el", ("EPSG:2100",), (), "Hellenic Cadastre", "", ()),
    ("SE", "Sweden", "السويد", "Sverige", "sv", ("EPSG:3006",), (), "Lantmäteriet", "https://www.lantmateriet.se",
     ()),
    ("NO", "Norway", "النرويج", "Norge", "nb", ("EPSG:25832", "EPSG:25833", "EPSG:25835"), (), "Kartverket",
     "https://www.kartverket.no", ()),
    ("DK", "Denmark", "الدنمارك", "Danmark", "da", ("EPSG:25832",), (), "Klimadatastyrelsen", "", ()),
    ("FI", "Finland", "فنلندا", "Suomi", "fi", ("EPSG:3067", "EPSG:2393"), (), "Maanmittauslaitos", "", ()),
    ("UA", "Ukraine", "أوكرانيا", "Україна", "uk", ("EPSG:5562", "EPSG:5563", "EPSG:28406"), (),
     "StateGeoCadastre", "https://e.land.gov.ua", ()),
    # ---- Africa -------------------------------------------------------------------------------------------
    ("NG", "Nigeria", "نيجيريا", "Nigeria", "en", ("EPSG:26391", "EPSG:26392", "EPSG:26393", "EPSG:26331"), (),
     "Office of the Surveyor-General of the Federation", "", ()),
    ("GH", "Ghana", "غانا", "Ghana", "en", ("EPSG:2136", "EPSG:2137", "EPSG:32630"), (), "Lands Commission",
     "https://lc.gov.gh", ()),
    ("KE", "Kenya", "كينيا", "Kenya", "sw", ("EPSG:21037", "EPSG:21036", "EPSG:32737"), (), "Ardhisasa",
     "https://ardhisasa.lands.go.ke", ()),
    ("TZ", "Tanzania", "تنزانيا", "Tanzania", "sw", ("EPSG:21037", "EPSG:21036", "EPSG:32737"), (), "", "", ()),
    ("UG", "Uganda", "أوغندا", "Uganda", "en", ("EPSG:21036", "EPSG:32636"), (), "", "", ()),
    ("RW", "Rwanda", "رواندا", "Rwanda", "rw", ("EPSG:32735", "EPSG:32736"), (), "", "", ()),
    ("ET", "Ethiopia", "إثيوبيا", "ኢትዮጵያ", "am", ("EPSG:20137", "EPSG:20138", "EPSG:32637"), (), "", "", ()),
    ("SN", "Senegal", "السنغال", "Sénégal", "fr", ("EPSG:31028", "EPSG:32628"), (), "", "", ()),
    ("CI", "Côte d'Ivoire", "ساحل العاج", "Côte d'Ivoire", "fr", ("EPSG:2040", "EPSG:2041", "EPSG:32630"), (), "",
     "", ()),
    ("CM", "Cameroon", "الكاميرون", "Cameroun", "fr", ("EPSG:2215", "EPSG:32632"), (), "", "", ()),
    ("ZA", "South Africa", "جنوب أفريقيا", "South Africa", "en", ("EPSG:2048", "EPSG:2049", "EPSG:2050",
                                                                    "EPSG:2051", "EPSG:2052", "EPSG:2053"), (),
     "Chief Surveyor-General (CSG)", "https://csg.drdlr.gov.za", ()),
    # ---- Americas and Oceania -----------------------------------------------------------------------------
    ("US", "United States", "الولايات المتحدة", "United States", "en", ("EPSG:6318", "EPSG:26917"), (), "", "", ()),
    ("CA", "Canada", "كندا", "Canada", "en", ("EPSG:3978", "EPSG:2958"), (), "", "", ()),
    ("MX", "Mexico", "المكسيك", "México", "es", ("EPSG:6372", "EPSG:32614"), (), "INEGI · RAN",
     "https://www.inegi.org.mx", ()),
    ("GT", "Guatemala", "غواتيمالا", "Guatemala", "es", ("EPSG:32615",), (), "", "", ()),
    ("CO", "Colombia", "كولومبيا", "Colombia", "es", ("EPSG:9377",), (), "IGAC", "https://www.igac.gov.co", ()),
    ("PE", "Peru", "بيرو", "Perú", "es", ("EPSG:32718", "EPSG:32717", "EPSG:24878"), (), "SUNARP · INGEMMET", "", ()),
    ("CL", "Chile", "تشيلي", "Chile", "es", ("EPSG:32719", "EPSG:5361"), (), "", "", ()),
    ("AR", "Argentina", "الأرجنتين", "Argentina", "es", ("EPSG:5343", "EPSG:5347", "EPSG:22195"), (), "", "", ()),
    ("BR", "Brazil", "البرازيل", "Brasil", "pt", ("EPSG:31983", "EPSG:31982", "EPSG:4674"), (), "INCRA (SIGEF) · "
     "IBGE", "https://sigef.incra.gov.br", ()),
    ("AU", "Australia", "أستراليا", "Australia", "en", ("EPSG:7856", "EPSG:7855", "EPSG:7844"), (), "", "", ()),
    ("NZ", "New Zealand", "نيوزيلندا", "Aotearoa New Zealand", "en", ("EPSG:2193",), (), "LINZ",
     "https://www.linz.govt.nz", ()),
]

COUNTRIES = {r[0]: Country(*r) for r in _ROWS}

# Tools announced for a country but still being built; the registry hides them until they are registered.
PENDING_TOOLS = {}

# National file formats of a country (formats/ package): read or write them from "My country".
NATIONAL_TOOLS = {
    "BR": ("biruni:exportnationalformat",),                               # SIGEF ODS
    "ES": ("biruni:exportnationalformat",),                               # Catastro INSPIRE GML (IVG)
    "TR": ("biruni:opennationalformat",),                                 # Netcad NCZ
    "RU": ("biruni:opennationalformat",),                                 # Panorama SXF, Rosreestr XML
    "UA": ("biruni:opennationalformat",),                                 # cadastral IN4/XML
    "IT": ("biruni:opennationalformat",),                                 # catasto CXF/SUP
    "JP": ("biruni:opennationalformat", "biruni:exportnationalformat"),   # SIMA
}
