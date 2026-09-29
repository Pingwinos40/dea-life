#!/usr/bin/env python3
"""Headless tests: recipe schema validation + resolution.

Run: .venv/Scripts/python.exe tests/test_lc_recipe.py
"""
import copy
import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

from core.recipe import Recipe, RecipeError, resolve, recipe_hash

GOOD = {
    'schema': 'dea-life/1',
    'name': 'T',
    'geometry': 'planar',
    'drive': {'waveform': 'SINE', 'freq_hz': 5.0, 'v_pk': {'kv': 1.5},
              'v_min_kv': 0.0},
    'stop': {'max_cycles': 10000, 'max_wall_h': 24},
    'blocks': [
        {'type': 'cycle', 'role': 'break_in', 'n_cycles': 100},
        {'type': 'full_interlude', 'set_baseline': True,
         'staircase': {'start_kv': 0.0, 'end_kv': 1.2, 'step_kv': 0.4,
                       'ramp_s': 1.0, 'landing_s': 3.0, 'settle_s': 1.0,
                       'snap_lead_s': 0.5}},
        {'type': 'loop', 'repeat': 3, 'blocks': [
            {'type': 'cycle', 'n_cycles': 1000},
            {'type': 'fast_interlude'},
        ]},
    ],
}

SPECIMEN = {'specimen_id': 'S1', 'geometry': 'planar16',
            'thickness_um_layer': '50', 'ebd_ref_v_per_um': '80',
            'ebd_ref_temp_c': '23', 'c_est_nf': '5'}


def _bad(mutate, *needles):
    d = copy.deepcopy(GOOD)
    mutate(d)
    try:
        Recipe.from_dict(d)
    except RecipeError as e:
        msg = str(e)
        for n in needles:
            assert n in msg, f'wanted {n!r} in:\n{msg}'
        return
    raise AssertionError(f'expected RecipeError for {needles}')


def test_good_parses():
    r = Recipe.from_dict(GOOD)
    assert r.name == 'T'
    assert r.planned_cycles() == 100 + 3 * 1000


def test_unknown_keys_are_hard_errors():
    _bad(lambda d: d.update(bogus=1), 'unknown key')
    _bad(lambda d: d['drive'].update(freq=5), 'unknown key')
    _bad(lambda d: d['blocks'][0].update(cycles=5), 'unknown key')


def test_freq_ceiling():
    _bad(lambda d: d['drive'].update(freq_hz=25.0), 'above maximum')


def test_baseline_structure_enforced():
    _bad(lambda d: d['blocks'][1].update(set_baseline=False),
         'set_baseline')
    _bad(lambda d: d['blocks'][0].update(role=''), 'break_in')
    # baseline before break-in
    def swap(d):
        d['blocks'][0], d['blocks'][1] = d['blocks'][1], d['blocks'][0]
    _bad(swap, 'AFTER')


def test_staircase_constraint_precheck():
    def tight(d):
        d['blocks'][1]['staircase'].update(landing_s=1.0, settle_s=0.7,
                                           snap_lead_s=0.5)
    _bad(tight, 'settle_s + snap_lead_s')


def test_flatten_loop_paths():
    r = Recipe.from_dict(GOOD)
    flat = list(r.flatten())
    assert len(flat) == 2 + 3 * 2
    idxs = [f[0] for f in flat]
    assert idxs == list(range(8))
    paths = [f[1] for f in flat]
    assert paths[2] == '2[1/3].0'
    assert paths[-1] == '2[3/3].1'


