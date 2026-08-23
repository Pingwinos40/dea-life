# RECIPES.md — the test-definition reference

TL;DR: a recipe is a JSON file in `recipes/`: a drive definition + an
ordered list of blocks. Unknown keys are HARD ERRORS (a typo'd
threshold must never silently become a default). Validation happens
twice: statically at load, and resolved against the specimen registry
(caps, breakdown reference, feasibility) before anything arms.

## Top level

```json
{
  "schema": "sldea-lifecycle/1",
  "name": "...", "description": "...",
  "geometry": "planar | bender",
  "drive": {"waveform": "SINE|SQUARE|RAMP", "freq_hz": 5.0,
            "v_pk": {"kv": 1.75}
                    OR {"fraction_of_breakdown": 0.5, "temp_c": 23},
            "v_min_kv": 0.0},
  "reference": {"ref_kv": 1.0, "ref_freq_hz": 0.1, "ref_cycles": 3,
                "leak_hold_kv": 1.0, "leak_hold_s": 20.0},
  "environment": {"t_c_band": [18, 28], "p_mbar_band": [900, 1100],
                  "stale_after_s": 1800, "reattest_on_rearm": true},
  "counting": "burst | timed",
  "camera_track_max_hz": 0.5,
  "stop": {"max_cycles": 1000000, "max_wall_h": 168},
  "failure_rules": { ...overrides, merged by rule id... },
  "blocks": [ ... ]
}
```

- `fraction_of_breakdown` resolves against the specimen registry's
  `ebd_ref_v_per_um × thickness_um_layer`; the recipe temp_c must be
  within 5 °C of `ebd_ref_temp_c` — there is deliberately NO
  temperature scaling model (measure E_BD at temperature, or give an
  explicit kv).
- v1 frequency ceiling: 10 Hz (user decision 2026-08-23 — the camera
  tracks motion directly; no strobe hardware).
- The `reference` condition is FIXED and cycling-independent so trends
  compare across specimens and conditions (the NERD-pattern rule).

## Blocks

| type | fields | what it does |
|---|---|---|
| `cycle` | `n_cycles`, `role` ("" or `break_in`), optional per-block `waveform/freq_hz/v_pk_kv/v_min_kv` | counted-burst cycling in 1000-cycle chunks with idle + FREQUENCY verification |
| `fast_interlude` | optional overrides of `reference` | rest frame → C up-ramp → peak frame (vision) → DC leakage hold → C down-ramp → ratios vs baseline → SLOW rules |
| `full_interlude` | `staircase` (SldeaProfile kwargs; `end_kv` may be `{"fraction_of_vpk": 0.8}`), `set_baseline` | a LEGACY-format staircase sub-run under `interludes/full_NN/` (Edge Review / sldea_plot compatible) + the fast-interlude measurement set |
| `dc_hold` | `kv`, `hold_s`, `sample_hz` | monitored DC hold (accelerated-stress segments) |
| `ramp` | `to_kv`, `rate_kv_s` | host-sequenced ramp |
| `wait_env` | `prompt`, optional band overrides | HV to 0; waits for a fresh, in-band environment attestation |
| `loop` | `blocks`, `repeat` (≤2 deep) | deterministic repetition; the stop caps + failure engine end runs early |

Structural rules (refusals): exactly ONE `set_baseline` full_interlude,
placed after a top-level `role: "break_in"` cycle block — every
ratio-based failure criterion anchors on the post-break-in state.
Staircase parameters are pre-checked against the vendored SldeaProfile
constraints (`settle_s + snap_lead_s < landing_s`) at load, never
mid-run.

## Failure rules

Three tiers (see docs/SAFETY.md for the fast tier). Slow-tier rules
carry `flag_at` (the AMBER "potential failure" threshold) below the
hard `threshold`. Defaults (all overridable per recipe by id):

    amplitude    disp_ratio < 0.80 (flag 0.90)  abort
    capacitance  |C/C0 - 1| > 0.20 (flag 0.10)  abort
    leakage      leak_ratio > 100x (flag 10x)   abort
    zero_drift   > 0.10 (flag 0.05)             warn
    wrinkle      wrinkle_idx > 2.0 (flag 1.5)   flag

Ratio criteria are the default on purpose — absolute thresholds are
meaningless without area/thickness/T/RH context. The FIRST hard trip is
the run's official failure mode; everything after keeps logging (the
precursor ordering is itself the scientific result).

## Shipped templates

- `life_dea_planar_v1.json` — the flagship **LIFE-DEA** protocol
  (**L**ifecycle **I**nterrogation for **F**light **E**nvironments —
  DEA; stratosphere-paper lineage): 50% of median E_BD, fast interlude
  per 10³ cycles, full per 5×10⁴, 10⁶-cycle cap. A specimen reaching the
  cap is a SUSPENSION (right-censored) — the claim reads "N cycles
  without dielectric breakdown".
- `life_dea_bender_v1.json` — bender variant at 1.75 kV / 2 Hz
  (within the Trek current budget for nF-class stacks), 10⁵ cap.
- `shakedown_mock.json` — minutes-long end-to-end exercise; the test
  suite and the GUI rehearsal button run it on simulated time.

## Life-test factor calculator

Advisory, printed at validate + shown in the GUI sidebar:

- NASA-STD-5017 non-human-rated: 2×operational + 4×ground +
  4×(functional+environmental+run-in); margin is added BEFORE the
  factor (§5.7(c)).
- ECSS-E-ST-33-01C Rev.2 Table 4-4: piecewise tiers (in-orbit
  10×/4×/2×/1.25×; ground 4×/2×/1.25×, minimum 10 ground cycles),
  accumulated per bin.

## Roadmap templates (standards pack, not yet shipped)

GEVS TVAC (2–3-cycle mechanism screen, plateau pull-in-voltage checks,
failure-free-hour accumulators) · MIL-810 502.7 (72 h rubber cold
soak) + 503.7 I-C shock · ECSS corona sweep 10→0.1 hPa. The block
vocabulary above (wait_env + dc_hold + interludes + loops) is designed
to express them.
