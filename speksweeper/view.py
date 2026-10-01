"""Waterfall widget: spectrum on top, waterfall, time axis left, frequency axis + bands below.

Drawing happens in a "content" coordinate system that can be rotated by 90°/270°
(landscape use even when the system itself does not rotate).
"""
import math
import time
from types import SimpleNamespace

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .bands import BANDS, POINTS  # noqa: E402

MIN_SPAN = 200e3
ROW_PX = 2            # minimum height of a sweep row (logical pixels)
ROW_MAX = 16          # maximum height (long sweeps over the full range)
AXIS_L = 52           # time axis
AXIS_B = 40           # frequency axis + band strip
FONT = "Sans 8"
SNAP_X = 16           # the marker snaps to the highest value within ± this many pixels …
SNAP_Y = 8
SNAP_DB_PER_PX = 0.15  # … with more distant points slightly penalised
SNAP_DERIVED_DB = 3    # and derived (grey) values ranking slightly below measured ones
BTN_R = 22            # radius of the round on-canvas buttons (Δ, +, −)
HOP_SHOW_S = 3.0      # the "currently measured" box disappears this long after the last retune


def _rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def _lut(stops):
    x = np.linspace(0, 1, 256)
    pos = [s[0] for s in stops]
    lut = np.zeros((256, 4), np.uint8)
    for ch in range(3):
        lut[:, ch] = np.interp(x, pos, [s[1][ch] for s in stops])
    lut[:, 3] = 255
    return lut


def _greyed(lut):
    """"Slightly greyed out": desaturated and pulled towards mid grey."""
    f = lut[:, :3].astype(np.float32)
    grey = (f @ np.array([0.3, 0.59, 0.11], np.float32))[:, None]
    d = (0.35 * f + 0.65 * grey) * 0.7 + 28
    out = np.empty_like(lut)
    out[:, :3] = np.clip(d, 0, 255)
    out[:, 3] = 255
    return out


def _make_lut():
    """Combined colour table, index layout see LUT_* offsets."""
    level = _lut([(0.0, (2, 4, 22)), (0.2, (8, 30, 110)), (0.4, (0, 120, 200)),
                  (0.55, (40, 190, 150)), (0.7, (220, 220, 40)), (0.85, (245, 110, 20)),
                  (1.0, (255, 245, 235))])
    # difference: blue = weaker than the reference, dark = unchanged, yellow/red = stronger
    diff = _lut([(0.0, (90, 170, 255)), (0.3, (20, 60, 170)), (0.5, (14, 14, 18)),
                 (0.7, (170, 50, 20)), (0.85, (245, 150, 30)), (1.0, (255, 245, 200))])
    unknown = np.array([[24, 24, 28, 255]], np.uint8)
    return np.vstack([level, _greyed(level), unknown, diff, _greyed(diff)])


LUT = _make_lut()
LUT_DERIVED = 256     # offset added for derived values
LUT_UNKNOWN = 512
LUT_DIFF = 513        # start of the difference colour table (+256 for derived)


def row_height(r):
    """Row height grows with sweep duration (square root) so long sweeps stay visible."""
    dur = max(r.t1 - r.t0, 0.0)
    return int(min(ROW_MAX, max(ROW_PX, round(ROW_PX * math.sqrt(dur / 0.5)))))


def fmt_mhz(f, decimals):
    return f"{f / 1e6:.{decimals}f}"


def fmt_span(span):
    if span >= 100e6:
        return f"{span / 1e6:.0f} MHz"
    if span >= 1e6:
        return f"{span / 1e6:.3g} MHz"
    return f"{span / 1e3:.3g} kHz"


