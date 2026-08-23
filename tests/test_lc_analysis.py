#!/usr/bin/env python3
"""Headless tests: Weibull (censoring-correct), compliance matrix,
trend reduction, report generation on a real mock-engine run folder.

Run: .venv/Scripts/python.exe tests/test_lc_analysis.py
"""
import json
import math
import os as _os
import sys as _sys
import tempfile

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

import os

import numpy as np

from analysis import compliance as comp
from analysis import reduce as red
from analysis import weibull as wb


def _rows(times, statuses):
    return [{'specimen_id': f'S{i}', 'cycles': t, 'status': s}
            for i, (t, s) in enumerate(zip(times, statuses))]


# ---- Weibull -------------------------------------------------------------

def test_mrr_recovers_exact_quantiles():
    # feed exact Weibull median-rank quantiles: MRR must recover
    # beta/eta essentially exactly (complete sample)
    beta, eta = 2.5, 1e5
    n = 10
    ts = []
    for i in range(1, n + 1):
        F = (i - 0.3) / (n + 0.4)
        ts.append(eta * (-math.log(1 - F)) ** (1 / beta))
    res = wb.fit_mrr(_rows(ts, ['failed'] * n))
    assert abs(res['beta'] - beta) / beta < 0.01, res
    assert abs(res['eta'] - eta) / eta < 0.01, res


def test_mle_matches_scipy_complete_sample():
    rng = np.random.default_rng(7)
    beta, eta = 1.8, 5e4
    ts = eta * rng.weibull(beta, size=40)
    res = wb.fit_mle(_rows(ts.tolist(), ['failed'] * 40))
    from scipy.stats import weibull_min
    c, _loc, scale = weibull_min.fit(ts, floc=0)
    assert abs(res['beta'] - c) / c < 0.02, (res['beta'], c)
    assert abs(res['eta'] - scale) / scale < 0.02, (res['eta'], scale)


def test_mle_censored_recovery():
    # simulate with a cycle cap: cap-reachers are suspensions; the
    # censored MLE must recover the truth, and a naive drop-the-
    # suspensions fit must be visibly biased low
    rng = np.random.default_rng(11)
    beta, eta = 2.0, 1e5
    cap = 0.8e5
    raw = eta * rng.weibull(beta, size=300)
    times = np.minimum(raw, cap)
    status = ['failed' if r < cap else 'suspended' for r in raw]
    res = wb.fit_mle(_rows(times.tolist(), status))
    assert abs(res['beta'] - beta) / beta < 0.15, res
    assert abs(res['eta'] - eta) / eta < 0.10, res
    # naive fit on failures only (dropping suspensions)
    fails = [t for t, s in zip(times, status) if s == 'failed']
    naive = wb.fit_mle(_rows(fails, ['failed'] * len(fails)))
    assert naive['eta'] < 0.9 * res['eta'], (naive['eta'], res['eta'])


def test_b10_and_ci():
    rng = np.random.default_rng(3)
    beta, eta = 2.2, 2e5
    raw = eta * rng.weibull(beta, size=12)
    cap = 2.5e5
    times = np.minimum(raw, cap)
    status = ['failed' if r < cap else 'suspended' for r in raw]
    res = wb.fit_mle(_rows(times.tolist(), status))
    b10_true = eta * (-math.log(0.9)) ** (1 / beta)
    lo, hi = res['b10_ci90']
    assert lo is not None and hi is not None
    assert lo < res['b10'] < hi
    # true value inside the 90% interval (should hold for this seed)
    assert lo < b10_true < hi, (lo, b10_true, hi)


def test_analyze_headline_and_suspension_marks():
    rows = _rows([1e4, 2e4, 3e4, 4e4, 5e4],
                 ['failed', 'failed', 'suspended', 'failed',
                  'suspended'])
    res = wb.analyze(rows)
    assert res['headline']['method'] == 'MLE'
    assert res['suspensions'] == 2
    assert len(res['plot']['susp_t']) == 2
    line = wb.summary_line(res)
    assert 'B10' in line and '2 susp' in line


def test_too_few_failures_refuses():
    rows = _rows([1e4, 2e4, 3e4],
                 ['failed', 'suspended', 'suspended'])
    res = wb.analyze(rows)
    assert res['headline'] is None
    assert res['errors']


# ---- compliance ----------------------------------------------------------

