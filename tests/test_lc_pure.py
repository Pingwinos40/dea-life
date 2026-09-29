#!/usr/bin/env python3
"""Headless tests: pure core math (feasibility, lifefactors, cycles,
checkpoint round-trip).

Run: .venv/Scripts/python.exe tests/test_lc_pure.py
"""
import math
import os as _os
import sys as _sys
import tempfile

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

from core import checkpoint as ck
from core import cycles
from core import feasibility as fz
from core import lifefactors as lf


# ---- feasibility ---------------------------------------------------------

def test_peak_current_math():
    # 5 nF, 1.5 kV unipolar sine at 5 Hz: I = 2*pi*5*5*0.75 = 117.8 uA
    got = fz.peak_current_ua(5.0, 5.0, 1.5, 0.0)
    assert abs(got - 2 * math.pi * 5 * 5 * 0.75) < 1e-9, got


def test_bender_worked_example_refuses():
    # The plan's example: 5 nF at 1.75 kV -- ~30 Hz caps the 610E-G.
    rep = fz.check_drive(100.0, 5.0, 1.75)
    assert rep['verdict'] == 'refuse', rep
    assert rep['max_feasible_hz'] < 100.0
    rep2 = fz.check_drive(10.0, 5.0, 1.75)
    assert rep2['verdict'] == 'ok', rep2


def test_square_edge_math():
    # 10 nF through 2 kV on 2000 - 50 uA: 20 uC / 1950 uA = 10.26 ms,
    # far slower than the 57 us slew limit -> current-limited
    t, i = fz.square_edge_s(10.0, 2.0, 0.0)
    assert abs(t - 20.0 / 1950.0) < 1e-12, t
    assert abs(i - 1950.0) < 1e-6, i


def test_square_flagship_ok_and_fast_square_refused():
    # bender flagship (2026-09-29): 2 kV, 0.25 Hz, nF-class stack
    rep = fz.check_drive(0.25, 10.0, 2.0, waveform='SQUARE')
    assert rep['verdict'] == 'ok' and rep['edge_frac'] < 0.01, rep
    # same stack at 10 Hz: ~10 ms edges in a 50 ms half-period
    rep = fz.check_drive(10.0, 10.0, 2.0, waveform='SQUARE')
    assert rep['verdict'] == 'warn', rep
    # a 100 nF stack at 10 Hz: edges eat the half-period
    rep = fz.check_drive(10.0, 100.0, 2.0, waveform='SQUARE')
    assert rep['verdict'] == 'refuse', rep
    assert rep['max_feasible_hz'] < 10.0
    assert 'REFUSED' in rep['msgs'][0]


def test_summary_line_per_waveform():
    sq = fz.summary(fz.check_drive(0.25, 10.0, 2.0, waveform='SQUARE'))
    assert sq.startswith('square edge') and 'Trek-limited' in sq, sq
    sn = fz.summary(fz.check_drive(5.0, 5.0, 1.5))
    assert sn.startswith('I_pk') and '% of Trek' in sn, sn
    assert 'C unknown' in fz.summary(
        fz.check_drive(0.25, None, 2.0, waveform='SQUARE'))


def test_square_unknown_c_warns():
    rep = fz.check_drive(0.25, None, 2.0, waveform='SQUARE')
    assert rep['verdict'] == 'warn' and rep['edge_s'] is None
    assert 'SKIPPED' in rep['msgs'][0]


def test_unknown_c_warns_not_passes():
    rep = fz.check_drive(5.0, None, 1.5)
    assert rep['verdict'] == 'warn'
    assert rep['i_pk_ua'] is None
    assert 'SKIPPED' in rep['msgs'][0]


def test_warn_band():
    # push I_pk into 60..90% of 2 mA
    c = 60.0                       # nF
    rep = fz.check_drive(5.0, c, 1.5)   # I ~ 1414 uA + 50 = 73%
    assert rep['verdict'] == 'warn', rep


