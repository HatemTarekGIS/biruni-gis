"""Map book: a whole book of map pages in one PDF (the Map Book panel's engine, as a tool for models and batch)."""
from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingParameterVectorLayer, QgsProject)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

SOURCE_KEYS = ["grid", "index", "strips", "features", "stops"]
SCALE_KEYS = ["best", "common", "fixed"]
ORIENT_KEYS = ["auto", "landscape", "portrait"]
PAPER_KEYS = ["A4", "A3", "A2", "A1", "A0", "Letter", "Tabloid"]
LANG_KEYS = ["", "ar", "en", "fr", "es", "pt_BR", "tr", "fa"]
PER_PAGE_KEYS = ["", "png", "jpg", "pdf"]


class MapBook(BiruniAlgorithm):
    ID = "mapbook"
    NAME = "Map book (pages, key map, contents, index - one PDF)"
    GROUP = "Cartography"
    GROUP_ID = "cartography"
    HELP = ("A book of map pages in one PDF, like a street atlas: pages from an automatic grid that keeps only the "
            "cells with data and uses the fewest pages, from an index layer, from strips along a road, pipe or "
            "river (each page turned along it), one page per feature, or the project's Map Stops. Every page gets "
            "the largest standard scale that fits (or one scale for the book), its number and name, a key map "
            "with the page shown, arrows with the numbers of the next pages, a legend of what it shows, an "
            "optional table of its features, the coordinate grid and a QR code that opens its area on a phone. "
            "The book has a cover with the key map, contents and the index of names. In the PDF, the bookmarks, "
            "the arrows, the key map cells, the contents and the index jump to their pages. The layout stays in "
            "the project, to change in the layout designer.")

    SOURCE, LAYER, NAME_FIELD, PAPER, ORIENTATION, SCALE_MODE, SCALE = (
        "SOURCE", "LAYER", "NAME_FIELD", "PAPER", "ORIENTATION", "SCALE_MODE", "SCALE")
    MAX_PAGES, OVERLAP, TITLE, LANGUAGE, INDEX_LAYER, INDEX_FIELD, TABLE_LAYER = (
        "MAX_PAGES", "OVERLAP", "TITLE", "LANGUAGE", "INDEX_LAYER", "INDEX_FIELD", "TABLE_LAYER")
    COVER, CONTENTS, PER_PAGE, SEAL, OUTPUT, PAGES = ("COVER", "CONTENTS", "PER_PAGE", "SEAL", "OUTPUT", "PAGES")

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, tr("Pages from"),
            [tr("An automatic grid over the data"), tr("An index layer (one page per polygon)"),
             tr("Strips along a line (road, pipe, river)"), tr("One page per feature (parcels…)"),
             tr("The project's Map Stops")], defaultValue=0))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.LAYER, tr("Layer (index polygons, route lines or features)"), optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.NAME_FIELD, tr("Page names from"), parentLayerParameterName=self.LAYER, optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.PAPER, tr("Paper"), PAPER_KEYS, defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.ORIENTATION, tr("Orientation"), [tr("Best fit for each page"), tr("Landscape"), tr("Portrait")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.SCALE_MODE, tr("Scale"), [tr("The largest scale that fits each page"),
                                           tr("One scale for the whole book"), tr("This scale:")], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SCALE, tr("Scale 1: (0 = chosen for you)"), Qgis.ProcessingNumberParameterType.Integer, 0,
            minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_PAGES, tr("At most (pages)"), Qgis.ProcessingNumberParameterType.Integer, 24, minValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            self.OVERLAP, tr("Overlap with the next page (%)"), Qgis.ProcessingNumberParameterType.Integer, 8,
            minValue=0, maxValue=45))
        self.addParameter(QgsProcessingParameterString(self.TITLE, tr("Book title"), "", optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.LANGUAGE, tr("Book language"), [tr("The interface's language"), "العربية", "English", "Français",
                                                 "Español", "Português", "Türkçe", "فارسی"], defaultValue=0))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INDEX_LAYER, tr("Index of names from (streets, places…)"), optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.INDEX_FIELD, tr("Name field"), parentLayerParameterName=self.INDEX_LAYER, optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.TABLE_LAYER, tr("Table of the features on each page from"), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.COVER, tr("Cover with the key map"), True))
        self.addParameter(QgsProcessingParameterBoolean(self.CONTENTS, tr("Contents"), True))
        self.addParameter(QgsProcessingParameterEnum(
            self.PER_PAGE, tr("Files"), [tr("One PDF only"), tr("…and a PNG picture per page"),
                                         tr("…and a JPG picture per page"), tr("…and a PDF per page")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(self.SEAL, tr("Seal the PDF (proof it was not changed)"),
                                                        False))
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, tr("Map book (PDF)"), "PDF (*.pdf)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.PAGES, tr("Map book pages"), Qgis.ProcessingSourceType.VectorPolygon, optional=True,
            createByDefault=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..mapbook import book as B
        from ..mapbook import render

        project = context.project() or QgsProject.instance()
        src = SOURCE_KEYS[self.parameterAsEnum(parameters, self.SOURCE, context)]
        layer = self.parameterAsVectorLayer(parameters, self.LAYER, context)
        if src in ("index", "strips", "features") and layer is None:
            raise QgsProcessingException(tr("Choose the layer the pages come from."))
        index_layer = self.parameterAsVectorLayer(parameters, self.INDEX_LAYER, context)
        table_layer = self.parameterAsVectorLayer(parameters, self.TABLE_LAYER, context)
        name_field = (self.parameterAsStrings(parameters, self.NAME_FIELD, context) or [""])[0]
        lang = LANG_KEYS[self.parameterAsEnum(parameters, self.LANGUAGE, context)]
        book = B.new_book(
            name=self.parameterAsString(parameters, self.TITLE, context) or tr("Map book"),
            title=self.parameterAsString(parameters, self.TITLE, context), source=src,
            name_field=name_field, naming="given" if name_field or src == "stops" else "number",
            paper=PAPER_KEYS[self.parameterAsEnum(parameters, self.PAPER, context)],
            orientation=ORIENT_KEYS[self.parameterAsEnum(parameters, self.ORIENTATION, context)],
            scale_mode=SCALE_KEYS[self.parameterAsEnum(parameters, self.SCALE_MODE, context)],
            scale=self.parameterAsInt(parameters, self.SCALE, context),
            max_pages=self.parameterAsInt(parameters, self.MAX_PAGES, context),
            overlap=self.parameterAsInt(parameters, self.OVERLAP, context) / 100.0,
            lang=lang, index_field=(self.parameterAsStrings(parameters, self.INDEX_FIELD, context) or [""])[0],
            cover=self.parameterAsBoolean(parameters, self.COVER, context),
            contents=self.parameterAsBoolean(parameters, self.CONTENTS, context))
        book["_objects"] = {"layer": layer, "index_layer": index_layer, "table_layer": table_layer}
        if src == "grid" and layer is not None:
            book["_data"] = [layer]
        from .. import i18n

        try:
            pages = B.build(book, project, i18n.is_rtl(lang or None))
        except ValueError as e:
            raise QgsProcessingException(tr("The pages could not be made: {e}").format(e=e)) from e
        feedback.pushInfo(tr("{n} pages.").format(n=len(pages)))
        feedback.setProgress(20)
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        per_page = PER_PAGE_KEYS[self.parameterAsEnum(parameters, self.PER_PAGE, context)]
        res = render.export_book(book, pages, path, project, per_page=per_page,
                                 seal=self.parameterAsBoolean(parameters, self.SEAL, context),
                                 feedback=lambda pc, _text: feedback.setProgress(20 + pc * 0.8))
        crs = B.book_crs(book, project)
        fields = QgsFields()
        for name, typ in (("number", QMetaType.Type.Int), ("name", QMetaType.Type.QString),
                          ("scale", QMetaType.Type.Int), ("angle", QMetaType.Type.Double),
                          ("pdf_page", QMetaType.Type.Int)):
            fields.append(QgsField(name, typ))
        sink, sid = self.parameterAsSink(parameters, self.PAGES, context, fields, Qgis.WkbType.Polygon, crs)
        if sink is not None:
            bl = res["builder"]
            for p in pages:
                if p.excluded:
                    continue
                f = QgsFeature(fields)
                f.setGeometry(p.polygon())
                f.setAttributes([p.number, p.name, int(round(p.scale)), round(p.angle, 3), bl.page_of[p.number] + 1])
                sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("Map book saved: {n} map pages, {links} page links.").format(n=res["pages"],
                                                                                     links=res["links"]))
        out = {self.OUTPUT: path, "PAGE_COUNT": res["pages"], "LINKS": res["links"], "FILES": res["files"]}
        if sid:
            out[self.PAGES] = sid
        return out
