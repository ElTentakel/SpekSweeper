"""Wasserfall-Widget: Spektrum oben, Wasserfall, Zeitachse links, Frequenzachse + Bänder unten.

Gezeichnet wird in einem „Inhalts“-Koordinatensystem, das bei Bedarf um 90°/270° gedreht wird
(Bedienung quer, auch wenn das System selbst nicht dreht).
"""
import math
import time

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .bands import BANDS, POINTS  # noqa: E402

MIN_SPAN = 200e3
ROW_PX = 2            # Mindesthöhe einer Sweep-Zeile (logische Pixel)
ROW_MAX = 16          # Höchsthöhe (lange Sweeps über den Gesamtbereich)
AXIS_L = 52           # Zeitachse
AXIS_B = 40           # Frequenzachse + Bandleiste
FONT = "Sans 8"
SNAP_X = 16           # Marker rastet im Umkreis von ± so vielen Pixeln auf den höchsten Wert ein …
SNAP_Y = 8
SNAP_DB_PER_PX = 0.15  # … wobei weiter entfernte Punkte etwas abgewertet werden
SNAP_DERIVED_DB = 3    # und abgeleitete (graue) Werte leicht hinter gemessenen zurückstehen


def _rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def _make_lut():
    stops = [(0.0, (2, 4, 22)), (0.2, (8, 30, 110)), (0.4, (0, 120, 200)),
             (0.55, (40, 190, 150)), (0.7, (220, 220, 40)), (0.85, (245, 110, 20)),
             (1.0, (255, 245, 235))]
    x = np.linspace(0, 1, 256)
    pos = [s[0] for s in stops]
    lut = np.zeros((256, 4), np.uint8)
    for ch in range(3):
        lut[:, ch] = np.interp(x, pos, [s[1][ch] for s in stops])
    lut[:, 3] = 255
    # „leicht ausgegraut“: entsättigt und etwas zum Mittelgrau gezogen
    f = lut[:, :3].astype(np.float32)
    grey = (f @ np.array([0.3, 0.59, 0.11], np.float32))[:, None]
    d = (0.35 * f + 0.65 * grey) * 0.7 + 28
    dl = np.empty_like(lut)
    dl[:, :3] = np.clip(d, 0, 255)
    dl[:, 3] = 255
    unknown = np.array([[24, 24, 28, 255]], np.uint8)
    return np.vstack([lut, dl, unknown])


LUT = _make_lut()


def row_height(r):
    """Zeilenhöhe wächst mit der Sweep-Dauer (Wurzel), damit lange Sweeps sichtbar bleiben."""
    dur = max(r.t1 - r.t0, 0.0)
    return int(min(ROW_MAX, max(ROW_PX, round(ROW_PX * math.sqrt(dur / 0.5)))))


def fmt_mhz(f, decimals):
    return f"{f / 1e6:.{decimals}f}".replace(".", ",")


