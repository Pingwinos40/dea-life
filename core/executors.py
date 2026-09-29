"""Per-block execution against the HAL: the re-homed run-executor logic.

The load-bearing patterns here are lifted from the Digital Multitool's
battle-tested `_sldea_worker` (gui.py:3669-4100) and `_sldea_capture`
(gui.py:4108-4190), re-expressed headless:

- watchdog baseline learn: 0.5 s settle, 10 reads, discard first 2,
  median of >= 4, credible_baseline_ua gate (gui.py:3804-3852)
- one monitor read per tick shared by watchdog + records (#157 premise)
- monitoring-loss ladder: 10 s -> loud BLIND warn, run continues
  (policy 2026-07-25); 60 s -> the monitor_loss FAST rule pauses at
  0 kV (new default for unattended lifecycle runs)
- trip path ordering: hold_flush FIRST, then the event row, then the
  evidence, then zero (gui.py:3951-3966 -- the two writes on this path
  once measured +4 s of HV-live time on a stalled share)
- capture: per-reading conversion straight after its own read; frame
  filename recorded only after a successful imwrite (gui.py:4146-4177)
"""
import collections
import csv
import os
import statistics

import sldea_profile  # vendored

from . import cycles as _cycles
from . import safety as _safety
from .failure import Firing

MONITOR_TICK_S = 0.5      # bench-validated watchdog cadence (gui.py:3866)
ENGINE_POLL_S = 0.05
RING_SAMPLES = 120        # ~60 s of monitor history frozen on events
BLIND_WARN_S = 10.0       # loud warn threshold (policy 2026-07-25)


class HardTrip(Exception):
    """A FAST abort-action rule confirmed. Carries the Firing."""

    def __init__(self, firing):
        self.firing = firing
        super().__init__(firing.message)


class StopRequested(Exception):
    """Operator stop/abort command."""


class SuspendRequested(Exception):
    """Operator right-censor: end the run as 'suspended'."""


class CapReached(Exception):
    """stop.max_cycles or max_wall_h reached: natural end."""


