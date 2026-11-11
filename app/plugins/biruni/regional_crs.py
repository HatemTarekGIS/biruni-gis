"""Regional coordinate systems of the world: the legacy and local systems that people
actually meet in old plans, deeds, CAD drawings and government data - the ones that
are missing from EPSG, easy to confuse, or need an official shift grid.

The catalogue is plain data keyed by ISO country code. Families of EPSG systems are
named by patterns (looked up in this PROJ database at run time), single systems by
code, and systems that EPSG does not carry by a PROJ definition (keys "REG:<id>").

Used by "What coordinate system is this?" (candidates for a place, hints from the
numbers themselves), by the CRS doctor and by the region packs (for_country)."""
import math
import os
import re
import sqlite3

from qgis.core import QgsCoordinateReferenceSystem, QgsProjUtils, QgsRectangle

# ---- the catalogue ----------------------------------------------------------------------------
CAPE_FOOT = 0.3148584          # metres (Clarke's Cape foot - South African SG diagrams)
GOLD_COAST_FOOT = 0.3047997    # metres (Ghana National Grid - built into EPSG:2136)
US_SURVEY_FOOT = 1200.0 / 3937.0

# VN-2000 official parameters (decision 05/2007/QD-BTNMT) are published in the Coordinate Frame
# convention; PROJ's +towgs84 expects Position Vector, so the three rotations change sign.
VN2000_TOWGS84 = "-191.90441429,-39.30318279,-111.45032835,-0.00928836,0.01975479,-0.00427372,0.252906278"

# Central meridian of each Vietnamese province (VN-2000 TM-3, k = 0.9999, circular 973/2001/TT-TCDC).
VN_PROVINCE_CM = {
    "Lai Chau": 103.0, "Dien Bien": 103.0, "Son La": 104.0, "Kien Giang": 104.5, "Ca Mau": 104.5,
    "Lao Cai": 104.75, "Yen Bai": 104.75, "Nghe An": 104.75, "Phu Tho": 104.75, "An Giang": 104.75,
    "Thanh Hoa": 105.0, "Vinh Phuc": 105.0, "Ha Nam": 105.0, "Ninh Binh": 105.0, "Ha Noi": 105.0,
    "Can Tho": 105.0, "Dong Thap": 105.0, "Hau Giang": 105.0, "Bac Lieu": 105.0,
    "Ha Giang": 105.5, "Bac Ninh": 105.5, "Hai Duong": 105.5, "Hung Yen": 105.5, "Nam Dinh": 105.5,
    "Thai Binh": 105.5, "Ha Tinh": 105.5, "Tay Ninh": 105.5, "Vinh Long": 105.5, "Tra Vinh": 105.5,
    "Soc Trang": 105.5,
    "Cao Bang": 105.75, "Hai Phong": 105.75, "Binh Duong": 105.75, "Long An": 105.75, "Tien Giang": 105.75,
    "Ben Tre": 105.75, "Ho Chi Minh City": 105.75,
    "Tuyen Quang": 106.0, "Hoa Binh": 106.0, "Quang Binh": 106.0,
    "Quang Tri": 106.25, "Binh Phuoc": 106.25,
    "Bac Kan": 106.5, "Thai Nguyen": 106.5,
    "Bac Giang": 107.0, "Thua Thien Hue": 107.0,
    "Lang Son": 107.25,
    "Kon Tum": 107.5,
    "Quang Ninh": 107.75, "Dong Nai": 107.75, "Ba Ria - Vung Tau": 107.75, "Quang Nam": 107.75,
    "Lam Dong": 107.75, "Da Nang": 107.75,
    "Quang Ngai": 108.0,
    "Ninh Thuan": 108.25, "Khanh Hoa": 108.25, "Binh Dinh": 108.25,
    "Dak Lak": 108.5, "Dak Nong": 108.5, "Phu Yen": 108.5, "Gia Lai": 108.5, "Binh Thuan": 108.5,
}

# Grid-zone prefixes on the easting (Gauss-Krüger style): 7 or 8 digit eastings whose leading digits are the zone.
GK_FAMILIES = ("Pulkovo 1942 / Gauss-Kruger zone {z}", "Pulkovo 1995 / Gauss-Kruger zone {z}",
               "GSK-2011 / Gauss-Kruger zone {z}", "UCS-2000 / Gauss-Kruger zone {z}",
               "CGCS2000 / 3-degree Gauss-Kruger zone {z}", "CGCS2000 / Gauss-Kruger zone {z}",
               "Xian 1980 / 3-degree Gauss-Kruger zone {z}", "Xian 1980 / Gauss-Kruger zone {z}",
               "Beijing 1954 / 3-degree Gauss-Kruger zone {z}", "Beijing 1954 / Gauss-Kruger zone {z}",
               "DHDN / 3-degree Gauss-Kruger zone {z}", "TUREF / 3-degree Gauss-Kruger zone {z}",
               "Pulkovo 1942(58) / Gauss-Kruger zone {z}", "Pulkovo 1942(83) / Gauss-Kruger zone {z}")
