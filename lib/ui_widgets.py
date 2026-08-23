#!/usr/bin/env python3
"""Small reusable Tk UI helpers: hover tooltips and scrollable tabs.

Both address long-standing GUI complaints (2026-07-10): content taller
than the window was simply CUT OFF with no scrollbar, and none of the
controls explained themselves (e.g. the LCR Speed/Avg fields).
"""
import tkinter as tk
from tkinter import ttk


class Tooltip:
    """Show `text` in a small popup after hovering `widget` for `delay` ms.

    Tk has no built-in tooltip; this is the standard Toplevel +
    overrideredirect pattern. Hides on leave/click/destroy.
    """

    def __init__(self, widget, text, delay=650, wraplength=340):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wraplength = wraplength
        self._after_id = None
        self._tip = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')
        widget.bind('<Destroy>', self._hide, add='+')

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 14
            y = (self.widget.winfo_rooty()
                 + self.widget.winfo_height() + 6)
        except tk.TclError:      # widget died while the timer was pending
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f'+{x}+{y}')
        tk.Label(tip, text=self.text, justify='left',
                 wraplength=self.wraplength, bg='#ffffe0', fg='black',
                 relief='solid', borderwidth=1, padx=7, pady=5).pack()
        self._tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def add_tooltip(widget, text):
    """Attach a hover tooltip; returns the widget for chaining."""
    Tooltip(widget, text)
    return widget


class SplashScreen(tk.Toplevel):
    """Borderless "starting up" window shown while the tabs are built.

    Building the six tabs takes ~2.6 s on the bench PC (measured), during
    which nothing appeared on screen at all -- a user double-clicking the
    desktop icon had no idea anything was happening. This paints
    immediately and reports each phase, then goes away.
    """

    W, H = 460, 170

    def __init__(self, root, version_text=''):
        super().__init__(root)
        self.overrideredirect(True)          # no title bar / decorations
        self.configure(bg='#1f3a5f')
        # centre on screen (the main window is not sized yet)
        x = (self.winfo_screenwidth() - self.W) // 2
        y = (self.winfo_screenheight() - self.H) // 3
        self.geometry(f'{self.W}x{self.H}+{x}+{y}')

        frame = tk.Frame(self, bg='#1f3a5f', padx=24, pady=18)
        frame.pack(fill='both', expand=True)
        # Name transition (2026-07-20): "SCPI Control" -> "Digital
        # Multitool", starting here on the splash. Same look, new name.
        tk.Label(frame, text='Digital Multitool', bg='#1f3a5f', fg='white',
                 font=('TkDefaultFont', 17, 'bold')).pack(anchor='w')
        tk.Label(frame, text='Lab instrument control suite', bg='#1f3a5f',
                 fg='#b8c7dc', font=('TkDefaultFont', 10)).pack(anchor='w')

        self._status = tk.Label(frame, text='Starting...', bg='#1f3a5f',
                                fg='#e6edf5', font=('TkDefaultFont', 10),
                                anchor='w')
        self._status.pack(anchor='w', pady=(14, 4), fill='x')
        self._bar = ttk.Progressbar(frame, mode='indeterminate', length=400)
        self._bar.pack(fill='x')
        self._bar.start(12)
        if version_text:
            tk.Label(frame, text=version_text, bg='#1f3a5f', fg='#8fa4bd',
                     font=('TkDefaultFont', 8)).pack(anchor='e',
                                                     pady=(8, 0))
        self.update_idletasks()
        self.update()                         # paint before the slow work

    def set_status(self, text):
        """Update the phase line and repaint (called from the build loop)."""
        try:
            self._status.config(text=text)
            self.update_idletasks()
            self.update()
        except tk.TclError:                   # already closed
            pass

    def close(self):
        try:
            self._bar.stop()
            self.destroy()
        except tk.TclError:
            pass


def content_width(canvas_width, content_reqwidth):
    """Width to give a ScrollableTab's content inside a canvas that wide.

    Stretching the content to the canvas width is load-bearing: it is what
    makes every tab frame fill the window at comfortable widths. Doing it
    UNCONDITIONALLY was the `#225` bug -- content wider than the window got
    squeezed and clipped instead of becoming scrollable, with no way to
    reach it. Taking the max keeps the stretch and lets genuinely wide
    content overflow into scrollable territory.
    """
    return max(int(canvas_width), int(content_reqwidth))


def needs_hscroll(canvas_width, content_reqwidth):
    """Should the horizontal scrollbar be on screen?

    Only when something is actually off to the right -- a permanent h-bar
    under every tab would be its own annoyance. Strict `>`: showing the bar
    costs VERTICAL space and never horizontal, so the decision cannot
    oscillate the way a conditional v-bar would.
    """
    return int(content_reqwidth) > int(canvas_width)