def test_resolve_explicit_kv_and_cap():
    r = Recipe.from_dict(GOOD)
    res = resolve(r, SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    assert res['drive']['v_pk_kv'] == 1.5
    assert res['planned_cycles'] == 3100
    assert len(res['blocks']) == 8
    # cap below the drive refuses
    try:
        resolve(r, SPECIMEN, cap_kv=1.0, c_est_nf=5.0)
    except RecipeError as e:
        assert 'hard cap' in str(e)
    else:
        raise AssertionError('expected cap refusal')


def test_resolve_fraction_of_breakdown():
    d = copy.deepcopy(GOOD)
    d['drive']['v_pk'] = {'fraction_of_breakdown': 0.5, 'temp_c': 23}
    r = Recipe.from_dict(d)
    res = resolve(r, SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    # 80 V/um * 50 um = 4 kV; 50% -> 2.0 kV
    assert abs(res['drive']['v_pk_kv'] - 2.0) < 1e-9
    # missing registry facts refuse
    try:
        resolve(r, dict(SPECIMEN, ebd_ref_v_per_um=''), cap_kv=10.0)
    except RecipeError as e:
        assert 'ebd_ref_v_per_um' in str(e)
    else:
        raise AssertionError('expected refusal')
    # temperature mismatch refuses (no scaling model on purpose)
    d2 = copy.deepcopy(d)
    d2['drive']['v_pk'] = {'fraction_of_breakdown': 0.5, 'temp_c': -40}
    try:
        resolve(Recipe.from_dict(d2), SPECIMEN, cap_kv=10.0)
    except RecipeError as e:
        assert 'temperature' in str(e).lower() or 'temp' in str(e)
    else:
        raise AssertionError('expected temp refusal')


def test_resolve_fraction_of_vpk_staircase():
    d = copy.deepcopy(GOOD)
    d['blocks'][1]['staircase']['end_kv'] = {'fraction_of_vpk': 0.8}
    r = Recipe.from_dict(d)
    res = resolve(r, SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    st = res['blocks'][1]['staircase']
    assert abs(st['end_kv'] - 1.2) < 1e-9


def test_resolve_feasibility_refusal():
    d = copy.deepcopy(GOOD)
    d['drive']['freq_hz'] = 10.0
    r = Recipe.from_dict(d)
    try:
        resolve(r, SPECIMEN, cap_kv=10.0, c_est_nf=60.0)
    except RecipeError as e:
        assert 'feasibility' in str(e)
    else:
        raise AssertionError('expected feasibility refusal')


def test_hash_stable_and_sensitive():
    r = Recipe.from_dict(GOOD)
    a = resolve(r, SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    b = resolve(Recipe.from_dict(GOOD), SPECIMEN, cap_kv=10.0,
                c_est_nf=5.0)
    assert a['sha256'] == b['sha256']
    d = copy.deepcopy(GOOD)
    d['drive']['freq_hz'] = 4.0
    c = resolve(Recipe.from_dict(d), SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    assert c['sha256'] != a['sha256']
    assert recipe_hash(a) == a['sha256']


def test_milestones_validated():
    d = copy.deepcopy(GOOD)
    d['milestones'] = [1000, 5000]
    assert Recipe.from_dict(d).milestones == [1000, 5000]
    assert Recipe.from_dict(GOOD).milestones == []
    _bad(lambda d: d.update(milestones=[5000, 1000]), 'strictly increasing')
    _bad(lambda d: d.update(milestones=[0]), 'positive integer')
    _bad(lambda d: d.update(milestones=[1.5]), 'positive integer')
    _bad(lambda d: d.update(milestones=[10000]), 'below stop.max_cycles')
    _bad(lambda d: d.update(milestones=1000), 'must be a list')


def test_milestones_resolve_and_hash():
    d = copy.deepcopy(GOOD)
    d['milestones'] = [1000]
    r1 = resolve(Recipe.from_dict(d), SPECIMEN, cap_kv=10.0, c_est_nf=5.0)
    assert r1['milestones'] == [1000]
    r0 = resolve(Recipe.from_dict(GOOD), SPECIMEN, cap_kv=10.0,
                 c_est_nf=5.0)
    assert r0['sha256'] != r1['sha256']


def test_bender_flagship_template():
    # author decisions 2026-09-29 (docs/MOTIVATION.md, roadmap 5)
    rec = Recipe.load(_os.path.join(_root, 'recipes',
                                    'dea_life_bender_v1.json'))
    assert (rec.waveform, rec.freq_hz) == ('SQUARE', 0.25)
    assert rec.v_pk_spec == {'kv': 2.0}
    assert rec.max_cycles == 1000000
    assert rec.milestones == [1000, 10000, 100000]
    assert rec.planned_cycles() >= rec.max_cycles
    # 1e6 cycles at 0.25 Hz is ~1111 h: the wall cap must not end the
    # run first
    assert rec.max_wall_h * 3600 > rec.max_cycles / rec.freq_hz
    row = {'specimen_id': 'B1', 'geometry': 'bender_20x80'}
    res = resolve(rec, row, cap_kv=2.5, c_est_nf=10.0)
    assert res['feasibility']['verdict'] == 'ok', res['feasibility']
    assert res['feasibility']['waveform'] == 'SQUARE'


def test_all_problems_reported_together():
    d = copy.deepcopy(GOOD)
    d['drive']['freq_hz'] = 99.0
    d['geometry'] = 'wrong'
    d['bogus'] = 1
    try:
        Recipe.from_dict(d)
    except RecipeError as e:
        assert len(e.problems) >= 3, e.problems
    else:
        raise AssertionError('expected errors')


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
