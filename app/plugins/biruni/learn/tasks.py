"""«What do you want to do?» - everyday tasks in plain words (Arabic, Egyptian
and formal, and English) mapped to the tool that does them. The search box
shows them as "Task" results; search() ranks them by the words they share
with what was typed, after Arabic normalisation and light stemming.

Targets are command ids of the Biruni catalogue or Processing algorithm ids,
like learn/dictionary.py."""
import re

try:
    from ..arabic_text import normalise as _normalise
except ImportError:  # pragma: no cover - arabic_text ships with Biruni
    def _normalise(text, strip_prefixes=False):
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()

# (target, English phrasings, Arabic phrasings)
TASKS = [
    # ---- measure -------------------------------------------------------------------------------
    ("biruni:geometryattributes",
     ("calculate area", "area of parcels", "length of lines", "add x y coordinates to points",
      "calculate geometry"),
     ("احسب المساحه", "عايز اعرف مساحة القطع", "حساب مساحات المضلعات", "احسب اطوال الخطوط",
      "طول كل خط", "اضيف الاحداثيات للنقط", "حساب المساحات والاطوال", "اضافة حقل المساحه")),
    ("measure",
     ("measure a distance on the map", "ruler"),
     ("اقيس مسافه على الخريطه", "قياس المسافه بالماوس", "مسطره")),
    ("measure_area",
     ("measure an area on the map",),
     ("اقيس مساحه على الخريطه", "قياس مساحه بالماوس")),
    ("qgis:distancematrix",
     ("distance matrix", "distances between all points"),
     ("مصفوفة المسافات", "المسافات بين كل النقط", "احسب المسافه بين كل نقطه والتانيه")),
    ("native:joinbynearest",
     ("distance from points to roads", "nearest feature", "closest road to each point", "nearest neighbour join"),
     ("احسب المسافه بين النقط والطرق", "اقرب طريق لكل نقطه", "المسافه لاقرب عنصر", "اقرب مبني لكل نقطه",
      "ربط باقرب عنصر")),
    ("native:shortestline",
     ("shortest line between two layers",),
     ("اقصر خط بين طبقتين", "اقصر مسافه بين طبقتين")),
    ("native:lineintersections",
     ("points where lines cross", "line intersections"),
     ("نقط تقاطع الخطوط", "اماكن تقاطع الطرق")),
    # ---- overlay -------------------------------------------------------------------------------
    ("native:buffer",
     ("buffer", "zone around features", "area within a distance"),
     ("اعمل نطاق حول النقط", "بافر", "حرم الطريق", "منطقه حول العنصر بمسافه", "نطاق بمسافه حول الخطوط")),
    ("native:multiringconstantbuffer",
     ("multiple ring buffer", "rings around a point"),
     ("حلقات حول نقطه", "نطاقات متعدده حول")),
    ("native:clip",
     ("clip a layer by a boundary", "cut layer to area"),
     ("اقص طبقه بحدود", "قص الطبقه بحدود منطقه", "اقطع البيانات على حدود", "كليب")),
    ("native:intersection",
     ("intersect two layers", "common part of two layers"),
     ("تقاطع طبقتين", "الجزء المشترك بين طبقتين", "انترسكت")),
    ("native:union",
     ("union of two layers",),
     ("اتحاد طبقتين", "يونيون")),
    ("native:difference",
     ("erase", "remove the overlapping part", "difference of layers"),
     ("امسح الجزء المتداخل", "اطرح طبقه من طبقه", "الفرق بين طبقتين", "شيل الجزء اللي جوه طبقه تانيه")),
    ("native:dissolve",
     ("dissolve", "merge polygons by field", "remove inner boundaries"),
     ("ادمج القطع حسب حقل", "دمج المضلعات المتجاوره", "ذوبان الحدود", "شيل الحدود الداخليه")),
    ("native:mergevectorlayers",
     ("merge layers", "combine two layers into one", "append layers"),
     ("ادمج طبقتين", "اجمع الطبقات في طبقه واحده", "دمج الطبقات", "ضم طبقه على طبقه")),
    ("native:splitwithlines",
     ("split polygons with lines",),
     ("قسم المضلعات بخطوط", "قطع القطع بخط")),
    # ---- join and attributes -------------------------------------------------------------------
    ("native:joinattributestable",
     ("join a table to a layer by field", "join excel to layer"),
     ("اربط جدول بطبقه", "ربط الاكسل بالطبقه بحقل", "جوين بالحقل", "اضيف بيانات جدول للطبقه")),
    ("biruni:arabicjoin",
     ("join by similar names", "fuzzy join"),
     ("اربط اسماء مكتوبه بشكل مختلف", "ربط بالاسم العربي", "مطابقة الاسماء", "الاسماء مش متطابقه")),
    ("native:joinattributesbylocation",
     ("spatial join", "attributes by location", "which district each point is in"),
     ("ربط مكاني", "انقل البيانات حسب الموقع", "اعرف كل نقطه في انهي حي", "كل قطعه تابعه لانهي منطقه")),
    ("native:countpointsinpolygon",
     ("count points in polygons",),
     ("عدد النقط في كل مضلع", "كام نقطه في كل حي", "عد النقاط جوه المضلعات")),
    ("native:joinbylocationsummary",
     ("summarize within polygons",),
     ("ملخص حسب الموقع", "مجموع القيم داخل كل مضلع")),
    ("native:sumlinelengths",
     ("sum line lengths in polygons",),
     ("مجموع اطوال الخطوط في كل مضلع", "اطوال الطرق في كل حي")),
    ("field_calc",
     ("field calculator", "calculate a field", "update values of a column"),
     ("احسب حقل", "حاسبة الحقول", "اعدل قيم عمود", "املا حقل بمعادله")),
    ("native:refactorfields",
     ("rename fields", "change field type", "reorder fields"),
     ("غير اسماء الحقول", "رتب الحقول", "غير نوع الحقل", "تعديل هيكل الجدول")),
    ("native:addautoincrementalfield",
     ("add a serial number field",),
     ("حقل رقم مسلسل", "اضيف رقم تسلسلي")),
    ("biruni:numberfeatures",
     ("number features in order",),
     ("رقم القطع", "ترقيم العناصر بالترتيب", "اعطي ارقام مسلسله للقطع")),
    ("attribute_table",
     ("open the attribute table",),
     ("جدول البيانات", "افتح جدول الطبقه", "جدول السمات")),
    ("attribute_rules",
     ("domains", "allowed values for a field", "required field"),
     ("قيم مسموحه للحقل", "دومين", "حقل اجباري", "قواعد الحقول")),
    # ---- select and extract --------------------------------------------------------------------
    ("select_expr",
     ("select by attributes", "select by value"),
     ("اختار بشرط", "تحديد حسب قيمه", "حدد العناصر اللي قيمتها")),
    ("select_location",
     ("select by location",),
     ("اختار حسب الموقع", "حدد العناصر اللي جوه منطقه")),
    ("native:extractbyattribute",
     ("extract by attribute",),
     ("استخرج حسب قيمه", "طلع العناصر اللي قيمتها في طبقه جديده")),
    ("native:extractbylocation",
     ("extract by location",),
     ("استخرج اللي جوه منطقه", "طلع العناصر اللي جوه حدود")),
    ("native:splitvectorlayer",
     ("split layer by attribute",),
     ("قسم الطبقه حسب حقل", "طبقه لكل نوع", "افصل الطبقه لطبقات")),
    # ---- convert -------------------------------------------------------------------------------
    ("smart_table",
     ("excel to points", "csv to points", "coordinates in excel", "xy table to point"),
     ("حول الاكسل لنقط", "اكسل لنقاط", "ملف اكسل فيه احداثيات", "حول csv لنقط", "اعرض جدول احداثيات على الخريطه",
      "نزل احداثيات الاكسل على الخريطه")),
    ("native:exporttospreadsheet",
     ("export table to excel",),
     ("صدر الجدول لاكسل", "حول الطبقه لاكسل", "طلع البيانات في اكسل")),
    ("export_layer",
     ("save layer as shapefile", "convert shapefile to geopackage", "export layer to kml"),
     ("احفظ الطبقه شيب فايل", "حول الشيب فايل لجيوباكدج", "تصدير الطبقه", "احفظ نسخه من الطبقه",
      "حول لكي ام ال")),
    ("biruni:exportlayers",
     ("export all layers at once",),
     ("صدر كل الطبقات مره واحده", "تصدير كل الطبقات لفولدر", "حول كل الطبقات شيب")),
    ("cad_to_gis",
     ("dxf to shapefile", "autocad to gis", "cad drawing to layers"),
     ("حول الاوتوكاد لشيب", "حول dxf لجي اي اس", "ملف كاد لطبقات", "افتح ملف الاوتوكاد", "تحويل الكاد")),
    ("native:dxfexport",
     ("export to autocad dxf",),
     ("صدر لاوتوكاد", "حول الطبقات dxf", "تصدير كاد")),
    ("dwg_import",
     ("open a dwg file",),
     ("افتح ملف dwg", "استيراد dwg")),
    ("cad_georef",
     ("move a drawing to real coordinates", "helmert transformation"),
     ("انقل الرسمه لاحداثيات حقيقيه", "اسقاط ملف الكاد", "حرك الرسم لمكانه الصحيح", "تحويل هلمرت")),
    ("georeferencer",
     ("georeference an image", "georeference a scanned map"),
     ("اسقاط صوره", "جيوريفرنس", "اعطي احداثيات لصوره", "تسجيل صوره على الخريطه")),
    ("biruni:scantoparcels",
     ("scanned plan to parcels",),
     ("صورة مخطط لقطع", "مخطط سكانر لقطع", "رقمنة مخطط ممسوح", "حول المخطط المصور لقطع")),
    ("native:reprojectlayer",
     ("change coordinate system", "reproject layer", "convert wgs84 to utm"),
     ("غير نظام الاحداثيات", "حول من wgs لutm", "اعادة اسقاط الطبقه", "تحويل احداثيات الطبقه",
      "غير الاسقاط")),
    ("native:assignprojection",
     ("define projection", "set the layer's crs"),
     ("حدد نظام الاحداثيات للطبقه", "الطبقه ملهاش اسقاط", "تعريف الاسقاط")),
    ("biruni:guesscrs",
     ("which coordinate system is this", "unknown projection", "layer in the wrong place"),
     ("مش عارف نظام الاحداثيات", "اعرف الاسقاط", "الاحداثيات بعيده عن مكانها", "الطبقه في مكان غلط",
      "انهي نظام احداثيات", "مش عارف الطبقه على انهي اسقاط", "الاسقاط مجهول")),
    ("coords",
     ("convert coordinates", "go to coordinates", "degrees minutes seconds"),
     ("حول احداثيات", "روح لاحداثيات", "حول من درجات لutm", "درجات دقائق ثواني", "دور على احداثي")),
    ("native:centroids",
     ("centroids", "point in the middle of each polygon"),
     ("نقطه في منتصف كل مضلع", "مركز القطعه", "سنترويد")),
    ("native:pointonsurface",
     ("point inside each polygon",),
     ("نقطه جوه كل مضلع",)),
    ("native:polygonstolines",
     ("polygons to lines",),
     ("مضلعات لخطوط", "حول المضلع لخط")),
    ("native:polygonize",
     ("lines to polygons", "close lines into polygons"),
     ("خطوط لمضلعات", "اقفل الخطوط مضلعات", "حول الخطوط لقطع")),
    ("native:extractvertices",
     ("polygon corners to points", "extract vertices"),
     ("رؤوس المضلع", "استخرج نقط الاركان", "احداثيات اركان القطعه")),
    ("native:pointstopath",
     ("connect points into a line",),
     ("نقط لخط", "وصل النقط بخط")),
    ("native:multiparttosingleparts",
     ("multipart to singlepart",),
     ("فك الاجزاء", "مالتي بارت لسنجل")),
    ("native:simplifygeometries",
     ("simplify lines",),
     ("بسط الخطوط", "قلل النقط")),
    ("native:smoothgeometry",
     ("smooth lines",),
     ("نعم الخطوط", "تنعيم")),
    ("native:pointsalonglines",
     ("points along a line every distance",),
     ("نقط على الخط كل مسافه", "محطات على الطريق كل")),
    ("native:splitlinesbylength",
     ("split lines by length",),
     ("قسم الخط لاطوال", "قطع الخطوط كل مسافه")),
    ("native:offsetline",
     ("parallel line", "offset a line"),
     ("خط موازي", "ازاحة خط")),
    ("native:translategeometry",
     ("move a layer by a distance",),
     ("حرك الطبقه بمسافه", "ازاحه الطبقه")),
    ("native:rotatefeatures",
     ("rotate features",),
     ("لف العناصر", "دوران الطبقه")),
    ("native:voronoipolygons",
     ("thiessen polygons", "voronoi"),
     ("مضلعات ثيسن", "فورونوي")),
    ("native:convexhull",
     ("convex hull",),
     ("غلاف محدب",)),
    ("native:creategrid",
     ("create a grid",),
     ("شبكه مربعات", "جريد")),
    # ---- survey --------------------------------------------------------------------------------
    ("cogo",
     ("draw a parcel from bearings and distances", "cogo"),
     ("ارسم قطعه من الاطوال والزوايا", "كوجو", "انحرافات ومسافات", "رسم من الصك بالزوايا")),
    ("biruni:traverse",
     ("traverse from a table",),
     ("مضلع من جدول", "ترافرس", "ميزانيه مضلعه")),
    ("biruni:subdividepolygon",
     ("subdivide a parcel", "split a parcel into equal parts"),
     ("قسم قطعه", "تقسيم ارض", "فرز قطعه لاجزاء متساويه", "قسم الارض بمساحات")),
    ("biruni:deedtoparcel",
     ("parcel from deed text",),
     ("ارسم القطعه من الصك", "حدود الصك", "حول الصك لقطعه", "نص الصك")),
    ("biruni:deedvssurvey",
     ("compare deed with survey",),
     ("قارن الصك بالرفع", "مطابقة الصك", "فرق مساحة الصك عن الطبيعه")),
    # ---- quality -------------------------------------------------------------------------------
    ("check_data",
     ("check my data", "find errors in data"),
     ("افحص بياناتي", "فيه اخطاء في البيانات", "فحص الطبقات", "دور على الاخطاء")),
    ("native:fixgeometries",
     ("fix geometries", "repair invalid shapes"),
     ("صلح الاشكال", "الهندسه غير صالحه", "اصلاح المضلعات", "صلح الاخطاء الهندسيه")),
    ("biruni:overlapsgaps",
     ("find overlaps and gaps",),
     ("تداخل القطع", "فراغات بين القطع", "اعرف التداخلات", "القطع راكبه على بعض")),
    ("biruni:topologycheck",
     ("topology rules",),
     ("قواعد الطوبولوجي", "فحص طوبولوجي")),
    ("native:deleteduplicategeometries",
     ("delete duplicate features",),
     ("امسح المكرر", "عناصر مكرره", "حذف التكرار")),
    ("biruni:deliveryspectemplate",
     ("delivery spec template",),
     ("اعمل كراسة شروط", "نموذج متطلبات التسليم")),
    ("biruni:deliverycheck",
     ("check a delivery against the spec",),
     ("افحص التسليم", "كراسة الشروط", "تسليم البيانات للجهه", "جاهزية التسليم")),
    ("biruni:comparelayers",
     ("compare two versions of a layer", "what changed"),
     ("قارن نسختين", "ايه اللي اتغير", "الفرق بين ملفين", "مقارنة تسليمين")),
    ("biruni:quantities",
     ("bill of quantities", "quantities and costs"),
     ("حصر كميات", "مستخلص", "اطوال البردورات ومساحات الاسفلت", "حساب الكميات والاسعار")),
    ("biruni:setbackcheck",
     ("setbacks and building coverage",),
     ("الارتدادات", "نسبة البناء", "مخالفات البناء", "فحص الارتداد")),
    # ---- data and field ------------------------------------------------------------------------
    ("biruni:whatsapplocations",
     ("whatsapp locations to points",),
     ("لوكيشنات الواتساب", "مواقع من الواتساب", "حول شات الواتساب لنقط")),
    ("biruni:downloadosm",
     ("download openstreetmap", "roads and buildings from osm"),
     ("نزل بيانات openstreetmap", "طرق ومباني من osm", "نزل الشوارع والمباني")),
    ("biruni:downloadsentinel2",
     ("satellite image", "sentinel", "ndvi"),
     ("صوره قمر صناعي", "سنتينل", "مرئيه فضائيه", "صوره فضائيه حديثه")),
    ("biruni:downloaddem",
     ("elevation model", "download dem"),
     ("نموذج ارتفاعات", "ارتفاعات الارض dem", "نزل ارتفاعات")),
    ("biruni:downloadlandcover",
     ("land cover", "land use"),
     ("غطاء الارض", "استخدامات الاراضي")),
    ("biruni:downloadboundaries",
     ("administrative boundaries",),
     ("حدود الدول", "حدود المحافظات", "حدود اداريه")),
    ("native:importphotos",
     ("geotagged photos to points",),
     ("صور فيها موقع", "صور جي بي اس لنقط", "حول الصور لنقط")),
    ("native:batchnominatimgeocoder",
     ("addresses to points", "geocode"),
     ("عناوين لنقط", "جيوكود")),
    ("qfield_send",
     ("send to the field", "field work on a phone"),
     ("الشغل الميداني", "نزل البيانات على الموبايل", "كيو فيلد")),
    ("qfield_back",
     ("bring back field edits",),
     ("رجع شغل الموبايل", "استرجاع بيانات الميدان")),
    ("phone_gps",
     ("phone gps", "track my position"),
     ("gps الموبايل", "تتبع موقعي", "اعرف مكاني")),
    ("biruni:analysetrack",
     ("analyse a gpx track",),
     ("مسار gpx", "تحليل مسار", "تحليل تراك")),
    # ---- terrain and hydrology -----------------------------------------------------------------
    ("gdal:contour",
     ("contour lines",),
     ("خطوط كنتور", "استخرج الكنتور", "خطوط الارتفاع المتساوي")),
    ("native:slope",
     ("slope",),
     ("الميول", "انحدار الارض")),
    ("native:hillshade",
     ("hillshade",),
     ("تظليل التضاريس", "هيلشيد")),
    ("biruni:streamsbasins",
     ("streams and watersheds", "drainage basins"),
     ("مجاري السيول", "الاوديه", "احواض التصريف", "مسارات المياه")),
    ("gdal:viewshed",
     ("viewshed", "what can be seen from a point"),
     ("مدي الرؤيه", "المناطق المرئيه من نقطه")),
    ("gdal:cliprasterbymasklayer",
     ("clip a raster", "clip an image by a boundary"),
     ("قص صوره بحدود", "قص راستر")),
    ("gdal:merge",
     ("merge rasters", "mosaic"),
     ("ادمج صور", "موزاييك", "دمج الصور الجويه")),
    ("native:zonalstatisticsfb",
     ("zonal statistics",),
     ("احصائيات الراستر داخل المضلعات", "متوسط الارتفاع لكل قطعه")),
    ("native:rastersampling",
     ("raster values at points",),
     ("قيم الارتفاع للنقط", "اقرا قيمة الراستر عند النقط")),
    ("gdal:polygonize",
     ("raster to polygons",),
     ("راستر لمضلعات", "حول الصوره لمضلعات")),
    ("gdal:rasterize",
     ("polygons to raster",),
     ("مضلعات لراستر", "حول الطبقه لراستر")),
    ("qgis:idwinterpolation",
     ("interpolation", "surface from points"),
     ("استيفاء", "سطح من نقط")),
    # ---- statistics ----------------------------------------------------------------------------
    ("native:heatmapkerneldensityestimation",
     ("heatmap", "density"),
     ("خريطه حراريه", "كثافة النقط")),
    ("native:kmeansclustering",
     ("cluster points",),
     ("تجميع النقط", "عنقده")),
    ("qgis:basicstatisticsforfields",
     ("field statistics", "sum and average of a field"),
     ("احصائيات حقل", "متوسط ومجموع العمود")),
    ("qgis:statisticsbycategories",
     ("statistics by category",),
     ("احصائيات حسب الفئه", "مجموع حسب النوع")),
    # ---- networks ------------------------------------------------------------------------------
    ("closest_facility",
     ("closest facility", "nearest hospital"),
     ("اقرب مستشفي", "اقرب مرفق", "اقرب مدرسه بالطريق")),
    ("biruni:odmatrix",
     ("od matrix", "distances along roads"),
     ("مصفوفة المسافات على الطرق", "المسافه بالطريق بين النقط")),
    ("shortest_path",
     ("shortest path", "route between two points"),
     ("اقصر طريق", "مسار بين نقطتين")),
    ("service_area",
     ("service area",),
     ("منطقة الخدمه", "المسافه المقطوعه في وقت")),
    # ---- maps and sharing ----------------------------------------------------------------------
    ("map_sheet",
     ("print a map", "map sheet", "map to pdf"),
     ("اطبع خريطه", "لوحه خريطه", "اخراج خريطه للطباعه", "خريطه pdf", "طباعة لوحه")),
    ("new_layout",
     ("new print layout",),
     ("تصميم لوحه جديده", "لاي اوت")),
    ("native:atlaslayouttopdf",
     ("map series", "atlas"),
     ("اطلس", "خريطه لكل قطعه")),
    ("web_map",
     ("web map", "share a map in the browser"),
     ("خريطه ويب", "ابعت الخريطه لحد يفتحها في المتصفح")),
    ("export_kmz",
     ("google earth", "kmz"),
     ("جوجل ايرث", "kmz")),
    ("package",
     ("package the project as zip", "send the project to someone"),
     ("ابعت المشروع لحد", "zip للمشروع", "اضغط المشروع")),
    ("consolidate",
     ("copy all data into the project",),
     ("انسخ البيانات للمشروع", "اجمع البيانات في فولدر المشروع")),
    ("history_versions",
     ("restore an older version", "undo a save"),
     ("رجع نسخه قديمه", "استرجاع المشروع", "تراجع عن حفظ")),
    ("activity_report",
     ("activity report",),
     ("تقرير الشغل", "تقرير النشاط")),
    ("timesheet",
     ("timesheet", "hours worked"),
     ("ساعات الشغل", "تايم شيت", "فاتوره بالساعه")),
    # ---- project and interface -----------------------------------------------------------------
    ("basemap",
     ("basemap", "satellite background", "street map background"),
     ("خريطه اساس", "خلفيه للخريطه", "خريطة الشوارع في الخلفيه")),
    ("add_data",
     ("add data", "open a file", "add a layer"),
     ("افتح ملف", "اضف بيانات", "اضف طبقه")),
    ("open_arcgis",
     ("open an arcgis project", "open mxd", "open aprx"),
     ("افتح مشروع arcgis", "افتح ملف mxd", "افتح aprx")),
    ("new_project",
     ("new project",),
     ("مشروع جديد",)),
    ("learn",
     ("lessons", "learn biruni"),
     ("دروس", "اتعلم البرنامج", "شرح البرنامج")),
    ("language",
     ("change language",),
     ("غير اللغه", "البرنامج بالانجليزي")),
    ("theme",
     ("dark mode", "light theme"),
     ("الوضع الليلي", "الوضع الداكن")),
    ("report_problem",
     ("report a problem",),
     ("ابلغ عن مشكله", "البرنامج فيه مشكله")),
]

