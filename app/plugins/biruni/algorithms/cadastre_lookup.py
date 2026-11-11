"""Find a parcel in an official, public cadastre service - by its reference or by clicking a point - and bring
its boundary into the project.

Only open services are used (no logins, no scraping): France (IGN API Carto), the Netherlands (Kadaster via
PDOK), Spain (Dirección General del Catastro, INSPIRE), Italy (Agenzia delle Entrate, INSPIRE WFS) and Poland
(GUGiK ULDK). Other countries are listed with the reason they are not connected."""
import json
import re
import ssl
import urllib.parse
import urllib.request
import uuid

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField,
                       QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterPoint, QgsProcessingParameterString,
                       QgsProject)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

TIMEOUT = 30
USER_AGENT = "BiruniGIS (free desktop GIS; https://github.com/HatemTarekGIS/biruni-gis)"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


class CadastreError(Exception):
    """A problem the user should read (wrong reference, service down …)."""


def fetch(url, timeout=TIMEOUT):
    """GET a URL and return the body (bytes); raises CadastreError with a readable message."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001 - every network failure becomes one readable message
        host = urllib.parse.urlparse(url).netloc
        raise CadastreError(tr("The cadastre service {h} did not answer ({e}). Check the internet connection or try "
                              "again later.").format(h=host, e=e)) from e


def _g(get):
    """The fetch function to use: the given one (tests) or the module's (looked up at call time)."""
    return get or fetch


class Parcel:
    def __init__(self, geometry, crs, reference, label="", official_area=None, attributes=None):
        self.geometry, self.crs, self.reference, self.label = geometry, crs, reference, label
        self.official_area, self.attributes = official_area, attributes or {}


# ---- parsing (pure functions, tested offline) ----------------------------------------------------------------
def parse_geojson(data, crs="EPSG:4326"):
    """[(QgsGeometry, properties)] of a GeoJSON FeatureCollection (bytes, str or dict)."""
    if isinstance(data, (bytes, str)):
        data = json.loads(data)
    out = []
    for f in data.get("features") or []:
        g = f.get("geometry")
        if not g:
            continue
        geom = QgsGeometry.fromWkt(_geojson_wkt(g))
        if geom.isNull():
            continue
        out.append((geom, f.get("properties") or {}))
    return out


def _ring(coords):
    return "(" + ",".join(f"{p[0]} {p[1]}" for p in coords) + ")"


def _geojson_wkt(g):
    t, c = g["type"], g["coordinates"]
    if t == "Polygon":
        return "POLYGON(" + ",".join(_ring(r) for r in c) + ")"
    if t == "MultiPolygon":
        return "MULTIPOLYGON(" + ",".join("(" + ",".join(_ring(r) for r in poly) + ")" for poly in c) + ")"
    if t == "Point":
        return f"POINT({c[0]} {c[1]})"
    raise ValueError(t)


def parse_gml(data):
    """[(QgsGeometry, attributes)] and the CRS authid of a WFS GML answer, read with OGR (no schema download)."""
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    path = f"/vsimem/biruni_cadastre_{uuid.uuid4().hex}.gml"
    gdal.FileFromMemBuffer(path, data if isinstance(data, bytes) else data.encode("utf-8"))
    out, authid = [], ""
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR, open_options=["DOWNLOAD_SCHEMA=NO", "SKIP_RESOLVE_ELEMS=ALL"])
        if ds is None:
            return out, authid
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(i)
            srs = lyr.GetSpatialRef()
            if srs is not None and not authid:
                srs.AutoIdentifyEPSG()
                code = srs.GetAuthorityCode(None)
                authid = f"EPSG:{code}" if code else ""
            defn = lyr.GetLayerDefn()
            for f in lyr:
                g = f.GetGeometryRef()
                if g is None:
                    continue
                geom = QgsGeometry()
                geom.fromWkb(bytes(g.ExportToIsoWkb()))
                attrs = {defn.GetFieldDefn(k).GetName(): f.GetField(k) for k in range(defn.GetFieldCount())}
                out.append((geom, attrs))
        ds = None  # noqa: F841 - closes the dataset before the memory file goes
    finally:
        gdal.Unlink(path)
    return out, authid