def test_scope_window_horizontal():
    assert fz.scope_window_horizontal_ok(5.0, 0.4)
    assert not fz.scope_window_horizontal_ok(5.0, 0.3)


# ---- lifefactors ---------------------------------------------------------

def test_ecss_worked_example_1():
    # Standard's Example 1: 15 ground + 100 in-orbit -> 520
    res = lf.ecss(ground=15, orbit=100)
    assert res['required_cycles'] == 520, res


def test_ecss_worked_example_2():
    # Example 2: 2 ground (min 10) + 1 in-orbit -> 50? No: 10*4 + 1*10
    res = lf.ecss(ground=2, orbit=1)
    assert res['required_cycles'] == 50, res


def test_nasa5017_formula():
    res = lf.nasa5017(1000, ground=50, functional=25)
    assert res['required_cycles'] == 2 * 1000 + 4 * 50 + 4 * 25
    res_h = lf.nasa5017(1000, ground=50, functional=25, human_rated=True)
    assert res_h['required_cycles'] == 4 * 1000 + 4 * 50 + 4 * 25


def test_nasa5017_margin_before_factor():
    res = lf.nasa5017(1000, margin=100)
    assert res['required_cycles'] == 2 * 1100


# ---- cycles --------------------------------------------------------------

def test_plan_chunks():
    assert cycles.plan_chunks(2500) == [1000, 1000, 500]
    assert cycles.plan_chunks(1000) == [1000]
    assert cycles.plan_chunks(0) == []
    assert cycles.plan_chunks(2500, hw_max=400) == [400] * 6 + [100]


def test_ledger_duty():
    led = cycles.CycleLedger()
    added = led.credit_cycles(1000, 5.0, 'SINE', 'burst')
    assert abs(added - 100.0) < 1e-9          # 200 s * 0.5 duty
    led.credit_hold(1.0, 20.0)
    assert abs(led.actuated_s - 120.0) < 1e-9
    led.credit_hold(0.0, 20.0)                # 0 kV hold: no dose
    assert abs(led.actuated_s - 120.0) < 1e-9
    assert led.cycles == 1000


def test_freq_ok():
    assert cycles.freq_ok(5.05, 5.0)
    assert not cycles.freq_ok(5.2, 5.0)
    assert not cycles.freq_ok(None, 5.0)


def test_timed_counter_integrates():
    tc = cycles.TimedCounter(5.0)
    t = 0.0
    tc.feed(t, 5.0)
    for _ in range(100):
        t += 0.1
        tc.feed(t, 5.0)
    assert abs(tc.credited() - 50) <= 1, tc.credited()
    assert not tc.mismatch


def test_timed_counter_stale_hold_stops_accrual():
    tc = cycles.TimedCounter(5.0, hold_s=1.0)
    tc.feed(0.0, 5.0)
    tc.feed(1.0, 5.0)          # 5 cycles live
    for i in range(2, 40):     # scope dead for 38 s
        tc.feed(float(i), None)
    # stale-hold credits at most ~hold_s worth of extra intervals; the
    # essential property is that accrual STOPPED (38 dead seconds at
    # 5 Hz would be 190 phantom cycles)
    assert 10 <= tc.credited() <= 20, tc.credited()


def test_timed_counter_flags_mismatch():
    tc = cycles.TimedCounter(5.0)
    tc.feed(0.0, 5.0)
    tc.feed(1.0, 6.0)
    assert tc.mismatch


# ---- checkpoint ----------------------------------------------------------

def test_checkpoint_roundtrip_and_unclean():
    with tempfile.TemporaryDirectory() as d:
        assert ck.load(d) is None
        st = ck.new_state('RUN1', 'S1', 'abc', 'mock')
        st['cycles'] = 1234
        ck.save(d, st)
        got = ck.load(d)
        assert got['cycles'] == 1234
        assert ck.unclean(d)
        st['clean_shutdown'] = True
        ck.save(d, st)
        assert not ck.unclean(d)


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