# Words that carry no task: "I want", "how", "please", articles …
STOP = {
    "عايز", "عاوز", "عايزه", "عاوزه", "اريد", "ابغي", "ابغا", "ابي", "نفسي", "ممكن", "ازاي", "ازاى", "كيف", "كيفيه",
    "طريقه", "اعمل", "اسوي", "سوي", "في", "من", "علي", "الي", "او", "و", "لكل", "كل", "انا", "حاجه", "شي",
    "ده", "دي", "دا", "هذا", "هذه", "هاذا", "بتاع", "بتاعه", "بتاعت", "اللي", "الذي", "التي", "عن", "مع",
    "لو", "يا", "بس", "برنامج", "اداه", "اداة", "بيه", "ليه", "عشان", "علشان", "لي", "لنا", "فين", "ايه",
    "the", "a", "an", "to", "of", "in", "on", "for", "how", "i", "want", "do", "my", "me", "can", "with", "and",
    "from", "by", "into", "is", "it", "each", "all", "please", "tool", "using",
}

# Words with the same meaning reduced to one key (after normalisation and stemming)
SYNONYMS = {
    "احسب": "حسب", "حساب": "حسب", "احسبلي": "حسب", "calculate": "حسب", "compute": "حسب",
    "اقص": "قص", "اقطع": "قص", "قطع": "قص", "clip": "قص", "cut": "قص",
    "حول": "حول", "تحويل": "حول", "حولي": "حول", "convert": "حول", "تحول": "حول",
    "ادمج": "دمج", "اجمع": "دمج", "ضم": "دمج", "merge": "دمج", "combine": "دمج",
    "اربط": "ربط", "join": "ربط",
    "اقيس": "قياس", "قيس": "قياس", "measure": "قياس",
    "ارسم": "رسم", "draw": "رسم",
    "صدر": "تصدير", "اصدر": "تصدير", "export": "تصدير",
    "افتح": "فتح", "open": "فتح",
    "احفظ": "حفظ", "save": "حفظ",
    "اعرف": "عرف", "معرفه": "عرف",
    "صلح": "اصلاح", "اصلح": "اصلاح", "تصليح": "اصلاح", "fix": "اصلاح", "repair": "اصلاح",
    "افحص": "فحص", "check": "فحص",
    "امسح": "حذف", "احذف": "حذف", "مسح": "حذف", "شيل": "حذف", "delete": "حذف", "remove": "حذف",
    "نزل": "تنزيل", "حمل": "تنزيل", "تحميل": "تنزيل", "download": "تنزيل",
    "قسم": "تقسيم", "اقسم": "تقسيم", "split": "تقسيم", "فصل": "تقسيم", "افصل": "تقسيم",
    "رقم": "ترقيم", "number": "ترقيم",
    "قارن": "مقارنه", "compare": "مقارنه",
    "اطبع": "طباعه", "print": "طباعه",
    "اكسيل": "اكسل", "excel": "اكسل", "xlsx": "اكسل", "xls": "اكسل", "csv": "اكسل", "spreadsheet": "اكسل",
    "شيب": "شيب", "shapefile": "شيب", "shp": "شيب",
    "اوتوكاد": "كاد", "autocad": "كاد", "dxf": "كاد", "cad": "كاد",
    "نقاط": "نقط", "نقطه": "نقط", "point": "نقط", "points": "نقط",
    "خطوط": "خط", "line": "خط", "lines": "خط",
    "حدود": "حد", "boundary": "حد", "border": "حد",
    "طرق": "طريق", "شوارع": "طريق", "شارع": "طريق", "road": "طريق", "roads": "طريق", "street": "طريق",
    "مباني": "مبني", "building": "مبني", "buildings": "مبني",
    "اراضي": "ارض", "قطعه": "قطع", "قطع": "قطع", "parcel": "قطع", "parcels": "قطع",
    "مسافه": "مسافه", "مسافات": "مسافه", "distance": "مسافه", "distances": "مسافه",
    "مساحه": "مساحه", "مساحات": "مساحه", "area": "مساحه",
    "طبقه": "طبق", "طبقات": "طبق", "طبقتين": "طبق", "layer": "طبق", "layers": "طبق",
    "احداثيات": "احداثي", "احداثي": "احداثي", "coordinates": "احداثي", "coordinate": "احداثي",
    "اسقاط": "اسقاط", "projection": "اسقاط", "crs": "اسقاط",
    "صوره": "صور", "صور": "صور", "image": "صور", "photo": "صور",
    "مضلع": "مضلع", "مضلعات": "مضلع", "polygon": "مضلع", "polygons": "مضلع",
}

