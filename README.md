# SpekSweeper

Waterfall overview of the full receive range of an RTL-SDR stick (DVB-T stick) – as a touch app
for **Phosh** on a Linux phone (developed on a Xiaomi Mi Mix 2S running postmarketOS/Nura).
Python + GTK4/libadwaita + numpy, talking to `librtlsdr` directly via ctypes.

![SpekSweeper in landscape on the Mi Mix 2S: full range 22–1100 MHz with FM, DAB+, 70 cm,
DVB-T2 and LTE/GSM activity; the green box marks the range being measured](docs/screenshot-landscape.png)

The goal is not a measuring instrument but a quick overview: where is something going on, and
when was it active? There is no number entry – just panning, zooming and tapping.

## Features

- **Waterfall** across the whole tuner range (FC0013: 22–1100 MHz, R820T/R828D: 24–1766 MHz),
  newest sweep on top, with a spectrum strip of the current sweep above it.
- **Frequency axis at the bottom** with round MHz ticks, a coloured band strip (FM, airband, DAB+,
  TETRA, 70 cm, DVB-T2, LTE, SRD 868 …) and notable single frequencies (ADS-B, ISM 433, AIS,
  ISS, …).
- **Time axis on the left** (hh:mm:ss).
- **The sweep follows the view**: a smaller range is scanned faster and at higher resolution.
- **Derived data**: whatever a sweep did not measure (because the view was panned/zoomed before,
  or the sweep is still running) is taken from the temporally nearest measurement of the same
  frequency and shown **slightly greyed out**.
- **Live indicator**: a dark green box in the spectrum strip marks the frequency range that is
  being measured right now.
- **Difference mode** (Δ button): the current sweep becomes the reference; from the next sweep on,
  the waterfall and spectrum show only the change against it (blue = weaker, dark = unchanged,
  yellow/red = stronger). Sweeps from before keep their normal display.
- **Marker** by tapping: frequency, level (dBFS) and time – in difference mode also the difference.
- **Portrait and landscape**, plus a rotate button (0°/90°/270°) for when the system does not
  rotate automatically.
- Automatic colour scale, selectable gain (AGC or a fixed step), pause.
- View, rotation and gain are stored in `~/.config/speksweeper.json`.

## Usage

| Gesture | Effect |
|---|---|
| Drag horizontally | Pan the frequency range |
| Pinch with two fingers | Frequency zoom around the centre of the fingers |
| + / − buttons bottom right / mouse wheel | Zoom (mouse wheel around the pointer) |
| Δ button (above + / −) | Switch difference mode on/off (green while active) |
| Tap | Set marker – snaps to the highest value within ±16 px |
| Tap the marker | Remove marker |

Header bar: pause · full range · rotate · menu (gain, clear history, about).
The Δ/+/− buttons are drawn inside the waterfall, so they rotate together with it.

The marker prefers nearby points (−0.15 dB per pixel of distance) and measured over derived
values (−3 dB). Outside the view it is hidden and reappears when panning back; once its row
drops out of the history it is removed.

## Requirements

- Linux with GTK ≥ 4.14 (uses `Gsk.PathBuilder`, `realize_for_display`), libadwaita ≥ 1.5,
  Python 3 with PyGObject and numpy, `librtlsdr`.
  On postmarketOS: `sudo apk add rtl-sdr py3-numpy py3-gobject3 libadwaita`
- No DVB kernel driver bound to the stick (otherwise blacklist `dvb_usb_rtl28xxu`).
- Access without root via the udev rule shipped with `rtl-sdr` (group `plugdev`; the user must be
  a member). If the stick was plugged in before the rule was loaded: `sudo udevadm trigger`.
- On the Mi Mix 2S: switch the USB port to host mode (`sudo usb-host-mode on`) and run the stick
  through a powered hub – the phone does not supply 5 V.

## Installation

    ./deploy.sh user@host

copies the app via `tar` over ssh to `~/.local/share/speksweeper` and creates the launcher
"SpekSweeper". To start directly:

    ./speksweeper-launch             # with stick
    ./speksweeper-launch --sim       # simulated signals (FM, DAB, DVB-T, LTE, …), no stick needed

Starting on the phone over ssh:
`XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 ~/.local/share/speksweeper/speksweeper-launch`

## Testing

    python3 tools/render_test.py [--sim] [seconds] [prefix]    # e.g. 60 /tmp/wf

