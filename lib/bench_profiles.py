#!/usr/bin/env python3
"""Named whole-bench profiles: LCR + scope + both sig-gen channels.

Presets used to exist for the signal generator only (siggen_presets.py);
everything else was retyped each session. A bench profile snapshots the
entire bench so either user can recall a colleague's full experiment in
one click (issue #47).

Profiles live in presets/bench_profiles.json -- the same relative
presets/ dir as the sig-gen presets, so on the shared-drive deployment
they are shared between users. Writes are atomic (tmp + os.replace) so
two users can't tear the file.

Headless self-test: .venv/bin/python tests/test_bench_profiles.py
"""
import json
import os

import presets_path

PROFILE_FILE_KIND = 'scpi_bench_profile'


def write_profile_file(path, profile):
    """Write ONE bench profile (a dict) to a JSON file the user browsed to.
    Atomic (tmp + os.replace). Returns the path."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be a dict")
    data = dict(profile)
    data['kind'] = PROFILE_FILE_KIND
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def read_profile_file(path):
    """Read a browsed bench-profile file -> the profile dict.

    Accepts a single-profile file (this exporter) OR a full store file
    (bench_profiles.json) when it holds exactly one profile. Raises
    ValueError on a multi-profile store or a non-profile file."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not a bench-profile file (expected a JSON object)")
    if data.get('kind') == PROFILE_FILE_KIND \
            or any(k in data for k in ('lcr', 'scope', 'siggen')):
        return data
    profiles = [v for v in data.values() if isinstance(v, dict)]
    if len(profiles) == 1:
        return profiles[0]
    if len(profiles) > 1:
        raise ValueError(
            "this file holds several profiles -- export one with 'Save "
            "Bench Profile to File'")
    raise ValueError("no bench-profile data in this file")


class BenchProfileStore:
    def __init__(self, path=None):
        self.path = path or os.path.join('presets', 'bench_profiles.json')

    def _load_all(self):
        """The whole file as a dict; missing/corrupt files read as empty
        (a broken JSON must never brick every profile operation)."""
        try:
            with open(presets_path.readable_path(
                    self.path, os.path.dirname(self.path) or '.')) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_all(self, data):
        path = presets_path.writable_path(
            self.path, root=os.path.dirname(self.path) or '.')
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)

    def names(self):
        return sorted(self._load_all())

    def save(self, name, profile):
        """Store `profile` (a plain dict) under `name`; returns the
        stripped name. Overwrites silently -- the GUI confirms first."""
        name = (name or '').strip()
        if not name:
            raise ValueError("Profile name must not be empty")
        if not isinstance(profile, dict):
            raise ValueError("Profile must be a dict")
        data = self._load_all()
        data[name] = profile
        self._save_all(data)
        return name

    def load(self, name):
        data = self._load_all()
        if name not in data:
            raise KeyError(f"No bench profile named {name!r}")
        return data[name]

    def delete(self, name):
        data = self._load_all()
        if name in data:
            del data[name]
            self._save_all(data)
