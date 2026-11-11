"""Reed-Solomon erasure coding over GF(256), written here (no GPL library), vectorised with numpy.

A systematic code: the first k shards are the data itself cut in k equal rows, the other n-k are parity rows made
with a Cauchy matrix. Any k of the n shards give the data back, whichever they are (every k x k sub-matrix of
[I ; Cauchy] can be inverted). Arithmetic: the field GF(2^8) with the polynomial x^8+x^4+x^3+x^2+1 (0x11d).
"""
import numpy as np

POLY = 0x11D
MAX_N = 255


class RSError(ValueError):
    pass


def _tables():
    exp = np.zeros(512, dtype=np.uint8)
    log = np.zeros(256, dtype=np.int32)
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= POLY
    exp[255:510] = exp[:255]
    a = np.arange(256)
    la = log[a]
    mul = exp[(la[:, None] + la[None, :]) % 255].astype(np.uint8)
    mul[0, :] = 0
    mul[:, 0] = 0
    return exp, log, mul


EXP, LOG, MUL = _tables()


def gmul(a, b):
    return int(MUL[a, b])


def ginv(a):
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(256)")
    return int(EXP[255 - LOG[a]])


def gdiv(a, b):
    return gmul(a, ginv(b))


def _row(idx, k):
    """The coefficients that make shard ``idx`` out of the k data rows."""
    if idx < k:
        return [1 if j == idx else 0 for j in range(k)]
    x = idx  # parity rows: x_r = idx >= k, data rows: y_j = j < k, so x ^ y is never 0
    return [ginv(x ^ j) for j in range(k)]


def _check(k, n):
    if not (1 <= k <= n <= MAX_N):
        raise RSError(f"need 1 <= k <= n <= {MAX_N} (k={k}, n={n})")


def shard_size(size, k):
    return max(1, -(-int(size) // k))


def _combine(coefs, rows):
    """XOR-sum of coef * row over GF(256) (rows: equal-length uint8 arrays)."""
    out = np.zeros(len(rows[0]), dtype=np.uint8)
    for c, r in zip(coefs, rows):
        if c == 0:
            continue
        if c == 1:
            out ^= r
        else:
            out ^= MUL[c][r]
    return out


def encode(data, k, n):
    """bytes -> list of n shards (bytes, all the same length)."""
    _check(k, n)
    data = bytes(data)
    ss = shard_size(len(data), k)
    buf = np.zeros(k * ss, dtype=np.uint8)
    buf[:len(data)] = np.frombuffer(data, dtype=np.uint8)
    rows = buf.reshape(k, ss)
    out = [rows[i].tobytes() for i in range(k)]
    for idx in range(k, n):
        out.append(_combine(_row(idx, k), rows).tobytes())
    return out


def _invert(m):
    """The inverse of a k x k matrix over GF(256) (lists of ints)."""
    k = len(m)
    a = [list(r) + [1 if i == j else 0 for j in range(k)] for i, r in enumerate(m)]
    for col in range(k):
        piv = next((r for r in range(col, k) if a[r][col]), None)
        if piv is None:
            raise RSError("the shards do not make an invertible set")
        a[col], a[piv] = a[piv], a[col]
        iv = ginv(a[col][col])
        a[col] = [gmul(v, iv) for v in a[col]]
        for r in range(k):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [v ^ gmul(f, w) for v, w in zip(a[r], a[col])]
    return [r[k:] for r in a]


def data_rows(shards, k, n):
    """{index: shard bytes} with at least k entries -> the k data rows (uint8 arrays)."""
    _check(k, n)
    have = sorted(i for i in shards if 0 <= int(i) < n)[:k]
    if len(have) < k:
        raise RSError(f"{len(have)} shards, {k} needed")
    ss = len(shards[have[0]])
    if any(len(shards[i]) != ss for i in have):
        raise RSError("shards of different sizes")
    rows = [np.frombuffer(bytes(shards[i]), dtype=np.uint8) for i in have]
    if have == list(range(k)):
        return rows
    inv = _invert([_row(i, k) for i in have])
    return [_combine(inv[j], rows) for j in range(k)]


def decode(shards, k, n, size):
    """{index: shard} (any k or more) -> the original bytes (``size`` long)."""
    rows = data_rows(shards, k, n)
    return b"".join(r.tobytes() for r in rows)[:int(size)]


def rebuild(shards, k, n, want):
    """{index: shard} (any k) -> {index: shard} for each index in ``want`` (the exact bytes the encoder made)."""
    rows = data_rows(shards, k, n)
    return {i: (rows[i].tobytes() if i < k else _combine(_row(i, k), rows).tobytes()) for i in want}
