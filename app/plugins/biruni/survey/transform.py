"""Plane coordinate transformations fitted to control points (no Qt):
similarity (Helmert: shift, rotation, one scale) and affine."""
import math
import re

from .cogo import normalize_digits


def _solve(a, b):
    """Solve the square system a·x = b (Gaussian elimination, partial pivoting)."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-15:
            raise ValueError("singular")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


class PlaneTransform:
    """X = m11·x + m21·y + dx ;  Y = m12·x + m22·y + dy  (QTransform order)."""

    def __init__(self, m11, m12, m21, m22, dx, dy, kind):
        self.m11, self.m12, self.m21, self.m22, self.dx, self.dy = m11, m12, m21, m22, dx, dy
        self.kind = kind
        self.residuals = []

    def apply(self, x, y):
        return self.m11 * x + self.m21 * y + self.dx, self.m12 * x + self.m22 * y + self.dy

    @property
    def scale(self):
        return math.sqrt(abs(self.m11 * self.m22 - self.m12 * self.m21))

    @property
    def rotation(self):
        """Counter-clockwise rotation of the x axis, degrees."""
        return math.degrees(math.atan2(self.m12, self.m11))

    @property
    def rmse(self):
        if not self.residuals:
            return 0.0
        return math.sqrt(sum(r[2] ** 2 for r in self.residuals) / len(self.residuals))

    def qtransform_args(self):
        return self.m11, self.m12, self.m21, self.m22, self.dx, self.dy


def fit_similarity(pairs):
    """Least-squares similarity from [((x, y), (X, Y)), ...]; exact with 2 pairs."""
    if len(pairs) < 2:
        raise ValueError("at least two control points are needed")
    n = len(pairs)
    cx = sum(p[0][0] for p in pairs) / n
    cy = sum(p[0][1] for p in pairs) / n
    cX = sum(p[1][0] for p in pairs) / n
    cY = sum(p[1][1] for p in pairs) / n
    sxx = sxy = ss = 0.0
    for (x, y), (X, Y) in pairs:
        x, y, X, Y = x - cx, y - cy, X - cX, Y - cY
        sxx += x * X + y * Y
        sxy += x * Y - y * X
        ss += x * x + y * y
    if ss == 0:
        raise ValueError("control points coincide")
    a, b = sxx / ss, sxy / ss           # a = s·cos θ, b = s·sin θ
    dx = cX - (a * cx - b * cy)
    dy = cY - (b * cx + a * cy)
    t = PlaneTransform(a, b, -b, a, dx, dy, "similarity")
    _residuals(t, pairs)
    return t


def fit_rigid(pairs):
    """Shift and rotation only (scale fixed at 1) - least squares."""
    t = fit_similarity(pairs)
    s = t.scale
    a, b = t.m11 / s, t.m12 / s
    n = len(pairs)
    cx = sum(p[0][0] for p in pairs) / n
    cy = sum(p[0][1] for p in pairs) / n
    cX = sum(p[1][0] for p in pairs) / n
    cY = sum(p[1][1] for p in pairs) / n
    r = PlaneTransform(a, b, -b, a, cX - (a * cx - b * cy), cY - (b * cx + a * cy), "rigid")
    _residuals(r, pairs)
    return r


def fit_affine(pairs):
    if len(pairs) < 3:
        raise ValueError("at least three control points are needed")
    # Normal equations for X = a·x + b·y + c (and the same for Y).
    ata = [[0.0] * 3 for _ in range(3)]
    atx = [0.0] * 3
    aty = [0.0] * 3
    for (x, y), (X, Y) in pairs:
        row = (x, y, 1.0)
        for i in range(3):
            atx[i] += row[i] * X
            aty[i] += row[i] * Y
            for j in range(3):
                ata[i][j] += row[i] * row[j]
    a, b, c = _solve(ata, atx)
    d, e, f = _solve(ata, aty)
    t = PlaneTransform(a, d, b, e, c, f, "affine")
    _residuals(t, pairs)
    return t


def _residuals(t, pairs):
    t.residuals = []
    for (x, y), (X, Y) in pairs:
        px, py = t.apply(x, y)
        t.residuals.append((X - px, Y - py, math.hypot(X - px, Y - py)))


def parse_pairs(text):
    """'x1,y1>X1,Y1; x2,y2>X2,Y2' (also '->', '=>', new lines, spaces) -> pairs."""
    pairs = []
    t = normalize_digits(text or "")
    for chunk in re.split(r"[;\n]+", t):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = re.split(r"\s*(?:->|=>|>|→)\s*", chunk)
        if len(parts) != 2:
            raise ValueError(chunk)
        src, dst = (tuple(float(v) for v in re.split(r"[\s,]+", p.strip()) if v) for p in parts)
        if len(src) != 2 or len(dst) != 2:
            raise ValueError(chunk)
        pairs.append((src, dst))
    return pairs
