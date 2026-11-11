"""Ed25519 signatures (RFC 8032) in plain Python: the fallback when the bundled
'cryptography' library cannot be loaded. Slow (a few signatures per second) but
a seal needs only one."""
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _add(p, q):
    """Point sum in extended coordinates (RFC 8032, 5.1.4)."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % P, g * h % P, f * g % P, e * h % P


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if x & 1 != sign:
        x = P - x
    return x


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _encode(p):
    x, y, z, _t = p
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def _decode(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _hint(*parts):
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little")


def _secret(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return a, h[32:]


def public_key(seed):
    """The 32-byte public key of a 32-byte private seed."""
    return _encode(_mul(_secret(seed)[0], G))


def sign(seed, msg):
    a, prefix = _secret(seed)
    pub = _encode(_mul(a, G))
    r = _hint(prefix, msg) % L
    big_r = _encode(_mul(r, G))
    s = (r + _hint(big_r, pub, msg) * a) % L
    return big_r + s.to_bytes(32, "little")


def verify(pub, sig, msg):
    """True when sig is a valid signature of msg by pub ([s]B = R + [k]A)."""
    if len(sig) != 64:
        return False
    a = _decode(pub)
    s = int.from_bytes(sig[32:], "little")
    if a is None or s >= L or _decode(sig[:32]) is None:
        return False
    k = _hint(sig[:32], pub, msg) % L
    minus_a = (-a[0] % P, a[1], a[2], -a[3] % P)
    return _encode(_add(_mul(s, G), _mul(k, minus_a))) == sig[:32]
