# CLAUDE.md — SLDEA Lifecycle Manager working conventions

TL;DR-first changelogs. Bench truth beats datasheet truth. Nothing that
touches an HV path ships without the matching gate/test. Run data never
enters the repo.

## Doc map

- `docs/MOTIVATION.md` — plan of record: the why, scope, dated
  decisions, open questions, and the numbered roadmap that code
  comments cite ("roadmap item N"). Replaces the off-repo
  calm-glacier plan (outdated as of 2026-09-29).
- `lib/VENDOR.md` — vendored-file provenance; the ONLY place lib/ deltas
  are recorded
- `docs/SAFETY.md` — every gate and kill path, with provenance
- `docs/DATA_FORMATS.md` — normative CSV/JSON schemas
- `BENCH_TEST.md` — hardware-in-the-loop checklist (§P burst semantics,
  §Q kill paths, §R dry rehearsal, §S dummy load, §T sacrificial disc)

## Conventions (inherited from Digital Multitool, kept on purpose)

- Python 3.10+, Tkinter for GUI, matplotlib figures in Paul Tol bright.
- Flat vendored imports: `lib/` is path-injected; never turn it into a
  package, never edit it without a VENDOR.md entry.
- Atomic writes everywhere: `tmp + os.replace`. JSON configs store raw
  strings. CSVs are utf-8-sig. `setup.txt` stays human-readable text.
- Worker threads never touch Tk; UI updates marshal via `root.after`.
- Every non-obvious decision carries a dated comment naming the bench
  session or incident that forced it.
- Dry-run is the default everywhere; presets/recipes never store an
  armed state.
- Runs live under `data_root` (config.json), never in the repo, never on
  OneDrive-synced paths (runstore refuses).
- Headless tests in `tests/` run via `python run_tests.py` (no hardware,
  no network). Hardware probes live in `bench/` only.

## Timeless traps

- BK4055B: 52-byte USB command cap (driver raises); `DUTY,50.0` parses
  as 5% (use `_fmt_param`); arb upload is LAN-only; **burst has no STPS
  in the vendored driver** — the HAL writes `BTWV STPS,270` raw and a
  pre-run gate scope-verifies the burst idle level ≈ 0 kV before any HV
  chunk. Never trust burst idle without that gate.
- MSO24: frequency measurement token is `FREQUENCY`, never `FREQ`;
  `9.9E37` = offscreen = clipped-but-real (counts as over-trip).
- Trek 610E-G: ±2 mA — feasibility gate computes I_pk = 2πfCV before
  any run; scope current monitor noise floor ≈ 6 µA (fault detection
  yes, nA leakage trending no — leakage is measured in DC holds only).
- `SldeaProfile` staircase constraints assume ≥ 60 s landings; recipe
  validation pre-checks `settle_s + snap_lead_s < landing_s` so the
  constructor can never throw mid-run.
- Cycle counts: `counting=burst` only after idle-verify AND per-chunk
  FREQUENCY verification; partial chunks are credited `estimated`.
- Damage dose ∝ actuated time, not just cycles: every record carries
  the three axes (cycles / wall clock / accumulated actuated seconds).
- Suspensions (cap reached, run stopped early for unrelated reasons)
  are right-censored — they enter Weibull as suspensions, never as
  failures and never dropped.
