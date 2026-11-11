"""Living paper: a QR code on a print layout that opens the printed map on the phone, with the reader's position.

Everything the phone shows travels inside the code (living_paper/codec.py); the phone page is static and receives
nothing - no server, no account.
"""
from qgis.core import (Qgis, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputString,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField, QgsProcessingParameterLayout,
                       QgsProcessingParameterNumber, QgsProcessingParameterString)

from ..i18n import tr
from .base import BiruniAlgorithm


class LiveQr(BiruniAlgorithm):
    ID = "liveqr"
    NAME = "Add a live QR to a print layout"
    GROUP = "Cartography"
    GROUP_ID = "cartography"
    HELP = ("Puts a QR code on a print layout. Scanned with a phone camera, it opens the area of the printed map "
            "on a live map with the reader's own position, and tells whether they are inside the map or how far "
            "and which way it is. Up to 40 points, or simplified outlines, of one layer can travel with it. Nothing "
            "goes through a server: the map is inside the code itself. Running it again replaces the code and keeps "
            "the place it was moved to.")

    LAYOUT, LAYER, LABEL_FIELD, TITLE, MAX_CHARS = "LAYOUT", "LAYER", "LABEL_FIELD", "TITLE", "MAX_CHARS"

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        from ..living_paper.codec import MAX_CHARS

        self.addParameter(QgsProcessingParameterLayout(self.LAYOUT, tr("Print layout")))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LAYER, tr("Features to carry (points, lines or polygons)"),
            [Qgis.ProcessingSourceType.VectorAnyGeometry], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.LABEL_FIELD, tr("Label field"), parentLayerParameterName=self.LAYER, optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.TITLE, tr("Title on the phone (empty: the map's title)"), optional=True))
        longest = QgsProcessingParameterNumber(
            self.MAX_CHARS, tr("Longest address (characters)"), Qgis.ProcessingNumberParameterType.Integer,
            MAX_CHARS, minValue=200, maxValue=2900)
        longest.setFlags(longest.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(longest)
        self.addOutput(QgsProcessingOutputString("URL", tr("Address in the code")))
        self.addOutput(QgsProcessingOutputString("CARRIED", tr("What the code carries")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..living_paper.layout import add_live_qr, describe

        layout = self.parameterAsLayout(parameters, self.LAYOUT, context)
        if layout is None:
            raise QgsProcessingException(tr("Choose a print layout."))
        source = self.parameterAsSource(parameters, self.LAYER, context)
        field = (self.parameterAsStrings(parameters, self.LABEL_FIELD, context) or [None])[0]
        title = self.parameterAsString(parameters, self.TITLE, context).strip() or None
        longest = self.parameterAsInt(parameters, self.MAX_CHARS, context)
        try:
            url, report, _pic = add_live_qr(layout, source, field, title, max_chars=longest)
        except ValueError as e:
            raise QgsProcessingException(str(e)) from e
        carried = describe(report)
        feedback.pushInfo(carried)
        feedback.pushInfo(url)
        return {"URL": url, "CARRIED": carried, "POINTS": report["points"], "SHAPES": report["shapes"],
                "QR_VERSION": report["qr_version"]}
