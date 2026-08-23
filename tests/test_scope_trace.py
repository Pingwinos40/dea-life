#!/usr/bin/env python3
"""Headless tests for the scope trace plot math (issue #42).

Run: .venv/bin/python tests/test_scope_trace.py
"""
# Runnable from anywhere: put the repo root (one level up) on sys.path
# so the app modules import when this file is executed directly.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from scope_trace import decimate_minmax, nice_ticks


def test_decimate_reduces_to_columns():
    values = [float(i % 100) for i in range(10000)]
    env = decimate_minmax(values, 500)
    assert len(env) == 500
    for lo, hi in env:
        assert lo <= hi


def test_decimate_preserves_glitch():
    # a single-sample spike must survive any decimation
    values = [0.0] * 10000
    values[6789] = 42.0
    env = decimate_minmax(values, 300)
    assert max(hi for _, hi in env) == 42.0
    values[6789] = -42.0
    env = decimate_minmax(values, 300)
    assert min(lo for lo, _ in env) == -42.0


def test_decimate_short_input_passthrough():
    env = decimate_minmax([1.0, 2.0, 3.0], 100)
    assert env == [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]


def test_decimate_covers_every_sample():
    # spans must tile the record: min of mins == global min, etc.
    values = [((i * 37) % 101) - 50.0 for i in range(997)]  # awkward length
    env = decimate_minmax(values, 100)
    assert len(env) == 100
    assert min(lo for lo, _ in env) == min(values)
    assert max(hi for _, hi in env) == max(values)


def test_decimate_bad_input():
    for bad_call in (lambda: decimate_minmax([], 10),
                     lambda: decimate_minmax([1.0], 0),
                     lambda: decimate_minmax([1.0], -5)):
        try:
            bad_call()
            assert False, "must raise ValueError"
        except ValueError:
            pass


def test_nice_ticks_basic():
    ticks = nice_ticks(0.0, 1.0)
    assert ticks[0] >= 0.0 and ticks[-1] <= 1.0 + 1e-12
    assert 0.0 in ticks and any(abs(t - 1.0) < 1e-12 for t in ticks)
    # steps are uniform
    steps = {round(b - a, 12) for a, b in zip(ticks, ticks[1:])}
    assert len(steps) == 1, steps


def test_nice_ticks_are_nice_numbers():
    for lo, hi in ((-0.003, 0.007), (0.0, 2.5e-6), (-120.0, 480.0)):
        ticks = nice_ticks(lo, hi)
        assert 2 <= len(ticks) <= 8, (lo, hi, ticks)
        step = ticks[1] - ticks[0]
        import math
        mantissa = step / 10 ** math.floor(math.log10(step))
        assert round(mantissa, 6) in (1.0, 2.0, 5.0), step


def test_nice_ticks_degenerate():
    assert nice_ticks(3.3, 3.3) == [3.3]
    assert nice_ticks(5.0, 4.0) == [5.0]


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == '__main__':
    _run()
