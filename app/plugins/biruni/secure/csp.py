"""Content-Security-Policy builder and validator for the public Biruni site pages.

Every page on biruni-gis.com and photos.biruni-gis.com (except the photos site's p/index.html, which keeps
bridge.with_csp) gets a strict <meta http-equiv="Content-Security-Policy"> right after <meta charset="utf-8">.
Inline scripts and styles are allowed only by their exact SHA-256 hash; inline event handlers and stray external
scripts or stylesheets are refused.
"""
import base64
import hashlib
from html.parser import HTMLParser
import os
import re
import urllib.parse

_UNSAFE_EVENT_ATTR = re.compile(r"^on\w+$", re.IGNORECASE)
_CSP_FORBIDDEN_IN_HEADER = ('"',)


def _line_starts(html):
    starts = [0]
    pos = 0
    while True:
        idx = html.find("\n", pos)
        if idx < 0:
            break
        pos = idx + 1
        starts.append(pos)
    return starts


def _origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def _is_origin_entry(entry):
    p = urllib.parse.urlsplit(entry)
    return bool(p.scheme and p.netloc and p.path in ("", "/") and not p.query and not p.fragment)


def _url_allowed(url, allowed):
    if not url:
        return False
    if url in allowed:
        return True
    p = urllib.parse.urlsplit(url)
    if p.scheme and p.netloc:
        origin = f"{p.scheme.lower()}://{p.netloc.lower()}"
        return any(_is_origin_entry(entry) and _origin(entry) == origin for entry in allowed)
    if not p.scheme and not p.netloc and not url.startswith("//"):
        return "'self'" in allowed
    return False


def _sha256_source(text):
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


class _HtmlInspector(HTMLParser):
    def __init__(self, html, script_allowed, style_allowed, style_attr):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.starts = _line_starts(html)
        self.script_allowed = tuple(script_allowed)
        self.style_allowed = tuple(style_allowed)
        self.style_attr = bool(style_attr)
        self.charset_end = None
        self.csp_spans = []
        self.script_hashes = []
        self.style_hashes = []
        self._open_block = None  # (tag, body_start)

    def _offset(self):
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def _check_attrs(self, tag, attrs):
        for name, _val in attrs:
            if _UNSAFE_EVENT_ATTR.match(name):
                raise ValueError(f"inline event handler attribute {name!r} on <{tag}> is not allowed")
            if name.lower() == "style" and not self.style_attr:
                raise ValueError(f"inline style attribute on <{tag}> is not allowed")

        attr_map = {name.lower(): val for name, val in attrs}
        if tag == "meta":
            if (attr_map.get("http-equiv") or "").strip().lower() == "content-security-policy":
                start = self._offset()
                end = start + len(self.get_starttag_text() or "")
                self.csp_spans.append((start, end))
            if self.charset_end is None and (attr_map.get("charset") or "").strip().lower() == "utf-8":
                self.charset_end = self._offset() + len(self.get_starttag_text() or "")
        elif tag == "script":
            if "src" in attr_map:
                src = attr_map.get("src") or ""
                if not _url_allowed(src, self.script_allowed):
                    raise ValueError(f"external script {src!r} is not allowed by script list")
        elif tag == "link":
            rels = (attr_map.get("rel") or "").lower().split()
            if "stylesheet" in rels:
                href = attr_map.get("href") or ""
                if not _url_allowed(href, self.style_allowed):
                    raise ValueError(f"external stylesheet {href!r} is not allowed by style list")
        return attr_map

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        attr_map = self._check_attrs(t, attrs)
        if t == "script" and "src" not in attr_map:
            body_start = self._offset() + len(self.get_starttag_text() or "")
            self._open_block = ("script", body_start)
        elif t == "style":
            body_start = self._offset() + len(self.get_starttag_text() or "")
            self._open_block = ("style", body_start)

    def handle_startendtag(self, tag, attrs):
        self._check_attrs(tag.lower(), attrs)

    def handle_endtag(self, tag):
        t = tag.lower()
        if self._open_block and self._open_block[0] == t:
            body = self.html[self._open_block[1]:self._offset()]
            h = _sha256_source(body)
            if t == "script":
                self.script_hashes.append(h)
            else:
                self.style_hashes.append(h)
            self._open_block = None


def _strip_existing_csp(html):
    finder = _CspMetaFinder(html)
    finder.feed(html)
    finder.close()
    if not finder.spans:
        return html
    out = []
    pos = 0
    for start, end in finder.spans:
        out.append(html[pos:start])
        pos = end
    out.append(html[pos:])
    return "".join(out)


