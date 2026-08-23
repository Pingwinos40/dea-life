#!/usr/bin/env python3
"""
Signal-generator preset persistence (pure logic, no Tk, no instrument I/O).

A *preset* is a named snapshot of both channels' state. The reusable unit is a
ChannelState dict:

    {"waveform": "SINE", "freq_hz": 1000.0, "amp_vpp": 1.0, "offset_v": 0.0,
     "output": False, "load": "HZ", "polarity": "NOR"}

Presets are stored in a single JSON file (default presets/siggen_presets.json):

    {"version": 1,
     "presets": {
        "<name>": {"name": "<name>", "saved_utc": "...",
                   "channels": {"1": <ChannelState>, "2": <ChannelState>}}}}

The single-file layout keeps enumeration trivial (one read populates a dropdown)
and avoids preset-name -> filename sanitisation. The top-level ``version`` field
leaves room for the sequence/arb formats that later PRs add to this module.
"""
import os
import re
import csv
import json
from datetime import datetime, timezone

from instruments import BK4055B
import presets_path

SCHEMA_VERSION = 1
DEFAULT_PATH = 'presets/siggen_presets.json'
DEFAULT_ARB_DIR = 'presets/arb'

# --------------------------------------------------------------------------
# Arbitrary-waveform CSV format
# --------------------------------------------------------------------------
# A waveform CSV has a header row and one of two layouts:
#   value             one column of sample values (one period, in order)
#   time,value        time column is accepted but ignored; rows are taken
#                     in file order (export tools often include it)
# Values are floats; anything in [-1, 1] uploads as-is, larger magnitudes
# are normalised to full scale at upload. Blank lines are skipped.

ARB_CSV_TEMPLATE_ROWS = 32  # one sine period in the template file


def sanitize_arb_name(name):
    """Instrument-safe arb name: [A-Za-z0-9_], max 16 chars."""
    clean = re.sub(r'[^A-Za-z0-9_]', '_', str(name).strip())[:16]
    if not clean:
        raise ValueError(f"unusable arb name {name!r}")
    return clean


def arb_from_csv(path):
    """Read waveform samples from a CSV file. Returns a list of floats.

    Accepts a 'value' column alone or 'time,value' (time ignored). Raises
    ValueError with a row reference on malformed content.
    """
    with open(path, newline='') as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{path}: empty file")
        cols = [c.strip().lower() for c in header]
        if 'value' not in cols:
            raise ValueError(
                f"{path}: header must contain a 'value' column "
                f"(got {header!r}); accepted layouts: 'value' or 'time,value'")
        vcol = cols.index('value')
        samples = []
        for lineno, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue  # skip blank lines
            try:
                samples.append(float(row[vcol]))
            except (IndexError, ValueError):
                raise ValueError(f"{path}: bad value on line {lineno}: {row!r}")
    if not samples:
        raise ValueError(f"{path}: no sample rows")
    return samples


def write_arb_template(path):
    """Write a template CSV (header + one sine period) a user can edit."""
    import math
    n = ARB_CSV_TEMPLATE_ROWS
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['value'])
        for i in range(n):
            writer.writerow([f'{math.sin(2 * math.pi * i / n):.6f}'])

# Canonical waveform list lives on the driver; mirror it here so validation has
# a single source of truth.
VALID_WAVEFORMS = BK4055B.WAVEFORMS

_CHANNEL_DEFAULTS = {
    'waveform': 'SINE',
    'freq_hz': 1000.0,
    'amp_vpp': 1.0,
    'offset_v': 0.0,
    'output': False,
    'load': 'HZ',
    'polarity': 'NOR',
}

# Waveform-specific parameters; stored only when present (no defaults).
# duty_pct: SQUARE/PULSE; sym_pct: RAMP; phase_deg: SINE/SQUARE/RAMP/ARB;
# rise_s/fall_s/delay_s: PULSE.
_OPTIONAL_NUMERIC = ('duty_pct', 'sym_pct', 'phase_deg',
                     'rise_s', 'fall_s', 'delay_s')


