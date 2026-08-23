"""Screen 4 -- Review: trend plots (embedded matplotlib -- allowed
here, this screen is never in the HV hot path), the event table with
disposition header, and the report generator."""
import os
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import (  # noqa: E402
    FigureCanvasTkAgg)
from matplotlib.figure import Figure  # noqa: E402

from analysis import reduce as _reduce  # noqa: E402
from analysis.figexport import TOL_BRIGHT  # noqa: E402

from .widgets import COLORS  # noqa: E402


class ReviewScreen(tk.Frame):
    def __init__(self, master, state):
        super().__init__(master)
        self.state = state
        self.run_dir = None
        self._build()

    def _build(self):
        top = tk.Frame(self)
        top.pack(fill='x', padx=8, pady=4)
        tk.Button(top, text='Open run...', command=self._browse).pack(
            side='left')
        self.head = tk.Label(top, text='(no run loaded)',
                             font=('TkDefaultFont', 11, 'bold'))
        self.head.pack(side='left', padx=10)
        tk.Button(top, text='Generate report.html',
                  command=self._report).pack(side='right')

        axis_row = tk.Frame(self)
        axis_row.pack(fill='x', padx=8)
        tk.Label(axis_row, text='x-axis:').pack(side='left')
        self.axis_var = tk.StringVar(value='cycles')
        for key, label in (('cycles', 'cycles'),
                           ('actuated_s', 'actuated time'),
                           ('wall_s', 'wall clock')):
            tk.Radiobutton(axis_row, text=label, value=key,
                           variable=self.axis_var,
                           command=self._plot).pack(side='left')

        mid = tk.PanedWindow(self, orient='horizontal')
        mid.pack(fill='both', expand=True, padx=8, pady=4)
        plot_frame = tk.Frame(mid)
        mid.add(plot_frame, stretch='always')
        self.fig = Figure(figsize=(5.6, 4.2), dpi=96)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)

        right = tk.Frame(mid, width=380)
        mid.add(right)
        tk.Label(right, text='Events',
                 font=('TkDefaultFont', 9, 'bold')).pack(anchor='w')
        cols = ('cycles', 'rule', 'action', 'message')
        self.tree = ttk.Treeview(right, columns=cols, show='headings',
                                 height=18)
        for c, w in zip(cols, (70, 90, 60, 220)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor='w')
        self.tree.pack(fill='both', expand=True)
        self.tree.tag_configure('abort', foreground=COLORS['red_fg'])
        self.tree.tag_configure('warn', foreground=COLORS['amber_fg'])

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.state.data_root,
                                    title='Pick a run folder')
        if d:
            self.load(d)

    def load(self, run_dir):
        self.run_dir = run_dir
        import json
        status = {}
        sp = os.path.join(run_dir, 'status.json')
        if os.path.exists(sp):
            status = json.load(open(sp, encoding='utf-8'))
        disp = status.get('disposition', status.get('state', '?'))
        fm = status.get('failure_mode', '')
        cyc = status.get('cycles_total', 0)
        if disp == 'failed':
            txt = (f'FAILED at {cyc:,} cycles -- first-tripped: {fm}')
            fg = COLORS['red_fg']
        elif disp in ('complete', 'suspended'):
            txt = f'{disp.upper()} at {cyc:,} cycles (right-censored)'
            fg = COLORS['good']
        else:
            txt = f'{disp} at {cyc:,} cycles'
            fg = COLORS['amber_fg']
        self.head.config(text=f'{os.path.basename(run_dir)}: {txt}',
                         fg=fg)
        self.tree.delete(*self.tree.get_children())
        for e in _reduce.read_csv(os.path.join(run_dir, 'events.csv')):
            tag = ('abort' if e.get('action') == 'abort' else
                   'warn' if e.get('action') in ('warn', 'flag',
                                                 'pause') else '')
            self.tree.insert('', 'end', values=(
                e.get('cycles_total'), e.get('rule_id'),
                e.get('action'), e.get('message')), tags=(tag,))
        self._plot()

    def _plot(self):
        if not self.run_dir:
            return
        trends = _reduce.trend_rows(self.run_dir)
        xkey = self.axis_var.get()
        pts = [t for t in trends['interludes']
               if t.get(xkey) is not None]
        self.fig.clear()
        axes = self.fig.subplots(3, 1, sharex=True)
        for (key, label, color), ax in zip(
                (('disp_ratio', 'amp/baseline', TOL_BRIGHT[0]),
                 ('leak_ua', 'leak uA', TOL_BRIGHT[1]),
                 ('c_est_nf', 'C nF', TOL_BRIGHT[2])), axes):
            xs = [t[xkey] for t in pts if t.get(key) is not None]
            ys = [t[key] for t in pts if t.get(key) is not None]
            ax.plot(xs, ys, 'o-', color=color, ms=3)
            ax.set_ylabel(label, fontsize=8)
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=7)
        if pts and (pts[-1].get(xkey) or 0) > 3000:
            for ax in axes:
                ax.set_xscale('symlog')
        axes[-1].set_xlabel(xkey, fontsize=8)
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _report(self):
        if not self.run_dir:
            return

        def work():
            from analysis.report_run import generate
            out = generate(self.run_dir)
            self.after(0, lambda: webbrowser.open(
                'file:///' + out.replace('\\', '/')))

        threading.Thread(target=work, daemon=True).start()
