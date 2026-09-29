"""The application shell: persistent top bar (identity + state chip +
the big red STOP, visible from every tab) over the 5-tab notebook.

The GUI is a LAUNCHER + MONITOR: runs execute in a detached CLI
subprocess (a Tk crash must never kill a week of cycling), and every
interaction with a live run goes through the run folder's
status.json / control.json. On boot the shell scans data_root for live
heartbeats and offers to re-attach.
"""
import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from ui_widgets import add_tooltip  # vendored

from core import runstore as _runstore
from core import safety as _safety
from core.registry import Registry

from .campaign_screen import CampaignScreen
from .dashboard_screen import DashboardScreen
from .preflight_screen import PreflightScreen
from .review_screen import ReviewScreen
from .setup_screen import SetupScreen
from .widgets import COLORS, StatusChip


class AppState:
    """Shared state + services for the screens."""

    def __init__(self, root_dir, cfg, app):
        self.root = root_dir
        self.cfg = cfg
        self.app = app
        self.data_root = (cfg.get('data_root')
                          or _runstore.default_data_root())
        self._registry = None
        self.setup = None                 # SetupScreen's validated dict
        self.active_run_dir = None
        self.engine_proc = None

    def registry(self):
        if self._registry is None:
            try:
                os.makedirs(self.data_root, exist_ok=True)
                self._registry = Registry(self.data_root)
            except Exception:
                return None
        return self._registry

    def paschen_band(self):
        band = self.cfg.get('paschen_warn_pa')
        if band:
            return (float(band[0]), float(band[1]))
        return _safety.DEFAULT_PASCHEN_BAND_PA

    def operator(self):
        return ((self.setup or {}).get('env') or {}).get('operator', '')

    def on_setup_valid(self, setup):
        self.setup = setup
        self.app.enable_preflight(setup is not None)

    # ---- run launching --------------------------------------------------
    def launch_run(self, mode, confirms):
        """Spawn the CLI detached; attach the dashboard to the run dir."""
        setup = self.setup
        if not setup:
            return
        import tempfile
        recipe_dir = os.path.join(self.data_root, '_recipes_launched')
        os.makedirs(recipe_dir, exist_ok=True)
        stamp = _runstore.run_dirname(__import__(
            'datetime').datetime.now())
        run_name = setup.get('run_name') or stamp
        recipe_path = os.path.join(recipe_dir, f'{run_name}.json')
        with open(recipe_path, 'w', encoding='utf-8') as fh:
            json.dump(setup['recipe_dict'], fh, indent=1)
        env = setup['env']
        cmd = [sys.executable,
               os.path.join(self.root, 'lifecycle_cli.py'), 'run',
               recipe_path, '--specimen', setup['specimen_id'],
               '--mode', mode, '--data-root', self.data_root,
               '--run-name', run_name,
               '--attest',
               f"{env['t_c']},{env['p_mbar']},{env['operator']}"]
        for tok in confirms:
            cmd += ['--confirm', tok]
        run_dir = os.path.join(self.data_root, setup['specimen_id'],
                               run_name)
        log_path = os.path.join(recipe_dir, f'{run_name}.console.log')
        logf = open(log_path, 'a', encoding='utf-8')
        flags = 0
        if os.name == 'nt':
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                     | getattr(subprocess, 'DETACHED_PROCESS', 0))
        self.engine_proc = subprocess.Popen(
            cmd, cwd=self.root, stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags,
            start_new_session=(os.name != 'nt'))
        self.active_run_dir = run_dir
        self.app.on_run_started(run_dir, mode)


