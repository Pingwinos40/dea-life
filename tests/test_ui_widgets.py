#!/usr/bin/env python3
"""ScrollableTab scrolling rules (`#225`).

Tabs had a vertical scrollbar but no horizontal one, and
`_on_canvas_configure` pinned the content's width to the canvas's width --
so content wider than the window was squeezed and clipped rather than
scrollable, with no way to reach it at all. The two decisions that fix it
are pure functions (`content_width`, `needs_hscroll`) and are tested
headlessly; the wiring that applies them drives a real ScrollableTab and
skips cleanly when no display can be opened.

The width pinning is load-bearing -- it is what makes tab frames fill the
window at normal widths -- so "stretch is preserved" is asserted just as
hard as "wide content scrolls".

Run: .venv/bin/python tests/test_ui_widgets.py
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))

from ui_widgets import content_width, needs_hscroll


# ---- the two decisions, headless ---------------------------------------

def test_content_width_stretches_narrow_content_to_the_canvas():
    """The load-bearing half: at comfortable widths the tab frame must
    still fill the window, which is why the pinning existed."""
    assert content_width(1200, 700) == 1200
    assert content_width(1200, 1199) == 1200
    assert content_width(1200, 1200) == 1200


def test_content_width_lets_wide_content_overflow():
    """The bug: `width=event.width` unconditionally, so 460 px of the DC
    Supply tab was squeezed off and unreachable at a 700 px window."""
    assert content_width(659, 1119) == 1119
    assert content_width(659, 688) == 688
    assert content_width(0, 500) == 500


def test_needs_hscroll_only_when_something_is_off_screen():
    """A permanent h-bar on every tab would be its own annoyance."""
    assert needs_hscroll(659, 1119) is True
    assert needs_hscroll(1109, 1119) is True       # 10 px still counts
    assert needs_hscroll(1200, 700) is False
    assert needs_hscroll(1200, 1200) is False      # exact fit: no bar


def test_the_two_decisions_agree():
    """Whenever the bar is shown there must be something to scroll to,
    and whenever it is hidden the content must exactly fill the canvas."""
    for canvas in (1, 200, 659, 1109, 1600):
        for req in (0, 199, 476, 688, 1119, 2000):
            wide = needs_hscroll(canvas, req)
            width = content_width(canvas, req)
            if wide:
                assert width > canvas, (canvas, req)
            else:
                assert width == canvas, (canvas, req)


def test_needs_hscroll_cannot_oscillate():
    """Showing the bar costs vertical space, never horizontal -- so the
    decision taken at a given canvas width is stable when re-evaluated."""
    for canvas in (300, 659, 1109):
        for req in (200, 659, 660, 1119):
            first = needs_hscroll(canvas, req)
            assert needs_hscroll(canvas, req) is first


# ---- the wiring, on a real widget --------------------------------------

def _tk_root():
    """A withdrawn root, or None when there is no display."""
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"   (skipped: no display for Tk: {e})")
        return None
    root.withdraw()
    return root


def _settle(root, n=5):
    for _ in range(n):
        root.update_idletasks()
        root.update()


def _tab_with_content(root, width, height, canvas_w=600, canvas_h=400):
    """A mapped ScrollableTab in a notebook, carrying one fixed-size block."""
    import tkinter as tk
    from tkinter import ttk
    from ui_widgets import ScrollableTab
    root.geometry(f'{canvas_w}x{canvas_h}')
    root.deiconify()
    nb = ttk.Notebook(root)
    nb.pack(fill='both', expand=True)
    tab = ScrollableTab(nb)
    nb.add(tab, text='t')
    block = tk.Frame(tab.body, width=width, height=height)
    block.pack_propagate(False)
    block.pack()
    _settle(root, 8)
    return nb, tab


def test_narrow_content_gets_no_bar_and_still_stretches():
    root = _tk_root()
    if root is None:
        return
    try:
        _nb, tab = _tab_with_content(root, 200, 100)
        cw = tab._canvas.winfo_width()
        assert not tab._hbar.winfo_ismapped(), "h-bar shown for content that fits"
        assert tab.body.winfo_width() == cw, (tab.body.winfo_width(), cw)
        assert tab._canvas.xview() == (0.0, 1.0), tab._canvas.xview()
    finally:
        root.destroy()


def test_wide_content_gets_a_bar_and_becomes_reachable():
    root = _tk_root()
    if root is None:
        return
    try:
        _nb, tab = _tab_with_content(root, 1400, 100)
        cv = tab._canvas
        cw = cv.winfo_width()
        assert tab._hbar.winfo_ismapped(), "no h-bar for content wider than the tab"
        assert tab.body.winfo_width() >= 1400, tab.body.winfo_width()
        assert cv.xview()[1] < 1.0, cv.xview()
        # the far right must actually come into view
        cv.xview_moveto(1.0)
        _settle(root, 4)
        right = cv.bbox('all')[2] - int(cv.canvasx(0))
        assert right <= cw + 1, (right, cw)
    finally:
        root.destroy()


def test_the_bar_appears_and_disappears_across_the_threshold():
    """Resizing past the point where content stops fitting must flip the
    bar both ways -- not just on."""
    root = _tk_root()
    if root is None:
        return
    try:
        _nb, tab = _tab_with_content(root, 800, 100, canvas_w=1200)
        assert not tab._hbar.winfo_ismapped(), "wide window should need no bar"
        root.geometry('500x400')
        _settle(root, 8)
        assert tab._hbar.winfo_ismapped(), "narrow window should show the bar"
        root.geometry('1200x400')
        _settle(root, 8)
        assert not tab._hbar.winfo_ismapped(), "bar stayed after it fit again"
    finally:
        root.destroy()


def test_vertical_scrolling_is_unregressed():
    """The v-bar existed and worked; the h-scroll work must not disturb
    it, and the scrollregion must still follow content built late."""
    root = _tk_root()
    if root is None:
        return
    try:
        import tkinter as tk
        _nb, tab = _tab_with_content(root, 200, 1200, canvas_h=400)
        cv = tab._canvas
        assert cv.yview()[1] < 1.0, cv.yview()
        cv.yview_moveto(1.0)
        _settle(root, 4)
        assert cv.yview()[1] >= 0.999, cv.yview()
        # content added after the tab was built still extends the region
        tall = int(cv.cget('scrollregion').split()[3])
        extra = tk.Frame(tab.body, width=100, height=300)
        extra.pack_propagate(False)
        extra.pack()
        _settle(root, 8)
        assert int(cv.cget('scrollregion').split()[3]) > tall
    finally:
        root.destroy()


def test_horizontal_keyboard_and_shift_wheel_are_bound():
    """`#225`: Up/Down/PageUp/PageDown had no Left/Right equivalents, and
    there was no pointer route sideways at all."""
    root = _tk_root()
    if root is None:
        return
    try:
        _nb, tab = _tab_with_content(root, 1400, 100)
        for key in ('<Left>', '<Right>'):
            assert tab._canvas.bind(key), f"{key} not bound"
        tab._bind_wheel()
        bound = set(tab._canvas.bind_all())
        # Shift+wheel is the cross-platform route and must always be there.
        # The X11 tilt wheel (buttons 6/7) is best-effort: Windows Tk knows
        # buttons 1-5 only and raises on the rest, so binding it is allowed
        # to fail -- but it must never take the whole binding pass down.
        for seq in ('<Shift-MouseWheel>', '<Shift-Button-4>',
                    '<Shift-Button-5>'):
            assert seq in bound, f"{seq} not bound for horizontal scrolling"
        assert '<MouseWheel>' in bound, "vertical wheel lost"

        class _Ev:
            def __init__(self, delta, num=0):
                self.delta, self.num, self.state = delta, num, 1

        cv = tab._canvas
        # the tilt-wheel button numbers are handled whether or not this
        # platform can deliver them
        cv.xview_moveto(0.0)
        _settle(root, 3)
        tab._wheel_x(_Ev(0, num=7))
        _settle(root, 3)
        assert cv.xview()[0] > 0.0, "button-7 (tilt right) did not scroll"
        tab._wheel_x(_Ev(0, num=6))
        _settle(root, 3)
        assert cv.xview()[0] == 0.0, "button-6 (tilt left) did not scroll back"

        cv.xview_moveto(0.0)
        _settle(root, 3)
        start = cv.xview()[0]
        for _ in range(4):
            tab._wheel_x(_Ev(-120))
        _settle(root, 3)
        assert cv.xview()[0] > start, (start, cv.xview())
        for _ in range(8):
            tab._wheel_x(_Ev(120))
        _settle(root, 3)
        assert cv.xview()[0] == 0.0, cv.xview()
        tab._unbind_wheel()
    finally:
        root.destroy()


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == '__main__':
    _run()
