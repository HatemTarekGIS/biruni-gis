"""The separate process one tile runs in: python runner.py <folder>.

<folder> is the private folder the worker made for the tile; it holds tile.json ({"task", "params", "data"}) and
gets result.json ({"result"} or {"error"}). Started at low priority with a time limit (worker.run_local); a tool
that crashes or hangs takes only this process with it.
"""
import json
import os
import sys
import traceback


def main(folder):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    here = os.path.dirname(os.path.abspath(__file__))
    plugins = os.path.dirname(os.path.dirname(here))
    if plugins not in sys.path:
        sys.path.insert(0, plugins)
    out = os.path.join(folder, "result.json")
    try:
        with open(os.path.join(folder, "tile.json"), encoding="utf-8") as f:
            spec = json.load(f)
        from qgis.analysis import QgsNativeAlgorithms
        from qgis.core import QgsApplication, QgsProcessingProvider

        app = QgsApplication([], False)
        app.initQgis()
        reg = QgsApplication.processingRegistry()
        if reg.providerById("native") is None:
            reg.addProvider(QgsNativeAlgorithms())
        from biruni.team_compute import compute, jobs

        if jobs.TASKS.get(spec.get("task"), {}).get("alg", "").startswith("biruni:"):
            from biruni.algorithms.overlaps_gaps import OverlapsGaps

            class _Provider(QgsProcessingProvider):
                def id(self):
                    return "biruni"

                def name(self):
                    return "Biruni GIS"

                def loadAlgorithms(self):  # noqa: N802
                    self.addAlgorithm(OverlapsGaps())

            prov = _Provider()
            reg.addProvider(prov)
        result = compute.run_tile(spec["task"], spec.get("params") or {}, spec["data"], folder)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"result": result}, f)
    except Exception as e:  # noqa: BLE001 - the worker reports it back
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]}, f)
    sys.stdout.flush()
    os._exit(0)     # no QGIS teardown: the answer is written and the process is not reused


if __name__ == "__main__":
    main(sys.argv[1])
