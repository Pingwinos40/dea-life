"""Run checkpoint: atomic JSON, written at every block boundary.

Deliberately JSON and not SQLite: the lab convention is atomic
``tmp + os.replace`` everywhere, operators eyeball files on the share,
and the bench share is CIFS that DROPS OUT (presets_path.py exists
because of ``OSError 112 Host is down``) -- SQLite over a flaky CIFS
mount is a known corruption trap. One writer, <10 kB of state, and the
CSVs -- not the checkpoint -- are the data of record.

``clean_shutdown`` flips true only in FINISHING; a startup that finds it
false is the unclean-shutdown detector. Resume requires the recipe hash
to match and the RECHAR gate (re-characterization interlude passing the
SLOW rules against the stored baseline) BEFORE any HV re-arm.
"""
import json
import os

CHECKPOINT_NAME = 'checkpoint.json'
SCHEMA = 'dea-life-checkpoint/1'


def save(run_dir, state):
    """Atomic write. `state` must carry the fields in new_state()."""
    path = os.path.join(run_dir, CHECKPOINT_NAME)
    body = dict(state, schema=SCHEMA)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(body, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load(run_dir):
    """The checkpoint dict, or None when the run has none."""
    path = os.path.join(run_dir, CHECKPOINT_NAME)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as fh:
        d = json.load(fh)
    if d.get('schema') != SCHEMA:
        raise ValueError(f'unrecognized checkpoint schema '
                         f'{d.get("schema")!r} in {path}')
    return d


def new_state(run_id, specimen_id, recipe_sha256, mode):
    return {
        'run_id': run_id,
        'specimen_id': specimen_id,
        'recipe_sha256': recipe_sha256,
        'mode': mode,
        'engine_state': 'IDLE',
        'flat_idx': 0,               # next flatten() index to execute
        'cycles': 0,
        'actuated_s': 0.0,
        'wall_s': 0.0,
        'counting_mode': '',
        'baseline': None,            # set by the set_baseline interlude
        'flags': [],                 # active AMBER soft flags
        'last_env': None,
        'block_rows': 0,             # blocks.csv rows written (reconcile)
        'interlude_rows': 0,
        'clean_shutdown': False,
        'written_iso': '',
    }


def unclean(run_dir):
    """True when a checkpoint exists and does not mark a clean end."""
    d = load(run_dir)
    return bool(d) and not d.get('clean_shutdown')
