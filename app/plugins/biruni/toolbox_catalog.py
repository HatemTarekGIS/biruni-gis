"""The Biruni toolbox: a curated, task-oriented selection of the most useful
general GIS tools. Entries are Processing algorithm ids (QGIS or Biruni) or
"cmd:<name>" project commands. Missing algorithms are skipped at runtime, and
the full QGIS catalogue stays one click away."""

# (key, title, icon, [entries])
CATEGORIES = [
    ("project", "Project", "folder", [
        "cmd:activity_report", "cmd:timesheet", "cmd:history_versions", "cmd:store_temporary", "cmd:consolidate",
        "cmd:package", "biruni:exportlayers", "native:package",
    ]),
    ("opendata", "Free open data", "world-download", [
        "biruni:downloadosm", "biruni:downloadsentinel2", "biruni:downloaddem", "biruni:downloadlandcover",
        "biruni:downloadboundaries", "cmd:sample_data",
    ]),
    ("import", "Import", "table-import", [
        "cmd:smart_table", "cmd:open_arcgis", "biruni:cadtogis", "native:createpointslayerfromtable",
        "native:importphotos",
    ]),
    ("survey", "Survey & CAD", "compass", [
        "cmd:cogo", "biruni:traverse", "biruni:subdividepolygon", "biruni:similaritytransform", "biruni:cadtogis",
        "cmd:coords", "biruni:geometryattributes", "biruni:numberfeatures", "cmd:map_sheet",
    ]),
    ("conversion", "Convert", "transform", [
        "native:savefeatures", "native:reprojectlayer", "native:createpointslayerfromtable", "native:pointstopath",
        "qgis:linestopolygons", "native:polygonstolines", "native:polygonize", "native:explodelines",
        "native:multiparttosingleparts", "native:collect", "native:centroids", "native:pointonsurface",
        "native:extractvertices", "native:exporttospreadsheet", "native:dxfexport", "gdal:convertformat",
    ]),
    ("overlay", "Overlay", "layers-intersect-2", [
        "native:clip", "native:intersection", "native:union", "native:difference", "native:symmetricaldifference",
        "native:dissolve", "native:splitwithlines", "native:mergevectorlayers",
    ]),
    ("proximity", "Proximity", "circles", [
        "native:buffer", "native:multiringconstantbuffer", "native:joinbynearest", "native:shortestline",
        "native:hublines", "qgis:distancematrix", "native:voronoipolygons", "native:convexhull",
        "native:minimumboundinggeometry",
    ]),
    ("select", "Select & extract", "filter", [
        "native:extractbyattribute", "native:extractbyexpression", "native:extractbylocation",
        "native:selectbylocation", "native:splitvectorlayer", "native:randomextract",
    ]),
    ("join", "Join & attributes", "link", [
        "native:joinattributestable", "native:joinattributesbylocation", "native:joinbylocationsummary",
        "native:fieldcalculator", "native:refactorfields", "native:retainfields", "native:renametablefield",
        "native:addautoincrementalfield", "biruni:numberfeatures", "native:aggregate",
    ]),
    ("measure", "Measure & geometry", "ruler-measure", [
        "biruni:geometryattributes", "native:pointsalonglines", "native:densifygeometriesgivenaninterval",
        "native:splitlinesbylength", "native:simplifygeometries", "native:smoothgeometry", "native:offsetline",
        "native:extendlines", "native:linesubstring", "native:lineintersections", "native:rotatefeatures",
        "native:translategeometry", "native:subdivide",
    ]),
    ("statistics", "Statistics & patterns", "chart-bar", [
        "qgis:basicstatisticsforfields", "qgis:statisticsbycategories", "native:countpointsinpolygon",
        "native:sumlinelengths", "native:nearestneighbouranalysis", "native:meancoordinates",
        "native:heatmapkerneldensityestimation", "native:kmeansclustering", "native:dbscanclustering",
    ]),
    ("quality", "Quality & topology", "shield-check", [
        "cmd:check_data", "biruni:topologycheck", "cmd:attribute_rules", "biruni:overlapsgaps", "native:checkvalidity", "native:fixgeometries", "native:deleteduplicategeometries",
        "native:removeduplicatevertices", "native:removenullgeometries", "native:snapgeometries",
        "native:deleteholes",
    ]),
    ("raster", "Raster & terrain", "mountain", [
        "native:slope", "native:aspect", "native:hillshade", "gdal:contour", "gdal:viewshed",
        "gdal:cliprasterbymasklayer", "gdal:cliprasterbyextent", "gdal:warpreproject", "gdal:merge",
        "native:reclassifybytable", "native:zonalstatisticsfb", "native:rastersampling", "gdal:fillnodata",
        "gdal:polygonize", "gdal:rasterize", "qgis:idwinterpolation", "qgis:tininterpolation",
    ]),
    ("hydrology", "Hydrology", "droplet", [
        "biruni:streamsbasins", "sagang:fillsinkswangliu", "grass:r.fill.dir", "grass:r.watershed",
        "sagang:flowaccumulationtopdown", "grass:r.stream.extract", "sagang:channelnetworkanddrainagebasins",
        "sagang:strahlerorder", "sagang:watershedbasins", "grass:r.water.outlet", "sagang:sinkdrainageroutedetection",
        "sagang:topographicwetnessindexonestep", "sagang:lsfactoronestep", "grass:r.lake",
    ]),
    ("network", "Networks", "route", [
        "biruni:closestfacility", "biruni:odmatrix", "native:shortestpathpointtopoint", "native:shortestpathpointtolayer", "native:serviceareafrompoint",
    ]),
    ("gps", "GPS & field", "device-mobile-pin", [
        "cmd:phone_gps", "biruni:analysetrack", "cmd:qfield_send", "cmd:qfield_back", "native:importphotos",
    ]),
    ("share", "Share", "world-share", [
        "cmd:map_sheet", "cmd:web_map", "cmd:export_kmz", "cmd:portfolio", "cmd:package",
    ]),
    ("cartography", "Grids & maps", "grid-3x3", [
        "native:creategrid", "native:polygonfromlayerextent", "native:printlayouttopdf",
        "native:printlayouttoimage", "native:atlaslayouttopdf", "native:batchnominatimgeocoder",
        "native:importphotos",
    ]),
]

