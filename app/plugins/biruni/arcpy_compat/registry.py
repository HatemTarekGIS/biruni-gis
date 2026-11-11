"""The one machine-readable list of what the arcpy shim covers.

Keys are normalised arcpy names: top-level functions/classes as 'Describe', toolbox tools as
'management.CopyFeatures' (so arcpy.CopyFeatures_management and arcpy.management.CopyFeatures map to the same key),
environment settings as 'env.workspace'. Status is 'supported' or 'partial'; anything not listed is 'missing'.
The test checks that every entry here resolves to a working object in the shim."""

S, P = "supported", "partial"

COVERAGE = {
    # ---- core functions
    "Describe": (S, ""), "Exists": (S, ""), "ListFeatureClasses": (S, ""), "ListFields": (S, ""),
    "ListRasters": (S, "Folders only; rasters inside geodatabases are not listed"), "ListTables": (S, ""),
    "ListDatasets": (S, ""), "ListWorkspaces": (S, ""), "ListFiles": (S, ""), "ListIndexes": (P, "Always empty"),
    "ListEnvironments": (S, ""), "ListDomains": (P, "Always empty"), "AddMessage": (S, ""), "AddWarning": (S, ""),
    "AddError": (S, ""), "AddIDMessage": (S, ""), "AddReturnMessage": (S, ""), "GetMessages": (S, ""),
    "GetMessage": (S, ""), "GetMessageCount": (S, ""), "GetMaxSeverity": (S, ""), "GetSeverity": (S, ""),
    "GetParameterAsText": (S, ""), "GetParameter": (S, ""), "SetParameterAsText": (S, ""), "SetParameter": (S, ""),
    "GetArgumentCount": (S, ""), "GetParameterCount": (S, ""), "SetProgressor": (S, ""),
    "SetProgressorLabel": (S, ""), "SetProgressorPosition": (S, ""), "ResetProgressor": (S, ""),
    "EnvManager": (S, ""), "ResetEnvironments": (S, ""), "ClearEnvironment": (S, ""), "CreateUniqueName": (S, ""),
    "CreateScratchName": (S, ""), "ValidateTableName": (S, ""), "ValidateFieldName": (S, ""),
    "AddFieldDelimiters": (S, ""), "ParseFieldName": (P, "No database owner/name parts"),
    "ParseTableName": (P, "No database owner/name parts"), "TestSchemaLock": (S, "Always True"),
    "CheckExtension": (S, "Always available"), "CheckOutExtension": (S, ""), "CheckInExtension": (S, ""),
    "CheckProduct": (S, ""), "SetProduct": (S, ""), "ProductInfo": (S, ""), "GetInstallInfo": (P, ""),
    "SetLogHistory": (S, ""), "GetLogHistory": (S, ""), "SetLogMetadata": (S, ""), "RefreshActiveView": (S, ""),
    "RefreshTOC": (S, ""), "RefreshCatalog": (S, ""), "GetSystemEnvironment": (S, ""), "Usage": (P, ""),
    "ExecuteError": (S, ""), "ExecuteWarning": (S, ""), "Field": (S, ""), "Result": (S, ""),
    "SearchCursor": (P, "ArcGIS 10.0 cursor with Row objects"), "UpdateCursor": (P, "ArcGIS 10.0 cursor"),
    "InsertCursor": (P, "ArcGIS 10.0 cursor"), "FieldMappings": (P, "Accepted; tools copy fields as they are"),
    "FieldMap": (P, "Accepted; tools copy fields as they are"), "ValueTable": (S, ""), "Raster": (S, ""),
    "RasterToNumPyArray": (S, ""), "NumPyArrayToRaster": (S, ""),
    "GetSigninToken": (P, "Always None: Biruni is not signed in to an ArcGIS portal"),
    "GetActivePortalURL": (P, "Always empty"),
    # ---- geometry
    "Point": (S, ""), "Array": (S, ""), "Polyline": (S, ""), "Polygon": (S, ""), "PointGeometry": (S, ""),
    "Multipoint": (S, ""), "Geometry": (S, ""), "Extent": (S, ""),
    "SpatialReference": (S, "EPSG/Esri codes, .prj files, WKT, datasets and common Esri names"), "FromWKT": (S, ""),
    "FromWKB": (S, ""), "AsShape": (S, ""),
    # ---- environment
    "env.workspace": (S, ""), "env.overwriteOutput": (S, ""), "env.outputCoordinateSystem": (S, ""),
    "env.scratchWorkspace": (S, ""), "env.scratchGDB": (S, ""), "env.scratchFolder": (S, ""),
    "env.addOutputsToMap": (S, ""), "env.qualifiedFieldNames": (P, "Accepted, no effect"),
    # ---- da
    "da.SearchCursor": (S, ""), "da.UpdateCursor": (S, ""), "da.InsertCursor": (S, ""), "da.Walk": (S, ""),
    "da.Describe": (S, ""), "da.Editor": (P, "Edits are saved immediately; no undo"),
    "da.FeatureClassToNumPyArray": (S, ""), "da.TableToNumPyArray": (S, ""), "da.ListDomains": (P, ""),
    "da.ListSubtypes": (P, ""),
    # ---- management
    "management.GetCount": (S, ""), "management.MakeFeatureLayer": (S, ""), "management.MakeTableView": (S, ""),
    "management.SelectLayerByAttribute": (S, ""), "management.SelectLayerByLocation": (S, ""),
    "management.CopyFeatures": (S, ""), "management.CopyRows": (S, ""), "management.ExportTable": (S, ""),
    "management.AddField": (S, ""), "management.AddFields": (S, ""), "management.DeleteField": (S, ""),
    "management.AlterField": (S, ""),
    "management.CalculateField": (S, "PYTHON3 and single-line ARCADE; simple VB; no SQL"),
    "management.CalculateFields": (S, ""), "management.CalculateGeometryAttributes": (S, ""),
    "management.AddGeometryAttributes": (S, ""), "management.AddXY": (S, ""),
    "management.JoinField": (S, ""), "management.Merge": (P, "Field mappings ignored"),
    "management.Append": (P, "Fields matched by name; field mappings ignored"),
    "management.Dissolve": (S, ""), "management.Project": (P, "No choice of datum transformation"),
    "management.CreateFeatureclass": (S, ""), "management.CreateTable": (S, ""),
    "management.CreateFileGDB": (S, ""), "management.CreateFeatureDataset": (S, ""),
    "management.CreateFolder": (S, ""), "management.Delete": (S, ""), "management.Rename": (S, ""),
    "management.Copy": (S, ""), "management.DeleteFeatures": (S, ""), "management.DeleteRows": (S, ""),
    "management.TruncateTable": (S, ""), "management.FeatureToPoint": (S, ""),
    "management.MultipartToSinglepart": (S, ""), "management.RepairGeometry": (S, ""),
    "management.DefineProjection": (P, "Shapefiles and rasters"), "management.AddSpatialIndex": (S, "No-op"),
    "management.AddIndex": (S, "No-op"), "management.AddAttributeIndex": (S, "No-op"),
    "management.RemoveIndex": (S, "No-op"), "management.RemoveSpatialIndex": (S, "No-op"),
    "management.Compact": (S, "No-op"), "management.ClearWorkspaceCache": (S, "No-op"),
    "management.Sort": (S, ""), "management.DeleteIdentical": (S, ""),
    "management.CheckGeometry": (S, ""), "management.PolygonToLine": (P, "Neighbours are not identified"),
    "management.FeatureToPolygon": (P, "No cluster tolerance; attributes from the polygon containing each result"),
    "management.MinimumBoundingGeometry": (P, "No MBG_ attribute fields"),
    "management.Clip": (S, "Raster clip"), "management.CopyRaster": (S, ""), "management.ProjectRaster": (S, ""),
    "management.Resample": (S, ""), "management.MosaicToNewRaster": (P, "LAST/FIRST only"),
    "management.GetRasterProperties": (S, ""), "management.CalculateStatistics": (S, ""),
    "management.BuildPyramids": (S, ""),
    # ---- analysis
    "analysis.Buffer": (S, "Geographic data buffered geodesically"), "analysis.PairwiseBuffer": (S, ""),
    "analysis.Clip": (S, ""), "analysis.PairwiseClip": (S, ""), "analysis.Erase": (S, ""),
    "analysis.PairwiseErase": (S, ""), "analysis.Intersect": (S, "Self-intersection of a single input skipped"),
    "analysis.PairwiseIntersect": (S, ""), "analysis.Union": (P, "Overlaps inside one input are not split"),
    "analysis.Identity": (S, ""), "analysis.SpatialJoin": (P, "Field mappings ignored; first match is kept"),
    "analysis.Statistics": (S, ""), "analysis.Frequency": (S, ""), "analysis.Select": (S, ""),
    "analysis.TableSelect": (S, ""), "analysis.Near": (P, "Planar only"), "analysis.PairwiseDissolve": (S, ""),
    # ---- conversion
    "conversion.FeatureClassToFeatureClass": (S, ""), "conversion.ExportFeatures": (S, ""),
    "conversion.TableToTable": (S, ""), "conversion.ExportTable": (S, ""),
    "conversion.FeatureClassToGeodatabase": (S, ""), "conversion.FeatureClassToShapefile": (S, ""),
    "conversion.TableToGeodatabase": (S, ""), "conversion.TableToExcel": (S, ".xlsx"),
    "conversion.ExcelToTable": (S, ""), "conversion.FeaturesToJSON": (P, "GeoJSON only"),
    "conversion.JSONToFeatures": (P, "GeoJSON only"), "conversion.RasterToPolygon": (P, "No simplification"),
    "conversion.PolygonToRaster": (P, ""), "conversion.FeatureToRaster": (P, ""),
    "conversion.PointToRaster": (P, "Last value per cell"),
    # ---- spatial analyst
    "sa.Raster": (S, ""), "sa.Slope": (S, ""), "sa.Aspect": (S, ""), "sa.Hillshade": (P, "No shadow modelling"),
    "sa.Con": (S, ""), "sa.SetNull": (S, ""), "sa.IsNull": (S, ""), "sa.Abs": (S, ""), "sa.Sqrt": (S, ""),
    "sa.Square": (S, ""), "sa.Exp": (S, ""), "sa.Ln": (S, ""), "sa.Log10": (S, ""), "sa.Log2": (S, ""),
    "sa.Sin": (S, ""), "sa.Cos": (S, ""), "sa.Tan": (S, ""), "sa.Power": (S, ""), "sa.Int": (S, ""),
    "sa.Float": (S, ""), "sa.RoundUp": (S, ""), "sa.RoundDown": (S, ""), "sa.Plus": (S, ""), "sa.Minus": (S, ""),
    "sa.Times": (S, ""), "sa.Divide": (S, ""), "sa.Reclassify": (S, ""), "sa.RemapValue": (S, ""),
    "sa.RemapRange": (S, ""), "sa.ExtractByMask": (S, ""), "sa.ExtractByAttributes": (P, "VALUE comparisons"),
    "sa.CellStatistics": (P, "No majority/minority/variety"),
    "ddd.Slope": (S, ""), "ddd.Aspect": (S, ""), "ddd.HillShade": (P, "No shadow modelling"),
    # ---- mapping
    "mp.ArcGISProject": (P, "'CURRENT' (the open Biruni project) and .qgz files; not .aprx"),
    "mp.LayerFile": (P, "Not readable; raises"),
}