def _claims_doc():
    return {'version': 1, 'campaign': 'T', 'claims': [
        {'req_id': 'LIFE-01', 'source_clause': 'NASA-STD-5017 s4.13.3',
         'statement': 'demonstrate 2x mission cycles',
         'parameter': 'cycles', 'op': '>=', 'required': 20000,
         'unit': 'cycles', 'achieved_path': 'campaign.min_cycles'},
        {'req_id': 'TVAC-01', 'source_clause': 'GEVS 2.6.3',
         'statement': 'plateau within tolerance',
         'op': '<=', 'required': 2.0, 'unit': 'C',
         'achieved_path': 'campaign.plateau_err_c'},
        {'req_id': 'OUT-01', 'source_clause': 'ASTM E595',
         'statement': 'material outgassing screened',
         'status': 'tailored',
         'tailoring_note': 'screened at material level, report X'},
    ]}


def test_compliance_pass_fail_and_autodowngrade():
    rows = comp.evaluate(_claims_doc(),
                         {'campaign': {'min_cycles': 25000,
                                       'plateau_err_c': 3.5}})
    by = {r['req_id']: r for r in rows}
    assert by['LIFE-01']['status'] == 'comply'
    assert by['TVAC-01']['status'] == 'deviate'
    assert 'auto-downgraded' in by['TVAC-01']['auto_note']
    assert by['OUT-01']['status'] == 'tailored'
    # missing data auto-downgrades, never passes
    rows2 = comp.evaluate(_claims_doc(), {})
    assert all(r['status'] == 'deviate' for r in rows2
               if r['req_id'] != 'OUT-01')


def test_tailored_requires_note():
    doc = _claims_doc()
    doc['claims'][2]['tailoring_note'] = ''
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'c.json')
        json.dump(doc, open(p, 'w'))
        try:
            comp.load_claims(p)
        except comp.ClaimsError as e:
            assert 'tailoring_note' in str(e)
        else:
            raise AssertionError('expected ClaimsError')


def test_no_eval_in_paths():
    assert comp.resolve_path({'a': {'b': 3}}, 'a.b') == 3
    assert comp.resolve_path({'a': {'b': 3}}, 'a.__class__') is None


# ---- reduce + report on a real mock run ---------------------------------

def _make_run(tmp):
    from core.clock import SimClock
    from core.engine import LifecycleEngine
    from core.recipe import Recipe, resolve
    from hal import mock as halmock
    RECIPE = json.load(open(os.path.join(_root, 'recipes',
                                         'shakedown_mock.json'),
                            encoding='utf-8'))
    SPEC = {'specimen_id': 'T-1', 'geometry': 'planar16',
            'thickness_um_layer': '50', 'ebd_ref_v_per_um': '80',
            'ebd_ref_temp_c': '23', 'c_est_nf': '5'}
    clock = SimClock()
    hal = halmock.build(clock)
    resolved = resolve(Recipe.from_dict(RECIPE), SPEC, 10.0,
                       c_est_nf=5.0)
    run_dir = os.path.join(tmp, 'LC_R')
    eng = LifecycleEngine(resolved, SPEC, 10.0, hal, run_dir, clock,
                          'mock', log_sink=lambda s: None)
    assert eng.run() == 'complete'
    return run_dir


def test_reduce_and_report_generation():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = _make_run(tmp)
        trends = red.trend_rows(run_dir)
        assert len(trends['interludes']) == 5
        assert trends['interludes'][0]['is_baseline']
        ratios = [t['disp_ratio'] for t in trends['interludes'][1:]]
        assert all(r is not None for r in ratios)
        # report
        from analysis.report_run import generate
        claims = os.path.join(tmp, 'claims.json')
        json.dump(_claims_doc(), open(claims, 'w'))
        out = generate(run_dir, claims_path=claims)
        body = open(out, encoding='utf-8').read()
        assert 'SUSPENSION' in body          # complete = right-censored
        assert 'Trends vs cycles' in body
        assert 'Trends vs actuated time' in body
        assert 'Compliance matrix' in body
        assert 'sha256' in body
        assert 'data:image/png;base64,' in body


def test_log_bins():
    edges = red.log_bins(1_000_000, per_decade=10)
    assert edges[0] == 0 and edges[-1] == 1_000_000
    assert 40 <= len(edges) <= 70
    assert edges == sorted(edges)


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
