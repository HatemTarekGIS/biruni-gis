"""The Biruni toolbox: a curated, task-oriented selection of the most useful
general GIS tools. Entries are Processing algorithm ids (QGIS or Biruni) or
"cmd:<name>" project commands. Missing algorithms are skipped at runtime, and
the full QGIS catalogue stays one click away."""

# (key, title, icon, [entries])
CATEGORIES = [
    ("project", "Project", "folder", [
        "cmd:map_stops", "cmd:project_doctor", "cmd:who_locks", "cmd:session_script", "cmd:activity_report", "cmd:timesheet",
        "cmd:history_versions", "cmd:store_temporary", "cmd:consolidate",
        "cmd:package", "biruni:exportlayers", "biruni:exportphotos", "native:package",
    ]),
    ("office", "Engineering office", "briefcase", [
        "biruni:guesscrs", "biruni:wrongplace", "biruni:cadastrelookup", "biruni:deliveryspectemplate", "biruni:deliverycheck", "biruni:sealfiles", "biruni:checkseal", "biruni:comparelayers",
        "biruni:quantities", "biruni:deedtoparcel", "biruni:deedvssurvey", "biruni:parceldescription",
        "biruni:parcelbundle", "biruni:linestoplots", "biruni:parcelatlas", "cmd:sales_map", "cmd:sales_back",
        "biruni:setbackcheck", "biruni:eudrcheck",
        "biruni:scantoparcels", "biruni:arabicjoin", "biruni:whatsapplocations",
    ]),
    ("opendata", "Free open data", "world-download", [
        "biruni:downloadosm", "biruni:downloadoverture", "biruni:cadastrelookup", "biruni:downloadsentinel2", "biruni:downloaddem", "biruni:downloadlandcover",
        "biruni:downloadboundaries", "cmd:sample_data",
    ]),
    ("import", "Import", "table-import", [
        "cmd:smart_table", "cmd:open_arcgis", "biruni:cadtogis", "biruni:floorplantoindoor", "biruni:whatsapplocations",
        "biruni:opennationalformat", "biruni:exportnationalformat", "biruni:scantoparcels", "biruni:linestoplots",
        "cmd:quick_align", "biruni:georefpdf", "biruni:fieldtofinish",
        "biruni:landxmlimport",
        "native:createpointslayerfromtable", "native:importphotos",
    ]),
    ("survey", "Survey & CAD", "compass", [
        "cmd:create_features", "cmd:modify_features", "cmd:cogo", "biruni:deedtoparcel", "biruni:deedvssurvey", "biruni:parceldescription", "biruni:parcelbundle",
        "biruni:fieldtofinish", "biruni:groundgrid", "biruni:localization", "biruni:landxmlimport",
        "biruni:landxmlexport", "biruni:orthotiles", "biruni:traverse", "biruni:subdividepolygon",
        "biruni:similaritytransform", "biruni:cadtogis", "biruni:floorplantoindoor", "biruni:indoorroute",
        "cmd:coords", "biruni:geometryattributes", "biruni:numberfeatures", "cmd:map_sheet",
    ]),
    ("conversion", "Convert", "transform", [
        "native:savefeatures", "native:reprojectlayer", "biruni:guesscrs", "biruni:wrongplace", "native:createpointslayerfromtable", "native:pointstopath",
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
        "native:selectbylocation", "cmd:spatial_filter", "cmd:spatial_filter_clear", "native:splitvectorlayer",
        "native:randomextract",
    ]),
    ("join", "Join & attributes", "link", [
        "native:joinattributestable", "biruni:multijoin", "biruni:arabicjoin", "biruni:cleantable",
        "biruni:hijridates", "cmd:table_tools", "native:joinattributesbylocation", "native:joinbylocationsummary",
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
        "qgis:basicstatisticsforfields", "qgis:statisticsbycategories", "biruni:quantities", "native:countpointsinpolygon",
        "native:sumlinelengths", "native:nearestneighbouranalysis", "native:meancoordinates",
        "native:heatmapkerneldensityestimation", "native:kmeansclustering", "native:dbscanclustering",
    ]),
    ("quality", "Quality & topology", "shield-check", [
        "cmd:check_data", "cmd:crs_doctor", "biruni:cleantable", "biruni:fixtextencoding", "biruni:arabictextdoctor", "biruni:convertdates",
        "biruni:hijridates", "biruni:spellvalues", "biruni:deliverycheck", "biruni:comparelayers",
        "biruni:topologycheck",
        "cmd:attribute_rules", "cmd:data_charter", "cmd:charter_learn", "cmd:charter_dictionary", "biruni:overlapsgaps", "native:checkvalidity", "native:fixgeometries", "native:deleteduplicategeometries",
        "native:removeduplicatevertices", "native:removenullgeometries", "native:snapgeometries",
        "native:deleteholes",
    ]),
    ("raster", "Raster & terrain", "mountain", [
        "biruni:terrainstyle", "native:slope", "native:aspect", "native:hillshade", "gdal:contour", "gdal:viewshed", "biruni:georefpdf",
        "gdal:cliprasterbymasklayer", "gdal:cliprasterbyextent", "gdal:warpreproject", "gdal:merge",
        "native:reclassifybytable", "native:zonalstatisticsfb", "native:rastersampling", "gdal:fillnodata",
        "gdal:polygonize", "gdal:rasterize", "qgis:idwinterpolation", "qgis:tininterpolation", "biruni:lasvolume",
        "biruni:lasprofile",
    ]),
    ("hydrology", "Hydrology", "droplet", [
        "biruni:streamsbasins", "sagang:fillsinkswangliu", "grass:r.fill.dir", "grass:r.watershed",
        "sagang:flowaccumulationtopdown", "grass:r.stream.extract", "sagang:channelnetworkanddrainagebasins",
        "sagang:strahlerorder", "sagang:watershedbasins", "grass:r.water.outlet", "sagang:sinkdrainageroutedetection",
        "sagang:topographicwetnessindexonestep", "sagang:lsfactoronestep", "grass:r.lake",
    ]),
    ("pointcloud", "Point clouds & drones", "drone", [
        "biruni:lasground", "biruni:lasprofile", "biruni:lasvolume",
        "pdal:info", "pdal:classifyground", "pdal:filternoisestatistical", "pdal:heightabovegroundtriangulation",
        "pdal:exportrastertin", "pdal:exportraster", "pdal:clip", "pdal:thinbyradius", "pdal:merge",
        "pdal:reproject", "pdal:assignprojection", "pdal:createcopc", "pdal:boundary", "pdal:density",
        "pdal:compare", "pdal:virtualpointcloud",
    ]),
    ("network", "Networks", "route", [
        "biruni:closestfacility", "biruni:odmatrix", "native:shortestpathpointtopoint", "native:shortestpathpointtolayer", "native:serviceareafrompoint",
    ]),
    ("gps", "GPS & field", "device-mobile-pin", [
        "cmd:phone_gps", "cmd:field_photos", "biruni:analysetrack", "biruni:gnssquality", "biruni:whatsapplocations", "cmd:qfield_send", "cmd:qfield_back", "native:importphotos",
    ]),
    ("spatialstats", "Spatial statistics", "chart-dots-3", [
        "cmd:ss_advisor",
        "biruni:ssnearest", "biruni:ssripley", "biruni:sskde", "biruni:ssdbscan", "biruni:sscentre",
        "biruni:sscolocation", "biruni:ssmoran", "biruni:sslisa", "biruni:sshotspots", "biruni:ssincremental",
        "biruni:ssbivariate", "biruni:ssscale", "biruni:ssemerging", "biruni:ssrates",
        "biruni:ssols", "biruni:ssgwr",
    ]),
    ("thesis", "Thesis", "school", [
        "cmd:thesis_starter", "biruni:thesisstarter", "cmd:thesis_methodology", "biruni:thesismethodology", "cmd:thesis_figures", "biruni:thesisfigurestyle",
        "biruni:thesisfiguresexport", "cmd:thesis_defense",
    ]),
    ("share", "Share", "world-share", [
        "cmd:map_studio", "cmd:map_sheet", "cmd:map_book", "cmd:web_map", "cmd:publish_site", "cmd:live_share", "cmd:earth_tour", "cmd:review_send", "cmd:review_back", "cmd:sales_map", "cmd:sales_back", "cmd:export_kmz", "cmd:portfolio",
        "cmd:package", "biruni:exportphotos",
    ]),
    ("cartography", "Grids & maps", "grid-3x3", [
        "cmd:map_studio", "cmd:thematic", "cmd:color_preview", "biruni:terrainstyle", "biruni:fourcolors", "biruni:labelcluster", "biruni:mapindex", "cmd:map_book", "biruni:mapbook", "biruni:liveqr", "biruni:parcelatlas", "native:creategrid", "native:polygonfromlayerextent", "native:printlayouttopdf",
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
    "pdal:info": "Point cloud information",
    "pdal:classifyground": "Classify ground points (PDAL)",
    "pdal:filternoisestatistical": "Remove noise points",
    "pdal:heightabovegroundtriangulation": "Height above ground",
    "pdal:exportrastertin": "Point cloud to raster (triangulated)",
    "pdal:exportraster": "Point cloud to raster",
    "pdal:thinbyradius": "Thin points (by distance)",
    "pdal:createcopc": "Convert to COPC (fast viewing)",
    "pdal:compare": "Compare two point clouds (change)",
    "pdal:virtualpointcloud": "Join tiles into one virtual point cloud",
}

# Project commands shown in the toolbox: name -> (label, icon, help, ProjectManager method)
COMMANDS = {
    "phone_gps": ("Track a phone (live GPS)", "device-mobile-pin",
                  "Connects a phone or GNSS receiver over Wi-Fi, Bluetooth or USB, shows the live position on "
                  "the map and records tracks - points, the track line with distance and speed, and stops - "
                  "into the project.", "open_phone_gps"),
    "create_features": ("Create features from templates", "template",
                        "Pick a template - a layer with its values already filled, made by hand or one per category "
                        "of the layer's symbology - and a way to draw it: a point at a distance and direction from a "
                        "snapped point, points every few metres along a line, right-angle lines and polygons, arcs "
                        "inside a line (stored as true curves where the layer allows), rectangles, circles, ellipses, "
                        "regular polygons, tracing along features, freehand, a polygon that fills the space between "
                        "existing ones, or a click inside an area closed by lines. While drawing, type L for a length, "
                        "A for a direction (N45°30'E works), D for a deflection, X / Y for coordinates, P / R for "
                        "parallel / perpendicular to a segment; Arabic digits and units like 25.4 m or 12 ft are read. "
                        "A box near the cursor shows the length, direction, total and area. Each new feature is one "
                        "Undo step.", "create_features"),
    "modify_features": ("Modify features", "edit/modify-features",
                        "One pane with every tool that changes features, searchable, with favourites: align edges, "
                        "move / rotate / scale with typed values, repeat copy with rotation, copy parallel, fillet, "
                        "split into parts or by a line, two-point cut, reshape, extend / trim, generalize and smooth "
                        "with a preview, shared-edge reshape, merge with attribute rules, clip, fill the gaps between "
                        "polygons, transfer attributes and clean-up (spikes, duplicate vertices, small gaps). Each "
                        "apply is checked first and is one Undo step.", "modify_features"),
    "map_book": ("Map Book (map series / atlas)", "mapbook/map-book",
                 "A book of map pages in one PDF: pages from an automatic grid that keeps only the cells with data, an index layer, strips along a road, one page per feature or your Map Stops - each at the largest scale that fits, with a key map, arrows to the next pages and a QR code; a cover, contents and a name index; bookmarks and arrows that jump to their pages.",
                 "map_book"),
    "map_stops": ("Map stops (saved views with pictures)", "stops/map-route",
                  "Saves the places on the map you come back to - the view with its rotation and scale, the layers that were on, the time, a note, a colour and a picture - in the project or for all your projects. Alt+1…9 go to the first nine, Alt+← / Alt+→ go back and forward through the views you visited, and the stops play as a tour or go to a phone as a QR code. Every stop is also a spatial bookmark; export for ArcGIS Pro, Google Earth and other GIS programs.",
                  "manage_stops"),
    "data_charter": ("Data charter (fields, lists, kinds, rules)", "license",
                     "One place per layer for its fields with Arabic and English names, shared value lists, kinds, "
                     "rules across fields and linked choices - learnt from the data if you like, with a live health "
                     "score, a bilingual data dictionary and a phone form.", "data_charter"),
    "charter_learn": ("Learn the charter from the data", "sparkles",
                      "Reads a layer and proposes value lists, one spelling for each value, ranges with suspicious "
                      "numbers, required and unique fields, kinds and linked choices.", "charter_learn"),
    "charter_dictionary": ("Data dictionary (Word / HTML)", "book",
                           "Documents every layer's fields, names, types, units, value lists, kinds, rules and health "
                           "in Arabic and English for a client or a supervisor.", "charter_dictionary"),
    "ss_advisor": ("Spatial statistics advisor", "zoom-question",
                   "Pick your question in plain words - are my points clustered? where are the hot spots? what explains this value? - and the advisor picks the tool, checks your data first and says what to watch out "
                   "for, then opens the tool with its settings filled in.", "ss_advisor"),
    "thesis_starter": ("Study area starter pack", "package",
                       "A ready project for a graduation project's study area in minutes: boundaries, OpenStreetMap "
                       "data, elevation with slope and contours, land cover with areas, satellite images and NDVI, "
                       "population, a summary of the area, the data sources with citations and the location map "
                       "of chapter 1.", "thesis_starter"),
    "thesis_methodology": ("Methodology chapter (written for you)", "list-details",
                           "Writes the methodology chapter of a thesis from what was done in this project - the data "
                           "and its sources, every tool with its settings, in order - in Arabic or English, with the "
                           "workflow diagram, the data sources table and the references.", "thesis_methodology"),
    "thesis_figures": ("Thesis map figures", "list-numbers",
                       "Every map of the thesis in your university's style: coordinate grid, north arrow, scale bar, "
                       "legend, source line and the numbered caption. Numbers follow the chapters and update when a "
                       "figure is moved or deleted; export all figures at 300 dpi and the list of figures in one "
                       "click; share your style with classmates.", "thesis_figures"),
    "thesis_defense": ("Defence presentation (PowerPoint, phone page, PDF)", "presentation",
                       "The slides for the thesis defence from the project's maps and results: PowerPoint with "
                       "speaker notes, one HTML page with an interactive map for the examiners' phones, and a PDF.",
                       "thesis_defense"),
    "live_share": ("Share my map live (link and QR code)", "antenna",
                   "Shares the map on your screen with a link and a QR code - the same layers and styling, updated "
                   "live while you work. Whoever opens it on a phone or any browser can move around, tap a feature to "
                   "see its details and see where they are. Nothing is uploaded: the map goes directly from this "
                   "computer to them, and stopping ends the link.", "live_share"),
    "field_photos": ("Field photos live from a phone", "camera",
                     "Someone in the street takes photos with a phone - no app, just a QR code - and each one "
                     "appears on this computer's map at once with its position, accuracy, direction, kind and "
                     "note. The photos are saved on this computer only; nearby over Wi-Fi, or from anywhere over "
                     "the internet without an account. Photos taken without signal are sent when it is back.",
                     "field_photos"),
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
    "review_send": ("Send the map for review", "message-2-share",
                    "One web page your client or supervisor opens in any browser - no account, no server. They "
                    "pin comments on the map (note, error, question, approved) and send them back as a small "
                    "file, even over WhatsApp.", "review_send"),
    "review_back": ("Bring back review comments", "message-2-down",
                    "Opens the comment files sent back by reviewers as a layer of comments with author, date, "
                    "type and status, without duplicates; replies can be sent back to the reviewer.",
                    "review_back"),
    "thematic": ("Thematic map wizard", "chart-dots-3",
                 "Choose a field and say what it should show: Biruni recognises the kind of data (categories, "
                 "counts, rates, change) and recommends the right map - choropleth with round class breaks, "
                 "proportional symbols, dot density, bivariate, hexagons, flows, small multiples - warns about "
                 "classic mistakes and can always undo.", "thematic"),
    "color_preview": ("Check map colours", "color-filter",
                      "See the map as people with colour blindness see it, in grey and as a print proof; colour "
                      "categories by their meaning or by an official land-use standard; take colours from a logo.",
                      "color_preview"),
    "map_studio": ("Map Studio (smart map production)", "wand",
                   "Say what the map is for - deed sketch, report or thesis figure, wall map, slide, social post "
                   "- and get a finished layout: page, round scale, clean legend, credits, inset, all placed where "
                   "your data isn't. The map doctor checks it before printing and fixes problems in one click; "
                   "one map exports to PDF, PNG, slide and social sizes at once.", "map_studio"),
    "crs_doctor": ("Why don't my layers line up?", "map-question",
                   "Checks every layer's coordinate system: missing or wrong systems, swapped coordinates, points "
                   "at 0,0, measuring in degrees, and how far apart the possible datum transformations put your "
                   "data - each with a one-click fix.", "crs_doctor"),
    "publish_site": ("Publish the map as a website", "world-upload",
                     "Writes a website folder (map page, data, a short README) ready to drag onto Netlify or "
                     "GitHub Pages. Big layers become fast map tiles. Nothing is uploaded by Biruni.",
                     "publish_site"),
    "earth_tour": ("Google Earth tour", "plane-departure",
                   "A KMZ that flies Google Earth from feature to feature (or along a route), with colours, names "
                   "and details - for Google Earth Pro users.", "earth_tour"),
    "project_doctor": ("Project doctor", "heart-rate-monitor",
                       "Finds and repairs layers whose files moved, tells you which projects use a file, renames "
                       "a whole project folder safely and lists what may cause trouble.", "project_doctor"),
    "who_locks": ("Who is using this file?", "lock-search",
                  "Shows which program holds a shapefile, GeoPackage, geodatabase or Excel file open, so you know "
                  "what to close.", "who_locks"),
    "session_script": ("Turn my steps into a script or model", "file-code",
                       "Writes the tools you ran (today, this session or this week) as a Python script or a "
                       "Graphical Modeler model you can run again on other data.", "session_script"),
    "quick_align": ("Place an image on the map by hand", "photo-pin",
                    "Lays a scan, PDF page or photo half-transparent on the map; move, turn and resize it until "
                    "it fits, or match two points, then save it as a georeferenced GeoTIFF.", "quick_align"),
    "sales_map": ("Plot sales map", "map-dollar",
                  "One web page for a developer's sales team: plots coloured by status (available, reserved, "
                  "sold), search, prices, and a WhatsApp button per plot. An editor copy lets one coordinator "
                  "update statuses and send them back.", "sales_map"),
    "sales_back": ("Bring back plot statuses", "home-dollar",
                   "Reads the statuses file saved from the editor copy of the sales map (or an Excel sheet) into "
                   "the plots layer, in one undoable step.", "sales_back"),
    "table_tools": ("Table tools (field order, highlight, sort)", "table-options",
                    "Put the fields of a layer in the order you want - in the table only or for good in the data, "
                    "with a backup - rename them, highlight empty, duplicate or out-of-range cells, sort numbers "
                    "naturally (2 before 10) and get suggestions while typing.", "table_tools"),
    "spatial_filter": ("Show only what is inside an area", "filter-cog",
                       "Select polygons (a district, a zone, a parcel) and every other layer shows only the "
                       "features inside them - like a spatial definition query. Nothing is deleted; Show "
                       "everything puts the layers back as they were.", "spatial_filter"),
    "spatial_filter_clear": ("Show everything again", "filter-off",
                             "Removes the area filter from all layers and restores their previous filters.",
                             "spatial_filter_clear"),
    "package": ("Package project as ZIP", "file-zip",
                "Copies all data into the project folder, saves the project and compresses the whole folder "
                "into one ZIP file that you can send to someone else.", "package"),
}
