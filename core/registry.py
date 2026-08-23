"""Specimen registry + admin caps.

Two files under data_root, deliberately separate:

- ``specimens.csv`` -- who the specimens are and what has happened to
  them. utf-8-sig (Excel-safe), atomic rewrite, values stored as raw
  strings (half-typed fields must never fail a save -- the vendored
  preset-store rule).
- ``admin_caps.json`` -- SAFETY-CRITICAL hard caps. No GUI editor
  exists on purpose; the file header says who may edit it. Kept apart
  from the registry so Excel-editing specimen metadata can never touch
  a cap. The engine clamps against these caps in TWO places (recipe
  validation and inside every DriveSource set) -- defense in depth.

Registry status transitions go through record_outcome(), never by hand:
the file header says so too.
"""
import csv
import io
import json
import os

SPECIMEN_COLUMNS = [
    'specimen_id', 'created_utc', 'geometry',
    'material_preset', 'elastomer', 'electrode', 'n_layers',
    'length_mm', 'width_mm', 'thickness_um_layer', 'mass_mg',
    'c_est_nf',                      # capacitance estimate, feeds feasibility
    'ebd_ref_v_per_um', 'ebd_ref_temp_c', 'ebd_source',
    'status',                        # virgin|in_test|failed|suspended|retired
    'cycles_accum', 'actuated_s_accum', 'last_run_id', 'failure_mode',
    'notes',
]
GEOMETRIES = ('planar16', 'bender_10x20', 'bender_20x40', 'custom')
STATUSES = ('virgin', 'in_test', 'failed', 'suspended', 'retired')

REGISTRY_NAME = 'specimens.csv'
CAPS_NAME = 'admin_caps.json'

DEFAULT_CAPS = {
    '_comment': ("SAFETY FILE -- hard voltage caps, edited only by the "
                 "lab supervisor, never by the GUI. Per-specimen caps "
                 "override _defaults by geometry. paschen_block_pa is "
                 "the pressure band [Pa] in which HV is inhibited "
                 "(PLACEHOLDER pending lab review). lab_ceiling_kv "
                 "bounds everything."),
    '_defaults': {'planar16': 10.0, 'bender_10x20': 2.5,
                  'bender_20x40': 2.5, 'custom': 2.0},
    'lab_ceiling_kv': 10.0,
    'paschen_block_pa': [1.0, 10000.0],
    'specimens': {},
}


class RegistryError(Exception):
    pass


def _atomic_write(path, data_bytes):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data_bytes)
    os.replace(tmp, path)


