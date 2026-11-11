"""Starter pack for a study area: one Processing tool around thesis/starter.py.

Downloads run in processAlgorithm (worker thread: files only); the project
(.qgz, layer groups, location layout) is written in postProcessAlgorithm, which
Processing runs on the main thread.
"""
import datetime as dt
import os

from qgis.core import (QgsProcessing, QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputFolder,
                       QgsProcessingOutputString, QgsProcessingParameterBoolean, QgsProcessingParameterDateTime,
                       QgsProcessingParameterEnum, QgsProcessingParameterExtent, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QDate

from ..i18n import tr
from .base import BiruniAlgorithm


class ThesisStarter(BiruniAlgorithm):
    ID = "thesisstarter"
    NAME = "Study Area Starter Pack"
    GROUP = "Thesis"
    GROUP_ID = "thesis"
    HELP = ("Gives a graduation project or thesis a ready project for its study area in a few minutes. Choose the "
            "area as a polygon layer, a place name or an extent. Biruni makes a project folder with the free basic "
            "data of the area - administrative boundaries, OpenStreetMap roads, buildings, land use, water and "
            "places, the 30 m elevation model with slope, aspect, hillshade and contours, the 10 m land cover with "
            "the area of each class, Sentinel-2 true colour, false colour and NDVI of a period, and the 2020 "
            "population - every layer cut to the area, in a projected coordinate system in metres, styled and "
            "grouped. It adds a summary of the area (area, elevation, road length by type, buildings), a data "
            "sources file with ready citations for the references chapter, and the location map of chapter 1 "
            "(country, region, study area). Each source is independent: what cannot be downloaded is reported "
            "and the rest is kept.")
    OSM_OPTIONS = ["roads", "buildings", "landuse", "water", "places"]

    def __init__(self):
        super().__init__()
        self._state = None

    def initAlgorithm(self, config=None):  # noqa: N802
        from ..thesis.starter import OSM_TITLES

        self.addParameter(QgsProcessingParameterFeatureSource("AREA", tr("Study area (polygon layer)"),
                                                              [QgsProcessing.SourceType.TypeVectorPolygon],
                                                              optional=True))
        self.addParameter(QgsProcessingParameterString("PLACE", tr("…or a place name (city, district, governorate)"),
                                                       optional=True))
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("…or an extent"), optional=True))
        self.addParameter(QgsProcessingParameterString("NAME", tr("Name of the study area / project"),
                                                       optional=True))
        self.addParameter(QgsProcessingParameterFile("FOLDER", tr("Create the project folder in"),
                                                     QgsProcessingParameterFile.Behavior.Folder))
        self.addParameter(QgsProcessingParameterBoolean("BOUNDARIES", tr("Administrative boundaries"), True))
        self.addParameter(QgsProcessingParameterEnum("OSM", tr("OpenStreetMap data"),
                                                     [tr(OSM_TITLES[k]) for k in self.OSM_OPTIONS],
                                                     allowMultiple=True, defaultValue=list(range(5)), optional=True))
        self.addParameter(QgsProcessingParameterBoolean("TERRAIN", tr("Elevation, slope, aspect, hillshade and "
                                                                      "contours"), True))
        self.addParameter(QgsProcessingParameterNumber("CONTOUR", tr("Contour interval (m, 0 = automatic)"),
                                                       QgsProcessingParameterNumber.Type.Double, 0, False, 0, 10000))
        self.addParameter(QgsProcessingParameterBoolean("LANDCOVER", tr("Land cover with area per class"), True))
        self.addParameter(QgsProcessingParameterBoolean("SENTINEL", tr("Sentinel-2 true colour, false colour and "
                                                                       "NDVI"), True))
        today = QDate.currentDate()
        self.addParameter(QgsProcessingParameterDateTime("S2_START", tr("Images from"),
                                                         QgsProcessingParameterDateTime.Type.Date,
                                                         today.addDays(-90)))
        self.addParameter(QgsProcessingParameterDateTime("S2_END", tr("Images until"),
                                                         QgsProcessingParameterDateTime.Type.Date, today))
        self.addParameter(QgsProcessingParameterNumber("CLOUD", tr("Maximum cloud cover (%)"),
                                                       QgsProcessingParameterNumber.Type.Integer, 20, False, 0, 100))
        self.addParameter(QgsProcessingParameterBoolean("POPULATION", tr("Population (WorldPop 2020)"), True))
        self.addParameter(QgsProcessingParameterBoolean("LOCATION_MAP", tr("Location map for chapter 1"), True))
        self.addOutput(QgsProcessingOutputFile("PROJECT", tr("Project file")))
        self.addOutput(QgsProcessingOutputFolder("PROJECT_FOLDER", tr("Project folder")))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", tr("Summary")))
        self.addOutput(QgsProcessingOutputFile("SOURCES", tr("Data sources and citations")))
        self.addOutput(QgsProcessingOutputString("REPORT", tr("What was downloaded")))

    def _area(self, parameters, context, feedback, sources):
        from .. import i18n
        from ..thesis import starter

        name = self.parameterAsString(parameters, "NAME", context).strip()
        if parameters.get("AREA"):
            src = self.parameterAsSource(parameters, "AREA", context)
            if src is not None:
                return starter.area_from_layer(src, name or src.sourceName())
        place = self.parameterAsString(parameters, "PLACE", context).strip()
        if place:
            return starter.area_from_place(place, sources, feedback, i18n.lang())
        if parameters.get("EXTENT"):
            crs = self.parameterAsExtentCrs(parameters, "EXTENT", context)
            rect = self.parameterAsExtent(parameters, "EXTENT", context, crs)
            if not rect.isEmpty():
                return starter.area_from_extent(rect, crs if crs.isValid() else context.project().crs(), name)
        raise QgsProcessingException(tr("Choose the study area: a polygon layer, a place name or an extent."))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..thesis import starter

        sources = getattr(self, "sources", None) or starter.OnlineSources(context)
        area = self._area(parameters, context, feedback, sources)
        name = self.parameterAsString(parameters, "NAME", context).strip() or area.name
        area.name = name
        folder = self.parameterAsFile(parameters, "FOLDER", context)
        if not folder:
            raise QgsProcessingException(tr("Choose the folder of the project."))
        os.makedirs(folder, exist_ok=True)
        osm = [self.OSM_OPTIONS[i] for i in self.parameterAsEnums(parameters, "OSM", context)]

        def date(key, default):
            v = self.parameterAsDateTime(parameters, key, context)
            return v.date().toPyDate() if v.isValid() else default

        end = date("S2_END", dt.date.today())
        opts = {"boundaries": self.parameterAsBoolean(parameters, "BOUNDARIES", context), "osm": osm,
                "terrain": self.parameterAsBoolean(parameters, "TERRAIN", context),
                "contour_interval": self.parameterAsDouble(parameters, "CONTOUR", context),
                "landcover": self.parameterAsBoolean(parameters, "LANDCOVER", context),
                "sentinel": self.parameterAsBoolean(parameters, "SENTINEL", context),
                "s2_start": date("S2_START", end - dt.timedelta(days=90)), "s2_end": end,
                "cloud": self.parameterAsInt(parameters, "CLOUD", context),
                "population": self.parameterAsBoolean(parameters, "POPULATION", context),
                "location_map": self.parameterAsBoolean(parameters, "LOCATION_MAP", context)}
        feedback.pushInfo(tr("Study area: {n}").format(n=name))
        self._state = starter.fetch(area, folder, name, opts, sources, feedback, context)
        st = self._state
        feedback.pushInfo(tr("Coordinate system: {c} - {r}").format(c=f"{st['crs_name']} ({st['crs']})",
                                                                  r=st["crs_reason"]))
        for line in starter.report_lines(st):
            (feedback.pushWarning if line.startswith("FAILED") else feedback.pushInfo)(line)
        return self._outputs()

    def _outputs(self):
        from ..thesis import starter

        st = self._state
        return {"PROJECT": st["qgz"], "PROJECT_FOLDER": st["root"], "SUMMARY": st.get("summary", ""),
                "SOURCES": st.get("sources_file", ""), "REPORT": "\n".join(starter.report_lines(st))}

    def postProcessAlgorithm(self, context, feedback):  # noqa: N802
        from ..thesis import starter

        if self._state is None:
            return {}
        try:
            starter.build_project(self._state)
            feedback.pushInfo(tr("Project ready: {p}").format(p=self._state["qgz"]))
        except Exception as e:  # noqa: BLE001
            feedback.reportError(tr("The project file could not be written: {e}").format(e=e))
        return self._outputs()
