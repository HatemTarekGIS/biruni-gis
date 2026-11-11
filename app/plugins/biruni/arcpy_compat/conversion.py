"""arcpy.conversion on GDAL/OGR."""
import os

from osgeo import gdal, ogr

from . import data as D
from ._base import fail, missing_callable, note_output, tool, unsupported
from .management import _inputs


def _join(out_path, out_name):
    if D._is_mem(str(out_path)):
        return f"memory\\{out_name}"
    return os.path.join(D.full_path(out_path), str(out_name))


@tool("conversion.FeatureClassToFeatureClass")
def FeatureClassToFeatureClass(in_features, out_path, out_name, where_clause=None, field_mapping=None,  # noqa: N802
                               config_keyword=None):
    return D.copy_source(in_features, _join(out_path, out_name), where_clause or None)


@tool("conversion.ExportFeatures")
def ExportFeatures(in_features, out_features, where_clause=None, use_field_alias_as_name=None,  # noqa: N802
                   field_mapping=None, sort_field=None):
    return D.copy_source(in_features, out_features, where_clause or None)


@tool("conversion.TableToTable")
def TableToTable(in_rows, out_path, out_name, where_clause=None, field_mapping=None,  # noqa: N802
                 config_keyword=None):
    return D.copy_source(in_rows, _join(out_path, out_name), where_clause or None, want="table")


@tool("conversion.ExportTable")
def ExportTable(in_table, out_table, where_clause=None, use_field_alias_as_name=None, field_mapping=None,  # noqa
                sort_field=None):
    return D.copy_source(in_table, out_table, where_clause or None, want="table")


def _batch(inputs, out_folder):
    outs = []
    base = D.full_path(out_folder)
    for item in _inputs(inputs):
        src = D.Source(item)
        outs.append(D.copy_source(item, os.path.join(base, src.ref.base_name)))
    return outs


@tool("conversion.FeatureClassToGeodatabase")
def FeatureClassToGeodatabase(Input_Features, Output_Geodatabase):  # noqa: N802,N803
    _batch(Input_Features, Output_Geodatabase)
    return D.full_path(Output_Geodatabase)


@tool("conversion.FeatureClassToShapefile")
def FeatureClassToShapefile(Input_Features, Output_Folder):  # noqa: N802,N803
    _batch(Input_Features, Output_Folder)
    return D.full_path(Output_Folder)


@tool("conversion.TableToGeodatabase")
def TableToGeodatabase(Input_Table, Output_Geodatabase):  # noqa: N802,N803
    base = D.full_path(Output_Geodatabase)
    for item in _inputs(Input_Table):
        D.copy_source(item, os.path.join(base, D.Source(item).ref.base_name), want="table")
    return base


@tool("conversion.TableToExcel")
def TableToExcel(Input_Table, Output_Excel_File, Use_field_alias_as_column_header="NAME",  # noqa: N802,N803
                 Use_domain_and_subtype_description="CODE"):  # noqa: N803
    out = D.full_path(Output_Excel_File)
    if not out.lower().endswith(".xlsx"):
        if out.lower().endswith(".xls"):
            out = out + "x"
        else:
            out += ".xlsx"
    src = D.Source(Input_Table)
    sheet = src.ref.base_name[:31] or "Sheet1"
    if os.path.exists(out):
        from ._base import env
        if not env.overwriteOutput:
            fail(725, f"Output Excel File: Dataset {out} already exists.")
        os.remove(out)
    D.copy_source(Input_Table, D.Ref("excel", out, sheet), want="table")
    return out


@tool("conversion.ExcelToTable")
def ExcelToTable(Input_Excel_File, Output_Table, Sheet=None, field_names_row=1, cell_range=None):  # noqa: N802,N803
    path = D.full_path(Input_Excel_File)
    ds = gdal.OpenEx(path, gdal.OF_VECTOR, allowed_drivers=["XLSX", "XLS", "CSV"],
                     open_options=["HEADERS=FORCE"] if int(field_names_row or 0) > 0 else ["HEADERS=DISABLE"])
    if ds is None:
        fail(732, f"Input Excel File: Dataset {Input_Excel_File} does not exist or is not supported")
    name = str(Sheet).rstrip("$") if Sheet else ds.GetLayer(0).GetName()
    ds = None
    if cell_range:
        unsupported("conversion.ExcelToTable with a cell range")
    return D.copy_source(D.Ref("excel", path, name), Output_Table, want="table")


