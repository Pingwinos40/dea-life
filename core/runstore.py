"""Run-folder writer: the on-disk shape of one lifecycle run.

    <data_root>/<specimen_id>/LC_YYYYmmdd_HHMMSS/
        setup.txt         human lab-notebook header (Key: value)
        recipe.json       the RESOLVED recipe + sha256
        blocks.csv        one row per executed block
        interludes.csv    one row per characterization interlude
        events.csv        the audit log (rules, flags, env, confirms)
        telemetry.csv     vendored TelemetryLog sidecar (DC phases)
        run.log           prelog-buffered, then append-through
        frames/           camera frames
        traces/           event freezes (ring dumps, scope waveforms)
        interludes/full_NN/   legacy-format SLDEA sub-runs (vendored
                              sldea_edge / sldea_plot run on these
                              unmodified)
        checkpoint.json   core/checkpoint.py
        status.json / status.html   core/status.py

CSVs are utf-8-sig (Excel-safe, the sldea_edge write-back convention),
append-only, flushed per row. On resume they reopen in append mode
(header written only when absent). Data of record = the CSVs.

Refuses a data_root on a OneDrive-synced path (the DEA board repo's
hard-learned rule: sync clients corrupt live run folders) and probes
writability via the vendored presets_path.
"""
import csv
import os

import presets_path  # vendored

BLOCKS_COLUMNS = [
    'block_idx', 'loop_path', 'type', 'role', 't_start_iso', 't_end_iso',
    'wall_s', 'cycles_planned', 'cycles_done', 'counting', 'cycles_total',
    'actuated_s_block', 'actuated_s_total', 'waveform', 'freq_cmd_hz',
    'freq_meas_hz', 'vpk_cmd_kv', 'vmin_cmd_kv', 'vmean_meas_kv',
    'vpk_meas_kv', 'vvalley_meas_kv', 'imean_ua', 'ipk_ua', 'ivalley_ua',
    'defl_metric', 'defl_pk', 'defl_valley', 'env_t_c', 'env_p_mbar',
    'env_age_s', 'soft_flags', 'verdict', 'notes',
]
INTERLUDES_COLUMNS = [
    'interlude_idx', 'kind', 'cycles_total', 'actuated_s_total',
    'wall_s_total', 't_iso', 'ref_kv', 'disp_metric', 'disp_value',
    'disp_baseline', 'disp_ratio', 'leak_ua', 'leak_baseline_ua',
    'leak_ratio', 'c_est_nf', 'c_quality', 'c_baseline_nf', 'c_ratio',
    'zero_value', 'zero_drift', 'wrinkle_idx', 'focus', 'frames',
    'subrun_dir', 'is_baseline', 'verdict', 'flags', 'notes',
]
EVENTS_COLUMNS = [
    'event_idx', 't_iso', 'wall_s', 'cycles_total', 'actuated_s_total',
    'tier', 'rule_id', 'action', 'value', 'threshold', 'message',
    'trace_file', 'frame_file', 'operator',
]


class RunStoreError(Exception):
    pass


def default_data_root():
    return os.path.join(os.path.expanduser('~'), 'dea-life-runs')


def check_data_root(data_root):
    """Refuse OneDrive paths; probe writability. Returns the root."""
    root = data_root or default_data_root()
    if 'onedrive' in root.lower():
        raise RunStoreError(
            f"data_root '{root}' is under OneDrive -- sync clients "
            f"corrupt live run folders (DEA-board rule: never write "
            f"runs to a synced path). Point data_root elsewhere.")
    got = presets_path.writable_path(root, is_dir=True)
    if got != root:
        raise RunStoreError(
            f"data_root '{root}' is not writable "
            f"({presets_path.fallback_note()}) -- a lifecycle run must "
            f"not silently land in a fallback dir; fix the path.")
    return root


def run_dirname(dt):
    return dt.strftime('LC_%Y%m%d_%H%M%S')


