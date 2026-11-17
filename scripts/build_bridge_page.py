r"""Build the phone page of the Biruni bridge for GitHub Pages (field_photos/bridge.py).

One static page with every language in it; the key never reaches it (it is after the
"#" of the address). Publish the folder on the gh-pages branch; it is served at
bridge.PAGE_URL.

It also writes the dashboard page (d/) and the portal viewer (portal/), then signs the whole folder: release.json
lists every file with its SHA-256 and release.sig signs it with the update-signing key (_secrets), so that each
office PC refuses to hand out links to a page Biruni did not release (secure/door.py). Pages added to the folder
later (the showcase paper/ page) need: build_bridge_page.py --sign-only

The Google Drive link page (field_photos/drive_page.py) is written beside it, to <out>-drive-helper\link\index.html:
it is published on its own site (repository biruni-drive, drive.biruni-gis.com), never with these pages.

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
from biruni.field_photos.server import all_phone_texts  # noqa: E402
from biruni.seal.page import public_page as seal_page  # noqa: E402
from biruni.live_share.viewer import public_page as share_page  # noqa: E402
from biruni.living_paper import public_page as map_page  # noqa: E402
from biruni.field_photos.room_page import room_page  # noqa: E402

from biruni.secure import csp, door  # noqa: E402

KEY = os.path.join(ROOT, "_secrets", "update-signing.key")
args = [a for a in sys.argv[1:] if a != "--sign-only"]
out = args[0] if args else os.path.join(ROOT, "build", "bridge-page")


def sign():
    if not os.path.isfile(KEY):
        print("NOT signed: no", KEY, "- every office PC will refuse links to these pages")
        return
    rel = door.sign_folder(out, KEY)
    print(f"signed release.json: {len(rel['files'])} files, version {rel['version']}")


if "--sign-only" in sys.argv[1:]:
    csp.apply_site(out)
    sign()
    sys.exit(0)
from biruni.field_photos import drive_page as _dpv  # noqa: E402

_test = [v for v in _dpv.TEST_VARS if os.environ.get(v)]
if _test:   # the test's fake Drive / sign-in would be built into the published pages
    sys.exit("NOT built: test settings are set (" + ", ".join(_test) + ")")
texts = all_phone_texts()
page = bridge.phone_page(texts)
page = page.replace("<title>Biruni GIS</title>",
                    '<title>Biruni GIS</title><meta name="robots" content="noindex">'
                    '<meta name="referrer" content="no-referrer">')
from biruni.field_photos import drive_page as _dp  # noqa: E402

# only its own script runs; it talks only to the relays and the Drive API, and frames only the Drive link page
page = bridge.with_csp(page, connect=(_dp.API,), frames=(_dp.HELPER,))
os.makedirs(os.path.join(out, "p"), exist_ok=True)
with open(os.path.join(out, "p", "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(page)
with open(os.path.join(out, "p", "sw.js"), "w", encoding="utf-8", newline="\n") as f:   # opens without internet
    f.write(bridge.SW_JS)
# the page a printed map's QR code opens (living_paper/) - no no-referrer: the map tiles need the origin
os.makedirs(os.path.join(out, "m"), exist_ok=True)
with open(os.path.join(out, "m", "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(map_page())
# the page a room code opens in the phone's camera app (field_photos, inside a building)
os.makedirs(os.path.join(out, "r"), exist_ok=True)
with open(os.path.join(out, "r", "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(room_page())
# the page that shows a map shared live (live_share/)
os.makedirs(os.path.join(out, "s"), exist_ok=True)
with open(os.path.join(out, "s", "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(share_page())
# the page that checks a sealed file (seal/)
os.makedirs(os.path.join(out, "v"), exist_ok=True)
with open(os.path.join(out, "v", "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(seal_page())
with open(os.path.join(out, ".nojekyll"), "w") as f:
    pass
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
print(f"{out}: p/index.html ({len(page) // 1024} KB, {len(texts)} languages)")
from biruni.field_photos import drive_page  # noqa: E402

helper = drive_page.helper_page()
assert ("https://www.googleapis.com" in helper and "https://accounts.google.com/gsi/client" in helper
        and "127.0.0.1" not in helper and "localhost" not in helper), "the link page must point at Google"
assert "accounts.google.com" not in page, "the photo page must not load Google's script"
hdir = os.path.join(os.path.normpath(out) + "-drive-helper", "link")
os.makedirs(hdir, exist_ok=True)
with open(os.path.join(hdir, "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(helper)
print(f"{os.path.join(hdir, 'index.html')} (the Google Drive link page: publish it to the biruni-drive repository)")
csp.apply_site(out)
sign()
