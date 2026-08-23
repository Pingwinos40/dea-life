#!/usr/bin/env python3
"""Subprocess-level CLI tests -- the exact invocation surface the GUI
spawns detached. Catches import/argument regressions that in-process
tests cannot.

Run: .venv/Scripts/python.exe tests/test_lc_cli.py
"""
import os as _os
import subprocess
import sys as _sys
import tempfile

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
CLI = _os.path.join(_root, 'lifecycle_cli.py')


def _run(args, **kw):
    return subprocess.run([_sys.executable, CLI] + args, cwd=_root,
                          capture_output=True, text=True, **kw)


def test_full_mock_run_via_subprocess():
    with tempfile.TemporaryDirectory() as tmp:
        r = _run(['specimen', 'create', 'CLI-1', '--geometry',
                  'planar16', '--data-root', tmp,
                  '--field', 'c_est_nf=5',
                  '--field', 'thickness_um_layer=50',
                  '--field', 'ebd_ref_v_per_um=80',
                  '--field', 'ebd_ref_temp_c=23'])
        assert r.returncode == 0, r.stdout + r.stderr
        r = _run(['validate', 'recipes/shakedown_mock.json',
                  '--specimen', 'CLI-1', '--data-root', tmp])
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'OK:' in r.stdout
        r = _run(['run', 'recipes/shakedown_mock.json', '--specimen',
                  'CLI-1', '--mode', 'mock', '--sim-time',
                  '--data-root', tmp, '--run-name', 'LC_CLITEST'])
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'disposition: complete' in r.stdout
        run_dir = _os.path.join(tmp, 'CLI-1', 'LC_CLITEST')
        assert _os.path.exists(_os.path.join(run_dir, 'blocks.csv'))
        r = _run(['status', run_dir])
        assert r.returncode == 0
        assert 'DONE' in r.stdout


def test_validate_refuses_bad_specimen():
    with tempfile.TemporaryDirectory() as tmp:
        r = _run(['validate', 'recipes/shakedown_mock.json',
                  '--specimen', 'NOPE', '--data-root', tmp])
        assert r.returncode == 2
        assert 'not in the registry' in r.stdout


def test_feasibility_calculator():
    r = _run(['feasibility', '--freq', '5', '--c-nf', '5',
              '--v-pk', '1.5'])
    assert r.returncode == 0
    assert 'I_pk = 117.8 uA' in r.stdout or 'I_pk = 118' in r.stdout


def _run_tests():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run_tests()
