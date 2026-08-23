"""Screen 2 -- Pre-flight: the gate chain as a visible checklist, a
rehearsal button, and the START controls.

The GUI checks what it can check locally (recipe resolution, caps,
data root, attestation freshness, disk); the ENGINE re-runs its own
gate chain at start regardless -- these rows are UX, the engine is
safety. Starting a run spawns the CLI as a DETACHED subprocess (the
run must survive a GUI crash) and hands the run dir to the dashboard.
"""
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox

from core import runstore as _runstore
from core import safety as _safety

from .widgets import COLORS, ChecklistRow


class PreflightScreen(tk.Frame):
    def __init__(self, master, state):
        super().__init__(master)
        self.state = state
        self._build()

    def _build(self):
        tk.Label(self, text='Pre-flight checklist',
                 font=('TkDefaultFont', 12, 'bold')).pack(anchor='w',
                                                          padx=8,
                                                          pady=4)
        box = tk.Frame(self)
        box.pack(fill='x', padx=10)
        self.rows = {}
        for key, label in (
                ('setup', 'Setup validated'),
                ('cap', 'Specimen hard cap'),
                ('dataroot', 'Data root writable, not OneDrive'),
                ('disk', 'Disk space'),
                ('attest', 'Environment attested + fresh'),
                ('paschen', 'Paschen band vs attested pressure'),
                ('engine', 'Engine-side gates (SG/scope/camera/'
                           'watchdog)')):
            r = ChecklistRow(box, label)
            r.pack(fill='x', pady=1)
            self.rows[key] = r
        self.rows['engine'].set(
            'PENDING', 'run at engine start -- monitor the dashboard '
                       'log; a gate failure refuses to arm')

        btns = tk.Frame(self)
        btns.pack(fill='x', padx=10, pady=8)
        self.recheck_btn = tk.Button(btns, text='Re-check',
                                     command=self.recheck)
        self.recheck_btn.pack(side='left')
        self.rehearse_btn = tk.Button(
            btns, text='Run rehearsal (mock, instant)',
            command=self.rehearse)
        self.rehearse_btn.pack(side='left', padx=8)
        self.start_mock_btn = tk.Button(
            btns, text='START (mock, real-time)', state='disabled',
            command=lambda: self.start('mock'))
        self.start_mock_btn.pack(side='left', padx=8)
        self.start_dry_btn = tk.Button(
            btns, text='START DRY RUN', state='disabled',
            command=lambda: self.start('dry'))
        self.start_dry_btn.pack(side='left', padx=8)
        self.start_live_btn = tk.Button(
            btns, text='ENERGIZE HV -- START LIVE', state='disabled',
            bg=COLORS['action'], fg='white',
            font=('TkDefaultFont', 10, 'bold'),
            command=lambda: self.start('live'))
        self.start_live_btn.pack(side='left', padx=8)

        self.out = tk.Text(self, height=14, state='disabled',
                           font=('Courier', 9))
        self.out.pack(fill='both', expand=True, padx=10, pady=4)

    def log(self, msg):
        self.out.config(state='normal')
        self.out.insert('end', msg + '\n')
        self.out.see('end')
        self.out.config(state='disabled')

    # ---- checks ---------------------------------------------------------
    def recheck(self):
        st = self.state
        setup = st.setup
        ok_all = True

        if setup and setup.get('resolved'):
            r = setup['resolved']
            self.rows['setup'].set(
                'PASS', f"{r['name']} for {setup['specimen_id']} "
                        f"(sha {r['sha256'][:10]})")
        else:
            self.rows['setup'].set('FAIL', 'validate on the Setup tab '
                                           'first')
            ok_all = False

        if setup and setup.get('resolved'):
            r = setup['resolved']
            vpk = r['drive'].get('v_pk_kv') or 0
            cap = r['cap_kv']
            ok = vpk <= cap
            self.rows['cap'].set('PASS' if ok else 'FAIL',
                                 f'{vpk:g} kV vs cap {cap:g} kV '
                                 f'(engine clamps independently)')
            ok_all &= ok
        else:
            self.rows['cap'].set('PENDING', '')

        try:
            root = _runstore.check_data_root(st.data_root)
            self.rows['dataroot'].set('PASS', root)
        except _runstore.RunStoreError as e:
            self.rows['dataroot'].set('FAIL', str(e))
            ok_all = False

        try:
            free_gb = shutil.disk_usage(st.data_root).free / 1e9
            ok = free_gb > 2.0
            self.rows['disk'].set('PASS' if ok else 'FAIL',
                                  f'{free_gb:.1f} GB free')
            ok_all &= ok
        except OSError as e:
            self.rows['disk'].set('FAIL', str(e))
            ok_all = False

        env = (setup or {}).get('env')
        if env and env.get('operator'):
            self.rows['attest'].set(
                'PASS', f"{env['t_c']:g} C, {env['p_mbar']:g} mbar "
                        f"by {env['operator']} (re-attested at every "
                        f"wait_env)")
            p_pa = env['p_mbar'] * 100.0
            band = st.paschen_band()
            if _safety.paschen_inhibited(p_pa, band):
                self.rows['paschen'].set(
                    'FAIL', f'{p_pa:g} Pa is INSIDE the HV-inhibit '
                            f'band {band[0]:g}..{band[1]:g} Pa '
                            f'(Paschen minimum region). Vent past the '
                            f'band or use the typed override at start.')
                ok_all = False
            else:
                self.rows['paschen'].set('PASS',
                                         f'{p_pa:g} Pa outside '
                                         f'{band[0]:g}..{band[1]:g} Pa')
        else:
            self.rows['attest'].set('FAIL', 'attest on the Setup tab')
            self.rows['paschen'].set('PENDING', '')
            ok_all = False

        state = 'normal' if ok_all else 'disabled'
        for b in (self.start_mock_btn, self.start_dry_btn,
                  self.start_live_btn):
            b.config(state=state)
        return ok_all

    # ---- rehearsal ------------------------------------------------------
    def rehearse(self):
        """Full recipe against mocks on simulated time, in a scratch
        data root -- proves sequencing + the whole write path and
        prints the timeline."""
        if not self.recheck():
            self.log('rehearsal blocked: fix the red rows first')
            return
        self.rehearse_btn.config(state='disabled')
        threading.Thread(target=self._rehearse_worker,
                         daemon=True).start()

    def _rehearse_worker(self):
        st = self.state
        setup = st.setup
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            recipe_path = os.path.join(tmp, 'recipe.json')
            with open(recipe_path, 'w', encoding='utf-8') as fh:
                json.dump(setup['recipe_dict'], fh)
            # rehearsal registry: clone the specimen row into the tmp
            # root so the real registry is never touched by a rehearsal
            from core.registry import Registry
            reg = Registry(tmp)
            row = st.registry().get(setup['specimen_id'])
            reg.create(row['specimen_id'], row['geometry'], 'rehearsal',
                       **{k: v for k, v in row.items()
                          if k in ('elastomer', 'electrode', 'n_layers',
                                   'thickness_um_layer', 'c_est_nf',
                                   'ebd_ref_v_per_um', 'ebd_ref_temp_c')
                          and v})
            cmd = [sys.executable,
                   os.path.join(st.root, 'lifecycle_cli.py'), 'run',
                   recipe_path, '--specimen', setup['specimen_id'],
                   '--mode', 'mock', '--sim-time', '--data-root', tmp]
            self._ui(lambda: self.log('rehearsal: running the full '
                                      'recipe on mocks...'))
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 cwd=st.root)
            tail = (res.stdout or '').strip().splitlines()[-12:]
            for ln in tail:
                self._ui(lambda ln=ln: self.log('  ' + ln))
            ok = res.returncode == 0
            self._ui(lambda: self.log(
                f'rehearsal {"PASSED" if ok else "FAILED"} '
                f'(exit {res.returncode})'))
        self._ui(lambda: self.rehearse_btn.config(state='normal'))

    def _ui(self, fn):
        self.after(0, fn)

    # ---- start ----------------------------------------------------------
    def start(self, mode):
        st = self.state
        setup = st.setup
        if not self.recheck() and mode != 'mock':
            return
        confirms = []
        if mode == 'live':
            r = setup['resolved']
            want = _safety.CONFIRM_ENERGIZE
            typed = TypedConfirm.ask(
                self, 'ENERGIZE HV',
                f"About to energize {r['drive']['v_pk_kv']:g} kV pk on "
                f"{setup['specimen_id']} (hard cap {r['cap_kv']:g} kV)"
                f", {r['planned_cycles']:,} planned cycles.\n\n"
                f"Type {want} to arm:", want)
            if not typed:
                return
            confirms.append(want)
            env = setup['env']
            band = st.paschen_band()
            if _safety.paschen_inhibited(env['p_mbar'] * 100.0, band):
                run_name = setup.get('run_name') or 'pending'
                tok = _safety.paschen_override_token(run_name)
                if not setup.get('run_name'):
                    messagebox.showerror(
                        'Paschen override needs a run name',
                        'Set an explicit run name on the Setup tab so '
                        'the override token is bound to this run.',
                        parent=self)
                    return
                typed = TypedConfirm.ask(
                    self, 'PASCHEN OVERRIDE',
                    f'Attested pressure is inside the HV-inhibit band.'
                    f'\nType exactly:\n{tok}', tok)
                if not typed:
                    return
                confirms.append(tok)
        st.launch_run(mode, confirms)


class TypedConfirm:
    """Modal typed confirmation; returns True only on an exact match.
    Default-refuse: Escape/close = no."""

    @staticmethod
    def ask(parent, title, prompt, token):
        top = tk.Toplevel(parent)
        top.title(title)
        top.transient(parent.winfo_toplevel())
        top.grab_set()
        tk.Label(top, text=prompt, justify='left', padx=12,
                 pady=8).pack()
        var = tk.StringVar()
        e = tk.Entry(top, textvariable=var, width=32)
        e.pack(pady=4)
        e.focus_set()
        result = {'ok': False}

        def submit(_ev=None):
            result['ok'] = var.get().strip() == token
            top.destroy()

        tk.Button(top, text='Confirm', command=submit).pack(pady=6)
        e.bind('<Return>', submit)
        top.bind('<Escape>', lambda _e: top.destroy())
        parent.wait_window(top)
        return result['ok']