class ScrollableTab(ttk.Frame):
    """Notebook tab with scrollbars when content doesn't fit the window.

    Build the tab's content into `.body` instead of the tab itself.

    Vertical: the bar is always there and scrolls when content is taller
    than the window -- the original cut-off case.
    Horizontal (`#225`): the bar appears ONLY when content is genuinely
    wider than the canvas. Wide content used to be pinned to the canvas
    width and clipped, unreachable by any means; Shift+wheel and
    Left/Right now reach it.
    """

    def __init__(self, notebook):
        super().__init__(notebook)
        bg = ttk.Style().lookup('TFrame', 'background') or None
        self._canvas = tk.Canvas(self, highlightthickness=0,
                                 **({'bg': bg} if bg else {}))
        vbar = ttk.Scrollbar(self, orient='vertical',
                             command=self._canvas.yview)
        self._hbar = ttk.Scrollbar(self, orient='horizontal',
                                   command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=vbar.set,
                               xscrollcommand=self._hbar.set)
        # grid rather than pack: the h-bar has to come and go underneath the
        # canvas without disturbing the v-bar, and grid_remove()/grid()
        # restores its slot exactly. Nothing outside this class is placed in
        # the tab frame itself -- callers only ever touch `.body`.
        self._canvas.grid(row=0, column=0, sticky='nsew')
        vbar.grid(row=0, column=1, sticky='ns')
        self._hbar.grid(row=1, column=0, sticky='ew')
        self._hbar.grid_remove()             # on demand only
        self._hbar_shown = False
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.body = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self.body,
                                               anchor='nw')
        self.body.bind('<Configure>', self._on_body_configure)
        self._canvas.bind('<Configure>', self._on_canvas_configure)
        # keyboard scrolling (audit 2026-07-25: tabs were mouse-wheel only)
        self._canvas.configure(takefocus=1)
        for key, n in (('<Up>', -1), ('<Down>', 1),
                       ('<Prior>', -5), ('<Next>', 5)):
            self._canvas.bind(key,
                              lambda e, n=n: self._canvas.yview_scroll(
                                  n, 'units'))
        for key, n in (('<Left>', -1), ('<Right>', 1)):
            self._canvas.bind(key,
                              lambda e, n=n: self._canvas.xview_scroll(
                                  n, 'units'))
        # Mouse wheel scrolls whichever tab the pointer is over. bind_all
        # is grabbed on Enter and released on Leave so tabs don't fight.
        self.bind('<Enter>', self._bind_wheel)
        self.bind('<Leave>', self._unbind_wheel)

    # ---- layout ---------------------------------------------------------

    def _refit(self, canvas_w=None):
        """Re-apply the content width and the h-bar show/hide decision."""
        if canvas_w is None:
            canvas_w = self._canvas.winfo_width()
        if canvas_w <= 1:
            return              # not laid out yet -- nothing to decide on
        need = self.body.winfo_reqwidth()
        self._canvas.itemconfigure(self._win,
                                   width=content_width(canvas_w, need))
        show = needs_hscroll(canvas_w, need)
        if show != self._hbar_shown:
            (self._hbar.grid if show else self._hbar.grid_remove)()
            self._hbar_shown = show

    def _on_body_configure(self, _event):
        # Content changed size: the scrollregion AND the width / h-bar
        # decision both follow it, so a tab that grows at runtime does not
        # keep the width it was born with.
        self._canvas.configure(scrollregion=self._canvas.bbox('all'))
        self._refit()

    def _on_canvas_configure(self, event):
        # event.width, not winfo_width(): during <Configure> the widget may
        # still report its previous size.
        self._refit(event.width)

    # ---- pointer --------------------------------------------------------

    def _wheel(self, event):
        if event.num == 4 or event.delta > 0:
            self._canvas.yview_scroll(-2, 'units')
        elif event.num == 5 or event.delta < 0:
            self._canvas.yview_scroll(2, 'units')

    def _wheel_x(self, event):
        """Shift+wheel (and an X11 tilt wheel) scrolls sideways -- the
        platform convention, and the pointer route to off-screen content."""
        if event.num in (4, 6) or event.delta > 0:
            self._canvas.xview_scroll(-2, 'units')
        elif event.num in (5, 7) or event.delta < 0:
            self._canvas.xview_scroll(2, 'units')

    _WHEEL_SEQS = (
        ('<Button-4>', '_wheel'),            # X11 up
        ('<Button-5>', '_wheel'),            # X11 down
        ('<MouseWheel>', '_wheel'),          # other OSes
        # Horizontal. The Shift- patterns are more specific than the plain
        # ones above, so Tk prefers them while Shift is held.
        ('<Shift-Button-4>', '_wheel_x'),
        ('<Shift-Button-5>', '_wheel_x'),
        ('<Shift-MouseWheel>', '_wheel_x'),
        ('<Button-6>', '_wheel_x'),          # X11 tilt wheel left
        ('<Button-7>', '_wheel_x'),          # X11 tilt wheel right
    )

    def _bind_wheel(self, _event=None):
        for seq, handler in self._WHEEL_SEQS:
            try:
                self._canvas.bind_all(seq, getattr(self, handler))
            except tk.TclError:
                # Windows Tk knows buttons 1-5 only and rejects the X11
                # tilt wheel outright. Bind what the platform accepts.
                pass

    def _unbind_wheel(self, _event=None):
        for seq, _handler in self._WHEEL_SEQS:
            try:
                self._canvas.unbind_all(seq)
            except tk.TclError:
                pass
