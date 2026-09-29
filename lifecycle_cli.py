#!/usr/bin/env python3
"""DEA-LIFE CLI.

    validate    parse + resolve a recipe against a specimen; print the
                feasibility + life-factor advisories, run nothing
    run         execute a recipe (--mode mock|dry|live)
    resume      continue an uncleanly-ended run (RECHAR gate enforced)
    status      print a run's status.json
    feasibility quick I_pk / f_max calculator
    specimen    create / list / show registry entries
    replay      (vision phase) run the bender pipeline over a recording

The CLI owns the terminal: typed confirmations (ENERGIZE, BLIND) are
collected HERE and passed to the engine as tokens -- the engine never
blocks on stdin. In mock mode nothing energizes and no confirmation is
asked.
"""
import argparse
import json
import os
import sys

import core  # noqa: F401  (injects lib/ onto sys.path first)
from core import checkpoint as _checkpoint
from core import feasibility as _feasibility
from core import lifefactors as _lifefactors
from core import recipe as _recipe
from core import runstore as _runstore
from core import safety as _safety
from core.clock import Clock, SimClock
from core.engine import LifecycleEngine
from core.registry import Registry, RegistryError


def _load_config(path):
    cfg = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as fh:
            cfg = json.load(fh)
    return cfg


def _mk_hal(mode, clock, cfg, geometry='planar', mock_script=None):
    if mode == 'mock':
        from hal import mock as halmock
        return halmock.build(clock, script=mock_script)
    # dry / live: real environment + real vision; instruments live-only
    # (dry never commands HV and may run instrument-less anywhere).
    from hal import HALBundle
    from hal import mock as halmock
    from hal.env_manual import ManualEnv
    if geometry == 'bender':
        from vision.bender import BenderVision
        vision = BenderVision(cfg.get('bender_vision') or {})
    else:
        from vision.planar import PlanarVision
        vision = PlanarVision(cfg.get('planar_vision') or {})

    drive = monitor = camera = None
    if mode == 'live':
        import instruments  # vendored; lazy-imports pyvisa
        from hal.camera_dfk import DFKCamera
        from hal.drive_bk4055b import BK4055BDrive
        from hal.monitor_mso24 import MSO24Monitor
        sign = -1.0 if cfg.get('trek_inverts') else 1.0
        sg_res = (cfg.get('sg_resource') or '').strip() \
            or os.environ.get('SCPI_SG_LAN') or None
        try:
            sg = instruments.BK4055B(resource=sg_res)
            drive = BK4055BDrive(sg, int(cfg.get('sg_channel', 1)),
                                 trek_sign=sign)
        except Exception as e:
            print(f'signal generator not reachable: {e}')
        try:
            scope = instruments.TekMSO24()
            monitor = MSO24Monitor(scope,
                                   int(cfg.get('scope_v_channel', 2)),
                                   int(cfg.get('scope_i_channel', 3)),
                                   trek_sign=sign)
        except Exception as e:
            print(f'scope not reachable: {e}')
        camera = DFKCamera(int(cfg.get('camera_index', 0)))
        if not camera.available():
            camera = None
    else:
        # dry: sequence against the mock rig (nothing energizes), but
        # keep the REAL attestation + vision flows
        mockb = halmock.build(clock, script=mock_script)
        drive, monitor, camera = mockb.drive, mockb.monitor, \
            mockb.camera
    bundle = HALBundle('live' if mode == 'live' else 'dry-mockrig',
                       drive, monitor, camera, ManualEnv(clock),
                       vision)
    return bundle


def _resolve(args, cfg):
    rec = _recipe.Recipe.load(args.recipe)
    data_root = _runstore.check_data_root(
        args.data_root or cfg.get('data_root') or
        _runstore.default_data_root())
    reg = Registry(data_root)
    row = reg.get(args.specimen)
    cap = reg.cap_kv(args.specimen)
    c_est = None
    if (row.get('c_est_nf') or '').strip():
        try:
            c_est = float(row['c_est_nf'])
        except ValueError:
            pass
    resolved = _recipe.resolve(rec, row, cap, c_est_nf=c_est)
    return rec, resolved, reg, row, cap, data_root


