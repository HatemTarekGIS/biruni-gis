"""What a map is for, and what that means for the page.

Every purpose fixes the page (or the choice of pages), the orientation rule, the elements, the type sizes for the
distance the map is read from, the resolution, the grid and the legend style. Sizes are in millimetres; screen
formats are given in pixels and turned into millimetres at their resolution."""

MM_PER_INCH = 25.4

# element ids
TITLE, SUBTITLE, LEGEND, NORTH, SCALE, CREDITS, INSET, GRID, CORNERS, TITLEBLOCK = (
    "title", "subtitle", "legend", "north", "scale", "credits", "inset", "grid", "corners", "titleblock")


class Intent:
    def __init__(self, iid, label, icon, help_text, pages, orientation="auto", elements=(), title_pt=18.0,
                 body_pt=8.0, min_pt=6.0, dpi=300, grid="none", legend="compact", margin=8.0, pixels=False,
                 delegate=None, font="Segoe UI", big_title=False):
        self.id = iid
        self.label = label                # English; shown through tr()
        self.icon = icon                  # Tabler icon name
        self.help = help_text             # English; shown through tr()
        self.pages = pages                # [(width, height)] portrait-first; mm, or px when pixels=True
        self.orientation = orientation    # auto | portrait | landscape | fixed (use the page as given)
        self.elements = frozenset(elements)
        self.title_pt = title_pt
        self.body_pt = body_pt            # legend and notes
        self.min_pt = min_pt              # nothing smaller is readable at the viewing distance
        self.dpi = dpi
        self.grid = grid                  # map_grid style
        self.legend = legend              # compact | full | none
        self.margin = margin              # mm
        self.pixels = pixels
        self.delegate = delegate          # "sheet": built by the quick map sheet with a sheet standard
        self.font = font
        self.big_title = big_title

    def page_mm(self, k=0):
        w, h = self.pages[min(k, len(self.pages) - 1)]
        if self.pixels:
            return w / self.dpi * MM_PER_INCH, h / self.dpi * MM_PER_INCH
        return float(w), float(h)

    def page_for(self, aspect, k=0):
        """(width, height) in mm for data of the given width/height ratio, following the orientation rule."""
        w, h = self.page_mm(k)
        if self.orientation == "fixed":
            return w, h
        portrait = (min(w, h), max(w, h))
        landscape = (max(w, h), min(w, h))
        if self.orientation == "portrait":
            return portrait
        if self.orientation == "landscape":
            return landscape
        return landscape if aspect >= 1.0 else portrait

    def has(self, element):
        return element in self.elements


A4, A3, A1, A0, A5 = (210, 297), (297, 420), (594, 841), (841, 1189), (148, 210)
_STD = (TITLE, LEGEND, NORTH, SCALE, CREDITS)

INTENTS = {i.id: i for i in [
    Intent("deed", "Deed sketch (kroki)", "file-certificate",
           "A4 sheet around one parcel: corner coordinates table, side lengths, grid, north arrow and the title "
           "block of your country's survey sketch.",
           [A4], "portrait", (TITLE, NORTH, SCALE, GRID, CORNERS, TITLEBLOCK), 14, 7, 6, 300, "projected", "none",
           delegate="sheet"),
    Intent("report", "Report figure", "file-text",
           "A figure for a Word or PDF report: the width of the text column, a compact legend, no frame clutter.",
           [(170, 120)], "fixed", (LEGEND, NORTH, SCALE, CREDITS), 11, 7, 6, 300, "none", "compact", margin=3),
    Intent("thesis", "Thesis / journal figure", "school",
           "Journal sizes (175 mm double column, 85 mm single column), 300 dpi, sans-serif type no smaller than "
           "6 pt.",
           [(175, 125), (85, 85)], "fixed", (LEGEND, NORTH, SCALE, CREDITS, GRID), 10, 7, 6, 300, "geographic",
           "compact", margin=2, font="Arial"),
    Intent("wall", "Wall map (A1 / A0)", "layout-board",
           "A big printed map read from a few metres: large title, inset location map, coordinate grid, credits.",
           [A1, A0], "auto", _STD + (SUBTITLE, INSET, GRID), 54, 16, 12, 200, "both", "full", margin=20,
           big_title=True),
    Intent("slide", "Presentation slide (16:9)", "presentation",
           "A slide for PowerPoint or a projector: large type, a minimal legend, nothing that cannot be read from "
           "the back of the room.",
           [(338.67, 190.5)], "fixed", (TITLE, LEGEND, SCALE, CREDITS), 30, 14, 12, 150, "none", "compact",
           margin=8, big_title=True),
    Intent("social_square", "Social post (1080 × 1080)", "brand-instagram",
           "A square image for Instagram, Facebook or LinkedIn: a big title, a credit line in the corner.",
           [(1080, 1080)], "fixed", (TITLE, LEGEND, CREDITS), 34, 14, 11, 144, "none", "compact", margin=6,
           pixels=True, big_title=True),
    Intent("social_portrait", "Social post (1080 × 1350)", "device-mobile",
           "A tall post (4:5), the size that fills a phone screen in a feed.",
           [(1080, 1350)], "fixed", (TITLE, LEGEND, CREDITS), 34, 14, 11, 144, "none", "compact", margin=6,
           pixels=True, big_title=True),
    Intent("story", "Story / status (1080 × 1920)", "device-mobile-message",
           "A full-screen story or WhatsApp status (9:16).",
           [(1080, 1920)], "fixed", (TITLE, LEGEND, CREDITS), 38, 16, 12, 144, "none", "compact", margin=6,
           pixels=True, big_title=True),
    Intent("social_wide", "Wide post (1920 × 1080)", "device-desktop",
           "A wide image (16:9) for X, YouTube or a website banner.",
           [(1920, 1080)], "fixed", (TITLE, LEGEND, CREDITS), 34, 14, 11, 144, "none", "compact", margin=6,
           pixels=True, big_title=True),
    Intent("atlas", "Atlas page", "book",
           "One page of an atlas or a map book: title, legend, scale, a small location map.",
           [A4], "auto", _STD + (INSET,), 18, 8, 6, 300, "projected", "compact", margin=10),
    Intent("engineering", "Engineering drawing sheet", "ruler-3",
           "A drawing sheet of your country's standard (ABNT, ISO, ANSI…) with its title block, grid and "
           "coordinates.",
           [A3], "landscape", (TITLE, NORTH, SCALE, GRID, CORNERS, TITLEBLOCK, LEGEND), 14, 7, 6, 300, "both",
           "compact", delegate="sheet"),
]}

# every text above, so scripts/extract_strings.py finds them for translation (shown through tr())
TITLES = [t for i in INTENTS.values() for t in (i.label, i.help)]

ORDER = ["report", "thesis", "slide", "wall", "social_square", "social_portrait", "story", "social_wide", "atlas",
         "deed", "engineering"]
# outputs offered for "one map, many outputs" (export id -> intent id or file kind)
OUTPUTS = {
    "pdf": "Print PDF",
    "png": "Image (PNG)",
    "slide": "Slide (16:9)",
    "social_square": "Square post (1080 × 1080)",
    "social_portrait": "Tall post (1080 × 1350)",
    "story": "Story (1080 × 1920)",
    "card": "Map card (JSON)",
}


def get(iid):
    return INTENTS.get(iid) or INTENTS["report"]


def readable_pt(distance_m):
    """Smallest comfortable type size (pt) for a reading distance: about 1 pt per 7 cm, at least 6 pt."""
    return max(6.0, round(distance_m * 100 / 7.0, 1))