def parse_uldk(text):
    """[(QgsGeometry, attributes)] of a GUGiK ULDK answer: first line the status ('0' = found), then one line per
    parcel 'SRID=n;WKT|teryt|voivodeship|county|commune|region|parcel'."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("0"):
        return []
    names = ("teryt", "voivodeship", "county", "commune", "region", "parcel")
    out = []
    for ln in lines[1:]:
        parts = ln.split("|")
        wkt = parts[0].split(";", 1)[-1]
        geom = QgsGeometry.fromWkt(wkt)
        if geom.isNull():
            continue
        out.append((_lonlat(geom, (14, 24.5), (49, 55)), dict(zip(names, parts[1:]))))
    return out


def _lonlat(geom, lon_range, lat_range):
    """Swap x/y when a geographic geometry came as lat/lon (some services write the EPSG axis order)."""
    c = geom.centroid().asPoint()
    if lat_range[0] <= c.x() <= lat_range[1] and lon_range[0] <= c.y() <= lon_range[1] and not (
            lon_range[0] <= c.x() <= lon_range[1]):
        g = QgsGeometry(geom)
        g.get().swapXy()
        return g
    return geom


def parse_catastro_rc(data):
    """The 14-character referencia catastral of a Catastro coordinates answer ('' when none)."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    pc1 = re.search(r"<pc1>([^<]+)</pc1>", text)
    pc2 = re.search(r"<pc2>([^<]+)</pc2>", text)
    return (pc1.group(1) + pc2.group(1)).strip() if pc1 and pc2 else ""


# ---- connectors ---------------------------------------------------------------------------------------------
class Connector:
    id = cc = ""
    available = True
    crs = "EPSG:4326"
    by_reference = by_point = True
    example = ""
    note = ""

    def name(self):
        return ""

    def lookup_reference(self, ref, get=None):
        raise CadastreError(tr("This service cannot search by reference; click a point instead."))

    def lookup_point(self, lon, lat, get=None):
        raise CadastreError(tr("This service cannot search by point; type the parcel reference instead."))


def _contains(parcels, lon, lat):
    """The parcels that contain the point (all of them when none does - the service already filtered)."""
    pt = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
    inside = [p for p in parcels if p.geometry.contains(pt)]
    return inside or parcels


class France(Connector):
    id, cc, example = "fr_apicarto", "FR", "75056 AB 0001"
    note = "IGN API Carto - cadastre (Etalab licence)"
    URL = "https://apicarto.ign.fr/api/cadastre/parcelle?"
    REF = re.compile(r"^\s*(\d[\dAB]\d{3})[\s-]*(?:(\d{3})[\s-]+)?([0-9A-Z]{1,2})[\s-]*(\d{1,4})\s*$", re.I)

    def name(self):
        return tr("France - cadastre (IGN API Carto)")

    def _parcels(self, body):
        out = []
        for g, p in parse_geojson(body):
            ref = p.get("idu") or ""
            out.append(Parcel(g, self.crs, ref, f"{p.get('nom_com', '')} {p.get('section', '')} {p.get('numero', '')}"
                              .strip(), p.get("contenance"), p))
        return out

    def lookup_reference(self, ref, get=None):
        m = self.REF.match(ref or "")
        if not m:
            raise CadastreError(tr("Write the French parcel as: INSEE code, section, number - e.g. {e}").format(
                e=self.example))
        q = {"code_insee": m.group(1).upper(), "section": m.group(3).upper().zfill(2), "numero": m.group(4).zfill(4)}
        if m.group(2):
            q["com_abs"] = m.group(2)
        return self._parcels(_g(get)(self.URL + urllib.parse.urlencode(q)))

    def lookup_point(self, lon, lat, get=None):
        geom = json.dumps({"type": "Point", "coordinates": [lon, lat]})
        return _contains(self._parcels(_g(get)(self.URL + urllib.parse.urlencode({"geom": geom}))), lon, lat)


class Netherlands(Connector):
    id, cc, example = "nl_pdok", "NL", "ASD05 G 1542"
    note = "Kadaster BRK - kadastrale kaart via PDOK OGC API (CC BY 4.0)"
    URL = "https://api.pdok.nl/kadaster/brk-kadastrale-kaart/ogc/v1/collections/perceel/items?f=json&limit=20&"
    REF = re.compile(r"^\s*(.+?)[\s-]+([A-Z]{1,2})[\s-]*(\d{1,5})\s*$", re.I)

    def name(self):
        return tr("Netherlands - kadastrale kaart (Kadaster / PDOK)")

    def _parcels(self, body):
        out = []
        for g, p in parse_geojson(body):
            ref = f"{p.get('akr_kadastrale_gemeente_code_waarde', '')} {p.get('sectie', '')} {p.get('perceelnummer', '')}"
            out.append(Parcel(g, self.crs, ref.strip(), f"{p.get('kadastrale_gemeente_waarde', '')} {ref}".strip(),
                              p.get("kadastrale_grootte_waarde"), p))
        return out

    def lookup_reference(self, ref, get=None):
        m = self.REF.match(ref or "")
        if not m:
            raise CadastreError(tr("Write the Dutch parcel as: cadastral municipality code or name, section, number "
                                  "- e.g. {e}").format(e=self.example))
        gem = m.group(1).strip()
        key = "akr_kadastrale_gemeente_code_waarde" if re.fullmatch(r"[A-Z]{3}\d{2}", gem, re.I) \
            else "kadastrale_gemeente_waarde"
        q = {key: gem.upper() if key.startswith("akr") else gem, "sectie": m.group(2).upper(),
             "perceelnummer": m.group(3)}
        return self._parcels(_g(get)(self.URL + urllib.parse.urlencode(q)))

    def lookup_point(self, lon, lat, get=None):
        d = 1e-5
        bbox = f"{lon - d},{lat - d},{lon + d},{lat + d}"
        return _contains(self._parcels(_g(get)(self.URL + urllib.parse.urlencode({"bbox": bbox}))), lon, lat)


