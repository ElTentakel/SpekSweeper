"""Minimal ctypes binding for librtlsdr (only what the sweeper needs)."""
import ctypes
import ctypes.util

import numpy as np

TUNER_NAMES = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013",
               4: "FC2580", 5: "R820T", 6: "R828D"}
# approximate tuning ranges per tuner in Hz
TUNER_RANGES = {1: (52e6, 2200e6), 2: (22e6, 948e6), 3: (22e6, 1100e6),
                4: (146e6, 924e6), 5: (24e6, 1766e6), 6: (24e6, 1766e6)}


class RtlSdrError(RuntimeError):
    pass


def _load():
    name = ctypes.util.find_library("rtlsdr") or "librtlsdr.so.0"
    lib = ctypes.CDLL(name)
    p = ctypes.c_void_p
    sig = {
        "rtlsdr_get_device_count": (ctypes.c_uint32, []),
        "rtlsdr_get_device_name": (ctypes.c_char_p, [ctypes.c_uint32]),
        "rtlsdr_open": (ctypes.c_int, [ctypes.POINTER(p), ctypes.c_uint32]),
        "rtlsdr_close": (ctypes.c_int, [p]),
        "rtlsdr_set_sample_rate": (ctypes.c_int, [p, ctypes.c_uint32]),
        "rtlsdr_set_center_freq": (ctypes.c_int, [p, ctypes.c_uint32]),
        "rtlsdr_set_freq_correction": (ctypes.c_int, [p, ctypes.c_int]),
        "rtlsdr_set_tuner_gain_mode": (ctypes.c_int, [p, ctypes.c_int]),
        "rtlsdr_set_tuner_gain": (ctypes.c_int, [p, ctypes.c_int]),
        "rtlsdr_get_tuner_gains": (ctypes.c_int, [p, ctypes.POINTER(ctypes.c_int)]),
        "rtlsdr_set_agc_mode": (ctypes.c_int, [p, ctypes.c_int]),
        "rtlsdr_get_tuner_type": (ctypes.c_int, [p]),
        "rtlsdr_reset_buffer": (ctypes.c_int, [p]),
        "rtlsdr_read_sync": (ctypes.c_int, [p, p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]),
    }
    for fn, (res, args) in sig.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    return lib


_lib = None


def lib():
    global _lib
    if _lib is None:
        _lib = _load()
    return _lib


def device_count():
    try:
        return lib().rtlsdr_get_device_count()
    except OSError:
        return 0


class RtlSdr:
    def __init__(self, index=0):
        self._l = lib()
        self._dev = ctypes.c_void_p()
        r = self._l.rtlsdr_open(ctypes.byref(self._dev), index)
        if r < 0:
            raise RtlSdrError(f"rtlsdr_open failed ({r}) – stick busy or no permission?")
        self.name = (self._l.rtlsdr_get_device_name(index) or b"").decode(errors="replace")
        self.tuner_type = self._l.rtlsdr_get_tuner_type(self._dev)
        self.tuner = TUNER_NAMES.get(self.tuner_type, "unknown")
        self.freq_range = TUNER_RANGES.get(self.tuner_type, (24e6, 1766e6))
        n = self._l.rtlsdr_get_tuner_gains(self._dev, None)
        arr = (ctypes.c_int * max(n, 1))()
        self._l.rtlsdr_get_tuner_gains(self._dev, arr)
        self.gains = [arr[i] / 10.0 for i in range(max(n, 0))]
        self._buf = None
        self.sample_rate = 0

    def close(self):
        if self._dev:
            self._l.rtlsdr_close(self._dev)
            self._dev = ctypes.c_void_p()

    def set_sample_rate(self, rate):
        if self._l.rtlsdr_set_sample_rate(self._dev, int(rate)) < 0:
            raise RtlSdrError("sample rate not supported")
        self.sample_rate = int(rate)

    def set_center_freq(self, f):
        return self._l.rtlsdr_set_center_freq(self._dev, int(round(f))) == 0

    def set_ppm(self, ppm):
        if ppm:
            self._l.rtlsdr_set_freq_correction(self._dev, int(ppm))

    def set_gain(self, gain_db):
        """gain_db=None → tuner AGC, otherwise the nearest gain step."""
        if gain_db is None or not self.gains:
            self._l.rtlsdr_set_tuner_gain_mode(self._dev, 0)
            return None
        g = min(self.gains, key=lambda x: abs(x - gain_db))
        self._l.rtlsdr_set_tuner_gain_mode(self._dev, 1)
        self._l.rtlsdr_set_tuner_gain(self._dev, int(round(g * 10)))
        return g

    def set_agc(self, on):
        self._l.rtlsdr_set_agc_mode(self._dev, 1 if on else 0)

    def reset_buffer(self):
        self._l.rtlsdr_reset_buffer(self._dev)

    def read_bytes(self, nbytes):
        """Reads nbytes (multiple of 512) raw as a uint8 array (interleaved I/Q)."""
        nbytes = (nbytes + 511) // 512 * 512
        if self._buf is None or len(self._buf) < nbytes:
            self._buf = np.empty(nbytes, dtype=np.uint8)
        n = ctypes.c_int(0)
        r = self._l.rtlsdr_read_sync(self._dev, self._buf.ctypes.data, nbytes, ctypes.byref(n))
        if r < 0:
            raise RtlSdrError(f"read error ({r}) – stick unplugged?")
        return self._buf[:n.value]

    def read_iq(self, nsamples):
        raw = self.read_bytes(nsamples * 2)
        iq = raw.astype(np.float32).view(np.complex64)
        iq -= (127.4 + 127.4j)
        return iq
