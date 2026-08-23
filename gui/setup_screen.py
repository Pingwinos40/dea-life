"""Screen 1 -- Setup: wizard-as-checklist. Five numbered sections, all
visible, green ticks, nothing hidden behind pages. Grad-knobs only
(drive kV / frequency / cycle cap overrides); everything else lives in
the recipe template and is viewable (read-only) under Advanced.
"""
import copy
import json
import os
import tkinter as tk
from tkinter import messagebox, ttk

from ui_widgets import add_tooltip  # vendored

from core import feasibility as _feasibility
from core import lifefactors as _lifefactors
from core import recipe as _recipe
from core.registry import GEOMETRIES, RegistryError

from .widgets import COLORS, CollapsibleSection, ValidatedEntry

GEOM_NOTE = {
    'planar': ('Top-down camera over the disc; backlight below.\n'
               'Vision: vendored sldea_edge active-area detection.'),
    'bender': ('Side-view camera, diffuse LED BACKLIGHT behind the\n'
               'specimen, ArUco tag on the clamp block.\n'
               'Vision: silhouette centerline -> tip deflection.'),
}


class SetupScreen(tk.Frame):
    def __init__(self, master, state):
        super().__init__(master)
        self.state = state              # shared AppState
        self._resolved = None
        self._build()
        self.refresh_specimens()

    # ---- layout ---------------------------------------------------------
    def _build(self):
        left = tk.Frame(self)
        left.pack(side='left', fill='both', expand=True, padx=8,
                  pady=6)
        right = tk.Frame(self, width=260)
        right.pack(side='right', fill='y', padx=8, pady=6)

        # -- 1 specimen
        self.s1 = self._section(left, '1. Specimen')
        row = tk.Frame(self.s1)
        row.pack(fill='x')
        self.spec_var = tk.StringVar()
        self.spec_combo = ttk.Combobox(row, textvariable=self.spec_var,
                                       state='readonly', width=24)
        self.spec_combo.pack(side='left')
        self.spec_combo.bind('<<ComboboxSelected>>',
                             lambda e: self._on_specimen())
        tk.Button(row, text='New specimen...',
                  command=self._new_specimen).pack(side='left', padx=6)
        tk.Button(row, text='Reload',
                  command=self.refresh_specimens).pack(side='left')
        self.spec_info = tk.Label(self.s1, text='(pick a specimen)',
                                  anchor='w', justify='left', fg='#555')
        self.spec_info.pack(fill='x', pady=2)
        self.cap_lbl = tk.Label(self.s1, text='', anchor='w',
                                font=('TkDefaultFont', 10, 'bold'),
                                fg=COLORS['red_fg'])
        self.cap_lbl.pack(fill='x')
        add_tooltip(self.cap_lbl,
                    'Hard voltage cap from admin_caps.json. No GUI '
                    'edits, on purpose: the engine refuses anything '
                    'above it independently of this screen.')

        # -- 2 geometry & camera
        self.s2 = self._section(left, '2. Geometry & camera')
        self.geom_lbl = tk.Label(self.s2, text='(from the specimen)',
                                 font=('TkDefaultFont', 10, 'bold'))
        self.geom_lbl.pack(anchor='w')
        self.geom_note = tk.Label(self.s2, text='', justify='left',
                                  fg='#555')
        self.geom_note.pack(anchor='w')

        # -- 3 environment
        self.s3 = self._section(left, '3. Environment (manual entry)')
        self.env_t = ValidatedEntry(self.s3, 'Chamber temperature',
                                    'degC', lo=-80, hi=200, default=23,
                                    on_change=self._recompute)
        self.env_t.pack(fill='x')
        self.env_p = ValidatedEntry(self.s3, 'Chamber pressure', 'mbar',
                                    lo=1e-8, hi=2000, default=1013,
                                    on_change=self._recompute)
        self.env_p.pack(fill='x')
        row = tk.Frame(self.s3)
        row.pack(fill='x')
        tk.Label(row, text='Your name', width=22, anchor='w').pack(
            side='left')
        self.op_var = tk.StringVar()
        tk.Entry(row, textvariable=self.op_var, width=18).pack(
            side='left')
        self.attest_var = tk.BooleanVar(value=False)
        cb = tk.Checkbutton(
            self.s3, variable=self.attest_var,
            command=self._recompute,
            text='I attest the chamber currently reads the values '
                 'above')
        cb.pack(anchor='w')
        add_tooltip(cb, 'Recorded with your name + timestamp. Goes '
                        'stale after the recipe\'s stale_after_s and '
                        'must be re-entered at every wait_env block.')

        # -- 4 recipe
        self.s4 = self._section(left, '4. Recipe')
        row = tk.Frame(self.s4)
        row.pack(fill='x')
        tk.Label(row, text='Template', width=22, anchor='w').pack(
            side='left')
        self.recipe_var = tk.StringVar()
        self.recipe_combo = ttk.Combobox(row,
                                         textvariable=self.recipe_var,
                                         state='readonly', width=30)
        self.recipe_combo.pack(side='left')
        self.recipe_combo.bind('<<ComboboxSelected>>',
                               lambda e: self._on_recipe())
        self.kv_override = ValidatedEntry(
            self.s4, 'Drive V_pk (blank = template)', 'kV', lo=0.01,
            hi=10.0, default='', on_change=self._recompute,
            tooltip='Overrides the template drive voltage. The '
                    'specimen hard cap still binds.')
        self.kv_override.pack(fill='x')
        self.freq_override = ValidatedEntry(
            self.s4, 'Frequency (blank = template)', 'Hz', lo=0.01,
            hi=10.0, default='', on_change=self._recompute)
        self.freq_override.pack(fill='x')
        self.cap_override = ValidatedEntry(
            self.s4, 'Cycle cap (blank = template)', 'cycles', lo=1,
            hi=1e9, default='', on_change=self._recompute)
        self.cap_override.pack(fill='x')
        self.adv = CollapsibleSection(self.s4, 'Advanced (resolved '
                                                'recipe, read-only)')
        self.adv.pack(fill='x', pady=2)
        self.adv_text = tk.Text(self.adv.body, height=14, width=70,
                                font=('Courier', 8), state='disabled')
        self.adv_text.pack(fill='both')

        # -- 5 identity + validate
        self.s5 = self._section(left, '5. Run identity')
        row = tk.Frame(self.s5)
        row.pack(fill='x')
        tk.Label(row, text='Run name (blank = auto)', width=22,
                 anchor='w').pack(side='left')
        self.runname_var = tk.StringVar()
        tk.Entry(row, textvariable=self.runname_var, width=24).pack(
            side='left')
        self.validate_btn = tk.Button(
            left, text='Validate  >>  Continue to Pre-flight',
            font=('TkDefaultFont', 11, 'bold'), command=self.validate)
        self.validate_btn.pack(pady=8)
        self.verdict = tk.Label(left, text='', justify='left',
                                anchor='w', wraplength=560)
        self.verdict.pack(fill='x')

        # -- right sidebar: feasibility + life factors
        tk.Label(right, text='FEASIBILITY (live)',
                 font=('TkDefaultFont', 10, 'bold'),
                 fg=COLORS['ink']).pack(anchor='w')
        self.feas_lbl = tk.Label(right, text='(pick specimen+recipe)',
                                 justify='left', anchor='nw',
                                 wraplength=250, relief='groove',
                                 bd=1, padx=6, pady=6)
        self.feas_lbl.pack(fill='x', pady=4)
        tk.Label(right, text='LIFE-TEST FACTORS',
                 font=('TkDefaultFont', 10, 'bold'),
                 fg=COLORS['ink']).pack(anchor='w', pady=(10, 0))
        self.mission = ValidatedEntry(right, 'Mission op cycles', '',
                                      lo=0, default=10000, width=9,
                                      on_change=self._recompute)
        self.mission.pack(fill='x')
        self.lf_lbl = tk.Label(right, text='', justify='left',
                               anchor='nw', wraplength=250,
                               relief='groove', bd=1, padx=6, pady=6)
        self.lf_lbl.pack(fill='x', pady=4)

    def _section(self, parent, title):
        f = tk.LabelFrame(parent, text=title, padx=8, pady=4,
                          font=('TkDefaultFont', 10, 'bold'))
        f.pack(fill='x', pady=3)
        return f

    # ---- data -----------------------------------------------------------
    def refresh_specimens(self):
        reg = self.state.registry()
        ids = reg.ids() if reg else []
        self.spec_combo['values'] = ids
        recipes_dir = os.path.join(self.state.root, 'recipes')
        recs = sorted(n for n in os.listdir(recipes_dir)
                      if n.endswith('.json')) \
            if os.path.isdir(recipes_dir) else []
        self.recipe_combo['values'] = recs
        if recs and not self.recipe_var.get():
            self.recipe_var.set(recs[0])
        self._recompute()

    def _on_specimen(self):
        reg = self.state.registry()
        sid = self.spec_var.get()
        if not reg or not sid:
            return
        row = reg.get(sid)
        cap = reg.cap_kv(sid)
        info = (f"{row['geometry']}  |  {row.get('elastomer') or '?'} / "
                f"{row.get('electrode') or '?'}  |  status "
                f"{row['status']}  |  {row.get('cycles_accum') or 0} "
                f"cycles so far  |  C_est "
                f"{row.get('c_est_nf') or '?'} nF")
        self.spec_info.config(text=info)
        self.cap_lbl.config(text=f'HARD CAP {cap:g} kV '
                                 f'(admin_caps.json, read-only)')
        geom = 'bender' if row['geometry'].startswith('bender') \
            else 'planar'
        self.geom_lbl.config(text=f'{geom.upper()} article '
                                  f'({row["geometry"]})')
        self.geom_note.config(text=GEOM_NOTE[geom])
        self._recompute()

    def _on_recipe(self):
        self._recompute()

    def _new_specimen(self):
        NewSpecimenDialog(self, self.state,
                          on_done=self.refresh_specimens)

    # ---- live computation ----------------------------------------------
    def _recipe_dict(self):
        name = self.recipe_var.get()
        if not name:
            return None
        path = os.path.join(self.state.root, 'recipes', name)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
        except Exception:
            return None
        d = copy.deepcopy(d)
        kv = self.kv_override.value()
        if kv:
            d.setdefault('drive', {})['v_pk'] = {'kv': kv}
        f = self.freq_override.value()
        if f:
            d.setdefault('drive', {})['freq_hz'] = f
        cap = self.cap_override.value()
        if cap:
            d.setdefault('stop', {})['max_cycles'] = int(cap)
        return d

    def _recompute(self):
        reg = self.state.registry()
        sid = self.spec_var.get()
        d = self._recipe_dict()
        if not (reg and sid and d):
            return
        try:
            row = reg.get(sid)
        except RegistryError:
            return
        drive = d.get('drive', {})
        vpk = drive.get('v_pk', {})
        kv = vpk.get('kv')
        c_est = None
        try:
            c_est = float(row.get('c_est_nf') or '')
        except ValueError:
            pass
        freq = drive.get('freq_hz', 0)
        if kv and freq:
            rep = _feasibility.check_drive(freq, c_est, kv)
            v = rep['verdict'].upper()
            color = {'OK': COLORS['good'], 'WARN': COLORS['amber_fg'],
                     'REFUSE': COLORS['red_fg']}.get(v, '#555')
            ipk = rep['i_pk_ua']
            txt = (f"{v}\n"
                   + (f"I_pk {ipk:.0f} uA "
                      f"({100 * (rep['i_frac'] or 0):.0f}% of Trek "
                      f"+/-2 mA)\n" if ipk is not None else
                      'C unknown -- record c_est_nf\n')
                   + (f"max feasible f: "
                      f"{rep['max_feasible_hz']:.1f} Hz\n"
                      if rep['max_feasible_hz'] else '')
                   + '\n'.join(rep['msgs']))
            self.feas_lbl.config(text=txt, fg=color)
        else:
            self.feas_lbl.config(
                text='(fraction-of-breakdown drive: resolved at '
                     'Validate against the registry E_BD)',
                fg='#555')
        cap_cycles = int((d.get('stop') or {}).get('max_cycles') or 0)
        mission = self.mission.value() or 0
        if cap_cycles and mission:
            lines = _lifefactors.advisory_lines(cap_cycles,
                                                operational=mission)
            self.lf_lbl.config(text='\n\n'.join(lines))

    # ---- validate -------------------------------------------------------
    def validate(self):
        reg = self.state.registry()
        sid = self.spec_var.get()
        d = self._recipe_dict()
        if not reg or not sid:
            self._verdict('Pick a specimen first.', bad=True)
            return
        if not d:
            self._verdict('Pick a recipe template.', bad=True)
            return
        for e in (self.env_t, self.env_p):
            if not e.validate():
                self._verdict('Fix the red environment fields.',
                              bad=True)
                return
        if not self.attest_var.get() or not self.op_var.get().strip():
            self._verdict('Environment attestation (checkbox + name) '
                          'is required.', bad=True)
            return
        try:
            row = reg.get(sid)
            cap = reg.cap_kv(sid)
            c_est = None
            if (row.get('c_est_nf') or '').strip():
                c_est = float(row['c_est_nf'])
            rec = _recipe.Recipe.from_dict(d)
            resolved = _recipe.resolve(rec, row, cap, c_est_nf=c_est)
        except (_recipe.RecipeError, RegistryError, ValueError) as e:
            self._verdict(str(e), bad=True)
            self._resolved = None
            self.state.on_setup_valid(None)
            return
        self._resolved = resolved
        self.adv_text.config(state='normal')
        self.adv_text.delete('1.0', 'end')
        self.adv_text.insert('1.0', json.dumps(
            {k: v for k, v in resolved.items() if k != 'blocks'},
            indent=1, default=str)
            + f"\n... plus {len(resolved['blocks'])} resolved blocks")
        self.adv_text.config(state='disabled')
        drv = resolved['drive']
        self._verdict(
            f"OK -- {resolved['name']} on {sid}: "
            f"{drv['waveform']} {drv['freq_hz']:g} Hz at "
            f"{drv['v_pk_kv']:g} kV pk (cap {cap:g}), "
            f"{resolved['planned_cycles']:,} planned / "
            f"{resolved['stop']['max_cycles']:,} cap. "
            f"sha256 {resolved['sha256'][:12]}...")
        self.state.on_setup_valid({
            'resolved': resolved, 'specimen_id': sid,
            'recipe_dict': d,
            'recipe_name': self.recipe_var.get(),
            'run_name': self.runname_var.get().strip(),
            'env': {'t_c': self.env_t.value(),
                    'p_mbar': self.env_p.value(),
                    'operator': self.op_var.get().strip()},
        })

    def _verdict(self, text, bad=False):
        self.verdict.config(text=text, fg=COLORS['red_fg'] if bad
                            else COLORS['good'])