Sweeps the full range, then zooms to 440–720 MHz, pans the view, places markers by simulated
taps and writes PNGs (portrait/landscape, rotated, markers). The widget is rendered without a
visible window, so this also works while the phone is locked (Phosh does not draw windows then).

Screenshot of the running window (only with an unlocked screen):
`SPEKSWEEPER_SHOT=/tmp/x.png:20 ./speksweeper-launch`

## Structure

| File | Contents |
|---|---|
| `speksweeper/rtlsdr.py` | ctypes binding for `librtlsdr` (open, tuner/gain, `read_sync`) |
| `speksweeper/sweeper.py` | Hop plan, FFT, sweeper thread, `SimDevice` for testing without a stick |
| `speksweeper/store.py` | Sweep history, mapping to pixel columns, derivation of missing values |
| `speksweeper/view.py` | Waterfall widget: GSK snapshot, gestures, axes, bands, marker, rotation |
| `speksweeper/bands.py` | Bands and notable frequencies for the axis labels |
| `speksweeper/app.py` | Adw application, header bar, menu, settings |
| `speksweeper-launch` | Start script |
| `io.github.speksweeper.SpekSweeper.desktop` | Launcher template (`deploy.sh` replaces `@DIR@`) |
| `tools/render_test.py` | Render test without a window |

### Measurement (`sweeper.py`)

- Sample rate 2.56 MS/s (the RTL2832U is stable up to ~2.56 MS/s), 85 % of it used per hop
  (filter skirts cut off).
- FFT size 256–8192 depending on the requested resolution (≈ one column per screen pixel),
  Hann window, power averaging, DC spike smoothed, result in dBFS.
- Ranges narrower than half a hop are offset-tuned so the DC spike lies outside.
- Narrow sweeps are stretched to ≥ 0.5 s by longer averaging (less noise, and the history does
  not scroll by too fast).
- More than 4096 bins per sweep are merged by taking the maximum (narrow carriers stay visible).
- When the view changes, the running sweep is aborted and re-planned after 350 ms of quiet.
- Every hop is reported to the UI immediately – the top sweep fills in live.

### Display (`store.py`, `view.py`)

- One waterfall row per sweep; its height grows with the sweep duration
  (0.5 s → 2 px, ≥ 32 s → 16 px) so slow full-range sweeps remain visible.
- Each row's values are mapped onto the pixel columns: the maximum when there are several bins per
  column, linear interpolation otherwise. Results are cached per row.
- Gaps are filled column by column from the temporally nearest row with a measurement (older or
  newer, the older one on a tie), falling back to rows below the visible area.
- Colour scale from percentiles (8 % / 99.7 %) of the measured values, smoothed. Derived values use
  a desaturated, lightened variant of the colour map.
- Difference mode: on activation the newest completed sweep (older sweeps fill frequencies it did
  not cover) is kept as reference. Every sweep that starts while the mode is on is displayed as
  value − reference with a symmetric, auto-scaled range (99.5th percentile of |Δ|, 6–40 dB).
  Each on/off period is a separate session, so the history keeps its meaning after switching.
  In difference sweeps the marker snaps to the largest |Δ| instead of the highest level.
- Drawing uses GSK (texture with nearest filter, paths, Pango) in a content coordinate system that
  is rotated as a whole; input coordinates are mapped back.

## Measurements on the Mi Mix 2S (Logilink VG0002A, RTL2832U + FC0013)

| | |
|---|---|
| FC0013 retune (`rtlsdr_set_center_freq`) | **~115 ms** per hop – the actual bottleneck |
| Measurement per hop (16k samples) | ~7 ms |
| Full range 22–1100 MHz | ~500 hops, **~65 s** |
| Image rendering (Python/numpy) | 10–70 ms |

Around 949 MHz the FC0013 reports "no valid PLL combination"; that hop is skipped and the gap
is shown as derived.

## Possible extensions

Sweep time depends almost entirely on the number of retunes. `rtl_power` would not be faster
(same driver, same ~130 ms per hop). Options:

1. **Wider hops** for wide ranges: 3.2 MS/s with 90 % usage → ~25 % fewer hops, at the cost of
   occasional sample loss and slightly less accurate hop edges.
2. **Adaptive sweeping**: measure quiet sections only every 3rd–5th pass and active ones every
   time; the existing derivation fills the gaps. Roughly a factor of 2–3.
3. **Profile the FC0013 driver**: find out where the 115 ms per retune are spent.
4. **Different stick**: RTL-SDR Blog V4 (R828D) up to ~1.7 GHz, with bias tee, probably much
   faster retuning (not measured).