class LifecycleApp:
    def __init__(self, root_dir=None, cfg=None):
        self.root_dir = root_dir or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.cfg = cfg if cfg is not None else self._load_cfg()
        self.tk = tk.Tk()
        self.tk.title('SLDEA Lifecycle Manager')
        self.tk.geometry('1280x820')
        self.state = AppState(self.root_dir, self.cfg, self)
        self._build()

    def _load_cfg(self):
        path = os.path.join(self.root_dir, 'config.json')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)
        return {}

    def _build(self):
        bar = tk.Frame(self.tk, bg='#f0f0f0')
        bar.pack(fill='x')
        tk.Label(bar, text='SLDEA Lifecycle Manager',
                 font=('TkDefaultFont', 12, 'bold'),
                 fg=COLORS['ink'], bg='#f0f0f0').pack(side='left',
                                                      padx=8, pady=4)
        self.run_lbl = tk.Label(bar, text='no active run',
                                bg='#f0f0f0', fg='#555')
        self.run_lbl.pack(side='left', padx=10)
        self.chip = StatusChip(bar, 'IDLE')
        self.chip.pack(side='left')
        self.stop_btn = tk.Button(
            bar, text='STOP', bg=COLORS['action'], fg='white',
            font=('TkDefaultFont', 11, 'bold'), width=8,
            command=self._stop_from_bar)
        self.stop_btn.pack(side='right', padx=8, pady=3)
        add_tooltip(self.stop_btn,
                    'Stops the ATTACHED run (same as the dashboard '
                    'STOP): HV ramps to zero, run ends as aborted '
                    'with a resumable checkpoint.')

        self.nb = ttk.Notebook(self.tk)
        self.nb.pack(fill='both', expand=True)
        self.setup = SetupScreen(self.nb, self.state)
        self.preflight = PreflightScreen(self.nb, self.state)
        self.dashboard = DashboardScreen(self.nb, self.state)
        self.review = ReviewScreen(self.nb, self.state)
        self.campaign = CampaignScreen(self.nb, self.state)
        for w, label in ((self.setup, ' 1. Setup '),
                         (self.preflight, ' 2. Pre-flight '),
                         (self.dashboard, ' 3. Run '),
                         (self.review, ' 4. Review '),
                         (self.campaign, ' 5. Campaign ')):
            self.nb.add(w, text=label)
        self.nb.tab(1, state='disabled')
        self.tk.after(800, self._scan_live_runs)
        self.tk.after(1000, self._bar_tick)

    # ---- shell services -------------------------------------------------
    def enable_preflight(self, ok):
        self.nb.tab(1, state='normal' if ok else 'disabled')
        if ok:
            self.preflight.recheck()

    def on_run_started(self, run_dir, mode):
        self.dashboard.attach(run_dir)
        self.run_lbl.config(text=f'{mode.upper()}: '
                                 f'{os.path.basename(run_dir)}')
        self.nb.select(2)

    def _stop_from_bar(self):
        self.nb.select(2)
        self.dashboard._stop()

    def _bar_tick(self):
        st = self.dashboard._status()
        if st:
            health = st.get('health', 'GREEN')
            level = {'GREEN': 'green', 'AMBER': 'amber',
                     'RED': 'red'}.get(health, 'gray')
            self.chip.set(st.get('state', '?'), level)
        self.tk.after(1000, self._bar_tick)

    def _scan_live_runs(self):
        """Boot re-attach: any run with a fresh heartbeat?"""
        import datetime
        root = self.state.data_root
        if not os.path.isdir(root):
            return
        fresh = []
        for spec in os.listdir(root):
            d = os.path.join(root, spec)
            if not os.path.isdir(d) or spec.startswith('_'):
                continue
            for run in os.listdir(d):
                sp = os.path.join(d, run, 'status.json')
                try:
                    stt = json.load(open(sp, encoding='utf-8'))
                    upd = datetime.datetime.fromisoformat(
                        stt['updated_iso'])
                    age = (datetime.datetime.now()
                           - upd).total_seconds()
                    if age < 30 and stt.get('state') not in ('DONE',):
                        fresh.append(os.path.join(d, run))
                except Exception:
                    continue
        if fresh:
            if messagebox.askyesno(
                    'Live run found',
                    f'A run appears to be live:\n{fresh[0]}\n\n'
                    f'Attach the dashboard to it?', parent=self.tk):
                self.dashboard.attach(fresh[0])
                self.nb.select(2)

    def run(self):
        self.tk.mainloop()


def main():
    LifecycleApp().run()


if __name__ == '__main__':
    main()
