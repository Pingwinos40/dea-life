# MOTIVATION.md — why DEA-LIFE exists and what it must deliver

TL;DR: DEA-LIFE builds the survival dataset for our multilayer DEA
benders: how many cycles, and how many actuated seconds, a bender lasts
in each environment it would meet in flight (hot, cold, vacuum,
radiation). It does that two ways: a set-and-forget Trek station on the
RHEL9 bench that cycles specimens to failure or to a 10⁶-cycle cap,
and an offline pipeline that pulls tip displacement and bend angle out
of existing recordings. The target is data good enough for real flight
qualification, not another demo. This file replaces the off-repo plan
of record (now outdated) as the source of the project's "why"; the
decisions below were recorded 2026-09-29.

## The question

How long do our benders survive, and how does their bending degrade on
the way, as a function of environment, material and drive? The answer
has to be a dataset with honest statistics: failures and suspensions
(specimens that hit the cap unbroken) both counted, sample size set by
convergence, and every number traceable to raw CSVs and a hashed
recipe. The headline claim format stays *"N cycles without dielectric
breakdown"* (or whichever failure criterion the recipe names; see Open
questions).

## Where this comes from

**Stratosphere paper.** C. Tugui, T. Thakar, A. Gogoj et al., "A Soft
Robotic Demonstration in the Stratosphere", arXiv:2603.04352 (2026).
Introduced the UV-curable resilient silicone (UV-RSE) and compared it
against CN9018 (acrylic) and Dragonskin-20. The cycle-life protocol
DEA-LIFE automates comes from here: 10 single-layer devices per
material per temperature, driven at 50% of that material's median
breakdown field at that temperature, strain measured every 10³ cycles,
stopped at no measurable strain, breakdown, or 10⁴ cycles. Conditions:
−40, 20 and 120 °C; 5 kPa at room temperature; 1 Hz on/off. Two
balloon flights followed (burst at 23.6 km; about −50 °C and 4 kPa at
the actuators), with UV-RSE benders driven at 1.8 kV.

**RINSC gamma campaign (2026).** "A Radiation Test Platform for Soft
Robotic Actuators", presented as a poster abstract at the IROS 2026
SRW workshop (no archival paper). Four benders (a UV-RSE pair and a
CN9018 pair, 20×40 mm) spent 551 h in a 1.48×10¹² Bq (40 Ci) Cs-137
field at the Rhode Island Nuclear Science Center, for an estimated
581–936 Gy total ionizing dose. The CN9018 pair actuated on camera
until the unshielded drive electronics stopped at 159 h (about 207 Gy
at the electronics) and still worked after retrieval; the UV-RSE pair
was inconclusive because of a fabrication defect. About 2,385 clips
were recorded at 4-minute intervals, but the timestamped drive records
were lost because they lived in volatile memory.

## What those campaigns taught the tool

| lesson | where it shows up in DEA-LIFE |
|---|---|
| RINSC drive logs were lost in volatile memory | append-only utf-8-sig CSVs, atomic JSON, checkpoint + resume (docs/DATA_FORMATS.md) |
| the drive electronics died before the actuator did, and nothing told the two apart | drive is measured, not assumed: Trek V/I monitors on the scope, drive-fidelity rule, NOT_ZEROED state |
| every environment needed its own one-off rig and script | one station, one recipe format; environment is a recipe parameter |
| the 10⁴ cap censored the silicones (UV-RSE never broke) | 10⁶ cap with milestone reports |
| RINSC evidence was camera-only; strain, C and leakage came after retrieval | fixed-reference interludes measure C, leakage and deflection throughout the run |
| different drive rates make "N cycles" hard to compare | three axes on every record: cycles, wall clock, actuated seconds |

## Scope

**In scope now**

1. **Live station.** RHEL9 bench, BK4055B → Trek 610E-G → specimen,
   MSO24 monitors, DFK camera. Benders are primary, single-layer
   planar discs secondary. Environments: −40, 20 and 120 °C at ambient
   pressure; 5 kPa at room temperature. Operated by any lab member,
   intended to run unattended.
2. **Offline analysis.** Recordings with a known drive in, tip
   displacement / bend angle / curvature over time, cycle and dose
   out. First corpus: the 159 h RINSC clips (camera views the bending
   plane edge-on).

**Later**

- Radiation as a live environment, only if a CV model on an NVIDIA
  Jetson Orin Nano proves reliable enough to run in-chamber.
- Combined cold + vacuum (no TVAC today).
- DEA-Characterization-Board v2 driving its own circuits.

## Decisions (2026-09-29)

