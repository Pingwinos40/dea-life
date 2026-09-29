"""LifecycleEngine: the run state machine.

    IDLE -> VALIDATE -> GATES -> ARMED -> RUNNING <-> PAUSED
         -> FINISHING -> DONE(complete|failed|aborted|suspended|
                              rechar_failed|NOT_ZEROED)
                    RUNNING <-> WAIT_OPERATOR (always at 0 kV)
    Resume: LOAD_CHECKPOINT -> RECHAR -> GATES -> RUNNING

Runs headless in the calling thread (the CLI owns the terminal; the GUI
spawns the CLI as a detached process). Commands arrive through
``submit()`` (queue) and/or the ``control.json`` file the front ends
write; both funnel into the same handler at every poll point.

The guaranteed-finally is the safety spine (gui.py:4037-4069 lineage):
the DriveSource handle is captured at ARMED, ``finally`` calls its
``zero()`` (which itself never raises), and a False return is the
terminal NOT_ZEROED state -- loud in the log, events.csv, status.json,
and the exit code. Software zero is the only kill path in v1
(docs/SAFETY.md says so; the Trek TTL interlock is roadmap item 1).
"""
import json
import os
import queue

from . import checkpoint as _checkpoint
from . import cycles as _cycles
from . import executors as _ex
from . import runstore as _runstore
from . import safety as _safety
from .failure import RuleEngine
from .status import StatusWriter

CONTROL_JSON = 'control.json'