# Polish CS2000: zone number 5-8 is the easting prefix, central meridian 3 x zone
PL_CS2000 = {5: "EPSG:2176", 6: "EPSG:2177", 7: "EPSG:2178", 8: "EPSG:2179"}


def E(cc, eid, label, local="", codes=(), patterns=(), proj="", transforms=(), grids=(), axis="", units="",
      ranges="", note="", bbox=None, license=""):
    return {"cc": cc, "id": eid, "label": label, "local": local, "codes": list(codes), "patterns": list(patterns),
            "proj": proj, "transforms": list(transforms), "grids": list(grids), "axis": axis, "units": units,
            "ranges": ranges, "note": note, "bbox": bbox, "license": license}


CATALOG = [
    # ---- Middle East & North Africa ----
    E("SA", "sa-ain-el-abd", "Ain el Abd 1970 / UTM 36N-40N", "عين العبد", codes=[f"EPSG:{c}" for c in range(20436, 20441)],
      transforms=["Ain el Abd to WGS 84 (several sets, about 10-25 m apart)"],
      note="Old Saudi and Kuwaiti plans. Lands 100-300 m from WGS 84 positions.", bbox=(34, 16, 56, 33)),
    E("SA", "sa-ksa-grf17", "KSA-GRF17 / UTM 36N-40N", "الإطار المرجعي السعودي", codes=[f"EPSG:{c}" for c in range(9356, 9361)],
      note="Current Saudi national frame (GASGI); practically WGS 84 for mapping.", bbox=(34, 16, 56, 33)),
    E("SA", "sa-mtrf2000", "MTRF-2000 / UTM 36N-40N", "MTRF-2000", codes=[f"EPSG:{c}" for c in range(8836, 8841)],
      note="Saudi frame used by MOMRA / Balady before KSA-GRF17.", bbox=(34, 16, 56, 33)),
    E("EG", "eg-belts", "Egypt 1907 / Red, Purple, Blue, Extended Purple belts", "الأحزمة المصرية",
      codes=["EPSG:22991", "EPSG:22992", "EPSG:22993", "EPSG:22994"],
      transforms=["Egypt 1907 to WGS 84 (EPSG:1148, about 11 m)", "unified transformation EPSG:7697 (about 1.2 m)"],
      note="Many conflicting TOWGS84 sets circulate; prefer the unified EPSG:7697 operation.", bbox=(24, 21, 37, 32)),
    E("IQ", "iq-karbala", "Karbala 1979 / UTM 37N-39N", "كربلاء 1979", patterns=["Karbala 1979 / %"],
      note="Fitted mostly south of Baghdad: weaker in Kurdistan (Erbil, Duhok).", bbox=(38, 29, 49, 38)),
    E("IL", "il-itm", "Israel 1993 / Israeli TM Grid (ITM)", "רשת ישראל", codes=["EPSG:2039", "EPSG:6991"],
      note="Coordinates about 150 000 / 600 000. A datum-less PROJ string puts data about 90 m off.",
      bbox=(34, 29, 36, 34)),
    E("IL", "il-ics", "Palestine 1923 / Israeli CS (old Cassini grid)", "ICS", patterns=["Palestine 1923 / %"],
      note="Older maps; Y below 100 000 hints the old grid.", bbox=(34, 29, 36, 34)),
    E("IR", "ir-utm", "WGS 84 / UTM 38N-41N (Iran)", "UTM ایران", codes=["EPSG:32638", "EPSG:32639", "EPSG:32640", "EPSG:32641"],
      note="Deed UTM maps; old maps may be on Nahrwan or FD58.", bbox=(44, 25, 64, 40)),
    E("MA", "ma-merchich", "Merchich / Nord, Sud Maroc (Lambert)", "مرشيش", patterns=["Merchich / %"],
      note="Data in the old Moroccan system lands about 400 km off when the wrong zone is used.", bbox=(-17, 21, -1, 36)),
    E("DZ", "dz-nord-sahara", "Nord Sahara 1959 / UTM and Voirol", "الصحراء الشمالية", patterns=["Nord Sahara 1959 / %", "Voirol %"],
      transforms=["official 2022 transformation (local differences up to 30 m)"], bbox=(-9, 18, 12, 38)),
    E("TN", "tn-carthage", "Carthage / UTM 32N, Nord & Sud Tunisie", "قرطاج", patterns=["Carthage / %"],
      note="UTM 32N on Carthage differs from WGS 84 by about 450 m; widely shared shift parameters are wrong.",
      bbox=(7, 30, 12, 38)),
    # ---- Africa ----
    E("KE", "ke-arc1960", "Arc 1960 / UTM 36S, 37S (and N)", "Arc 1960", codes=["EPSG:21036", "EPSG:21037", "EPSG:21096",
                                                                            "EPSG:21097"],
      note="Typical shift to WGS 84 is 120-170 m. Old Kenyan plans are Cassini-Soldner on local origins: "
           "fit them from control points (Site calibration).", bbox=(29, -12, 42, 5)),
    E("NG", "ng-minna", "Minna / Nigeria West, Mid, East belts (NTM) and UTM", "Minna",
      codes=["EPSG:26391", "EPSG:26392", "EPSG:26393", "EPSG:26331", "EPSG:26332"],
      transforms=["several Minna to WGS 84 sets, 3-25 m accuracy, about 100-200 m shift"], bbox=(2, 4, 15, 14)),
    E("GH", "gh-ngg", "Accra / Ghana National Grid (Gold Coast feet)", "Ghana National Grid", codes=["EPSG:2136", "EPSG:2137"],
      units="Gold Coast foot = 0.3047997 m", note="Foot/metre mix-ups are common.", bbox=(-4, 4, 2, 12)),
    E("ZA", "za-lo", "Hartebeesthoek94 / Lo15-Lo33 (south-oriented)", "Lo-stelsel", codes=[f"EPSG:{c}" for c in range(2046, 2056)],
      axis="South-oriented: Y positive west, X positive south. GIS files often store -Y / -X.",
      note="Data in Egypt or the mid-Atlantic means the signs or the axes are swapped.", bbox=(15, -35, 33, -22)),
    E("ZA", "za-cape", "Cape / Lo zones (old SG diagrams, also in Cape feet)", "Cape datum", patterns=["Cape / Lo%"],
      units="Cape foot = 0.3148584 m (SG diagrams); Natal used English feet",
      note="Y about 9 million in feet; no EPSG system in feet - the finder tries the Cape-foot scale.",
      bbox=(15, -35, 33, -22)),
    E("ET", "et-adindan", "Adindan / UTM 35N-38N", "Adindan", patterns=["Adindan / %"],
      note="Google Earth and GIS positions can differ by 150 m depending on the transformation.", bbox=(32, 3, 48, 15)),
    E("SN", "sn-yoff", "Yoff / UTM 28N", "Yoff", patterns=["Yoff / %"], bbox=(-18, 12, -11, 17)),
    E("CI", "ci-locodjo", "Locodjo 1965 / Abidjan 1987 (UTM 29N, 30N)", "Locodjo", patterns=["Locodjo 1965 / %", "Abidjan 1987 / %"],
      note="Split between UTM 29N and 30N at 6°W.", bbox=(-9, 4, -2, 11)),
    E("CM", "cm-manoca", "Manoca 1962 / Douala 1948 (Clarke 1880)", "Manoca", patterns=["Manoca 1962 / %", "Douala 1948 / %"],
      note="Two conflicting transformations to WGS 84 are in use.", bbox=(8, 1, 17, 13)),
    # ---- Asia ----
    E("PK", "pk-kalianpur62", "Kalianpur 1962 / UTM 41N-43N", "کلیانپور", patterns=["Kalianpur 1962 / %"],
      transforms=["DMA onshore, Badin-Mehran, Gambat, East Sind and offshore Indus sets differ by metres"],
      note="Choose UTM 42N or 43N by longitude 72°E.", bbox=(60, 23, 78, 38)),
    E("IN", "in-kalianpur", "Kalianpur 1937 / 1962 / 1975, Everest and India NSF LCC", "Everest",
      patterns=["Kalianpur 1937 / %", "Kalianpur 1975 / %", "Indian 1960 / %"], codes=["EPSG:7755"],
      note="'Everest 1830' on toposheets is one of the Kalianpur datums; transformations are good to 10-15 m.",
      bbox=(68, 6, 98, 37)),
    E("BD", "bd-btm", "Gulshan 303 / Bangladesh TM (BTM) and TM 90 NE", "BTM", codes=["EPSG:9678", "EPSG:3106"],
      note="BTM has false northing -2 000 000; TM 90 NE has 0. The northing tells them apart.", bbox=(88, 20, 93, 27)),
    E("TH", "th-indian75", "Indian 1975 / UTM 47N, 48N", "Indian 1975", codes=["EPSG:24047", "EPSG:24048"],
      transforms=["EPSG:1154 and EPSG:1304 and the Chulalongkorn set (204.4, 837.7, 294.7) compete"],
      note="Almost one kilometre from WGS 84 without a datum shift; zones split at 102°E. Old deeds may be on "
           "local origins or floating plots: fit them from control points.", bbox=(97, 5, 106, 21)),
    E("VN", "vn-2000", "VN-2000 / TM-3 per province (k = 0.9999) and UTM 48N, 49N", "VN-2000",
      patterns=["VN-2000 / TM-3 %", "VN-2000 / UTM %"],
      note="Each province has its own central meridian. Official parameters are Coordinate Frame: flip the "
           "rotation signs for PROJ +towgs84 (about 0.5 m otherwise).", bbox=(102, 8, 110, 24)),
    E("KR", "kr-bessel", "Korean 1985 (Bessel) Modified belts incl. 5174 (KLIS) and Korea 2000 (GRS80) belts", "지적좌표계",
      codes=["EPSG:5173", "EPSG:5174", "EPSG:5175", "EPSG:5176", "EPSG:5185", "EPSG:5186", "EPSG:5187", "EPSG:5188"],
      transforms=["2002 7-parameter Bursa-Wolf vs 2003 10-parameter Molodensky-Badekas (many tools still use 2002)"],
      note="EPSG:5174 is the 'modified central belt' used by KLIS cadastral data.", bbox=(124, 33, 132, 39)),
    E("JP", "jp-plane", "Japan Plane Rectangular CS I-XIX (JGD2011, JGD2000, Tokyo)", "平面直角座標系",
      patterns=["JGD2011 / Japan Plane Rectangular CS %", "JGD2000 / Japan Plane Rectangular CS %",
                "Tokyo / Japan Plane Rectangular CS %"],
      axis="Japanese surveys write X = north, Y = east: survey CSVs load with X and Y swapped.",
      note="Picking the wrong zone shifts data by tens of km; Tokyo datum vs JGD differs by hundreds of metres.",
      bbox=(122, 20, 154, 46)),
    E("PH", "ph-prs92", "PRS92 / Philippines zones 1-5 (PTM)", "PRS92", codes=[f"EPSG:{c}" for c in range(3121, 3126)],
      patterns=["Luzon 1911 / %"], note="PRS92 to WGS 84 is not a simple shift; fixed offsets give 80 m errors.",
      bbox=(116, 4, 127, 21)),
    E("ID", "id-tm3", "DGN95 / SRGI2013 Indonesia TM-3 (BPN) and UTM", "TM-3 BPN",
      patterns=["DGN95 / Indonesia TM-3 %", "SRGI2013 / %"],
      note="BPN TM-3: k = 0.9999, false easting 200 000, false northing 1 500 000.", bbox=(94, -11, 142, 6)),
    E("CN", "cn-cgcs2000", "CGCS2000 / Xian 1980 / Beijing 1954 Gauss-Krüger 3° and 6° zones", "国家大地坐标系",
      patterns=["CGCS2000 / 3-degree Gauss-Kruger %", "CGCS2000 / Gauss-Kruger %", "Xian 1980 / 3-degree Gauss-Kruger %",
                "Beijing 1954 / 3-degree Gauss-Kruger %"],
      transforms=["Xian 80 / Beijing 54 to CGCS2000 need local 7- or 4-parameter sets (not public): fit them from "
                  "control points"],
      note="The zone number is the first digits of the easting (e.g. 38 512 345 = zone 38).", bbox=(73, 18, 135, 54)),
    # ---- Russia, Ukraine, Turkey ----
    E("RU", "ru-sk42", "Pulkovo 1942 (SK-42), Pulkovo 1995, GSK-2011 Gauss-Krüger zones", "СК-42 / ГСК-2011",
      patterns=["Pulkovo 1942 / Gauss-Kruger zone %", "Pulkovo 1995 / Gauss-Kruger zone %", "GSK-2011 / Gauss-Kruger %"],
      note="The zone number is the first digits of the easting. SK-42 TOWGS84 differs between GOST 2001 and 2008.",
      bbox=(19, 41, 180, 82)),
    E("RU", "ru-msk", "MSK local systems (МСК-XX)", "МСК", proj="+proj=tmerc +lat_0=0 +lon_0={lon_0} +k=1 +x_0={x_0} "
      "+y_0={y_0} +ellps=krass +towgs84=23.57,-140.95,-79.8,0,0.35,0.79,-0.22 +units=m +no_defs",
      note="MSK parameters are not public and not in EPSG: enter the central meridian and false origin you were "
           "given, or fit the system from 3+ control points (Site calibration).", bbox=(19, 41, 180, 82)),
    E("KZ", "kz-sk42", "Pulkovo 1942 Gauss-Krüger zones (Kazakhstan)", "СК-42", patterns=["Pulkovo 1942 / Gauss-Kruger zone %"],
      note="Local city systems (e.g. Almaty) are not public: fit from control points.", bbox=(46, 40, 88, 56)),
    E("UA", "ua-ucs2000", "UCS-2000 (USK-2000) and SK-42/SK-63", "УСК-2000", patterns=["UCS-2000 / %"],
      note="SK-63 keys are not public: fit from control points. Official conversion is online-only (privacy).",
      bbox=(22, 44, 41, 53)),
    E("TR", "tr-tm3", "ED50 / TUREF (ITRF96) 3° TM zones", "TM 3 derece", patterns=["ED50 / TM%", "TUREF / TM%"],
      transforms=["ED50 to ITRF96 residuals up to 38 cm; TKGM parameters are valid only locally"],
      bbox=(25, 35, 45, 43)),
    # ---- Europe ----
    E("ES", "es-ed50", "ED50 and ETRS89 / UTM 28N-31N", "ED50", codes=["EPSG:23029", "EPSG:23030", "EPSG:23031",
                                                                     "EPSG:25829", "EPSG:25830", "EPSG:25831"],
      grids=["es_ign_SPED2ETV2.tif"], transforms=["ED50 to ETRS89 by the PENR2009 NTv2 grid"], bbox=(-19, 27, 5, 44)),
    E("FR", "fr-lambert", "NTF Lambert I-IV / II étendu, Lambert 93, CC42-CC50", "Lambert",
      codes=["EPSG:2154"], patterns=["RGF93 v1 / CC%", "NTF (Paris) / Lambert %"], grids=["fr_ign_gr3df97a.tif"],
      transforms=["NTF to RGF93 by the gr3df97a grid"], note="The millions digit of Y gives the old Lambert zone.",
      bbox=(-6, 41, 10, 52)),
    E("DE", "de-gk", "DHDN Gauß-Krüger zones 2-5 and ETRS89 / UTM 32N, 33N", "Gauß-Krüger",
      patterns=["DHDN / 3-degree Gauss-Kruger zone %"], codes=["EPSG:25832", "EPSG:25833"],
      grids=["de_adv_BETA2007.tif"], transforms=["BeTA2007 national grid or state grids (BWTA2017, HeTA2010)"],
      note="UTM eastings with the zone prefix (32 500 000) are EPSG:4647 / 5677 style.", bbox=(5, 47, 16, 56)),
    E("AT", "at-mgi", "MGI / Austria GK and Lambert", "MGI", patterns=["MGI / Austria %"],
      grids=["at_bev_AT_GIS_GRID_2021_09_28.tif"], transforms=["BEV grid (a plain Helmert shift is about 30 cm off)"],
      bbox=(9, 46, 18, 49.5)),
    E("CH", "ch-lv", "CH1903 / LV03 and CH1903+ / LV95", "LV95", codes=["EPSG:21781", "EPSG:2056"],
      grids=["ch_swisstopo_CHENyx06_ETRS.tif"], bbox=(5.9, 45.8, 10.5, 47.9)),
    E("IT", "it-gauss-boaga", "Monte Mario (Roma40) / Gauss-Boaga, RDN2008", "Gauss-Boaga",
      codes=["EPSG:3003", "EPSG:3004", "EPSG:7791", "EPSG:7792"], patterns=["Monte Mario / %"],
      license="The official IGM grids (Roma40/ED50 to RDN2008) are sold by Istituto Geografico Militare: buy the "
              "GK2/GSB files and put them in Biruni's proj folder.",
      note="Cadastral CXF sheets use Cassini-Soldner local origins: fit them per sheet from fiducial points.",
      bbox=(6, 36, 19, 47.5)),
    E("NL", "nl-rd", "Amersfoort / RD New", "RD", codes=["EPSG:28992"], grids=["nl_nsgi_rdtrans2018.tif"],
      transforms=["RDNAPTRANS 2018 grid"], bbox=(3, 50.7, 7.3, 53.7)),
    E("PL", "pl-cs", "PL-1992 and PL-2000 zones 5-8", "PL-1992 / PL-2000", codes=["EPSG:2180", *PL_CS2000.values()],
      axis="Polish surveys write x = north: files often arrive with x and y swapped.", bbox=(14, 49, 24.2, 55)),
    E("FI", "fi-kkj", "KKJ zones and ETRS-TM35FIN", "KKJ", codes=["EPSG:3067"], patterns=["KKJ / Finland %"],
      grids=["fi_nls_ykj_etrs35fin.json"], transforms=["KKJ to TM35FIN by the national triangulation (3 cm)"],
      bbox=(19, 59, 32, 71)),
    E("SE", "se-sweref", "SWEREF99 TM and local zones", "SWEREF 99", codes=["EPSG:3006", *[f"EPSG:{c}" for c in range(3007, 3019)]],
      bbox=(10, 55, 25, 69.5)),
    E("NO", "no-ntm", "ETRS89 / NTM zones and UTM 32N-35N", "NTM", patterns=["%/ NTM zone %"],
      codes=["EPSG:25832", "EPSG:25833", "EPSG:25835"], note="SOSI files often lack the KOORDSYS header.",
      bbox=(4, 57, 32, 72)),
    # ---- Americas ----
    E("BR", "br-sirgas", "SAD69, Córrego Alegre and SIRGAS 2000 / UTM", "SAD69 / SIRGAS",
      patterns=["SAD69 / UTM %", "Corrego Alegre 1970-72 / UTM %", "SIRGAS 2000 / UTM %"], grids=["br_ibge_SAD69_003.tif"],
      transforms=["IBGE NTv2 grid (ProGriD)"],
      note="SIGEF computes area in the Local Geodetic System (SGL), not UTM: expect about 0.15% difference.",
      bbox=(-74, -34, -34, 6)),
    E("PE", "pe-psad56", "PSAD56 / UTM 17S-19S", "PSAD56", patterns=["PSAD56 / UTM %"],
      transforms=["generic transformations are 10-17 m off on cadastral parcels; INGEMMET uses its own sets"],
      bbox=(-82, -19, -68, 0)),
    E("CO", "co-ctm12", "MAGNA-SIRGAS origins and CTM12 national origin", "Origen Nacional", codes=["EPSG:9377"],
      patterns=["MAGNA-SIRGAS / %"], note="Resolution 370/2021 requires CTM12 (EPSG:9377).", bbox=(-80, -5, -66, 13.5)),
    E("AR", "ar-posgar", "POSGAR 2007 and Campo Inchauspe / Argentina strips 1-7", "POSGAR",
      patterns=["POSGAR 2007 / %", "Campo Inchauspe / %"],
      note="The default Campo Inchauspe transformation is only about 5 m accurate.", bbox=(-74, -56, -53, -21)),
]

