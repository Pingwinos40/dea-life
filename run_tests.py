#!/usr/bin/env python3
"""Run every headless test suite in tests/ (no instruments needed).

Usage: python run_tests.py
Exit code 0 only if every suite passes. Hardware-in-the-loop scripts
live in bench/ and are NOT run here -- see BENCH_TEST.md.

Adapted from the Digital Multitool runner (commit bc4f9f0): identical
evidence-trail behavior, plus one difference -- vendored suites import
flat module names (`import sldea_profile`) that live in lib/ here, so
each child process gets PYTHONPATH = repo root + lib/. New-code suites
import packages from the root (`from core import recipe`).

A failing suite's output is EVIDENCE and is kept twice (`#280` upstream):
replayed after the summary, and written verbatim to
test_failures/<suite>.log. The summary block is what people quote and
grep, so nothing new is ever added inside it; everything lands after it
or in a file.

Console output is forced to ASCII (backslash-escaping anything else) --
suite output can carry emoji, and a Windows console that cannot encode
them would kill the runner mid-report, losing the very traceback this
exists to keep. The .log files are UTF-8 and hold the real characters.
"""
import glob
import locale
import os
import subprocess
import sys

# Per-run failure dumps. Overwritten every run, named in the summary
# footer, and .gitignore'd -- this is a test artifact, never a commit.
FAIL_DIRNAME = 'test_failures'

# Documented environmental failures: {suite: (platform prefix, reason)}.
# A listed suite failing ON THAT PLATFORM reports as 'env ' and does not
# fail the run (evidence still dumped); anywhere else it is a real FAIL.
# Mirrors the upstream repo's documented 34/38 Windows baseline instead
# of leaving the runner permanently red on dev machines.
KNOWN_ENV_FAILURES = {
    'test_presets_path.py': ('win32',
        "share-death simulated with chmod 0o555, which Windows ignores "
        "on directories; passes on the Linux bench"),
}


def _ascii(text):
    """`text` rendered so any console can print it."""
    return text.encode('ascii', 'backslashreplace').decode('ascii')


def _say(line=''):
    """print() that cannot raise UnicodeEncodeError."""
    sys.stdout.write(_ascii(line) + '\n')


def _decode(raw):
    """Suite bytes -> str, never raising.

    Captured as bytes rather than text=True on purpose: the child picks its
    own stdout encoding, and a strict decode here would turn a suite that
    merely printed an emoji into a crash of the runner itself.
    """
    if not raw:
        return ''
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors='replace')


def _clear_fail_dir(fail_dir):
    """Drop the previous run's dumps so nothing stale is ever read."""
    try:
        for old in glob.glob(os.path.join(fail_dir, '*.log')):
            os.remove(old)
    except OSError:
        pass


def _write_dump(fail_dir, name, returncode, out, err):
    """Write one suite's full output; return its path, or None if it could
    not be written (a read-only checkout must not cost us the replay)."""
    path = os.path.join(fail_dir, os.path.splitext(name)[0] + '.log')
    body = (f"suite:     {name}\n"
            f"command:   {sys.executable} tests/{name}\n"
            f"exit code: {returncode}\n"
            f"\n--- stdout ---\n{out}"
            f"\n--- stderr ---\n{err}")
    try:
        os.makedirs(fail_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8', errors='replace') as fh:
            fh.write(body)
    except OSError:
        return None
    return path


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    suites = sorted(glob.glob(os.path.join(root, 'tests', 'test_*.py')))
    fail_dir = os.path.join(root, FAIL_DIRNAME)
    _clear_fail_dir(fail_dir)

    # Vendored suites import flat names from lib/; new suites import
    # core/hal/vision/analysis packages from the root.
    env = dict(os.environ)
    lib_dir = os.path.join(root, 'lib')
    extra = os.pathsep.join([root, lib_dir])
    prev = env.get('PYTHONPATH')
    env['PYTHONPATH'] = extra + ((os.pathsep + prev) if prev else '')

    failed = []
    for path in suites:
        name = os.path.basename(path)
        result = subprocess.run([sys.executable, path], cwd=root,
                                capture_output=True, env=env)
        out = _decode(result.stdout)
        err = _decode(result.stderr)
        lines = out.strip().splitlines()
        tail = lines[-1] if lines else '(no output)'
        if result.returncode == 0:
            _say(f"ok   {name:32s} {tail}")
        elif (name in KNOWN_ENV_FAILURES
              and sys.platform.startswith(KNOWN_ENV_FAILURES[name][0])):
            # Documented environmental failure on this platform: visible,
            # evidence kept, but not a red run.
            _write_dump(fail_dir, name, result.returncode, out, err)
            _say(f"env  {name:32s} known env failure here: "
                 f"{KNOWN_ENV_FAILURES[name][1]}")
        else:
            # The dump goes AFTER the summary, never between these lines.
            _say(f"FAIL {name}")
            failed.append((name, result.returncode, out, err))
    _say(f"\n{len(suites) - len(failed)}/{len(suites)} suites passed")

    # ---- everything below is the evidence trail, outside the summary
    if failed:
        dumps = [(name, rc, out, err,
                  _write_dump(fail_dir, name, rc, out, err))
                 for name, rc, out, err in failed]
        _say()
        _say(f"failure output for {len(dumps)} suite(s) -- "
             f"full text in {FAIL_DIRNAME}{os.sep} (UTF-8, rewritten each run):")
        for name, _rc, _out, _err, dump in dumps:
            where = os.path.relpath(dump, root) if dump else '(could not write)'
            _say(f"  {name:32s} {where}")
        for name, rc, out, err, _dump in dumps:
            _say()
            _say(f"===== FAIL {name} (exit {rc}) =====")
            _say("--- stdout ---")
            _say(out.rstrip('\n') if out.strip() else '(empty)')
            _say("--- stderr ---")
            _say(err.rstrip('\n') if err.strip() else '(empty)')
            _say(f"===== end {name} =====")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
