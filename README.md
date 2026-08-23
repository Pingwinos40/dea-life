# SLDEA Lifecycle Manager

TL;DR: automated lifecycle / fatigue testing station for dielectric
elastomer actuators (planar single-layer discs and cantilever benders)
against space-relevant environments. Put a specimen in the rig, pick a
template test ("cycle until failure at −40 °C"), walk away: the engine
cycles the Trek, characterizes on a schedule, AMBER-flags potential
failure, hard-trips safely on real failure, survives crashes, and writes
a standards-aligned report (censoring-correct Weibull/B10, NASA-STD-5017
/ ECSS-E-ST-33-01 life factors). Headline output format: *"10,000
actuation cycles without dielectric breakdown."*

Built on the Digital Multitool's proven measurement chain (vendored at a
pinned commit — see `lib/VENDOR.md`): BK4055B signal generator → Trek
610E-G (1 V → 1 kV) → DEA, with the Trek's monitor BNCs read on a Tek
MSO24 (CH2: 1 V = 1 kV; CH3: 1 V = 200 µA) and a DFK 37BUX250 camera.

## Mode ladder

| mode | hardware | where | use |
|---|---|---|---|
| `mock` | none — deterministic simulated rig | any OS | development, tests, GUI demos, recipe rehearsal |
| `dry`  | instruments read, **HV never commanded** | bench | timing/sequencing rehearsal on real hardware (default) |
| `live` | full drive | Linux bench only | real runs; requires the gate chain all-green + typed ENERGIZE |

Promotion up the ladder is always explicit (`--mode live`); nothing —
no preset, no recipe, no resume — ever stores an armed state.

## Quickstart (mock, any OS)

```
python -m pip install -r requirements.txt
python run_tests.py
python lifecycle_cli.py validate recipes/shakedown_mock.json
python lifecycle_cli.py run recipes/shakedown_mock.json --specimen DEMO-001 --mode mock
```

Watch a run from anywhere on the LAN: open `status.html` in the run
folder (read-only by design — stopping HV requires bench presence).

## Layout

- `lifecycle_cli.py` — validate | run | resume | status | feasibility | specimen | replay
- `core/` — recipe schema, engine state machine, executors, failure
  rules, cycle ledger, run store, checkpoint/resume, safety gates
- `hal/` — DriveSource / MonitorSource / Camera / EnvironmentSource;
  BK4055B+Trek, MSO24, DFK camera, manual environment, full mock rig
- `vision/` — planar adapter (vendored sldea_edge) + bender side-view
  pipeline + capture + replay CLI
- `analysis/` — trend reduction, Weibull (censoring-correct), report
  generator, compliance matrix, figure export
- `gui/` — Tkinter app (Setup · Pre-flight · Run · Review · Campaign)
- `recipes/` — test templates (LIFE-DEA flagship)
- `lib/` — vendored Digital Multitool modules (`lib/VENDOR.md`)
- `tests/` — headless suites (`python run_tests.py`); `bench/` —
  hardware-in-the-loop probes (see `BENCH_TEST.md`)
- `docs/` — SAFETY.md, DATA_FORMATS.md, RECIPES.md

## Safety, in one paragraph

The engine process owns HV. Its guaranteed-`finally` zeroes the signal
generator through a handle captured at arm time (dual-handle retry;
failure = terminal `NOT_ZEROED` state, loud everywhere). A breakdown
watchdog (100 µA deviation / 3 s sustained, credible-baseline gated)
aborts and discharges; drive-fidelity and monitor-loss rules pause at
0 kV. Specimen hard caps live in `admin_caps.json` (no GUI editor) and
are enforced twice: at recipe validation and clamped inside every drive
call. HV is inhibited while attested chamber pressure is inside the
Paschen band. Software zero is currently the only kill path — the Trek's
remote-TTL hardware interlock is the first roadmap upgrade. Details and
provenance: `docs/SAFETY.md`.
