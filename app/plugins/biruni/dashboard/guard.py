"""«Who leaked this screenshot?» — an invisible mark per card (PC side of ext_guard).

Every card sees the dashboard through a faint pattern that belongs to that card only: a tile of 192×192 CSS
pixels, repeated over the whole window, made of two independent ±1 patterns drawn from the card's own key:

* luminance: 12×12 cells of 16 px, one grey level up or down (added with ``plus-lighter``, taken away with
  ``difference``, so it works on dark and light themes alike);
* chroma: 6×6 cells of 32 px, one level of blue up or down (the eye is least sensitive there; the cells are
  big enough for JPEG's halved colour resolution).

Big cells and a zero-mean ±1 keep the mark under any visible step (PSNR ≈ 48 dB against the same page without
it) while it survives what a screenshot goes through: half size, JPEG, WhatsApp, a crop of one widget, a gamma
change or a soft blur. Nothing in the numbers, the text or what is copied changes.

Finding it in a picture: the picture is high-passed (its content removed, edges clipped), the tile's period is
searched (any scale: a phone, a 50 % copy, a zoomed browser), every pixel is folded onto one tile (the content
averages away, the mark adds up), and the folded tile is correlated with every issued card's pattern at every
shift (FFT). A real mark gives one sharp peak far above the others; the answer names the card, how sure (a z
score) and the runner-up, or says no mark was found.
"""
import hashlib
import hmac

import numpy as np

N_L, CELL_L = 12, 16         # luminance cells per tile side, CSS px per cell
N_C, CELL_C = 6, 32          # chroma
TILE = N_L * CELL_L          # 192 CSS px
AMP_L, AMP_C = 1, 1          # levels (±)
BINS = 96                    # the folded tile's resolution
THRESHOLD = 8.0              # combined z score for "found" (unmarked screenshots stay under ≈ 7)
MARGIN = 2.0                 # and that far above every other card
_TEST_SECRET = []


def secret():
    """This PC's guard secret (from the dashboard signing key, never stored on its own)."""
    if _TEST_SECRET:
        return _TEST_SECRET[0]
    from . import cards

    return hmac.new(cards.seed(), b"biruni-dashboard-guard-1", hashlib.sha256).digest()


def card_key(card_id, sec=None):
    return hmac.new(sec or secret(), ("card:" + str(card_id)).encode("utf-8"), hashlib.sha256).digest()


