"""The model of click-to-digitise, an optional download: MobileSAM (Segment Anything, light), on the CPU.

Not in the installer: downloaded once, with consent, from a fixed revision, every file checked against its SHA-256;
or brought on a flash drive and imported from a folder (offices without internet). Kept in Biruni's settings folder,
used offline after that.

Licences, all free for any use, companies and governments included:
  * MobileSAM weights and code: Apache-2.0 (https://github.com/ChaoningZhang/MobileSAM/blob/master/LICENSE)
  * Segment Anything mask decoder: Apache-2.0 (https://github.com/facebookresearch/segment-anything/blob/main/LICENSE)
  * the ONNX export of both: MIT (https://huggingface.co/Acly/MobileSAM)
"""
import hashlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request

import numpy as np
from qgis.core import QgsApplication

from . import core

REVISION = "0d3b403339b4674a82493d5e97964dd78089ddc8"
BASE = f"https://huggingface.co/Acly/MobileSAM/resolve/{REVISION}/"
MODEL = {
    "id": "mobilesam",
    "name": "MobileSAM (Segment Anything)",
    "license": "Apache-2.0 (weights) / MIT (ONNX export)",
    "license_url": "https://github.com/ChaoningZhang/MobileSAM/blob/master/LICENSE",
    "source": "https://huggingface.co/Acly/MobileSAM",
    "files": [
        {"name": "mobile_sam_image_encoder.onnx", "size": 28157093,
         "sha256": "580f5fb648ea1062c0aabc26217aed56921985f03f0cbbd852bba81d760cc749"},
        {"name": "sam_mask_decoder_multi.onnx", "size": 16496559,
         "sha256": "8976b90a87ba50a6a72217a5ff994f7d25ce16f2229fcc1ed259e1294c622ffe"},
    ],
}


class ModelError(Exception):
    pass


def total_size(model=MODEL):
    return sum(f["size"] for f in model["files"])


def models_dir():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "models")


def model_dir(model=MODEL, root=None):
    return os.path.join(root or models_dir(), model["id"])


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def installed(model=MODEL, root=None):
    """True when every file is there, of its size, and was checked when it came (a note of it beside them)."""
    d = model_dir(model, root)
    note = os.path.join(d, "checked.json")
    try:
        with open(note, encoding="utf-8") as f:
            ok = json.load(f)
    except (OSError, ValueError):
        return False
    for spec in model["files"]:
        p = os.path.join(d, spec["name"])
        if not os.path.isfile(p) or os.path.getsize(p) != spec["size"] or ok.get(spec["name"]) != spec["sha256"]:
            return False
    return True


def _note(model, root):
    d = model_dir(model, root)
    with open(os.path.join(d, "checked.json"), "w", encoding="utf-8") as f:
        json.dump({s["name"]: s["sha256"] for s in model["files"]}, f, indent=1)