class RunContext:
    """Everything one run's executors share."""

    def __init__(self, hal, store, ledger, rules, clock, log, status,
                 resolved, mode, process_commands):
        self.hal = hal
        self.store = store
        self.ledger = ledger
        self.rules = rules
        self.clock = clock
        self.log = log
        self.status = status
        self.resolved = resolved
        self.mode = mode                     # mock | dry | live
        self.process_commands = process_commands   # engine callback
        self.t0 = clock.monotonic()
        self.wall_offset_s = 0.0             # resumed prior wall time
        self.baseline = None                 # set by set_baseline
        self.watchdog = None
        self.telemetry = None
        self.ring = collections.deque(maxlen=RING_SAMPLES)
        self.flags = []                      # active AMBER flags
        self.stop = False
        self.pause = False
        self.suspend = False
        self.interlude_idx = 0
        self.subrun_idx = 0
        self._last_mon_t = -1e9
        self._blind_since = None
        self._blind_warned = False
        self._last_status_t = -1e9
        self._trace_idx = 0
        self._partial_cycles = 0             # abort-mid-chunk credit
        self.recent_events = collections.deque(maxlen=10)
        self.hv_allowed = True               # False after hard trip
        self.paschen_band_pa = _safety.DEFAULT_PASCHEN_BAND_PA

    # ---- time ----------------------------------------------------------
    def el(self):
        return self.clock.monotonic() - self.t0

    def wall_s(self):
        return self.wall_offset_s + self.el()

    # ---- events --------------------------------------------------------
    def event(self, tier, rule_id, action, value='', threshold='',
              message='', trace_file='', frame_file='', operator=''):
        self.log(f'[{tier}/{rule_id}] {action}: {message}')
        row = {'t_iso': self.clock.now_iso(), 'rule_id': rule_id,
               'message': message}
        self.recent_events.append(row)
        self.store.event(self.clock.now_iso(), self.wall_s(),
                         self.ledger, tier, rule_id, action, value,
                         threshold, message, trace_file, frame_file,
                         operator)

    def poll(self):
        """Service commands + stop caps; called inside every wait loop."""
        self.process_commands(self)
        if self.suspend:
            raise SuspendRequested()
        if self.stop:
            raise StopRequested()
        cap = self.resolved['stop']
        if self.ledger.cycles >= cap['max_cycles']:
            raise CapReached(f"cycle cap {cap['max_cycles']:,} reached")
        if self.wall_s() >= cap['max_wall_h'] * 3600.0:
            raise CapReached(f"wall cap {cap['max_wall_h']:g} h reached")

    def sleep_poll(self, seconds):
        """Sleep in ENGINE_POLL_S slices, polling commands throughout."""
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end:
            self.poll()
            self.clock.sleep(min(ENGINE_POLL_S,
                                 end - self.clock.monotonic()))
        self.poll()

    # ---- the shared monitor tick ---------------------------------------
    def monitor_tick(self, v_expected_kv=None, v_span_kv=None,
                     force=False):
        """One I read (+ occasional V read) feeding the watchdog, the
        FAST rules, the ring buffer, and blind tracking. Called from
        every energized wait loop at MONITOR_TICK_S cadence."""
        el = self.el()
        if not force and el - self._last_mon_t < MONITOR_TICK_S:
            return
        self._last_mon_t = el
        mon = self.hal.monitor
        if mon is None or not mon.connected():
            return
        ua, ist = mon.read_ua()
        ioff = ist == 'offscreen'
        self.ring.append((round(el, 3), ua, ist))

        # -- monitoring-loss ladder (gui.py:3907-3922 + FAST rule)
        blind_s = 0.0
        if ua is None and not ioff:
            if self._blind_since is None:
                self._blind_since = el
            blind_s = el - self._blind_since
            if blind_s >= BLIND_WARN_S and not self._blind_warned:
                self._blind_warned = True
                self.event('fast', 'monitor_loss', 'warn',
                           value=round(blind_s, 1), threshold=BLIND_WARN_S,
                           message='CURRENT MONITORING LOST -- scope '
                                   'unreadable for 10 s'
                                   + (', breakdown watchdog is BLIND'
                                      if self.watchdog else '')
                                   + '. Run continues; the monitor_loss '
                                     'rule pauses at 60 s.')
        else:
            if self._blind_warned:
                self.event('fast', 'monitor_loss', 'log',
                           message='current monitoring recovered')
            self._blind_since, self._blind_warned = None, False

        # -- the vendored watchdog decides FIRST (trip beats telemetry)
        if self.watchdog is not None and \
                self.watchdog.update(el, ua, offscreen=ioff):
            firing = self.rules.report_watchdog_trip(
                'OFFSCREEN' if self.watchdog.last_offscreen
                else round(self.watchdog.last_ua or 0.0, 1))
            self._on_fast_trip(firing, ua, ist)

        # -- drive fidelity (every other tick: the V read is a second
        #    locked round-trip on real hardware)
        metrics = {'blind_s': blind_s if blind_s else None}
        if v_expected_kv is not None and v_expected_kv > 0.05:
            mkv, vst = mon.read_stat('MEAN', 'v')
            if mkv is not None:
                metrics['v_err_frac'] = abs(mkv - v_expected_kv) / \
                    v_expected_kv
        for f in self.rules.eval_fast(el, metrics):
            self._apply_firing(f)
        return ua, ist

    def _on_fast_trip(self, firing, ua, ist):
        """gui.py:3951-3966 ordering, faithfully: hold_flush FIRST, then
        the event row, then evidence, then the raise that zeroes HV."""
        if self.telemetry is not None:
            self.telemetry.hold_flush = True
            try:
                self.telemetry.event(
                    self.el(), self.clock.now_iso(), 0.0,
                    firing.rule_id.upper() + ' CONFIRMED',
                    ua=ua, i_status=ist)
            except Exception:
                pass
        trace = self.freeze_ring(firing.rule_id)
        frame = self._best_effort_frame(f'{firing.rule_id}_trip')
        self.event(firing.tier, firing.rule_id, 'abort',
                   value=firing.value, threshold=firing.threshold,
                   message=firing.message + ' -- zeroing HV and aborting',
                   trace_file=trace, frame_file=frame)
        self.hv_allowed = False
        raise HardTrip(firing)

    def _apply_firing(self, f):
        if f.action == 'abort':
            self._on_fast_trip(f, None, '')
        elif f.action == 'pause':
            self.event(f.tier, f.rule_id, 'pause', value=f.value,
                       threshold=f.threshold, message=f.message)
            self.pause = True
        elif f.action in ('warn', 'flag', 'log'):
            if f.is_flag:
                self.add_flag(f)
            else:
                self.event(f.tier, f.rule_id, f.action, value=f.value,
                           threshold=f.threshold, message=f.message)

    def add_flag(self, firing):
        flag = {'id': firing.rule_id, 'tier': firing.tier,
                'value': firing.value, 'threshold': firing.threshold,
                'message': f'POTENTIAL FAILURE: {firing.message}',
                'raised_iso': self.clock.now_iso(), 'acked_by': ''}
        self.flags.append(flag)
        self.event(firing.tier, firing.rule_id, 'flag',
                   value=firing.value, threshold=firing.threshold,
                   message=flag['message'])

    # ---- evidence ------------------------------------------------------
    def freeze_ring(self, label):
        """Dump the monitor ring to traces/; returns the relative path.
        Never raises (evidence must not block the shutdown)."""
        self._trace_idx += 1
        name = f'evt{self._trace_idx:04d}_{label}_monitor.csv'
        try:
            path = os.path.join(self.store.traces_dir, name)
            with open(path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(['t_s', 'measured_uA', 'i_status'])
                for row in self.ring:
                    w.writerow(row)
            return os.path.join('traces', name)
        except Exception:
            return ''

    def freeze_waveform(self, label):
        """Pull the scope's own acquisition memory AFTER HV is zeroed --
        it holds the pre-trigger history; capturing the shutdown too is
        evidence, not contamination."""
        mon = self.hal.monitor
        if mon is None or not mon.connected():
            return ''
        try:
            wf = mon.capture_waveform('i')
            if not wf:
                return ''
            name = f'evt{self._trace_idx:04d}_{label}_scope.csv'
            path = os.path.join(self.store.traces_dir, name)
            with open(path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(['t_s', 'value'])
                for t, v in zip(wf['t'], wf['v']):
                    w.writerow([t, v])
            return os.path.join('traces', name)
        except Exception:
            return ''

    def _best_effort_frame(self, tag):
        cam = self.hal.camera
        if cam is None or not cam.available():
            return ''
        try:
            frame = cam.grab()
            if frame is None:
                return ''
            name = f'EVT_{tag}_{self.ledger.cycles}.png'
            return _write_frame(self.store.frames_dir, name, frame)
        except Exception:
            return ''

    def save_frame(self, name):
        cam = self.hal.camera
        if cam is None or not cam.available():
            return '', None
        try:
            frame = cam.grab()
        except Exception as e:
            self.log(f'capture error: {e}')
            return '', None
        if frame is None:
            return '', None
        return _write_frame(self.store.frames_dir, name, frame), frame

    # ---- status --------------------------------------------------------
    def push_status(self, **extra):
        el = self.el()
        if el - self._last_status_t < 2.0 and not extra.get('force'):
            return
        self._last_status_t = el
        extra.pop('force', None)
        base = self.baseline or {}
        self.status.update(
            cycles_total=self.ledger.cycles,
            actuated_s_total=round(self.ledger.actuated_s, 1),
            wall_s=round(self.wall_s(), 1),
            cycles_target=self.resolved['stop']['max_cycles'],
            flags=list(self.flags),
            health='RED' if not self.hv_allowed
                   else ('AMBER' if self.flags else 'GREEN'),
            recent_events=list(self.recent_events),
            **extra)


def _write_frame(framedir, name, frame):
    """Frame filename recorded only after a REAL write (gui.py:4164)."""
    try:
        import cv2
        import numpy as np
        path = os.path.join(framedir, name)
        bgr = cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR)
        if not cv2.imwrite(path, bgr):
            raise IOError('imwrite returned False')
        return name
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# watchdog arming
# ---------------------------------------------------------------------------

def learn_watchdog_baseline(ctx, trip_ua, confirm_s):
    """gui.py:3804-3852 verbatim in spirit: settle, 10 reads at 0.1 s,
    discard the first 2, median of >= 4, credible_baseline_ua gate."""
    wd = sldea_profile.BreakdownWatchdog(trip_ua, confirm_s)
    mon = ctx.hal.monitor
    if mon is None or not mon.connected():
        ctx.log('watchdog: no monitor -- DISABLED (blind run)')
        return None
    ctx.clock.sleep(0.5)
    base = []
    for k in range(10):
        try:
            ua, _st = mon.read_ua()
        except Exception:
            ua = None
        if k >= 2 and ua is not None:
            base.append(ua)
        ctx.clock.sleep(0.1)
    if len(base) >= 4:
        med = statistics.median(base)
        if sldea_profile.credible_baseline_ua(med, trip_ua):
            wd.baseline_ua = med
            ctx.log(f'watchdog baseline {med:.1f} uA (median of '
                    f'{len(base)} reads at 0 kV); trip |I-baseline| >= '
                    f'{trip_ua:g} uA for {confirm_s:g}s')
        else:
            ctx.log(f'watchdog baseline {med:.1f} uA is NOT a credible '
                    f'0 kV rest level -- keeping absolute trip '
                    f'|I| >= {trip_ua:g} uA (a standing fault current '
                    f'must trip, not normalize)')
    else:
        ctx.log(f'watchdog baseline unavailable ({len(base)}/8 reads '
                f'ok) -- absolute trip |I| >= {trip_ua:g} uA')
    return wd


# ---------------------------------------------------------------------------
# block executors
# ---------------------------------------------------------------------------

def run_cycle_block(ctx, block, idx):
    """Counted-burst cycling with per-chunk verification; falls back to
    timed counting. Writes one blocks.csv row whatever happens."""
    drive = ctx.hal.drive
    mon = ctx.hal.monitor
    freq = block['freq_hz']
    vpk, vmin = block['v_pk_kv'], block['v_min_kv']
    waveform = block['waveform']
    planned = block['n_cycles']
    counting_req = ctx.resolved['counting']
    t_start = ctx.clock.now_iso()
    el_start = ctx.el()
    done = 0
    mode_used = counting_req
    freq_meas_last = None
    vstats = {}
    istats = {}
    verdict = 'ok'
    notes = []
    energize = ctx.mode != 'dry' and ctx.hv_allowed

    try:
        if energize:
            drive.configure_cycle(waveform, freq, vpk, vmin)
            # ---- burst idle-level gate (the STPS trap): before the
            # first HV chunk, the armed-but-idle output must sit at
            # v_min, not at the offset midpoint.
            if counting_req == 'burst' and mon is not None and \
                    mon.connected():
                ctx.clock.sleep(0.2)
                mkv, _st = mon.read_stat('MEAN', 'v')
                tol = 0.05 * max(vpk - vmin, 0.1) + 0.05
                if mkv is None or abs(mkv - vmin) > tol:
                    mode_used = 'timed'
                    ctx.event(
                        'fast', 'burst_idle', 'warn',
                        value='' if mkv is None else round(mkv, 3),
                        threshold=round(vmin, 3),
                        message=f'burst idle level '
                                f'{"unreadable" if mkv is None else f"{mkv:.3f} kV"} '
                                f'!= v_min {vmin:g} kV -- burst counting '
                                f'REFUSED (STPS unsupported/failed?); '
                                f'degrading to timed counting')

        if mode_used == 'burst' and energize:
            done, freq_meas_last = _run_burst_chunks(
                ctx, drive, mon, block, planned, vstats, istats)
        elif energize:
            done, freq_meas_last = _run_timed(ctx, drive, mon, block,
                                              planned)
            mode_used = 'timed'
        else:
            # dry / post-trip: sequence timing only, nothing energized
            ctx.sleep_poll(min(planned / freq, 2.0))
            done = 0
            mode_used = 'dry'
            notes.append('dry: SG never commanded')
    except (HardTrip, StopRequested, SuspendRequested, CapReached) as e:
        # Partial chunk: credit f * elapsed as 'estimated', then the
        # engine handles the outcome. The row still gets written.
        verdict = type(e).__name__
        notes.append(str(e) or verdict)
        raise
    finally:
        if energize:
            try:
                drive.end_cycle()
            except Exception as e:
                notes.append(f'end_cycle error: {e}')
        partial = getattr(ctx, '_partial_cycles', 0)
        if partial:
            done += partial
            mode_used = 'estimated'
            ctx._partial_cycles = 0
        added = ctx.ledger.credit_cycles(done, freq, waveform,
                                         mode_used)
        env = ctx.hal.env.current() if ctx.hal.env else None
        ctx.store.blocks.write({
            'block_idx': idx, 'loop_path': block.get('loop_path', ''),
            'type': 'cycle', 'role': block.get('role', ''),
            't_start_iso': t_start, 't_end_iso': ctx.clock.now_iso(),
            'wall_s': round(ctx.el() - el_start, 2),
            'cycles_planned': planned, 'cycles_done': done,
            'counting': mode_used, 'cycles_total': ctx.ledger.cycles,
            'actuated_s_block': round(added, 2),
            'actuated_s_total': round(ctx.ledger.actuated_s, 1),
            'waveform': waveform, 'freq_cmd_hz': freq,
            'freq_meas_hz': '' if freq_meas_last is None
            else round(freq_meas_last, 4),
            'vpk_cmd_kv': vpk, 'vmin_cmd_kv': vmin,
            'vmean_meas_kv': _r(vstats.get('MEAN'), 4),
            'vpk_meas_kv': _r(vstats.get('MAXIMUM'), 4),
            'vvalley_meas_kv': _r(vstats.get('MINIMUM'), 4),
            'imean_ua': _r(istats.get('MEAN'), 2),
            'ipk_ua': _r(istats.get('MAXIMUM'), 2),
            'ivalley_ua': _r(istats.get('MINIMUM'), 2),
            'env_t_c': '' if not env else env.get('t_c', ''),
            'env_p_mbar': '' if not env else env.get('p_mbar', ''),
            'env_age_s': '' if not env else round(env.get('age_s', 0)),
            'soft_flags': len(ctx.flags), 'verdict': verdict,
            'notes': '; '.join(notes),
        })


def _run_burst_chunks(ctx, drive, mon, block, planned, vstats, istats):
    """Returns (cycles_done, last_measured_freq). vstats/istats are
    filled MID-BURST (the recorded stats must describe the cycling
    waveform, not the idle level between chunks -- first shakedown
    recorded 0 kV means for a 1.5 kV run, 2026-08-23)."""
    freq = block['freq_hz']
    done = 0
    freq_last = None
    for chunk in _cycles.plan_chunks(planned):
        ctx.poll()
        if ctx.pause:
            _pause_point(ctx, drive)
        chunk_t0 = ctx.clock.monotonic()
        try:
            drive.fire_burst(chunk)
            wait = _cycles.expected_burst_s(chunk, freq)
            end = ctx.clock.monotonic() + wait
            mid = ctx.clock.monotonic() + wait / 2.0
            freq_seen = None
            stats_done = False
            while ctx.clock.monotonic() < end:
                ctx.monitor_tick(
                    v_expected_kv=0.5 * (block['v_pk_kv']
                                         + block['v_min_kv']))
                if mon is not None and freq_seen is None:
                    f, _st = mon.read_stat('FREQUENCY', 'v')
                    if f is not None:
                        freq_seen = f
                        freq_last = f
                if (mon is not None and not stats_done
                        and ctx.clock.monotonic() >= mid):
                    stats_done = True
                    for kind in ('MEAN', 'MAXIMUM', 'MINIMUM'):
                        val, _ = mon.read_stat(kind, 'v')
                        if val is not None:
                            vstats[kind] = val
                        val, _ = mon.read_stat(kind, 'i')
                        if val is not None:
                            istats[kind] = val
                ctx.poll()
                ctx.push_status(state='RUNNING', kv_cmd=block['v_pk_kv'])
                ctx.clock.sleep(ENGINE_POLL_S)
        except (HardTrip, StopRequested, SuspendRequested, CapReached):
            # Completed chunks + f*elapsed of the interrupted one; the
            # caller's finally block credits it all as 'estimated'.
            elapsed = ctx.clock.monotonic() - chunk_t0
            ctx._partial_cycles = done + min(chunk, int(elapsed * freq))
            raise
        # ---- per-chunk verification: FREQUENCY consistent + idle back
        ok_f = _cycles.freq_ok(freq_seen, freq)
        mkv, _st = (mon.read_stat('MEAN', 'v') if mon is not None
                    else (None, ''))
        tol = 0.05 * max(block['v_pk_kv'] - block['v_min_kv'], 0.1) + 0.05
        ok_idle = mkv is not None and abs(mkv - block['v_min_kv']) <= tol
        if ok_f and ok_idle:
            done += chunk
        else:
            done += chunk
            ctx.event('fast', 'burst_verify', 'warn',
                      value='' if freq_seen is None
                      else round(freq_seen, 3),
                      threshold=freq,
                      message=f'chunk verification failed '
                              f'(freq_ok={ok_f}, idle_ok={ok_idle}) -- '
                              f'cycles credited but counting quality '
                              f'degraded')
            # keep counting but mark the block; repeated failures also
            # feed the medium-tier transient counter via flags
    return done, freq_last


def _run_timed(ctx, drive, mon, block, planned):
    """Continuous drive for n/f seconds; credit by integrating measured
    frequency (falls back to commanded when the scope cannot read it,
    bounded by TimedCounter's stale hold)."""
    freq = block['freq_hz']
    drive.configure_cycle(block['waveform'], freq, block['v_pk_kv'],
                          block['v_min_kv'])
    drive.fire_burst(planned)      # mock/real: still a bounded emission
    counter = _cycles.TimedCounter(freq)
    counter.feed(ctx.clock.monotonic(), freq)   # seed with commanded
    end = ctx.clock.monotonic() + planned / freq
    last_f = None
    try:
        while ctx.clock.monotonic() < end:
            ctx.monitor_tick(v_expected_kv=0.5 * (block['v_pk_kv']
                                                  + block['v_min_kv']))
            f = None
            if mon is not None and mon.connected():
                f, _st = mon.read_stat('FREQUENCY', 'v')
            counter.feed(ctx.clock.monotonic(), f)
            last_f = f if f is not None else last_f
            ctx.poll()
            ctx.push_status(state='RUNNING')
            ctx.clock.sleep(ENGINE_POLL_S)
    except (HardTrip, StopRequested, SuspendRequested, CapReached):
        ctx._partial_cycles = min(planned, counter.credited())
        raise
    if counter.mismatch:
        ctx.event('fast', 'timed_freq', 'warn',
                  message='measured frequency deviated >2% from '
                          'commanded during timed counting')
    # Honest crediting: only what the frequency integral supports. A
    # dead scope stops accrual after TimedCounter's stale hold, and the
    # block row then shows cycles_done < planned out loud.
    return min(planned, counter.credited()), last_f


def _pause_point(ctx, drive):
    """Pause at a safe boundary: HV to 0, checkpoint via engine hook,
    wait for resume. Wall clock keeps running; actuated time does not
    (nothing is energized)."""
    ctx.event('engine', 'pause', 'pause', message='paused at safe point '
              '(HV at 0 kV); waiting for resume')
    try:
        drive.set_kv(0.0)
    except Exception:
        pass
    ctx.push_status(state='PAUSED', force=True)
    ctx.status.render_html()
    while ctx.pause and not (ctx.stop or ctx.suspend):
        ctx.process_commands(ctx)
        ctx.clock.sleep(0.2)
    ctx.poll()
    ctx.event('engine', 'resume', 'log', message='resumed')


# ---------------------------------------------------------------------------
# interludes
# ---------------------------------------------------------------------------

def characterize(ctx, ref):
    """The shared measurement set: C estimate (charge integration on a
    slow ramp), displacement pair (rest + peak frames -> vision), and a
    DC leakage hold. Returns the metrics dict. Runs at the REFERENCE
    condition -- fixed and cycling-independent so trends are comparable
    across specimens (NERD-pattern requirement)."""
    drive = ctx.hal.drive
    mon = ctx.hal.monitor
    ref_kv = float(ref['ref_kv'])
    hold_kv = float(ref['leak_hold_kv'])
    hold_s = float(ref['leak_hold_s'])
    energize = ctx.mode != 'dry' and ctx.hv_allowed
    out = {'ref_kv': ref_kv, 'frames': [], 'notes': []}

    # rest frame at 0 kV
    if energize:
        drive.set_kv(0.0)
    ctx.sleep_poll(0.5)
    rest_name, rest_frame = ctx.save_frame(
        f'INT{ctx.interlude_idx:03d}_rest_{ctx.ledger.cycles}.png')
    if rest_name:
        out['frames'].append(rest_name)

    # C by SYMMETRIC charge integration: up-ramp charge carries
    # (+C*dV + leak*dt), the later down-ramp carries (-C*dV + leak*dt);
    # C = (q_up - q_down) / (2*dV) cancels leakage AND any standing
    # instrument offset -- integrating only the up-ramp would fold the
    # leakage straight into C (worked example: 2 uA leak over a 2 s
    # ramp reads as +2 nF on a 5 nF specimen).
    q_up = q_down = None
    ramp_s = 2.0
    steps = 10
    if energize and mon is not None and mon.connected():
        q_up = _ramp_charge(ctx, drive, mon, 0.0, ref_kv, ramp_s, steps)
        ctx.ledger.credit_hold(ref_kv / 2.0, ramp_s)
    elif energize:
        drive.set_kv(ref_kv)
        ctx.sleep_poll(1.0)

    # peak frame at ref_kv
    peak_name, peak_frame = ctx.save_frame(
        f'INT{ctx.interlude_idx:03d}_peak_{ctx.ledger.cycles}.png')
    if peak_name:
        out['frames'].append(peak_name)
    vis = ctx.hal.vision.measure_pair(rest_frame, peak_frame,
                                      {'geometry':
                                       ctx.resolved['geometry']}) \
        if ctx.hal.vision else {'value': None, 'rest_value': None,
                                'quality': 'none', 'wrinkle_idx': None,
                                'notes': 'no vision adapter'}

    # leakage: DC hold; leak = median(last half of I) - learned 0 kV
    # baseline (the capacitive settle lives in the first half)
    leak_ua = None
    if energize and mon is not None and mon.connected():
        if abs(hold_kv - ref_kv) > 1e-6:
            drive.set_kv(hold_kv)
        samples = []
        end = ctx.clock.monotonic() + hold_s
        while ctx.clock.monotonic() < end:
            ctx.monitor_tick(v_expected_kv=hold_kv)
            ua, ist = mon.read_ua()
            if ua is not None:
                samples.append(ua)
            ctx.poll()
            ctx.clock.sleep(MONITOR_TICK_S)
        ctx.ledger.credit_hold(hold_kv, hold_s)
        if samples:
            half = samples[len(samples) // 2:]
            base0 = (ctx.watchdog.baseline_ua
                     if ctx.watchdog and ctx.watchdog.baseline_ua
                     is not None else 0.0)
            leak_ua = statistics.median(half) - base0
        # back to ref_kv, then the down-ramp integration
        if abs(hold_kv - ref_kv) > 1e-6:
            drive.set_kv(ref_kv)
            ctx.sleep_poll(0.5)
        q_down = _ramp_charge(ctx, drive, mon, ref_kv, 0.0, ramp_s,
                              steps)
        ctx.ledger.credit_hold(ref_kv / 2.0, ramp_s)
    c_est = None
    if q_up is not None and q_down is not None and ref_kv > 0:
        c_est = (q_up - q_down) / (2.0 * ref_kv)
    if energize:
        drive.set_kv(0.0)

    out.update(
        disp_metric=(ctx.hal.vision.metric_name if ctx.hal.vision
                     else ''),
        disp_value=vis.get('value'), rest_value=vis.get('rest_value'),
        vision_quality=vis.get('quality', ''),
        wrinkle_idx=vis.get('wrinkle_idx'),
        leak_ua=leak_ua, c_est_nf=c_est,
        # honesty flag: at the ~6 uA Trek monitor noise floor a small-C
        # estimate is marginal; bender-scale currents are credible.
        c_quality=('mock' if ctx.mode == 'mock' else
                   ('ok' if c_est is not None and c_est > 1.0
                    else 'marginal')))
    return out


def _ramp_charge(ctx, drive, mon, from_kv, to_kv, ramp_s, steps):
    """Integral of I dt over a stepped ramp [uA*s]. Each read lands
    right after its own step so the capacitive increment C*dV is
    captured exactly; watchdog ticks interleave with dV ~ 0.

    The PRIMING read matters: without it, the first sample averages the
    current over however long the sense sat idle before the ramp, and
    the first step's charge is undercounted by an amount that varies
    with what ran before -- measured as a 20-30% C bias that moved
    between interludes and false-tripped the capacitance rule
    (mock shakedown, 2026-08-23)."""
    q = 0.0
    mon.read_ua()                      # prime: align the sense interval
    t_prev = ctx.clock.monotonic()
    for i in range(1, steps + 1):
        drive.set_kv(from_kv + (to_kv - from_kv) * i / steps)
        ctx.clock.sleep(ramp_s / steps)
        ua, _ist = mon.read_ua()
        now = ctx.clock.monotonic()
        if ua is not None:
            q += ua * (now - t_prev)
        t_prev = now
        ctx.monitor_tick(force=True)
        ctx.poll()
    return q


def run_interlude(ctx, block, idx, kind):
    """fast_interlude / full_interlude. Full additionally runs the
    legacy-format staircase sub-run first, then the same measurement
    set; set_baseline stores the ratios' anchor."""
    ctx.interlude_idx += 1
    ref = dict(ctx.resolved['reference'])
    for k in ('ref_kv', 'ref_freq_hz', 'ref_cycles', 'leak_hold_kv',
              'leak_hold_s'):
        if k in block:
            ref[k] = block[k]
    subrun_rel = ''
    if kind == 'full':
        subrun_rel = run_staircase_subrun(ctx, block)

    m = characterize(ctx, ref)
    base = ctx.baseline
    is_baseline = bool(block.get('set_baseline'))
    ratios = {}
    if is_baseline:
        ctx.baseline = {'disp_value': m['disp_value'],
                        'rest_value': m['rest_value'],
                        'leak_ua': m['leak_ua'],
                        'c_est_nf': m['c_est_nf'],
                        'interlude_idx': ctx.interlude_idx}
        ctx.log(f"baseline set: disp {_r(m['disp_value'], 3)}, leak "
                f"{_r(m['leak_ua'], 3)} uA, C {_r(m['c_est_nf'], 3)} nF")
    elif base:
        ratios = _ratios(m, base)
        for f in ctx.rules.eval_slow(ratios):
            ctx._apply_firing(f)

    env = ctx.hal.env.current() if ctx.hal.env else None
    ctx.store.interludes.write({
        'interlude_idx': ctx.interlude_idx, 'kind': kind,
        'cycles_total': ctx.ledger.cycles,
        'actuated_s_total': round(ctx.ledger.actuated_s, 1),
        'wall_s_total': round(ctx.wall_s(), 1),
        't_iso': ctx.clock.now_iso(), 'ref_kv': ref['ref_kv'],
        'disp_metric': m['disp_metric'],
        'disp_value': _r(m['disp_value'], 4),
        'disp_baseline': _r((base or {}).get('disp_value'), 4),
        'disp_ratio': _r(ratios.get('disp_ratio'), 4),
        'leak_ua': _r(m['leak_ua'], 3),
        'leak_baseline_ua': _r((base or {}).get('leak_ua'), 3),
        'leak_ratio': _r(ratios.get('leak_ratio'), 3),
        'c_est_nf': _r(m['c_est_nf'], 3), 'c_quality': m['c_quality'],
        'c_baseline_nf': _r((base or {}).get('c_est_nf'), 3),
        'c_ratio': _r(ratios.get('c_ratio'), 4),
        'zero_value': _r(m['rest_value'], 4),
        'zero_drift': _r(ratios.get('zero_drift'), 4),
        'wrinkle_idx': _r(m['wrinkle_idx'], 3),
        'focus': '', 'frames': ';'.join(m['frames']),
        'subrun_dir': subrun_rel,
        'is_baseline': 'yes' if is_baseline else '',
        'verdict': 'baseline' if is_baseline else
                   ('flagged' if ratios and ctx.flags else 'ok'),
        'flags': ';'.join(fl['id'] for fl in ctx.flags),
        'notes': '; '.join(m['notes']),
    })
    ctx.status.interlude_point(ctx.ledger.cycles,
                               ratios.get('disp_ratio'),
                               m['leak_ua'])
    ctx.push_status(
        state='INTERLUDE', force=True,
        disp_ratio_pct='' if not ratios.get('disp_ratio')
        else round(100 * ratios['disp_ratio'], 1),
        leak_ua_last=_r(m['leak_ua'], 3))
    ctx.status.render_html()
    return m


def _ratios(m, base):
    """SLOW-rule metric dict from a measurement + the baseline. Guards:
    a near-zero baseline leakage cannot make an honest ratio -- floor it
    at 0.05 uA (half the Trek monitor's offset spec)."""
    out = {}
    if m['disp_value'] is not None and base.get('disp_value'):
        out['disp_ratio'] = m['disp_value'] / base['disp_value']
    if m['leak_ua'] is not None and base.get('leak_ua') is not None:
        out['leak_ratio'] = m['leak_ua'] / max(abs(base['leak_ua']), 0.05)
    if m['c_est_nf'] is not None and base.get('c_est_nf'):
        out['c_ratio'] = m['c_est_nf'] / base['c_est_nf']
        out['c_ratio_dev'] = abs(out['c_ratio'] - 1.0)
    if m['rest_value'] is not None and base.get('rest_value') is not None:
        scale = max(abs(base.get('disp_value') or 1.0), 1e-9)
        out['zero_drift'] = abs(m['rest_value']
                                - base['rest_value']) / scale
    if m['wrinkle_idx'] is not None:
        out['wrinkle_idx'] = m['wrinkle_idx']
    return out


def run_staircase_subrun(ctx, block):
    """The full interlude's voltage staircase as a LEGACY-format SLDEA
    sub-run (setup.txt + data.csv + telemetry.csv + frames/) under
    interludes/full_NN/ -- vendored sldea_edge / sldea_plot run on it
    unmodified. Re-homes the _sldea_worker staircase loop."""
    ctx.subrun_idx += 1
    subdir = ctx.store.subrun_dir(ctx.subrun_idx)
    rel = os.path.join('interludes', os.path.basename(subdir))
    st = {k: v for k, v in block['staircase'].items()
          if isinstance(v, (int, float))}
    if 'n_steps' in st:
        st['n_steps'] = int(st['n_steps'])
        st.setdefault('step_kv', None)
    p = sldea_profile.SldeaProfile(**st)
    drive = ctx.hal.drive
    mon = ctx.hal.monitor
    energize = ctx.mode != 'dry' and ctx.hv_allowed

    with open(os.path.join(subdir, 'setup.txt'), 'w',
              encoding='utf-8') as sf:
        sf.write(p.setup_text(
            os.path.basename(subdir), ctx.clock.now_iso(), 1, 2, 3,
            not energize, 'lifecycle interlude staircase',
            electrode=''))
        sf.write(f'\nLifecycle context: cycles_total='
                 f'{ctx.ledger.cycles}, interlude '
                 f'{ctx.interlude_idx}\n')

    tel = sldea_profile.TelemetryLog(
        os.path.join(subdir, sldea_profile.TELEMETRY_FILENAME),
        clock=ctx.clock.monotonic)
    ctx.telemetry = tel
    fh = open(os.path.join(subdir, 'data.csv'), 'w', newline='',
              encoding='utf-8')
    writer = csv.DictWriter(fh, fieldnames=p.CSV_COLUMNS)
    writer.writeheader()
    fh.flush()
    snaps = sorted(p.snapshots, key=lambda s: s['t'])
    si = 0
    sub_t0 = ctx.clock.monotonic()
    last_kv = None
    try:
        while True:
            el = ctx.clock.monotonic() - sub_t0
            if el > p.total_duration_s + 0.3:
                break
            ctx.monitor_tick(v_expected_kv=p.kv_at(el)
                             if p.kv_at(el) > 0.2 else None)
            if tel.due(el):
                ua, ist = (mon.read_ua() if mon is not None
                           and mon.connected() else (None, 'error'))
                kv_t, vst = (None, '')
                if tel.kv_due(el) and mon is not None and mon.connected():
                    kv_t, vst = mon.read_kv()
                tel.sample(el, ctx.clock.now_iso(), p.kv_at(el), ua=ua,
                           i_status=ist, kv=kv_t, v_status=vst or '')
            if energize:
                kv = p.kv_at(el)
                if last_kv is None or abs(kv - last_kv) > 1e-4:
                    drive.set_kv(kv)
                    last_kv = kv
            while si < len(snaps) and el >= snaps[si]['t']:
                _capture_subrun_snapshot(ctx, p, snaps[si], si + 1,
                                         subdir, writer, fh)
                si += 1
            ctx.poll()
            ctx.push_status(state='INTERLUDE',
                            block_desc=f'staircase {el:.0f}/'
                                       f'{p.total_duration_s:.0f}s')
            ctx.clock.sleep(0.1)
    finally:
        if energize:
            try:
                drive.set_kv(0.0)
            except Exception:
                pass
        try:
            fh.close()
        except Exception:
            pass
        ctx.log(tel.summary())
        tel.close()
        ctx.telemetry = None
        ctx.ledger.credit_profile(p)
    return rel


def _capture_subrun_snapshot(ctx, p, snap, index, subdir, writer, fh):
    """_sldea_capture re-homed (gui.py:4108-4190): frame first, then
    per-reading converted scope reads, filename only on real write."""
    mon = ctx.hal.monitor
    frame_name = ''
    cam = ctx.hal.camera
    frame = None
    if cam is not None and cam.available():
        for _attempt in range(2):
            try:
                frame = cam.grab()
            except Exception as e:
                ctx.log(f'capture error: {e}')
                frame = None
            if frame is not None:
                break
    mkv = mua = None
    note = ''
    if mon is not None and mon.connected():
        mkv, vst = mon.read_kv()
        mua, ist = mon.read_ua()
        if vst == 'offscreen':
            note = 'V_Out off-screen (clipped)'
    if frame is not None:
        name = p.frame_filename(snap['step'], snap['nominal_kv'],
                                snap['tag'])
        wrote = _write_frame(os.path.join(subdir, 'frames'), name, frame)
        frame_name = wrote
    writer.writerow({
        'snapshot': index, 'step': snap['step'], 'tag': snap['tag'],
        'nominal_kV': round(snap['nominal_kv'], 3),
        'control_V': round(
            sldea_profile.control_v_for_kv(snap['nominal_kv']), 3),
        'measured_kV': '' if mkv is None else round(mkv, 4),
        'measured_uA': '' if mua is None else round(mua, 2),
        't_planned_s': round(snap['t'], 2),
        'timestamp': ctx.clock.now_iso(),
        'frame_file': frame_name,
        'active_area_px': '', 'active_area_mm2': '',
        'active_diam_mm': '', 'wrinkle_idx': '', 'notes': note,
    })
    fh.flush()


# ---------------------------------------------------------------------------
# simple blocks
# ---------------------------------------------------------------------------

def run_dc_hold(ctx, block, idx):
    kv = float(block['kv'])
    hold_s = float(block['hold_s'])
    t_start = ctx.clock.now_iso()
    el_start = ctx.el()
    energize = ctx.mode != 'dry' and ctx.hv_allowed
    verdict = 'ok'
    try:
        if energize:
            ctx.hal.drive.set_kv(kv)
        end = ctx.clock.monotonic() + hold_s
        while ctx.clock.monotonic() < end:
            ctx.monitor_tick(v_expected_kv=kv if kv > 0.2 else None)
            ctx.poll()
            ctx.push_status(state='RUNNING', kv_cmd=kv)
            ctx.clock.sleep(MONITOR_TICK_S)
        if energize:
            ctx.ledger.credit_hold(kv, hold_s)
    except (HardTrip, StopRequested, SuspendRequested, CapReached) as e:
        verdict = type(e).__name__
        raise
    finally:
        if energize:
            try:
                ctx.hal.drive.set_kv(0.0)
            except Exception:
                pass
        ctx.store.blocks.write({
            'block_idx': idx, 'loop_path': block.get('loop_path', ''),
            'type': 'dc_hold', 't_start_iso': t_start,
            't_end_iso': ctx.clock.now_iso(),
            'wall_s': round(ctx.el() - el_start, 2),
            'vpk_cmd_kv': kv, 'cycles_total': ctx.ledger.cycles,
            'actuated_s_total': round(ctx.ledger.actuated_s, 1),
            'soft_flags': len(ctx.flags), 'verdict': verdict,
        })


def run_ramp(ctx, block, idx):
    to_kv = float(block['to_kv'])
    rate = float(block['rate_kv_s'])
    energize = ctx.mode != 'dry' and ctx.hv_allowed
    t_start = ctx.clock.now_iso()
    if energize:
        # step at 10 Hz along the ramp (host-sequenced, like upstream)
        mon_kv, _ = (ctx.hal.monitor.read_kv()
                     if ctx.hal.monitor and ctx.hal.monitor.connected()
                     else (None, ''))
        start = mon_kv if mon_kv is not None else 0.0
        dur = abs(to_kv - start) / rate if rate > 0 else 0.0
        steps = max(1, int(dur * 10))
        for i in range(1, steps + 1):
            ctx.hal.drive.set_kv(start + (to_kv - start) * i / steps)
            ctx.monitor_tick()
            ctx.poll()
            ctx.clock.sleep(dur / steps)
        ctx.ledger.credit_hold((to_kv + (start or 0)) / 2.0, dur)
    ctx.store.blocks.write({
        'block_idx': idx, 'loop_path': block.get('loop_path', ''),
        'type': 'ramp', 't_start_iso': t_start,
        't_end_iso': ctx.clock.now_iso(), 'vpk_cmd_kv': to_kv,
        'cycles_total': ctx.ledger.cycles,
        'actuated_s_total': round(ctx.ledger.actuated_s, 1),
        'verdict': 'ok',
    })


def run_wait_env(ctx, block, idx):
    """HV to 0; wait for a fresh, in-band environment attestation. In
    mock mode the MockEnv auto-attests and this passes straight
    through. The engine feeds env_entry commands into the manual
    EnvironmentSource; this loop just watches."""
    env_cfg = ctx.resolved['environment']
    t_band = block.get('t_c_band', env_cfg['t_c_band'])
    p_band = block.get('p_mbar_band', env_cfg['p_mbar_band'])
    stale = float(env_cfg['stale_after_s'])
    t_start = ctx.clock.now_iso()
    if ctx.mode != 'dry' and ctx.hv_allowed:
        try:
            ctx.hal.drive.set_kv(0.0)
        except Exception:
            pass
    ctx.event('engine', 'wait_env', 'log',
              message=f"WAITING FOR OPERATOR: {block['prompt']} "
                      f"(T band {t_band} C, p band {p_band} mbar)")
    prompted = False
    while True:
        cur = ctx.hal.env.current() if ctx.hal.env else None
        fresh = cur is not None and cur.get('age_s', 1e9) <= stale
        in_band = (cur is not None
                   and t_band[0] <= cur['t_c'] <= t_band[1]
                   and p_band[0] <= cur['p_mbar'] <= p_band[1])
        if fresh and in_band:
            break
        if cur is not None and fresh and not in_band and not prompted:
            prompted = True
            ctx.event('medium', 'env_band', 'warn',
                      message=f"attested environment "
                              f"({cur['t_c']:g} C, {cur['p_mbar']:g} "
                              f"mbar) is OUTSIDE the recipe band -- "
                              f"holding at 0 kV until it is in band")
        ctx.push_status(state='WAIT_OPERATOR', force=not prompted)
        ctx.poll()
        ctx.clock.sleep(0.5)
    ctx.event('engine', 'wait_env', 'log',
              message=f"environment attested: {cur['t_c']:g} C, "
                      f"{cur['p_mbar']:g} mbar (by "
                      f"{cur.get('operator', '?')})")
    # the pre-arm Paschen advisory only saw the first attestation; a
    # mid-run pump-down or vent lands here (advisory, 2026-09-29)
    note = _safety.paschen_note(cur.get('p_pa', cur['p_mbar'] * 100.0),
                                ctx.paschen_band_pa)
    if note:
        ctx.event('gate', 'paschen', 'warn', message=note)
    ctx.store.blocks.write({
        'block_idx': idx, 'loop_path': block.get('loop_path', ''),
        'type': 'wait_env', 't_start_iso': t_start,
        't_end_iso': ctx.clock.now_iso(),
        'env_t_c': cur['t_c'], 'env_p_mbar': cur['p_mbar'],
        'env_age_s': round(cur.get('age_s', 0)),
        'cycles_total': ctx.ledger.cycles,
        'actuated_s_total': round(ctx.ledger.actuated_s, 1),
        'verdict': 'ok',
    })


def _r(v, places):
    return '' if v is None else round(float(v), places)
