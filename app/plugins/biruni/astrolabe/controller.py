"""The astrolabe on the map: the disc (disc.py) over the canvas, the sentence it writes, the preview of the
command on the map while the rings turn, and the command carried out when the hub is pressed.

A command reads as a sentence - "Buffer 15 m around layer Roads -> new layer" - and "Then +" adds a step that
works on the result of the step before, so three tools become one sentence.
"""
import html

from qgis.core import Qgis, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvasItem, QgsRubberBand
from qgis.PyQt.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QCursor, QFont, QPainterPath, QPen, QTextDocument
from qgis.PyQt.QtWidgets import QApplication, QLabel, QMenu, QPushButton, QToolButton

from ..i18n import tr
from . import engine
from .disc import BRASS, GHOST, Disc, Ring, short
from .layers import PREVIEW_CAP, CommitError, LayerSource, commit

SQM = {"m2": 1.0, "ha": 1 / 10000.0, "ft2": 10.7639104, "ac": 1 / 4046.8564224}
METRE = {"m2": 1.0, "ha": 1 / 1000.0, "ft2": 3.2808399, "ac": 1 / 1609.344}


def area_unit(opt):
    return {"m2": tr("m²"), "ha": tr("ha"), "ft2": tr("ft²"), "ac": tr("ac")}[opt]


def length_unit(opt):   # a line measured "in hectares" is given in the length that goes with them
    return {"m2": tr("m"), "ha": tr("km"), "ft2": tr("ft"), "ac": tr("mi")}[opt]


def verbs():
    words = (("buffer", tr("ring|Buffer")), ("split", tr("ring|Split")), ("union", tr("ring|Dissolve")),
             ("intersect", tr("ring|Intersect")), ("erase", tr("ring|Erase")), ("select", tr("ring|Select")),
             ("rotate", tr("ring|Rotate")), ("scale", tr("ring|Resize")), ("centroid", tr("ring|Centre")),
             ("hull", tr("ring|Hull")), ("measure", tr("ring|Measure")))
    return [(k, short(w)) for k, w in words]


def sentences():
    return {"buffer": tr("Buffer {v} around {t} → new layer"),
            "split": tr("Split {t} into {v} equal-area parts"),
            "union": tr("Dissolve {t} into one shape → new layer"),
            "intersect": tr("Intersect {t} with {o} → new layer"),
            "erase": tr("Erase {o} from {t} → new layer"),
            "select": tr("Select from {t} what touches {o}"),
            "rotate": tr("Rotate {t} by {v}"),
            "scale": tr("Resize {t} to {v}"),
            "centroid": tr("Centre point of each of {t} → new layer"),
            "hull": tr("Hull around {t}, {v} → new layer"),
            "measure": tr("Measure {t} in {v}")}


def errors():
    return {"no_selection": tr("Nothing is selected. Select features on the map first."),
            "no_target": tr("This target has no features."),
            "empty": tr("This command gives nothing here."),
            "same_layer": tr("Choose another layer on the outer ring, not the target itself."),
            "other_empty": tr("The layer chosen on the outer ring has no features."),
            "polygons_only": tr("Only polygons can be split."),
            "layer_features_only": tr("Selecting works on a layer's features, not on a step's result."),
            "failed": tr("This command could not be worked out on these shapes."),
            "no_layer": tr("Open a vector layer first."),
            "readonly": tr("Layer {name} cannot be edited.")}


def measure_text(at, value, kind, st):
    opt = st.get("opt", "m2")
    if kind == "area":
        return f"{value * SQM[opt]:,.{3 if opt in ('ha', 'ac') else 1}f} {area_unit(opt)}"
    if kind == "length":
        return f"{value * METRE[opt]:,.{3 if opt in ('ha', 'ac') else 1}f} {length_unit(opt)}"
    return f"{at.x():,.2f}, {at.y():,.2f}"


