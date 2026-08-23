#!/usr/bin/env python3
"""BENCH_TEST section P probe: BK4055B burst semantics, SG-ONLY.

    SAFETY: run with the Trek OFF or its input disconnected. Everything
    here happens at control-level volts on the SG output, watched on
    the scope's SG channel (CH1 by default).

Proves, on THIS box's firmware, before any specimen ever sees HV:
  1. STPS accepted: C<ch>:BTWV STPS,270 read back from BTWV?
  2. Idle level: burst armed + output on -> scope MEAN ~= waveform
     MINIMUM (the offset-midpoint idle is the dangerous failure)
  3. NCYC max: binary-search the largest TIME the firmware accepts
  4. FREQUENCY verification at 0.1 / 1 / 5 / 10 Hz during a burst

Usage (bench, Linux):
    python bench/probe_burst_idle.py [--sg-ch 1] [--scope-ch 1]
"""
import argparse
import os
import sys
import time

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_root, os.path.join(_root, 'lib')):
    if p not in sys.path:
        sys.path.insert(0, p)

import instruments  # vendored  # noqa: E402

AMP_V = 1.0      # 1 V control span -- harmless on the SG, Trek is OFF
OFST_V = 0.5


def say(ok, label, detail=''):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f' -- {detail}' if detail else ''))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sg-ch', type=int, default=1)
    ap.add_argument('--scope-ch', type=int, default=1)
    args = ap.parse_args()
    ch = args.sg_ch

    print('== section P probe: SG-only, verify the Trek is OFF ==')
    input('Confirm the Trek is OFF / disconnected, then press Enter: ')

    sg = instruments.BK4055B()
    scope = instruments.TekMSO24()
    ok_all = True

    # unipolar 0..1 V sine, the lifecycle drive shape at control scale
    sg.set_load_polarity(ch, load='HZ', polarity='NOR')
    sg.set_basic_wave(ch, WVTP='SINE', FRQ=1.0, AMP=AMP_V, OFST=OFST_V)
    sg.set_burst(ch, True, ncycles=5, trigger='MAN')
    sg.write(f'C{ch}:BTWV STPS,270')
    sg.set_output(ch, True)
    time.sleep(0.5)

    # 1 -- STPS accepted?
    raw = sg.get_burst_dict(ch)
    stps = str(raw.get('STPS', '(absent)'))
    ok = stps.startswith('270')
    ok_all &= say(ok, 'STPS accepted', f'BTWV? STPS={stps}')
    if not ok:
        print('    -> burst counting must be BANNED on this box; the '
              'engine auto-degrades to timed counting (recipe '
              '"counting": "timed").')

    # 2 -- idle level ~ minimum (0 V), not the 0.5 V midpoint
    val, st = scope.measure_raw('MEAN', args.scope_ch)
    ok = val is not None and abs(val) < 0.1
    ok_all &= say(ok, 'idle level at waveform MINIMUM',
                  f'scope MEAN {val!r} V ({st}); midpoint would be '
                  f'{OFST_V} V')

    # 3 -- NCYC max by binary search on the TIME readback
    lo, hi = 1, 1 << 20
    while lo < hi - 1:
        mid = (lo + hi) // 2
        try:
            sg.set_burst(ch, True, ncycles=mid, trigger='MAN')
            got = int(float(sg.get_burst_dict(ch).get('TIME', 0)))
            if got == mid:
                lo = mid
            else:
                hi = mid
        except Exception:
            hi = mid
    say(True, 'NCYC max (record in BENCH_TEST.md)', f'{lo}')

    # 4 -- FREQUENCY verification during bursts
    for f in (0.1, 1.0, 5.0, 10.0):
        sg.set_basic_wave(ch, WVTP='SINE', FRQ=f, AMP=AMP_V,
                          OFST=OFST_V)
        n = max(3, int(f * 4))
        sg.set_burst(ch, True, ncycles=n, trigger='MAN')
        sg.write(f'C{ch}:BTWV STPS,270')
        sg.burst_trigger(ch)
        time.sleep(min(n / f * 0.5, 5.0))
        meas, st = scope.measure_raw('FREQUENCY', args.scope_ch)
        ok = meas is not None and abs(meas - f) / f < 0.02
        ok_all &= say(ok, f'FREQUENCY at {f:g} Hz',
                      f'scope read {meas!r} ({st})')
        time.sleep(max(n / f * 0.6, 0.5))
        # idle again after the burst?
        val, st = scope.measure_raw('MEAN', args.scope_ch)
        ok = val is not None and abs(val) < 0.1
        ok_all &= say(ok, f'post-burst idle at {f:g} Hz',
                      f'MEAN {val!r} V')

    sg.set_output(ch, False)
    sg.set_basic_wave(ch, WVTP='DC', OFST=0.0)
    verdict = 'ALL PASS' if ok_all else 'FAILURES -- do NOT proceed to HV'
    print(f'\n== section P: {verdict} ==')
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
