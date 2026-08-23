#!/usr/bin/env python3
"""Scope waveform display: pure plot math + the Tk trace window (issue #42).

Captured waveforms used to go straight to CSV with no way to SEE them in
the app. TraceWindow renders the record on a canvas: a per-pixel-column
min/max envelope (so a 10k-point record stays honest at 600 px -- glitches
survive decimation), 'nice' axis ticks, and Save CSV from the window.

The math lives in module-level functions with no Tk so it can be tested
headless: .venv/bin/python tests/test_scope_trace.py
"""
import csv
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from lcr_format import format_si


def decimate_minmax(values, columns):
    """Reduce a sample record to per-column (lo, hi) envelope pairs.

    Splits `values` into `columns` roughly equal spans and returns
    [(min, max), ...] one per span -- the standard scope-style reduction
    that cannot swallow narrow glitches. With fewer samples than columns,
    returns one (v, v) pair per sample (caller just draws fewer columns).
    """
    if columns <= 0:
        raise ValueError(f"columns must be positive, got {columns}")
    n = len(values)
    if n == 0:
        raise ValueError("empty waveform")
    if n <= columns:
        return [(v, v) for v in values]
    out = []
    for c in range(columns):
        a = c * n // columns
        b = max((c + 1) * n // columns, a + 1)
        span = values[a:b]
        out.append((min(span), max(span)))
    return out


def nice_ticks(lo, hi, target=6):
    """'Nice' tick positions (1/2/5 x 10^k steps) covering [lo, hi].

    Returns a list of floats including values just inside the range ends.
    Degenerate ranges (hi <= lo) return the single value.
    """
    if not (hi > lo):
        return [lo]
    span = hi - lo
    raw = span / max(target, 1)
    step = 10.0 ** math.floor(math.log10(raw))
    for mult in (1, 2, 5, 10):
        if span / (step * mult) <= target:
            step *= mult
            break
    first = math.ceil(lo / step) * step
    ticks = []
    t = first
    while t <= hi + step * 1e-9:
        # round off accumulated float error so labels stay clean
        ticks.append(round(t / step) * step)
        t += step
    return ticks


class TraceWindow(tk.Toplevel):
    """Plot one captured scope record; offers Save CSV of the same data."""

    W, H = 640, 360
    PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 16, 32

    def __init__(self, app, channel, waveform):
        super().__init__(app.root)
        self.app = app
        self.channel = channel
        self.wf = waveform
        n = waveform['npts']
        self.title(f"Scope CH{channel} waveform -- {n} points, "
                   f"dt {format_si(waveform['dt'], 's')}")
        self.resizable(False, False)

        self.canvas = tk.Canvas(self, width=self.W, height=self.H,
                                bg='white', highlightthickness=0)
        self.canvas.pack(padx=8, pady=8)

        btns = ttk.Frame(self)
        btns.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(btns, text=self._summary()).pack(side=tk.LEFT)
        ttk.Button(btns, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Save CSV...",
                   command=self._save_csv).pack(side=tk.RIGHT)

        self._draw()

    def _summary(self):
        v = self.wf['v']
        lo, hi = min(v), max(v)
        return (f"min {format_si(lo, 'V')}   max {format_si(hi, 'V')}   "
                f"pk-pk {format_si(hi - lo, 'V')}")

    def _draw(self):
        c = self.canvas
        t, v = self.wf['t'], self.wf['v']
        x0, y0 = self.PAD_L, self.PAD_T
        x1, y1 = self.W - self.PAD_R, self.H - self.PAD_B
        plot_w, plot_h = x1 - x0, y1 - y0

        v_lo, v_hi = min(v), max(v)
        if v_hi <= v_lo:                       # flat trace: pad the range
            v_lo, v_hi = v_lo - 0.5, v_hi + 0.5
        margin = 0.05 * (v_hi - v_lo)
        v_lo, v_hi = v_lo - margin, v_hi + margin
        t_lo, t_hi = t[0], t[-1]
        if t_hi <= t_lo:
            t_hi = t_lo + 1e-9

        def px(tv):
            return x0 + (tv - t_lo) / (t_hi - t_lo) * plot_w

        def py(vv):
            return y1 - (vv - v_lo) / (v_hi - v_lo) * plot_h

        # grid + labels from nice ticks
        for tick in nice_ticks(v_lo, v_hi):
            y = py(tick)
            c.create_line(x0, y, x1, y, fill='#e0e0e0')
            c.create_text(x0 - 6, y, text=format_si(tick, 'V', digits=3),
                          anchor='e', font=('TkDefaultFont', 8), fill='#555')
        for tick in nice_ticks(t_lo, t_hi):
            x = px(tick)
            c.create_line(x, y0, x, y1, fill='#e0e0e0')
            c.create_text(x, y1 + 6, text=format_si(tick, 's', digits=3),
                          anchor='n', font=('TkDefaultFont', 8), fill='#555')
        if v_lo < 0 < v_hi:
            c.create_line(x0, py(0), x1, py(0), fill='#999', dash=(3, 3))
        c.create_rectangle(x0, y0, x1, y1, outline='#888')

        # min/max envelope, one vertical line per pixel column
        env = decimate_minmax(v, plot_w)
        colw = plot_w / len(env)
        for i, (lo, hi) in enumerate(env):
            x = x0 + (i + 0.5) * colw
            yl, yh = py(lo), py(hi)
            if abs(yl - yh) < 1:               # keep hairline visible
                yl = yh + 1
            c.create_line(x, yl, x, yh, fill='#1565c0')

    def _save_csv(self):
        filename = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"waveform_ch{self.channel}.csv")
        if not filename:
            return
        try:
            with open(filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['Time (s)', 'Voltage (V)'])
                for tv, vv in zip(self.wf['t'], self.wf['v']):
                    writer.writerow([tv, vv])
        except Exception as e:
            messagebox.showerror("Save error", str(e), parent=self)
            return
        self.app.status_bar.config(
            text=f"CH{self.channel} waveform saved: {filename}")