class GhostLabels(QgsMapCanvasItem):
    """Numbers of the preview written on the map (the areas of the parts, the measures), and the pivot."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.labels = []     # (QgsPointXY in the map's CRS, text)
        self.pivot = None
        self.colour = QColor(GHOST["light"])
        self.halo = QColor("#FFFFFF")
        self.setZValue(900)

    def set(self, labels, pivot):
        self.labels, self.pivot = labels, pivot
        self.update()

    def updatePosition(self):  # noqa: N802
        self.setPos(0, 0)

    def boundingRect(self):  # noqa: N802
        return QRectF(0, 0, self.canvas.width(), self.canvas.height())

    def paint(self, painter, option=None, widget=None):
        if not self.labels and self.pivot is None:
            return
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        f = QFont()
        f.setPointSizeF(9)
        f.setBold(True)
        fm = painter.fontMetrics()
        painter.setFont(f)
        fm = painter.fontMetrics()
        for at, text in self.labels:
            p = self.toCanvasCoordinates(at)
            w = fm.horizontalAdvance(text)
            path = QPainterPath()
            path.addText(QPointF(p.x() - w / 2.0, p.y() + fm.ascent() / 2.0 - 1), f, text)
            painter.setPen(QPen(self.halo, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.colour)
            painter.drawPath(path)
        if self.pivot is not None:
            p = self.toCanvasCoordinates(self.pivot)
            painter.setPen(QPen(self.colour, 1.6))
            painter.drawLine(QPointF(p.x() - 6, p.y()), QPointF(p.x() + 6, p.y()))
            painter.drawLine(QPointF(p.x(), p.y() - 6), QPointF(p.x(), p.y() + 6))
        painter.restore()


class Ghost:
    """The command drawn on the map before it happens: the step being set in full colour, the steps before it
    fainter."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.bands = {}
        self.labels = GhostLabels(canvas)
        self.colour = QColor(GHOST["light"])

    def set_theme(self, dark, halo):
        self.colour = QColor(GHOST["dark" if dark else "light"])
        self.labels.colour = self.colour
        self.labels.halo = QColor(halo)

    def _band(self, kind, level):
        key = (kind, level)
        rb = self.bands.get(key)
        if rb is None:
            rb = self.bands[key] = QgsRubberBand(self.canvas, {"sel": Qgis.GeometryType.Line}.get(kind, kind))
        a = 1.0 if level else .45
        stroke, fill = QColor(self.colour), QColor(self.colour)
        stroke.setAlphaF(a)
        fill.setAlphaF(.2 * a if kind == Qgis.GeometryType.Polygon else a)
        rb.setStrokeColor(stroke)
        rb.setFillColor(fill)
        if kind == "sel":
            rb.setWidth(3)
        elif kind == Qgis.GeometryType.Point:
            rb.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
            rb.setIconSize(9)
            rb.setWidth(2)
        else:
            rb.setWidth(2)
            rb.setLineStyle(Qt.PenStyle.DashLine)
        return rb

    def clear(self):
        for (kind, _level), rb in self.bands.items():
            rb.reset({"sel": Qgis.GeometryType.Line}.get(kind, kind))
        self.labels.set([], None)

    def show(self, steps, outs, crs):
        self.clear()
        to_map = QgsCoordinateTransform(crs, self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
        labels, pivot = [], None
        for n, (st, o) in enumerate(zip(steps, outs)):
            if o.err:
                continue
            level = 1 if n == len(outs) - 1 else 0
            for g in o.geoms:
                self._band(g.type(), level).addGeometry(g, crs, False)
            for item in o.sel or []:
                g = item.geom
                self._band("sel", level).addGeometry(g if g.type() != Qgis.GeometryType.Polygon
                                                     else g.convertToType(Qgis.GeometryType.Line, True), crs, False)
            if level:
                for at, value, kind in o.labels:
                    try:
                        labels.append((to_map.transform(QgsPointXY(at)), measure_text(at, value, kind, st)))
                    except Exception:  # noqa: BLE001
                        pass
                if o.pivot is not None:
                    pivot = to_map.transform(o.pivot)
        for rb in self.bands.values():
            rb.updatePosition()
            rb.update()
        self.labels.set(labels, pivot)

    def remove(self):
        scene = self.canvas.scene()
        for rb in self.bands.values():
            scene.removeItem(rb)
        self.bands = {}
        scene.removeItem(self.labels)


class Astrolabe(QObject):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.canvas = plugin.iface.mapCanvas()
        self.chain = []          # the steps before the one on the rings
        self.history = []        # [(steps, sentence)]
        self.outs = []
        self.source = None
        self.last_result = None
        self.disc = Disc(self.canvas)
        self.disc.hide()
        self.say = QLabel(self.canvas)
        self.say.setTextFormat(Qt.TextFormat.RichText)
        self.say.setWordWrap(True)
        self.say.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.say.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.say.hide()
        self.close_btn = self._button("×", tr("Close (Esc)"), self.close)
        self.then_btn = self._button(tr("Then +"), tr("Add a step that works on this step's result (Shift+Enter)"), self.then)
        self.back_btn = self._button(tr("Back"), tr("Back one step"), self.back)
        self.hist_btn = self._button(tr("History"), tr("The commands carried out: choose one to change it or run it again"), self._history_menu)
        self.ghost = Ghost(self.canvas)
        self.ring0 = Ring([("sel", tr("Selected"))])
        self.ring1 = Ring(verbs())
        self.disc.set_ring(1, self.ring1)
        self.params = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._refresh_now)
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(150)
        self._view_timer.timeout.connect(self._view_changed)
        d = self.disc
        d.changed.connect(self._ring_changed)
        d.run.connect(self.execute)
        d.then.connect(self.then)
        d.back.connect(self.back)
        d.closed.connect(self.close)
        d.dragged.connect(self._place)
        self._links = []
        self._theme()
        theme = getattr(plugin, "theme", None)
        if theme is not None and hasattr(theme, "on_change"):
            theme.on_change(self._theme)

    # ---- widgets -------------------------------------------------------------------------------------------------
    def _button(self, text, tip, slot):
        b = QToolButton(self.canvas)
        b.setText(text)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda _=False: slot())
        b.hide()
        return b

    def _tokens(self):
        theme = getattr(self.plugin, "theme", None)
        t = theme.tokens() if theme is not None else self.disc.tokens
        dark = bool(theme.is_dark()) if theme is not None and hasattr(theme, "is_dark") else False
        return t, dark

    def _theme(self):
        t, dark = self._tokens()
        self.dark = dark
        self.disc.set_theme(t, dark)
        self.ghost.set_theme(dark, t["panel"])
        self.say.setStyleSheet(f"QLabel{{background:{t['panel']};color:{t['text']};border:1px solid {t['border']};"
                               "border-radius:10px;padding:6px 12px;}")
        plain = (f"QToolButton{{background:{t['panel']};color:{t['text']};border:1px solid {t['border']};"
                 "border-radius:13px;padding:2px 9px;font-weight:600;}"
                 f"QToolButton:hover{{border-color:{t['accent']};}}")
        for b in (self.close_btn, self.back_btn, self.hist_btn):
            b.setStyleSheet(plain)
        self.then_btn.setStyleSheet(f"QToolButton{{background:{t['accent']};color:{t['accent_text']};border:0;"
                                    "border-radius:13px;padding:3px 11px;font-weight:600;}")
        if self.is_open():
            self._refresh_now()

    def is_open(self):
        return self.disc.isVisible()

    # ---- the rings follow the project ----------------------------------------------------------------------------
    def _layer_items(self):
        return [("L:" + lyr.id(), lyr.name()) for lyr in self.source.layers()]

    def _sync_rings(self):
        prev = [("prev", short(tr("ring|Result")))] if self.chain else []
        layers = self._layer_items()
        self.ring0.set_items([("sel", tr("Selected"))] + layers + [("view", tr("In view"))] + prev)
        for verb in ("intersect", "erase", "select"):
            self.params[verb].set_items([("sel", tr("Selected"))] + layers + prev)
        self.disc.set_ring(0, self.ring0)
        self.disc.set_ring(2, self.params[self.ring1.key])

    def _make_params(self):
        unit = " " + tr("m")
        layers = self._layer_items()
        second = layers[1][0] if len(layers) > 1 else "sel"

        def other():
            return Ring([("sel", tr("Selected"))] + layers, key=second)

        def none():
            return Ring([("none", tr("No value"))], dim=True)
        self.params = {
            "buffer": Ring(min=0, max=100, step=1, k=3, minor=1, major=10, unit=unit, value=10, fine=True),
            "split": Ring(min=2, max=12, step=1, k=30, minor=1, major=1, unit="", value=3),
            "union": none(), "intersect": other(), "erase": other(), "select": other(),
            "rotate": Ring(min=-180, max=180, step=1, k=1, minor=5, major=30, unit="°", value=30, fine=True),
            "scale": Ring(min=25, max=300, step=1, k=1, minor=5, major=25, unit="%", value=150),
            "centroid": none(),
            "hull": Ring([("all", tr("All together")), ("each", tr("Each one"))]),
            "measure": Ring([(k, area_unit(k)) for k in ("m2", "ha", "ft2", "ac")])}

    def _apart(self):
        """A verb that works with another layer cannot work with its own target: the target moves aside."""
        verb = self.ring1.key
        if verb not in ("intersect", "erase") or self.ring0.key != self.params[verb].key:
            return
        keys = [k for k, _l in self.ring0.items if k not in (self.ring0.key, "view")]
        if keys:
            self.ring0.set_key(next((k for k in keys if k.startswith("L:")), keys[0]))

    def _ring_changed(self, z):
        if z == 1:
            self._apart()
            self.disc.set_ring(2, self.params[self.ring1.key])
        self.refresh()

    # ---- steps ---------------------------------------------------------------------------------------------------
    def step(self):
        verb = self.ring1.key
        p = self.params[verb]
        st = {"t": self.ring0.key, "v": verb}
        if p.kind == "scale":
            st["val"] = p.value
        elif verb in ("intersect", "erase", "select"):
            st["other"] = p.key
        elif not p.dim:
            st["opt"] = p.key
        return st

    def load_step(self, st):
        self.ring0.set_key(st["t"])
        self.ring1.set_key(st["v"])
        p = self.params[self.ring1.key]
        if p.kind == "scale":
            p.value = p.snap(float(st.get("val", p.value)), .01)
            p.angle = p.rest_angle()
        else:
            p.set_key(st.get("other") or st.get("opt"))
        self.disc.set_ring(0, self.ring0)
        self.disc.set_ring(1, self.ring1)
        self.disc.set_ring(2, p)

    def steps(self):
        return [dict(s) for s in self.chain] + [self.step()]

    # ---- the sentence --------------------------------------------------------------------------------------------
    def _name(self, key):
        lyr = self.source.layer(key) if self.source is not None else None
        return lyr.name() if lyr is not None else "?"

    def _target(self, key, n):
        if key == "sel":
            return tr("the selected features ({n})").format(n=n)
        if key == "prev":
            return tr("the previous step's result ({n})").format(n=n)
        if key == "view":
            return tr("{name} in view ({n})").format(name=self._name(key), n=n)
        return tr("layer {name} ({n})").format(name=self._name(key), n=n)

    def _other(self, key):
        if key == "sel":
            return tr("the selected features")
        if key == "prev":
            return tr("the previous step's result")
        return tr("layer {name}").format(name=self._name(key))

    def _value(self, st):
        p = self.params[st["v"]]
        if "val" in st:
            return f"{st['val']:g}{p.unit}"
        if "opt" in st:
            return next((label for k, label in p.items if k == st["opt"]), "")
        return ""

    def sentence(self, st, o=None, rich=False):
        t, v, other = self._target(st["t"], o.n if o is not None else 0), self._value(st), self._other(st.get("other", ""))
        if rich:
            tok, dark = self._tokens()
            brass = BRASS["dark" if dark else "light"]
            t = f"<span style='color:{tok['accent']};font-weight:600'>{html.escape(t)}</span>"
            v = f"<span style='color:{brass};font-weight:600'>{html.escape(v)}</span>"
            other = f"<span style='color:{brass};font-weight:600'>{html.escape(other)}</span>"
        return sentences()[st["v"]].format(t=t, v=v, o=other)

    def command_text(self, steps, outs=None):
        return tr(", then ").join(self.sentence(st, outs[i] if outs else None) for i, st in enumerate(steps))

    def _note(self, st, o):
        if o.err:
            return errors().get(o.err, o.err), True
        notes = []
        if o.note and o.note[0] == "select":
            notes.append(tr("{k} of {n} will be selected").format(k=o.note[1], n=o.note[2]))
        elif o.note and o.note[0] == "split":
            notes.append(tr("{a} → {b} parts").format(a=o.note[1], b=o.note[2]))
        if self.source.capped.get(st["t"]):
            notes.append(tr("The preview shows {k} features; the command runs on all {n}.").format(k=PREVIEW_CAP, n=o.n))
        return " · ".join(notes), False

    def _say(self):
        steps, tok = self.steps(), self._tokens()[0]
        rows = []
        for i, (st, o) in enumerate(zip(steps, self.outs)):
            s = self.sentence(st, o, rich=True)
            no = f"<b>{i + 1}.</b> " if len(steps) > 1 else ""
            if i < len(steps) - 1:
                rows.append(f"<div style='color:{tok['text_dim']};font-size:small'>{no}{s}</div>")
            else:
                rows.append(f"<div>{no}{s}</div>")
        note, bad = self._note(steps[-1], self.outs[-1])
        if note:
            colour = GHOST["dark" if self.dark else "light"] if bad else tok["text_dim"]
            rows.append(f"<div style='color:{colour};font-size:small'>{html.escape(note)}</div>")
        self.say.setText("".join(rows))
        self._layout()

    # ---- preview -------------------------------------------------------------------------------------------------
    def refresh(self):
        if self.is_open():
            self._timer.start()

    def _refresh_now(self):
        if not self.is_open() or self.source is None:
            return
        steps = self.steps()
        self.outs = engine.run(steps, self.source)
        self.ghost.show(steps, self.outs, self.source.crs)
        self._say()
        self.back_btn.setVisible(bool(self.chain))

    def _view_changed(self):
        if self.is_open() and self.source is not None:
            for key in ["view"] + [k for k in self.source.capped]:
                self.source._cache.pop(key, None)
            self._refresh_now()

    def _data_changed(self, *_a):
        if self.is_open() and self.source is not None:
            self.source.invalidate()
            self.refresh()

    def _project_changed(self, *_a):
        if not self.is_open():
            return
        self._new_source()
        self._sync_rings()
        self.refresh()

    def _new_source(self):
        active = self.iface.activeLayer() if hasattr(self.iface, "activeLayer") else None
        self.source = LayerSource(self.canvas, active if isinstance(active, QgsVectorLayer) else None, cap=PREVIEW_CAP)

    # ---- open / close --------------------------------------------------------------------------------------------
    def _size(self):
        w, h = self.canvas.width(), self.canvas.height()
        return int(max(260, min(400, min(w, h) * .5)))

    def _layout(self):
        d = self.disc
        x, y, size = d.x(), d.y(), d.width()
        # as wide as its longest line, up to a limit (a word-wrapped label left alone picks a narrow column)
        doc = QTextDocument()
        doc.setDefaultFont(self.say.font())
        doc.setHtml(self.say.text())
        limit = min(600, max(260, self.canvas.width() - 24))
        self.say.setFixedWidth(int(min(limit, doc.idealWidth() + 30)))
        self.say.setFixedHeight(self.say.heightForWidth(self.say.width()))
        sw, sh = self.say.width(), self.say.height()
        sx = max(4, min(self.canvas.width() - sw - 4, x + (size - sw) // 2))
        sy = y - sh - 8 if y - sh - 8 >= 4 else y + size + 8
        self.say.move(sx, sy)
        for b in (self.close_btn, self.then_btn, self.back_btn, self.hist_btn):
            b.adjustSize()
        self.close_btn.move(x + size - self.close_btn.width(), y)
        self.hist_btn.move(x, y)
        self.then_btn.move(x + size - self.then_btn.width(), y + size - self.then_btn.height())
        self.back_btn.move(x, y + size - self.back_btn.height())

    def _place(self, x, y):
        size = self.disc.width()
        x = max(4, min(self.canvas.width() - size - 4, int(x)))
        y = max(4, min(self.canvas.height() - size - 4, int(y)))
        self.disc.move(x, y)
        self._layout()

    def _beside_target(self, at, size):
        """A place for the disc beside what the command works on (so the preview stays in sight); else ``at``."""
        items = self.source.items(self.ring0.key) if self.ring0.key != "prev" else []
        if not items or len(items) > 400:
            return at
        box = None
        for i in items:
            b = i.geom.boundingBox()
            box = b if box is None else box
            box.combineExtentWith(b)
        try:
            ct = QgsCoordinateTransform(self.source.crs, self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
            box = ct.transformBoundingBox(box)
        except Exception:  # noqa: BLE001
            return at
        m2p = self.canvas.getCoordinateTransform()
        a, b = m2p.transform(box.xMinimum(), box.yMaximum()), m2p.transform(box.xMaximum(), box.yMinimum())
        x0, y0, x1, y1 = a.x(), a.y(), b.x(), b.y()
        mx, my, gap, w, h = (x0 + x1) / 2, (y0 + y1) / 2, 34, self.canvas.width(), self.canvas.height()
        if (x1 - x0) > w * .6 or (y1 - y0) > h * .6:
            return at
        cands = [(x1 + size / 2 + gap, my, 0), (x0 - size / 2 - gap, my, 0), (mx, y1 + size / 2 + gap, 1),
                 (mx, y0 - size / 2 - gap - 50, 1)]
        fits = [c for c in cands if c[0] - size / 2 >= 4 and c[0] + size / 2 <= w - 4
                and c[1] - size / 2 >= 50 and c[1] + size / 2 <= h - 4]
        if not fits:
            return at
        fits.sort(key=lambda c: c[2] * 1e4 + abs(c[0] - at.x()) + abs(c[1] - at.y()))
        return QPoint(int(fits[0][0]), int(fits[0][1]))

    def open(self, at=None):
        """Show the astrolabe at a point of the map (canvas pixels); by default beside what is selected."""
        self._new_source()
        if not self.source.layers():
            self._message(errors()["no_layer"], Qgis.MessageLevel.Warning)
            return False
        if not self.params:
            self._make_params()
        self.chain = []
        self._sync_rings()
        if self.ring0.key in (None, "-"):
            self.ring0.set_key("sel")
        if self.ring0.key == "sel" and not self.source.count("sel"):
            self.ring0.set_key(self._layer_items()[0][0])   # nothing selected: the current layer
            self.disc.set_ring(0, self.ring0)
        self._apart()
        size = self._size()
        self.disc.set_size(size)
        if at is None:
            at = QPoint(self.canvas.width() // 2, self.canvas.height() // 2)
        at = self._beside_target(at, size)
        for w in (self.disc, self.say, self.close_btn, self.then_btn, self.hist_btn):
            w.show()
            w.raise_()
        self._place(at.x() - size // 2, at.y() - size // 2)
        self.disc.setFocus()
        self._connect(True)
        self._refresh_now()
        return True

    def close(self):
        self._connect(False)
        for w in (self.disc, self.say, self.close_btn, self.then_btn, self.back_btn, self.hist_btn):
            w.hide()
        self.chain = []
        self.ghost.clear()
        self.canvas.setFocus()

    def toggle(self):
        if self.is_open():
            self.close()
            return
        p = self.canvas.mapFromGlobal(QCursor.pos())
        self.open(p if self.canvas.rect().contains(p) else None)

    def _connect(self, on):
        if on and not self._links:
            self._links = [(self.canvas.selectionChanged, self._data_changed),
                           (self.canvas.extentsChanged, self._view_timer.start),
                           (QgsProject.instance().layersAdded, self._project_changed),
                           (QgsProject.instance().layersRemoved, self._project_changed)]
            if hasattr(self.iface, "currentLayerChanged"):
                self._links.append((self.iface.currentLayerChanged, self._project_changed))
            for sig, slot in self._links:
                sig.connect(slot)
        elif not on:
            for sig, slot in self._links:
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._links = []

    # ---- then / back / run ---------------------------------------------------------------------------------------
    def then(self):
        if not self.is_open() or not self.outs:
            return
        o = self.outs[-1]
        if o.err:
            return
        self.chain.append(self.step())
        self._sync_rings()
        self.ring0.set_key("prev")
        for verb in ("intersect", "erase", "select"):
            self.params[verb].set_key("prev")
        self._apart()
        self.disc.set_ring(0, self.ring0)
        self.disc.set_ring(2, self.params[self.ring1.key])
        self._refresh_now()

    def back(self):
        if not self.chain:
            return
        st = self.chain.pop()
        self._sync_rings()
        self.load_step(st)
        self._refresh_now()

    def load(self, steps):
        """Put a command back on the rings (from the history) to change it or run it again."""
        if not self.is_open() and not self.open():
            return
        self.chain = [dict(s) for s in steps[:-1]]
        self._sync_rings()
        self.load_step(steps[-1])
        self._refresh_now()

    def layer_name(self, st):
        return {"buffer": tr("Buffer {v}").format(v=self._value(st)), "union": tr("Dissolved"),
                "intersect": tr("Intersection"), "erase": tr("Erased"), "centroid": tr("Centres"),
                "hull": tr("Hull"), "measure": tr("Measurements")}.get(st["v"], tr("Result"))

    def execute(self, steps=None):
        """Carry the command out: on all the features, not only those of the preview."""
        if self.source is None:
            self._new_source()
        if not self.params:
            self._make_params()
        steps = [dict(s) for s in steps] if steps else self.steps()
        active = self.source.active
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            full = LayerSource(self.canvas, active)
            outs = engine.run(steps, full)
            bad = next((o for o in outs if o.err), None)
            if bad is not None:
                self._message(errors().get(bad.err, bad.err), Qgis.MessageLevel.Warning)
                return False
            text = self.command_text(steps, outs)
            try:
                result = commit(steps, outs, full, self.layer_name, measure_text, text)
            except CommitError as e:
                self._message(errors()[e.code].format(name=e.name), Qgis.MessageLevel.Warning)
                return False
        finally:
            QApplication.restoreOverrideCursor()
        self.last_result = result
        self.history.append((steps, text))
        del self.history[:-12]
        self.close()
        if active is not None and self.iface.activeLayer() is not active and hasattr(self.iface, "setActiveLayer"):
            self.iface.setActiveLayer(active)   # a new layer must not take the place of the layer being worked on
        done = tr("Done: {s}").format(s=text)
        if result["total"] is not None:
            done += " · " + tr("Total: {v}").format(
                v=measure_text(None, result["total"][0], result["total"][1], steps[-1]))
        self._message(done, Qgis.MessageLevel.Success, undo=True)
        log = getattr(getattr(self.plugin, "activity", None), "log", None)
        if log is not None:
            log("astrolabe", "+".join(s["v"] for s in steps), text[:200], "", len(steps))
        return True

    def repeat(self):
        """The last command again, on what is selected now."""
        if self.history:
            return self.execute(self.history[-1][0])
        return False

    def undo_last(self):
        r, self.last_result = self.last_result, None
        if not r:
            return
        for lyr in r["layers"]:
            try:
                QgsProject.instance().removeMapLayer(lyr.id())
            except RuntimeError:
                pass
        for lyr in r["edited"]:
            try:
                lyr.undoStack().undo()
                lyr.triggerRepaint()
            except RuntimeError:
                pass

    # ---- small things --------------------------------------------------------------------------------------------
    def _history_menu(self):
        m = QMenu(self.canvas)
        if not self.history:
            m.addAction(tr("No command yet")).setEnabled(False)
        for steps, text in reversed(self.history[-8:]):
            a = m.addAction(text if len(text) < 90 else text[:88] + "…")
            a.triggered.connect(lambda _=False, s=steps: self.load(s))
        m.exec(self.hist_btn.mapToGlobal(QPoint(0, self.hist_btn.height())))

    def _message(self, text, level, undo=False):
        bar = self.iface.messageBar() if hasattr(self.iface, "messageBar") else None
        if bar is None:
            return
        item = bar.createMessage(tr("Astrolabe"), html.escape(text))
        if undo:
            b = QPushButton(tr("Undo"))

            def back(_=False):
                self.undo_last()
                try:
                    bar.popWidget(item)
                except RuntimeError:
                    pass
            b.clicked.connect(back)
            item.layout().addWidget(b)
        bar.pushWidget(item, level, 12 if level == Qgis.MessageLevel.Success else 8)

    def unload(self):
        self.close()
        self.ghost.remove()
        for w in (self.disc, self.say, self.close_btn, self.then_btn, self.back_btn, self.hist_btn):
            w.deleteLater()
