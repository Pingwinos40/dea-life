#!/usr/bin/env python3
"""BENCH_TEST section Q probe: the kill paths, dummy load, low voltage.

    SAFETY: resistor dummy load on the Trek output, levels <= 0.2 kV.
    Operator present throughout.

  1. abort mid-burst: fire a long burst, call the HAL zero() -- scope
     MEAN must read ~0 within a second, output off
  2. cable-pull NOT_ZEROED: operator unplugs the SG link when prompted;
     zero() must return False (the engine turns that into the
     NOT_ZEROED terminal state + operator instruction)

Usage (bench, Linux):
    python bench/probe_kill_path.py [--sg-ch 1] [--scope-ch 1]
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

from hal.drive_bk4055b import BK4055BDrive  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sg-ch', type=int, default=1)
    ap.add_argument('--scope-ch', type=int, default=1)
    args = ap.parse_args()

    print('== section Q probe: dummy load, <=0.2 kV ==')
    input('Confirm the DUMMY LOAD is connected and the level plan is '
          '<=0.2 kV, then press Enter: ')

    sg = instruments.BK4055B()
    scope = instruments.TekMSO24()
    drive = BK4055BDrive(sg, args.sg_ch)
    drive.specimen_cap_kv = 0.2

    # 1 -- abort mid-burst
    drive.configure_cycle('SINE', 2.0, 0.2, 0.0)
    drive.fire_burst(600)          # 5 minutes' worth -- we kill it
    time.sleep(3.0)
    ok = drive.zero()
    time.sleep(1.0)
    val, st = scope.measure_raw('MEAN', args.scope_ch)
    zeroed = val is not None and abs(val) < 0.05
    print(f"[{'PASS' if ok and zeroed else 'FAIL'}] mid-burst zero(): "
          f"returned {ok}, scope MEAN {val!r} V ({st})")

    # 2 -- cable-pull NOT_ZEROED
    input('\nNow UNPLUG the SG USB/LAN link, then press Enter: ')
    drive2 = BK4055BDrive(sg, args.sg_ch)
    got = drive2.zero()
    print(f"[{'PASS' if got is False else 'FAIL'}] cable-pull zero() "
          f"returned {got} (False = the NOT_ZEROED path; the engine "
          f"then demands a front-panel shutdown)")
    print('\nReconnect the link and power-cycle checks per '
          'BENCH_TEST.md. SIGTERM + resume drills run against a real '
          'engine run (section T).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
