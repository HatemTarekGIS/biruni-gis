"""Repair the %1, %2 … placeholders of QGIS translations (.qm) in place.

Some translations came out as "1% [اختياري]", "% 1" or "1%" instead of "%1" (most
in Arabic and Tamil, a few dozen in most other languages): Qt then shows the
text without its value, so every optional Processing parameter in Arabic had no
name. Each translation whose placeholders differ from the source text is
repaired; one that cannot be repaired is replaced by the English source. Safe
to run more than once.

usage: python fix_qm.py <qgis_xx.qm> [...]
"""
import re
import struct
import sys

PLACEHOLDER = re.compile(r"%\d")
# "1%", "% 1", "٪ 1", "1٪", "%١", "%۱" (Arabic-Indic and Persian digits)
MANGLED = re.compile(r"[%٪] ?([0-9٠-٩۰-۹])(?![0-9٠-٩۰-۹])|(?<![0-9٠-٩۰-۹%])([0-9٠-٩۰-۹]) ?[%٪]")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
MAGIC = bytes([0x3C, 0xB8, 0x64, 0x18, 0xCA, 0xEF, 0x9C, 0x95, 0xCD, 0x21, 0x1C, 0xBF, 0x60, 0xA1, 0xBD, 0xDD])
HASHES, MESSAGES = 0x42, 0x69
END, TRANSLATION, OBSOLETE1, SOURCE, CONTEXT, COMMENT = 1, 3, 5, 6, 7, 8
LENGTH_TAGS = {2, 3, 4, 6, 7, 8, 9}


def repair(source, text):
    """The repaired translation; None when it is fine as it is.

    Only messages whose source has %1-style placeholders are checked: a source like "Zoom to 100%" or one with
    the plural %n is never formatted with arg(), so its translation ("%100 Yakınlaştır") is left alone."""
    want = sorted(set(PLACEHOLDER.findall(source)))
    if not want or sorted(set(PLACEHOLDER.findall(text))) == want:
        return None
    fixed = MANGLED.sub(lambda m: "%" + (m.group(1) or m.group(2)).translate(DIGITS), text)
    if sorted(set(PLACEHOLDER.findall(fixed))) == want:
        return fixed
    # A broken placeholder left in the text ("% no remapeable", "%\n1") would show as garbage: use English.
    # A translation that simply leaves the value out still reads fine in its own language: keep it.
    return source if "%" in text or "٪" in text else None


def read_blocks(data):
    if data[:16] != MAGIC:
        raise ValueError("not a .qm file")
    blocks, pos = [], 16
    while pos < len(data):
        tag = data[pos]
        n = struct.unpack(">I", data[pos + 1:pos + 5])[0]
        blocks.append([tag, data[pos + 5:pos + 5 + n]])
        pos += 5 + n
    return blocks


def fix_messages(msgs):
    """Rebuilt messages block, map of old record offset -> new offset, repaired count."""
    out, moved, fixed, i = bytearray(), {}, 0, 0
    while i < len(msgs):
        moved[i] = len(out)
        fields, translations = {}, []
        # read one record up to its end tag
        while True:
            tag = msgs[i]
            i += 1
            if tag == END:
                break
            if tag == OBSOLETE1:
                fields.setdefault("raw", []).append((tag, msgs[i:i + 4]))
                i += 4
                continue
            if tag not in LENGTH_TAGS:
                raise ValueError(f"unknown tag {tag} at {i - 1}")
            n = struct.unpack(">I", msgs[i:i + 4])[0]
            i += 4
            if tag == TRANSLATION and n == 0xFFFFFFFF:
                value = None
            else:
                value = msgs[i:i + n]
                i += n
            fields.setdefault("raw", []).append((tag, value))
            if tag == TRANSLATION:
                translations.append(value)
            elif tag == SOURCE:
                fields["source"] = value.decode("utf-8", "replace")
        source = fields.get("source", "")
        for tag, value in fields.get("raw", []):
            if tag == TRANSLATION and value is not None and "%" in source:
                new = repair(source, value.decode("utf-16-be", "replace"))
                if new is not None:
                    value = new.encode("utf-16-be")
                    fixed += 1
            out.append(tag)
            if tag == OBSOLETE1:
                out += value
            elif value is None:
                out += struct.pack(">I", 0xFFFFFFFF)
            else:
                out += struct.pack(">I", len(value)) + value
        out.append(END)
    return bytes(out), moved, fixed


def fix_file(path):
    data = open(path, "rb").read()
    blocks = read_blocks(data)
    tags = {b[0]: b for b in blocks}
    msgs, moved, fixed = fix_messages(tags[MESSAGES][1])
    if not fixed:
        return 0
    tags[MESSAGES][1] = msgs
    hashes = tags[HASHES][1]
    new_hashes = bytearray()
    for k in range(0, len(hashes), 8):
        h, off = struct.unpack(">II", hashes[k:k + 8])
        new_hashes += struct.pack(">II", h, moved[off])
    tags[HASHES][1] = bytes(new_hashes)
    with open(path, "wb") as f:
        f.write(MAGIC)
        for tag, payload in blocks:
            f.write(bytes([tag]) + struct.pack(">I", len(payload)) + payload)
    return fixed


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(f"{p}: {fix_file(p)} translations repaired")
