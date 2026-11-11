"""Shamir secret sharing over GF(256), byte by byte: a secret becomes n shares, any k give it back, k-1 say nothing
(for every possible secret there is exactly one polynomial through those k-1 points)."""
import os

import numpy as np

from .rs import MUL, ginv, gmul


class ShamirError(ValueError):
    pass


def _eval(coefs, x):
    """coefs[0] + coefs[1]·x + … (each coef a uint8 array) at the field point x (Horner)."""
    y = coefs[-1].copy()
    for c in reversed(coefs[:-1]):
        y = MUL[x][y] ^ c
    return y


def split(secret, k, n):
    """bytes -> [(x, share bytes)] for x = 1..n."""
    if not (1 <= k <= n <= 255):
        raise ShamirError(f"need 1 <= k <= n <= 255 (k={k}, n={n})")
    s = np.frombuffer(bytes(secret), dtype=np.uint8)
    coefs = [s] + [np.frombuffer(os.urandom(len(s)), dtype=np.uint8) for _ in range(k - 1)]
    return [(x, _eval(coefs, x).tobytes()) for x in range(1, n + 1)]


def _lagrange_at(xs, at):
    out = []
    for i, xi in enumerate(xs):
        num, den = 1, 1
        for j, xj in enumerate(xs):
            if i != j:
                num = gmul(num, at ^ xj)
                den = gmul(den, xi ^ xj)
        out.append(gmul(num, ginv(den)))
    return out


def interpolate(shares, at):
    """The value at field point ``at`` of the polynomial through the shares [(x, bytes)] (all of them are used:
    give exactly k)."""
    xs = [int(x) for x, _ in shares]
    if len(set(xs)) != len(xs) or any(not 1 <= x <= 255 for x in xs):
        raise ShamirError("share points must be distinct and in 1..255")
    lens = {len(b) for _, b in shares}
    if len(lens) != 1:
        raise ShamirError("shares of different lengths")
    ls = _lagrange_at(xs, at)
    out = np.zeros(lens.pop(), dtype=np.uint8)
    for li, (_, b) in zip(ls, shares):
        out ^= MUL[li][np.frombuffer(bytes(b), dtype=np.uint8)]
    return out.tobytes()


def combine(shares):
    """[(x, bytes)] (exactly k of them) -> the secret."""
    return interpolate(shares, 0)


def extend(shares, x):
    """From k shares, the share at a new point x (the same one split() made there): repairs a lost share without
    ever learning the secret on the way out (the caller does see it could combine, as anyone holding k can)."""
    return interpolate(shares, int(x))
