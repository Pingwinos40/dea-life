#!/usr/bin/env python3
"""Headless tests: the full LifecycleEngine on the mock rig + SimClock.

The whole shakedown recipe runs in well under a second of real time;
row counts, ledger axes, dispositions and the failure paths are pinned.

Run: .venv/Scripts/python.exe tests/test_lc_engine.py
"""
import csv
import json
import os as _os
import sys as _sys
import tempfile

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

import os

from core import checkpoint as _checkpoint
from core.clock import SimClock
from core.engine import LifecycleEngine
from core.recipe import Recipe, resolve
from hal import mock as halmock

RECIPE = json.load(open(os.path.join(_root, 'recipes',
                                     'shakedown_mock.json'),
                        encoding='utf-8'))
SPECIMEN = {'specimen_id': 'T-1', 'geometry': 'planar16',
            'thickness_um_layer': '50', 'ebd_ref_v_per_um': '80',
            'ebd_ref_temp_c': '23', 'c_est_nf': '5'}


def _mk(tmp, script=None, recipe_dict=None, resume=False,
        run_name='LC_T'):
    clock = SimClock()
    hal = halmock.build(clock, script=script)
    rec = Recipe.from_dict(recipe_dict or RECIPE)
    resolved = resolve(rec, SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    run_dir = os.path.join(tmp, run_name)
    logs = []
    eng = LifecycleEngine(resolved, SPECIMEN, 10.0, hal, run_dir, clock,
                          'mock', log_sink=logs.append, resume=resume)
    eng._logs = logs
    return eng, run_dir, hal


def _read_csv(run_dir, name):
    with open(os.path.join(run_dir, name), 'r',
              encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))


def test_complete_run_row_counts_and_axes():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, hal = _mk(tmp)
        assert eng.run() == 'complete'
        blocks = _read_csv(run_dir, 'blocks.csv')
        inter = _read_csv(run_dir, 'interludes.csv')
        # shakedown: 4 cycle blocks; 2 full + 3 fast interludes
        cyc = [b for b in blocks if b['type'] == 'cycle']
        assert len(cyc) == 4, [b['type'] for b in blocks]
        assert len(inter) == 5
        assert [r['kind'] for r in inter] == ['full', 'fast', 'fast',
                                              'fast', 'full']
        assert inter[0]['is_baseline'] == 'yes'
        # cycle accounting: 200 + 3*500, burst-counted
        assert [int(b['cycles_done']) for b in cyc] == [200, 500, 500,
                                                        500]
        assert all(b['counting'] == 'burst' for b in cyc)
        assert int(cyc[-1]['cycles_total']) == 1700
        # three axes monotone
        tot_c = [int(b['cycles_total']) for b in blocks
                 if b['cycles_total']]
        assert tot_c == sorted(tot_c)
        tot_a = [float(b['actuated_s_total']) for b in blocks
                 if b['actuated_s_total']]
        assert tot_a == sorted(tot_a)
        # actuated: 1700 cycles at 5 Hz sine = 170 s + interlude holds
        assert tot_a[-1] > 170.0
        # rig experienced exactly the planned cycles
        assert hal.rig.cycles == 1700
        # checkpoint marks clean
        ck = _checkpoint.load(run_dir)
        assert ck['clean_shutdown'] and ck['cycles'] == 1700
        # legacy sub-runs exist with vendored-format data.csv
        sub = os.path.join(run_dir, 'interludes', 'full_01', 'data.csv')
        assert os.path.exists(sub)
        rows = list(csv.DictReader(open(sub, encoding='utf-8')))
        assert rows[0]['tag'] == 'warmup'
        assert rows[1]['tag'] == 'baseline'
        # status.html rendered
        assert os.path.exists(os.path.join(run_dir, 'status.html'))


def test_scripted_breakdown_trips_watchdog_first():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, hal = _mk(tmp,
                                script={'breakdown_at_cycle': 900})
        assert eng.run() == 'failed'
        status = json.load(open(os.path.join(run_dir, 'status.json'),
                                encoding='utf-8'))
        assert status['disposition'] == 'failed'
        assert status['failure_mode'] == 'overcurrent'
        assert status['health'] == 'RED'
        events = _read_csv(run_dir, 'events.csv')
        trip = [e for e in events if e['rule_id'] == 'overcurrent'
                and e['action'] == 'abort']
        assert len(trip) == 1
        assert trip[0]['trace_file'].startswith('traces')
        assert os.path.exists(os.path.join(run_dir,
                                           trip[0]['trace_file']))
        # partial block credited as estimated
        blocks = _read_csv(run_dir, 'blocks.csv')
        last_cyc = [b for b in blocks if b['type'] == 'cycle'][-1]
        assert last_cyc['counting'] == 'estimated'
        assert last_cyc['verdict'] == 'HardTrip'
        # the drive was zeroed
        assert hal.rig.output_on is False