class _CspMetaFinder(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.starts = _line_starts(html)
        self.spans = []

    def _check(self, tag, attrs):
        if tag.lower() != "meta":
            return
        for name, val in attrs:
            if name.lower() == "http-equiv" and (val or "").strip().lower() == "content-security-policy":
                line, col = self.getpos()
                start = self.starts[line - 1] + col
                end = start + len(self.get_starttag_text() or "")
                self.spans.append((start, end))
                return

    def handle_starttag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._check(tag, attrs)


def add_csp(
    html,
    script=(),
    style=(),
    img=(),
    media=(),
    connect=(),
    font=(),
    worker=(),
    manifest=(),
    frame=(),
    style_attr=False,
):
    """Validate `html` and insert a strict Content-Security-Policy <meta> after <meta charset="utf-8">."""
    if "\r" in html:
        raise ValueError("html must not contain CR (\\r)")

    cleaned = _strip_existing_csp(html)
    inspector = _HtmlInspector(cleaned, script, style, style_attr)
    inspector.feed(cleaned)
    inspector.close()

    if inspector.charset_end is None:
        raise ValueError('missing <meta charset="utf-8">')

    script_sources = list(dict.fromkeys([*inspector.script_hashes, *script]))
    style_sources = list(dict.fromkeys([*inspector.style_hashes, *style]))

    directives = ["default-src 'none'"]
    if script_sources:
        directives.append("script-src " + " ".join(script_sources))
    if style_sources:
        directives.append("style-src " + " ".join(style_sources))
    for name, values in (
        ("img-src", img),
        ("media-src", media),
        ("connect-src", connect),
        ("font-src", font),
        ("worker-src", worker),
        ("manifest-src", manifest),
    ):
        if values:
            directives.append(f"{name} {' '.join(values)}")
    directives.append("frame-src " + (" ".join(frame) if frame else "'none'"))
    if style_attr:
        directives.append("style-src-attr 'unsafe-inline'")
    directives.extend(["base-uri 'none'", "form-action 'none'"])

    policy = "; ".join(directives)
    for bad in _CSP_FORBIDDEN_IN_HEADER:
        if bad in policy:
            raise ValueError(f"invalid character {bad!r} in CSP policy")

    meta = f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
    idx = inspector.charset_end
    return cleaned[:idx] + meta + cleaned[idx:]


def site_policies():
    """Return a dict mapping each built site HTML path to its add_csp() keyword arguments."""
    from ..field_photos import bridge

    nt = _origin(bridge.NTFY)
    br = tuple(dict.fromkeys([*(_origin(u) for u in bridge.RELAYS), nt, re.sub(r"^http", "ws", nt)]))
    site_docs = {
        "style": ("https://fonts.googleapis.com",),
        "font": ("https://fonts.gstatic.com",),
        "img": ("'self'",),
    }
    return {
        "index.html": dict(site_docs),
        "privacy.html": dict(site_docs),
        "404.html": {},
        "p/index.html": {},
        "r/index.html": {},
        "v/index.html": {},
        "m/index.html": {
            "script": ("https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",),
            "style": ("https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",),
            "img": ("https://tile.openstreetmap.org", "data:"),
        },
        "s/index.html": {
            "img": ("blob:",),
            "connect": br,
        },
        "d/index.html": {
            "img": ("data:", "blob:"),
            "connect": br,
            "worker": ("blob:",),
            "frame": ("'self'",),
        },
        "paper/index.html": {
            "img": ("data:", "blob:"),
            "worker": ("blob:",),
            "style_attr": True,
        },
        "portal/index.html": {
            "img": ("https:",),
            "connect": ("'self'", "https:"),
        },
    }


def photos_policies():
    """Return a dict mapping each photos-site HTML path (except p/index.html) to its add_csp() keyword arguments."""
    return {
        "index.html": {},
        "r/index.html": {},
        "404.html": {},
    }


def apply_site(folder, policies=None, keep=()):
    """Apply `policies` (default site_policies()) to every *.html under `folder` (except .git and `keep`).

    Raises ValueError if an HTML file has no entry in `policies`. Returns the list of rewritten file paths.
    """
    policies = site_policies() if policies is None else policies
    keep_set = set(keep)
    rewritten = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(names):
            if not name.lower().endswith(".html"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if rel in keep_set:
                continue
            if rel not in policies:
                raise ValueError(f"no CSP policy defined for {rel}")
            with open(path, "r", encoding="utf-8", newline="") as f:
                html = f.read()
            updated = add_csp(html, **policies[rel])
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(updated)
            rewritten.append(path)
    return rewritten