class _Csv:
    """Append-only CSV with flush-per-row; reopen-safe for resume."""

    def __init__(self, path, columns):
        self.path = path
        self.columns = columns
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        # utf-8-sig on create; plain append after (the BOM exists once).
        self._f = open(path, 'a', newline='',
                       encoding='utf-8-sig' if new else 'utf-8')
        self._w = csv.DictWriter(self._f, fieldnames=columns,
                                 extrasaction='ignore')
        if new:
            self._w.writeheader()
            self._f.flush()
        self.rows = 0

    def write(self, row):
        clean = {k: ('' if row.get(k) is None else row.get(k, ''))
                 for k in self.columns}
        self._w.writerow(clean)
        self._f.flush()
        self.rows += 1

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


class RunLog:
    """run.log with the prelog-buffer semantics (gui.py:3722 lineage):
    lines logged before the run dir exists are buffered, then flushed
    into the file and appended-through after. Also mirrors to a callable
    sink (CLI print / GUI queue)."""

    def __init__(self, sink=None, clock=None):
        self._sink = sink or (lambda line: None)
        self._clock = clock
        self._prelog = []
        self._path = None

    def log(self, msg):
        stamp = self._clock.now_iso(timespec='seconds') if self._clock \
            else ''
        line = f'[{stamp}] {msg}\n' if stamp else f'{msg}\n'
        self._sink(msg)
        if self._path is None:
            self._prelog.append(line)
            return
        try:
            with open(self._path, 'a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception:
            pass                     # the log must never kill the run

    def attach(self, path):
        """Flush the prelog into `path` and switch to append-through.
        Flush AND swap together so no line lands in a dropped buffer."""
        try:
            with open(path, 'a', encoding='utf-8') as fh:
                fh.writelines(self._prelog)
        except Exception:
            pass
        self._path = path
        self._prelog = []


class RunStore:
    """One run folder's writers. Create fresh or reopen for resume."""

    def __init__(self, run_dir, resume=False):
        self.run_dir = run_dir
        if not resume and os.path.exists(
                os.path.join(run_dir, 'blocks.csv')):
            raise RunStoreError(f'{run_dir} already holds a run '
                                f'(use resume)')
        self.frames_dir = os.path.join(run_dir, 'frames')
        self.traces_dir = os.path.join(run_dir, 'traces')
        self.interludes_dir = os.path.join(run_dir, 'interludes')
        for d in (run_dir, self.frames_dir, self.traces_dir,
                  self.interludes_dir):
            os.makedirs(d, exist_ok=True)
        self.blocks = _Csv(os.path.join(run_dir, 'blocks.csv'),
                           BLOCKS_COLUMNS)
        self.interludes = _Csv(os.path.join(run_dir, 'interludes.csv'),
                               INTERLUDES_COLUMNS)
        self.events = _Csv(os.path.join(run_dir, 'events.csv'),
                           EVENTS_COLUMNS)
        self._event_idx = 0

    def write_setup(self, lines):
        """setup.txt, written FIRST (before anything slow) so even an
        interrupted run leaves its metadata. Append-mode on resume."""
        path = os.path.join(self.run_dir, 'setup.txt')
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')

    def write_recipe(self, resolved, _json=None):
        import json
        path = os.path.join(self.run_dir, 'recipe.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(resolved, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def subrun_dir(self, n):
        """interludes/full_NN/ -- a legacy-format SLDEA sub-run dir."""
        d = os.path.join(self.interludes_dir, f'full_{n:02d}')
        os.makedirs(os.path.join(d, 'frames'), exist_ok=True)
        return d

    def event(self, t_iso, wall_s, ledger, tier, rule_id, action,
              value='', threshold='', message='', trace_file='',
              frame_file='', operator=''):
        self._event_idx += 1
        self.events.write({
            'event_idx': self._event_idx, 't_iso': t_iso,
            'wall_s': round(wall_s, 1), 'cycles_total': ledger.cycles,
            'actuated_s_total': round(ledger.actuated_s, 1),
            'tier': tier, 'rule_id': rule_id, 'action': action,
            'value': value, 'threshold': threshold, 'message': message,
            'trace_file': trace_file, 'frame_file': frame_file,
            'operator': operator,
        })
        return self._event_idx

    def set_event_idx(self, idx):
        """Resume: continue event numbering from the checkpointed row
        count instead of restarting at 1."""
        self._event_idx = int(idx)

    def close(self):
        for w in (self.blocks, self.interludes, self.events):
            w.close()
