# Vendored modules — provenance and deltas

TL;DR: every file in `lib/` is copied byte-for-byte from the Digital
Multitool repo at one pinned commit, except one documented delta in
`presets_path.py`. To re-vendor, diff against that commit — a clean diff
means a clean upgrade.

## Source

- Repository: Digital-Multitool (local checkout:
  `C:\Users\Anatol Gogoj\Desktop\Digital Multitool\Digital-Multitool`)
- Commit: `bc4f9f005f409d3202df2e2829dcdc9940e42267`
  (2026-08-09, "Merge pull request #295 … claude/handoff-vm-move",
  tree clean at copy time)
- Copied: 2026-08-23

## Files

| file | role here | deltas |
|---|---|---|
| sldea_profile.py | SldeaProfile staircase, BreakdownWatchdog, TelemetryLog, monitor checks, exposure_verdict — consumed as-is by core/ | none |
| instruments.py | BK4055B / TekMSO24 / BK894 / BK9174B / BK5493C SCPI drivers | none |
| webcam.py | DFK 37BUX250 capture (one-shot + V4L2 Bayer stream), control locking, focus_score | none |
| sldea_edge.py | planar top-down disc edge detection (headless functions consumed by vision/planar.py) | none |
| sldea_plot.py | plotting engine; figexport contract source (PNG + tidy CSV + figspec.json) | none |
| scope_trace.py | min-max decimation for trace freezing | none |
| presets_path.py | share-with-local-fallback path resolver | **appname `scpi_control` → `dea_life`** in LOCAL_FALLBACK / LOCAL_FALLBACK2 (keeps this app's local mirrors separate; was `sldea_lifecycle` until the 2026-09-29 DEA-LIFE rename) |
| waveform_render.py | drive waveform preview rendering | none |
| ui_widgets.py | Tooltip / ScrollableTab / SplashScreen (GUI layer) | none |
| tk_fontfix.py | must import before tkinter (emoji/X crash guard) | none |

Also vendored (pulled in by imports/tests, all pure): `lcr_format.py`
(format_si, imported by scope_trace), `siggen_presets.py` and
`bench_profiles.py` (preset stores, exercised by test_presets_path),
and `fonts.conf` (upstream `deploy/fonts.conf`, placed at `lib/`
because `tk_fontfix._candidates()` searches relative to its own file).

Vendored test suites (byte-identical) live in `tests/`:
test_sldea_profile.py, test_sldea_edge.py, test_presets_path.py,
test_scope_trace.py, test_waveform_render.py, test_ui_widgets.py,
test_webcam.py, test_webcam_bayer.py. They import flat names
(`import sldea_profile`); `run_tests.py` supplies `lib/` on PYTHONPATH
so they run unmodified.

NOT copied: `test_sldea_calibration.py` and `test_sldea_plot.py` — both
import the un-vendored GUI monoliths (`sldea_edge_gui`, `sldea_plot_gui`)
and stay upstream-only. `test_presets_path.py` is a documented
environmental failure on Windows (chmod cannot make dirs read-only);
`run_tests.py` KNOWN_ENV_FAILURES reports it as `env ` there and it must
pass on the Linux bench.

## Desired upstream additions (not patched here)

- `instruments.BK4055B`: burst start-phase support (`BTWV STPS,<deg>`).
  `_BTWV_NUMERIC` lacks STPS and `set_burst` never writes it; this app
  issues a raw `C<ch>:BTWV STPS,270` from `hal/drive_bk4055b.py` so a
  unipolar burst idles at its minimum (0 kV) instead of the offset
  midpoint. See the plan's "Cycle accounting" traps and
  `bench/probe_burst_idle.py`.

## Licensing

The upstream Digital-Multitool repository carries no license file
(all-rights-reserved by default). The copies in this directory are
published under this repository's GPLv3 (see /LICENSE) by their
copyright holder, who owns both projects. Re-vendoring from upstream
does not change that grant.

## Rules

- Never edit a `lib/` file without recording the delta in the table above.
- Prefer re-homing logic into `core/` / `hal/` over patching `lib/`.
- Re-vendoring: copy from a newer upstream commit, re-apply the
  presets_path appname delta, update the commit hash here, rerun
  `python run_tests.py`.