# Systems that EPSG lacks, by key "REG:<id>": {"label", "proj" | "func", "pre": (sx, sy, factor), "bbox", "cc"}
_REG = {}


def _vn_key(cm):
    d, m = int(cm), int(round((cm - int(cm)) * 60))
    return f"REG:VN2000-TM3-{d}-{m:02d}", f"VN-2000 / TM-3 {d}°{m:02d}'"


for _cm in sorted(set(VN_PROVINCE_CM.values())):
    _k, _label = _vn_key(_cm)
    _REG[_k] = {"cc": "VN", "label": _label + " (" + ", ".join(p for p, c in VN_PROVINCE_CM.items() if c == _cm) + ")",
                "proj": f"+proj=tmerc +lat_0=0 +lon_0={_cm} +k=0.9999 +x_0=500000 +y_0=0 +ellps=WGS84 "
                        f"+towgs84={VN2000_TOWGS84} +units=m +no_defs", "bbox": (102, 8, 110, 24)}
for _lo, _code in zip(range(15, 35, 2), range(2046, 2056)):
    for _suffix, _sx, _sy in (("-x", -1, 1), ("-y", 1, -1), ("-xy", -1, -1)):
        _REG[f"REG:ZA-Lo{_lo}{_suffix}"] = {"cc": "ZA", "label": f"Hartebeesthoek94 / Lo{_lo} (signs {_suffix})",
                                            "authid": f"EPSG:{_code}", "pre": (_sx, _sy, 1.0), "bbox": (15, -35, 33, -22)}
