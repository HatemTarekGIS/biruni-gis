"""The sound modem of "share by sound" (ext_sound.py): a short burst of tones that carries up to 43 bytes from one
loudspeaker to the microphones in the room, written for this purpose (the page has the same modem in JS).

    sync      1406.25 Hz for 192 ms
    20 frames of 64 ms; in each, six tones sound together - one per band of 16 tones, 46.875 Hz apart, from
              1875 Hz up (1875 … 6328 Hz) - so a frame carries 6 nibbles = 3 bytes
    the 60 bytes: [length] + payload (padded to 44) + 16 Reed-Solomon bytes (GF(256), 0x11d): up to 8 wrong
              bytes are corrected (noise, an echo, a cough)

The tones are 46.875 Hz apart = two whole cycles in a 2048-sample window at 48 kHz, so a listener's window
sees each tone alone; any sample rate works (the listener measures each tone by Goertzel at its exact
frequency). About 1.6 s in all.
"""
import io
import math
import struct
import wave

SR = 48000
F0 = 1875.0
DF = 46.875
SYNC_F = 1406.25
BANDS = 6
TONES = 16
FRAME = 0.064
SYNC_T = 0.192
DATA = 44
NSYM = 16
TOTAL = DATA + NSYM        # 60 bytes = 20 frames
MAX_PAYLOAD = DATA - 1

# ---- GF(256) and Reed-Solomon (systematic encoder; the decoder is in the page) ---------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for j, qj in enumerate(q):
        for i, pi in enumerate(p):
            r[i + j] ^= _mul(pi, qj)
    return r


def _generator(nsym):
    g = [1]
    for i in range(nsym):
        g = _poly_mul(g, [1, _EXP[i]])
    return g


def rs_encode(msg, nsym=NSYM):
    gen = _generator(nsym)
    out = list(msg) + [0] * nsym
    for i in range(len(msg)):
        coef = out[i]
        if coef:
            for j in range(1, len(gen)):
                out[i + j] ^= _mul(gen[j], coef)
    return bytes(list(msg) + out[len(msg):])


def block(payload):
    """The 60 bytes sent for a payload (at most 43 bytes)."""
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    data = bytes([len(payload)]) + payload + bytes(DATA - 1 - len(payload))
    return rs_encode(data)


def nibbles(code):
    out = []
    for b in code:
        out += [b >> 4, b & 15]
    return out


def tone(band, n):
    return F0 + (band * TONES + n) * DF


def synth(code, sr=SR, lead=0.08, tail=0.12):
    """Float samples (-1..1) of one transmission of the 60 bytes ``code``."""
    ramp = int(0.004 * sr)
    out = [0.0] * int(lead * sr)

    def burst(freqs, dur, amp):
        n = int(round(dur * sr))
        seg = [0.0] * n
        for f in freqs:
            w = 2 * math.pi * f / sr
            for i in range(n):
                seg[i] += amp * math.sin(w * i)
        for i in range(min(ramp, n)):
            k = 0.5 - 0.5 * math.cos(math.pi * i / ramp)
            seg[i] *= k
            seg[n - 1 - i] *= k
        return seg

    out += burst([SYNC_F], SYNC_T, 0.8)
    nib = nibbles(code)
    for k in range(len(code) // 3):
        out += burst([tone(b, nib[k * 6 + b]) for b in range(BANDS)], FRAME, 0.14)
    out += [0.0] * int(tail * sr)
    return out


def wav_bytes(samples, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples))
    return buf.getvalue()


# ---- what is carried ------------------------------------------------------------------------------------------------
def bridge_payload(key_b64u, token):
    """A link from anywhere: the bridge key (32 bytes) and the join token (8 bytes)."""
    import base64

    key = base64.urlsafe_b64decode(key_b64u + "=" * (-len(key_b64u) % 4))
    if len(key) != 32 or len(token) != 8:
        raise ValueError("bad key or token")
    return bytes([1, 1]) + key + bytes(token)


def lan_payload(ip, port, path_token, token):
    """A link on this network: the PC's address, the dashboard server's path token and the join token."""
    parts = [int(x) for x in ip.split(".")]
    pt = path_token.encode("ascii")
    if len(parts) != 4 or len(pt) > 26 or len(token) != 8:
        raise ValueError("bad address")
    return bytes([1, 2]) + bytes(parts) + struct.pack(">H", port) + bytes([len(pt)]) + pt + bytes(token)
