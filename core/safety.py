"""Mode ladder, gate chain, Paschen advisory, typed confirmations.

Every gate carries its provenance -- the Digital Multitool incident or
audit that forced it. The chain runs before ARMED; each gate returns a
GateResult and the engine refuses to arm unless every required gate
passed. docs/SAFETY.md restates this file in prose.

Modes: mock -> dry (default) -> live. Promotion is explicit per
invocation (--mode live); nothing ever stores an armed state (the
sldea_presets rule). live additionally requires Linux
(INSTRUMENTS_SUPPORTED lineage, gui.py:178).
"""
import sys

from . import feasibility as _feasibility

MODES = ('mock', 'dry', 'live')

# Typed confirmations: the engine never blocks on stdin itself -- the
# CLI/GUI collects the text and passes it in as a command. The token
# must match VERBATIM.
CONFIRM_ENERGIZE = 'ENERGIZE'
CONFIRM_BLIND = 'BLIND'                       # scope missing, run anyway


# Paschen band [Pa] for the ADVISORY note (config.json paschen_warn_pa).
# Until 2026-09-29 this band inhibited HV behind a typed override; the
# author's decision that day (docs/MOTIVATION.md, roadmap item 4) made it
# a warning: encapsulated specimens run at 4-5 kPa on purpose (the
# stratosphere-paper vacuum condition sits inside this band), and an
# operator is never locked out of a pressure.
DEFAULT_PASCHEN_BAND_PA = (1.0, 10000.0)


class GateResult:
    def __init__(self, gate, ok, detail='', fix=None, warn=''):
        self.gate = gate
        self.ok = ok
        self.detail = detail
        self.fix = fix                # optional callable: one-click fix
        self.warn = warn              # advisory text; never blocks arming

    def __repr__(self):
        return f'GateResult({self.gate}, {"PASS" if self.ok else "FAIL"})'


def platform_ok(mode):
    """live drives instruments through PyVISA-py/USB-TMC, which this lab
    runs on the RHEL9 bench only (gui.py:178 semantics)."""
    if mode != 'live':
        return GateResult('platform', True, f'mode {mode}: any OS')
    if sys.platform.startswith('linux'):
        return GateResult('platform', True, 'linux bench')
    return GateResult(
        'platform', False,
        f'live mode requires the Linux bench (this is {sys.platform}); '
        f'use --mode mock or --mode dry here')


def paschen_note(p_pa, band_pa):
    """Advisory text for an attested pressure, or '' when there is
    nothing to say. Never a reason to refuse HV (see
    DEFAULT_PASCHEN_BAND_PA).

    The band (default 1 Pa .. 10 kPa) covers the Paschen-minimum region
    where the breakdown voltage of mm-scale gaps collapses; pump-down
    and vent both transit it. Inside it, exposed conductors and lead
    gaps can arc even when the encapsulated specimen is fine."""
    if p_pa is None:
        return 'no pressure attested -- Paschen advisory not evaluated'
    lo, hi = float(band_pa[0]), float(band_pa[1])
    if lo <= float(p_pa) <= hi:
        return (f'attested pressure {float(p_pa):g} Pa is inside the '
                f'Paschen band {lo:g}..{hi:g} Pa: exposed conductors and '
                f'mm-scale gaps can arc here. Encapsulate electrodes and '
                f'check lead spacing. Advisory only -- HV is not blocked.')
    return ''


def gate_chain(mode, hal, resolved, cap_kv, run_id, env_sample,
               paschen_band_pa, confirmations, log):
    """Run every pre-arm gate; returns (all_ok, [GateResult]).

    `confirmations` is a set of typed tokens already collected by the
    front end (CONFIRM_ENERGIZE, CONFIRM_BLIND).
    Ordering matters and is preserved from the proven chain
    (gui.py:3075-3230): platform -> drive present -> monitor present or
    typed BLIND -> monitor windows -> feasibility -> specimen cap ->
    Paschen (advisory: always passes, may carry a warning) -> ENERGIZE.
    Camera preflight and the watchdog baseline
    learn run AFTER arming, inside the executors, exactly as upstream.
    """
    results = []

    def add(gate, ok, detail='', fix=None, warn=''):
        r = GateResult(gate, ok, detail, fix, warn)
        results.append(r)
        log(f"gate {gate}: {'PASS' if ok else 'FAIL'}"
            + (' (WARNING)' if warn else '')
            + (f' -- {detail}' if detail else ''))
        return ok

    add('platform', *_split(platform_ok(mode)))

    if mode == 'live':
        add('drive_connected', hal.drive is not None and
            hal.drive.connected(),
            'signal generator reachable' if hal.drive and
            hal.drive.connected() else 'signal generator NOT reachable')
    else:
        add('drive_connected', True, f'{mode}: drive not required live')

    mon_ok = hal.monitor is not None and hal.monitor.connected()
    if mon_ok:
        add('monitor_connected', True, 'scope reachable')
        problems, fixplan = hal.monitor.check_window(
            max_kv=max(resolved['drive'].get('v_pk_kv') or 0.0,
                       resolved['reference']['ref_kv']),
            trip_ua=_trip_ua(resolved),
            freq_hz=resolved['drive']['freq_hz'])
        add('monitor_window', not problems,
            '; '.join(problems) if problems else
            'monitor vertical + horizontal windows sane',
            fix=fixplan)
    elif mode == 'live':
        # Scope-less live run: the watchdog is blind from the start.
        # Allowed only behind a typed confirmation (upstream askyesno
        # default-no, hardened to typed for unattended lifecycle runs).
        add('monitor_connected', CONFIRM_BLIND in confirmations,
            'scope NOT reachable -- type BLIND to run without current '
            'monitoring (watchdog disabled; strongly discouraged for '
            'lifecycle runs)')
    else:
        add('monitor_connected', True, f'{mode}: scope not required')

    feas = resolved.get('feasibility')
    if feas is None:
        add('feasibility', True, 'no drive voltage resolved (no cycling)')
    else:
        add('feasibility', feas['verdict'] != 'refuse',
            '; '.join(feas['msgs']) or _feasibility.summary(feas))

    vpk = resolved['drive'].get('v_pk_kv') or 0.0
    add('specimen_cap', vpk <= cap_kv + 1e-9,
        f'drive {vpk:g} kV vs hard cap {cap_kv:g} kV (admin_caps.json)')

    p_pa = None if env_sample is None else env_sample.get('p_pa')
    note = paschen_note(p_pa, paschen_band_pa)
    add('paschen', True,
        note or f'attested pressure {float(p_pa):g} Pa outside the '
                f'Paschen band',
        warn=note)

    if mode == 'live':
        add('energize_confirm', CONFIRM_ENERGIZE in confirmations,
            f"type {CONFIRM_ENERGIZE} to arm: {vpk:g} kV peak on "
            f"specimen {resolved['specimen_id']} (cap {cap_kv:g} kV), "
            f"{resolved['planned_cycles']:,} planned cycles")
    else:
        add('energize_confirm', True, f'{mode}: HV never commanded')

    return all(r.ok for r in results), results


def _split(r):
    return r.ok, r.detail


def _trip_ua(resolved):
    for rule in resolved['failure_rules']['fast']:
        if rule['id'] == 'overcurrent':
            return float(rule['threshold'])
    return 100.0