_REG["REG:GCJ02"] = {"cc": "CN", "label": "GCJ-02 (Chinese web-map offset: AMap, Tencent, Google China)",
                     "func": "gcj02", "bbox": (72, 0.8, 138, 56)}
_REG["REG:BD09"] = {"cc": "CN", "label": "BD-09 (Baidu map offset)", "func": "bd09", "bbox": (72, 0.8, 138, 56)}


def _register_cape_feet():
    """Cape / Lo zones in Cape feet (old SG diagrams), resolved from the PROJ database."""
    for authid, name in pattern_codes("Cape / Lo%").items():
        _REG[f"REG:{re.sub(r'[^0-9A-Za-z]+', '', name)}-capeft"] = {"cc": "ZA", "label": f"{name} in Cape feet",
                                                         "authid": authid, "pre": (1.0, 1.0, CAPE_FOOT),
                                                         "bbox": (15, -35, 33, -22)}


# ---- PROJ database lookups ---------------------------------------------------------------------
_db_cache = {}


def proj_db():
    if "path" not in _db_cache:
        _db_cache["path"] = next((os.path.join(p, "proj.db") for p in QgsProjUtils.searchPaths()
                                  if os.path.exists(os.path.join(p, "proj.db"))), None)
    return _db_cache["path"]


