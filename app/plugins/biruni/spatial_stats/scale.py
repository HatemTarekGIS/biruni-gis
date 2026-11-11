"""Scale check (the modifiable areal unit problem, MAUP): the same events counted in hexagons of several
sizes. If the clustering (global Moran's I) and the hot spots are found at every size, the conclusion is
robust; if only at some sizes, it depends on the cell size chosen - which is worth knowing before a map
of hot spots is shown to anyone."""
import numpy as np

from . import autocorr as ac
from . import weights as wts

DEFAULT_FACTORS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)


def parse_factors(text):
    vals = []
    for part in str(text or "").replace(";", ",").split(","):
        try:
            v = float(part.strip())
        except ValueError:
            continue
        if v > 0:
            vals.append(v)
    return sorted(set(vals)) or list(DEFAULT_FACTORS)


def one_size(xy, weights, width, permutations, rng, use_fdr=True, min_cells=30):
    """Moran's I of the hexagon counts (six touching hexagons as neighbours) and the share of hot spot
    cells (Gi*, 95% confidence, neighbours within two cell widths). None when too few cells."""
    grid = wts.hex_cells(xy, width, weights=weights, fill_hull=True)
    cells = grid["centres"]
    y = grid["counts"]
    m = len(cells)
    if m < min_cells or np.ptp(y) == 0:
        return {"width": width, "cells": m, "I": np.nan, "z": np.nan, "p": np.nan, "hot": np.nan,
                "cold": np.nan, "ok": False}
    w = wts.hex_neighbours(cells, width)
    w, _ = wts.attach_islands(w, cells)
    r = ac.moran(y, wts.row_standardise(w), permutations, rng)
    gz, gp = ac.gi_star(y, wts.distance_band(cells, width * 2.01))
    bins = ac.confidence_bins(gz, gp, use_fdr)
    p = r["p_sim"] if r["p_sim"] is not None else r["p_norm"]
    return {"width": width, "cells": m, "I": r["I"], "z": r["z"], "p": p, "hot": float((bins >= 2).mean()),
            "cold": float((bins <= -2).mean()), "ok": True, "grid": grid, "bins": bins}


def verdict(rows, alpha=0.05):
    """'robust' (clustered at every size), 'none' (at no size), 'depends' (only at some sizes);
    with the sizes where it was found."""
    ok = [r for r in rows if r["ok"]]
    found = [r for r in ok if r["I"] > 0 and r["p"] <= alpha]
    if not ok:
        return "none", []
    if len(found) == len(ok):
        return "robust", found
    if not found:
        return "none", []
    return "depends", found