def cmd_validate(args, cfg):
    try:
        rec, resolved, reg, row, cap, _root = _resolve(args, cfg)
    except (_recipe.RecipeError, RegistryError,
            _runstore.RunStoreError) as e:
        print(f'INVALID:\n{e}')
        return 2
    print(f"OK: recipe '{resolved['name']}' resolves for specimen "
          f"{row['specimen_id']} (cap {cap:g} kV)")
    drv = resolved['drive']
    print(f"  drive: {drv['waveform']} {drv['freq_hz']:g} Hz, "
          f"{drv['v_pk_kv']:g} kV pk ({drv['v_min_kv']:g} kV min)")
    feas = resolved.get('feasibility') or {}
    for m in feas.get('msgs', []):
        print(f'  feasibility: {m}')
    if feas and not feas.get('msgs'):
        print(f"  feasibility: ok ({_feasibility.summary(feas)})")
    print(f"  planned cycles: {resolved['planned_cycles']:,}  "
          f"cap {resolved['stop']['max_cycles']:,}")
    for line in _lifefactors.advisory_lines(
            resolved['stop']['max_cycles'],
            operational=resolved['stop']['max_cycles'] // 2,
            ground=0, functional=0):
        print(f'  advisory: {line}')
    print(f"  sha256: {resolved['sha256']}")
    return 0


def _collect_confirmations(args, resolved, cap, hal, band):
    """Typed confirmations for live mode: interactive prompts, or the
    GUI passes tokens it collected in ITS typed dialogs via --confirm
    (the typing still happened; this is transport, not a bypass)."""
    tokens = set(args.confirm or [])
    if args.mode != 'live' or tokens:
        return tokens
    drv = resolved['drive']
    env = hal.env.current() if hal.env else None
    note = _safety.paschen_note(None if env is None else env.get('p_pa'),
                                band)
    if note:
        print(f'NOTE (Paschen advisory): {note}')
    print(f"About to ENERGIZE: {drv['v_pk_kv']:g} kV pk on specimen "
          f"{resolved['specimen_id']} (hard cap {cap:g} kV), "
          f"{resolved['planned_cycles']:,} planned cycles.")
    print(f"Type {_safety.CONFIRM_ENERGIZE} to arm (anything else "
          f"aborts):")
    if input('> ').strip() == _safety.CONFIRM_ENERGIZE:
        tokens.add(_safety.CONFIRM_ENERGIZE)
    return tokens


def cmd_run(args, cfg, resume=False):
    try:
        rec, resolved, reg, row, cap, data_root = _resolve(args, cfg)
    except (_recipe.RecipeError, RegistryError,
            _runstore.RunStoreError) as e:
        print(f'INVALID:\n{e}')
        return 2
    clock = SimClock() if (args.mode == 'mock' and args.sim_time) \
        else Clock()
    hal = _mk_hal(args.mode, clock, cfg, geometry=resolved['geometry'],
                  mock_script=json.loads(args.mock_script)
                  if args.mock_script else None)

    spec_dir = os.path.join(data_root, row['specimen_id'])
    if resume:
        run_dir = args.run_dir
        if not run_dir or not os.path.isdir(run_dir):
            print('resume needs --run-dir pointing at the run folder')
            return 2
    else:
        run_dir = os.path.join(spec_dir, args.run_name
                               or _runstore.run_dirname(clock.now()))
    run_id = os.path.basename(run_dir)

    if args.mode in ('live', 'dry') and hasattr(hal.env, 'attest'):
        if args.attest:
            t_c, p_mbar, op = args.attest.split(',', 2)
            hal.env.attest(float(t_c), float(p_mbar), op.strip(),
                           clock.now_iso())
        else:
            print('Attest chamber conditions (manual environment).')
            t_c = float(input('  chamber T [C]: ').strip())
            p_mbar = float(input('  chamber p [mbar]: ').strip())
            op = input('  your name: ').strip()
            hal.env.attest(t_c, p_mbar, op, clock.now_iso())

    band = tuple(cfg.get('paschen_warn_pa')
                 or _safety.DEFAULT_PASCHEN_BAND_PA)
    tokens = _collect_confirmations(args, resolved, cap, hal,
                                    band)

    if args.mode != 'mock':
        reg.mark_in_test(row['specimen_id'], run_id)
    eng = LifecycleEngine(
        resolved, row, cap, hal, run_dir, clock, args.mode,
        registry=reg if args.mode != 'mock' else None,
        confirmations=tokens, paschen_band_pa=band, resume=resume)
    disposition = eng.run()
    print(f'\ndisposition: {disposition}')
    print(f'run folder:  {run_dir}')
    return 0 if disposition in ('complete', 'suspended') else 1


def cmd_status(args, cfg):
    path = os.path.join(args.run_dir, 'status.json')
    if not os.path.exists(path):
        print(f'no status.json in {args.run_dir}')
        return 2
    with open(path, 'r', encoding='utf-8') as fh:
        d = json.load(fh)
    for k in sorted(d):
        print(f'{k}: {d[k]}')
    if _checkpoint.unclean(args.run_dir):
        print('\nNOTE: checkpoint marks an UNCLEAN shutdown -- '
              'resumable via: lifecycle_cli.py resume ...')
    return 0


