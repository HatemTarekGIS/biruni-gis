"""Links inside the book's PDF: bookmarks (the outline) and clickable areas that jump to another page.

The layout exporter writes the PDF; this adds an incremental update at its end (the pages themselves are not
touched), the same way the quick map sheet adds its web links. Also reads the structure back (pages, outline,
links) for checks.
"""
import re

from ..cartography import _pdf_objects

MM = 72.0 / 25.4


def _pdf_text(text):
    """A PDF text string: plain ASCII as is, anything else as UTF-16 with its byte order mark."""
    text = str(text)
    if all(32 <= ord(ch) < 127 for ch in text):
        return b"(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii") + b")"
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _decode_text(raw):
    raw = raw.strip()
    if raw.startswith(b"<"):
        data = bytes.fromhex(raw[1:-1].decode("ascii"))
        return data[2:].decode("utf-16-be") if data[:2] == b"\xfe\xff" else data.decode("latin-1")
    body = raw[1:-1]
    body = re.sub(rb"\\([()\\])", rb"\1", body)
    if body[:2] == b"\xfe\xff":
        return body[2:].decode("utf-16-be")
    return body.decode("latin-1")


def _page_numbers(objs):
    """Page objects in document order (the page tree's kids, walked)."""
    root_pages = None
    for num, (_s, _e, body) in objs.items():
        if re.search(rb"/Type\s*/Pages\b", body) and not re.search(rb"/Parent\s", body):
            root_pages = num
            break
    if root_pages is None:
        return sorted(n for n, (_s, _e, b) in objs.items() if re.search(rb"/Type\s*/Page\b(?!s)", b))
    out = []

    def walk(num, depth=0):
        body = objs[num][2]
        if re.search(rb"/Type\s*/Page\b(?!s)", body):
            out.append(num)
            return
        kids = re.search(rb"/Kids\s*\[([^\]]*)\]", body)
        if kids and depth < 32:
            for n in re.findall(rb"(\d+)\s+0\s+R", kids.group(1)):
                if int(n) in objs:
                    walk(int(n), depth + 1)

    walk(root_pages)
    return out


def _trailer_ref(data, key):
    found = re.findall(rb"/" + key + rb"\s+(\d+)\s+0\s+R", data)
    return int(found[-1]) if found else None


def _set_annots(objs, pnum, refs, new):
    """Append annotation refs to a page (its own array, an array object, or a new array)."""
    body = objs[pnum][2].strip()
    m = re.search(rb"/Annots\s*\[([^\]]*)\]", body)
    m2 = re.search(rb"/Annots\s+(\d+)\s+0\s+R", body)
    if m:
        new.append((pnum, body[:m.start()] + b"/Annots [" + m.group(1) + b" " + refs + b"]" + body[m.end():]))
    elif m2 and int(m2.group(1)) in objs:
        arr = int(m2.group(1))
        new.append((arr, objs[arr][2].strip().rstrip(b"]") + b" " + refs + b"]"))
    else:
        end = body.rfind(b">>")
        new.append((pnum, body[:end] + b"/Annots [" + refs + b"] " + body[end:]))


