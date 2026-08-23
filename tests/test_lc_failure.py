#!/usr/bin/env python3
"""Headless tests: the three-tier failure rule engine.

Run: .venv/Scripts/python.exe tests/test_lc_failure.py
"""
import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

from core.failure import (DEFAULT_RULES, RuleEngine, merge_rules,
                          validate_rules, RuleConfigError)


def _engine(overrides=None):
    return RuleEngine(merge_rules(overrides or {}))


def test_validate_catches_unknown_tier_and_op():
    probs = validate_rules({'weird': []})
    assert any('unknown tier' in p for p in probs)
    probs = validate_rules({'slow': [{'id': 'amplitude', 'op': '~='}]})
    assert any("op '~='" in p for p in probs)


def test_validate_new_rule_needs_full_definition():
    probs = validate_rules({'slow': [{'id': 'my_rule'}]})
    assert any('missing' in p for p in probs)
    probs = validate_rules({'slow': [
        {'id': 'my_rule', 'metric': 'x', 'op': '>', 'threshold': 1,
         'action': 'warn'}]})
    assert probs == [], probs


def test_merge_overrides_by_id():
    rules = merge_rules({'slow': [{'id': 'amplitude',
                                   'threshold': 0.70}]})
    amp = [r for r in rules['slow'] if r['id'] == 'amplitude'][0]
    assert amp['threshold'] == 0.70
    assert amp['flag_at'] == 0.90          # untouched default
    # defaults object not mutated
    amp0 = [r for r in DEFAULT_RULES['slow']
            if r['id'] == 'amplitude'][0]
    assert amp0['threshold'] == 0.80


def test_merge_raises_on_invalid():
    try:
        merge_rules({'slow': [{'id': 'x'}]})
    except RuleConfigError:
        pass
    else:
        raise AssertionError('expected RuleConfigError')


def test_slow_flag_at_vs_threshold():
    eng = _engine()
    # inside flag band: one AMBER flag, once only
    f1 = eng.eval_slow({'disp_ratio': 0.85})
    assert len(f1) == 1 and f1[0].is_flag and f1[0].action == 'flag'
    f2 = eng.eval_slow({'disp_ratio': 0.84})
    assert f2 == []                        # flagged once, not spammed
    # past the hard threshold: abort firing
    f3 = eng.eval_slow({'disp_ratio': 0.79})
    assert len(f3) == 1 and not f3[0].is_flag
    assert f3[0].action == 'abort'
    assert eng.failure_mode == 'amplitude'


def test_first_cause_latch():
    eng = _engine()
    eng.eval_slow({'leak_ratio': 150.0})
    eng.eval_slow({'disp_ratio': 0.5})
    assert eng.failure_mode == 'leakage'   # first abort wins
    assert eng.first_abort.value == 150.0


def test_watchdog_trip_report_latches():
    eng = _engine()
    f = eng.report_watchdog_trip(512.0)
    assert f.rule_id == 'overcurrent' and f.action == 'abort'
    assert eng.failure_mode == 'overcurrent'


def test_fast_sustain_semantics():
    eng = _engine()
    # drive_fidelity: >5% sustained 5 s of consecutive evaluations
    assert eng.eval_fast(0.0, {'v_err_frac': 0.10}) == []
    assert eng.eval_fast(2.0, {'v_err_frac': 0.10}) == []
    got = eng.eval_fast(5.5, {'v_err_frac': 0.10})
    assert len(got) == 1 and got[0].action == 'pause'
    # a dip resets the streak
    eng2 = _engine()
    eng2.eval_fast(0.0, {'v_err_frac': 0.10})
    eng2.eval_fast(3.0, {'v_err_frac': 0.01})     # below: reset
    assert eng2.eval_fast(6.0, {'v_err_frac': 0.10}) == []
    # None = no evidence, does NOT reset (watchdog convention)
    eng3 = _engine()
    eng3.eval_fast(0.0, {'v_err_frac': 0.10})
    eng3.eval_fast(3.0, {'v_err_frac': None})
    got = eng3.eval_fast(5.5, {'v_err_frac': 0.10})
    assert len(got) == 1


def test_medium_tier():
    eng = _engine()
    got = eng.eval_medium({'soft_flags_per_kcycle': 6.0})
    assert len(got) == 1 and got[0].action == 'pause'
    assert eng.eval_medium({'soft_flags_per_kcycle': 2.0}) == []


def test_monitor_loss_rule():
    eng = _engine()
    got = eng.eval_fast(100.0, {'blind_s': 61.0})
    assert len(got) == 1 and got[0].rule_id == 'monitor_loss'
    assert got[0].action == 'pause'


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
