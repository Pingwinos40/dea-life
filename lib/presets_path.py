#!/usr/bin/env python3
"""Where presets/profiles/arbs live, with a fallback for a dead share.

The launchers run the GUI with the working directory ON the ShareDrive so
the relative ``presets/`` path is shared between bench users. When the NAS
drops out, every write to it raises OSError -- on 2026-07-20 that turned a
"Save to Library" click into an unhandled traceback for the second user
(``OSError: [Errno 112] Host is down: 'presets'``), losing their waveform.

Writes now degrade to a per-user local directory and say so, instead of
failing; reads prefer the shared copy and fall back to the local one.

Headless self-test: .venv/bin/python tests/test_presets_path.py
"""
import os

# Per-user, always-local landing zone used when the share is unreachable.
# Module-level so tests can point it at a temp dir.
# VENDOR DELTA (the only one in lib/): appname 'scpi_control' ->
# 'sldea_lifecycle' so this app's local fallbacks never collide with the
# Digital Multitool's. See lib/VENDOR.md.
LOCAL_FALLBACK = os.path.join(os.path.expanduser('~'), '.local', 'share',
                              'sldea_lifecycle', 'presets')
# Second-chance fallback: the root-run desktop installer left
# ~/.local/share/scpi_control root-owned in one user's home (same failure
# the camera settings hit, webcam.py 2026-07-24). ~/.cache/<appname> is
# created BY the user at every launch, so it is always writable.
LOCAL_FALLBACK2 = os.path.join(os.path.expanduser('~'), '.cache',
                               'sldea_lifecycle', 'presets')

_note = None          # human-readable description of the last fallback


def fallback_note():
    """Message about the most recent fallback, or None. The GUI shows this
    in the status bar so a local save is never silent."""
    return _note


def clear_note():
    global _note
    _note = None


def local_mirror(path, root=None, base=None):
    """`path` remapped into the local fallback (`base`, default
    LOCAL_FALLBACK).

    With `root` (the configured presets directory) the layout underneath it
    is preserved, so presets/arb/<name>.csv mirrors to
    <fallback>/arb/<name>.csv rather than collapsing into the top level.
    """
    tail = os.path.basename(os.path.normpath(path))
    if root:
        try:
            rel = os.path.relpath(path, root)
            if not rel.startswith(os.pardir) and rel != os.curdir:
                tail = rel
        except ValueError:      # different drives on Windows
            pass
    return os.path.join(base or LOCAL_FALLBACK, tail)


def writable_path(path, is_dir=False, root=None):
    """`path` if its location accepts writes, else a local mirror of it.

    Probes with a real create+delete because an unreachable CIFS mount
    fails at write time, not at os.path.exists() time. Only raises if the
    LOCAL fallback is also unusable, which means the machine itself is
    broken.
    """
    global _note
    target = path if is_dir else (os.path.dirname(path) or '.')
    probe = os.path.join(target, '.scpi-write-probe')
    try:
        os.makedirs(target, exist_ok=True)
        with open(probe, 'w'):
            pass
        os.remove(probe)
        return path
    except OSError as e:
        # Try each local fallback in turn: the primary can itself be
        # unwritable (root-owned installer artefact — audit 2026-07-25;
        # the unguarded makedirs here used to escape and kill the save).
        last = e
        for base in (LOCAL_FALLBACK, LOCAL_FALLBACK2):
            alt = local_mirror(path, root, base=base)
            alt_dir = alt if is_dir else os.path.dirname(alt)
            try:
                os.makedirs(alt_dir, exist_ok=True)
                probe2 = os.path.join(alt_dir, '.scpi-write-probe')
                with open(probe2, 'w'):
                    pass
                os.remove(probe2)
            except OSError as e2:
                last = e2
                continue
            _note = (f"Shared drive unavailable ({e.strerror or e}) -- "
                     f"saved to the local copy in {alt_dir}")
            return alt
        raise last


def readable_path(path, root=None):
    """`path` when it is reachable, else its local mirror when that exists.

    Falls back to returning `path` unchanged so callers keep their normal
    "missing file" handling.
    """
    try:
        if os.path.exists(path):
            return path
    except OSError:
        pass
    alt = local_mirror(path, root)
    try:
        if os.path.exists(alt):
            return alt
    except OSError:
        pass
    return path


def listable_dir(path, root=None):
    """`path` if it can be listed, else its local mirror if that can be."""
    for cand in (path, local_mirror(path, root)):
        try:
            if os.path.isdir(cand):
                os.listdir(cand)
                return cand
        except OSError:
            continue
    return path
