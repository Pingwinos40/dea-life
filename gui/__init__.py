"""Tkinter GUI. Importing this package injects lib/ (via core) and
applies the vendored tk_fontfix BEFORE tkinter ever loads -- color-emoji
glyphs hard-crash Tk over X on the bench (vendored rule, bench report
2026-07-27). No-op on Windows."""
import core  # noqa: F401  (lib/ path injection)

import tk_fontfix

tk_fontfix.apply()