def fmt_span(span):
    if span >= 1e6:
        return f"{span / 1e6:.3g} MHz".replace(".", ",")
    return f"{span / 1e3:.3g} kHz".replace(".", ",")


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
        self.status = "Warte auf Stick …"
        self.levels = None                 # (lo_dB, hi_dB), geglättet
        self._dirty = True
        self._img = None                   # (tex, filled, derived, rows, ncols, key)
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
        self.force_size = None             # (w, h) für Tests ohne Fenster
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: setattr(self, "_pointer", (x, y)))
        self.add_controller(motion)

    # ------------------------------------------------------------------ Geometrie
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

    # ------------------------------------------------------------------ Ansicht
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

    # ------------------------------------------------------------------ Eingabe
    def _on_drag_begin(self, g, x, y):
        self._drag_start = (x, y, self.lo, self.hi)
        self._drag_dead = self._zooming

    def _on_drag_update(self, g, ox, oy):
        if self._zooming or self._drag_dead or self._drag_start is None:
            return
        dx, _ = self.vec_to_content(ox, oy)
        wf, _ = self.layout()
        _, _, lo, hi = self._drag_start
        df = -dx / wf[2] * (hi - lo)
        self.set_view(lo + df, hi + df)

    def _on_drag_end(self, g, ox, oy):
        st = self._drag_start
        self._drag_start = None
        if st is None or self._zooming or self._drag_dead:
            self._drag_dead = False
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
        if self._dirty or self._img is None:   # Bild passend zur aktuellen Ansicht/Größe
            self._dirty = False
            self._render_image(wf)
        img = self._img
        rows = img[3] if img else []
        if not rows:
            return
        tops = img[5]
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
        # erneutes Tippen nahe am Marker entfernt ihn
        if self.marker and self._marker_xy() is not None:
            mx, my = self._marker_xy()
            if abs(mx - x) < 14 and (yc is None or abs(my - y) < 14):
                self.marker = None
                self.queue_draw()
                return
        self.marker = self._snap(x - wf[0], yc, cand, img, wf)
        self.queue_draw()

    def _snap(self, x, yc, cand, img, wf):
        """Sucht in der Nähe des Tippunkts den höchsten (gemessenen) Wert → (freq, row_id)."""
        _, filled, derived, rows, ncols, tops, _ = img
        ww = wf[2]
        px_per_col = ww / ncols
        c_tap = min(ncols - 1, max(0, int(x / px_per_col)))
        dc = int(math.ceil(SNAP_X / px_per_col))
        c0, c1 = max(0, c_tap - dc), min(ncols, c_tap + dc + 1)
        best = None
        cols = np.arange(c0, c1)
        for ri in cand:
            vals = filled[ri, c0:c1]
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
        """Marker-Position in Inhaltskoordinaten oder None, wenn nicht sichtbar."""
        img = self._img
        if not self.marker or img is None:
            return None
        wf, _ = self.layout()
        f, rid = self.marker
        rows, tops = img[3], img[5]
        ri = next((i for i, r in enumerate(rows) if r.id == rid), None)
        if ri is None or not (self.lo <= f <= self.hi) or tops[ri] >= wf[3]:
            return None
        x = wf[0] + (f - self.lo) / (self.hi - self.lo) * wf[2]
        return x, wf[1] + tops[ri] + row_height(rows[ri]) / 2

    # ------------------------------------------------------------------ Rendern
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
        known = filled[~derived]
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
        idx = idx.astype(np.int16) + derived.astype(np.int16) * 256
        idx[nan] = 512
        rgba = np.repeat(LUT[idx], heights, axis=0)
        h, w = rgba.shape[:2]
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8,
                                    GLib.Bytes.new(rgba.tobytes()), w * 4)
        self._img = (tex, filled, derived, rows, ncols, tops, h)

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
            return
        tex, filled, derived, rows, ncols, tops, img_h = img
        snap.push_clip(_rect(x0, y0, ww, wh))
        snap.append_scaled_texture(tex, Gsk.ScalingFilter.NEAREST, _rect(x0, y0, ww, img_h))
        snap.pop()
        self._draw_time_axis(snap, wf, rows, tops)
        self._draw_freq_axis(snap, wf, ch)
        if spec:
            self._draw_spectrum(snap, spec, filled[0], derived[0], ncols)
        self._draw_marker(snap, wf, spec, img)

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
        # Bandleiste
        yb = ya + 1
        occupied = []
        for f0, f1, name, rgb in BANDS:
            if f1 < self.lo or f0 > self.hi:
                continue
            a, b = max(fx(f0), x0), min(fx(f1), x0 + ww)
            snap.append_color(_rgba(*rgb, 0.75), _rect(a, yb, max(b - a, 1.5), 4))
        # Ticks + Beschriftung
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
        # Bandnamen / markante Punkte in eine zweite Zeile, wenn Platz
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
        labels.sort(key=lambda l: -l[3])        # breite Bänder zuerst
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

    def _draw_spectrum(self, snap, spec, vals, derived, ncols):
        sx, sy, sw, sh = spec
        snap.append_color(_rgba(0.1, 0.1, 0.12), _rect(sx, sy, sw, sh))
        lo, hi = self.levels
        lo -= 5
        hi += 5
        step = 10 if (hi - lo) * 14 < sh * 10 else 20
        d = math.floor(hi / step) * step
        first = True
        while d > lo:
            y = sy + sh * (1 - (d - lo) / (hi - lo))
            snap.append_color(_rgba(1, 1, 1, 0.08), _rect(sx, y, sw, 1))
            if y < sy + sh - 10:
                txt = f"{d:.0f} dB" if first else f"{d:.0f}"
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
        tex, filled, derived, rows, ncols, tops, img_h = img
        f, rid = self.marker
        ri = next((i for i, r in enumerate(rows) if r.id == rid), None)
        if ri is None:
            if not any(r.id == rid for r in self.history.rows):
                self.marker = None           # Zeile aus dem Verlauf gefallen
            return
        pos = self._marker_xy()
        if pos is None:                      # außerhalb des sichtbaren Bereichs → nicht zeichnen
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
        amp = "–" if v != v else f"{v:.1f} dBFS".replace(".", ",").replace("-", "−")
        txt = f"{fmt_mhz(f, dec)} MHz   {amp}   " + \
            time.strftime("%H:%M:%S", time.localtime(rows[ri].t0))
        if der:
            txt += "  (abgeleitet)"
        self._text(snap, x0 + 6, y0 + 4, txt, _rgba(1, 1, 1), bg=_rgba(0, 0, 0, 0.7))