def http_get(url, dest, progress=None, timeout=30):
    """Download ``url`` to ``dest``; ``progress(done, total)`` may return False to stop."""
    req = urllib.request.Request(url, headers={"User-Agent": "Biruni-GIS"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:   # noqa: S310 - fixed https
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            b = resp.read(1 << 16)
            if not b:
                break
            out.write(b)
            done += len(b)
            if progress is not None and progress(done, total) is False:
                raise ModelError("cancelled")


def download(model=MODEL, root=None, fetch=http_get, progress=None):
    """Download the model (each file checked against its SHA-256 before it is kept)."""
    d = model_dir(model, root)
    os.makedirs(d, exist_ok=True)
    base_done = 0
    for spec in model["files"]:
        dest = os.path.join(d, spec["name"])
        if os.path.isfile(dest) and os.path.getsize(dest) == spec["size"] and sha256_of(dest) == spec["sha256"]:
            base_done += spec["size"]
            continue
        part = dest + ".part"

        def each(done, _total, base=base_done):
            return None if progress is None else progress(base + done, total_size(model))
        try:
            fetch(BASE + spec["name"], part, each)
        except (urllib.error.URLError, OSError) as e:
            _drop(part)
            raise ModelError("offline") from e
        except ModelError:
            _drop(part)
            raise
        if sha256_of(part) != spec["sha256"]:
            _drop(part)
            raise ModelError("checksum")
        os.replace(part, dest)
        base_done += spec["size"]
    _note(model, root)
    return d


def import_from(folder, model=MODEL, root=None):
    """Bring the model from a folder (a flash drive): its files found by name anywhere under it, each checked."""
    found = {}
    for dirpath, _dirs, files in os.walk(folder):
        for spec in model["files"]:
            if spec["name"] in files and spec["name"] not in found:
                found[spec["name"]] = os.path.join(dirpath, spec["name"])
    missing = [s["name"] for s in model["files"] if s["name"] not in found]
    if missing:
        raise ModelError("missing: " + ", ".join(missing))
    for spec in model["files"]:
        if sha256_of(found[spec["name"]]) != spec["sha256"]:
            raise ModelError("checksum")
    d = model_dir(model, root)
    os.makedirs(d, exist_ok=True)
    for spec in model["files"]:
        dest = os.path.join(d, spec["name"])
        if os.path.abspath(found[spec["name"]]) != os.path.abspath(dest):
            shutil.copyfile(found[spec["name"]], dest + ".part")
            os.replace(dest + ".part", dest)
    _note(model, root)
    return d


def remove(model=MODEL, root=None):
    shutil.rmtree(model_dir(model, root), ignore_errors=True)


def _drop(p):
    try:
        os.remove(p)
    except OSError:
        pass


def runtime_ok():
    from ..vendor import ensure_vendor
    ensure_vendor()        # onnxruntime is bundled in <app>/python
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


# ---- running it ------------------------------------------------------------------------------------------------
def _resize(img, w, h):
    from PIL import Image
    return np.asarray(Image.fromarray(img).resize((w, h), Image.BILINEAR))


class Segmenter:
    """The model on the CPU: ``encode`` a tile once (seconds), then ``segment`` for every click (a tenth of one)."""

    def __init__(self, model=MODEL, root=None, threads=0):
        if not runtime_ok():
            raise ModelError("runtime")
        if not installed(model, root):
            raise ModelError("not installed")
        import onnxruntime as ort
        d = model_dir(model, root)
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        self.encoder = ort.InferenceSession(os.path.join(d, model["files"][0]["name"]), opts,
                                            providers=["CPUExecutionProvider"])
        self.decoder = ort.InferenceSession(os.path.join(d, model["files"][1]["name"]), opts,
                                            providers=["CPUExecutionProvider"])
        self.last_encode = 0.0
        self.last_decode = 0.0

    def encode(self, tile):
        """The tile's embedding (kept on the tile: a tile is encoded once)."""
        if tile.embedding is not None:
            return tile.embedding
        t0 = time.perf_counter()
        scale = core.resize_scale(tile.width, tile.height)
        w, h = max(1, round(tile.width * scale)), max(1, round(tile.height * scale))
        img = tile.image if (w, h) == (tile.width, tile.height) else _resize(tile.image, w, h)
        emb = self.encoder.run(None, {"input_image": img.astype(np.float32)})[0]
        tile.embedding = (emb, scale)
        tile.low_res = None
        self.last_encode = time.perf_counter() - t0
        return tile.embedding

    def segment(self, tile, points, box=None, refine=True):
        """The mask (bool, h x w of the tile) for clicks [(x, y, positive)] and/or a box (QgsRectangle), both in
        the layer's coordinates, with the model's own score of it."""
        emb, scale = self.encode(tile)
        t0 = time.perf_counter()
        coords, labels = core.model_points(tile, points, scale)
        if box is not None:
            bc, bl = core.box_points(tile, box, scale)
            coords, labels = coords + bc, labels + bl
        else:
            coords, labels = coords + [[0.0, 0.0]], labels + [-1.0]     # no box: the padding point
        prev = getattr(tile, "low_res", None) if refine and len(points) > 1 else None
        feeds = {
            "image_embeddings": emb,
            "point_coords": np.array([coords], dtype=np.float32),
            "point_labels": np.array([labels], dtype=np.float32),
            "mask_input": prev if prev is not None else np.zeros((1, 1, 256, 256), dtype=np.float32),
            "has_mask_input": np.array([1.0 if prev is not None else 0.0], dtype=np.float32),
            "orig_im_size": np.array([tile.height, tile.width], dtype=np.float32),
        }
        masks, scores, low = self.decoder.run(None, feeds)
        if len(points) == 1 and box is None:          # one click: the best of the model's three guesses
            i = int(np.argmax(scores[0][1:])) + 1
        else:
            i = 0
        tile.low_res = low[:, i:i + 1, :, :]
        self.last_decode = time.perf_counter() - t0
        return masks[0, i] > 0.0, float(scores[0][i])