_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال")
_CLITICS = ("و", "ب", "ل", "ف", "ك")
_SUFFIXES = ("تين", "ات", "ين", "ون", "يه", "ها", "هم", "كم", "ه", "ي")


def _stem(word):
    w = word
    for p in _PREFIXES:
        if w.startswith(p) and len(w) - len(p) >= 2:
            w = w[len(p):]
            break
    else:
        if len(w) >= 5 and w[0] in _CLITICS and not w.isascii():
            w = w[1:]
    if w in SYNONYMS:
        return w
    if not w.isascii():
        for s in _SUFFIXES:
            if w.endswith(s) and len(w) - len(s) >= 3:
                return w[:-len(s)]
    elif len(w) > 4 and w.endswith("s"):
        return w[:-1]
    return w


def _key(word):
    if word in SYNONYMS:
        return SYNONYMS[word]
    s = _stem(word)
    return SYNONYMS.get(s, s)


def words(text, vocab=None):
    """Content words of a phrase or query as matching keys. With the vocabulary of the task
    phrases, a short word glued to a one-letter prefix ("لنقط", "بحدود") is also recognised."""
    out = []
    for w in _normalise(text).split():
        if w in STOP or len(w) < 2:
            continue
        k = _key(w)
        if vocab is not None and k not in vocab and len(w) >= 4 and w[0] in _CLITICS:
            k2 = _key(w[1:])
            if k2 in vocab:
                k = k2
        if k and k not in STOP and k not in out:
            out.append(k)
    return out


def _same(a, b):
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long_.startswith(short)


_INDEX = None
_VOCAB = set()


def _index():
    global _INDEX
    if _INDEX is None:
        _INDEX = [(target, phrase, words(phrase)) for target, en, ar in TASKS for phrase in en + ar]
        _VOCAB.update(k for _t, _p, ks in _INDEX for k in ks)
    return _INDEX


def search(text, n=5, min_score=0.45):
    """Best tasks for what was typed: [(score, target, phrase)], best first, one per target."""
    _index()
    q = words(text, _VOCAB)
    if not q:
        return []
    best = {}
    for target, phrase, p in _index():
        if not p:
            continue
        hit_q = sum(1 for a in q if any(_same(a, b) for b in p))
        if not hit_q:
            continue
        hit_p = sum(1 for b in p if any(_same(a, b) for a in q))
        score = 0.7 * hit_q / len(q) + 0.3 * hit_p / len(p)
        if score >= min_score and score > best.get(target, (0, ""))[0]:
            best[target] = (score, phrase)
    ranked = sorted(((s, t, ph) for t, (s, ph) in best.items()), key=lambda x: -x[0])
    return ranked[:n]