class WaterfallView(Gtk.Widget):
    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, history):
        super().__init__()
        self.history = history
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.range = (22e6, 1100e6)
        self.lo, self.hi = self.range
        self.rotation = 0                  # 0, 90, 270
        self.marker = None                 # (freq, row_id)
        self.status = "Waiting for stick …"
        self.levels = None                 # (lo_dB, hi_dB), smoothed
        self._dirty = True
        self._img = None                   # SimpleNamespace, see _render_image
        self.diff_sessions = []            # difference mode periods, see toggle_diff
        self.diff_range = None             # ± dB of the difference colour scale, smoothed
        self.current_hop = None            # (f0, f1, t) of the hop being measured
        self._btn = None                   # button pressed at drag begin
        self._font = Pango.FontDescription.from_string(FONT)
        self._drag_start = None
        self._zooming = False
        self._drag_dead = False

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._on_zoom_begin)
        zoom.connect("scale-changed", self._on_zoom_changed)
        zoom.connect("end", self._on_zoom_end)
        self.add_controller(zoom)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        self._pointer = None
        self.force_size = None             # (w, h) for tests without a window
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: setattr(self, "_pointer", (x, y)))
        self.add_controller(motion)

    # ------------------------------------------------------------------ geometry
    def _size(self):
        return self.force_size or (self.get_width(), self.get_height())

    def content_size(self):
        w, h = self._size()
        return (h, w) if self.rotation in (90, 270) else (w, h)

    def to_content(self, sx, sy):
        w, h = self._size()
        if self.rotation == 90:
            return sy, w - sx
        if self.rotation == 270:
            return h - sy, sx
        return sx, sy

    def vec_to_content(self, dx, dy):
        if self.rotation == 90:
            return dy, -dx
        if self.rotation == 270:
            return -dy, dx
        return dx, dy

    def layout(self):
        cw, ch = self.content_size()
        spec_h = 70 if ch > 420 else (44 if ch > 220 else 0)
        wf = (AXIS_L, spec_h, max(10, cw - AXIS_L - 4), max(10, ch - spec_h - AXIS_B))
        spec = (AXIS_L, 4, wf[2], spec_h - 8) if spec_h else None
        return wf, spec

    # ------------------------------------------------------------------ view
    def set_range(self, r0, r1):
        self.range = (r0, r1)
        self.set_view(self.lo, self.hi)

    def set_view(self, lo, hi, notify=True):
        r0, r1 = self.range
        span = min(max(hi - lo, MIN_SPAN), r1 - r0)
        c = 0.5 * (lo + hi)
        lo = min(max(c - span / 2, r0), r1 - span)
        if (lo, lo + span) != (self.lo, self.hi):
            self.lo, self.hi = lo, lo + span
            self._dirty = True
            self.queue_draw()
            if notify:
                self.emit("view-changed")

    def zoom(self, factor, anchor_frac=0.5):
        f = self.lo + anchor_frac * (self.hi - self.lo)
        span = (self.hi - self.lo) / factor
        self.set_view(f - anchor_frac * span, f + (1 - anchor_frac) * span)

    def full(self):
        self.set_view(*self.range)

    def set_rotation(self, rot):
        self.rotation = rot
        self._dirty = True
        self.queue_draw()
        self.emit("view-changed")

    def target_bins(self):
        wf, _ = self.layout()
        return int(min(2048, wf[2] * self.get_scale_factor()))

    def data_changed(self):
        self._dirty = True
        self.queue_draw()

    def set_current_hop(self, f0, f1, t):
        self.current_hop = (f0, f1, t)
        self.queue_draw()

    # ------------------------------------------------------------------ difference mode
    @property
    def diff_active(self):
        return bool(self.diff_sessions) and self.diff_sessions[-1].end is None

    def toggle_diff(self):
        """Starts/stops difference mode.

        On start, the newest completed sweep (with older sweeps filling in frequencies it did not
        cover) becomes the reference. All sweeps that start afterwards show the difference to it,
        until the mode is switched off again; earlier sweeps keep their normal display.
        """
        rows = list(self.history.rows)
        next_id = rows[-1].id + 1 if rows else 0
        if self.diff_active:
            self.diff_sessions[-1].end = next_id
        else:
            refs = [r for r in reversed(rows) if r.done][:100]
            self.diff_sessions.append(SimpleNamespace(start=next_id, end=None, refs=refs,
                                                      key=None, cols=None))
        self._dirty = True
        self.queue_draw()

    def _diff_session(self, row):
        for s in self.diff_sessions:
            if row.id >= s.start and (s.end is None or row.id < s.end):
                return s
        return None

    def _ref_columns(self, sess, edges, key):
        if sess.key != key:
            cols = np.full(len(edges) - 1, np.nan, np.float32)
            for r in sess.refs:
                miss = np.isnan(cols)
                if not miss.any():
                    break
                if r.f_hi < edges[0] or r.f_lo > edges[-1]:
                    continue
                cols[miss] = r.columns(edges)[miss]
            sess.key, sess.cols = key, cols
        return sess.cols

    def _buttons(self):
        """On-canvas buttons (content coordinates): name → (cx, cy)."""
        wf, _ = self.layout()
        x0, y0, ww, wh = wf
        cx = x0 + ww - BTN_R - 10
        cy = y0 + wh - BTN_R - 10
        gap = 2 * BTN_R + 10
        return {"zoom_out": (cx, cy), "zoom_in": (cx, cy - gap), "diff": (cx, cy - 2 * gap)}

    def _button_at(self, x, y):
        for name, (cx, cy) in self._buttons().items():
            if math.hypot(x - cx, y - cy) <= BTN_R + 4:
                return name
        return None

    def _press_button(self, name):
        if name == "zoom_in":
            self.zoom(2.0)
        elif name == "zoom_out":
            self.zoom(0.5)
        elif name == "diff":
            self.toggle_diff()

    # ------------------------------------------------------------------ input
    def _on_drag_begin(self, g, x, y):
        self._drag_start = (x, y, self.lo, self.hi)
        self._drag_dead = self._zooming
        self._btn = self._button_at(*self.to_content(x, y))

    def _on_drag_update(self, g, ox, oy):
        if self._zooming or self._drag_dead or self._drag_start is None or self._btn:
            return
        dx, _ = self.vec_to_content(ox, oy)
        wf, _ = self.layout()
        _, _, lo, hi = self._drag_start
        df = -dx / wf[2] * (hi - lo)
        self.set_view(lo + df, hi + df)

    def _on_drag_end(self, g, ox, oy):
        st = self._drag_start
        btn, self._btn = self._btn, None
        self._drag_start = None
        if st is None or self._zooming or self._drag_dead:
            self._drag_dead = False
            return
        if btn:
            if self._button_at(*self.to_content(st[0] + ox, st[1] + oy)) == btn:
                self._press_button(btn)
            return
        if math.hypot(ox, oy) < 10:
            self._tap(*self.to_content(st[0], st[1]))

    def _on_zoom_begin(self, g, seq):
        ok, cx, cy = g.get_bounding_box_center()
        x, _ = self.to_content(cx, cy)
        wf, _ = self.layout()
        a = min(max((x - wf[0]) / wf[2], 0), 1)
        self._zoom_start = (self.lo, self.hi, a)
        self._zooming = True
        self._drag_dead = True

    def _on_zoom_changed(self, g, scale):
        lo, hi, a = self._zoom_start
        f = lo + a * (hi - lo)
        span = (hi - lo) / max(scale, 1e-3)
        self.set_view(f - a * span, f + (1 - a) * span)

    def _on_zoom_end(self, g, seq):
        self._zooming = False

    def _on_scroll(self, c, dx, dy):
        a = 0.5
        if self._pointer is not None:
            x, _ = self.to_content(*self._pointer)
            wf, _ = self.layout()
            a = min(max((x - wf[0]) / wf[2], 0), 1)
        if dy:
            self.zoom(1.25 ** (-dy), a)
        elif dx:
            span = self.hi - self.lo
            self.set_view(self.lo + dx * 0.1 * span, self.hi + dx * 0.1 * span)
        return True

    def _tap(self, x, y):
        wf, spec = self.layout()
        if not (wf[0] <= x <= wf[0] + wf[2]):
            return
        if self._dirty or self._img is None:   # image matching the current view/size
            self._dirty = False
            self._render_image(wf)
        img = self._img
        rows = img.rows if img else []
        if not rows:
            return
        tops = img.tops
        if spec and y < wf[1]:
            cand = [0]
            yc = None
        elif wf[1] <= y <= wf[1] + wf[3]:
            yc = y - wf[1]
            heights = np.array([row_height(r) for r in rows])
            cand = np.flatnonzero((tops < yc + SNAP_Y) & (tops + heights > yc - SNAP_Y)).tolist()
            if not cand:
                self.marker = None
                self.queue_draw()
                return
        else:
            return
        # tapping again near the marker removes it
        if self.marker and self._marker_xy() is not None:
            mx, my = self._marker_xy()
            if abs(mx - x) < 14 and (yc is None or abs(my - y) < 14):
                self.marker = None
                self.queue_draw()
                return
        self.marker = self._snap(x - wf[0], yc, cand, img, wf)
        self.queue_draw()

    def _snap(self, x, yc, cand, img, wf):
        """Finds the highest (preferably measured) value near the tap point → (freq, row_id)."""
        rows, ncols, tops, derived = img.rows, img.ncols, img.tops, img.derived
        # in difference rows the largest change counts, otherwise the highest level
        score_src = np.where(img.diff_rows[:, None], np.abs(img.diff), img.filled)
        ww = wf[2]
        px_per_col = ww / ncols
        c_tap = min(ncols - 1, max(0, int(x / px_per_col)))
        dc = int(math.ceil(SNAP_X / px_per_col))
        c0, c1 = max(0, c_tap - dc), min(ncols, c_tap + dc + 1)
        best = None
        cols = np.arange(c0, c1)
        for ri in cand:
            vals = score_src[ri, c0:c1]
            ry = 0 if yc is None else tops[ri] + row_height(rows[ri]) / 2 - yc
            dist = np.hypot((cols + 0.5) * px_per_col - x, ry)
            score = vals - SNAP_DB_PER_PX * dist - SNAP_DERIVED_DB * derived[ri, c0:c1]
            score[np.isnan(vals) | (dist > SNAP_X)] = -np.inf
            k = int(np.argmax(score))
            if np.isfinite(score[k]) and (best is None or score[k] > best[0]):
                best = (score[k], ri, c0 + k)
        span = self.hi - self.lo
        if best is None:
            ri = cand[0]
            return (self.lo + x / ww * span, rows[ri].id)
        _, ri, c = best
        e0 = self.lo + c * span / ncols
        e1 = e0 + span / ncols
        f = rows[ri].peak_freq(e0, e1)
        return (f if f is not None else 0.5 * (e0 + e1), rows[ri].id)

    def _marker_xy(self):
        """Marker position in content coordinates, or None if not visible."""
        img = self._img
        if not self.marker or img is None:
            return None
        wf, _ = self.layout()
        f, rid = self.marker
        rows, tops = img.rows, img.tops
        ri = next((i for i, r in enumerate(rows) if r.id == rid), None)
        if ri is None or not (self.lo <= f <= self.hi) or tops[ri] >= wf[3]:
            return None
        x = wf[0] + (f - self.lo) / (self.hi - self.lo) * wf[2]
        return x, wf[1] + tops[ri] + row_height(rows[ri]) / 2

    # ------------------------------------------------------------------ rendering
    def _render_image(self, wf):
        ncols = max(16, int(min(2048, wf[2] * self.get_scale_factor())))
        nrows = max(1, int(wf[3] // ROW_PX))
        filled, derived, rows = self.history.render(self.lo, self.hi, ncols, nrows)
        if not rows:
            self._img = None
            return
        heights = np.array([row_height(r) for r in rows])
        tops = np.concatenate([[0], np.cumsum(heights)[:-1]])
        n = max(1, int(np.searchsorted(tops, wf[3], side="left")))
        rows, heights, tops = rows[:n], heights[:n], tops[:n]
        filled, derived = filled[:n], derived[:n]
        # difference rows
        edges = np.linspace(self.lo, self.hi, ncols + 1)
        key = (self.lo, self.hi, ncols)
        diff = np.full_like(filled, np.nan)
        diff_rows = np.zeros(n, bool)
        for i, r in enumerate(rows):
            sess = self._diff_session(r)
            if sess is not None:
                diff_rows[i] = True
                diff[i] = filled[i] - self._ref_columns(sess, edges, key)
        if diff_rows.any():
            d = np.abs(diff[diff_rows])
            d = d[~np.isnan(d)]
            if d.size > 50:
                rng = float(np.clip(np.percentile(d[:: max(1, d.size // 20000)], 99.5), 6, 40))
                self.diff_range = rng if self.diff_range is None else \
                    self.diff_range + 0.3 * (rng - self.diff_range)
        if self.diff_range is None:
            self.diff_range = 15.0
        known = filled[~derived & ~diff_rows[:, None]]
        known = known[~np.isnan(known)]
        if known.size > 50:
            sub = known[:: max(1, known.size // 20000)]
            lo, hi = np.percentile(sub, [8, 99.7])
            hi = max(hi, lo + 20)
            if self.levels is None:
                self.levels = (lo, hi)
            else:
                k = 0.3
                self.levels = (self.levels[0] + k * (lo - self.levels[0]),
                               self.levels[1] + k * (hi - self.levels[1]))
        if self.levels is None:
            self.levels = (-60.0, -20.0)
        lo, hi = self.levels
        nan = np.isnan(filled)
        idx = np.clip((np.nan_to_num(filled, nan=lo) - lo) * (255.0 / (hi - lo)), 0, 255)
        idx = idx.astype(np.int16) + derived.astype(np.int16) * LUT_DERIVED
        idx[nan] = LUT_UNKNOWN
        if diff_rows.any():
            D = self.diff_range
            dv = diff[diff_rows]
            di = np.clip((np.nan_to_num(dv, nan=0.0) + D) * (255.0 / (2 * D)), 0, 255)
            di = di.astype(np.int16) + LUT_DIFF + derived[diff_rows].astype(np.int16) * 256
            di[np.isnan(dv)] = LUT_UNKNOWN
            idx[diff_rows] = di
        rgba = np.repeat(LUT[idx], heights, axis=0)
        h, w = rgba.shape[:2]
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8,
                                    GLib.Bytes.new(rgba.tobytes()), w * 4)
        self._img = SimpleNamespace(tex=tex, filled=filled, derived=derived, rows=rows,
                                    ncols=ncols, tops=tops, h=h, diff=diff, diff_rows=diff_rows)

    def do_snapshot(self, snap):
        W, H = self._size()
        snap.append_color(_rgba(0.07, 0.07, 0.09), _rect(0, 0, W, H))
        snap.save()
        if self.rotation == 90:
            snap.translate(Graphene.Point().init(W, 0))
            snap.rotate(90)
        elif self.rotation == 270:
            snap.translate(Graphene.Point().init(0, H))
            snap.rotate(270)
        self._draw(snap)
        snap.restore()

    def _text(self, snap, x, y, text, color=None, align="l", bg=None):
        lay = self.create_pango_layout(text)
        lay.set_font_description(self._font)
        _, log = lay.get_pixel_extents()
        tw, th = log.width, log.height
        if align == "c":
            x -= tw / 2
        elif align == "r":
            x -= tw
        if bg is not None:
            snap.append_color(bg, _rect(x - 3, y - 1, tw + 6, th + 2))
        snap.save()
        snap.translate(Graphene.Point().init(x, y))
        snap.append_layout(lay, color or _rgba(0.85, 0.85, 0.88))
        snap.restore()
        return tw, th

    def _draw(self, snap):
        cw, ch = self.content_size()
        wf, spec = self.layout()
        x0, y0, ww, wh = wf
        if self._dirty or self._img is None:
            self._dirty = False
            self._render_image(wf)
        img = self._img
        snap.append_color(_rgba(0.1, 0.1, 0.12), _rect(x0, y0, ww, wh))
        if img is None:
            self._text(snap, x0 + ww / 2, y0 + wh / 2 - 10, self.status, align="c")
            self._draw_freq_axis(snap, wf, ch)
            self._draw_buttons(snap)
            return
        snap.push_clip(_rect(x0, y0, ww, wh))
        snap.append_scaled_texture(img.tex, Gsk.ScalingFilter.NEAREST, _rect(x0, y0, ww, img.h))
        snap.pop()
        self._draw_time_axis(snap, wf, img.rows, img.tops)
        self._draw_freq_axis(snap, wf, ch)
        if spec:
            self._draw_spectrum(snap, spec, img)
        self._draw_marker(snap, wf, spec, img)
        self._draw_buttons(snap)

    def _draw_buttons(self, snap):
        font = Pango.FontDescription.from_string("Sans Bold 15")
        labels = {"zoom_in": "+", "zoom_out": "−", "diff": "Δ"}
        for name, (cx, cy) in self._buttons().items():
            active = name == "diff" and self.diff_active
            pb = Gsk.PathBuilder.new()
            pb.add_circle(Graphene.Point().init(cx, cy), BTN_R)
            path = pb.to_path()
            fill = _rgba(0.1, 0.45, 0.2, 0.9) if active else _rgba(0.05, 0.05, 0.07, 0.72)
            snap.append_fill(path, Gsk.FillRule.WINDING, fill)
            snap.append_stroke(path, Gsk.Stroke.new(1.0), _rgba(1, 1, 1, 0.25))
            lay = self.create_pango_layout(labels[name])
            lay.set_font_description(font)
            _, log = lay.get_pixel_extents()
            snap.save()
            snap.translate(Graphene.Point().init(cx - log.width / 2, cy - log.height / 2))
            snap.append_layout(lay, _rgba(1, 1, 1, 0.95))
            snap.restore()

    def _draw_time_axis(self, snap, wf, rows, tops):
        x0, y0, ww, wh = wf
        col = _rgba(0.75, 0.75, 0.8)
        last_y = -100
        last_lbl = None
        for r, top in zip(rows, tops.tolist()):
            y = y0 + top
            if y - last_y < 34 or y > y0 + wh - 12:
                continue
            lbl = time.strftime("%H:%M:%S", time.localtime(r.t0))
            if lbl == last_lbl:
                continue
            snap.append_color(col, _rect(x0 - 4, y, 4, 1))
            self._text(snap, x0 - 6, max(y - 6, y0), lbl, col, align="r")
            last_y, last_lbl = y, lbl

    def _nice_step(self, span, px, min_px=64):
        raw = span * min_px / max(px, 1)
        e = 10 ** math.floor(math.log10(raw))
        for m in (1, 2, 2.5, 5, 10):
            if m * e >= raw:
                return m * e
        return 10 * e

    def _draw_freq_axis(self, snap, wf, ch):
        x0, y0, ww, wh = wf
        span = self.hi - self.lo
        fx = lambda f: x0 + (f - self.lo) / span * ww  # noqa: E731
        ya = y0 + wh
        snap.append_color(_rgba(0.5, 0.5, 0.55), _rect(x0, ya, ww, 1))
        # band strip
        yb = ya + 1
        occupied = []
        for f0, f1, name, rgb in BANDS:
            if f1 < self.lo or f0 > self.hi:
                continue
            a, b = max(fx(f0), x0), min(fx(f1), x0 + ww)
            snap.append_color(_rgba(*rgb, 0.75), _rect(a, yb, max(b - a, 1.5), 4))
        # ticks + labels
        step = self._nice_step(span, ww)
        dec = max(0, -math.floor(math.log10(step / 1e6) + 1e-9))
        f = math.ceil(self.lo / step) * step
        col = _rgba(0.8, 0.8, 0.85)
        while f <= self.hi + 1e-3:
            x = fx(f)
            snap.append_color(_rgba(1, 1, 1, 0.07), _rect(x, y0, 1, wh))
            snap.append_color(col, _rect(x, yb + 4, 1, 4))
            tw, _ = self._text(snap, x, yb + 7, fmt_mhz(f, dec), col, align="c")
            occupied.append((x - tw / 2 - 4, x + tw / 2 + 4))
            f += step
        # band names / notable points in a second line, where there is room
        yl = yb + 21
        labels = []
        for f0, f1, name, rgb in BANDS:
            if f1 < self.lo or f0 > self.hi:
                continue
            a, b = max(fx(f0), x0), min(fx(f1), x0 + ww)
            labels.append(((a + b) / 2, name, _rgba(*rgb), b - a))
        for fp, name in POINTS:
            if self.lo <= fp <= self.hi:
                x = fx(fp)
                snap.append_color(_rgba(1, 0.9, 0.5, 0.9), _rect(x - 0.5, yb, 1.5, 8))
                labels.append((x, name, _rgba(1, 0.9, 0.5), 0))
        labels.sort(key=lambda l: -l[3])        # wide bands first
        used = []
        for x, name, c, width in labels:
            lay = self.create_pango_layout(name)
            lay.set_font_description(self._font)
            tw = lay.get_pixel_extents()[1].width
            a, b = x - tw / 2 - 3, x + tw / 2 + 3
            if a < x0 - AXIS_L + 2 or b > x0 + ww + 2:
                continue
            if any(not (b < u0 or a > u1) for u0, u1 in used):
                continue
            used.append((a, b))
            self._text(snap, x, yl, name, c, align="c")

    def _draw_spectrum(self, snap, spec, img):
        sx, sy, sw, sh = spec
        ncols = img.ncols
        derived = img.derived[0]
        snap.append_color(_rgba(0.1, 0.1, 0.12), _rect(sx, sy, sw, sh))
        # box around the frequency range being measured right now
        hop = self.current_hop
        if hop and time.time() - hop[2] < HOP_SHOW_S and hop[1] > self.lo and hop[0] < self.hi:
            span = self.hi - self.lo
            a = sx + max(0.0, (hop[0] - self.lo) / span) * sw
            b = sx + min(1.0, (hop[1] - self.lo) / span) * sw
            if b - a < 4:
                a, b = (a + b) / 2 - 2, (a + b) / 2 + 2
            snap.append_color(_rgba(0.0, 0.32, 0.1, 0.55), _rect(a, sy, b - a, sh))
            for r in (_rect(a, sy, b - a, 1), _rect(a, sy + sh - 1, b - a, 1),
                      _rect(a, sy, 1, sh), _rect(b - 1, sy, 1, sh)):
                snap.append_color(_rgba(0.1, 0.6, 0.25, 0.95), r)
        if img.diff_rows[0]:
            vals = img.diff[0]
            lo, hi = -self.diff_range - 2, self.diff_range + 2
            unit, sign = "dB Δ", True
        else:
            vals = img.filled[0]
            lo, hi = self.levels
            lo -= 5
            hi += 5
            unit, sign = "dB", False
        step = 10 if (hi - lo) * 14 < sh * 10 else 20
        if sign and hi - lo < 30:
            step = 5
        d = math.floor(hi / step) * step
        first = True
        while d > lo:
            y = sy + sh * (1 - (d - lo) / (hi - lo))
            snap.append_color(_rgba(1, 1, 1, 0.25 if sign and d == 0 else 0.08), _rect(sx, y, sw, 1))
            if y < sy + sh - 10:
                num = f"{d:+.0f}" if sign and d else f"{d:.0f}"
                txt = f"{num} {unit}" if first else num
                self._text(snap, sx - 6, y - 6, txt.replace("-", "−"), _rgba(0.6, 0.6, 0.65), align="r")
                first = False
            d -= step
        xs = sx + (np.arange(ncols) + 0.5) * (sw / ncols)
        ys = sy + sh * (1 - (np.clip(vals, lo, hi) - lo) / (hi - lo))
        for want_derived, color in ((False, _rgba(0.95, 0.85, 0.3)), (True, _rgba(0.55, 0.55, 0.6))):
            pb = Gsk.PathBuilder.new()
            pen = False
            any_seg = False
            for x, y, dv in zip(xs.tolist(), ys.tolist(), derived.tolist()):
                if dv != want_derived or y != y:
                    pen = False
                    continue
                if pen:
                    pb.line_to(x, y)
                else:
                    pb.move_to(x, y)
                    pen = True
                    any_seg = True
            if any_seg:
                snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.0), color)

    def _draw_marker(self, snap, wf, spec, img):
        if not self.marker:
            return
        x0, y0, ww, wh = wf
        filled, derived, rows, ncols = img.filled, img.derived, img.rows, img.ncols
        f, rid = self.marker
        ri = next((i for i, r in enumerate(rows) if r.id == rid), None)
        if ri is None:
            if not any(r.id == rid for r in self.history.rows):
                self.marker = None           # row dropped out of the history
            return
        pos = self._marker_xy()
        if pos is None:                      # outside the visible area → do not draw
            return
        x, y = pos
        span = self.hi - self.lo
        col = min(ncols - 1, max(0, int((f - self.lo) / span * ncols)))
        der = derived[ri, col]
        v = rows[ri].value_at(f)
        if v is None or v != v:
            v = filled[ri, col]
        top = spec[1] if spec else y0
        snap.append_color(_rgba(1, 1, 1, 0.6), _rect(x, top, 1, y0 + wh - top))
        snap.append_color(_rgba(1, 1, 1, 0.6), _rect(x0, y, ww, 1))
        snap.append_color(_rgba(1, 1, 1, 1), _rect(x - 3, y - 3, 7, 7))
        snap.append_color(_rgba(0, 0, 0, 1), _rect(x - 1.5, y - 1.5, 4, 4))
        dec = 3 if span > 2e6 else 4
        amp = "–" if v != v else f"{v:.1f} dBFS".replace("-", "−")
        txt = f"{fmt_mhz(f, dec)} MHz   {amp}   " + \
            time.strftime("%H:%M:%S", time.localtime(rows[ri].t0))
        if img.diff_rows[ri]:
            ref = filled[ri, col] - img.diff[ri, col]      # reference level of this column
            if ref == ref and v == v:
                txt += f"   Δ {v - ref:+.1f} dB".replace("-", "−")
        if der:
            txt += "  (derived)"
        # readout next to the marker line (below it in the upper half, above it in the lower half),
        # so it does not cover the newest sweeps
        ty = y + 6 if y < y0 + wh / 2 else y - 22
        self._text(snap, x0 + 6, ty, txt, _rgba(1, 1, 1), bg=_rgba(0, 0, 0, 0.7))