def add_navigation(path, links=(), outline=(), page_sizes_mm=()):
    """Add internal links and bookmarks to the PDF at ``path``.
    links: [(page index, (x, y, w, h) mm from the page's top-left, target page index)].
    outline: [(title, target page index, [children in the same form])].
    Returns (links written, bookmarks written)."""
    data = open(path, "rb").read()
    objs = _pdf_objects(data)
    pages = _page_numbers(objs)
    if not pages:
        return 0, 0
    size_m = re.findall(rb"/Size\s+(\d+)", data)
    nxt = [max(int(size_m[-1]) if size_m else 0, max(objs) + 1)]
    root = _trailer_ref(data, b"Root")
    info = _trailer_ref(data, b"Info")
    prev = re.findall(rb"startxref\s+(\d+)", data)
    prev = int(prev[-1]) if prev else 0
    new = []

    def alloc():
        n = nxt[0]
        nxt[0] += 1
        return n

    def dest(k):
        return b"[%d 0 R /Fit]" % pages[max(0, min(k, len(pages) - 1))]

    n_links = 0
    by_page = {}
    for page, (x, y, w, h), target in links:
        if page >= len(pages) or target is None or target >= len(pages):
            continue
        pw, ph = page_sizes_mm[page] if page < len(page_sizes_mm) else page_sizes_mm[-1]
        x1, y1, x2, y2 = x * MM, (ph - y - h) * MM, (x + w) * MM, (ph - y) * MM
        num = alloc()
        new.append((num, b"<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] /H /I "
                         b"/Dest " % (x1, y1, x2, y2) + dest(target) + b" >>"))
        by_page.setdefault(pages[page], []).append(num)
        n_links += 1
    for pnum, nums in by_page.items():
        _set_annots(objs, pnum, b" ".join(b"%d 0 R" % n for n in nums), new)

    n_marks = 0
    if outline and root is not None and root in objs:
        top = alloc()

        def items(entries, parent):
            nonlocal n_marks
            nums = [alloc() for _e in entries]
            total = 0
            for i, (title, target, children) in enumerate(entries):
                n_marks += 1
                body = b"<< /Title " + _pdf_text(title) + b" /Parent %d 0 R" % parent
                if i > 0:
                    body += b" /Prev %d 0 R" % nums[i - 1]
                if i + 1 < len(nums):
                    body += b" /Next %d 0 R" % nums[i + 1]
                if target is not None:
                    body += b" /Dest " + dest(target)
                if children:
                    first, last, count = items(children, nums[i])
                    body += b" /First %d 0 R /Last %d 0 R /Count %d" % (first, last, -count)
                    total += count
                new.append((nums[i], body + b" >>"))
                total += 1
            return nums[0], nums[-1], len(entries)

        first, last, count = items(list(outline), top)
        new.append((top, b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (first, last, count)))
        cat = objs[root][2].strip()
        cat = re.sub(rb"/Outlines\s+\d+\s+0\s+R", b"", cat)
        cat = re.sub(rb"/PageMode\s*/\w+", b"", cat)
        end = cat.rfind(b">>")
        new.append((root, cat[:end] + b"/Outlines %d 0 R /PageMode /UseOutlines " % top + cat[end:]))
    if not new:
        return 0, 0
    out = bytearray(data)
    if not out.endswith(b"\n"):
        out += b"\n"
    offsets = {}
    for num, body in new:
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n"
    for num in sorted(offsets):
        out += b"%d 1\n%010d 00000 n \n" % (num, offsets[num])
    trailer = b"<< /Size %d /Prev %d" % (max(nxt[0], max(offsets) + 1), prev)
    if root is not None:
        trailer += b" /Root %d 0 R" % root
    if info is not None:
        trailer += b" /Info %d 0 R" % info
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    with open(path, "wb") as f:
        f.write(bytes(out))
    return n_links, n_marks


def read_navigation(path):
    """What a PDF's reader sees: {"pages": n, "sizes": [(w, h) pt], "outline": [(depth, title, page index)],
    "links": [(page index, (x1, y1, x2, y2) pt, target page index or url)]}."""
    data = open(path, "rb").read()
    objs = _pdf_objects(data)
    pages = _page_numbers(objs)
    where = {n: k for k, n in enumerate(pages)}
    sizes = []
    for n in pages:
        m = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", objs[n][2])
        sizes.append((float(m.group(3)) - float(m.group(1)), float(m.group(4)) - float(m.group(2))) if m else None)

    def target_of(body):
        m = re.search(rb"/Dest\s*\[\s*(\d+)\s+0\s+R", body)
        if m:
            return where.get(int(m.group(1)))
        m = re.search(rb"/D\s*\[\s*(\d+)\s+0\s+R", body)
        if m:
            return where.get(int(m.group(1)))
        m = re.search(rb"/URI\s*\(((?:\\.|[^\\)])*)\)", body)
        return m.group(1).decode("latin-1") if m else None

    links = []
    for k, n in enumerate(pages):
        body = objs[n][2]
        m = re.search(rb"/Annots\s*\[([^\]]*)\]", body)
        refs = m.group(1) if m else b""
        m2 = re.search(rb"/Annots\s+(\d+)\s+0\s+R", body)
        if not m and m2 and int(m2.group(1)) in objs:
            refs = objs[int(m2.group(1))][2]
        for r in re.findall(rb"(\d+)\s+0\s+R", refs):
            a = objs.get(int(r))
            if a is None or not re.search(rb"/Subtype\s*/Link", a[2]):
                continue
            rect = re.search(rb"/Rect\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)", a[2])
            box = tuple(float(v) for v in rect.groups()) if rect else None
            links.append((k, box, target_of(a[2])))
    outline = []
    root = _trailer_ref(data, b"Root")
    if root in objs:
        m = re.search(rb"/Outlines\s+(\d+)\s+0\s+R", objs[root][2])
        if m and int(m.group(1)) in objs:
            def walk(num, depth, guard=0):
                while num in objs and guard < 100000:
                    guard += 1
                    body = objs[num][2]
                    t = re.search(rb"/Title\s*(<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\)])*\))", body)
                    outline.append((depth, _decode_text(t.group(1)) if t else "", target_of(body)))
                    f = re.search(rb"/First\s+(\d+)\s+0\s+R", body)
                    if f:
                        walk(int(f.group(1)), depth + 1)
                    nx = re.search(rb"/Next\s+(\d+)\s+0\s+R", body)
                    if not nx:
                        break
                    num = int(nx.group(1))

            first = re.search(rb"/First\s+(\d+)\s+0\s+R", objs[int(m.group(1))][2])
            if first:
                walk(int(first.group(1)), 0)
    return {"pages": len(pages), "sizes": sizes, "outline": outline, "links": links}
