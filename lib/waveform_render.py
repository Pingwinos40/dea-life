#!/usr/bin/env python3
"""
Pure-math waveform sample generation for the GUI preview (no Tk, no instrument).

unit_waveform() returns samples in [-1, +1] at unit amplitude over a number of
periods; scale_waveform() maps them to volts given amplitude (Vpp) and offset.
The GUI draws the result on a Canvas; keeping the math here makes the shapes
(duty, symmetry, rise/fall, phase) testable headless.
"""
import math
import random


def unit_waveform(waveform, n_periods=3, points_per_period=200,
                  duty_pct=50.0, sym_pct=50.0,
                  rise_frac=0.0, fall_frac=0.0,
                  phase_deg=0.0, samples=None, seed=0):
    """Generate unit-amplitude samples for a waveform preview.

    waveform          one of BK4055B.WAVEFORMS (SINE/SQUARE/RAMP/PULSE/NOISE/
                      ARB/DC); unknown types render as a flat line
    n_periods         periods to render
    points_per_period samples per period
    duty_pct          SQUARE/PULSE high fraction, percent of period
    sym_pct           RAMP rising fraction, percent of period
    rise_frac/fall_frac  PULSE edge times as a fraction of the period (the GUI
                      converts rise/fall seconds via rise_s * freq); clamped so
                      the edges fit inside the high time
    phase_deg         start-phase shift (SINE/SQUARE/RAMP/ARB)
    samples           ARB only: one period of samples to tile
    seed              NOISE only: makes the pseudo-noise deterministic

    Returns a list of n_periods * points_per_period floats in [-1, +1].
    """
    waveform = (waveform or '').upper()
    n = max(1, int(n_periods)) * max(2, int(points_per_period))
    shift = (phase_deg / 360.0) % 1.0

    if waveform == 'NOISE':
        rng = random.Random(seed)
        return [rng.uniform(-1.0, 1.0) for _ in range(n)]

    if waveform == 'DC':
        return [0.0] * n  # level comes entirely from the offset scaling

    if waveform == 'ARB':
        if not samples:
            return [0.0] * n
        peak = max(abs(s) for s in samples) or 1.0
        period = [s / peak for s in samples]
        out = []
        for i in range(n):
            frac = (i / points_per_period + shift) % 1.0
            out.append(period[int(frac * len(period)) % len(period)])
        return out

    duty = min(max(duty_pct, 0.0), 100.0) / 100.0
    sym = min(max(sym_pct, 0.0), 100.0) / 100.0

    out = []
    for i in range(n):
        t = (i / points_per_period + shift) % 1.0  # position within period
        out.append(unit_sample(waveform, t, duty, sym, rise_frac, fall_frac))
    return out


def unit_sample(waveform, t, duty=0.5, sym=0.5, rise_frac=0.0, fall_frac=0.0):
    """One unit-amplitude sample of a base waveform at phase t in [0, 1).

    Shared by unit_waveform() (the channel preview) and the arb editor's
    segment renderer so both produce identical SINE/SQUARE/RAMP/PULSE shapes.
    duty/sym are fractions (0..1); returns a value in [-1, 1].
    """
    waveform = (waveform or '').upper()
    if waveform == 'SINE':
        return math.sin(2.0 * math.pi * t)
    if waveform == 'SQUARE':
        return 1.0 if t < duty else -1.0
    if waveform == 'RAMP':
        # Rise from -1 to +1 over the sym fraction, fall back over the rest.
        if sym <= 0.0:
            return 1.0 - 2.0 * t
        if sym >= 1.0:
            return -1.0 + 2.0 * t
        if t < sym:
            return -1.0 + 2.0 * (t / sym)
        return 1.0 - 2.0 * ((t - sym) / (1.0 - sym))
    if waveform == 'PULSE':
        return _pulse_sample(t, max(duty, 1e-6), rise_frac, fall_frac)
    return 0.0


def _pulse_sample(t, duty, rise_frac, fall_frac):
    """One pulse sample: low -1, high +1, linear rise/fall edges.

    The rising edge starts at t=0 and the falling edge ends at t=duty; edge
    fractions are clamped so both fit inside the high time.
    """
    high = max(duty, 1e-6)
    rise = min(max(rise_frac, 0.0), high / 2.0)
    fall = min(max(fall_frac, 0.0), high / 2.0)
    if t < rise:
        return -1.0 + 2.0 * (t / rise)
    if t < high - fall:
        return 1.0
    if t < high:
        return 1.0 - 2.0 * ((t - (high - fall)) / fall) if fall > 0 else 1.0
    return -1.0


def scale_waveform(unit_samples, amp_vpp, offset_v):
    """Map unit samples to volts: y * (Vpp / 2) + offset."""
    half = amp_vpp / 2.0
    return [y * half + offset_v for y in unit_samples]