class Spain(Connector):
    id, cc, example = "es_catastro", "ES", "9872023VH5797S"
    note = "Dirección General del Catastro - INSPIRE Cadastral Parcels WFS"
    WFS = "https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx?service=wfs&version=2&request=getfeature&" \
          "STOREDQUERIE_ID=GetParcel&srsname=EPSG::4258&refcat="
    COORDS = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_RCCOOR?" \
             "SRS=EPSG:4326&"
    crs = "EPSG:4258"

    def name(self):
        return tr("Spain - Catastro (INSPIRE)")

    def lookup_reference(self, ref, get=None):
        rc = re.sub(r"[\s-]", "", ref or "").upper()
        if not re.fullmatch(r"[0-9A-Z]{14}([0-9A-Z]{4}([0-9A-Z]{2})?)?", rc):
            raise CadastreError(tr("Write the referencia catastral (14 or 20 characters) - e.g. {e}").format(
                e=self.example))
        feats, authid = parse_gml(_g(get)(self.WFS + rc[:14]))
        crs = authid or self.crs
        return [Parcel(_lonlat(g, (-19, 5), (27, 44.5)) if crs in ("EPSG:4258", "EPSG:4326") else g, crs, rc[:14],
                       rc[:14], _number(a.get("areaValue")), a) for g, a in feats]

    def lookup_point(self, lon, lat, get=None):
        rc = parse_catastro_rc(_g(get)(self.COORDS + urllib.parse.urlencode({"Coordenada_X": f"{lon:.7f}",
                                                                          "Coordenada_Y": f"{lat:.7f}"})))
        if not rc:
            raise CadastreError(tr("The Catastro has no parcel at this point."))
        return self.lookup_reference(rc, get)


class Italy(Connector):
    id, cc = "it_ade", "IT"
    by_reference = False
    note = "Agenzia delle Entrate - INSPIRE Cadastral Parcels WFS (search by point only; the service refuses " \
           "attribute filters)"
    URL = "https://wfs.cartografia.agenziaentrate.gov.it/inspire/wfs/owfs01.php?service=WFS&version=2.0.0&" \
          "request=GetFeature&typeNames=CP:CadastralParcel&count=20&"
    crs = "EPSG:6706"

    def name(self):
        return tr("Italy - catasto (Agenzia delle Entrate)")

    def lookup_point(self, lon, lat, get=None):
        d = 2e-5
        bbox = f"{lat - d},{lon - d},{lat + d},{lon + d},urn:ogc:def:crs:EPSG::4326"
        feats, authid = parse_gml(_g(get)(self.URL + urllib.parse.urlencode({"bbox": bbox})))
        parcels = [Parcel(_lonlat(g, (6, 19), (35, 47.5)), authid or self.crs,
                          a.get("NATIONALCADASTRALREFERENCE") or a.get("INSPIREID_LOCALID") or "",
                          a.get("LABEL") or "", None, a) for g, a in feats]
        return _contains(parcels, lon, lat)


class Poland(Connector):
    id, cc, example = "pl_uldk", "PL", "141201_1.0001.6509"
    note = "GUGiK ULDK - Usługa Lokalizacji Działek Katastralnych"
    URL = "https://uldk.gugik.gov.pl/?"
    RESULT = "geom_wkt,teryt,voivodeship,county,commune,region,parcel"

    def name(self):
        return tr("Poland - działki (GUGiK ULDK)")

    def _parcels(self, body):
        found = parse_uldk(body)
        if not found:
            raise CadastreError(tr("ULDK found no parcel ({m}).").format(
                m=(body.decode("utf-8", "replace") if isinstance(body, bytes) else body).strip()[:120]))
        return [Parcel(g, self.crs, a.get("teryt", ""), f"{a.get('commune', '')} {a.get('parcel', '')}".strip(),
                       None, a) for g, a in found]

    def lookup_reference(self, ref, get=None):
        ref = (ref or "").strip()
        if not re.fullmatch(r"\d{6}_\d\.\d{4}\.\S+", ref):
            raise CadastreError(tr("Write the TERYT parcel identifier - e.g. {e}").format(e=self.example))
        return self._parcels(_g(get)(self.URL + urllib.parse.urlencode(
            {"request": "GetParcelById", "id": ref, "result": self.RESULT, "srid": "4326"})))

    def lookup_point(self, lon, lat, get=None):
        return self._parcels(_g(get)(self.URL + urllib.parse.urlencode(
            {"request": "GetParcelByXY", "xy": f"{lon},{lat},4326", "result": self.RESULT, "srid": "4326"})))


