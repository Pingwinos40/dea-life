"""New widgets for the lifecycle GUI, beside the vendored ui_widgets
(Tooltip / ScrollableTab / SplashScreen). Plain tk only -- the run
dashboard must stay responsive during HV activity (the lab's measured
PySide6-lag lesson), so no matplotlib anywhere near the live path.

Palette = the Multitool state vocabulary: DRY amber, LIVE red, action
red #c62828, good green #2e7d32, ink #1f3a5f.
"""
import tkinter as tk

COLORS = {
    'ink': '#1f3a5f', 'good': '#2e7d32', 'action': '#c62828',
    'amber_bg': '#fff3cd', 'amber_fg': '#8a5a00',
    'red_bg': '#f8d7da', 'red_fg': '#a01010',
    'chip_green': '#2e7d32', 'chip_amber': '#8a5a00',
    'chip_red': '#a01010', 'chip_gray': '#666666',
}


class BigTile(tk.Frame):
    """Value + caption tile for the dashboard grid."""

    def __init__(self, master, caption, value='--', **kw):
        super().__init__(master, relief='groove', bd=1, padx=8, pady=4,
                         **kw)
        self.value_lbl = tk.Label(self, text=value,
                                  font=('TkDefaultFont', 18, 'bold'),
                                  fg=COLORS['ink'])
        self.value_lbl.pack()
        self.sub_lbl = tk.Label(self, text='', font=('TkDefaultFont', 8))
        self.sub_lbl.pack()
        tk.Label(self, text=caption, font=('TkDefaultFont', 9),
                 fg='#555').pack()

    def set(self, value, sub=''):
        self.value_lbl.config(text=str(value))
        self.sub_lbl.config(text=str(sub))


class StatusChip(tk.Label):
    """Colored state pill."""

    def __init__(self, master, text='IDLE', **kw):
        super().__init__(master, text=text, fg='white',
                         bg=COLORS['chip_gray'], padx=10, pady=2,
                         font=('TkDefaultFont', 11, 'bold'), **kw)

    def set(self, text, level='gray'):
        self.config(text=text,
                    bg=COLORS.get(f'chip_{level}', COLORS['chip_gray']))


class Sparkline(tk.Canvas):
    """Tiny trend line fed with (x, y) points."""

    def __init__(self, master, width=180, height=36, color=None, **kw):
        super().__init__(master, width=width, height=height,
                         bg='white', highlightthickness=1,
                         highlightbackground='#ccc', **kw)
        self._color = color or COLORS['ink']
        self._sw, self._sh = width, height

    def set_points(self, xs, ys):
        self.delete('all')
        pts = [(x, y) for x, y in zip(xs, ys)
               if isinstance(y, (int, float))]
        if len(pts) < 2:
            self.create_text(4, self._sh // 2, anchor='w',
                             text='(awaiting data)', fill='#999',
                             font=('TkDefaultFont', 7))
            return
        x0, x1 = pts[0][0], pts[-1][0]
        ylo = min(p[1] for p in pts)
        yhi = max(p[1] for p in pts)
        if x1 == x0:
            x1 = x0 + 1
        if yhi == ylo:
            yhi = ylo + 1e-9
        coords = []
        for x, y in pts:
            px = 3 + (self._sw - 6) * (x - x0) / (x1 - x0)
            py = self._sh - 4 - (self._sh - 12) * (y - ylo) / (yhi - ylo)
            coords += [px, py]
        self.create_line(*coords, fill=self._color, width=1.5)
        self.create_text(3, 7, anchor='w', text=f'{yhi:.3g}',
                         fill='#999', font=('TkDefaultFont', 6))
        self.create_text(3, self._sh - 6, anchor='w', text=f'{ylo:.3g}',
                         fill='#999', font=('TkDefaultFont', 6))


class Banner(tk.Frame):
    """AMBER/RED full-width banner with action buttons."""

    def __init__(self, master, level='amber'):
        bg = COLORS['amber_bg'] if level == 'amber' else COLORS['red_bg']
        fg = COLORS['amber_fg'] if level == 'amber' else COLORS['red_fg']
        super().__init__(master, bg=bg, padx=8, pady=6)
        self._fg, self._bg = fg, bg
        self.msg = tk.Label(self, text='', bg=bg, fg=fg,
                            font=('TkDefaultFont', 10, 'bold'),
                            wraplength=760, justify='left')
        self.msg.pack(side='left', fill='x', expand=True)
        self.btns = tk.Frame(self, bg=bg)
        self.btns.pack(side='right')

    def set_message(self, text):
        self.msg.config(text=text)

    def add_button(self, text, command):
        b = tk.Button(self.btns, text=text, command=command)
        b.pack(side='left', padx=3)
        return b