class NewSpecimenDialog(tk.Toplevel):
    """Material presets in dropdowns; free text only in notes (the
    grad-proofing rule)."""

    ELASTOMERS = ('UV-RSE', 'CN9018', 'Dragonskin-20', 'Elastosil 2030',
                  'Sylgard 184', 'other (notes)')
    ELECTRODES = ('CNT', 'Carbon Solutions P3-SWNT',
                  'Carbon Solutions P2-SWNT', 'carbon black', 'eGaIn')

    def __init__(self, master, state, on_done=None):
        super().__init__(master)
        self.title('New specimen')
        self.state = state
        self.on_done = on_done
        self.transient(master)
        body = tk.Frame(self, padx=10, pady=8)
        body.pack(fill='both', expand=True)

        self.vars = {}

        def row(label, widget):
            r = tk.Frame(body)
            r.pack(fill='x', pady=2)
            tk.Label(r, text=label, width=20, anchor='w').pack(
                side='left')
            widget(r)

        def combo(key, values):
            def make(r):
                v = tk.StringVar(value=values[0])
                self.vars[key] = v
                ttk.Combobox(r, textvariable=v, values=values,
                             state='readonly', width=26).pack(
                    side='left')
            return make

        def entry(key, default=''):
            def make(r):
                v = tk.StringVar(value=default)
                self.vars[key] = v
                tk.Entry(r, textvariable=v, width=28).pack(side='left')
            return make

        row('Specimen ID', entry('specimen_id'))
        row('Geometry', combo('geometry', GEOMETRIES))
        row('Elastomer', combo('elastomer', self.ELASTOMERS))
        row('Electrode', combo('electrode', self.ELECTRODES))
        row('Layers', entry('n_layers', '1'))
        row('Layer thickness [um]', entry('thickness_um_layer', '50'))
        row('C estimate [nF]', entry('c_est_nf'))
        row('E_BD median [V/um]', entry('ebd_ref_v_per_um'))
        row('E_BD ref temp [C]', entry('ebd_ref_temp_c', '23'))
        row('Notes', entry('notes'))
        tk.Button(body, text='Create', command=self._create).pack(
            pady=6)
        self.err = tk.Label(body, text='', fg=COLORS['red_fg'])
        self.err.pack()

    def _create(self):
        reg = self.state.registry()
        vals = {k: v.get().strip() for k, v in self.vars.items()}
        sid = vals.pop('specimen_id')
        geom = vals.pop('geometry')
        try:
            from core.clock import Clock
            reg.create(sid, geom, Clock().now_iso(timespec='seconds'),
                       **{k: v for k, v in vals.items() if v})
        except RegistryError as e:
            self.err.config(text=str(e))
            return
        messagebox.showinfo(
            'Specimen created',
            f'{sid} created (hard cap {reg.cap_kv(sid):g} kV from the '
            f'geometry default; a per-specimen cap needs an '
            f'admin_caps.json edit).', parent=self)
        if self.on_done:
            self.on_done()
        self.destroy()