# Plain names for GRASS / SAGA tools whose own names are module codes or English only
# (the original name stays searchable).
LABELS = {
    "grass:r.fill.dir": "Fill sinks (GRASS)",
    "grass:r.watershed": "Flow direction, accumulation and basins (GRASS)",
    "grass:r.stream.extract": "Extract streams (GRASS)",
    "grass:r.water.outlet": "Watershed above an outlet point (GRASS)",
    "grass:r.lake": "Fill a lake to a water level (GRASS)",
    "sagang:fillsinkswangliu": "Fill sinks - Wang & Liu (SAGA)",
    "sagang:flowaccumulationtopdown": "Flow accumulation (SAGA)",
    "sagang:channelnetworkanddrainagebasins": "Channel network and drainage basins (SAGA)",
    "sagang:strahlerorder": "Strahler order (SAGA)",
    "sagang:watershedbasins": "Watershed basins (SAGA)",
    "sagang:sinkdrainageroutedetection": "Sink drainage routes (SAGA)",
    "sagang:topographicwetnessindexonestep": "Topographic wetness index - TWI (SAGA)",
    "sagang:lsfactoronestep": "LS factor - soil erosion (SAGA)",
}

# Project commands shown in the toolbox: name -> (label, icon, help, ProjectManager method)
COMMANDS = {
    "phone_gps": ("Track a phone (live GPS)", "device-mobile-pin",
                  "Connects a phone or GNSS receiver over Wi-Fi, Bluetooth or USB, shows the live position on "
                  "the map and records tracks - points, the track line with distance and speed, and stops - "
                  "into the project.", "open_phone_gps"),
    "activity_report": ("Project activity report (Excel)", "report-analytics",
                        "Exports everything recorded about the work on this project - working sessions and "
                        "active time, features added, reshaped, edited and deleted per layer and per day, tools "
                        "used, users - as an Excel workbook with a summary and charts.", "activity_report"),
    "store_temporary": ("Save temporary layers into the project", "database-import",
                        "Moves every temporary (scratch) layer into the project database so it is kept with "
                        "the project.", "store_temporary_layers"),
    "timesheet": ("Timesheet (hours and work done)", "clock-dollar",
                  "Turns the work recorded in this project into a timesheet for a client: active hours per day, "
                  "what was done each day and the amount at your hourly rate, as an Excel sheet.", "timesheet"),
    "history_versions": ("Project history (versions)", "history-toggle",
                         "Every save keeps a version of the project and its database. Put the project back the "
                         "way it was an hour, a day or a week ago.", "history_versions"),
    "sample_data": ("Sample world data", "world",
                    "Adds the bundled world map - countries, cities, rivers and lakes - for practice.",
                    "sample_data"),
    "smart_table": ("Excel / CSV to points", "table-import",
                    "Turns a spreadsheet into points: the coordinate columns are found by themselves - degrees, "
                    "degrees-minutes-seconds, UTM, map links (Google Maps, WhatsApp) or WKT, with Arabic digits "
                    "and headers.", "smart_table"),
    "open_arcgis": ("Open an ArcGIS project (Pro or ArcMap)", "file-arrow-right",
                    "Opens ArcGIS Pro files (.aprx, .mapx, .lyrx) with the layers, groups, colours, labels and "
                    "definition queries of the maps, and ArcMap documents (.mxd) with their layers, groups, "
                    "visibility and definition queries in default styles - with the data they point to.",
                    "open_arcgis"),
    "cogo": ("COGO - bearings and distances", "compass",
             "Draw a parcel or a line from bearings and distances (a deed, a field book) with a live preview, "
             "misclosure and precision, and the compass rule adjustment.", "cogo"),
    "coords": ("Coordinate converter", "current-location",
               "Type a coordinate in any format - degrees, DMS, UTM, MGRS, map link - and see it in all the "
               "others; go to it or keep it as a point.", "coords"),
    "map_sheet": ("Map sheet in one step", "file-certificate",
                  "A finished print layout: title, coordinate frame, north arrow, scale bar, legend and - for a "
                  "selected parcel - its corner coordinates table. Arabic or English.", "map_sheet"),
    "check_data": ("Check my data", "stethoscope",
                   "Scans every layer of the project for real problems - invalid or duplicate geometries, missing "
                   "coordinate systems, swapped coordinates, slivers, overlaps, empty fields, unsaved temporary "
                   "layers - and fixes the safe ones.", "check_data"),
    "attribute_rules": ("Attribute rules (domains)", "list-check",
                        "Required fields, unique values, lists of allowed values, ranges and automatic values "
                        "(area, length, date, user, next number) for the active layer.", "attribute_rules"),
    "web_map": ("Publish a web map (one HTML file)", "world-share",
                "One HTML file with the chosen layers, colours, legend and pop-ups that opens in any browser - "
                "no server needed.", "web_map"),
    "export_kmz": ("Google Earth (KMZ)", "world-pin", "Saves layers with their colours in one KMZ file.",
                   "export_kmz"),
    "portfolio": ("Portfolio page", "id-badge-2",
                  "One web page with the project's maps and the work behind them.", "portfolio"),
    "qfield_send": ("Send to the field (QField)", "device-mobile-up",
                    "Prepares a copy of the project for QField, the free field app, with an optional offline "
                    "basemap.", "qfield_send"),
    "qfield_back": ("Bring back from the field", "device-mobile-down",
                    "Shows what was added, changed and deleted in the field copy and applies it to the project.",
                    "qfield_back"),
    "consolidate": ("Copy all data into the project folder", "folder-down",
                    "Copies every file-based layer that is stored outside the project folder into it (vectors "
                    "into the project database, rasters into the data folder) and points the layers to the "
                    "copies. The project then works on any computer.", "consolidate"),
    "package": ("Package project as ZIP", "file-zip",
                "Copies all data into the project folder, saves the project and compresses the whole folder "
                "into one ZIP file that you can send to someone else.", "package"),
}
