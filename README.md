# SpekSweeper

Waterfall overview of the full receive range of an RTL-SDR stick (DVB-T stick) – as a touch app
for **Phosh** on a Linux phone (developed on a Xiaomi Mi Mix 2S running postmarketOS/Nura).
Python + GTK4/libadwaita + numpy, talking to `librtlsdr` directly via ctypes.

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
- **Marker** by tapping: frequency, level (dBFS) and time.
- **Portrait and landscape**, plus a rotate button (0°/90°/270°) for when the system does not
  rotate automatically.
- Automatic colour scale, selectable gain (AGC or a fixed step), pause.
- View, rotation and gain are stored in `~/.config/speksweeper.json`.

## Usage

| Gesture | Effect |
|---|---|
| Drag horizontally | Pan the frequency range |
| Pinch with two fingers | Frequency zoom around the centre of the fingers |
| Zoom buttons bottom right / mouse wheel | Zoom (mouse wheel around the pointer) |
| Tap | Set marker – snaps to the highest value within ±16 px |
| Tap the marker | Remove marker |

Header bar: pause · full range · rotate · menu (gain, clear history, about).

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
- USB port must be in host mode

## Installation

    ./install.sh                  # on the device itself
    ./deploy.sh user@host         # from another machine, via ssh

Both install the app to `~/.local/share/speksweeper` and create the launcher "SpekSweeper"
(`./install.sh --uninstall` removes both). To start directly from the source tree:

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
| `io.github.speksweeper.SpekSweeper.desktop` | Launcher template (`install.sh` replaces `@DIR@`) |
| `install.sh` / `deploy.sh` | Install locally / on another device via ssh |
| `tools/render_test.py` | Render test without a window |

