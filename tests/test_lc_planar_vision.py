#!/usr/bin/env python3
"""Headless tests: planar adapter over the vendored sldea_edge engine.

Synthetic disc-on-paper frames in the vendored detector's own luminance
regime (P3 baselines: disc 162-167 vs paper 176-190).

Run: .venv/Scripts/python.exe tests/test_lc_planar_vision.py
"""
import math
import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

import numpy as np

from vision.planar import PlanarVision


def _disc_frame(r_px, size=480, disc=163.0, paper=184.0, dark_ring=0.0,
                seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    d2 = (yy - size / 2) ** 2 + (xx - size / 2) ** 2
    img = np.full((size, size), paper, dtype=np.float32)
    img[d2 <= r_px * r_px] = disc
    if dark_ring:
        ring = (d2 > r_px * r_px) & (d2 <= (r_px + dark_ring) ** 2)
        img[ring] = disc - 8.0
    img += rng.normal(0, 1.2, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def test_rest_area_from_disc():
    pv = PlanarVision({'diam_mm': 16.0})
    rest = _disc_frame(120)
    peak = _disc_frame(120, seed=1)          # same size: gated no-change
    m = pv.measure_pair(rest, peak, {})
    # scale anchors on the nominal 16 mm: resting area = pi*8^2
    want = math.pi * 8.0 ** 2
    assert m['rest_value'] is not None, m['notes']
    assert abs(m['rest_value'] - want) < 0.05 * want, m


def test_expansion_measured():
    pv = PlanarVision({'diam_mm': 16.0})
    rest = _disc_frame(120)
    peak = _disc_frame(138, seed=2)          # ~32% area expansion
    m = pv.measure_pair(rest, peak, {})
    assert m['value'] is not None, m['notes']
    ratio = m['value'] / m['rest_value']
    want = (138.0 / 120.0) ** 2
    assert abs(ratio - want) < 0.10, (ratio, want, m['notes'])


def test_refusal_is_honest():
    pv = PlanarVision({'diam_mm': 16.0})
    blank = np.full((480, 480), 200, dtype=np.uint8)
    m = pv.measure_pair(blank, blank, {})
    assert m['value'] is None
    assert 'refused' in m['notes']


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