def cmd_feasibility(args, cfg):
    rep = _feasibility.check_drive(args.freq, args.c_nf, args.v_pk,
                                   waveform=args.waveform)
    if rep.get('edge_s') is not None:
        print(f"square edge = {1000 * rep['edge_s']:.1f} ms "
              f"({100 * rep['edge_frac']:.1f}% of the half-period) at "
              f"{rep['i_pk_ua']:.0f} uA (Trek-limited)")
    else:
        print(f"I_pk = {rep['i_pk_ua']:.1f} uA "
              f"({100 * rep['i_frac']:.0f}% of Trek +/-2 mA)")
    print(f"max feasible f at this C/V: {rep['max_feasible_hz']:.2f} Hz")
    print(f"verdict: {rep['verdict']}")
    for m in rep['msgs']:
        print(f'  {m}')
    return 0


def cmd_specimen(args, cfg):
    data_root = _runstore.check_data_root(
        args.data_root or cfg.get('data_root') or
        _runstore.default_data_root())
    reg = Registry(data_root)
    if args.action == 'list':
        for sid in reg.ids():
            row = reg.get(sid)
            print(f"{sid:20s} {row['geometry']:14s} {row['status']:10s} "
                  f"cycles {row['cycles_accum'] or 0}")
    elif args.action == 'show':
        row = reg.get(args.id)
        for k, v in row.items():
            if v != '':
                print(f'{k}: {v}')
        print(f'hard cap: {reg.cap_kv(args.id):g} kV')
    elif args.action == 'create':
        fields = {}
        for kv in args.field or []:
            k, _, v = kv.partition('=')
            fields[k] = v
        row = reg.create(args.id, args.geometry,
                         Clock().now_iso(timespec='seconds'), **fields)
        print(f"created {row['specimen_id']} "
              f"(cap {reg.cap_kv(args.id):g} kV)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='lifecycle_cli')
    ap.add_argument('--config', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'config.json'))
    sub = ap.add_subparsers(dest='cmd', required=True)

    def add_run_args(p):
        p.add_argument('recipe')
        p.add_argument('--specimen', required=True)
        p.add_argument('--mode', choices=_safety.MODES, default='dry')
        p.add_argument('--data-root', default=None)
        p.add_argument('--sim-time', action='store_true',
                       help='mock only: simulated clock (instant)')
        p.add_argument('--mock-script', default=None,
                       help='JSON overrides for the mock degradation '
                            'model (e.g. {"breakdown_at_cycle": 5000})')
        p.add_argument('--confirm', action='append', default=None,
                       help='typed confirmation token collected by a '
                            'front end (repeatable); replaces the '
                            'interactive prompt')
        p.add_argument('--attest', default=None,
                       help='"T_C,P_MBAR,NAME" initial environment '
                            'attestation (front-end collected)')
        p.add_argument('--run-name', default=None,
                       help='override the auto run dir name')

    p = sub.add_parser('validate')
    p.add_argument('recipe')
    p.add_argument('--specimen', required=True)
    p.add_argument('--data-root', default=None)

    p = sub.add_parser('run')
    add_run_args(p)

    p = sub.add_parser('resume')
    add_run_args(p)
    p.add_argument('--run-dir', required=True)

    p = sub.add_parser('status')
    p.add_argument('run_dir')

    p = sub.add_parser('feasibility')
    p.add_argument('--freq', type=float, required=True)
    p.add_argument('--c-nf', type=float, required=True)
    p.add_argument('--v-pk', type=float, required=True)
    p.add_argument('--waveform', default='SINE',
                   choices=('SINE', 'SQUARE', 'RAMP'))

    p = sub.add_parser('specimen')
    p.add_argument('action', choices=('list', 'show', 'create'))
    p.add_argument('id', nargs='?')
    p.add_argument('--geometry', default='custom')
    p.add_argument('--field', action='append',
                   help='k=v registry field (repeatable)')
    p.add_argument('--data-root', default=None)

    args = ap.parse_args(argv)
    cfg = _load_config(args.config)
    if args.cmd == 'validate':
        return cmd_validate(args, cfg)
    if args.cmd == 'run':
        return cmd_run(args, cfg)
    if args.cmd == 'resume':
        return cmd_run(args, cfg, resume=True)
    if args.cmd == 'status':
        return cmd_status(args, cfg)
    if args.cmd == 'feasibility':
        return cmd_feasibility(args, cfg)
    if args.cmd == 'specimen':
        return cmd_specimen(args, cfg)
    return 2


if __name__ == '__main__':
    sys.exit(main())
