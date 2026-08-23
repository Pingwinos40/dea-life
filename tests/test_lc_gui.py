#!/usr/bin/env python3
"""Headless GUI construction tests: every screen builds, the Setup ->
validate -> Pre-flight flow works against a temp registry, the
dashboard renders a status dict, widgets behave.

Run: .venv/Scripts/python.exe tests/test_lc_gui.py
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
import tkinter as tk


def _mk_app(tmp):
    # the dev box genuinely has <1 GB free on C:, so the honest disk
    # gate would (correctly) refuse -- stub it for the flow test
    import collections
    from gui import preflight_screen
    Usage = collections.namedtuple('usage', 'total used free')
    preflight_screen.shutil.disk_usage = \
        lambda p: Usage(500e9, 100e9, 400e9)
    from gui.app import LifecycleApp
    cfg = {'data_root': os.path.join(tmp, 'data')}
    app = LifecycleApp(root_dir=_root, cfg=cfg)
    app.tk.withdraw()
    # seed a specimen
    reg = app.state.registry()
    reg.create('GUI-1', 'planar16', 'test', c_est_nf='5',
               thickness_um_layer='50', ebd_ref_v_per_um='80',
               ebd_ref_temp_c='23')
    app.setup.refresh_specimens()
    return app


def test_app_builds_and_flows():
    with tempfile.TemporaryDirectory() as tmp:
        app = _mk_app(tmp)
        try:
            root = app.tk
            # pre-flight tab starts disabled
            assert str(app.nb.tab(1, 'state')) == 'disabled'
            # drive the setup screen
            s = app.setup
            s.spec_var.set('GUI-1')
            s._on_specimen()
            assert 'HARD CAP' in s.cap_lbl.cget('text')
            s.recipe_var.set('shakedown_mock.json')
            s.env_t.var.set('23')
            s.env_p.var.set('1013')
            s.op_var.set('tester')
            s.attest_var.set(True)
            s.validate()
            root.update()
            assert app.state.setup is not None, \
                s.verdict.cget('text')
            assert app.state.setup['resolved']['drive']['v_pk_kv'] \
                == 1.5
            assert str(app.nb.tab(1, 'state')) == 'normal'
            # knob override flows into the resolved recipe
            s.kv_override.var.set('1.2')
            s.validate()
            assert app.state.setup['resolved']['drive']['v_pk_kv'] \
                == 1.2
            # pre-flight recheck: all local rows green (paschen at
            # ambient pressure passes)
            ok = app.preflight.recheck()
            assert ok, {k: (r.state_name, r.detail.cget('text'))
                        for k, r in app.preflight.rows.items()}
            assert str(app.preflight.start_live_btn.cget('state')) \
                == 'normal'
        finally:
            app.tk.destroy()


def test_setup_refuses_without_attestation():
    with tempfile.TemporaryDirectory() as tmp:
        app = _mk_app(tmp)
        try:
            s = app.setup
            s.spec_var.set('GUI-1')
            s._on_specimen()
            s.recipe_var.set('shakedown_mock.json')
            s.op_var.set('tester')
            s.attest_var.set(False)
            s.validate()
            assert app.state.setup is None
            assert 'attest' in s.verdict.cget('text').lower()
        finally:
            app.tk.destroy()


def test_dashboard_renders_status_and_flags():
    with tempfile.TemporaryDirectory() as tmp:
        app = _mk_app(tmp)
        try:
            run_dir = os.path.join(tmp, 'RUN_X')
            os.makedirs(run_dir)
            for name, rows in (('events.csv', None),
                               ('interludes.csv', None)):
                pass
            # minimal CSVs
            with open(os.path.join(run_dir, 'events.csv'), 'w',
                      encoding='utf-8-sig') as fh:
                fh.write('event_idx,t_iso,wall_s,cycles_total,'
                         'actuated_s_total,tier,rule_id,action,value,'
                         'threshold,message,trace_file,frame_file,'
                         'operator\n'
                         '1,t0,0,100,10,slow,amplitude,flag,0.85,0.9,'
                         'POTENTIAL FAILURE: amp 85%,,,\n')
            with open(os.path.join(run_dir, 'interludes.csv'), 'w',
                      encoding='utf-8-sig') as fh:
                fh.write('interlude_idx,kind,cycles_total,disp_ratio,'
                         'leak_ua\n1,fast,100,0.85,2.4\n'
                         '2,fast,200,0.83,2.6\n')
            from datetime import datetime
            status = {
                'run_id': 'RUN_X', 'state': 'RUNNING',
                'health': 'AMBER', 'cycles_total': 200,
                'cycles_target': 1000, 'wall_s': 3600,
                'updated_iso': datetime.now().isoformat(
                    timespec='seconds'),
                'flags': [{'id': 'amplitude',
                           'message': 'POTENTIAL FAILURE: amplitude '
                                      '85% of baseline', 'acked_by': ''}],
            }
            with open(os.path.join(run_dir, 'status.json'), 'w',
                      encoding='utf-8') as fh:
                json.dump(status, fh)
            d = app.dashboard
            d.attach(run_dir)
            d._render(d._status())
            app.tk.update()
            assert d.tiles['cycles'].value_lbl.cget('text') == '200'
            assert 'amplitude' in d._banners       # AMBER banner shown
            # ack via control.json
            d._ack('amplitude')
            ctl = json.load(open(os.path.join(run_dir,
                                              'control.json'),
                                 encoding='utf-8'))
            assert ctl['cmd'] == 'ack_flag'
            assert ctl['flag_id'] == 'amplitude'
            # acked flag clears the banner on next render
            status['flags'][0]['acked_by'] = 'tester'
            with open(os.path.join(run_dir, 'status.json'), 'w',
                      encoding='utf-8') as fh:
                json.dump(status, fh)
            d._render(d._status())
            assert 'amplitude' not in d._banners
        finally:
            app.tk.destroy()


def test_review_and_campaign_build():
    with tempfile.TemporaryDirectory() as tmp:
        app = _mk_app(tmp)
        try:
            # registry outcomes for the campaign Weibull
            reg = app.state.registry()
            for i, (c, st) in enumerate(((1e4, 'failed'),
                                         (2e4, 'failed'),
                                         (3e4, 'suspended'),
                                         (2.5e4, 'failed'))):
                sid = f'W-{i}'
                reg.create(sid, 'planar16', 't')
                reg.mark_in_test(sid, 'R')
                reg.record_outcome(sid, st.replace('suspended',
                                                   'suspended'),
                                   c, 100, 'R',
                                   failure_mode='overcurrent'
                                   if st == 'failed' else '')
            app.campaign.refresh()
            app.campaign._weibull()
            app.tk.update()
            txt = app.campaign.summary.cget('text')
            assert 'B10' in txt, txt
        finally:
            app.tk.destroy()


def test_widgets_validated_entry():
    from gui.widgets import ValidatedEntry
    root = tk.Tk()
    root.withdraw()
    try:
        e = ValidatedEntry(root, 'x', 'kV', lo=0.0, hi=10.0, default=5)
        assert e.validate()
        e.var.set('12')
        assert not e.validate()
        assert 'max' in e.err.cget('text')
        e.var.set('abc')
        assert not e.validate()
    finally:
        root.destroy()


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
