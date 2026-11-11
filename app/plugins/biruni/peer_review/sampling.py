"""The statistics of the peer review lottery, exact and in plain Python (no tables, no scipy).

A delivery is checked by lots: the features one person made form one lot. From each lot a random sample is
drawn and reviewed; what the review finds says, with a stated confidence, how many errors the whole lot holds.

* hypergeometric: drawing n of N features without putting any back, D of the N being wrong, the chance of
  finding exactly x wrong ones. This is the exact law for a finite lot (the binomial and Poisson tables of the
  standards are approximations of it).
* plan(N, aql, ltpd, alpha, beta): the single sampling plan (n, c) - review n features, accept the lot when at
  most c are wrong - with the smallest n for which a lot at the acceptable error rate (AQL) is accepted with a
  chance of at least 1-alpha (the producer's risk) and a lot at the limit (LTPD) is accepted with a chance of at
  most beta (the consumer's risk). The logic of ISO 2859 / ANSI Z1.4 plans, computed exactly for this N.
* bounds(N, n, x): the exact (Clopper-Pearson style, finite population) confidence bounds on the number of wrong
  features D in the lot after finding x wrong in n.
* stratified(lots): the whole delivery from its lots: the estimate is the sum of the lots' estimates; its bounds
  join the lots' bounds at a Bonferroni level, which is conservative (the true confidence is at least the stated).
"""
import hashlib
import math



def _lc(n, k):
    """log of n choose k."""
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def pmf(x, N, D, n):
    """P(X = x): x wrong in a sample of n from a lot of N holding D wrong."""
    if x < max(0, n - (N - D)) or x > min(n, D):
        return 0.0
    return math.exp(_lc(D, x) + _lc(N - D, n - x) - _lc(N, n))


def cdf(x, N, D, n):
    """P(X <= x)."""
    lo = max(0, n - (N - D))
    if x < lo:
        return 0.0
    hi = min(n, D)
    if x >= hi:
        return 1.0
    return min(1.0, sum(pmf(k, N, D, n) for k in range(lo, x + 1)))


def sf(x, N, D, n):
    """P(X >= x)."""
    if x <= 0:
        return 1.0
    return max(0.0, 1.0 - cdf(x - 1, N, D, n))


class PlanError(ValueError):
    pass


def defects_at(N, rate, up=False):
    """How many wrong features a lot of N holds at an error rate (at least one at the limit)."""
    v = rate * N
    return min(N, max(1, math.ceil(v - 1e-9))) if up else min(N, int(math.floor(v + 1e-9)))


def plan(N, aql=0.01, ltpd=0.05, alpha=0.05, beta=0.10):
    """The smallest single sampling plan for a lot of N: {"n", "c", "pa_aql", "pa_ltpd", "d1", "d2", "all"}.
    all: the lot is so small that the whole of it is to be reviewed."""
    N = int(N)
    if N <= 0:
        raise PlanError("empty lot")
    if not (0 <= aql < ltpd <= 1):
        raise PlanError("the acceptable rate must be below the limit")
    d1, d2 = defects_at(N, aql), defects_at(N, ltpd, up=True)
    if d2 <= d1:
        d2 = d1 + 1
    if d2 > N:
        return {"n": N, "c": d1, "pa_aql": 1.0, "pa_ltpd": 0.0, "d1": d1, "d2": d2, "all": True}
    for n in range(1, N + 1):
        # the smallest c that accepts a good lot often enough
        c = 0
        while cdf(c, N, d1, n) < 1 - alpha:
            c += 1
        pb = cdf(c, N, d2, n)
        if pb <= beta:
            return {"n": n, "c": c, "pa_aql": cdf(c, N, d1, n), "pa_ltpd": pb, "d1": d1, "d2": d2, "all": n >= N}
    return {"n": N, "c": d1, "pa_aql": 1.0, "pa_ltpd": 0.0, "d1": d1, "d2": d2, "all": True}