def validate_channel_state(state):
    """Return a normalised copy of a ChannelState, or raise ValueError.

    Missing keys fall back to defaults; numerics are coerced to float; the
    waveform must be one of VALID_WAVEFORMS and polarity one of NOR/INVT.
    Waveform-specific keys (_OPTIONAL_NUMERIC) are kept only when present.
    """
    if not isinstance(state, dict):
        raise ValueError(f"channel state must be a dict, got {type(state).__name__}")
    out = dict(_CHANNEL_DEFAULTS)
    out.update({k: state[k] for k in _CHANNEL_DEFAULTS if k in state})

    for key in _OPTIONAL_NUMERIC:
        if key in state and state[key] is not None:
            try:
                out[key] = float(state[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number, got {state[key]!r}")

    # ARB waveform reference (name in the arb library / instrument memory)
    if state.get('arb_name'):
        out['arb_name'] = sanitize_arb_name(state['arb_name'])

    out['waveform'] = str(out['waveform']).upper()
    if out['waveform'] not in VALID_WAVEFORMS:
        raise ValueError(
            f"waveform {out['waveform']!r} must be one of {VALID_WAVEFORMS}")

    for key in ('freq_hz', 'amp_vpp', 'offset_v'):
        try:
            out[key] = float(out[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number, got {out[key]!r}")

    out['output'] = bool(out['output'])
    out['load'] = str(out['load'])
    out['polarity'] = str(out['polarity']).upper()
    if out['polarity'] not in ('NOR', 'INVT'):
        raise ValueError(f"polarity {out['polarity']!r} must be NOR or INVT")
    return out


PRESET_FILE_KIND = 'scpi_sg_preset'


def write_preset_file(path, channels):
    """Write ONE preset (a {1|2 -> ChannelState} mapping) to a JSON file the
    user chose via a browse dialog. Channel states are validated; the write
    is atomic (tmp + os.replace). Returns the path."""
    normalised = {str(ch): validate_channel_state(st)
                  for ch, st in channels.items()}
    data = {
        'kind': PRESET_FILE_KIND,
        'version': SCHEMA_VERSION,
        'saved_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'channels': normalised,
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path


def read_preset_file(path):
    """Read a browsed preset file -> validated {'1'|'2' -> ChannelState}.

    Accepts a single-preset file (this exporter) OR a full store file
    (siggen_presets.json) when it holds exactly one preset. Raises
    ValueError on anything that isn't a usable single preset."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not a preset file (expected a JSON object)")
    channels = data.get('channels')
    if channels is None and isinstance(data.get('presets'), dict):
        presets = data['presets']
        if len(presets) == 1:
            channels = next(iter(presets.values())).get('channels')
        elif len(presets) > 1:
            raise ValueError(
                "this file holds several presets -- import one saved with "
                "'Save to File'")
    if not isinstance(channels, dict) or not channels:
        raise ValueError("no signal-generator channel settings in this file")
    return {str(k): validate_channel_state(v) for k, v in channels.items()}


class SignalGenPresetStore:
    """JSON-backed store of named signal-generator presets."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self._data = {'version': SCHEMA_VERSION, 'presets': {}}
        self.load()

    @property
    def _root(self):
        """Configured presets directory; the fallback mirrors its layout."""
        return os.path.dirname(self.path) or '.'

    # -- persistence -------------------------------------------------------
    def load(self):
        """(Re)load from disk. Missing file -> empty store; malformed file is
        moved aside to ``<path>.corrupt`` and an empty store is started."""
        path = presets_path.readable_path(self.path, self._root)
        if not os.path.exists(path):
            self._data = {'version': SCHEMA_VERSION, 'presets': {}}
            return self._data
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get('presets'), dict):
                raise ValueError("missing 'presets' object")
            self._data = {
                'version': data.get('version', SCHEMA_VERSION),
                'presets': data['presets'],
            }
        except (json.JSONDecodeError, ValueError, OSError):
            try:
                os.replace(path, path + '.corrupt')
            except OSError:
                pass
            self._data = {'version': SCHEMA_VERSION, 'presets': {}}
        return self._data

    def _save(self):
        """Atomically persist: write a temp file then os.replace onto path."""
        path = presets_path.writable_path(self.path, root=self._root)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, path)

    # -- queries -----------------------------------------------------------
    def names(self):
        """Sorted list of preset names (for a combobox)."""
        return sorted(self._data['presets'])

    def get(self, name):
        """Return the full preset record. Raises KeyError if absent."""
        return self._data['presets'][name]

    # -- mutations ---------------------------------------------------------
    def save(self, name, channels):
        """Create or overwrite a preset from a {1|2 -> ChannelState} mapping.

        Channel keys may be ints or strings; they are normalised to '1'/'2'.
        Each channel state is validated. Returns the stored record.
        """
        name = str(name).strip()
        if not name:
            raise ValueError("preset name must not be empty")
        normalised = {}
        for ch, state in channels.items():
            normalised[str(ch)] = validate_channel_state(state)
        record = {
            'name': name,
            'saved_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'channels': normalised,
        }
        self._data['presets'][name] = record
        self._save()
        return record

    def delete(self, name):
        """Remove a preset. Returns True if it existed, False otherwise."""
        if name in self._data['presets']:
            del self._data['presets'][name]
            self._save()
            return True
        return False

    # -- arbitrary-waveform library (CSV files under arb_dir) ---------------
    # The library is just the directory listing: presets/arb/<name>.csv.
    # No index file to corrupt; the preset JSON references arbs by name.

    @property
    def arb_dir(self):
        return getattr(self, '_arb_dir', DEFAULT_ARB_DIR)

    @arb_dir.setter
    def arb_dir(self, path):
        self._arb_dir = path

    def _arb_path(self, name, directory=None):
        d = self.arb_dir if directory is None else directory
        return os.path.join(d, f'{sanitize_arb_name(name)}.csv')

    def _arb_recipe_path(self, name, directory=None):
        d = self.arb_dir if directory is None else directory
        return os.path.join(d, f'{sanitize_arb_name(name)}.recipe.json')

    def arb_names(self):
        """Sorted names of saved arb waveforms."""
        directory = presets_path.listable_dir(self.arb_dir, self._root)
        if not os.path.isdir(directory):
            return []
        return sorted(os.path.splitext(f)[0] for f in os.listdir(directory)
                      if f.endswith('.csv'))

    def save_arb(self, name, samples, recipe=None):
        """Save samples as <arb_dir>/<name>.csv. Returns the sanitised name.

        If ``recipe`` (a JSON-serialisable breakpoint/segment dict) is given, it
        is written alongside as <name>.recipe.json so the arb can be reopened in
        the editor and re-edited. The CSV remains the upload source of truth.
        """
        if not samples:
            raise ValueError("samples must be non-empty")
        clean = sanitize_arb_name(name)
        directory = presets_path.writable_path(self.arb_dir, is_dir=True,
                                               root=self._root)
        os.makedirs(directory, exist_ok=True)
        tmp = self._arb_path(clean, directory) + '.tmp'
        with open(tmp, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['value'])
            for s in samples:
                writer.writerow([f'{float(s):.6g}'])
        os.replace(tmp, self._arb_path(clean, directory))

        recipe_path = self._arb_recipe_path(clean, directory)
        if recipe is not None:
            rtmp = recipe_path + '.tmp'
            with open(rtmp, 'w') as f:
                json.dump(recipe, f, indent=2)
            os.replace(rtmp, recipe_path)
        elif os.path.exists(recipe_path):
            os.remove(recipe_path)  # stale recipe would no longer match the CSV
        return clean

    def load_arb(self, name):
        """Load samples for a saved arb. Raises FileNotFoundError/ValueError."""
        return arb_from_csv(
            presets_path.readable_path(self._arb_path(name), self._root))

    def load_arb_recipe(self, name):
        """Return the saved editor recipe for an arb, or None if there isn't one."""
        path = presets_path.readable_path(self._arb_recipe_path(name),
                                          self._root)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def delete_arb(self, name):
        """Delete a saved arb (and its recipe sidecar). Returns True if it existed."""
        path = presets_path.readable_path(self._arb_path(name), self._root)
        existed = os.path.exists(path)
        if existed:
            os.remove(path)
        recipe_path = presets_path.readable_path(
            self._arb_recipe_path(name), self._root)
        if os.path.exists(recipe_path):
            os.remove(recipe_path)
        return existed
