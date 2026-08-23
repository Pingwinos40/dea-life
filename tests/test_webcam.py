#!/usr/bin/env python3
"""Headless tests for webcam.py pure logic (no camera needed).

focus_score needs numpy; the rest is stdlib-only. Run:
    .venv/bin/python tests/test_webcam.py
"""
# Runnable from anywhere: put the repo root (one level up) on sys.path
# so the app modules import when this file is executed directly.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
import datetime as dt

import webcam as wc


def test_frange_ascending():
    assert wc.frange(0, 5, 1) == [0, 1, 2, 3, 4, 5]
    assert wc.frange(0.0, 1.0, 0.25) == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_frange_descending():
    assert wc.frange(2, 0, -1) == [2, 1, 0]


def test_frange_single_point():
    assert wc.frange(3, 3, 1) == [3]


def test_frange_errors():
    for args in [(0, 5, 0), (0, 5, -1), (5, 0, 1)]:
        try:
            wc.frange(*args)
            assert False, f"expected ValueError for {args}"
        except ValueError:
            pass


def test_capture_filename_basic():
    assert wc.capture_filename('shot', 1) == 'shot_0001.png'
    assert wc.capture_filename('shot', 42, ext='jpg') == 'shot_0042.jpg'


def test_capture_filename_value_safe():
    # -2.5 V -> filename-safe token
    assert wc.capture_filename('v', 3, value=-2.5) == 'v_0003_m2p5V.png'
    assert wc.capture_filename('v', 3, value=1.0) == 'v_0003_1V.png'


def test_capture_filename_sanitises_prefix():
    out = wc.capture_filename('my run/#1', 0)
    assert '/' not in out and '#' not in out
    assert out.endswith('_0000.png')


def test_capture_filename_timestamp():
    ts = dt.datetime(2026, 6, 27, 14, 30, 5)
    out = wc.capture_filename('t', 1, ts=ts)
    assert out == 't_0001_20260627-143005.png'


def test_focus_score_flat_is_zero():
    flat = [[100] * 8 for _ in range(8)]
    assert wc.focus_score(flat) == 0.0


def test_focus_score_edges_higher_than_flat():
    flat = [[10] * 8 for _ in range(8)]
    checker = [[(0 if (i + j) % 2 == 0 else 255) for j in range(8)]
               for i in range(8)]
    assert wc.focus_score(checker) > wc.focus_score(flat)


def test_focus_score_noise_robust():
    # The bench bug (2026-07-20): a noisy defocused frame must NOT beat a
    # clean focused one. Correlated edges survive block-downsampling; the
    # per-pixel sensor noise averages away. Uses a realistic camera-scale
    # frame (the downsample engages at >= a few hundred px).
    import numpy as np
    rng = np.random.RandomState(0)
    n = 720
    sharp = np.zeros((n, n))
    sharp[:, n // 2:] = 255.0                       # one strong, real edge
    noisy_flat = np.clip(128 + rng.normal(0, 18, (n, n)), 0, 255)  # noise only
    assert wc.focus_score(sharp) > wc.focus_score(noisy_flat), \
        "noise must not out-score a real edge"


def test_focus_score_center_weighted():
    # An edge in the CENTER should score higher than the same feature shoved
    # into a corner, because the metric is weighted to the middle.
    import numpy as np
    n = 720
    center = np.zeros((n, n))
    center[n // 2 - 60:n // 2 + 60, n // 2 - 60:n // 2 + 60] = 255.0
    corner = np.zeros((n, n))
    corner[:120, :120] = 255.0
    assert wc.focus_score(center) > wc.focus_score(corner)


def test_focus_score_rejects_tiny():
    try:
        wc.focus_score([[1, 2], [3, 4]])
        assert False, "expected ValueError for 2x2"
    except ValueError:
        pass


def test_deps_available_returns_tuple():
    ok, reason = wc.deps_available()
    assert isinstance(ok, bool) and isinstance(reason, str)


def test_list_cameras_returns_list():
    assert isinstance(wc.list_cameras(max_index=2), list)


def test_ordinary_webcams_carry_their_device_path():
    """A cv2-captured webcam still has V4L2 controls. Without the device
    path there is nothing to re-stamp the locked exposure onto, so the
    firmware picked the exposure on every open and the SLDEA tab's
    exposure/gain fields were silently ignored (2026-08-05: the
    carbon-black run's baseline came out saturated, median 255)."""
    real_v4l2, real_exists, real_bayer = (wc.v4l2_available,
                                          _os.path.exists, wc.bayer_format)
    try:
        wc.v4l2_available = lambda: True
        _os.path.exists = lambda p: True
        wc.bayer_format = lambda dev: None          # ordinary webcam
        spec = wc.resolve_camera(0)
        assert spec['kind'] == 'cv2' and spec['index'] == 0
        assert spec['device'] == '/dev/video0', spec
        # no v4l2 on this host -> no device path, and nothing breaks
        wc.v4l2_available = lambda: False
        bare = wc.resolve_camera(0)
        assert bare['kind'] == 'cv2' and 'device' not in bare, bare
    finally:
        wc.v4l2_available, wc.bayer_format = real_v4l2, real_bayer
        _os.path.exists = real_exists


def test_locked_controls_are_restamped_before_every_grab():
    """The lock has to be re-applied per grab, not once in advance: the
    DFK's firmware auto-gain walks a written value back within ~0.5 s
    (bench 2026-07-24), so a lock set half a second early is a lock the
    firmware has time to undo before the shutter."""
    calls = []
    real_apply, real_locked = wc.apply_locked, dict(wc.LOCKED_CONTROLS)
    try:
        wc.apply_locked = lambda dev, exclude=None: calls.append(dev)
        # index 999 will not open; the point is the stamp happened first
        wc.oneshot_rgb({'kind': 'cv2', 'index': 999,
                        'device': '/dev/video9'}, count=1)
        assert calls == ['/dev/video9'], calls
        calls.clear()
        wc.oneshot_rgb({'kind': 'cv2', 'index': 999}, count=1)
        assert calls == [], calls
    finally:
        wc.apply_locked = real_apply
        wc.set_locked(real_locked)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == '__main__':
    _run()
