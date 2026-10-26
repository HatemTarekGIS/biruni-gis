"""Check QGIS 4 Python API names and algorithm ids used by Biruni (standalone)."""
import sys

from qgis.core import Qgis, QgsApplication

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

checks = [
    "Qgis.ProcessingSourceType.VectorPolygon", "Qgis.ProcessingSourceType.VectorAnyGeometry",
    "Qgis.ProcessingSourceType.Vector", "Qgis.ProcessingNumberParameterType.Double",
    "Qgis.ProcessingNumberParameterType.Integer", "Qgis.ProcessingAlgorithmFlag.NoThreading",
    "Qgis.GeometryType.Polygon", "Qgis.WkbType.MultiPolygon", "Qgis.AreaUnit.Hectares",
    "Qgis.AreaUnit.Acres", "Qgis.DistanceUnit.Miles", "Qgis.ProcessingFieldParameterDataType.Any",
]
for c in checks:
    try:
        eval(c)
        print("OK  ", c)
    except Exception as e:
        print("FAIL", c, e)

import qgis.core as qc  # noqa: E402

for n in ["QgsProcessingParameterFeatureSource", "QgsProcessingParameterFeatureSink", "QgsProcessingParameterNumber",
          "QgsProcessingParameterEnum", "QgsProcessingParameterField", "QgsProcessingParameterExpression",
          "QgsProcessingParameterString", "QgsProcessingParameterCrs", "QgsProcessingParameterBoolean",
          "QgsProcessingParameterFolderDestination", "QgsProcessingParameterMultipleLayers", "QgsDistanceArea",
          "QgsSpatialIndex", "QgsVectorFileWriter", "QgsCurvePolygon", "QgsPolygon"]:
    print("OK  " if hasattr(qc, n) else "FAIL", n)
print("ActionOnExistingFile:", [a for a in dir(qc.QgsVectorFileWriter.ActionOnExistingFile) if not a.startswith("_")][:6])

reg = QgsApplication.processingRegistry()
ids = """native:reprojectlayer native:savefeatures gdal:convertformat native:createpointslayerfromtable native:pointstopath
qgis:linestopolygons native:polygonstolines native:explodelines native:multiparttosingleparts native:collect native:centroids
native:pointonsurface gdal:rasterize gdal:polygonize native:package native:buffer native:multiringconstantbuffer native:clip
native:intersection native:union native:difference native:symmetricaldifference native:dissolve native:splitwithlines
native:extractbylocation native:joinattributesbylocation native:joinattributesbynearest native:joinattributestable
native:voronoipolygons native:convexhull native:minimumboundinggeometry native:extractbyattribute native:extractbyexpression
native:selectbylocation native:randomextract native:splitvectorlayer native:exportaddgeometrycolumns native:fieldcalculator
native:pointsalonglines native:densifygeometriesgivenaninterval native:simplifygeometries native:smoothgeometry
native:offsetline native:linesubstring native:lineintersections native:shortestline native:hublines
qgis:basicstatisticsforfields qgis:statisticsbycategories native:countpointsinpolygon native:sumlinelengths
native:nearestneighbouranalysis qgis:distancematrix qgis:heatmapkerneldensityestimation native:heatmapkerneldensityestimation
native:kmeansclustering native:dbscanclustering native:meancoordinates native:zonalstatisticsfb native:checkvalidity
native:fixgeometries native:deleteduplicategeometries native:removenullgeometries native:removeduplicatevertices
native:snapgeometries native:deleteholes native:slope native:aspect native:hillshade gdal:contour gdal:cliprasterbymasklayer
gdal:cliprasterbyextent gdal:warpreproject gdal:merge native:reclassifybytable native:rastercalc qgis:rastercalculator
gdal:viewshed native:rastersampling gdal:fillnodata native:shortestpathpointtopoint native:serviceareafrompoint
native:shortestpathpointtolayer native:creategrid native:polygonfromlayerextent native:printlayouttopdf native:printlayouttoimage
native:atlaslayouttopdf native:exporttospreadsheet native:dxfexport native:setlayerstyle native:extractvertices
native:geometrybyexpression native:mergevectorlayers native:retainfields native:refactorfields native:renametablefield
native:addautoincrementalfield native:orderbyexpression native:batchnominatimgeocoder native:tininterpolation qgis:idwinterpolation
native:idwinterpolation native:rasterlayerzonalstats native:extractzvalues native:setzfromraster native:linedensity
native:splitlinesbylength native:rotatefeatures native:translategeometry native:affinetransform native:subdivide
native:polygonize native:extendlines native:extractspecificvertices native:interpolatepoint native:randompointsinpolygons
native:joinbylocationsummary native:aggregate native:filedownloader native:importphotos native:kmlexport
native:pixelstopoints native:pixelstopolygons native:rasterize native:fuzzifyrasterlinearmembership native:roundrastervalues""".split()
missing = [i for i in ids if reg.algorithmById(i) is None]
print("MISSING ALGS:", missing)
app.exitQgis()
