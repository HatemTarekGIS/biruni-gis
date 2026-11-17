r"""Build the public Biruni sites (main site, photos site, and Google Drive link helper).

- Main site (`out`, https://biruni-gis.com/): dashboard (`d/`), live share (`s/`), living map (`m/`),
  seal (`v/`), portal (`portal/`), docs, `404.html`, `_headers`, and forwarding pages at `p/` and `r/`
  with `p/sw.js` unregistering old service workers.
- Photos site (`<out>-photos`, https://photos.biruni-gis.com/): the phone bridge page (`p/index.html` at
  bridge.PAGE_URL, with its strict CSP), its offline worker (`p/sw.js`), room code page (`r/index.html`),
  `index.html`, `404.html`, and `_headers`.
- Google Drive link page (`<out>-drive-helper`, https://drive.biruni-gis.com/): `link/index.html` and
  `_headers`.

Both `out` and `<out>-photos` get CSP applied and are signed (`release.json` and `release.sig`) with the
update-signing key (_secrets), so each office PC refuses links to an unreleased page (secure/door.py).
Pages added to `out` later (the showcase `paper/` page) need: build_bridge_page.py --sign-only

usage: engine\bin\python-qgis.bat scripts\build_bridge_page.py [out folder]   (default build\bridge-page)
       engine\bin\python-qgis.bat scripts\build_bridge_page.py --sign-only [out folder]
"""
import os
import shutil
import sys

# a test's fake relays or fake Google must never reach a published page
_test = [v for v in ("BIRUNI_BRIDGE_RELAYS", "BIRUNI_BRIDGE_NTFY", "BIRUNI_DRIVE_API", "BIRUNI_DRIVE_HELPER",
                     "BIRUNI_DRIVE_PARENT", "BIRUNI_DRIVE_GSI") if os.environ.get(v)]
if _test:
    sys.exit("refusing to build with test settings: " + ", ".join(_test))

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))

from biruni.field_photos import bridge  # noqa: E402
from biruni.field_photos.room_page import room_page  # noqa: E402
from biruni.field_photos.server import all_phone_texts  # noqa: E402
from biruni.live_share.viewer import public_page as share_page  # noqa: E402
from biruni.living_paper import public_page as map_page  # noqa: E402
from biruni.seal.page import public_page as seal_page  # noqa: E402
from biruni.secure import csp, door  # noqa: E402

KEY = os.path.join(ROOT, "_secrets", "update-signing.key")
args = [a for a in sys.argv[1:] if a != "--sign-only"]
out = args[0] if args else os.path.join(ROOT, "build", "bridge-page")
photos = os.path.normpath(out) + "-photos"

