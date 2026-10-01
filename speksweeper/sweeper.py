"""Sweeper thread: scans a frequency range in hops and delivers power spectra (dBFS).

The requested range can be changed at any time (it follows the view); a running sweep is then
aborted and a new one started. Results are passed as events to a callback (called in the sweeper
thread – the receiver has to forward them to the GUI thread itself).
"""
import math
import threading
import time

import numpy as np

from . import rtlsdr

SAMPLE_RATE = 2_560_000      # stable up to ~2.56 MS/s
USABLE = 0.85                # usable fraction of the bandwidth per hop (filter skirts cut off)
MAX_ROW_BINS = 4096          # more bins per sweep are merged by taking the maximum
MIN_ROW_TIME = 0.5           # narrow sweeps are stretched to ≥ this duration by longer averaging


def _pow2(x):
    return 1 << max(0, math.ceil(math.log2(max(1, x))))


class SweepPlan:
    """Hop plan for a range. All hops share a common bin grid starting at f_lo."""

    def __init__(self, f_lo, f_hi, target_bins, fs=SAMPLE_RATE):
        span = f_hi - f_lo
        self.fs = fs
        bin_w = span / max(64, target_bins)
        self.nfft = int(min(8192, max(256, _pow2(fs / bin_w))))
        self.bin_w = fs / self.nfft
        hop_bins = int(self.nfft * USABLE)
        self.hop_bw = hop_bins * self.bin_w
        n_total = max(1, int(math.ceil(span / self.bin_w)))
        self.f_lo = f_lo
        self.n_bins = n_total
        self.f_hi = f_lo + n_total * self.bin_w
        self.hops = []   # (tune_freq, first target bin, number of bins, first FFT bin (after fftshift))
        if n_total <= hop_bins // 2 - 4:
            # Narrow range: a single hop, placed beside the DC peak (offset tuning)
            start_fft = self.nfft // 2 + 4
            fc = f_lo - (start_fft - self.nfft // 2) * self.bin_w
            self.hops.append((fc, 0, n_total, start_fft))
        else:
            first = 0
            lo_fft = (self.nfft - hop_bins) // 2
            while first < n_total:
                n = min(hop_bins, n_total - first)
                # hop centre chosen so that target bin "first" falls on FFT bin lo_fft
                fc = f_lo + (first - lo_fft + self.nfft // 2) * self.bin_w
                self.hops.append((fc, first, n, lo_fft))
                first += n
        self.pool = max(1, math.ceil(n_total / MAX_ROW_BINS))

    @property
    def row_bins(self):
        return math.ceil(self.n_bins / self.pool)


class Sweeper(threading.Thread):
    def __init__(self, callback, simulate=False):
        super().__init__(daemon=True, name="sweeper")
        self.cb = callback
        self.simulate = simulate
        self._lock = threading.Lock()
        self._req = None          # (f_lo, f_hi, target_bins)
        self._req_gen = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.paused = False
        self.gain = 19.7           # None = AGC
        self._gain_dirty = True
        self.freq_range = (22e6, 1100e6)
        self.dev = None
        self._row_seq = 0

    # --- control (from the GUI thread) ---
    def request(self, f_lo, f_hi, target_bins):
        with self._lock:
            if self._req != (f_lo, f_hi, target_bins):
                self._req = (f_lo, f_hi, target_bins)
                self._req_gen += 1
        self._wake.set()

    def set_gain(self, g):
        self.gain = g
        self._gain_dirty = True

    def set_paused(self, p):
        self.paused = p
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    # --- Thread ---
    def run(self):
        while not self._stop.is_set():
            try:
                self._open()
                self._loop()
            except rtlsdr.RtlSdrError as e:
                self.cb("status", str(e))
            except OSError as e:
                self.cb("status", f"cannot load librtlsdr: {e}")
            finally:
                if self.dev is not None:
                    self.dev.close()
                    self.dev = None
            self._stop.wait(2.0)

    def _open(self):
        if self.simulate:
            self.dev = SimDevice()
        else:
            while rtlsdr.device_count() == 0:
                self.cb("status", "No RTL-SDR stick found (USB host mode on? powered hub?)")
                if self._stop.wait(2.0):
                    return
            self.dev = rtlsdr.RtlSdr(0)
        self.dev.set_sample_rate(SAMPLE_RATE)
        self.dev.set_agc(False)
        self._gain_dirty = True
        self.freq_range = self.dev.freq_range
        self.cb("device", {"name": self.dev.name, "tuner": self.dev.tuner,
                           "range": self.dev.freq_range, "gains": self.dev.gains})
        self.dev.reset_buffer()
        self.dev.read_bytes(32768)

    def _loop(self):
        win = None
        while not self._stop.is_set():
            if self.paused or self._req is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if self._gain_dirty:
                self._gain_dirty = False
                g = self.dev.set_gain(self.gain)
                self.cb("gain", g)
            with self._lock:
                f_lo, f_hi, tb = self._req
                gen = self._req_gen
            r0, r1 = self.freq_range
            f_lo, f_hi = max(f_lo, r0), min(f_hi, r1)
            if f_hi - f_lo < 10e3:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            plan = SweepPlan(f_lo, f_hi, tb)
            if win is None or len(win) != plan.nfft:
                win = np.hanning(plan.nfft).astype(np.float32)
                norm = 10 * math.log10((win.sum() ** 2) * 127.5 ** 2)
            # averaging: at least 16k samples, narrow sweeps longer (MIN_ROW_TIME)
            per_hop = max(16384, MIN_ROW_TIME * SAMPLE_RATE / len(plan.hops))
            n_frames = max(1, int(min(per_hop, SAMPLE_RATE * 0.5) // plan.nfft))
            self._row_seq += 1
            row_id = self._row_seq
            self.cb("sweep_start", {"id": row_id, "f_lo": plan.f_lo, "bin_w": plan.bin_w * plan.pool,
                                    "n": plan.row_bins, "t": time.time()})
            aborted = False
            for fc, first, n, fft0 in plan.hops:
                if self._stop.is_set() or self.paused or self._req_gen != gen or self._gain_dirty:
                    aborted = True
                    break
                f0 = plan.f_lo + first * plan.bin_w
                self.cb("tune", (f0, f0 + n * plan.bin_w, time.time()))
                if not self.dev.set_center_freq(fc):
                    continue
                self.dev.read_bytes(8192)              # discard settling samples
                iq = self.dev.read_iq(n_frames * plan.nfft)
                m = len(iq) // plan.nfft
                if m == 0:
                    continue
                fr = iq[:m * plan.nfft].reshape(m, plan.nfft) * win
                p = np.fft.fftshift(np.abs(np.fft.fft(fr, axis=1)) ** 2, axes=1).mean(axis=0)
                c = plan.nfft // 2                    # smooth the DC spike
                p[c - 1:c + 2] = 0.5 * (p[c - 3] + p[c + 3])
                db = 10 * np.log10(p[fft0:fft0 + n] + 1e-12) - norm
                self.cb("hop", (row_id, first, db.astype(np.float32), plan.pool, time.time()))
            self.cb("sweep_end", (row_id, aborted))


class SimDevice:
    """Simulated stick for testing without hardware: noise plus some typical transmitters."""
    name = "Simulation"
    tuner = "SIM"
    freq_range = (24e6, 1766e6)
    gains = [0.0, 10.0, 20.0, 30.0, 40.0]

    def __init__(self):
        self.fc = 100e6
        self.rng = np.random.default_rng()
        self.t0 = time.time()
        carriers = [(f * 1e6, -25) for f in (88.6, 91.2, 94.8, 99.7, 103.1, 105.5)]
        carriers += [(f * 1e6, -55) for f in (145.5, 433.92, 446.1, 868.3)]
        carriers += [(f * 1e6, -45) for f in (124.0, 129.4, 1090.0)]
        self.carriers = carriers
        self.wide = [(f * 1e6, 7.6e6, -45) for f in (482, 522, 586, 626, 690)]   # DVB-T muxes
        self.wide += [(f * 1e6, 1.5e6, -50) for f in (178.35, 202.93, 223.94)]  # DAB
        self.wide += [(f * 1e6, 9e6, -50) for f in (796, 806, 816, 935, 950)]    # LTE/GSM

    def set_sample_rate(self, r): self.fs = r
    def set_agc(self, on): pass
    def set_gain(self, g): return g
    def reset_buffer(self): pass
    def close(self): pass

    def set_center_freq(self, f):
        self.fc = f
        time.sleep(0.02)
        return True

    def read_bytes(self, n):
        time.sleep(n / 2 / self.fs)
        return np.zeros(n, np.uint8)

    def read_iq(self, n):
        time.sleep(n / self.fs)
        t = np.arange(n) / self.fs
        x = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)) * 0.4
        lo, hi = self.fc - self.fs / 2, self.fc + self.fs / 2
        tt = time.time() - self.t0
        for f, a in self.carriers:
            if lo < f < hi:
                a = a + 8 * math.sin(tt / 7 + f / 1e7)
                x += 127 * 10 ** (a / 20) * np.exp(2j * np.pi * (f - self.fc) * t)
        for f, bw, a in self.wide:
            if f + bw / 2 > lo and f - bw / 2 < hi:
                nb = self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)
                spec = np.fft.fft(nb)
                fr = np.fft.fftfreq(n, 1 / self.fs) + self.fc
                spec[np.abs(fr - f) > bw / 2] = 0
                x += 127 * 10 ** (a / 20) * np.fft.ifft(spec) * 3
        return x.astype(np.complex64)
