# SpekSweeper

Wasserfall-Übersicht über den gesamten Empfangsbereich eines RTL-SDR-Sticks (DVB-T-Stick) –
als Touch-App für **Phosh** auf einem Linux-Telefon (entwickelt auf Xiaomi Mi Mix 2S mit
postmarketOS/Nura). Python + GTK4/libadwaita + numpy, `librtlsdr` direkt per ctypes.

Ziel ist kein Messgerät, sondern ein schneller Überblick: Wo ist was los, wann war es aktiv?
Bedient wird ohne Zahleneingabe – nur Verschieben, Zoomen und Antippen.

## Funktionen

- **Wasserfall** über den gesamten Tunerbereich (FC0013: 22–1100 MHz, R820T/R828D: 24–1766 MHz),
  neueste Messung oben, darüber ein Spektrumstreifen der aktuellen Messung.
- **Frequenzachse unten** mit runden MHz-Marken, farbiger Bandleiste (UKW, Flugfunk, DAB+, BOS,
  70 cm, DVB-T2, LTE, SRD 868 …) und markanten Einzelfrequenzen (ADS-B, ISM 433, AIS, ISS, …).
- **Zeitachse links** (hh:mm:ss).
- **Sweep folgt der Ansicht**: Ein kleinerer Ausschnitt wird schneller und feiner abgetastet.
- **Abgeleitete Daten**: Was in einem Sweep nicht gemessen wurde (weil vorher verschoben/gezoomt
  wurde oder der Sweep noch läuft), wird aus der zeitlich nächsten Messung derselben Frequenz
  übernommen und **leicht grau getönt** dargestellt.
- **Marker** per Tippen: Frequenz, Pegel (dBFS) und Zeit.
- **Hoch- und Querformat**, zusätzlich Drehknopf (0°/90°/270°) für den Fall, dass das System
  nicht automatisch dreht.
- Automatische Farbskala, Verstärkung wählbar (AGC oder feste Stufe), Pause.
- Ansicht, Drehung und Verstärkung werden in `~/.config/speksweeper.json` gespeichert.

## Bedienung

| Geste | Wirkung |
|---|---|
| Waagrecht ziehen | Frequenzbereich verschieben |
| Zwei Finger spreizen/zusammenziehen | Frequenz-Zoom um den Mittelpunkt der Finger |
| Lupen-Knöpfe unten rechts / Mausrad | Zoom (Mausrad um die Zeigerposition) |
| Tippen | Marker setzen – rastet auf den höchsten Wert im Umkreis von ±16 px ein |
| Auf den Marker tippen | Marker entfernen |

Kopfleiste: Pause · Gesamtbereich · Drehen · Menü (Verstärkung, Verlauf löschen, Über).

Der Marker bevorzugt nahe Punkte (−0,15 dB pro Pixel Abstand) und gemessene vor abgeleiteten
Werten (−3 dB). Außerhalb der Ansicht wird er ausgeblendet und erscheint beim Zurückschieben
wieder; fällt seine Zeile aus dem Verlauf, wird er gelöscht.

## Voraussetzungen

- Linux mit GTK ≥ 4.14 (genutzt: `Gsk.PathBuilder`, `realize_for_display`), libadwaita ≥ 1.5,
  Python 3 mit PyGObject und numpy, `librtlsdr`.
  Auf postmarketOS: `sudo apk add rtl-sdr py3-numpy py3-gobject3 libadwaita`
- Kein DVB-Kerneltreiber für den Stick aktiv (sonst `dvb_usb_rtl28xxu` blacklisten).
- Zugriff ohne root über die udev-Regel aus `rtl-sdr` (Gruppe `plugdev`, Benutzer muss Mitglied
  sein). War der Stick vor dem Laden der Regel eingesteckt: `sudo udevadm trigger`.
- Auf dem Mi Mix 2S: USB-Port in den Host-Modus schalten (`sudo usb-host-mode on`) und den Stick
  über einen Hub mit eigener Stromversorgung betreiben – das Telefon liefert keine 5 V.

## Installation

    ./deploy.sh benutzer@host

kopiert die App per `tar` über ssh nach `~/.local/share/speksweeper` und legt den Starter
„SpekSweeper“ an. Direkt starten:

    ./speksweeper-launch            # mit Stick
    ./speksweeper-launch --sim      # simulierte Signale (UKW, DAB, DVB-T, LTE, …), kein Stick nötig

Per ssh auf dem Telefon starten:
`XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 ~/.local/share/speksweeper/speksweeper-launch`

## Testen

    python3 tools/render_test.py [--sim] [sekunden] [präfix]    # z. B. 60 /tmp/wf

Sweept erst den Gesamtbereich, zoomt dann auf 440–720 MHz, verschiebt die Ansicht, setzt Marker
per simuliertem Tippen und schreibt PNGs (Hoch-/Querformat, gedreht, Marker). Das Widget wird dabei
ohne sichtbares Fenster gerendert – funktioniert also auch bei gesperrtem Telefon, auf dem Phosh
keine Fenster zeichnet.

Screenshot des laufenden Fensters (nur bei entsperrtem Bildschirm):
`SPEKSWEEPER_SHOT=/tmp/x.png:20 ./speksweeper-launch`