class Registry:
    """specimens.csv + admin_caps.json under one data_root."""

    def __init__(self, data_root):
        self.data_root = data_root
        self.path = os.path.join(data_root, REGISTRY_NAME)
        self.caps_path = os.path.join(data_root, CAPS_NAME)
        os.makedirs(data_root, exist_ok=True)
        self._rows = self._load_rows()
        self._caps = self._load_caps()

    # ---- specimens ------------------------------------------------------
    def _load_rows(self):
        if not os.path.exists(self.path):
            return {}
        rows = {}
        with open(self.path, 'r', newline='', encoding='utf-8-sig') as fh:
            for row in csv.DictReader(fh):
                sid = (row.get('specimen_id') or '').strip()
                if sid:
                    rows[sid] = {k: (row.get(k) or '') for k in
                                 SPECIMEN_COLUMNS}
        return rows

    def _save_rows(self):
        buf = io.StringIO(newline='')
        w = csv.DictWriter(buf, fieldnames=SPECIMEN_COLUMNS)
        w.writeheader()
        for sid in sorted(self._rows):
            w.writerow(self._rows[sid])
        # utf-8-sig so Excel opens it right; atomic so a crash mid-save
        # cannot half-write the registry.
        _atomic_write(self.path, buf.getvalue().encode('utf-8-sig'))

    def ids(self):
        return sorted(self._rows)

    def get(self, specimen_id):
        row = self._rows.get(str(specimen_id).strip())
        if row is None:
            raise RegistryError(
                f"specimen '{specimen_id}' not in the registry "
                f"({self.path}); create it first "
                f"(lifecycle_cli specimen create ...)")
        return dict(row)

    def create(self, specimen_id, geometry, created_utc, **fields):
        sid = str(specimen_id).strip()
        if not sid:
            raise RegistryError('specimen_id must be non-empty')
        if sid in self._rows:
            raise RegistryError(f"specimen '{sid}' already exists")
        if geometry not in GEOMETRIES:
            raise RegistryError(
                f"geometry '{geometry}' not one of {GEOMETRIES}")
        row = {k: '' for k in SPECIMEN_COLUMNS}
        row.update(specimen_id=sid, geometry=geometry,
                   created_utc=created_utc, status='virgin',
                   cycles_accum='0', actuated_s_accum='0')
        for k, v in fields.items():
            if k not in SPECIMEN_COLUMNS:
                raise RegistryError(f"unknown specimen field '{k}'")
            row[k] = '' if v is None else str(v)
        self._rows[sid] = row
        self._save_rows()
        return dict(row)

    def update(self, specimen_id, **fields):
        sid = str(specimen_id).strip()
        row = self._rows.get(sid)
        if row is None:
            raise RegistryError(f"specimen '{sid}' not in the registry")
        if 'status' in fields:
            raise RegistryError(
                'status changes go through record_outcome(), not update()')
        for k, v in fields.items():
            if k not in SPECIMEN_COLUMNS:
                raise RegistryError(f"unknown specimen field '{k}'")
            row[k] = '' if v is None else str(v)
        self._save_rows()
        return dict(row)

    def mark_in_test(self, specimen_id, run_id):
        row = self._rows.get(str(specimen_id).strip())
        if row is None:
            raise RegistryError(f"specimen '{specimen_id}' not registered")
        if row['status'] in ('failed', 'retired'):
            raise RegistryError(
                f"specimen '{specimen_id}' is {row['status']} -- it does "
                f"not go back on the rig (create a new specimen)")
        row['status'] = 'in_test'
        row['last_run_id'] = run_id
        self._save_rows()

    def record_outcome(self, specimen_id, disposition, cycles, actuated_s,
                       run_id, failure_mode=''):
        """End-of-run bookkeeping.

        disposition: 'failed' latches the specimen failed with its
        failure mode. ANYTHING else (complete = reached the cap,
        suspended = operator right-censor, aborted = rig reason) lands on
        'suspended': the specimen has demonstrably survived
        cycles_accum cycles so far -- exactly what right-censored means
        -- and mark_in_test() can put it back on the rig, at which point
        a later failure counts its full accumulated history."""
        row = self._rows.get(str(specimen_id).strip())
        if row is None:
            raise RegistryError(f"specimen '{specimen_id}' not registered")
        prev_c = float(row.get('cycles_accum') or 0)
        prev_a = float(row.get('actuated_s_accum') or 0)
        row['cycles_accum'] = f"{prev_c + float(cycles):.0f}"
        row['actuated_s_accum'] = f"{prev_a + float(actuated_s):.1f}"
        row['last_run_id'] = run_id
        if disposition == 'failed':
            row['status'] = 'failed'
            row['failure_mode'] = failure_mode or 'unspecified'
        else:
            row['status'] = 'suspended'
        self._save_rows()

    # ---- caps -----------------------------------------------------------
    def _load_caps(self):
        if not os.path.exists(self.caps_path):
            _atomic_write(self.caps_path,
                          json.dumps(DEFAULT_CAPS, indent=2).encode('utf-8'))
            return dict(DEFAULT_CAPS)
        with open(self.caps_path, 'r', encoding='utf-8') as fh:
            return json.load(fh)

    def reload_caps(self):
        self._caps = self._load_caps()
        return self._caps

    def cap_kv(self, specimen_id):
        """Hard voltage cap for one specimen: per-specimen entry, else
        the geometry default, else the lab ceiling -- and NEVER above the
        lab ceiling regardless of what the file says."""
        caps = self._caps
        ceiling = float(caps.get('lab_ceiling_kv', 10.0))
        row = self.get(specimen_id)
        per = caps.get('specimens', {}).get(row['specimen_id'])
        if per is not None:
            return min(float(per), ceiling)
        geo = caps.get('_defaults', {}).get(row['geometry'])
        if geo is not None:
            return min(float(geo), ceiling)
        return ceiling

    def paschen_band_pa(self):
        band = self._caps.get('paschen_block_pa',
                              DEFAULT_CAPS['paschen_block_pa'])
        return (float(band[0]), float(band[1]))

    # ---- survival-analysis view ----------------------------------------
    def survival_rows(self):
        """[{specimen_id, cycles, status 'failed'|'suspended'}] for the
        Weibull module. Only failed/suspended specimens carry
        information; in_test and virgin are excluded here (the analysis
        layer may choose to treat in_test as suspended-at-current)."""
        out = []
        for sid, row in self._rows.items():
            if row['status'] in ('failed', 'suspended'):
                out.append({'specimen_id': sid,
                            'cycles': float(row.get('cycles_accum') or 0),
                            'status': row['status'],
                            'failure_mode': row.get('failure_mode', '')})
        return sorted(out, key=lambda r: r['cycles'])