def upper(N, n, x, alpha=0.05):
    """The largest D for which finding x or fewer wrong in n is still likely (one-sided, 1-alpha)."""
    if n >= N:
        return x
    lo, hi = x, N - (n - x)   # D cannot be below x nor leave fewer than n-x right ones
    # cdf(x; D) falls as D grows: find the last D with cdf >= alpha
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cdf(x, N, mid, n) >= alpha:
            lo = mid
        else:
            hi = mid - 1
    return lo


def lower(N, n, x, alpha=0.05):
    """The smallest D for which finding x or more wrong in n is still likely (one-sided, 1-alpha)."""
    if n >= N:
        return x
    if x <= 0:
        return 0
    lo, hi = x, N - (n - x)
    while lo < hi:   # sf(x; D) grows with D: the first D with sf >= alpha
        mid = (lo + hi) // 2
        if sf(x, N, mid, n) >= alpha:
            hi = mid
        else:
            lo = mid + 1
    return lo


def bounds(N, n, x, conf=0.95):
    """{"est", "rate", "upper", "lo", "hi"} for the number of wrong features in the lot: the estimate N·x/n, the
    one-sided upper bound at conf and the two-sided interval at conf (rates as fractions of N)."""
    a = 1 - conf
    est = N * x / n if n else 0.0
    up = upper(N, n, x, a)
    lo, hi = lower(N, n, x, a / 2), upper(N, n, x, a / 2)
    return {"N": N, "n": n, "x": x, "est": est, "rate": est / N if N else 0.0, "upper": up,
            "upper_rate": up / N if N else 0.0, "lo": lo, "hi": hi,
            "lo_rate": lo / N if N else 0.0, "hi_rate": hi / N if N else 0.0, "conf": conf}


def stratified(lots, conf=0.95):
    """The whole delivery from lots [(N, n, x)]: the estimate is exact (the sum), the bounds are conservative."""
    lots = [(N, n, x) for N, n, x in lots if N]
    if not lots:
        return None
    k = len(lots)
    a = (1 - conf) / k
    N = sum(t[0] for t in lots)
    est = sum(t[0] * t[2] / t[1] for t in lots if t[1])
    up = sum(upper(t[0], t[1], t[2], a) for t in lots)
    lo = sum(lower(t[0], t[1], t[2], a / 2) for t in lots)
    hi = sum(upper(t[0], t[1], t[2], a / 2) for t in lots)
    return {"N": N, "n": sum(t[1] for t in lots), "x": sum(t[2] for t in lots), "est": est, "rate": est / N,
            "upper": up, "upper_rate": up / N, "lo": lo, "hi": hi, "lo_rate": lo / N, "hi_rate": hi / N,
            "conf": conf, "lots": k}


# ---- the lottery -----------------------------------------------------------------------------------------------
def seed_of(commitment, salt):
    """The draw's seed: the commitment (the data's fingerprint and the plan, published first) and the salt (given
    after it by others: words said in the meeting, a number from outside)."""
    return hashlib.sha256(b"biruni-review-seed-1\n" + commitment.encode() + b"\n" + salt.strip().encode()).hexdigest()


def rank(seed, key):
    """The place of one item in the draw: a hash, so anyone with the seed puts the items in the same order."""
    return hashlib.sha256(f"{seed}\n{key}".encode()).hexdigest()


def draw(seed, items, n):
    """n of the items (strings) chosen by the seed: the n with the smallest ranks, in that order."""
    return sorted(items, key=lambda k: rank(seed, k))[:n]


def assign(seed, sample, reviewers):
    """Each (key, author) of the sample to a reviewer who is not its author, the loads kept level. -> {key:
    reviewer}. The order and the ties come from the seed (anyone redraws the same assignment). An item whose
    author is the only reviewer cannot be given: it is left out (missing in the answer)."""
    load = {r: 0 for r in reviewers}
    out = {}
    for key, author in sorted(sample, key=lambda t: rank(seed, "a\n" + t[0])):
        free = [r for r in reviewers if r != author]
        if not free:
            continue
        best = min(free, key=lambda r: (load[r], rank(seed, f"r\n{key}\n{r}")))
        out[key] = best
        load[best] += 1
    return out
