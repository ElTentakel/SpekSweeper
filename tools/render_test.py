#!/usr/bin/env python3
"""Rendert das Wasserfall-Widget ohne sichtbares Fenster in PNG-Dateien (Test/Screenshots).

Aufruf: render_test.py [--sim] [sekunden] [ausgabe-präfix]
Sweept erst ganz, dann gezoomt (DVB-T/UKW), damit abgeleitete Bereiche entstehen.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gsk, Gtk  # noqa: E402

from speksweeper.store import History  # noqa: E402
from speksweeper.sweeper import Sweeper  # noqa: E402
from speksweeper.view import WaterfallView  # noqa: E402

args = [a for a in sys.argv[1:] if a != "--sim"]
sim = "--sim" in sys.argv
secs = float(args[0]) if args else 20
prefix = args[1] if len(args) > 1 else "/tmp/wf"

Gtk.init()
hist = History()
view = WaterfallView(hist)
ev = []
sw = Sweeper(lambda k, d: ev.append((k, d)), simulate=sim)
sw.request(*view.range, 1024)
sw.start()


def pump(dur):
    end = time.time() + dur
    while time.time() < end:
        time.sleep(0.05)
        while ev:
            k, d = ev.pop(0)
            if k == "sweep_start":
                hist.start(d["id"], d["t"], d["f_lo"], d["bin_w"], d["n"])
            elif k == "hop":
                hist.add_hop(*d)
            elif k == "sweep_end":
                hist.finish(*d)
            elif k == "device":
                view.range = d["range"]
                print("Gerät:", d["name"], d["tuner"], d["range"])
            elif k == "status":
                print("Status:", d)


renderer = Gsk.CairoRenderer.new()
renderer.realize_for_display(Gdk.Display.get_default())


def shot(name, w, h, rot=0):
    view.force_size = (w, h)
    view.rotation = rot
    view._dirty = True
    snap = Gtk.Snapshot()
    t = time.time()
    view.do_snapshot(snap)
    dt = time.time() - t
    node = snap.to_node()
    renderer.render_texture(node, None).save_to_png(f"{prefix}_{name}.png")
    print(f"{name}: {w}x{h} rot={rot} snapshot {dt * 1000:.0f} ms")


pump(secs)
view.lo, view.hi = view.range
shot("full", 360, 650)
lo, hi = 440e6, 720e6
view.lo, view.hi = lo, hi
sw.request(lo, hi, 1024)
pump(secs / 2)
view.lo, view.hi = 400e6, 680e6       # verschoben → links abgeleitete Bereiche
shot("dvbt_portrait", 360, 650)
shot("dvbt_landscape", 720, 300)
shot("dvbt_rot90", 360, 650, 90)
view.force_size = (360, 650)
view.rotation = 0
view._dirty = True
wf, _ = view.layout()
for f_tap in (430e6, 512e6):                 # Tippen neben ISM 433 bzw. neben einem DVB-T-Mux
    x = wf[0] + (f_tap - view.lo) / (view.hi - view.lo) * wf[2]
    view.marker = None
    view._tap(x, wf[1] + 3)
    print(f"Tippen bei {f_tap / 1e6:.2f} MHz → Marker {view.marker[0] / 1e6:.4f} MHz")
    shot(f"marker_{f_tap / 1e6:.0f}", 360, 650)
view.lo, view.hi = 600e6, 700e6          # Marker außerhalb → darf nicht erscheinen
shot("marker_hidden", 360, 650)
sw.stop()
