"""The `arcpy` module the shim puts on sys.modules. Everything arcpy scripts reach as `arcpy.X`."""
import sys
import types

from . import _legacy, analysis, conversion, da, management, mp, registry, sa
from . import data as _data
from . import geometry as _geometry
from ._base import (AddError, AddIDMessage, AddMessage, AddReturnMessage, AddWarning, CheckExtension,  # noqa: F401
                    CheckInExtension, CheckOutExtension, CheckProduct, ClearEnvironment, EnvManager, ExecuteAbort,
                    ExecuteError, ExecuteWarning, GetArgumentCount, GetInstallInfo, GetLogHistory, GetMaxSeverity,
                    GetMessage, GetMessageCount, GetMessages, GetParameter, GetParameterAsText, GetParameterCount,
                    GetParameterInfo, GetReturnCode, GetSeverity, GetSystemEnvironment, ImportToolbox,
                    ListEnvironments, ProductInfo, RefreshActiveView, RefreshCatalog, RefreshTOC, ResetEnvironments,
                    ResetProgressor, Result, SetLogHistory, SetLogMetadata, SetParameter, SetParameterAsText,
                    SetProduct, SetProgressor, SetProgressorLabel, SetProgressorPosition, Usage, env,
                    missing_callable, tool)
from ._base import GetActivePortalURL, GetSigninToken  # noqa: F401
from ._legacy import FieldMap, FieldMappings, InsertCursor, SearchCursor, UpdateCursor, ValueTable  # noqa: F401
from .data import (AddFieldDelimiters, CreateScratchName, CreateUniqueName, Describe, Exists, Field,  # noqa: F401
                   ListDatasets, ListDomains, ListFeatureClasses, ListFields, ListFiles, ListIndexes, ListRasters,
                   ListSpatialReferences, ListTables, ListUsers, ListVersions, ListWorkspaces, ParseFieldName,
                   ParseTableName, TestSchemaLock, ValidateFieldName, ValidateTableName)
from .geometry import (Array, AsShape, Extent, FromCoordString, FromGeohash, FromWKB, FromWKT,  # noqa: F401
                       Geometry, Multipoint, Point, PointGeometry, Polygon, Polyline, SpatialReference)
from .sa import NumPyArrayToRaster, Raster, RasterToNumPyArray  # noqa: F401

__version__ = "3.3-biruni"
__biruni_shim__ = True


class _StubModule(types.ModuleType):
    """A toolbox Biruni does not have yet: every tool raises ExecuteError naming itself."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        short = self.__name__.split(".", 1)[-1]
        return missing_callable(f"{short}.{name}")


# ---------------------------------------------------------------- 3D Analyst raster tools (tool form of sa)

ddd = _StubModule("arcpy.ddd")


@tool("ddd.Slope")
def _ddd_slope(in_raster, out_raster, output_measurement="DEGREE", z_factor=1, method="PLANAR", z_unit="METER",
               analysis_target_device=None):
    r = sa.Slope(in_raster, output_measurement, z_factor)
    r.save(out_raster)
    return r.catalogPath


@tool("ddd.Aspect")
def _ddd_aspect(in_raster, out_raster, method="PLANAR", z_unit="METER", project_geodesic_azimuths=None,
                analysis_target_device=None):
    r = sa.Aspect(in_raster)
    r.save(out_raster)
    return r.catalogPath


@tool("ddd.HillShade")
def _ddd_hillshade(in_raster, out_raster, azimuth=315, altitude=45, model_shadows="NO_SHADOWS", z_factor=1):
    r = sa.Hillshade(in_raster, azimuth, altitude, model_shadows, z_factor)
    r.save(out_raster)
    return r.catalogPath


ddd.Slope = _ddd_slope
ddd.Aspect = _ddd_aspect
ddd.HillShade = _ddd_hillshade
ddd.Hillshade = _ddd_hillshade

_SA_TOOL_FORM = {"slope": _ddd_slope, "aspect": _ddd_aspect, "hillshade": _ddd_hillshade}

# ---------------------------------------------------------------- stub toolboxes

_IMPLEMENTED = {"management": management, "analysis": analysis, "conversion": conversion, "sa": sa, "da": da,
                "mp": mp, "ddd": ddd}
_STUBS = {}
for _name in sorted(registry.TOOLBOX_MODULES):
    if _name in _IMPLEMENTED or _name == "env":
        continue
    _STUBS[_name] = _StubModule(f"arcpy.{_name}")
for _name, _mod in _STUBS.items():
    globals()[_name] = _mod

stats = _STUBS.get("stats")
mapping = _STUBS.get("mapping")
cartography = _STUBS.get("cartography")
edit = _STUBS.get("edit")
na = _STUBS.get("na")


def resolve(name):
    """'CopyFeatures_management' -> the shim function, or None."""
    if "_" in name:
        base, _sep, box = name.rpartition("_")
        alias = registry.TOOLBOX_ALIASES.get(box.lower())
        if alias and base:
            if alias == "sa" and base.lower() in _SA_TOOL_FORM:
                return _SA_TOOL_FORM[base.lower()]
            if alias in _IMPLEMENTED:
                return getattr(_IMPLEMENTED[alias], base)
            return missing_callable(f"{alias}.{base}")
    return None


class _GP:
    """arcpy.gp - the old geoprocessor object: arcpy.gp.Slope_sa(...) etc."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        fn = resolve(name)
        if fn is not None:
            return fn
        if hasattr(sys.modules[__name__], name):
            return getattr(sys.modules[__name__], name)
        return missing_callable(f"gp.{name}")


gp = _GP()


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    fn = resolve(name)
    if fn is not None:
        return fn
    return missing_callable(name)


def submodules():
    """name -> module, for sys.modules registration."""
    out = dict(_IMPLEMENTED)
    out.update(_STUBS)
    return out


del _data, _geometry, _legacy, _name, _mod
