# SAFETY.md — every gate and kill path, with provenance

TL;DR: the engine process owns HV. Nothing arms without the gate chain
all-green; a guaranteed `finally` zeroes the signal generator through a
handle captured at arm time; failure to zero is the terminal
`NOT_ZEROED` state, loud everywhere. **Software zero is currently the
only kill path** — the Trek's remote-TTL hardware interlock
(`DriveSource.hw_interlock()`, a no-op slot today) is the first
hardware upgrade on the roadmap. Until it exists, an unattended live
run relies on: the fail-safe MAN-trigger burst design (a dead host
leaves the SG idle), the watchdog, and the finally-block discipline.

## Mode ladder

`mock` → `dry` (default) → `live`. Promotion is explicit per invocation
(`--mode live`); presets, recipes and checkpoints never store an armed
state (vendored sldea_presets rule). `live` requires Linux
(`INSTRUMENTS_SUPPORTED` lineage, Digital Multitool gui.py:178).

## The gate chain (core/safety.py, ordered)

| # | gate | provenance |
|---|---|---|
| 1 | platform (live = Linux bench) | gui.py:178 |
| 2 | signal generator reachable | upstream sldea_run |
| 3 | scope reachable, else typed `BLIND` confirm (watchdog disabled — discouraged) | upstream askyesno default-no, hardened to typed for unattended runs |
| 4 | monitor windows: vendored `monitor_problems` vertical checks + NEW horizontal check (window ≥ 2 carrier periods) + one-click `monitor_fix_plan` | bench incident 2026-07-25 (CH2 at 2.6 mV/div passed silently for five runs); horizontal added for cycling drive |
| 5 | feasibility: SINE/RAMP — I_pk = 2πfCV + leakage margin vs Trek ±2 mA (warn ≥60%, refuse ≥90%), slew, large-signal BW; SQUARE — edge time C·ΔV / I vs the half-period (warn ≥5%, refuse ≥25%), since square edges run the Trek at its limit by design | silent amp clipping invalidates week-long runs; SQUARE model added with the bender flagship (2026-09-29) |
| 6 | specimen hard cap (admin_caps.json): validated in the recipe AND clamped inside every `DriveSource` set | defense in depth; no GUI editor exists on purpose |
| 7 | Paschen **advisory**: attested pressure inside the band (config.json `paschen_warn_pa`, default 1 Pa–10 kPa) ⇒ the gate PASSES with a warning on the pre-flight (amber `!` row), in run.log, setup.txt (`--- Advisories ---`) and events.csv (`gate/paschen/warn`); re-evaluated after every mid-run wait_env attestation. No typed override exists — HV is never refused on pressure. | ECSS corona-sweep rationale: the Paschen minimum region is where mm-gap breakdown collapses. Until 2026-09-29 this inhibited HV; the author's decision that day (docs/MOTIVATION.md, roadmap 4) made it advisory: encapsulated specimens run at 4–5 kPa on purpose, and operators are never locked out of a pressure |
| 8 | typed `ENERGIZE` confirm quoting resolved kV / cap / cycles | upstream Energize-HV modal, default no |
| 9 | camera preflight (exposure_verdict + focus) | upstream _sldea_preflight |
| 10 | watchdog baseline learn: 0.5 s settle, 10 reads, discard 2, median ≥ 4, `credible_baseline_ua` gate (a standing fault current must trip, not normalize) | gui.py:3804-3852, review 2026-08-04 |

## During the run

- **Overcurrent** = the vendored `BreakdownWatchdog`, semantics
  untouched: trip on |I − baseline| ≥ 100 µA sustained 3 s of
  consecutive samples; None samples don't reset; `offscreen` (Tek
  9.9E37) counts as over-trip evidence. Trip path ordering preserved
  verbatim: telemetry `hold_flush` FIRST, then the event row, then
  evidence (ring dump; scope waveform pulled AFTER zeroing), then the
  abort (gui.py:3951-3966 — the writes on this path once measured +4 s
  of HV-live time on a stalled share).
- **Drive fidelity**: readback vs command > 5% sustained 5 s → pause at
  0 kV.
- **Monitoring loss**: 10 s unreadable → loud "watchdog is BLIND" warn,
  run continues (policy 2026-07-25); 60 s → pause at 0 kV (new default
  for unattended lifecycle runs; recipe-overridable).
- **Burst idle-level gate**: before the first HV chunk, the armed-idle
  output must scope-read ≈ v_min; a wrong idle (the STPS trap: SDG
  bursts idle at the start-phase point, default = the offset midpoint =
  half voltage held on the specimen) refuses burst counting and
  degrades to timed, loudly. BENCH_TEST §P proves the firmware side
  before any specimen.
- **Pause** is honored at safe boundaries only and always ramps to
  0 kV + checkpoints.
- **AMBER soft flags** ("potential failure") never stop the run by
  themselves; they demand acknowledgement (logged with the operator's
  name). Crossing the hard threshold aborts and latches the
  first-tripped criterion as the official failure mode.

## Shutdown and afterwards

- Guaranteed `finally`: `drive.zero()` = output OFF then DC 0 (never
  `BTWV STATE,OFF` mid-burst), captured handle first, then any
  reconnected handle (audit 2026-07-25 C1/C3 — a mid-run Reconnect must
  not strand the kill path). `zero()` never raises; False ⇒ terminal
  `NOT_ZEROED`: run.log + events.csv + status.json/html all carry
  "TURN OFF THE SG/TREK AT THE FRONT PANEL NOW", and the exit code is
  nonzero.
- SIGTERM/SIGINT: abort requested, 3 s grace, then the same finally.
- **Resume** (unclean checkpoint): recipe hash must match, and the
  RECHAR re-characterization interlude must pass the SLOW rules against
  the stored baseline BEFORE HV re-arms. RECHAR failure ends the run as
  `rechar_failed` — a human decides failed vs suspended.
- status.html is READ-ONLY by design: stopping HV requires bench
  presence (or the control-side GUI/CLI on the bench host).

## Known gaps (stated, not hidden)

1. No hardware interlock: software zero is the only kill (roadmap #1:
   Trek remote-TTL — open/high = OFF, inherently fail-safe — plus a
   heartbeat watchdog relay).
2. Environment is ATTESTED, not measured, in v1 — the Paschen
   advisory and the temperature-band gates are only as good as the
   operator's entry (roadmap #2: dedicated sensors drop into
   `EnvironmentSource`).
3. Nothing stops HV at Paschen-minimum pressures (advisory only, by
   decision). Exposed conductors, lead gaps and feedthroughs are the
   operator's responsibility; the band default has not been reviewed
   by the lab.
4. Trek 610E-G limits assumed from the 610E datasheet (±2 mA,
   ~1.2 kHz); confirm the -G variant manual at Phase 5 bench time.
