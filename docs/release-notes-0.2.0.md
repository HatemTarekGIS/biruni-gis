## Biruni GIS 0.2.0 - first public release

Free and open-source desktop GIS for Windows, in **English and Arabic**, based on QGIS 4.2.3.

**Download:** `Biruni-GIS-0.2.0-Setup.exe` below (Windows 10/11 64-bit, ~2.5 GB installed).
Check the download against `Biruni-GIS-0.2.0-Setup.exe.sha256`.

> The installer is not code-signed yet, so Windows SmartScreen may say *"Windows protected your PC"*:
> click **More info → Run anyway**. Signing through the SignPath Foundation is on the way.

### What's inside

- **Ribbon interface** in English and Arabic (full right-to-left), light/dark themes, search everything (Ctrl+K).
- **Projects are folders**: map, project database and data kept together; package as ZIP.
- **Coming from ArcGIS**: opens ArcGIS Pro projects (.aprx/.mapx/.lyrx) with layers, colours and labels, and ArcMap
  documents (.mxd) with their layers, groups and definition queries; reads and writes File Geodatabases; type an ArcGIS tool name and get the matching tool.
- **Excel/CSV to points** that finds the coordinate columns by itself (degrees, DMS, UTM, map links, Arabic digits).
- **Open data in one click**: OpenStreetMap, Sentinel-2 imagery, 30 m elevation, land cover, boundaries.
- **CAD and survey**: DXF to GIS, Helmert/affine to real coordinates, COGO, traverse, parcel subdivision.
- **Hydrology**: streams and watersheds from a DEM in one click (sinks filled, Strahler order, basin areas), plus the
  GRASS and SAGA hydrology tools (fill sinks, flow accumulation, stream extraction, TWI, LS factor).
- **Toolbox**: ~125 hand-picked tools in 18 categories; all ~1,370 QGIS, GDAL, GRASS and SAGA algorithms one click away.
- **Data quality**: check my data, topology rules, attribute rules, overlaps and gaps.
- **Networks**: closest facility, OD matrix, shortest path, service areas.
- **Maps and sharing**: print-ready map sheet in one click, web map in one HTML file, KMZ, QField round trip.
- **Safety and records**: project history (every save is a version), activity report and timesheet in Excel.
- **Phone GPS**: live track from a phone over Wi-Fi/Bluetooth/USB or from the phone's browser via a QR code
  (experimental).
- **Learn** panel with guided lessons and an ArcGIS ↔ Biruni dictionary. An Arabic PDF guide is attached to this
  release.

### Known limits

- ArcMap documents (.mxd) open with default styles: their symbology, labels and layout are stored in a closed format.
  Annotation, Esri basemap, server and SDE layers are skipped with a message.
- ECW and MrSID imagery support is not bundled (their licences forbid redistribution).

### Licence

GNU GPL v2 or later. See LICENSE and NOTICE.txt for QGIS, GRASS, SAGA and the other components.
ArcGIS, Excel, AutoCAD, Windows and QGIS are trademarks of their owners; Biruni GIS is independent and not affiliated
with or endorsed by them.

---

<div dir="rtl">

## Biruni GIS 0.2.0 - أول إصدار للجميع

برنامج نظم معلومات جغرافية مجاني ومفتوح المصدر للويندوز، **بالعربي والإنجليزي**، ومبني على QGIS 4.2.3.

**التحميل:** حمّل `Biruni-GIS-0.2.0-Setup.exe` من الملفات اللي تحت (ويندوز 10/11، 64 بت).

> المثبّت لسه مش موقّع رقميًا، فممكن ويندوز يقول *«Windows protected your PC»*: اضغط **More info** وبعدين **Run anyway**.

**أهم المزايا:** واجهة شريطية عربي/إنجليزي · فتح مشاريع ArcGIS Pro ومستندات ArcMap ‏(.mxd) وكتابة File Geodatabase · من Excel لنقاط ·
بيانات مفتوحة بضغطة (OSM، صور Sentinel-2، ارتفاعات 30 م) · من الأوتوكاد للـ GIS وCOGO وتقسيم الأراضي ·
**المجاري والأحواض من نموذج الارتفاعات بضغطة** وأدوات GRASS وSAGA للهيدرولوجي · فحص البيانات والطوبولوجي ·
لوحة خريطة جاهزة للطباعة · GPS الموبايل · تاريخ المشروع وتقرير النشاط · دروس وقاموس ArcGIS ودليل PDF بالعربي.

**ملاحظة:** مستندات ArcMap ‏(.mxd) بتتفتح بالطبقات والمجموعات، بس بالألوان الافتراضية.

الأسماء ArcGIS, Excel, AutoCAD, Windows, QGIS علامات تجارية لأصحابها. البرنامج مستقل، وغير تابع لهم ولا معتمد منهم.

</div>