class LifecycleEngine:
    def __init__(self, resolved, specimen_row, cap_kv, hal, run_dir,
                 clock, mode, log_sink=print, registry=None,
                 confirmations=(),
                 paschen_band_pa=_safety.DEFAULT_PASCHEN_BAND_PA,
                 resume=False):
        self.resolved = resolved
        self.specimen_row = specimen_row
        self.cap_kv = float(cap_kv)
        self.hal = hal
        self.run_dir = run_dir
        self.clock = clock
        self.mode = mode
        self.registry = registry
        self.confirmations = set(confirmations)
        self.paschen_band_pa = paschen_band_pa
        self.resume = resume
        self.state = 'IDLE'
        self.disposition = None
        self.in_q = queue.Queue()
        self.runlog = _runstore.RunLog(log_sink, clock)
        self._control_seq_seen = 0
        self._checkpoint_state = None
        self.ctx = None

    # ---- public control -------------------------------------------------
    def submit(self, cmd):
        """Queue a command dict: {'cmd': stop|pause|resume|suspend|
        ack_flag|env_entry, ...}."""
        self.in_q.put(dict(cmd))

    # ---- command plumbing ----------------------------------------------
    def _process_commands(self, ctx):
        self._drain_control_file()
        while True:
            try:
                cmd = self.in_q.get_nowait()
            except queue.Empty:
                break
            self._handle(ctx, cmd)

    def _drain_control_file(self):
        path = os.path.join(self.run_dir, CONTROL_JSON)
        try:
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
        except Exception:
            return
        seq = int(d.get('seq', 0))
        if seq > self._control_seq_seen:
            self._control_seq_seen = seq
            self.in_q.put(d)

    def _handle(self, ctx, cmd):
        kind = cmd.get('cmd')
        by = cmd.get('by', '')
        if kind == 'stop':
            ctx.stop = True
            ctx.event('engine', 'stop', 'log',
                      message=f'stop requested{f" by {by}" if by else ""}')
        elif kind == 'suspend':
            ctx.suspend = True
            ctx.event('engine', 'suspend', 'log', operator=by,
                      message='operator suspend (right-censor)')
        elif kind == 'pause':
            ctx.pause = True
        elif kind == 'resume':
            ctx.pause = False
        elif kind == 'ack_flag':
            fid = cmd.get('flag_id')
            for fl in ctx.flags:
                if fl['id'] == fid and not fl['acked_by']:
                    fl['acked_by'] = by or 'operator'
                    ctx.event('engine', fid, 'log', operator=by,
                              message=f'AMBER flag {fid} acknowledged -- '
                                      f'run continues')
        elif kind == 'env_entry':
            env = self.hal.env
            if hasattr(env, 'attest'):
                env.attest(cmd.get('t_c'), cmd.get('p_mbar'), by,
                           self.clock.now_iso())
                ctx.event('engine', 'env_entry', 'log', operator=by,
                          message=f"environment attested: "
                                  f"{cmd.get('t_c')} C, "
                                  f"{cmd.get('p_mbar')} mbar")
        if ctx is not None:
            self.status.update(control_ack_seq=self._control_seq_seen)

    # ---- the run --------------------------------------------------------
    def run(self):
        """Execute; returns the disposition string. Never raises for a
        run-outcome reason -- exceptions mean engine bugs."""
        log = self.runlog.log
        self.state = 'VALIDATE'
        run_id = os.path.basename(self.run_dir)
        log(f'== SLDEA Lifecycle run {run_id} '
            f'({self.mode.upper()}) ==')

        store = _runstore.RunStore(self.run_dir, resume=self.resume)
        self.status = StatusWriter(self.run_dir, self.clock)
        self.status.update(run_id=run_id,
                           specimen_id=self.resolved['specimen_id'],
                           mode=self.mode, state='VALIDATE',
                           health='GREEN')

        ck = None
        if self.resume:
            ck = _checkpoint.load(self.run_dir)
            if ck is None:
                log('resume requested but no checkpoint found -- abort')
                return self._finish(store, None, 'aborted')
            if ck['recipe_sha256'] != self.resolved['sha256']:
                log('resume REFUSED: recipe hash mismatch (the recipe '
                    'or specimen facts changed since the checkpoint)')
                return self._finish(store, None, 'aborted')
            if ck.get('clean_shutdown'):
                log('checkpoint marks a clean end -- nothing to resume')
                return self._finish(store, None, 'aborted')
            log(f"UNCLEAN SHUTDOWN detected at block {ck['flat_idx']}, "
                f"{ck['cycles']:,} cycles -- resuming with RECHAR gate")

        # ---- gates
        self.state = 'GATES'
        env_sample = self.hal.env.current() if self.hal.env else None
        ok, gates = _safety.gate_chain(
            self.mode, self.hal, self.resolved, self.cap_kv, run_id,
            env_sample, self.paschen_band_pa, self.confirmations, log)
        if not ok:
            log('gate chain FAILED -- not arming')
            self.status.update(state='GATE_FAIL', health='RED')
            return self._finish(store, None, 'aborted')

        store.write_recipe(self.resolved)
        store.write_setup(self._setup_lines(run_id, gates))
        self.runlog.attach(os.path.join(self.run_dir, 'run.log'))

        # ---- ARMED: capture the drive handle for the finally block
        self.state = 'ARMED'
        drive = self.hal.drive
        rules = RuleEngine(self.resolved['failure_rules'])
        ledger = _cycles.CycleLedger(
            cycles=ck['cycles'] if ck else 0,
            actuated_s=ck['actuated_s'] if ck else 0.0)
        ctx = _ex.RunContext(self.hal, store, ledger, rules, self.clock,
                             log, self.status, self.resolved, self.mode,
                             self._process_commands)
        self.ctx = ctx
        ctx.paschen_band_pa = self.paschen_band_pa
        if ck:
            ctx.wall_offset_s = ck['wall_s']
            ctx.baseline = ck.get('baseline')
            ctx.flags = list(ck.get('flags') or [])
            ctx.interlude_idx = ck.get('interlude_rows', 0)
            store.set_event_idx(ck.get('event_rows', 0))
        start_idx = ck['flat_idx'] if ck else 0
        # advisory gates (Paschen, 2026-09-29) passed but left a note:
        # put it on the audit record, not just in run.log
        for g in gates:
            if g.warn:
                ctx.event('gate', g.gate, 'warn', message=g.warn)

        if drive is not None:
            drive.specimen_cap_kv = self.cap_kv
        disposition = 'complete'
        failure_mode = ''
        zeroed = True
        try:
            # camera preflight (after arming, before HV -- upstream order)
            if self.hal.camera is not None and self.hal.camera.available():
                ok_cam, verdict, _f = self.hal.camera.preflight()
                log(f'camera preflight: {verdict}')
                if not ok_cam:
                    ctx.event('engine', 'camera_preflight', 'warn',
                              message=verdict)
            # watchdog baseline learn at 0 kV
            if self.mode != 'dry':
                oc = rules.rule('fast', 'overcurrent')
                ctx.watchdog = _ex.learn_watchdog_baseline(
                    ctx, float(oc['threshold']),
                    float(oc.get('sustain_s', 3.0)))

            # RECHAR gate on resume: re-characterize BEFORE HV re-arm at
            # cycling levels; the SLOW rules must pass vs the stored
            # baseline or the run ends here. No early return -- the
            # guaranteed finally below must always be the LAST thing to
            # touch HV, and a zero failure must override the
            # disposition (control-flow review 2026-08-23).
            rechar_ok = True
            if ck and ctx.baseline:
                self.state = 'RECHAR'
                log('RECHAR: re-characterization vs stored baseline...')
                m = _ex.characterize(ctx, self.resolved['reference'])
                ratios = _ex._ratios(m, ctx.baseline)
                bad = [f for f in rules.eval_slow(ratios)
                       if f.action == 'abort' and not f.is_flag]
                if bad:
                    for f in bad:
                        ctx.event(f.tier, f.rule_id, 'abort',
                                  value=f.value, threshold=f.threshold,
                                  message='RECHAR FAILED: ' + f.message)
                    log('RECHAR failed -- run ends as rechar_failed; '
                        'decide failed vs suspended and record it via '
                        'the registry')
                    disposition = 'rechar_failed'
                    failure_mode = bad[0].rule_id
                    rechar_ok = False
                else:
                    log('RECHAR passed -- resuming the block sequence')

            # ---- RUNNING
            if rechar_ok:
                self.state = 'RUNNING'
                blocks = self.resolved['blocks']
                n = len(blocks)
                try:
                    for idx in range(start_idx, n):
                        block = blocks[idx]
                        ctx.poll()
                        if ctx.pause:
                            _ex._pause_point(ctx, drive)
                        ctx.push_status(
                            state='RUNNING', block_idx=idx, n_blocks=n,
                            block_desc=f"{block['type']} "
                                       f"{block.get('loop_path', '')}",
                            force=True)
                        self._dispatch(ctx, block, idx)
                        self._checkpoint(ctx, idx + 1)
                except _ex.HardTrip as e:
                    disposition = 'failed'
                    failure_mode = e.firing.rule_id
                    log(f'HARD TRIP: {e.firing.message}')
                except _ex.StopRequested:
                    disposition = 'aborted'
                    log('run aborted by operator')
                except _ex.SuspendRequested:
                    disposition = 'suspended'
                    log('run suspended (right-censored) by operator')
                except _ex.CapReached as e:
                    disposition = 'complete'
                    ctx.event('engine', 'stop_cap', 'log',
                              message=f'stop cap reached: {e}')
                    log(f'run complete: {e}')
                else:
                    disposition = 'complete'
                    log('run complete: all blocks executed')
                # SLOW-tier abort discovered at an interlude arrives as
                # a HardTrip via _apply_firing, so it is covered above.
        finally:
            self.state = 'FINISHING'
            zeroed = self._safe_zero(drive, log)
            if not zeroed:
                disposition = 'NOT_ZEROED'
        if rules.first_abort and not failure_mode:
            failure_mode = rules.first_abort.rule_id
            if disposition == 'complete':
                disposition = 'failed'
        return self._finish(store, ctx, disposition, failure_mode,
                            zeroed=zeroed)

    # ---- helpers --------------------------------------------------------
    def _dispatch(self, ctx, block, idx):
        btype = block['type']
        if btype == 'cycle':
            _ex.run_cycle_block(ctx, block, idx)
        elif btype == 'fast_interlude':
            _ex.run_interlude(ctx, block, idx, 'fast')
        elif btype == 'full_interlude':
            _ex.run_interlude(ctx, block, idx, 'full')
        elif btype == 'dc_hold':
            _ex.run_dc_hold(ctx, block, idx)
        elif btype == 'ramp':
            _ex.run_ramp(ctx, block, idx)
        elif btype == 'wait_env':
            _ex.run_wait_env(ctx, block, idx)
        else:
            ctx.log(f'unknown block type {btype!r} -- skipped')

    def _checkpoint(self, ctx, next_idx):
        st = _checkpoint.new_state(
            os.path.basename(self.run_dir),
            self.resolved['specimen_id'], self.resolved['sha256'],
            self.mode)
        st.update(engine_state=self.state, flat_idx=next_idx,
                  cycles=ctx.ledger.cycles,
                  actuated_s=round(ctx.ledger.actuated_s, 3),
                  wall_s=round(ctx.wall_s(), 3),
                  baseline=ctx.baseline, flags=ctx.flags,
                  last_env=self.hal.env.current() if self.hal.env
                  else None,
                  block_rows=ctx.store.blocks.rows,
                  interlude_rows=ctx.interlude_idx,
                  event_rows=ctx.store.events.rows,
                  written_iso=self.clock.now_iso())
        self._checkpoint_state = st
        _checkpoint.save(self.run_dir, st)

    def _safe_zero(self, drive, log):
        """The guaranteed shutdown. zero() never raises by contract;
        belt-and-suspenders try anyway."""
        if drive is None:
            return True
        try:
            ok = drive.zero()
        except Exception as e:            # contract breach -- still loud
            log(f'SG zeroing attempt raised: {e}')
            ok = False
        if not ok:
            log('!! FAILED TO ZERO THE SG OUTPUT -- the Trek may still '
                'be energized. TURN OFF THE SG/TREK AT THE FRONT PANEL '
                'NOW.')
            self.status.update(state='NOT_ZEROED', health='RED',
                               not_zeroed=True)
            self.status.render_html()
        return ok

    def _finish(self, store, ctx, disposition, failure_mode='',
                zeroed=True):
        self.disposition = disposition
        log = self.runlog.log
        if ctx is not None:
            ctx.event('engine', 'end', 'log',
                      message=f'run {disposition}'
                              + (f' (failure_mode={failure_mode})'
                                 if failure_mode else ''))
            if disposition != 'NOT_ZEROED':
                st = self._checkpoint_state or _checkpoint.new_state(
                    os.path.basename(self.run_dir),
                    self.resolved['specimen_id'],
                    self.resolved['sha256'], self.mode)
                st.update(engine_state='DONE', clean_shutdown=True,
                          cycles=ctx.ledger.cycles,
                          actuated_s=round(ctx.ledger.actuated_s, 3),
                          wall_s=round(ctx.wall_s(), 3),
                          baseline=ctx.baseline,
                          written_iso=self.clock.now_iso())
                _checkpoint.save(self.run_dir, st)
            self.status.update(
                state='DONE', disposition=disposition,
                failure_mode=failure_mode,
                health='RED' if disposition in ('failed', 'NOT_ZEROED')
                else ('AMBER' if ctx.flags else 'GREEN'))
            self.status.render_html()
            if self.registry is not None and self.mode != 'mock':
                try:
                    self.registry.record_outcome(
                        self.resolved['specimen_id'],
                        'failed' if disposition == 'failed'
                        else 'suspended',
                        ctx.ledger.cycles, ctx.ledger.actuated_s,
                        os.path.basename(self.run_dir),
                        failure_mode=failure_mode)
                except Exception as e:
                    log(f'registry outcome write failed: {e}')
        log(f'== disposition: {disposition}'
            + (f' | failure mode: {failure_mode}' if failure_mode else '')
            + ' ==')
        store.close()
        self.state = 'DONE'
        return disposition

    def _setup_lines(self, run_id, gates=()):
        r = self.resolved
        drv = r['drive']
        feas = r.get('feasibility') or {}
        lines = [
            f'SLDEA Lifecycle Run  --  {run_id}',
            f'Started: {self.clock.now_iso(timespec="seconds")}',
            'MODE: *** DRY RUN (HV never commanded) ***'
            if self.mode == 'dry' else
            ('MODE: MOCK (simulated rig)' if self.mode == 'mock'
             else 'MODE: LIVE (HV energized)'),
            '',
            '--- Specimen ---',
        ]
        for k in ('specimen_id', 'geometry', 'material_preset',
                  'elastomer', 'electrode', 'n_layers',
                  'thickness_um_layer', 'c_est_nf', 'ebd_ref_v_per_um',
                  'ebd_ref_temp_c', 'status', 'cycles_accum'):
            v = self.specimen_row.get(k, '')
            if v != '':
                lines.append(f'{k}: {v}')
        lines += [
            f'HARD CAP: {self.cap_kv:g} kV (admin_caps.json)',
            '',
            '--- Drive ---',
            f"waveform {drv['waveform']}  {drv['freq_hz']:g} Hz  "
            f"v_pk {drv['v_pk_kv']:g} kV  v_min {drv['v_min_kv']:g} kV",
            f"counting: {r['counting']}  planned cycles: "
            f"{r['planned_cycles']:,}  cap: "
            f"{r['stop']['max_cycles']:,} cycles / "
            f"{r['stop']['max_wall_h']:g} h",
        ]
        if feas:
            ipk = feas.get('i_pk_ua')
            lines.append(
                f"feasibility: {feas['verdict']} -- I_pk "
                + ('(unknown C)' if ipk is None else f'{ipk:.0f} uA, '
                   f"{100 * (feas.get('i_frac') or 0):.0f}% of Trek "
                   f'limit'))
        warns = [g for g in gates if g.warn]
        if warns:
            lines += ['', '--- Advisories ---']
            lines += [f'{g.gate}: {g.warn}' for g in warns]
        lines += ['', '--- Recipe ---',
                  f"name: {r['name']}",
                  f"sha256: {r['sha256']}", '']
        if self.hal.monitor is not None:
            lines += ['--- Monitor readback ---']
            lines += self.hal.monitor.setup_lines()
        return lines