class ChecklistRow(tk.Frame):
    """PENDING / RUNNING / PASS / WARN / FIX / FAIL row for the
    pre-flight. WARN is advisory: it never disables Start."""

    STATES = {'PENDING': ('#666', '...'), 'RUNNING': ('#8a5a00', '>>'),
              'PASS': ('#2e7d32', 'OK'), 'WARN': ('#8a5a00', '!'),
              'FIX': ('#8a5a00', 'FIX'), 'FAIL': ('#a01010', 'X')}

    def __init__(self, master, label, action_text=None, action_cb=None):
        super().__init__(master)
        self.chip = tk.Label(self, text='...', width=4, fg='white',
                             bg='#666', font=('TkDefaultFont', 9,
                                              'bold'))
        self.chip.pack(side='left', padx=(0, 6))
        tk.Label(self, text=label, width=26, anchor='w',
                 font=('TkDefaultFont', 10)).pack(side='left')
        self.detail = tk.Label(self, text='', anchor='w', fg='#555',
                               wraplength=430, justify='left')
        self.detail.pack(side='left', fill='x', expand=True)
        self.action = None
        if action_text:
            self.action = tk.Button(self, text=action_text,
                                    command=action_cb, state='disabled')
            self.action.pack(side='right', padx=4)
        self.state_name = 'PENDING'

    def set(self, state, detail=''):
        color, chip = self.STATES.get(state, ('#666', '?'))
        self.chip.config(text=chip, bg=color)
        self.detail.config(text=detail)
        self.state_name = state
        if self.action:
            self.action.config(state='normal' if state == 'FIX'
                               else 'disabled')


class CollapsibleSection(tk.Frame):
    """'> Advanced' disclosure."""

    def __init__(self, master, title):
        super().__init__(master)
        self._open = False
        self._btn = tk.Button(self, text=f'> {title}', anchor='w',
                              relief='flat', fg=COLORS['ink'],
                              command=self.toggle)
        self._btn.pack(fill='x')
        self.body = tk.Frame(self)
        self._title = title

    def toggle(self):
        self._open = not self._open
        if self._open:
            self.body.pack(fill='both', expand=True, padx=12)
            self._btn.config(text=f'v {self._title}')
        else:
            self.body.pack_forget()
            self._btn.config(text=f'> {self._title}')


class ValidatedEntry(tk.Frame):
    """Entry + unit label; range-checked on focus-out; red outline +
    reason when invalid. The run cannot proceed while any is red."""

    def __init__(self, master, label, unit='', lo=None, hi=None,
                 default='', width=10, tooltip=None, on_change=None):
        super().__init__(master)
        self.lo, self.hi = lo, hi
        self.on_change = on_change
        tk.Label(self, text=label, anchor='w', width=22).pack(
            side='left')
        self.var = tk.StringVar(value=str(default))
        self.entry = tk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side='left')
        tk.Label(self, text=unit, fg='#555').pack(side='left', padx=3)
        self.err = tk.Label(self, text='', fg=COLORS['red_fg'],
                            font=('TkDefaultFont', 8))
        self.err.pack(side='left', padx=4)
        self.entry.bind('<FocusOut>', lambda e: self.validate())
        self.var.trace_add('write', lambda *a: self._changed())
        self.valid = True
        if tooltip:
            try:
                from ui_widgets import add_tooltip
                add_tooltip(self.entry, tooltip)
            except Exception:
                pass

    def _changed(self):
        if self.on_change:
            self.on_change()

    def value(self):
        try:
            return float(self.var.get())
        except ValueError:
            return None

    def validate(self):
        v = self.value()
        problem = ''
        if v is None:
            problem = 'not a number'
        elif self.lo is not None and v < self.lo:
            problem = f'min {self.lo:g}'
        elif self.hi is not None and v > self.hi:
            problem = f'max {self.hi:g}'
        self.valid = not problem
        self.err.config(text=problem)
        self.entry.config(highlightthickness=1 if problem else 0,
                          highlightbackground=COLORS['red_fg'],
                          highlightcolor=COLORS['red_fg'])
        if self.on_change:
            self.on_change()
        return self.valid