# environment settings scripts set that the shim accepts but that do not change results
ACCEPTED_ENV = {"extent", "cellSize", "mask", "snapRaster", "outputZFlag", "outputMFlag", "XYTolerance",
                "XYResolution", "parallelProcessingFactor", "geographicTransformations", "transferDomains",
                "preserveGlobalIds", "maintainAttachments", "nodata", "compression", "pyramid", "rasterStatistics",
                "autoCommit", "outputZValue", "randomGenerator", "configKeyword", "tileSize", "resamplingMethod",
                "packageWorkspace", "terrainMemoryUsage", "transferGDBAttributeProperties",
                "maintainCurveSegments", "processingServer"}

# toolbox suffixes of the classic arcpy.Tool_toolbox spelling
TOOLBOX_ALIASES = {"management": "management", "analysis": "analysis", "conversion": "conversion", "sa": "sa",
                   "3d": "ddd", "ddd": "ddd", "cartography": "cartography", "edit": "edit", "stats": "stats",
                   "na": "na", "lr": "lr", "ia": "ia", "geocoding": "geocoding", "server": "server",
                   "intelligence": "intelligence", "md": "md", "gapro": "gapro", "ra": "ra", "topographic": "topographic",
                   "production": "production", "defense": "defense", "maritime": "maritime", "aviation": "aviation",
                   "locref": "locref", "bim": "bim", "crowdsourcing": "crowdsourcing", "geoanalytics": "geoanalytics",
                   "ba": "ba", "conflation": "conflation", "tn": "tn", "un": "un", "pipelinerules": "pipelinerules",
                   "indoorpositioning": "indoorpositioning", "indoors": "indoors", "parcel": "parcel",
                   "reviewer": "reviewer", "wmx": "wmx", "nd": "nd", "td": "td", "ca": "ca", "schematics": "schematics",
                   "arc": "arc", "sas": "sas", "trace": "trace", "transit": "transit", "spatialstats": "stats",
                   "sharing": "sharing", "metadata": "metadata"}

TOOLBOX_MODULES = {"management", "analysis", "conversion", "sa", "ddd", "cartography", "edit", "stats", "na",
                   "lr", "ia", "geocoding", "server", "intelligence", "md", "gapro", "ra", "topographic", "sharing",
                   "metadata", "mp", "da", "mapping", "env", "ca", "parcel", "un", "tn", "nd", "bim", "ba",
                   "conflation", "locref", "indoors", "production", "defense", "maritime", "aviation",
                   "geoanalytics", "crowdsourcing", "reviewer", "wmx", "td", "time", "charts", "nax", "utils",
                   "geoprocessing", "ai", "agolservices", "spatial"}


def status(name):
    """'supported' | 'partial' | 'missing' for a normalised name."""
    if name in COVERAGE:
        return COVERAGE[name][0]
    if name.startswith("env."):
        return "partial" if name[4:] in ACCEPTED_ENV else "missing"
    return "missing"


def as_list():
    """[{'name', 'status', 'note'}] - for tools, docs and other agents."""
    return [{"name": k, "status": v[0], "note": v[1]} for k, v in sorted(COVERAGE.items())]


def counts():
    out = {"supported": 0, "partial": 0}
    for st, _n in COVERAGE.values():
        out[st] += 1
    return out
