"""Screen 3 -- Run dashboard. Plain tk only (the PySide6-lag lesson):
BigTile grid, sparklines, event ticker, AMBER/RED flag UX, STOP.

Attaches to a run FOLDER (spawned by this GUI or found by Browse) and
polls status.json at 1 Hz; commands go through control.json with an
incrementing seq the engine echoes. A heartbeat older than
heartbeat_stale_s flips the chip to STALE -- the GUI never guesses
whether the engine is alive.
"""
import json
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from analysis import reduce as _reduce

from .widgets import Banner, BigTile, COLORS, Sparkline, StatusChip

TICKER_MAX_LINES = 500        # long-run Tk memory discipline


class DashboardScreen(tk.Frame):
    def __init__(self, master, state):
        super().__init__(master)
        self.state = state
        self.run_dir = None
        self._seq = 0
        self._last_events = 0
        self._banners = {}
        self._build()
        self._tick()

    def _build(self):
        top = tk.Frame(self)
        top.pack(fill='x', padx=8, pady=4)
        tk.Button(top, text='Attach to run...',
                  command=self._browse).pack(side='left')
        self.run_lbl = tk.Label(top, text='(no run attached)',
                                fg='#555')
        self.run_lbl.pack(side='left', padx=8)
        self.chip = StatusChip(top, 'IDLE')
        self.chip.pack(side='left', padx=8)

        tiles = tk.Frame(self)
        tiles.pack(fill='x', padx=8)
        self.tiles = {}
        for i, (key, cap) in enumerate((
                ('cycles', 'CYCLES'), ('pct', '% OF CAP'),
                ('kv', 'V_pk cmd / readback'), ('ua', 'LAST CURRENT'),
                ('leak', 'LEAKAGE @ interlude'),
                ('amp', 'AMPLITUDE % baseline'),
                ('env', 'T / p (attested)'), ('eta', 'ELAPSED / ETA'))):
            t = BigTile(tiles, cap)
            t.grid(row=i // 4, column=i % 4, padx=4, pady=4,
                   sticky='nsew')
            self.tiles[key] = t
        for c in range(4):
            tiles.columnconfigure(c, weight=1)

        spark_row = tk.Frame(self)
        spark_row.pack(fill='x', padx=8, pady=2)
        tk.Label(spark_row, text='amplitude ratio').pack(side='left')
        self.spark_amp = Sparkline(spark_row, color=COLORS['ink'])
        self.spark_amp.pack(side='left', padx=4)
        tk.Label(spark_row, text='leakage uA').pack(side='left')
        self.spark_leak = Sparkline(spark_row,
                                    color=COLORS['amber_fg'])
        self.spark_leak.pack(side='left', padx=4)

        self.banner_box = tk.Frame(self)
        self.banner_box.pack(fill='x', padx=8)

        ctl = tk.Frame(self)
        ctl.pack(fill='x', padx=8, pady=4)
        self.stop_btn = tk.Button(
            ctl, text='STOP RUN', bg=COLORS['action'], fg='white',
            font=('TkDefaultFont', 12, 'bold'), width=12,
            command=self._stop)
        self.stop_btn.pack(side='left')
        tk.Button(ctl, text='Pause after block',
                  command=lambda: self._cmd('pause')).pack(side='left',
                                                           padx=6)
        tk.Button(ctl, text='Resume',
                  command=lambda: self._cmd('resume')).pack(
            side='left')
        tk.Button(ctl, text='Suspend (right-censor)',
                  command=self._suspend).pack(side='left', padx=6)
        tk.Button(ctl, text='Attest environment...',
                  command=self._attest).pack(side='left', padx=6)
        tk.Button(ctl, text='Open run folder',
                  command=self._open_folder).pack(side='right')

        tk.Label(self, text='Events (newest last)',
                 font=('TkDefaultFont', 9, 'bold')).pack(anchor='w',
                                                         padx=8)
        self.ticker = tk.Text(self, height=10, state='disabled',
                              font=('Courier', 9))
        self.ticker.pack(fill='both', expand=True, padx=8, pady=4)

    # ---- attach ---------------------------------------------------------
    def attach(self, run_dir):
        self.run_dir = run_dir
        self._last_events = 0
        self._seq = int(time.time()) % 100000    # fresh seq epoch
        self.run_lbl.config(text=run_dir)
        self._log(f'attached to {run_dir}')

    def _browse(self):
        d = filedialog.askdirectory(
            initialdir=self.state.data_root,
            title='Pick a run folder (contains status.json)')
        if d:
            self.attach(d)

    # ---- commands -------------------------------------------------------
    def _cmd(self, cmd, **extra):
        if not self.run_dir:
            return
        self._seq += 1
        body = dict({'seq': self._seq, 'cmd': cmd,
                     'by': self.state.operator() or 'gui'}, **extra)
        path = os.path.join(self.run_dir, 'control.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(body, fh)
        os.replace(tmp, path)
        self._log(f'>> {cmd} (seq {self._seq})')

    def _stop(self):
        if not self.run_dir:
            return
        if messagebox.askyesno(
                'Stop run', 'Stop the run now? HV ramps to zero and the '
                'run ends as ABORTED (resumable state is checkpointed).',
                default='no', parent=self):
            self._cmd('stop')
            self.after(10000, self._check_ack)

    def _suspend(self):
        if messagebox.askyesno(
                'Suspend (right-censor)',
                'End the run and record the specimen as SUSPENDED '
                '(survived this many cycles; enters Weibull as '
                'right-censored)?', default='no', parent=self):
            self._cmd('suspend')

    def _attest(self):
        top = tk.Toplevel(self)
        top.title('Attest environment')
        top.transient(self.winfo_toplevel())
        vals = {}
        for key, label, default in (('t_c', 'Chamber T [C]', '23'),
                                    ('p_mbar', 'Pressure [mbar]',
                                     '1013')):
            r = tk.Frame(top)
            r.pack(fill='x', padx=10, pady=3)
            tk.Label(r, text=label, width=16, anchor='w').pack(
                side='left')
            v = tk.StringVar(value=default)
            vals[key] = v
            tk.Entry(r, textvariable=v, width=10).pack(side='left')

        def send():
            try:
                self._cmd('env_entry', t_c=float(vals['t_c'].get()),
                          p_mbar=float(vals['p_mbar'].get()))
                top.destroy()
            except ValueError:
                pass

        tk.Button(top, text='Attest', command=send).pack(pady=6)

    def _check_ack(self):
        st = self._status()
        if st and st.get('control_ack_seq', 0) < self._seq:
            messagebox.showwarning(
                'No acknowledgement',
                'The engine has not acknowledged the command within '
                '10 s. Engine-side hard paths still stand: the FAST '
                'rules and the guaranteed HV-zero on exit. If the '
                'process is dead, HV was zeroed by its finally-block '
                'or the run shows NOT_ZEROED -- check the bench.',
                parent=self)

    def _open_folder(self):
        if self.run_dir and os.path.isdir(self.run_dir):
            os.startfile(self.run_dir)  # noqa: S606 (Windows dev box)

    # ---- polling --------------------------------------------------------
    def _status(self):
        if not self.run_dir:
            return None
        try:
            with open(os.path.join(self.run_dir, 'status.json'),
                      encoding='utf-8') as fh:
                return json.load(fh)
        except Exception:
            return None

    def _tick(self):
        try:
            self._render(self._status())
        except Exception:
            pass                        # the poll must never die
        self.after(1000, self._tick)

    def _render(self, st):
        if st is None:
            self.chip.set('NO DATA', 'gray')
            return
        # staleness = crash detector
        stale = True
        try:
            from datetime import datetime
            upd = datetime.fromisoformat(st['updated_iso'])
            age = (datetime.now() - upd).total_seconds()
            stale = age > self.state.cfg.get('heartbeat_stale_s', 15.0)
        except Exception:
            pass
        state = st.get('state', '?')
        health = st.get('health', 'GREEN')
        if stale and state not in ('DONE', 'NOT_ZEROED'):
            self.chip.set('STALE -- engine not reporting', 'amber')
        else:
            level = {'GREEN': 'green', 'AMBER': 'amber',
                     'RED': 'red'}.get(health, 'gray')
            self.chip.set(f'{state}', level)

        cyc = st.get('cycles_total', 0)
        cap = st.get('cycles_target') or 0
        self.tiles['cycles'].set(f'{cyc:,}')
        self.tiles['pct'].set(f'{100 * cyc / cap:.1f}%' if cap else '--',
                              f'of {cap:,}')
        self.tiles['kv'].set(
            f"{st.get('kv_cmd', '--')}",
            f"rb {st.get('kv_meas', '--')} kV")
        self.tiles['ua'].set(f"{st.get('ua_last', '--')}", 'uA')
        self.tiles['leak'].set(f"{st.get('leak_ua_last', '--')}", 'uA')
        self.tiles['amp'].set(f"{st.get('disp_ratio_pct', '--')}", '%')
        env = st.get('env') or {}
        self.tiles['env'].set(
            f"{env.get('t_c', st.get('env_t_c', '--'))} C",
            f"{env.get('p_mbar', st.get('env_p_mbar', '--'))} mbar")
        self.tiles['eta'].set(
            f"{(st.get('wall_s') or 0) / 3600:.1f} h",
            st.get('eta_iso', ''))

        # flags -> banners
        flags = {f.get('id'): f for f in (st.get('flags') or [])}
        for fid, f in flags.items():
            if fid not in self._banners and not f.get('acked_by'):
                b = Banner(self.banner_box, 'amber')
                b.set_message(f.get('message', fid))
                b.add_button('Acknowledge -- keep running',
                             lambda fid=fid: self._ack(fid))
                b.add_button('Abort run', self._stop)
                b.pack(fill='x', pady=2)
                self._banners[fid] = b
        for fid in list(self._banners):
            f = flags.get(fid)
            if f is None or f.get('acked_by'):
                self._banners.pop(fid).destroy()

        # RED post-mortem
        if st.get('disposition') == 'failed' and \
                not getattr(self, '_postmortem_shown', False):
            self._postmortem_shown = True
            messagebox.showerror(
                'HARD TRIP -- HV SAFED',
                f"First-tripped criterion: {st.get('failure_mode')}\n"
                f"at {cyc:,} cycles.\n\nHV was zeroed by the engine. "
                f"Evidence: traces/ + frames/ in the run folder; open "
                f"the Review tab for the post-mortem.", parent=self)
        if st.get('state') == 'NOT_ZEROED' and \
                not getattr(self, '_nz_shown', False):
            self._nz_shown = True
            messagebox.showerror(
                'HV NOT ZEROED',
                'The engine could NOT zero the signal generator.\n\n'
                'TURN OFF THE SG / TREK AT THE FRONT PANEL NOW, then '
                'check the amplifier.', parent=self)

        # ticker from events.csv tail
        rows = _reduce.read_csv(os.path.join(self.run_dir,
                                             'events.csv'))
        for e in rows[self._last_events:]:
            self._log(f"{e.get('t_iso', '')[-12:]} "
                      f"[{e.get('tier')}/{e.get('rule_id')}] "
                      f"{e.get('action')}: {e.get('message')}")
        self._last_events = len(rows)

        # sparklines from interludes.csv
        inter = _reduce.read_csv(os.path.join(self.run_dir,
                                              'interludes.csv'))
        xs = [float(r['cycles_total'] or 0) for r in inter]
        amp = [float(r['disp_ratio']) if r.get('disp_ratio') else None
               for r in inter]
        leak = [float(r['leak_ua']) if r.get('leak_ua') else None
                for r in inter]
        self.spark_amp.set_points(xs, amp)
        self.spark_leak.set_points(xs, leak)

    def _ack(self, flag_id):
        self._cmd('ack_flag', flag_id=flag_id)

    def _log(self, line):
        self.ticker.config(state='normal')
        self.ticker.insert('end', line + '\n')
        n = int(self.ticker.index('end-1c').split('.')[0])
        if n > TICKER_MAX_LINES:
            self.ticker.delete('1.0', f'{n - TICKER_MAX_LINES}.0')
        self.ticker.see('end')
        self.ticker.config(state='disabled')
