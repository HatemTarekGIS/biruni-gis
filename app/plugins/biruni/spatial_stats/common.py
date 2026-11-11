"""Shared pieces of the spatial statistics tools: the language of the explanations, the false
discovery rate, permutation p-values, chart text and the chart canvas."""
import io

import numpy as np

from .. import i18n

GROUP, GROUP_ID = "Spatial statistics", "spatialstats"


def text_lang():
    """The explanations are written in Arabic or English: Arabic for an Arabic interface, else English."""
    return "ar" if i18n.lang() == "ar" else "en"


def say(texts, lang=None, **values):
    """texts: {"ar": "...{n}...", "en": "...{n}..."} -> the sentence in the explanation language."""
    t = texts.get(lang or text_lang()) or texts["en"]
    return t.format(**values)


def fdr(p, alpha=0.05):
    """Benjamini-Hochberg: True where a p-value stays significant once many places are tested at once."""
    p = np.asarray(p, dtype=float)
    n = p.size
    if n == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    passed = ranked <= alpha
    keep = np.zeros(n, dtype=bool)
    if passed.any():
        last = np.max(np.nonzero(passed)[0])
        keep[order[:last + 1]] = True
    return keep


def perm_p(observed, simulated, two_sided=True):
    """Pseudo p-value of a permutation test: (extreme + 1) / (permutations + 1)."""
    sim = np.asarray(simulated, dtype=float)
    if two_sided:
        centre = sim.mean()
        extreme = np.sum(np.abs(sim - centre) >= abs(observed - centre))
    else:
        extreme = np.sum(sim >= observed)
    return (extreme + 1.0) / (sim.size + 1.0)


def rng(seed=None):
    """Random numbers for permutations: a fixed seed gives the same answer every run (reproducible results)."""
    return np.random.default_rng(12345 if seed is None else seed)


def chart_text(text):
    """Text for a matplotlib chart. matplotlib 3.11+ lays out Arabic itself (joined letters, right to
    left, mirrored brackets): the text goes in as written. Older versions neither join nor order it,
    so there Arabic is shaped and reversed here (Latin words and numbers inside keep their order)."""
    from ..arabic_text import script, shape

    if not text or script(text) != "arabic" or _mpl_lays_out_arabic():
        return text
    out, run = [], []
    for ch in shape(text):
        if ch.isascii() and (ch.isalnum() or ch in ".,%-+:/()"):
            run.append(ch)
        else:
            if run:
                out.append("".join(run)[::-1])
                run = []
            out.append(ch)
    if run:
        out.append("".join(run)[::-1])
    return "".join(out)[::-1]


def _mpl_lays_out_arabic():
    import matplotlib

    try:
        return tuple(int(x) for x in matplotlib.__version__.split(".")[:2]) >= (3, 11)
    except ValueError:
        return False


def figure(width=6.4, height=4.2):
    """A matplotlib figure that draws without a screen."""
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(width, height), dpi=150)
    return fig


def png(fig):
    """The figure as PNG bytes (and the figure closed)."""
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