@tool("conversion.FeaturesToJSON")
def FeaturesToJSON(in_features, out_json_file, format_json=None, include_z_values=None, include_m_values=None,  # noqa
                   geoJSON="GEOJSON", outputToWGS84=None, use_field_alias=None):  # noqa: N803
    out = D.full_path(out_json_file)
    if str(geoJSON).upper() not in ("GEOJSON", "TRUE") and geoJSON is not True:
        unsupported("conversion.FeaturesToJSON with Esri JSON output")
    target = None
    if str(outputToWGS84).upper() in ("WGS84", "TRUE") or outputToWGS84 is True:
        from osgeo import osr
        target = osr.SpatialReference()
        target.ImportFromEPSG(4326)
    if not out.lower().endswith((".geojson", ".json")):
        out += ".geojson"
    return D.copy_source(in_features, out, target_srs=target)


@tool("conversion.JSONToFeatures")
def JSONToFeatures(in_json_file, out_features, geometry_type=None):  # noqa: N802
    return D.copy_source(D.full_path(in_json_file), out_features)


@tool("conversion.KMLToLayer")
def KMLToLayer(in_kml_file, output_folder, output_data=None, include_groundoverlay=None):  # noqa: N802
    unsupported("conversion.KMLToLayer")


@tool("conversion.RasterToPolygon")
def RasterToPolygon(in_raster, out_polygon_features, simplify="SIMPLIFY", raster_field="Value",  # noqa: N802
                    create_multipart_features="SINGLE_OUTER_PART", max_vertices_per_feature=None):
    from .sa import raster_path
    src = gdal.Open(raster_path(in_raster))
    band = src.GetRasterBand(1)
    mem = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("poly")
    srs = src.GetSpatialRef()
    lyr = mem.CreateLayer("poly", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("gridcode", ogr.OFTInteger))
    gdal.Polygonize(band, band.GetMaskBand(), lyr, 0, [], callback=None)
    fields = [ogr.FieldDefn("Id", ogr.OFTInteger), ogr.FieldDefn("gridcode", ogr.OFTInteger)]
    w = D.Writer(out_polygon_features, ogr.wkbPolygon, srs, fields)
    for i, f in enumerate(lyr):
        w.add([i + 1, f.GetField(0)], f.GetGeometryRef().Clone())
    return w.close()


@tool("conversion.PolygonToRaster")
def PolygonToRaster(in_features, value_field, out_rasterdataset, cell_assignment="CELL_CENTER",  # noqa: N802
                    priority_field=None, cellsize=None, build_rat=None):
    return _rasterize(in_features, value_field, out_rasterdataset, cellsize,
                      str(cell_assignment).upper() == "MAXIMUM_COMBINED_AREA")


PointToRaster = None
FeatureToRaster = None


def _rasterize(in_features, value_field, out, cellsize, all_touched=False):
    from .management import _out_raster
    path, lname = D.materialize(in_features)
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    lyr = ds.GetLayerByName(lname) if lname else ds.GetLayer(0)
    x0, x1, y0, y1 = lyr.GetExtent()
    try:
        size = float(str(cellsize).split()[0])
    except (TypeError, ValueError, IndexError):
        size = max(x1 - x0, y1 - y0) / 250.0 or 1.0
    target = _out_raster(out)
    attr = None if str(value_field).upper() in ("", "NONE", "OID", "FID", "OBJECTID") else str(value_field)
    opts = gdal.RasterizeOptions(format="GTiff", xRes=size, yRes=size, outputBounds=(x0, y0, x1, y1),
                                 attribute=attr, burnValues=None if attr else [1], noData=-9999,
                                 outputType=gdal.GDT_Float32, layers=[lyr.GetName()], allTouched=all_touched)
    res = gdal.Rasterize(target, path, options=opts)
    res = None
    note_output(target)
    return target


@tool("conversion.FeatureToRaster")
def _feature_to_raster(in_features, field, out_raster, cell_size=None):
    return _rasterize(in_features, field, out_raster, cell_size)


@tool("conversion.PointToRaster")
def _point_to_raster(in_features, value_field, out_rasterdataset, cell_assignment="MOST_FREQUENT",
                     priority_field=None, cellsize=None, build_rat=None):
    return _rasterize(in_features, value_field, out_rasterdataset, cellsize)


FeatureToRaster = _feature_to_raster
PointToRaster = _point_to_raster


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return missing_callable(f"conversion.{name}")
