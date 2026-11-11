r"""Small, documented changes to the QGIS engine that Biruni ships (run by build.ps1; safe to run again).

1. QGIS 4's qgis.core imports geopandas (with pandas and shapely) at start-up only to offer
   QgsVectorLayer.as_geopandas(). That costs about 0.3-0.6 s and 45-70 MB on every start, for a method
   almost nobody calls. Here the import moves inside the method: it behaves the same when called.

2. Shapely: the engine's 2.1 has no coverage_clean (the parcel coverage doctor fixes the gaps and overlaps of a
   whole parcel layer with it). The wheel kept in _vendor\wheels (from PyPI, BSD-3, with its own GEOS inside
   shapely.libs) takes the place of the engine's Shapely. 2.2 keeps the 2.x API, so the rest goes on as before.

3. Chromium's sandbox: QtWebEngineProcess.exe (apps' qt6 bin folder) needs six libraries that live only in the
   engine's bin folder. A sandboxed process does not search PATH, so with the sandbox on every web page died at
   start (0xC0000135, DLL not found) and the hidden bridge page had to run with --no-sandbox. Copies of the six
   libraries are put beside QtWebEngineProcess.exe; field_photos/bridge.py turns the sandbox on when they are there.

usage: python scripts\patch_engine.py [engine folder]   (default: engine\ next to this script's folder)
"""
import os
import sys

MARK = "# biruni: geopandas is imported when as_geopandas() is called (scripts/patch_engine.py)"

OLD = """try:
   import geopandas as _gpd

   def _vector_layer_as_geopandas(self) -> _gpd.GeoDataFrame:
       \"\"\"
       Returns the vector layer as a GeoPandas GeoDataFrame.

       :raises QgsNotSupportedException: if geopandas is not available on the system

       .. versionadded:: 4.0
       \"\"\"
       features = list(self.getFeatures())
"""

NEW = MARK + """
try:
   import importlib.util as _ilu
   if _ilu.find_spec("geopandas") is None:
      raise ModuleNotFoundError("geopandas")

   def _vector_layer_as_geopandas(self):
       \"\"\"
       Returns the vector layer as a GeoPandas GeoDataFrame.

       :raises QgsNotSupportedException: if geopandas is not available on the system

       .. versionadded:: 4.0
       \"\"\"
       import geopandas as _gpd

       features = list(self.getFeatures())
"""


def patch(engine):
    path = os.path.join(engine, "apps", "qgis", "python", "qgis", "core", "__init__.py")
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if MARK in text:
        return "already patched"
    old = OLD.replace("\n", "\r\n") if "\r\n" in text and OLD not in text else OLD
    new = NEW.replace("\n", "\r\n") if old != OLD else NEW
    if text.count(old) != 1:
        return "NOT patched: the QGIS code changed - check scripts/patch_engine.py"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace(old, new))
    return "patched"



def upgrade_shapely(engine, wheels=None):
    import glob
    import shutil
    import zipfile
    here = os.path.dirname(os.path.abspath(__file__))
    wheels = wheels or os.path.join(here, "..", "_vendor", "wheels")
    found = sorted(glob.glob(os.path.join(wheels, "shapely-*-cp312-cp312-win_amd64.whl")))
    if not found:
        return "NOT upgraded: no Shapely wheel in _vendor\wheels"
    wheel = found[-1]
    version = os.path.basename(wheel).split("-")[1]
    site = os.path.join(engine, "apps", "Python312", "Lib", "site-packages")
    if os.path.isdir(os.path.join(site, f"shapely-{version}.dist-info")):
        return f"already {version}"
    for old in glob.glob(os.path.join(site, "shapely-*.dist-info")) + [os.path.join(site, "shapely"),
                                                                       os.path.join(site, "shapely.libs")]:
        if os.path.isdir(old):
            shutil.rmtree(old)
    with zipfile.ZipFile(wheel) as z:
        z.extractall(site)
    return f"upgraded to {version}"


SANDBOX_DLLS = ("brotlicommon.dll", "brotlidec.dll", "freetype.dll", "libpng16.dll", "zlib.dll", "zstd.dll")


def sandbox_dlls(engine):
    src, dst = os.path.join(engine, "bin"), os.path.join(engine, "apps", "qt6", "bin")
    if not os.path.isfile(os.path.join(dst, "QtWebEngineProcess.exe")):
        return "NOT done: no QtWebEngineProcess.exe in apps~qt6~bin".replace("~", chr(92))
    done = []
    for name in SANDBOX_DLLS:
        a, b = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.isfile(a):
            return "NOT done: %s is not in the engine's bin folder - check scripts/patch_engine.py" % name
        if not os.path.isfile(b) or os.path.getsize(a) != os.path.getsize(b):
            shutil.copy2(a, b)
            done.append(name)
    return ("copied " + ", ".join(done)) if done else "already there"


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    engine = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "engine")
    print("lazy geopandas:", patch(engine))
    print("shapely:", upgrade_shapely(engine))
    print("chromium sandbox libraries:", sandbox_dlls(engine))
