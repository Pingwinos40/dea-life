"""LifecycleRecipe: the on-disk JSON test definition and its validation.

Hand-editable JSON, strict on load: unknown keys ANYWHERE are hard
errors -- a typo'd threshold must not silently become a default. All
problems are collected and reported together (one fix-everything pass,
not error whack-a-mole).

Two stages:

- ``Recipe.load(path)`` / ``Recipe.from_dict(d)`` -- parse + static
  validation (no registry needed).
- ``resolve(recipe, specimen_row, cap_kv)`` -- turns
  ``{"fraction_of_breakdown": ...}`` voltages into kV against the
  specimen's registry breakdown reference, enforces the specimen hard
  cap on every reachable level, dry-constructs every staircase
  (SldeaProfile's own constraints checked HERE, at validate time, never
  mid-run), and runs the feasibility check. Returns a plain dict -- the
  exact resolved recipe that is hashed, written into the run folder, and
  compared on resume.
"""
import copy
import hashlib
import json

from . import failure as _failure
from . import feasibility as _feasibility

import sldea_profile  # vendored (lib/ on sys.path via core.__init__)

SCHEMA = 'sldea-lifecycle/1'
MAX_FREQ_HZ = 10.0        # v1 ceiling (user decision 2026-08-23): the
                          # camera tracks motion directly; no strobe.
WAVEFORMS = ('SINE', 'SQUARE', 'RAMP')
GEOMETRIES = ('planar', 'bender')
COUNTING = ('burst', 'timed')
BLOCK_TYPES = ('cycle', 'fast_interlude', 'full_interlude', 'dc_hold',
               'ramp', 'wait_env', 'loop')

_TOP_KEYS = {'schema', 'name', 'description', 'geometry', 'drive',
             'reference', 'environment', 'counting',
             'camera_track_max_hz', 'stop', 'failure_rules', 'blocks'}
_DRIVE_KEYS = {'waveform', 'freq_hz', 'v_pk', 'v_min_kv'}
_REF_KEYS = {'ref_kv', 'ref_freq_hz', 'ref_cycles', 'leak_hold_kv',
             'leak_hold_s'}
_ENV_KEYS = {'t_c_band', 'p_mbar_band', 'stale_after_s',
             'reattest_on_rearm'}
_STOP_KEYS = {'max_cycles', 'max_wall_h'}
_STAIR_KEYS = {'start_kv', 'end_kv', 'step_kv', 'n_steps', 'ramp_s',
               'landing_s', 'settle_s', 'snap_lead_s'}
_BLOCK_KEYS = {
    'cycle': {'type', 'n_cycles', 'role', 'waveform', 'freq_hz',
              'v_pk_kv', 'v_min_kv'},
    'fast_interlude': {'type', 'ref_kv', 'ref_freq_hz', 'ref_cycles',
                       'leak_hold_kv', 'leak_hold_s'},
    'full_interlude': {'type', 'staircase', 'set_baseline'},
    'dc_hold': {'type', 'kv', 'hold_s', 'sample_hz'},
    'ramp': {'type', 'to_kv', 'rate_kv_s'},
    'wait_env': {'type', 'prompt', 't_c_band', 'p_mbar_band'},
    'loop': {'type', 'blocks', 'repeat'},
}

REFERENCE_DEFAULTS = {'ref_kv': 1.0, 'ref_freq_hz': 0.1, 'ref_cycles': 3,
                      'leak_hold_kv': 1.0, 'leak_hold_s': 20.0}
ENVIRONMENT_DEFAULTS = {'t_c_band': [15.0, 30.0],
                        'p_mbar_band': [900.0, 1100.0],
                        'stale_after_s': 1800.0,
                        'reattest_on_rearm': True}