def _bits(key, label, n):
    """n ±1 values from the key (sha256 counter stream), exactly half of each."""
    out = bytearray()
    i = 0
    while len(out) < 4 * n:
        out += hashlib.sha256(key + label + i.to_bytes(4, "big")).digest()
        i += 1
    order = np.argsort(np.frombuffer(bytes(out[:4 * n]), dtype=">u4"), kind="stable")
    v = np.full(n, -1, dtype=np.int8)
    v[order[: n // 2]] = 1
    return v


def patterns(card_id, sec=None):
    """(luminance N_L×N_L, chroma N_C×N_C) of ±1 for one card."""
    k = card_key(card_id, sec)
    return (_bits(k, b"L", N_L * N_L).reshape(N_L, N_L), _bits(k, b"C", N_C * N_C).reshape(N_C, N_C))


def to_hex(p):
    """A ±1 pattern as hex bits (1 = +1), row by row - what the page gets."""
    return np.packbits((p.ravel() > 0).astype(np.uint8)).tobytes().hex()


def message(card_id, sec=None):
    L, C = patterns(card_id, sec)
    return {"t": "guard", "L": to_hex(L), "C": to_hex(C), "nl": N_L, "cl": CELL_L, "nc": N_C, "cc": CELL_C,
            "al": AMP_L, "ac": AMP_C}


# ---- the page asks for its mark -------------------------------------------------------------------------------------
try:
    from .service import handler

    @handler("guard_hello")
    def _hello(service, session, msg):
        card = getattr(session, "card", None)
        if not card or not card.get("id"):
            session._out({"t": "guard", "none": True})   # the builder's preview: nothing to trace
            return
        session._out(message(card["id"]))
except ImportError:   # pragma: no cover - outside the plugin
    pass


# ---- reading a picture ------------------------------------------------------------------------------------------------
def image_array(img):
    """RGB float array (h, w, 3) from a QImage, a path or an array."""
    if isinstance(img, np.ndarray):
        return img.astype(np.float32)
    from qgis.PyQt.QtGui import QImage

    if isinstance(img, str):
        q = QImage(img)
        if q.isNull():
            raise ValueError("not a picture")
    else:
        q = img
    q = q.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = q.width(), q.height(), q.bytesPerLine()
    a = np.frombuffer(q.constBits().asstring(bpl * h), dtype=np.uint8).reshape(h, bpl)[:, :w * 3]
    return a.reshape(h, w, 3).astype(np.float32)


def _box(x, r):
    """Mean over a (2r+1)² box (edges by reflection) with cumulative sums."""
    r = int(max(1, min(r, (min(x.shape) - 2) // 2)))
    p = np.pad(x, r + 1, mode="reflect")
    c = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s[:x.shape[0], :x.shape[1]] / (k * k)


def planes(rgb, scale=1.0):
    """(luminance, chroma) planes with the content taken away: a wide high-pass (the cells are kept), the
    remaining edges clipped to a few levels so text and lines cannot outweigh the mark."""
    R, G, B = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    Y = 0.299 * R + 0.587 * G + 0.114 * B
    C = B - 0.5 * (R + G)
    r = max(3, int(round(20 * scale)))
    yp = np.clip(Y - _box(Y, r), -2.5, 2.5)
    cp = np.clip(C - _box(C, 2 * r), -2.5, 2.5)
    return yp, cp


def fold(hp, period, n=BINS):
    """Fold the plane onto one tile of n×n bins for a tile period of ``period`` picture pixels (zero mean)."""
    h, w = hp.shape
    xb = np.floor((np.arange(w) % period) / period * n).astype(np.int64).clip(0, n - 1)
    yb = np.floor((np.arange(h) % period) / period * n).astype(np.int64).clip(0, n - 1)
    idx = (yb[:, None] * n + xb[None, :]).ravel()
    s = np.bincount(idx, weights=hp.ravel(), minlength=n * n)
    c = np.bincount(idx, minlength=n * n).astype(np.float64)
    f = np.where(c > 0, s / np.maximum(c, 1), 0.0)
    if (c > 0).any():
        f = np.where(c > 0, f - f[c > 0].mean(), 0.0)
    return f.reshape(n, n), c.reshape(n, n)


def _energy(hp, period):
    f, c = fold(hp, period)
    v = hp.var() or 1.0
    return float(f.var() * max(c.mean(), 1) / v)


def _periods(hp, lo, hi, top=5):
    """Candidate tile periods from the picture's autocorrelation along x and along y."""
    h, w = hp.shape
    out = []
    for axis, size in ((1, w), (0, h)):
        if size < 2 * lo + 4:
            continue
        P = (np.abs(np.fft.rfft(hp, axis=axis)) ** 2).mean(axis=1 - axis)
        acf = np.fft.irfft(P, n=size)
        acf = acf / (acf[0] or 1)
        top_lag = int(min(hi, size - 2))
        cand = [i for i in range(int(lo) + 1, top_lag) if acf[i] > acf[i - 1] and acf[i] >= acf[i + 1]]
        cand.sort(key=lambda i: -acf[i])
        for i in cand[:top]:
            a, b, c = acf[i - 1], acf[i], acf[i + 1]
            den = a - 2 * b + c
            d = 0.5 * (a - c) / den if den else 0.0
            out.append(float(i + max(-0.5, min(0.5, d))))
    return out


def _refine(hp, p0):
    best, bp = _energy(hp, p0), p0
    for span, step in ((0.03, 0.004), (0.005, 0.0007)):
        base = bp
        for k in np.arange(-span, span + 1e-9, step):
            p = base * (1 + k)
            e = _energy(hp, p)
            if e > best:
                best, bp = e, p
    return bp, best


def _up(p, n=BINS):
    """A ±1 cell pattern as an n×n tile (cells stretched to the bins), zero mean."""
    k = n // p.shape[0]
    t = np.kron(p.astype(np.float64), np.ones((k, k)))
    return t - t.mean()


def _zcorr(f, spec):
    c = np.fft.irfft2(np.fft.rfft2(f) * spec, s=f.shape)
    s = c.std() or 1.0
    return (c - c.mean()) / s


def _specs(candidates, sec):
    out = []
    for cid, name in candidates:
        L, C = patterns(cid, sec)
        out.append((cid, name, np.conj(np.fft.rfft2(_up(L))), np.conj(np.fft.rfft2(_up(C)))))
    return out


def detect(img, candidates, sec=None, threshold=THRESHOLD):
    """Which card's mark is in the picture.

    candidates: [(card_id, name)]. -> {"found": bool, "card", "name", "score", "second": {card, name, score} or
    None, "period": picture px per tile, "scale": period / tile}"""
    rgb = image_array(img)
    h, w = rgb.shape[:2]
    if h < 64 or w < 64:
        return {"found": False, "why": "too small", "score": 0.0, "second": None}
    specs = _specs(candidates, sec)
    if not specs:
        return {"found": False, "why": "no cards", "score": 0.0, "second": None}
    lo, hi = 0.25 * TILE, min(3.2 * TILE, max(h, w) * 0.95)
    yp0, _cp0 = planes(rgb)
    acf = _periods(yp0, lo, hi)
    # the autocorrelation may show a multiple of the tile: its halves and thirds are tried too
    cands = acf + [p / k for p in acf for k in (2, 3)] + [TILE * s for s in (0.33, 0.4, 0.5, 0.6, 0.667, 0.75, 0.8,
                                                                             0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0,
                                                                             2.5, 3.0)]
    cands = sorted(p for p in cands if lo <= p <= hi)
    tried = []
    for p in cands:
        if any(abs(p - q) / q < 0.006 for q in tried):
            continue
        tried.append(p)
    planes_at = {}

    def at(scale):
        k = round(scale * 4) / 4 or 0.25
        if k not in planes_at:
            planes_at[k] = planes(rgb, k)
        return planes_at[k]

    scores = {}
    done = []
    for p in tried:
        yp, cp = at(p / TILE)
        rp, _e = _refine(yp, p)
        if any(abs(rp - q) / q < 0.0015 for q in done):
            continue
        done.append(rp)
        fy, _ = fold(yp, rp)
        fc, _ = fold(cp, rp)
        for cid, name, sl, sc in specs:
            z = _zcorr(fy, sl) + _zcorr(fc, sc)
            m = float(z.max())
            if m > scores.get(cid, (-1e9,))[0]:
                scores[cid] = (m, name, rp)
    ranked = sorted(((v[0], cid, v[1], v[2]) for cid, v in scores.items()), reverse=True)
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    found = top[0] >= threshold and (second is None or top[0] - second[0] >= MARGIN)
    return {"found": bool(found), "card": top[1], "name": top[2], "score": round(float(top[0]), 2),
            "second": ({"card": second[1], "name": second[2], "score": round(float(second[0]), 2)} if second else None),
            "period": round(float(top[3]), 2), "scale": round(float(top[3]) / TILE, 3)}


def issued_candidates(project=None):
    """[(card id, name)] of every card issued on this PC's dashboards in the project."""
    from . import model

    out = []
    for r in model.issued_cards(project):
        if r.get("id"):
            out.append((r["id"], r.get("name") or r["id"]))
    return out


def who_ui(plugin, dock=None):
    """ACTION: pick a screenshot, say which card it came from."""
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QApplication, QFileDialog, QMessageBox

    from ..i18n import tr

    title = tr("Who leaked this screenshot?")
    parent = dock
    path, _ = QFileDialog.getOpenFileName(parent, title, "", tr("Pictures") + " (*.png *.jpg *.jpeg *.webp *.bmp)")
    if not path:
        return None
    cands = issued_candidates()
    if not cands:
        QMessageBox.information(parent, title, tr("No cards have been issued yet."))
        return None
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        r = detect(path, cands)
    except Exception as e:  # noqa: BLE001
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(parent, title, str(e))
        return None
    QApplication.restoreOverrideCursor()
    if r.get("found"):
        text = tr("This picture came from the card «{name}» (certainty {score}).").format(name=r["name"],
                                                                                          score=r["score"])
        if r.get("second"):
            text += "\n" + tr("Next closest card: «{name}» ({score}).").format(name=r["second"]["name"],
                                                                                score=r["second"]["score"])
    else:
        text = tr("No card's mark was found in this picture.") + "\n" + tr(
            "It may not come from a shared dashboard, or it was changed too much (a photo at an angle, heavy "
            "blur or a very small copy).")
    QMessageBox.information(parent, title, text)
    return r
