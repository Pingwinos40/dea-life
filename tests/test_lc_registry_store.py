#!/usr/bin/env python3
"""Headless tests: specimen registry + admin caps + run store + safety.

Run: .venv/Scripts/python.exe tests/test_lc_registry_store.py
"""
import json
import os as _os
import sys as _sys
import tempfile

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

import os

from core import runstore
from core import safety
from core.clock import SimClock
from core.registry import Registry, RegistryError


# ---- registry ------------------------------------------------------------

def test_create_get_update_cycle():
    with tempfile.TemporaryDirectory() as d:
        reg = Registry(d)
        reg.create('B-1', 'bender_10x20', '2026-08-23', elastomer='UV-RSE',
                   c_est_nf='4.7')
        row = reg.get('B-1')
        assert row['status'] == 'virgin'
        assert row['elastomer'] == 'UV-RSE'
        reg.update('B-1', notes='first batch')
        # reload from disk
        reg2 = Registry(d)
        assert reg2.get('B-1')['notes'] == 'first batch'
        try:
            reg.create('B-1', 'bender_10x20', 'x')
        except RegistryError:
            pass
        else:
            raise AssertionError('duplicate id must refuse')


def test_status_transitions_guarded():
    with tempfile.TemporaryDirectory() as d:
        reg = Registry(d)
        reg.create('B-1', 'bender_10x20', 'x')
        try:
            reg.update('B-1', status='failed')
        except RegistryError as e:
            assert 'record_outcome' in str(e)
        else:
            raise AssertionError('direct status edit must refuse')
        reg.mark_in_test('B-1', 'RUN1')
        assert reg.get('B-1')['status'] == 'in_test'
        reg.record_outcome('B-1', 'failed', 12345, 600.0, 'RUN1',
                           failure_mode='overcurrent')
        row = reg.get('B-1')
        assert row['status'] == 'failed'
        assert row['cycles_accum'] == '12345'
        try:
            reg.mark_in_test('B-1', 'RUN2')
        except RegistryError as e:
            assert 'does not go back' in str(e)
        else:
            raise AssertionError('failed specimen must not re-arm')


def test_outcome_accumulates_and_censors():
    with tempfile.TemporaryDirectory() as d:
        reg = Registry(d)
        reg.create('B-2', 'bender_10x20', 'x')
        reg.record_outcome('B-2', 'complete', 10000, 1000.0, 'R1')
        reg.record_outcome('B-2', 'aborted', 500, 50.0, 'R2')
        row = reg.get('B-2')
        assert row['status'] == 'suspended'
        assert row['cycles_accum'] == '10500'
        rows = reg.survival_rows()
        assert rows == [{'specimen_id': 'B-2', 'cycles': 10500.0,
                         'status': 'suspended', 'failure_mode': ''}]


def test_caps_hierarchy_and_ceiling():
    with tempfile.TemporaryDirectory() as d:
        reg = Registry(d)
        reg.create('B-1', 'bender_10x20', 'x')
        reg.create('P-1', 'planar16', 'x')
        assert reg.cap_kv('B-1') == 2.5      # geometry default
        assert reg.cap_kv('P-1') == 10.0
        caps = json.load(open(reg.caps_path, encoding='utf-8'))
        caps['specimens']['B-1'] = 1.9
        caps['lab_ceiling_kv'] = 8.0
        with open(reg.caps_path, 'w', encoding='utf-8') as fh:
            json.dump(caps, fh)
        reg.reload_caps()
        assert reg.cap_kv('B-1') == 1.9      # per-specimen wins
        assert reg.cap_kv('P-1') == 8.0      # ceiling clamps geometry
        assert reg.paschen_band_pa() == (1.0, 10000.0)


# ---- runstore ------------------------------------------------------------

def test_onedrive_refused():
    try:
        runstore.check_data_root(r'C:\Users\x\OneDrive\runs')
    except runstore.RunStoreError as e:
        assert 'OneDrive' in str(e)
    else:
        raise AssertionError('OneDrive path must refuse')


def test_csv_headers_and_append_reopen():
    with tempfile.TemporaryDirectory() as d:
        run = os.path.join(d, 'LC_1')
        store = runstore.RunStore(run)
        store.blocks.write({'block_idx': 0, 'type': 'cycle',
                            'cycles_done': 100})
        store.close()
        # fresh open without resume refuses
        try:
            runstore.RunStore(run)
        except runstore.RunStoreError:
            pass
        else:
            raise AssertionError('existing run dir must need resume')
        store2 = runstore.RunStore(run, resume=True)
        store2.blocks.write({'block_idx': 1, 'type': 'cycle'})
        store2.close()
        with open(os.path.join(run, 'blocks.csv'), 'r',
                  encoding='utf-8-sig') as fh:
            lines = fh.read().strip().splitlines()
        assert lines[0].startswith('block_idx,')   # single header
        assert len(lines) == 3
        assert lines[0].count(',') == lines[1].count(',')


def test_runlog_prelog_semantics():
    with tempfile.TemporaryDirectory() as d:
        seen = []
        log = runstore.RunLog(sink=seen.append, clock=SimClock())
        log.log('before dir exists')
        path = os.path.join(d, 'run.log')
        log.attach(path)
        log.log('after')
        body = open(path, encoding='utf-8').read()
        assert 'before dir exists' in body and 'after' in body
        assert body.index('before') < body.index('after')
        assert seen == ['before dir exists', 'after']


def test_event_rows_numbered():
    with tempfile.TemporaryDirectory() as d:
        from core.cycles import CycleLedger
        store = runstore.RunStore(os.path.join(d, 'LC_1'))
        led = CycleLedger()
        store.event('t', 0.0, led, 'fast', 'x', 'log')
        store.event('t', 1.0, led, 'fast', 'y', 'log')
        store.set_event_idx(10)
        store.event('t', 2.0, led, 'fast', 'z', 'log')
        store.close()
        body = open(os.path.join(d, 'LC_1', 'events.csv'),
                    encoding='utf-8-sig').read().splitlines()
        assert body[1].startswith('1,')
        assert body[3].startswith('11,')


# ---- safety --------------------------------------------------------------

def test_paschen_inhibit_band():
    band = (1.0, 10000.0)
    assert safety.paschen_inhibited(None, band)       # no attestation
    assert safety.paschen_inhibited(500.0, band)      # in band
    assert not safety.paschen_inhibited(101300.0, band)
    assert not safety.paschen_inhibited(0.5, band)    # hard vacuum


def test_platform_gate():
    assert safety.platform_ok('mock').ok
    assert safety.platform_ok('dry').ok
    live = safety.platform_ok('live')
    assert live.ok == _sys.platform.startswith('linux')


def test_override_token_is_run_scoped():
    t1 = safety.paschen_override_token('RUN_A')
    t2 = safety.paschen_override_token('RUN_B')
    assert t1 != t2 and 'RUN_A' in t1


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
