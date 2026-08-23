"""Screen 5 -- Campaign: the specimens x outcomes table, Weibull fit of
a selection (suspensions handled correctly), and the compliance-matrix
viewer."""
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use('TkAgg')
import numpy as np  # noqa: E402
from matplotlib.backends.backend_tkagg import (  # noqa: E402
    FigureCanvasTkAgg)
from matplotlib.figure import Figure  # noqa: E402

from analysis import compliance as _compliance  # noqa: E402
from analysis import weibull as _weibull  # noqa: E402
from analysis.figexport import TOL_BRIGHT  # noqa: E402

from .widgets import COLORS  # noqa: E402


class CampaignScreen(tk.Frame):
    def __init__(self, master, state):
        super().__init__(master)
        self.state = state
        self._build()

    def _build(self):
        top = tk.Frame(self)
        top.pack(fill='x', padx=8, pady=4)
        tk.Button(top, text='Reload registry',
                  command=self.refresh).pack(side='left')
        tk.Button(top, text='Weibull fit selected',
                  command=self._weibull).pack(side='left', padx=6)
        tk.Button(top, text='Compliance matrix...',
                  command=self._compliance).pack(side='left')
        self.summary = tk.Label(top, text='', fg=COLORS['ink'],
                                font=('TkDefaultFont', 10, 'bold'))
        self.summary.pack(side='left', padx=10)

        cols = ('specimen', 'geometry', 'status', 'cycles',
                'failure_mode', 'last_run')
        self.tree = ttk.Treeview(self, columns=cols, show='headings',
                                 height=14, selectmode='extended')
        for c, w in zip(cols, (130, 100, 90, 90, 110, 150)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor='w')
        self.tree.pack(fill='both', expand=False, padx=8, pady=4)
        self.tree.tag_configure('failed', foreground=COLORS['red_fg'])
        self.tree.tag_configure('suspended', foreground='#555')
        self.tree.tag_configure('in_test', foreground=COLORS['good'])

        self.fig = Figure(figsize=(6.4, 3.4), dpi=96)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill='both', expand=True,
                                         padx=8, pady=4)
        self.refresh()

    def refresh(self):
        reg = self.state.registry()
        self.tree.delete(*self.tree.get_children())
        if not reg:
            return
        for sid in reg.ids():
            row = reg.get(sid)
            self.tree.insert('', 'end', iid=sid, values=(
                sid, row['geometry'], row['status'],
                row.get('cycles_accum') or 0,
                row.get('failure_mode') or '',
                row.get('last_run_id') or ''),
                tags=(row['status'],))

    def _selected_rows(self):
        reg = self.state.registry()
        sids = self.tree.selection() or self.tree.get_children()
        rows = []
        for sid in sids:
            r = reg.get(sid)
            if r['status'] in ('failed', 'suspended'):
                rows.append({'specimen_id': sid,
                             'cycles': float(r.get('cycles_accum')
                                             or 0),
                             'status': r['status']})
        return rows

    def _weibull(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo(
                'Weibull', 'Select specimens with failed/suspended '
                           'outcomes (in-test and virgin rows carry no '
                           'survival information).', parent=self)
            return
        res = _weibull.analyze(rows)
        self.summary.config(text=_weibull.summary_line(res))
        self._plot(res)

    def _plot(self, res):
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        plot = res['plot']
        if plot['fail_t']:
            t = np.asarray(plot['fail_t'])
            F = np.asarray(plot['fail_F'])
            y = np.log(-np.log(1 - F))
            ax.plot(np.log(t), y, 'o', color=TOL_BRIGHT[0],
                    label='failures (median ranks)')
        for i, s in enumerate(plot['susp_t']):
            ax.axvline(np.log(s), color='#999', ls=':', lw=1,
                       label='suspension' if i == 0 else None)
        for key, color, style in (('mrr', TOL_BRIGHT[2], '--'),
                                  ('mle', TOL_BRIGHT[1], '-')):
            fit = res.get(key)
            if fit and plot['fail_t']:
                ts = np.linspace(min(plot['fail_t']) * 0.7,
                                 max(plot['fail_t']
                                     + plot['susp_t']) * 1.3, 50)
                y = fit['beta'] * (np.log(ts) - np.log(fit['eta']))
                ax.plot(np.log(ts), y, style, color=color,
                        label=f"{fit['method']} beta={fit['beta']:.2f}")
        h = res.get('headline')
        if h:
            ax.axvline(np.log(h['b10']), color=COLORS['red_fg'], lw=1,
                       label=f"B10 {h['b10']:,.0f}")
        ax.set_xlabel('ln(cycles)', fontsize=8)
        ax.set_ylabel('ln(-ln(1-F))', fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)
        ax.tick_params(labelsize=7)
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _compliance(self):
        path = filedialog.askopenfilename(
            title='Claims file (JSON)',
            filetypes=[('claims JSON', '*.json')])
        if not path:
            return
        try:
            doc = _compliance.load_claims(path)
        except _compliance.ClaimsError as e:
            messagebox.showerror('Claims invalid', str(e), parent=self)
            return
        rows = self._selected_rows()
        cycles = [r['cycles'] for r in rows]
        ctx = {'campaign': {
            'n': len(rows),
            'min_cycles': min(cycles) if cycles else None,
            'max_cycles': max(cycles) if cycles else None,
        }}
        try:
            res = _weibull.analyze(rows) if rows else None
            if res and res.get('headline'):
                ctx['campaign']['b10'] = res['headline']['b10']
        except Exception:
            pass
        evaluated = _compliance.evaluate(doc, ctx)
        top = tk.Toplevel(self)
        top.title('Compliance matrix')
        txt = tk.Text(top, width=110, height=24, font=('Courier', 9))
        txt.pack(fill='both', expand=True)
        for r in evaluated:
            txt.insert('end',
                       f"{r.get('req_id', ''):10s} "
                       f"{r.get('status', ''):9s} "
                       f"{r.get('source_clause', ''):28s} "
                       f"req {r.get('required', '')!s:>10s}  "
                       f"got {r.get('achieved', '')!s:>10s}  "
                       f"{r.get('tailoring_note') or r.get('auto_note') or ''}\n")
        txt.config(state='disabled')