def pattern_codes(pattern):
    """{authid: name} of the non-deprecated EPSG projected systems whose name matches a LIKE pattern."""
    key = ("pat", pattern)
    if key in _db_cache:
        return _db_cache[key]
    out = {}
    db = proj_db()
    if db:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                for code, name in con.execute("SELECT code, name FROM projected_crs WHERE auth_name='EPSG' AND "
                                              "deprecated=0 AND name LIKE ? ORDER BY code", (pattern,)):
                    out[f"EPSG:{code}"] = name
            finally:
                con.close()
        except sqlite3.Error:
            pass
    _db_cache[key] = out
    return out


def entry_codes(entry):
    """{authid: name} of every EPSG system of a catalogue entry."""
    out = {}
    for c in entry["codes"]:
        out.setdefault(c, "")
    for p in entry["patterns"]:
        for c, n in pattern_codes(p).items():
            out[c] = n
    return out


# ---- public API ----------------------------------------------------------------------------------
def countries():
    return sorted({e["cc"] for e in CATALOG})


def for_country(cc):
    """The catalogue entries of a country (ISO code): dicts with 'label', 'authid' (first system) or 'proj',
    'codes', 'note', 'transforms', 'grids', 'axis', 'units', 'license'."""
    cc = (cc or "").upper()
    out = []
    for e in CATALOG:
        if e["cc"] != cc:
            continue
        d = dict(e)
        codes = list(entry_codes(e))
        d["codes"] = codes
        d["authid"] = codes[0] if codes else ""
        out.append(d)
    if not _REG_CAPE_DONE:
        _ensure_cape()
    for k, spec in _REG.items():
        if spec["cc"] == cc:
            out.append({"cc": cc, "id": k, "label": spec["label"], "authid": spec.get("authid", ""),
                        "proj": spec.get("proj", ""), "codes": [k], "note": "", "transforms": [], "grids": [],
                        "axis": "", "units": "", "license": ""})
    return out