class RecipeError(Exception):
    """Carries every problem found, joined for display."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('recipe invalid:\n  - ' +
                         '\n  - '.join(self.problems))


def _unknown(d, allowed, where, probs):
    extra = set(d) - allowed
    if extra:
        probs.append(f"{where}: unknown key(s) {sorted(extra)}")


def _num(d, key, where, probs, lo=None, hi=None, default=None,
         required=False):
    if key not in d:
        if required:
            probs.append(f"{where}: missing '{key}'")
        return default
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        probs.append(f"{where}.{key}: not a number ({d[key]!r})")
        return default
    if lo is not None and v < lo:
        probs.append(f"{where}.{key}: {v:g} below minimum {lo:g}")
    if hi is not None and v > hi:
        probs.append(f"{where}.{key}: {v:g} above maximum {hi:g}")
    return v


class Recipe:
    """Parsed + statically-validated recipe. Fields mirror the JSON."""

    def __init__(self, d):
        self.raw = d
        probs = []
        self._parse(d, probs)
        if probs:
            raise RecipeError(probs)

    # ---- parsing --------------------------------------------------------
    def _parse(self, d, probs):
        if not isinstance(d, dict):
            probs.append('top level must be a JSON object')
            return
        _unknown(d, _TOP_KEYS, 'top level', probs)
        if d.get('schema') != SCHEMA:
            probs.append(f"schema must be '{SCHEMA}' "
                         f"(got {d.get('schema')!r})")
        self.name = str(d.get('name') or '').strip()
        if not self.name:
            probs.append("missing 'name'")
        self.description = str(d.get('description') or '')
        self.geometry = d.get('geometry')
        if self.geometry not in GEOMETRIES:
            probs.append(f"geometry must be one of {GEOMETRIES} "
                         f"(got {self.geometry!r})")

        drive = d.get('drive')
        if not isinstance(drive, dict):
            probs.append("missing/invalid 'drive' object")
            drive = {}
        _unknown(drive, _DRIVE_KEYS, 'drive', probs)
        self.waveform = drive.get('waveform', 'SINE')
        if self.waveform not in WAVEFORMS:
            probs.append(f"drive.waveform must be one of {WAVEFORMS}")
        self.freq_hz = _num(drive, 'freq_hz', 'drive', probs,
                            lo=0.001, hi=MAX_FREQ_HZ, required=True)
        self.v_min_kv = _num(drive, 'v_min_kv', 'drive', probs, lo=0.0,
                             hi=sldea_profile.TREK_MAX_KV, default=0.0)
        self.v_pk_spec = self._parse_vpk(drive.get('v_pk'), probs)

        ref = dict(REFERENCE_DEFAULTS)
        got = d.get('reference', {})
        if not isinstance(got, dict):
            probs.append("'reference' must be an object")
            got = {}
        _unknown(got, _REF_KEYS, 'reference', probs)
        ref.update(got)
        self.reference = ref
        _num(ref, 'ref_kv', 'reference', probs, lo=0.0,
             hi=sldea_profile.TREK_MAX_KV)
        _num(ref, 'ref_freq_hz', 'reference', probs, lo=0.001, hi=2.0)
        _num(ref, 'leak_hold_s', 'reference', probs, lo=1.0, hi=600.0)

        env = dict(ENVIRONMENT_DEFAULTS)
        got = d.get('environment', {})
        if not isinstance(got, dict):
            probs.append("'environment' must be an object")
            got = {}
        _unknown(got, _ENV_KEYS, 'environment', probs)
        env.update(got)
        self.environment = env
        for band in ('t_c_band', 'p_mbar_band'):
            b = env.get(band)
            if (not isinstance(b, (list, tuple)) or len(b) != 2
                    or not all(isinstance(x, (int, float)) for x in b)
                    or b[0] >= b[1]):
                probs.append(f"environment.{band} must be [lo, hi] "
                             f"numbers with lo < hi (got {b!r})")

        self.counting = d.get('counting', 'burst')
        if self.counting not in COUNTING:
            probs.append(f"counting must be one of {COUNTING}")
        self.camera_track_max_hz = _num(
            d, 'camera_track_max_hz', 'top level', probs, lo=0.0,
            hi=MAX_FREQ_HZ, default=0.5)

        stop = d.get('stop')
        if not isinstance(stop, dict):
            probs.append("missing/invalid 'stop' object "
                         "(needs max_cycles, max_wall_h)")
            stop = {}
        _unknown(stop, _STOP_KEYS, 'stop', probs)
        self.max_cycles = _num(stop, 'max_cycles', 'stop', probs, lo=1,
                               required=True)
        self.max_wall_h = _num(stop, 'max_wall_h', 'stop', probs,
                               lo=0.001, required=True)
        if self.max_cycles is not None:
            self.max_cycles = int(self.max_cycles)

        rules = d.get('failure_rules', {})
        probs.extend(_failure.validate_rules(rules))
        self.failure_rules_overrides = rules

        blocks = d.get('blocks')
        if not isinstance(blocks, list) or not blocks:
            probs.append("'blocks' must be a non-empty list")
            blocks = []
        self.blocks = [self._parse_block(b, f'blocks[{i}]', probs, depth=0)
                       for i, b in enumerate(blocks)]
        self._check_structure(probs)

    def _parse_vpk(self, spec, probs):
        if isinstance(spec, dict) and set(spec) == {'kv'}:
            kv = _num(spec, 'kv', 'drive.v_pk', probs, lo=0.001,
                      hi=sldea_profile.TREK_MAX_KV, required=True)
            return {'kv': kv}
        if isinstance(spec, dict) and \
                set(spec) == {'fraction_of_breakdown', 'temp_c'}:
            frac = _num(spec, 'fraction_of_breakdown', 'drive.v_pk',
                        probs, lo=0.01, hi=0.95, required=True)
            temp = _num(spec, 'temp_c', 'drive.v_pk', probs,
                        required=True)
            return {'fraction_of_breakdown': frac, 'temp_c': temp}
        probs.append("drive.v_pk must be {'kv': X} or "
                     "{'fraction_of_breakdown': F, 'temp_c': T}")
        return {}

    def _parse_block(self, b, where, probs, depth):
        if not isinstance(b, dict) or 'type' not in b:
            probs.append(f"{where}: every block needs a 'type'")
            return {'type': 'invalid'}
        btype = b['type']
        if btype not in BLOCK_TYPES:
            probs.append(f"{where}: unknown block type '{btype}'")
            return {'type': 'invalid'}
        _unknown(b, _BLOCK_KEYS[btype], where, probs)
        out = dict(b)
        if btype == 'cycle':
            n = _num(b, 'n_cycles', where, probs, lo=1, required=True)
            out['n_cycles'] = int(n) if n else 0
            out['role'] = str(b.get('role', ''))
            if out['role'] not in ('', 'break_in'):
                probs.append(f"{where}.role must be '' or 'break_in'")
            if 'freq_hz' in b:
                _num(b, 'freq_hz', where, probs, lo=0.001, hi=MAX_FREQ_HZ)
            if 'waveform' in b and b['waveform'] not in WAVEFORMS:
                probs.append(f"{where}.waveform must be one of {WAVEFORMS}")
        elif btype == 'fast_interlude':
            pass                     # all keys optional overrides
        elif btype == 'full_interlude':
            st = b.get('staircase')
            if not isinstance(st, dict):
                probs.append(f"{where}: full_interlude needs a "
                             f"'staircase' object")
            else:
                _unknown(st, _STAIR_KEYS, f'{where}.staircase', probs)
                self._check_staircase(st, f'{where}.staircase', probs)
            out['set_baseline'] = bool(b.get('set_baseline', False))
        elif btype == 'dc_hold':
            _num(b, 'kv', where, probs, lo=0.0,
                 hi=sldea_profile.TREK_MAX_KV, required=True)
            _num(b, 'hold_s', where, probs, lo=0.1, required=True)
            _num(b, 'sample_hz', where, probs, lo=0.1, hi=2.0,
                 default=2.0)
        elif btype == 'ramp':
            _num(b, 'to_kv', where, probs, lo=0.0,
                 hi=sldea_profile.TREK_MAX_KV, required=True)
            _num(b, 'rate_kv_s', where, probs, lo=0.001, required=True)
        elif btype == 'wait_env':
            if not str(b.get('prompt', '')).strip():
                probs.append(f"{where}: wait_env needs a 'prompt'")
        elif btype == 'loop':
            if depth >= 2:
                probs.append(f"{where}: loops nest at most 2 deep")
            rep = _num(b, 'repeat', where, probs, lo=1, required=True)
            out['repeat'] = int(rep) if rep else 1
            inner = b.get('blocks')
            if not isinstance(inner, list) or not inner:
                probs.append(f"{where}: loop needs a non-empty 'blocks'")
                out['blocks'] = []
            else:
                out['blocks'] = [
                    self._parse_block(ib, f'{where}.blocks[{j}]', probs,
                                      depth + 1)
                    for j, ib in enumerate(inner)]
        return out

    def _check_staircase(self, st, where, probs):
        """Static pre-check of the vendored SldeaProfile constraints so
        the constructor can never throw mid-run. The vendored defaults
        (settle 2 s, snap_lead 1 s) were tuned for 60 s landings
        (sldea_profile.py:776-781) -- short-landing interlude staircases
        must satisfy settle_s + snap_lead_s < landing_s explicitly."""
        landing = _num(st, 'landing_s', where, probs, lo=0.5, default=8.0)
        settle = _num(st, 'settle_s', where, probs, lo=0.0, default=2.0)
        lead = _num(st, 'snap_lead_s', where, probs, lo=0.0, default=1.0)
        if landing is not None and settle is not None and lead is not None:
            if settle + lead >= landing:
                probs.append(
                    f"{where}: settle_s + snap_lead_s "
                    f"({settle:g}+{lead:g}) must be < landing_s "
                    f"({landing:g}) -- SldeaProfile refuses it "
                    f"(vendored constraint)")
        if 'step_kv' not in st and 'n_steps' not in st:
            probs.append(f"{where}: needs step_kv or n_steps")
        end = st.get('end_kv')
        ok_end = (isinstance(end, (int, float)) or
                  (isinstance(end, dict) and set(end) == {'fraction_of_vpk'}
                   and isinstance(end.get('fraction_of_vpk'), (int, float))
                   and 0 < end['fraction_of_vpk'] <= 1.0))
        if not ok_end:
            probs.append(f"{where}: end_kv must be a number or "
                         f"{{'fraction_of_vpk': 0..1}}")

    def _check_structure(self, probs):
        """Exactly one set_baseline full_interlude, positioned after a
        break_in cycle block (both at top level, outside loops)."""
        baseline_idx = [i for i, b in enumerate(self.blocks)
                        if b.get('type') == 'full_interlude'
                        and b.get('set_baseline')]
        breakin_idx = [i for i, b in enumerate(self.blocks)
                       if b.get('type') == 'cycle'
                       and b.get('role') == 'break_in']
        if len(baseline_idx) != 1:
            probs.append(
                f"exactly one full_interlude with set_baseline=true is "
                f"required at top level (found {len(baseline_idx)}) -- "
                f"it anchors every ratio-based failure criterion")
        if not breakin_idx:
            probs.append("a top-level cycle block with role='break_in' is "
                         "required before the baseline interlude "
                         "(ratio criteria reference POST-break-in state)")
        if baseline_idx and breakin_idx and \
                baseline_idx[0] < breakin_idx[0]:
            probs.append('the baseline interlude must come AFTER the '
                         'break_in cycle block')

    # ---- helpers --------------------------------------------------------
    @classmethod
    def load(cls, path):
        with open(path, 'r', encoding='utf-8') as fh:
            try:
                d = json.load(fh)
            except json.JSONDecodeError as e:
                raise RecipeError([f'not valid JSON: {e}'])
        return cls(d)

    @classmethod
    def from_dict(cls, d):
        return cls(copy.deepcopy(d))

    def flatten(self):
        """Yield (flat_idx, loop_path, block) in execution order.
        loop_path like '3[2/20].1[7/50]' locates a block instance inside
        nested loops; top-level blocks are just their index."""
        idx = 0
        for path, block in self._walk(self.blocks, ''):
            yield idx, path, block
            idx += 1

    def _walk(self, blocks, prefix):
        for i, b in enumerate(blocks):
            here = f'{prefix}{i}'
            if b.get('type') == 'loop':
                for it in range(b['repeat']):
                    tag = f"{here}[{it + 1}/{b['repeat']}]."
                    yield from self._walk(b['blocks'], tag)
            else:
                yield here, b

    def planned_cycles(self):
        return sum(b['n_cycles'] for _, _, b in self.flatten()
                   if b['type'] == 'cycle')


def resolve(recipe, specimen_row, cap_kv, c_est_nf=None):
    """Recipe + registry facts -> the resolved run dict (hashed, stored,
    compared on resume). Raises RecipeError with everything wrong."""
    probs = []
    v_pk = _resolve_vpk(recipe, specimen_row, probs)
    v_min = recipe.v_min_kv or 0.0
    if v_pk is not None and v_min >= v_pk:
        probs.append(f"drive.v_min_kv {v_min:g} must be < resolved "
                     f"v_pk {v_pk:g} kV")

    cap = float(cap_kv)
    trek = sldea_profile.TREK_MAX_KV
    if cap > trek:
        probs.append(f"specimen cap {cap:g} kV exceeds the Trek max "
                     f"{trek:g} kV -- fix admin_caps.json")

    resolved_blocks = []
    reachable = []
    if v_pk is not None:
        reachable.append(('drive.v_pk', v_pk))
    reachable.append(('reference.ref_kv',
                      float(recipe.reference['ref_kv'])))
    reachable.append(('reference.leak_hold_kv',
                      float(recipe.reference['leak_hold_kv'])))
    for idx, path, b in recipe.flatten():
        rb = dict(b, loop_path=path)
        if b['type'] == 'cycle':
            rb['waveform'] = b.get('waveform', recipe.waveform)
            rb['freq_hz'] = float(b.get('freq_hz', recipe.freq_hz))
            rb['v_pk_kv'] = float(b['v_pk_kv']) if 'v_pk_kv' in b \
                else v_pk
            rb['v_min_kv'] = float(b.get('v_min_kv', v_min))
            if rb['v_pk_kv'] is not None:
                reachable.append((f'block {path} v_pk', rb['v_pk_kv']))
        elif b['type'] == 'full_interlude' and isinstance(
                b.get('staircase'), dict):
            st = dict(b['staircase'])
            end = st.get('end_kv')
            if isinstance(end, dict):
                if v_pk is None:
                    probs.append(f'block {path}: end_kv fraction_of_vpk '
                                 f'needs a resolvable drive v_pk')
                else:
                    st['end_kv'] = round(
                        end['fraction_of_vpk'] * v_pk, 4)
            if isinstance(st.get('end_kv'), (int, float)):
                reachable.append((f'block {path} staircase end',
                                  float(st['end_kv'])))
            rb['staircase'] = st
            _dry_construct_staircase(st, path, probs)
        elif b['type'] in ('dc_hold', 'ramp'):
            key = 'kv' if b['type'] == 'dc_hold' else 'to_kv'
            reachable.append((f'block {path} {key}', float(b[key])))
        resolved_blocks.append(rb)

    for label, kv in reachable:
        if kv is None:
            continue
        if kv > cap + 1e-9:
            probs.append(f"{label} = {kv:g} kV exceeds the specimen hard "
                         f"cap {cap:g} kV (admin_caps.json)")
        if kv > trek + 1e-9:
            probs.append(f"{label} = {kv:g} kV exceeds the Trek max "
                         f"{trek:g} kV")

    feas = None
    if v_pk is not None:
        feas = _feasibility.check_drive(recipe.freq_hz, c_est_nf, v_pk,
                                        v_min)
        if feas['verdict'] == 'refuse':
            probs.extend('feasibility: ' + m for m in feas['msgs'])

    if probs:
        raise RecipeError(probs)

    resolved = {
        'schema': SCHEMA,
        'name': recipe.name,
        'description': recipe.description,
        'geometry': recipe.geometry,
        'specimen_id': specimen_row['specimen_id'],
        'cap_kv': cap,
        'drive': {'waveform': recipe.waveform, 'freq_hz': recipe.freq_hz,
                  'v_pk_kv': v_pk, 'v_min_kv': v_min,
                  'v_pk_spec': recipe.v_pk_spec},
        'reference': dict(recipe.reference),
        'environment': dict(recipe.environment),
        'counting': recipe.counting,
        'camera_track_max_hz': recipe.camera_track_max_hz,
        'stop': {'max_cycles': recipe.max_cycles,
                 'max_wall_h': recipe.max_wall_h},
        'failure_rules': _failure.merge_rules(
            recipe.failure_rules_overrides),
        'feasibility': feas,
        'blocks': resolved_blocks,
        'planned_cycles': recipe.planned_cycles(),
    }
    resolved['sha256'] = recipe_hash(resolved)
    return resolved


def recipe_hash(resolved):
    """Stable hash of the resolved recipe (minus the hash itself)."""
    d = {k: v for k, v in resolved.items() if k != 'sha256'}
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


def _resolve_vpk(recipe, specimen_row, probs):
    spec = recipe.v_pk_spec
    if 'kv' in spec:
        return spec['kv']
    if 'fraction_of_breakdown' not in spec:
        return None
    ebd = (specimen_row.get('ebd_ref_v_per_um') or '').strip()
    thick = (specimen_row.get('thickness_um_layer') or '').strip()
    ref_t = (specimen_row.get('ebd_ref_temp_c') or '').strip()
    if not ebd or not thick:
        probs.append(
            "drive.v_pk uses fraction_of_breakdown but the specimen's "
            "registry row lacks ebd_ref_v_per_um and/or "
            "thickness_um_layer -- record them (from the breakdown "
            "campaign) or give an explicit {'kv': X}")
        return None
    try:
        v_bd_kv = float(ebd) * float(thick) / 1000.0
    except ValueError:
        probs.append('specimen ebd_ref_v_per_um / thickness_um_layer are '
                     'not numbers')
        return None
    # The registry stores ONE breakdown reference at ONE temperature;
    # there is no scaling model here on purpose. A recipe asking for a
    # different temperature must bring its own kv.
    if ref_t:
        try:
            if abs(float(ref_t) - spec['temp_c']) > 5.0:
                probs.append(
                    f"drive.v_pk.temp_c {spec['temp_c']:g} C is more than "
                    f"5 C from the specimen's breakdown reference "
                    f"({float(ref_t):g} C) -- no temperature scaling "
                    f"model exists; measure E_BD at temperature or give "
                    f"an explicit kv")
        except ValueError:
            pass
    return round(spec['fraction_of_breakdown'] * v_bd_kv, 4)


def _dry_construct_staircase(st, path, probs):
    """Actually construct the vendored SldeaProfile so ITS validation
    runs now, not mid-run."""
    kwargs = {k: v for k, v in st.items() if k in _STAIR_KEYS
              and isinstance(v, (int, float))}
    if 'n_steps' in kwargs:
        kwargs['n_steps'] = int(kwargs['n_steps'])
        kwargs.setdefault('step_kv', None)
    try:
        sldea_profile.SldeaProfile(**kwargs)
    except (ValueError, TypeError) as e:
        probs.append(f'block {path}: staircase rejected by SldeaProfile '
                     f'({e})')
