#!/usr/bin/env python3
"""Headless tests for waveform_render (no Tk, no instrument).

Run: .venv/bin/python tests/test_waveform_render.py
"""
# Runnable from anywhere: put the repo root (one level up) on sys.path
# so the app modules import when this file is executed directly.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from waveform_render import unit_waveform, scale_waveform

PPP = 200  # points per period used throughout


def _one(wave, **kw):
    return unit_waveform(wave, n_periods=1, points_per_period=PPP, **kw)


def test_sine_bounds_and_zero_start():
    y = _one('SINE')
    assert max(y) <= 1.0 and min(y) >= -1.0
    assert abs(y[0]) < 1e-9                  # sin(0) = 0
    assert abs(max(y) - 1.0) < 0.01          # reaches ~+1
    assert abs(min(y) + 1.0) < 0.01          # reaches ~-1


def test_square_duty():
    y = _one('SQUARE', duty_pct=30.0)
    high = sum(1 for v in y if v > 0)
    assert abs(high / PPP - 0.30) < 0.02     # ~30% of period high
    assert set(y) == {1.0, -1.0}             # no intermediate levels


def test_ramp_symmetry():
    y = _one('RAMP', sym_pct=25.0)
    peak_idx = y.index(max(y))
    assert abs(peak_idx / PPP - 0.25) < 0.02  # peak at 25% of period
    assert abs(y[0] + 1.0) < 0.05             # starts at trough

    saw = _one('RAMP', sym_pct=100.0)         # pure rising sawtooth
    assert saw[0] == -1.0
    assert saw[-1] > 0.9


def test_pulse_plateau_and_edges():
    y = _one('PULSE', duty_pct=40.0, rise_frac=0.05, fall_frac=0.05)
    # Centre of the high time is a flat +1 plateau
    assert y[int(0.20 * PPP)] == 1.0
    # Past the duty fraction it is low
    assert y[int(0.60 * PPP)] == -1.0
    # Mid-rising-edge is an intermediate level
    mid_rise = y[int(0.025 * PPP)]
    assert -1.0 < mid_rise < 1.0


def test_dc_and_noise():
    assert set(_one('DC')) == {0.0}
    n1 = _one('NOISE', seed=42)
    n2 = _one('NOISE', seed=42)
    assert n1 == n2                          # deterministic per seed
    assert max(n1) <= 1.0 and min(n1) >= -1.0
    assert len(set(n1)) > PPP // 2           # actually noisy


def test_arb_tiling_and_normalisation():
    samples = [0.0, 2.0, 0.0, -2.0]          # |max| = 2 -> normalised to 1
    y = unit_waveform('ARB', n_periods=2, points_per_period=PPP,
                      samples=samples)
    assert len(y) == 2 * PPP
    assert max(y) == 1.0 and min(y) == -1.0
    assert y[:PPP] == y[PPP:]                # period 2 repeats period 1

    flat = _one('ARB', samples=None)         # no samples -> flat line
    assert set(flat) == {0.0}


def test_phase_shift():
    base = _one('SINE')
    shifted = _one('SINE', phase_deg=90.0)
    assert abs(shifted[0] - 1.0) < 0.01      # sin(90 deg) = 1


def test_scale_waveform():
    y = scale_waveform([1.0, 0.0, -1.0], amp_vpp=4.0, offset_v=1.0)
    assert y == [3.0, 1.0, -1.0]             # +/-2 V around +1 V offset


def test_unknown_waveform_is_flat():
    assert set(_one('BOGUS')) == {0.0}


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(tests)} waveform-render tests passed.")
