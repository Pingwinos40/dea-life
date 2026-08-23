#!/usr/bin/env python3
"""Keep Tk from crashing on colour-emoji fonts.

Import this BEFORE tkinter creates a connection, in every process that
opens a Tk window:

    import tk_fontfix; tk_fontfix.apply()

Why: Tk uploads glyphs via RENDER/RenderAddGlyphs. Colour bitmap fonts
(NotoColorEmoji and friends) overflow that request, the X server returns
BadLength, and because that is an X PROTOCOL error Xlib aborts the whole
process -- the app dies the instant an emoji is drawn, with only
"BadLength ... RenderAddGlyphs" on stderr. Bench report 2026-07-27: the
GUI died at startup over forwarded X (display :10.0) on RHEL9.

Fix: point fontconfig at a config that includes the system one and
rejects the colour emoji families, so the monochrome outline font serves
those codepoints instead. Process-local (an env var) -- nothing on the
machine is modified, and other users/apps are unaffected.
"""
import os

CONF_NAME = 'fonts.conf'


def _candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, 'deploy', CONF_NAME)
    yield os.path.join(here, CONF_NAME)


def apply(force=False):
    """Set FONTCONFIG_FILE to the bundled config. Returns the path used,
    or None when it was skipped (already set, or config not found)."""
    if not force and os.environ.get('FONTCONFIG_FILE'):
        return None                      # someone configured this already
    if os.name != 'posix':
        return None                      # Windows/macOS use other backends
    for path in _candidates():
        if os.path.exists(path):
            os.environ['FONTCONFIG_FILE'] = path
            return path
    return None