## Aufbau

| Datei | Inhalt |
|---|---|
| `speksweeper/rtlsdr.py` | ctypes-Anbindung an `librtlsdr` (öffnen, Tuner/Gain, `read_sync`) |
| `speksweeper/sweeper.py` | Hop-Plan, FFT, Sweeper-Thread, `SimDevice` für Tests ohne Stick |
| `speksweeper/store.py` | Sweep-Verlauf, Abbildung auf Pixelspalten, Ableitung fehlender Werte |
| `speksweeper/view.py` | Wasserfall-Widget: GSK-Snapshot, Gesten, Achsen, Bänder, Marker, Drehung |
| `speksweeper/bands.py` | Bänder und markante Frequenzen für die Achsenbeschriftung |
| `speksweeper/app.py` | Adw-Anwendung, Kopfleiste, Menü, Einstellungen |
| `speksweeper-launch` | Startskript |
| `io.github.speksweeper.SpekSweeper.desktop` | Starter-Vorlage (`@DIR@` ersetzt `deploy.sh`) |
| `tools/render_test.py` | Render-Test ohne Fenster |

### Messung (`sweeper.py`)

- Abtastrate 2,56 MS/s (RTL2832U stabil bis ~2,56 MS/s), davon 85 % pro Hop genutzt
  (Filterflanken abgeschnitten).
- FFT-Größe 256–8192 abhängig von der gewünschten Auflösung (≈ eine Spalte pro Bildschirmpixel),
  Hann-Fenster, Leistungsmittelung, DC-Spitze geglättet, Ergebnis in dBFS.
- Bereiche schmaler als ein halber Hop werden mit Offset abgestimmt, damit die DC-Spitze außerhalb
  liegt.
- Schmale Sweeps werden durch längere Mittelung auf ≥ 0,5 s gestreckt (weniger Rauschen, Verlauf
  läuft nicht zu schnell durch).
- Mehr als 4096 Bins pro Sweep werden per Maximum zusammengefasst (schmale Träger bleiben sichtbar).
- Ändert sich die Ansicht, wird der laufende Sweep abgebrochen und nach 350 ms Ruhe neu geplant.
- Jeder Hop wird sofort an die Oberfläche gemeldet – der oberste Sweep füllt sich live.

### Darstellung (`store.py`, `view.py`)

- Eine Wasserfall-Zeile pro Sweep; ihre Höhe wächst mit der Sweep-Dauer
  (0,5 s → 2 px, ≥ 32 s → 16 px), damit langsame Gesamt-Sweeps sichtbar bleiben.
- Pro Zeile werden die Messwerte auf die Pixelspalten abgebildet: bei mehreren Bins pro Spalte das
  Maximum, sonst linear interpoliert. Ergebnisse werden je Zeile gecacht.
- Lücken werden spaltenweise aus der zeitlich nächsten Zeile mit Messwert gefüllt (ältere oder
  neuere, bei Gleichstand die ältere), notfalls aus Zeilen unterhalb des sichtbaren Bereichs.
- Farbskala aus Perzentilen (8 % / 99,7 %) der gemessenen Werte, geglättet. Abgeleitete Werte
  nutzen eine entsättigte, aufgehellte Variante der Farbtabelle.
- Gezeichnet wird mit GSK (Textur mit Nearest-Filter, Pfade, Pango) in einem Inhalts-
  Koordinatensystem, das für die Drehung als Ganzes rotiert wird; Eingaben werden zurückgerechnet.

## Messwerte auf dem Mi Mix 2S (Logilink VG0002A, RTL2832U + FC0013)

| | |
|---|---|
| Umstimmen FC0013 (`rtlsdr_set_center_freq`) | **~115 ms** pro Hop – der eigentliche Engpass |
| Messung pro Hop (16k Samples) | ~7 ms |
| Gesamtbereich 22–1100 MHz | ~500 Hops, **~65 s** |
| Bildaufbau (Python/numpy) | 10–70 ms |

Bei ~949 MHz meldet der FC0013 „no valid PLL combination“; der Hop wird übersprungen und die
Lücke als abgeleitet dargestellt.

## Mögliche Erweiterungen

Die Sweep-Zeit hängt fast nur an der Zahl der Umstimmvorgänge. `rtl_power` wäre nicht schneller
(gleicher Treiber, gleiche ~130 ms/Hop). Ansätze:

1. **Breitere Hops** für weite Bereiche: 3,2 MS/s und 90 % Nutzung → ~25 % weniger Hops; dafür
   gelegentliche Sample-Verluste und etwas ungenauere Ränder.
2. **Adaptiv sweepen**: ruhige Abschnitte nur jeden 3.–5. Durchlauf messen, aktive jedes Mal; die
   Lücken füllt die vorhandene Ableitung. Grob Faktor 2–3.
3. **FC0013-Treiber profilieren**: herausfinden, wo die 115 ms beim Umstimmen entstehen.
4. **Anderer Stick**: RTL-SDR Blog V4 (R828D) bis ~1,7 GHz, mit Bias-Tee, vermutlich deutlich
   schnelleres Umstimmen (nicht gemessen).