def test_amplitude_decay_flags_then_aborts():
    # aggressive decay: AMBER flag first, hard abort at a later
    # interlude; first-cause latch says 'amplitude'
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, hal = _mk(tmp, script={'amp_decay': 0.72})
        got = eng.run()
        assert got == 'failed', got
        status = json.load(open(os.path.join(run_dir, 'status.json'),
                                encoding='utf-8'))
        assert status['failure_mode'] == 'amplitude'
        events = _read_csv(run_dir, 'events.csv')
        kinds = [(e['rule_id'], e['action']) for e in events]
        assert ('amplitude', 'flag') in kinds
        assert ('amplitude', 'abort') in kinds
        assert kinds.index(('amplitude', 'flag')) < \
            kinds.index(('amplitude', 'abort'))


def test_not_zeroed_terminal_state():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, hal = _mk(tmp, script={'zero_fail': True})
        assert eng.run() == 'NOT_ZEROED'
        status = json.load(open(os.path.join(run_dir, 'status.json'),
                                encoding='utf-8'))
        assert status['health'] == 'RED'
        assert status.get('not_zeroed') is True
        # checkpoint must NOT claim a clean shutdown
        ck = _checkpoint.load(run_dir)
        assert not ck.get('clean_shutdown')
        assert any('TURN OFF THE SG/TREK' in ln for ln in eng._logs)


def test_operator_stop_and_suspend():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, _hal = _mk(tmp, run_name='LC_STOP')
        eng.submit({'cmd': 'stop', 'by': 'tester'})
        assert eng.run() == 'aborted'
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, _hal = _mk(tmp, run_name='LC_SUSP')
        eng.submit({'cmd': 'suspend', 'by': 'tester'})
        assert eng.run() == 'suspended'


def test_cycle_cap_ends_complete():
    d = json.loads(json.dumps(RECIPE))
    d['stop']['max_cycles'] = 800          # mid-recipe
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, hal = _mk(tmp, recipe_dict=d)
        assert eng.run() == 'complete'
        assert hal.rig.cycles <= 800 + 1000   # chunk granularity bound
        events = _read_csv(run_dir, 'events.csv')
        assert any('cap' in e['message'] for e in events)


def test_resume_rechar_pass_continues_counting():
    with tempfile.TemporaryDirectory() as tmp:
        # 1) run to completion, then forge an unclean mid-run checkpoint
        eng, run_dir, hal = _mk(tmp)
        assert eng.run() == 'complete'
        ck = _checkpoint.load(run_dir)
        ck['clean_shutdown'] = False
        ck['flat_idx'] = 4          # resume from inside the loop
        ck['cycles'] = 700
        ck['actuated_s'] = 86.5
        _checkpoint.save(run_dir, ck)
        # 2) resume: RECHAR must pass (healthy rig) and counting must
        # continue from the checkpoint, not restart
        eng2, _rd, hal2 = _mk(tmp, resume=True)
        hal2.rig.cycles = 700       # the specimen remembers its dose
        assert eng2.run() == 'complete'
        blocks = _read_csv(run_dir, 'blocks.csv')
        totals = [int(b['cycles_total']) for b in blocks
                  if b['cycles_total']]
        assert totals[-1] == 1700   # 700 + remaining 2*500
        assert any('RECHAR passed' in ln for ln in eng2._logs)


def test_resume_rechar_fail_blocks_hv():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, hal = _mk(tmp)
        assert eng.run() == 'complete'
        ck = _checkpoint.load(run_dir)
        ck['clean_shutdown'] = False
        ck['flat_idx'] = 4
        _checkpoint.save(run_dir, ck)
        # the specimen degraded while the rig was down
        eng2, _rd, hal2 = _mk(tmp, resume=True,
                              script={'amp_decay': 1.0})
        hal2.rig.amp0 = 60.0        # 60% of the stored ~100 baseline
        assert eng2.run() == 'rechar_failed'
        # no cycling happened after the failed gate
        assert hal2.rig.cycles == 0
        assert any('RECHAR FAILED' in e['message']
                   for e in _read_csv(run_dir, 'events.csv')
                   if e['message'])


def test_resume_refuses_hash_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, _hal = _mk(tmp)
        assert eng.run() == 'complete'
        ck = _checkpoint.load(run_dir)
        ck['clean_shutdown'] = False
        ck['recipe_sha256'] = 'someone-edited-the-recipe'
        _checkpoint.save(run_dir, ck)
        eng2, _rd, _h = _mk(tmp, resume=True)
        assert eng2.run() == 'aborted'
        assert any('hash mismatch' in ln for ln in eng2._logs)


def test_control_file_commands():
    with tempfile.TemporaryDirectory() as tmp:
        eng, run_dir, _hal = _mk(tmp, run_name='LC_CTRL')
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, 'control.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump({'seq': 1, 'cmd': 'stop', 'by': 'gui'}, fh)
        assert eng.run() == 'aborted'
        status = json.load(open(os.path.join(run_dir, 'status.json'),
                                encoding='utf-8'))
        assert status['control_ack_seq'] == 1


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