_REG_CAPE_DONE = False


def _ensure_cape():
    global _REG_CAPE_DONE
    if not _REG_CAPE_DONE:
        _REG_CAPE_DONE = True
        _register_cape_feet()


def _in_bbox(lon, lat, bbox, margin=0.5):
    return bbox is not None and bbox[0] - margin <= lon <= bbox[2] + margin and bbox[1] - margin <= lat <= bbox[3] + margin


def countries_at(lon, lat):
    return sorted({e["cc"] for e in CATALOG if _in_bbox(lon, lat, e["bbox"])})


def candidates_for_point(lon, lat):
    """{key: name} of the regional systems made for a place (EPSG families and REG keys)."""
    _ensure_cape()
    out = {}
    for e in CATALOG:
        if not _in_bbox(lon, lat, e["bbox"]):
            continue
        for c, n in entry_codes(e).items():
            out.setdefault(c, n or e["label"])
    for k, spec in _REG.items():
        if _in_bbox(lon, lat, spec["bbox"]):
            out.setdefault(k, spec["label"])
    return out


def candidates_for_values(points):
    """{key: name} hinted by the numbers themselves: a Gauss-Krüger zone written as the first digits of the
    easting (7 or 8 digit eastings: SK-42, CGCS2000, Xian 80, Beijing 54, DHDN, GSK-2011, UCS-2000, TUREF,
    Polish CS2000)."""
    zones = {}
    for x, y in points:
        for v, other in ((x, y), (y, x)):           # also with the axes swapped
            # both numbers in the millions: a prefixed easting with a northing (UTM eastings have 6 digits)
            if 1e6 <= abs(v) < 1e8 and 1e6 <= abs(other) < 1e7:
                z = int(abs(v) // 1e6)
                frac = abs(v) - z * 1e6
                if 100000 <= frac <= 900000:          # 500 000 +- 400 km around the zone's meridian
                    zones[z] = zones.get(z, 0) + 1
    out = {}
    if not points:
        return out
    for z, n in zones.items():
        if n < len(points) / 2:
            continue
        for fam in GK_FAMILIES:
            out.update(pattern_codes(fam.format(z=z)))
        if z in PL_CS2000:
            out.setdefault(PL_CS2000[z], f"PL-2000 zone {z}")
    return out


class Spec:
    """How to turn the raw numbers of a candidate into WGS 84 positions."""

    def __init__(self, key, crs=None, sx=1.0, sy=1.0, factor=1.0, func=None, bbox=None, label=""):
        self.key, self.crs, self.sx, self.sy, self.factor, self.func = key, crs, sx, sy, factor, func
        self.label = label
        self.bounds = QgsRectangle(*bbox[:2], *bbox[2:]) if bbox else (crs.bounds() if crs is not None else QgsRectangle())

    @property
    def scaled(self):
        return self.sx != 1.0 or self.sy != 1.0 or self.factor != 1.0

    def pre(self, x, y):
        return x * self.sx * self.factor, y * self.sy * self.factor

    def is_geographic(self):
        return self.func is not None or (self.crs is not None and self.crs.isGeographic())


def resolve(key):
    """The Spec of a candidate key: an authority id ('EPSG:20438') or a regional key ('REG:...')."""
    if not key.startswith("REG:"):
        crs = QgsCoordinateReferenceSystem(key)
        return Spec(key, crs if crs.isValid() else None)
    _ensure_cape()
    spec = _REG.get(key)
    if spec is None:
        return Spec(key)
    if "func" in spec:
        return Spec(key, func={"gcj02": gcj02_to_wgs84, "bd09": bd09_to_wgs84}[spec["func"]], bbox=spec["bbox"],
                    label=spec["label"])
    crs = QgsCoordinateReferenceSystem.fromProj(spec["proj"]) if spec.get("proj") else \
        QgsCoordinateReferenceSystem(spec["authid"])
    sx, sy, factor = spec.get("pre", (1.0, 1.0, 1.0))
    return Spec(key, crs if crs.isValid() else None, sx, sy, factor, bbox=spec["bbox"], label=spec["label"])


def describe(entry):
    """One paragraph about a catalogue entry (for tool tips and reports)."""
    parts = [entry["label"]]
    if entry.get("local"):
        parts[0] += f" ({entry['local']})"
    codes = entry.get("codes") or list(entry_codes(entry))
    if codes:
        shown = ", ".join(codes[:6]) + (f" … ({len(codes)} systems)" if len(codes) > 6 else "")
        parts.append(f"Systems: {shown}.")
    for key in ("transforms", "grids"):
        if entry.get(key):
            parts.append(("Transformations: " if key == "transforms" else "Shift grids: ") + "; ".join(entry[key]) + ".")
    for key in ("axis", "units", "note", "license"):
        if entry.get(key):
            parts.append(entry[key].rstrip(".") + ".")
    return " ".join(parts)


def axis_note(authid):
    """The axis-order remark for a system, if its country writes coordinates the other way round."""
    for e in CATALOG:
        if e["axis"] and authid in entry_codes(e):
            return e["axis"]
    return ""


def msk_crs(lon_0, x_0=0.0, y_0=0.0):
    """A Russian MSK system from the parameters a user was given (central meridian, false origin)."""
    entry = next(e for e in CATALOG if e["id"] == "ru-msk")
    return QgsCoordinateReferenceSystem.fromProj(entry["proj"].format(lon_0=lon_0, x_0=x_0, y_0=y_0))


# ---- Chinese web-map offsets (public GCJ-02 / BD-09 algorithm) -----------------------------------
_A, _EE = 6378245.0, 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def out_of_china(lon, lat):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tlon(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lon, lat):
    if out_of_china(lon, lat):
        return lon, lat
    dlat, dlon = _tlat(lon - 105.0, lat - 35.0), _tlon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sq) * math.pi)
    dlon = (dlon * 180.0) / (_A / sq * math.cos(rad) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon, lat):
    """Inverse by iteration (better than 1 cm after a few steps)."""
    if out_of_china(lon, lat):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(30):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        dx, dy = glon - lon, glat - lat
        wlon, wlat = wlon - dx, wlat - dy
        if abs(dx) < 1e-10 and abs(dy) < 1e-10:
            break
    return wlon, wlat


def gcj02_to_bd09(lon, lat):
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    t = math.atan2(lat, lon) + 0.000003 * math.cos(lon * _X_PI)
    return z * math.cos(t) + 0.0065, z * math.sin(t) + 0.006


def bd09_to_gcj02(lon, lat):
    x, y = lon - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    t = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(t), z * math.sin(t)


def bd09_to_wgs84(lon, lat):
    return gcj02_to_wgs84(*bd09_to_gcj02(lon, lat))


def wgs84_to_bd09(lon, lat):
    return gcj02_to_bd09(*wgs84_to_gcj02(lon, lat))


def is_valid_key(key):
    return bool(re.match(r"^(EPSG:\d+|REG:[\w\-.]+)$", key))