| topic | decision | consequence for the tool |
|---|---|---|
| name | DEA-LIFE (was LIFE-DEA): **DEA** **L**ifecycle **I**nterrogation for **F**light **E**nvironments | repo, recipes, schema ids (`dea-life/1`), data root (`~/dea-life-runs`) and launcher (`dea_life_gui.py`) renamed while no run data existed; vendored upstream modules keep their `sldea_*` names |
| audience | the author's thesis first | data provenance and analysis must survive thesis-committee scrutiny |
| qualification | aim at real flight qualification; that is what carries over to the space economy | NASA-STD-5017 / ECSS life factors and the standards-pack templates are real roadmap items, not decoration |
| distribution | open source (GPLv3); single-lab use for now | honest docs and a clean RHEL9 install; no multi-site features |
| specimens | benders primary: 20×40 and 20×80 mm, usually encapsulated; layer count and capacitance vary per build | done: bender recipe is the flagship and `bender_20x80` is a registry geometry; capacitance comes from each specimen's registry row, never a nominal |
| materials | every MLDEA chemistry we build (acrylics, silicones, future ones) | material is a registry field, never a code path |
| sample size | no fixed 3/5/10; enough specimens for the error to converge | campaign view reports Weibull / B10 confidence-bound width as specimens accrue; the stopping rule is a CI-width target (value open). Zero-failure campaigns need a Weibayes-style lower bound (not implemented yet) |
| cold | dry-ice bed, thermocouple-verified (the 10⁴-cycle paper method) | temperature stays operator-attested until sensors land (roadmap 2) |
| vacuum | chamber with an acrylic viewing window; no TVAC | camera looks through the window; vacuum specimens are encapsulated |
| drive in vacuum | the Trek need not reach into the chamber; our own untethered circuits (up to 4 kV) can drive inside | the tool needs a **declared-drive** mode: drive parameters are declared, not commanded, and cycles come from the declared schedule and/or the video (roadmap 10) |
| vacuum HV gate | remove the pressure-band HV block; keep a logged warning note; never lock an operator out of a pressure | done (roadmap 4): the Paschen gate always passes, warns on the pre-flight, and writes the note to run.log, setup.txt and events.csv; no typed override |
| radiation | review existing footage now; live in-chamber only via reliable on-device CV later | roadmap 8 now, roadmap 12 later |
| cycle cap | 10⁶ with milestone reports | done: recipe `milestones` key; flagship recipes snapshot at 10³ / 10⁴ / 10⁵ (the final report covers the cap) |
| waveform | square on/off. 5 s / 5 s is the preferred look; the default is 2 s / 2 s (0.25 Hz) so each half-period covers the bending time constant | done: flagship is SQUARE 0.25 Hz; the feasibility gate models square edges (edge time C·ΔV / I vs the half-period) |
| drive level | 2.0 kV default for benders (more bending than 1.75 kV) | done: flagship drive and reference level; per-specimen hard caps still apply |
| failure definition | open; probably test-dependent | every criterion keeps logging after the first trip; the recipe names the official one and the claim states it |
| operators | anyone; set-and-forget | unattended live runs rest on software zero alone until the Trek remote-TTL interlock exists (docs/SAFETY.md known gap 1); roadmap 1 is therefore a prerequisite for unattended use |
| measurement hardware | Trek + scope monitors now; DEA-Characterization-Board v1 needs the Trek, v2 will drive its own circuits | the board slots in as a HAL plugin (roadmap 3) |
| vision | detect bending fingers against **any flat background**; no backlight requirement | bender vision must not depend on silhouette polarity (roadmap 6) |
| modelling | a 2D surrogate bender (N nodes along the length, FEA-like) fitted to the extracted centerline over time | roadmap 9 |

## Open questions

1. **Test duration.** At 2 s / 2 s, 10⁶ cycles take 4×10⁶ s ≈ 46 days
   per specimen (1 Hz: 11.6 days; 5 Hz: 2.3 days). Options: a faster
   default, an on-time derived from each specimen's measured bending
   time constant, or several specimens per run. Cycles at different
   rates are only comparable alongside actuated seconds.
2. **Several specimens per run.** Given 1. and convergence-driven
   sample sizes, parallel specimens look necessary. The engine runs
   one specimen per run today. Parallel drive needs per-specimen
   current isolation (one short must not end the others' runs),
   multi-ROI vision, and a per-specimen failure latch. The RINSC rig
   already drove four benders at once.
3. **RINSC drive timing.** The poster text says 5 s charge / 5 s
   discharge × 3 per trigger, one trigger every 4 minutes (0.1 Hz);
   the author recalls 0.25 Hz. The Pi Zero drive script is the
   tiebreaker, and clip time has to be back-calculated from the frames
   plus that script.
4. **Failure definition.** Short (breakdown), loss of bending
   (amplitude ratio), capacitance or leakage drift, or recipe-specific.
5. **Sample-size stopping rule.** Which CI width counts as converged.
6. **Milestones.** The flagship recipes use 10³ / 10⁴ / 10⁵ (the final
   report covers the 10⁶ cap); adjust if other counts matter.
7. **Declared-drive cycle counting.** Trust the declared schedule,
   count bends in the video, or both with a cross-check.

## Roadmap

Numbers are stable identifiers, not priorities; code comments cite
them ("roadmap item 1", "items 2-3", "item 8"). Older references to
"Phase 5" (bench bring-up) and "Phase 8" (bender fixture) map to
BENCH_TEST.md §P–§T and §U.

1. Trek remote-TTL hardware interlock + heartbeat watchdog relay
   (`DriveSource.hw_interlock()` slot).
2. Environment sensors (thermocouple, vacuum gauge) into
   `EnvironmentSource`, replacing typed attestations.
3. DEA-Characterization-Board plugin (`HAL_PLUGINS['charboard']`):
   v1 measurement with Trek drive, v2 own drive.
4. Vacuum gate becomes an advisory note (decision above). **Done.**
5. Bender flagship recipe: 2 kV, square 2 s / 2 s, 10⁶ cap, milestone
   reports, square-wave feasibility check. **Done.**
6. Background-agnostic bender vision: any flat background, no
   backlight.
7. Several specimens per run (open question 2).
8. RINSC 159 h corpus replay: tip displacement and bend angle vs time
   and dose (`vision/bender_replay.py`).
9. Surrogate 2D N-node bender model fitted to extracted centerlines.
10. Declared-drive mode for untethered in-chamber circuits.
11. Convergence-driven sample size + Weibayes bound in the campaign
    view.
12. Jetson Orin Nano on-device CV for in-chamber radiation runs.
13. Standards-pack templates (GEVS TVAC, MIL-STD-810, ECSS corona
    sweep; docs/RECIPES.md).

Hardware bring-up (BENCH_TEST.md §P–§T) gates every live item and runs
in parallel with the software items.
