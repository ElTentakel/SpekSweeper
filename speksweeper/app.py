"""Hauptprogramm: Adw-Anwendung mit Wasserfall, Kopfleiste und Sweeper-Thread."""
import json
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .store import History  # noqa: E402
from .sweeper import Sweeper  # noqa: E402
from .view import WaterfallView, fmt_mhz, fmt_span  # noqa: E402

APP_ID = "io.github.speksweeper.SpekSweeper"
CONFIG = os.path.join(GLib.get_user_config_dir(), "speksweeper.json")

CSS = b"""
.osd-zoom button { min-width: 48px; min-height: 48px; }
"""


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


class Window(Adw.ApplicationWindow):
    def __init__(self, app, simulate):
        super().__init__(application=app, title="SpekSweeper")
        self.set_default_size(360, 720)
        self.cfg = load_config()
        self.history = History()
        self.view = WaterfallView(self.history)
        self.view.connect("view-changed", self._on_view_changed)
        self._req_timer = 0

        # Kopfleiste
        self.wtitle = Adw.WindowTitle(title="SpekSweeper", subtitle="")
        hb = Adw.HeaderBar(title_widget=self.wtitle)
        self.pause_btn = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic",
                                          tooltip_text="Sweep anhalten")
        self.pause_btn.connect("toggled", self._on_pause)
        hb.pack_start(self.pause_btn)
        full = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="Gesamter Bereich")
        full.connect("clicked", lambda b: self.view.full())
        hb.pack_start(full)

        self.menu = Gio.Menu()
        self.gain_menu = Gio.Menu()
        self.menu.append_submenu("Verstärkung", self.gain_menu)
        self.menu.append("Verlauf löschen", "win.clear")
        self.menu.append("Über SpekSweeper", "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self.menu))
        rot = Gtk.Button(icon_name="object-rotate-right-symbolic",
                         tooltip_text="Anzeige drehen (Hoch-/Querformat)")
        rot.connect("clicked", self._on_rotate)
        hb.pack_end(rot)

        # Zoom-Knöpfe als OSD über dem Wasserfall
        overlay = Gtk.Overlay(child=self.view)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      halign=Gtk.Align.END, valign=Gtk.Align.END,
                      margin_end=10, margin_bottom=56)
        box.add_css_class("osd-zoom")
        for icon, fac in (("zoom-in-symbolic", 2.0), ("zoom-out-symbolic", 0.5)):
            b = Gtk.Button(icon_name=icon)
            b.add_css_class("osd")
            b.add_css_class("circular")
            b.connect("clicked", lambda _b, f=fac: self.view.zoom(f))
            box.append(b)
        overlay.add_overlay(box)

        tv = Adw.ToolbarView(content=overlay)
        tv.add_top_bar(hb)
        self.set_content(tv)

        # Aktionen
        gain = self.cfg.get("gain", 19.7)
        self.gain_action = Gio.SimpleAction.new_stateful(
            "gain", GLib.VariantType.new("s"), GLib.Variant("s", "auto" if gain is None else str(gain)))
        self.gain_action.connect("activate", self._on_gain)
        self.add_action(self.gain_action)
        for name, cb in (("clear", self._on_clear), ("about", self._on_about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

        # Sweeper
        self.sweeper = Sweeper(self._from_thread, simulate=simulate)
        self.sweeper.gain = gain
        lo, hi = self.cfg.get("view", (None, None))
        if lo and hi:
            self.view.lo, self.view.hi = lo, hi
        self.view.rotation = self.cfg.get("rotation", 0)
        self.sweeper.start()
        self.connect("close-request", self._on_close)
        self._update_title()
        GLib.idle_add(self._request_sweep)
        shot = os.environ.get("SPEKSWEEPER_SHOT")       # Debug: "pfad.png:sekunden"
        if shot:
            path, _, sec = shot.partition(":")
            GLib.timeout_add_seconds(int(sec or 5), self._screenshot, path)

    def _screenshot(self, path):
        paint = Gtk.WidgetPaintable.new(self)
        w, h = paint.get_intrinsic_width(), paint.get_intrinsic_height()
        snap = Gtk.Snapshot()
        paint.snapshot(snap, w, h)
        node = snap.to_node()
        if node is None:
            return True            # noch nichts gezeichnet → später nochmal
        self.get_renderer().render_texture(node, None).save_to_png(path)
        print("Screenshot:", path, w, h, flush=True)
        return False

    # --- Sweeper-Ereignisse (Thread → GUI) ---
    def _from_thread(self, kind, data):
        GLib.idle_add(self._on_event, kind, data)

    def _on_event(self, kind, data):
        if kind == "hop":
            rid, first, db, pool, t = data
            self.history.add_hop(rid, first, db, pool, t)
            self.view.data_changed()
        elif kind == "sweep_start":
            self.history.start(data["id"], data["t"], data["f_lo"], data["bin_w"], data["n"])
        elif kind == "sweep_end":
            self.history.finish(*data)
            self.view.data_changed()
        elif kind == "device":
            self.view.status = f"{data['name']} · {data['tuner']}"
            self._fill_gain_menu(data["gains"])
            self.view.set_range(*data["range"])
            self._update_title()
            self._request_sweep()
        elif kind == "status":
            self.view.status = data
            self.wtitle.set_subtitle(data[:60])
            self.view.data_changed()
        return False

    def _fill_gain_menu(self, gains):
        self.gain_menu.remove_all()
        self.gain_menu.append("Automatisch (AGC)", "win.gain::auto")
        for g in gains:
            self.gain_menu.append(f"{g:.1f} dB".replace(".", ","), f"win.gain::{g}")

    # --- Ansicht ---
    def _on_view_changed(self, view):
        self._update_title()
        if self._req_timer:
            GLib.source_remove(self._req_timer)
        self._req_timer = GLib.timeout_add(350, self._request_sweep)

    def _request_sweep(self):
        self._req_timer = 0
        self.sweeper.request(self.view.lo, self.view.hi, max(256, self.view.target_bins()))
        return False

    def _update_title(self):
        lo, hi = self.view.lo, self.view.hi
        dec = 0 if hi - lo > 50e6 else (1 if hi - lo > 5e6 else 3)
        self.wtitle.set_subtitle(f"{fmt_mhz(lo, dec)} – {fmt_mhz(hi, dec)} MHz · {fmt_span(hi - lo)}")

    def _on_rotate(self, b):
        nxt = {0: 90, 90: 270, 270: 0}[self.view.rotation]
        self.view.set_rotation(nxt)

    def _on_pause(self, b):
        p = b.get_active()
        b.set_icon_name("media-playback-start-symbolic" if p else "media-playback-pause-symbolic")
        self.sweeper.set_paused(p)

    def _on_gain(self, action, param):
        action.set_state(param)
        s = param.get_string()
        self.sweeper.set_gain(None if s == "auto" else float(s))

    def _on_clear(self, *a):
        self.history.rows.clear()
        self.history.by_id.clear()
        self.view.marker = None
        self.view.levels = None
        self.view.data_changed()

    def _on_about(self, *a):
        Adw.AboutDialog(application_name="SpekSweeper", version="0.1.0",
                        comments="Wasserfall-Übersicht über den gesamten Empfangsbereich eines "
                                 "RTL-SDR-Sticks.\n\nZiehen: verschieben · Zwei Finger: zoomen · "
                                 "Tippen: Frequenz und Pegel. Grau getönte Bereiche sind aus "
                                 "zeitlich benachbarten Messungen abgeleitet.",
                        application_icon="network-wireless-symbolic").present(self)

    def _on_close(self, *a):
        st = self.gain_action.get_state().get_string()
        self.cfg.update(view=[self.view.lo, self.view.hi], rotation=self.view.rotation,
                        gain=None if st == "auto" else float(st))
        save_config(self.cfg)
        self.sweeper.stop()
        return False


class App(Adw.Application):
    def __init__(self, simulate=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.simulate = simulate

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Window(self, self.simulate).present()


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    sim = "--sim" in argv
    if sim:
        argv.remove("--sim")
    return App(simulate=sim).run(argv)
