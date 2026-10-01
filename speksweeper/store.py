"""Sweep history and mapping onto a pixel grid (including derivation of missing values)."""
import collections

import numpy as np


class Row:
    __slots__ = ("id", "t0", "t1", "f_lo", "bin_w", "vals", "done", "version")

    def __init__(self, rid, t, f_lo, bin_w, n):
        self.id, self.t0, self.t1 = rid, t, t
        self.f_lo, self.bin_w = f_lo, bin_w
        self.vals = np.full(n, np.nan, np.float32)
        self.done = False
        self.version = 0

    @property
    def f_hi(self):
        return self.f_lo + len(self.vals) * self.bin_w

    def add_hop(self, first, db, pool, t):
        if pool == 1:
            seg = self.vals[first:first + len(db)]
            np.fmax(seg, db[:len(seg)], out=seg)
        else:
            idx = (first + np.arange(len(db))) // pool
            ok = idx < len(self.vals)
            np.fmax.at(self.vals, idx[ok], db[ok])
        self.t1 = t
        self.version += 1

    def peak_freq(self, f0, f1):
        """Frequency (bin centre) of the highest measured bin between f0 and f1, else None."""
        n = len(self.vals)
        i0 = max(0, int((f0 - self.f_lo) / self.bin_w))
        i1 = min(n, max(i0 + 1, int(np.ceil((f1 - self.f_lo) / self.bin_w))))
        seg = self.vals[i0:i1]
        if i0 >= n or np.isnan(seg).all():
            return None
        return self.f_lo + (i0 + int(np.nanargmax(seg)) + 0.5) * self.bin_w

    def value_at(self, f):
        i = int((f - self.f_lo) / self.bin_w)
        if 0 <= i < len(self.vals):
            return float(self.vals[i])
        return None

    def columns(self, edges):
        """Values of this row for pixel columns with frequency boundaries edges (length ncols+1)."""
        n = len(self.vals)
        ncols = len(edges) - 1
        e = (edges - self.f_lo) / self.bin_w
        bpc = e[1] - e[0]
        out = np.full(ncols, np.nan, np.float32)
        if e[-1] <= 0 or e[0] >= n:
            return out
        if bpc >= 1.5:
            # several bins per column: maximum (narrow carriers are not lost)
            i0 = np.floor(e[:-1]).astype(np.int64)
            i1 = np.ceil(e[1:]).astype(np.int64)
            ok = (i1 > 0) & (i0 < n)
            i0 = np.clip(i0[ok], 0, n - 1)
            last = min(n, int(i1[ok][-1]))
            with np.errstate(invalid="ignore"):
                out[ok] = np.fmax.reduceat(self.vals[:last], i0)
        else:
            x = (0.5 * (e[:-1] + e[1:])) - 0.5
            ok = (x >= -0.5) & (x <= n - 0.5)
            xc = np.clip(x[ok], 0, n - 1)
            j = np.minimum(np.floor(xc).astype(np.int64), max(n - 2, 0))
            fr = (xc - j).astype(np.float32)
            v0 = self.vals[j]
            v1 = self.vals[np.minimum(j + 1, n - 1)]
            v = v0 + (v1 - v0) * fr
            # edge/NaN neighbours: use the nearest known value
            bad = np.isnan(v)
            if bad.any():
                v[bad] = np.where(fr[bad] < 0.5, v0[bad], v1[bad])
            out[ok] = v
        return out


class History:
    def __init__(self, max_rows=3000):
        self.rows = collections.deque()
        self.by_id = {}
        self.max_rows = max_rows
        self._cache_key = None
        self._cache = {}

    def start(self, rid, t, f_lo, bin_w, n):
        r = Row(rid, t, f_lo, bin_w, n)
        self.rows.append(r)
        self.by_id[rid] = r
        while len(self.rows) > self.max_rows:
            old = self.rows.popleft()
            self.by_id.pop(old.id, None)
            self._cache.pop(old.id, None)
        return r

    def add_hop(self, rid, first, db, pool, t):
        r = self.by_id.get(rid)
        if r is not None:
            r.add_hop(first, db, pool, t)

    def finish(self, rid, aborted):
        r = self.by_id.get(rid)
        if r is None:
            return
        r.done = True
        if np.isnan(r.vals).all():           # aborted without data → discard
            self.rows.remove(r)
            del self.by_id[rid]

    def __len__(self):
        return len(self.rows)

    def newest(self):
        return self.rows[-1] if self.rows else None

    def render(self, f_lo, f_hi, ncols, nrows):
        """Matrix nrows×ncols (row 0 = newest sweep).

        Returns (values, derived, rows) – missing values are taken from the temporally nearest
        known value in the same column and flagged as derived. Columns without any known value
        stay NaN.
        """
        edges = np.linspace(f_lo, f_hi, ncols + 1)
        key = (f_lo, f_hi, ncols)
        if key != self._cache_key:
            self._cache_key = key
            self._cache = {}
        allrows = list(self.rows)[::-1]
        rows = allrows[:nrows]
        R = len(rows)
        M = np.full((max(R, 1), ncols), np.nan, np.float32)
        for i, r in enumerate(rows):
            M[i] = self._columns(r, edges)
        if R == 0:
            return M[:0], np.zeros((0, ncols), bool), []
        valid = ~np.isnan(M)
        derived = np.zeros_like(valid)
        need = np.flatnonzero(~valid.all(axis=0))     # only process columns with gaps
        if need.size == 0:
            return M, derived, rows
        # fallback value from rows older than the visible range
        base = np.full(need.size, np.nan, np.float32)
        for r in allrows[R:R + 400]:
            miss = np.isnan(base)
            if not miss.any():
                break
            if r.f_hi < f_lo or r.f_lo > f_hi:
                continue
            base[miss] = self._columns(r, edges)[need[miss]]
        Ms = M[:, need]
        vs = valid[:, need]
        ridx = np.arange(R, dtype=np.int32)[:, None]
        big = np.int32(1 << 30)
        old = np.where(vs, ridx, np.int32(R))
        old = np.minimum.accumulate(old[::-1], axis=0)[::-1]       # nearest older row
        new = np.where(vs, ridx, np.int32(-1))
        new = np.maximum.accumulate(new, axis=0)                   # nearest newer row
        d_old = np.where(old < R, old - ridx, big)
        d_new = np.where(new >= 0, ridx - new, big)
        src = np.where(d_old <= d_new, old, new)
        src[(d_old == big) & (d_new == big)] = R
        filled = np.take_along_axis(np.vstack([Ms, base[None, :]]), src, axis=0)
        M[:, need] = filled
        derived[:, need] = ~vs & ~np.isnan(filled)
        return M, derived, rows

    def _columns(self, r, edges):
        c = self._cache.get(r.id)
        if c is None or c[0] != r.version:
            c = (r.version, r.columns(edges))
            self._cache[r.id] = c
        return c[1]