class NotConnected(Connector):
    available = False
    by_reference = by_point = False

    def __init__(self, cid, cc, title, reason):
        self.id, self.cc, self._title, self.note = cid, cc, title, reason

    def name(self):
        return self._title


CONNECTORS = [France(), Netherlands(), Spain(), Italy(), Poland(),
              NotConnected("tr_tkgm", "TR", "Türkiye - TKGM Parsel Sorgu",
                           "Public web map only; no published open API or terms for automated queries."),
              NotConnected("br_sigef", "BR", "Brasil - SIGEF / Acervo Fundiário (INCRA)",
                           "No stable public parcel-query endpoint (the INCRA geoservice was unreachable)."),
              NotConnected("za_csg", "ZA", "South Africa - CSG documents",
                           "Survey diagrams are documents, not a parcel geometry service.")]
BY_ID = {c.id: c for c in CONNECTORS}


def connectors_for(cc):
    """The connectors (available or not) of a country."""
    cc = (cc or "").upper()
    return [c for c in CONNECTORS if c.cc == cc]


def available_connectors():
    return [c for c in CONNECTORS if c.available]


def _number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _json_safe(attrs):
    out = {}
    for k, v in attrs.items():
        out[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    return out


# ---- the tool -----------------------------------------------------------------------------------------------
class CadastreLookup(BiruniAlgorithm):
    ID = "cadastrelookup"
    NAME = "Find a parcel in the official cadastre"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Brings a parcel boundary from an official, public cadastre service into the project - by its "
            "reference (for example 75056 AB 0001 in France, ASD05 G 1542 in the Netherlands, a referencia "
            "catastral in Spain, a TERYT id in Poland) or by a point clicked on the map. Connected now: France, "
            "the Netherlands, Spain, Italy (by point) and Poland. The official area is kept next to the measured "
            "one. Nothing is sent except the reference or the point.")

    SERVICE, REFERENCE, POINT, OUTPUT = "SERVICE", "REFERENCE", "POINT", "OUTPUT"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterEnum(
            self.SERVICE, tr("Cadastre service"), [c.name() for c in available_connectors()], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.REFERENCE, tr("Parcel reference (leave empty to use the point)"), optional=True))
        self.addParameter(QgsProcessingParameterPoint(self.POINT, tr("Or a point inside the parcel"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Parcels from the cadastre"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        con = available_connectors()[self.parameterAsEnum(parameters, self.SERVICE, context)]
        ref = (self.parameterAsString(parameters, self.REFERENCE, context) or "").strip()
        try:
            if ref:
                if not con.by_reference:
                    raise CadastreError(tr("{s} can only search by a point.").format(s=con.name()))
                feedback.pushInfo(tr("Asking {s} for {r}…").format(s=con.name(), r=ref))
                parcels = con.lookup_reference(ref)
            elif parameters.get(self.POINT):
                pt = self.parameterAsPoint(parameters, self.POINT, context, WGS84)
                feedback.pushInfo(tr("Asking {s} for the parcel at {y:.6f}, {x:.6f}…").format(
                    s=con.name(), x=pt.x(), y=pt.y()))
                parcels = con.lookup_point(pt.x(), pt.y())
            else:
                raise CadastreError(tr("Type a parcel reference or pick a point on the map."))
        except CadastreError as e:
            raise QgsProcessingException(str(e)) from e
        if not parcels:
            raise QgsProcessingException(tr("No parcel was found."))

        crs = QgsCoordinateReferenceSystem(parcels[0].crs)
        fields = QgsFields()
        for name, typ in (("service", QMetaType.Type.QString), ("reference", QMetaType.Type.QString),
                          ("label", QMetaType.Type.QString), ("official_area", QMetaType.Type.Double),
                          ("area_m2", QMetaType.Type.Double), ("details", QMetaType.Type.QString)):
            fields.append(QgsField(name, typ))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.MultiPolygon, crs)
        da = QgsDistanceArea()
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
        da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7019")
        for p in parcels:
            g = QgsGeometry(p.geometry)
            g.convertToMultiType()
            f = QgsFeature(fields)
            f.setGeometry(g)
            f.setAttributes([con.name(), p.reference, p.label, _number(p.official_area), da.measureArea(g),
                             json.dumps(_json_safe(p.attributes), ensure_ascii=False)])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("{n} parcel(s) found.").format(n=len(parcels)))
        return {self.OUTPUT: dest, "COUNT": len(parcels)}
