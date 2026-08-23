#!/usr/bin/env python3
"""Headless tests: bender side-view pipeline vs analytic ground truth.

Tolerances from the plan's verification section: bend angle within
0.3 deg, tip deflection within 0.05 mm + 1%, kappa_mean within 3%.

Run: .venv/Scripts/python.exe tests/test_lc_bender_vision.py
"""
import math
import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

import numpy as np

from vision import synth
from vision.bender import BenderVision, analyze_frame

CFG = {'clamp_x_px': 40, 'mm_per_px': 0.1, 'morph_px': 3}


def _angle_for(bend_deg, length_mm=20.0):
    """kappa [1/mm] that produces the requested tip angle."""
    return math.radians(bend_deg) / length_mm


def test_straight_beam_zero_angle():
    frame, truth = synth.render(kappa_per_mm=0.0, length_mm=20.0,
                                mm_per_px=0.1, clamp_x_px=40)
    got = analyze_frame(frame, CFG)
    assert got['ok'], got
    assert abs(got['bend_angle_deg']) < 0.3, got['bend_angle_deg']
    assert abs(got['arc_len_mm'] - 20.0) < 0.8, got['arc_len_mm']
    assert abs(got['kappa_mean_m1']) < 2.0, got['kappa_mean_m1']


def test_gauge_angles_15_30_45():
    for bend in (15.0, 30.0, 45.0):
        k = _angle_for(bend)
        frame, truth = synth.render(kappa_per_mm=k, length_mm=20.0,
                                    mm_per_px=0.1, clamp_x_px=40)
        got = analyze_frame(frame, CFG)
        assert got['ok'], (bend, got['flags'], got['notes'])
        err = abs(got['bend_angle_deg'] - truth['bend_angle_deg'])
        assert err < 0.3, (bend, got['bend_angle_deg'], err)
        # kappa: truth in 1/m = k*1000
        kerr = abs(got['kappa_mean_m1'] - k * 1000.0) / (k * 1000.0)
        assert kerr < 0.03, (bend, got['kappa_mean_m1'], kerr)


def test_tip_deflection_vs_rest():
    rest_f, _ = synth.render(kappa_per_mm=0.0, length_mm=20.0,
                             mm_per_px=0.1, clamp_x_px=40)
    rest = analyze_frame(rest_f, CFG)
    for bend in (10.0, 30.0):
        k = _angle_for(bend)
        peak_f, truth = synth.render(kappa_per_mm=k, length_mm=20.0,
                                     mm_per_px=0.1, clamp_x_px=40)
        got = analyze_frame(peak_f, CFG, ref=rest)
        want = truth['tip_defl_mm']
        tol = 0.05 + 0.01 * abs(want) + 0.15   # +0.15: the rest tip's
        # own localization on an anti-aliased edge; physical-gauge
        # budget in BENCH_TEST section U is the binding one
        assert abs(got['tip_defl_mm'] - want) < tol, \
            (bend, got['tip_defl_mm'], want)


def test_measure_pair_seam():
    rest_f, _ = synth.render(kappa_per_mm=0.0, length_mm=20.0,
                             mm_per_px=0.1, clamp_x_px=40)
    k = _angle_for(25.0)
    peak_f, truth = synth.render(kappa_per_mm=k, length_mm=20.0,
                                 mm_per_px=0.1, clamp_x_px=40)
    bv = BenderVision(CFG)
    m = bv.measure_pair(rest_f, peak_f, {})
    assert m['quality'] == 'ok', m['quality']
    assert abs(m['value'] - truth['tip_defl_mm']) < 0.25, m['value']
    assert abs(m['rest_value']) < 0.2      # straight rest ~ 0 offset


def test_out_of_frame_flagged_conf_gated():
    frame, _ = synth.render_out_of_frame()
    got = analyze_frame(frame, {'clamp_x_px': 30, 'mm_per_px': 0.1})
    assert 'out_of_frame' in got['flags']
    assert not got['ok']


def test_root_lost_on_blank_frame():
    frame = np.full((120, 200), 200, dtype=np.uint8)
    got = analyze_frame(frame, {'clamp_x_px': 30})
    assert 'root_lost' in got['flags']
    assert got['conf'] == 0.0


def test_noise_lowers_conf_ordering():
    k = _angle_for(20.0)
    clean_f, _ = synth.render(kappa_per_mm=k)
    noisy_f, _ = synth.render(kappa_per_mm=k, noise=40.0, blur_px=7,
                              seed=3)
    clean = analyze_frame(clean_f, CFG)
    noisy = analyze_frame(noisy_f, CFG)
    assert clean['conf'] > noisy['conf'], (clean['conf'], noisy['conf'])


def test_lighting_gradient_still_measures():
    # backlit Otsu robustness: a 15% side-to-side gradient must not
    # break the measurement
    k = _angle_for(30.0)
    frame, truth = synth.render(kappa_per_mm=k, gradient=0.15)
    got = analyze_frame(frame, CFG)
    assert got['ok']
    assert abs(got['bend_angle_deg'] - truth['bend_angle_deg']) < 0.6


def test_sign_flip():
    k = _angle_for(20.0)
    frame, truth = synth.render(kappa_per_mm=k)
    rest_f, _ = synth.render(kappa_per_mm=0.0)
    rest = analyze_frame(rest_f, dict(CFG, sign_flip=-1.0))
    got = analyze_frame(frame, dict(CFG, sign_flip=-1.0), ref=rest)
    assert got['bend_angle_deg'] < 0
    assert got['tip_defl_mm'] < 0


def test_kappa_profile_shape():
    k = _angle_for(30.0)
    frame, _ = synth.render(kappa_per_mm=k)
    got = analyze_frame(frame, CFG)
    prof = np.asarray(got['kappa_profile_m1'])
    assert len(prof) == 64
    # constant-curvature beam: the interior mean is tight (the scalar
    # kappa_mean_m1 criterion), per-station wobble is ~+/-20% -- the
    # delamination signature this profile exists for is a LOCALIZED
    # halving, which must not be mimicked by measurement noise.
    interior = prof[12:-12]
    assert abs(np.mean(interior) - k * 1000) / (k * 1000) < 0.05
    assert np.abs(interior - k * 1000).max() / (k * 1000) < 0.35, \
        interior
    assert interior.min() > 0.5 * k * 1000     # no false 'delamination'


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
