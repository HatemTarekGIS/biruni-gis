"""A fountain code: any K-and-a-few of the endless stream of pieces give back the data, in any order.

The data is cut into K blocks of the same size (the last one padded with zeros). Piece number i is:

* i < K   - block i itself (a "systematic" piece: a receiver that sees the first pass whole has the data
            with nothing to solve);
* i >= K  - the XOR of 2 ln K to 4 ln K blocks chosen from i alone (a "repair" piece), by a small
            pseudo-random generator (splitmix64) that is the same on every computer and every Python.

The receiver reduces every piece as it comes against those kept before (Gaussian elimination over GF(2)),
so a piece that tells nothing new is recognised at once and the work is spread over the transfer. Rows of
about 2 ln K blocks touch every block, which a sparse Luby-transform stream does not: with them a few
pieces past K are enough (measured: 0-3 % for K >= 100), whichever pieces were lost and wherever the
receiver started. No answer goes back to the sender: it keeps sending new pieces until it is stopped.
"""
import math

import numpy as np

MASK64 = (1 << 64) - 1


class _Rand:
    """splitmix64: tiny, and the same everywhere (random.Random may change between Python versions)."""

    def __init__(self, seed):
        self.s = (int(seed) * 0x9E3779B97F4A7C15 + 0x632BE59BD9B4E019) & MASK64

    def next(self):
        self.s = (self.s + 0x9E3779B97F4A7C15) & MASK64
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def below(self, n):
        return self.next() % n


def blocks_of(index, k):
    """The block numbers XOR-ed into piece ``index`` of a ``k``-block stream."""
    if index < k or k == 1:
        return [index % k]
    rnd = _Rand(index * 2654435761 + k)
    if k <= 24:                          # few blocks: each in or out at random (rows of 2 ln K would repeat)
        while True:
            bits = rnd.next() & ((1 << k) - 1)
            if bits & (bits - 1):        # two blocks or more
                return [b for b in range(k) if bits >> b & 1]
    base = max(3, int(math.ceil(2.0 * math.log(k + 1))))
    d = min(k, base + rnd.below(base))
    if d * 3 > k:                        # many of few: a partial shuffle
        pool = list(range(k))
        for i in range(d):
            j = i + rnd.below(k - i)
            pool[i], pool[j] = pool[j], pool[i]
        return sorted(pool[:d])
    out = set()
    while len(out) < d:
        out.add(rnd.below(k))
    return sorted(out)


def split(data, block):
    """K = the number of blocks of ``data`` (at least one) and the blocks as a uint8 array K x block."""
    k = max(1, -(-len(data) // block))
    buf = np.zeros(k * block, np.uint8)
    buf[:len(data)] = np.frombuffer(bytes(data), np.uint8)
    return k, buf.reshape(k, block)


class Encoder:
    def __init__(self, data, block):
        self.length = len(data)
        self.block = int(block)
        self.k, self.blocks = split(data, self.block)

    def piece(self, index):
        idx = blocks_of(index, self.k)
        if len(idx) == 1:
            return self.blocks[idx[0]].tobytes()
        return np.bitwise_xor.reduce(self.blocks[idx], axis=0).tobytes()


class Decoder:
    """Collects pieces; ``done`` once K independent ones are kept; ``data()`` then gives the bytes."""

    def __init__(self, k, block, length):
        self.k, self.block, self.length = int(k), int(block), int(length)
        self.pivots = {}                 # lowest block of the row -> [row bits, uint8 array]
        self.seen = set()
        self._data = None

    @property
    def done(self):
        return self._data is not None or len(self.pivots) >= self.k

    def progress(self):
        """(independent pieces kept = blocks as good as known, pieces read, K)."""
        return (self.k if self._data is not None else len(self.pivots)), len(self.seen), self.k

    def add(self, index, payload):
        """Keep piece ``index``; True if it told something new."""
        if self.done or index in self.seen:
            return False
        self.seen.add(index)
        if len(payload) != self.block:
            return False
        m = 0
        for b in blocks_of(index, self.k):
            m |= 1 << b
        d = np.frombuffer(bytes(payload), np.uint8).copy()
        piv = self.pivots
        while m:
            low = (m & -m).bit_length() - 1
            p = piv.get(low)
            if p is None:
                piv[low] = [m, d]
                return True
            m ^= p[0]
            d ^= p[1]
        return False

    def _finish(self):
        piv = self.pivots
        nb = (self.k + 7) // 8
        solved = np.zeros((self.k, self.block), np.uint8)
        for c in range(self.k - 1, -1, -1):  # each row down to its own block, from the last block back
            m, d = piv[c]
            rest = m & ~(1 << c)
            if rest:
                idx = np.flatnonzero(np.unpackbits(np.frombuffer(rest.to_bytes(nb, "little"), np.uint8),
                                                   bitorder="little"))
                d = d ^ np.bitwise_xor.reduce(solved[idx], axis=0)
            solved[c] = d
        self._data = solved.tobytes()[:self.length]
        self.pivots = {}

    def data(self):
        if self._data is None:
            if len(self.pivots) < self.k:
                return None
            self._finish()
        return self._data