PHOTOS_INDEX_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">'
    "<title>Biruni GIS</title></head>"
    "<body><p>Biruni GIS field photos. Scan the QR code from Biruni GIS to open this page.</p></body></html>"
)
NOT_FOUND_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">'
    "<title>Biruni GIS</title></head>"
    "<body><p>Page not found.</p></body></html>"
)
PHOTOS_HEADERS = (
    "/*\n"
    "  Content-Security-Policy: frame-ancestors 'none'\n"
    "  X-Content-Type-Options: nosniff\n"
    "  Referrer-Policy: no-referrer\n"
)
MAIN_HEADERS = (
    "/*\n"
    "  Content-Security-Policy: frame-ancestors 'self'\n"
    "  X-Content-Type-Options: nosniff\n"
)
HELPER_HEADERS = (
    "/*\n"
    "  Content-Security-Policy: frame-ancestors https://photos.biruni-gis.com\n"
    "  X-Content-Type-Options: nosniff\n"
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def sign(folder):
    if not os.path.isfile(KEY):
        print("NOT signed: no", KEY, "- every office PC will refuse links to these pages")
        return
    rel = door.sign_folder(folder, KEY)
    print(f"signed {folder}/release.json: {len(rel['files'])} files, version {rel['version']}")


if "--sign-only" in sys.argv[1:]:
    csp.apply_site(out)
    sign(out)
    if os.path.isdir(photos):
        csp.apply_site(photos, csp.photos_policies(), keep=("p/index.html",))
        sign(photos)
    sys.exit(0)

from biruni.field_photos import drive_page as _dp  # noqa: E402

_test = [v for v in _dp.TEST_VARS if os.environ.get(v)]
if _test:   # the test's fake Drive / sign-in would be built into the published pages
    sys.exit("NOT built: test settings are set (" + ", ".join(_test) + ")")
assert bridge.PAGE_URL == _dp.PARENT + "/p/"

texts = all_phone_texts()
page = bridge.phone_page(texts)
page = page.replace("<title>Biruni GIS</title>",
                    '<title>Biruni GIS</title><meta name="robots" content="noindex">'
                    '<meta name="referrer" content="no-referrer">')
# only its own script runs; it talks only to the relays and the Drive API, and frames only the Drive link page
page = bridge.with_csp(page, connect=(_dp.API,), frames=(_dp.HELPER,))

# ---- photos site (https://photos.biruni-gis.com/) ----------------------------------------------------------------
_write(os.path.join(photos, "p", "index.html"), page)
_write(os.path.join(photos, "p", "sw.js"), bridge.SW_JS)   # opens without internet
_write(os.path.join(photos, "r", "index.html"), room_page())
_write(os.path.join(photos, "index.html"), PHOTOS_INDEX_HTML)
_write(os.path.join(photos, "404.html"), NOT_FOUND_HTML)
_write(os.path.join(photos, "_headers"), PHOTOS_HEADERS)

# ---- main site (https://biruni-gis.com/) -------------------------------------------------------------------------
_write(os.path.join(out, "p", "index.html"), bridge.moved_page("https://photos.biruni-gis.com/p/"))
_write(os.path.join(out, "p", "sw.js"), bridge.SW_GONE_JS)
_write(os.path.join(out, "r", "index.html"), bridge.moved_page("https://photos.biruni-gis.com/r/"))
_write(os.path.join(out, "404.html"), NOT_FOUND_HTML)
_write(os.path.join(out, "_headers"), MAIN_HEADERS)
# the page a printed map's QR code opens (living_paper/) - no no-referrer: the map tiles need the origin
_write(os.path.join(out, "m", "index.html"), map_page())
# the page that shows a map shared live (live_share/)
_write(os.path.join(out, "s", "index.html"), share_page())
# the page that checks a sealed file (seal/)
_write(os.path.join(out, "v", "index.html"), seal_page())
_write(os.path.join(out, ".nojekyll"), "")
# the website's home page and its pictures (docs/site)
shutil.copytree(os.path.join(ROOT, "docs", "site"), out, dirs_exist_ok=True)
# the dashboard's public page (d/) and the portal viewer (portal/)
from biruni.dashboard.page import write_public_page  # noqa: E402

write_public_page(out)
try:
    from biruni.portal.page import build_bridge_viewer  # noqa: E402

    build_bridge_viewer(out)
except ImportError:
    pass
print(f"{photos}: p/index.html ({len(page) // 1024} KB, {len(texts)} languages)")

# ---- Google Drive link helper (https://drive.biruni-gis.com/) ----------------------------------------------------
helper = _dp.helper_page()
assert ("https://www.googleapis.com" in helper and "https://accounts.google.com/gsi/client" in helper
        and "127.0.0.1" not in helper and "localhost" not in helper), "the link page must point at Google"
assert "accounts.google.com" not in page, "the photo page must not load Google's script"
helper_root = os.path.normpath(out) + "-drive-helper"
_write(os.path.join(helper_root, "link", "index.html"), helper)
_write(os.path.join(helper_root, "_headers"), HELPER_HEADERS)
print(f"{os.path.join(helper_root, 'link', 'index.html')} "
      "(the Google Drive link page: publish it to the biruni-drive repository)")

csp.apply_site(out)
sign(out)
csp.apply_site(photos, csp.photos_policies(), keep=("p/index.html",))
sign(photos)
