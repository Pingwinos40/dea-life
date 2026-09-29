# DATA_FORMATS.md — normative schemas

TL;DR: one run = one folder of append-only utf-8-sig CSVs + a
human-readable setup.txt + atomic JSON state. The CSVs are the data of
record; checkpoint/status are operational state. Nothing here is ever
rewritten in place — append or atomic-replace only.

## Run folder

    <data_root>/<specimen_id>/LC_YYYYmmdd_HHMMSS/
        setup.txt         human Key: value header (append-on-resume)
        recipe.json       RESOLVED recipe + sha256 (atomic)
        blocks.csv        one row per executed block
        interludes.csv    one row per characterization interlude
        events.csv        the audit log
        telemetry.csv     vendored TelemetryLog sidecar (DC phases,
                          inside interlude sub-runs)
        run.log           prelog-buffered then append-through
        frames/           INTnnn_{rest|peak}_<cycles>.png + event frames
        traces/           evtNNNN_<rule>_monitor.csv ring dumps +
                          evtNNNN_<rule>_scope.csv waveform freezes
        interludes/full_NN/   LEGACY-format SLDEA sub-run dirs
                          (setup.txt + data.csv + telemetry.csv +
                          frames/) — vendored sldea_edge / Edge Review /
                          sldea_plot work on these unmodified
        checkpoint.json   core/checkpoint.py (atomic)
        status.json       ~2 s heartbeat (atomic)
        status.html       read-only LAN page
        control.json      front-end commands ({seq, cmd, ...})
        report.html       analysis/report_run.py output
        report_milestone_NNNNNNNN.html
                          mid-run snapshot per recipe milestone
                          (cycle count, zero-padded)

## The three axes

Every record carries three independent progress axes:

- `cycles` — the claim currency ("10,000 cycles")
- wall clock — calendar cost (pause keeps it running)
- `actuated_s` — damage dose: Σ duty·Δt with duty = mean(V/V_pk).
  Convention: unipolar SINE / SQUARE(50%) / RAMP cycling = 0.5; DC hold
  above 0 kV = 1.0; interlude staircases integrate their own levels;
  C-estimate ramps ≈ half duty. Deterministic and clock-free — mock and
  live agree exactly. (Basis: EPFL high-cycle aging collapsed onto
  actuated-time, 10–200 Hz.)

## Counting modes (blocks.csv `counting`)

- `burst` — hardware-anchored: counted N-cycle burst fired AND the
  idle-level + per-chunk FREQUENCY verifications passed
- `timed` — continuous drive credited by ∫f_meas dt (stale-hold bounded:
  a dead scope stops accrual, honestly under-crediting)
- `estimated` — an interrupted chunk credited f × elapsed
- `dry` — nothing energized

## blocks.csv

    block_idx, loop_path, type, role, t_start_iso, t_end_iso, wall_s,
    cycles_planned, cycles_done, counting, cycles_total,
    actuated_s_block, actuated_s_total, waveform, freq_cmd_hz,
    freq_meas_hz, vpk_cmd_kv, vmin_cmd_kv, vmean_meas_kv, vpk_meas_kv,
    vvalley_meas_kv, imean_ua, ipk_ua, ivalley_ua, defl_metric,
    defl_pk, defl_valley, env_t_c, env_p_mbar, env_age_s, soft_flags,
    verdict, notes

`loop_path` like `2[7/50].0` locates a block instance inside nested
loops. V/I stats are captured MID-burst (idle values would be lies).

## interludes.csv

    interlude_idx, kind(fast|full), cycles_total, actuated_s_total,
    wall_s_total, t_iso, ref_kv, disp_metric, disp_value,
    disp_baseline, disp_ratio, leak_ua, leak_baseline_ua, leak_ratio,
    c_est_nf, c_quality, c_baseline_nf, c_ratio, zero_value,
    zero_drift, wrinkle_idx, focus, frames, subrun_dir, is_baseline,
    verdict, flags, notes

- `disp_metric` = `active_area_mm2` (planar, vendored engine) or
  `tip_defl_mm` (bender pipeline); ratios are vs the single
  `is_baseline=yes` interlude (post-break-in).
- `leak_ua` = median of the last half of a DC-hold's samples minus the
  learned 0 kV baseline. Only DC holds measure leakage (cycling MEAN
  folds in capacitive current).
- `c_est_nf` = symmetric charge integration, (q_up − q_down)/(2·ΔV):
  up- and down-ramps cancel leakage and standing offsets. `c_quality`
  is honest about the ~6 µA Trek monitor floor.
- `zero_drift` tracks the 0 kV rest metric across interludes (creep),
  scaled by the baseline displacement.

## events.csv

    event_idx, t_iso, wall_s, cycles_total, actuated_s_total, tier,
    rule_id, action, value, threshold, message, trace_file,
    frame_file, operator

Every rule firing, AMBER flag + acknowledgement, environment
attestation, Paschen advisory (`gate/paschen/warn`), milestone
snapshot (`engine/milestone`, threshold = milestone, value = cycles
at the snapshot), pause/resume, stop-cap, and typed
confirmation lands here. It is the audit log.

## checkpoint.json

Atomic JSON (deliberately not SQLite: CIFS shares drop out and
SQLite-over-CIFS is a corruption trap; one writer; CSVs are the data of
record). Fields: schema, run/specimen ids, recipe_sha256, mode,
engine_state, flat_idx (next block), cycles, actuated_s, wall_s,
counting_mode, baseline{...}, flags[], last_env, block_rows,
interlude_rows, event_rows, clean_shutdown, written_iso.
`clean_shutdown` flips true only on a clean end; startup finding false
= the unclean-shutdown detector → resume flow (hash match + RECHAR).

## status.json / control.json

status: run_id, specimen_id, mode, state, health(GREEN|AMBER|RED),
cycles_total, cycles_target, actuated_s_total, wall_s, block info,
kv_cmd/kv_meas, ua_last, leak_ua_last, disp_ratio_pct, env{...},
flags[], recent_events[], disposition, failure_mode, control_ack_seq,
updated_iso. A stale updated_iso IS the crash detector.

control: {seq, cmd: stop|pause|resume|suspend|ack_flag|env_entry,
flag_id?, t_c?, p_mbar?, by} — the engine acts on seq > last seen and
echoes control_ack_seq.

## Bender vision outputs

`analyze_frame` fields (also the replay CSV): tip/root px, tip_defl_mm
(vs the rest reference, at the FIXED material arc length L* so every
frame measures the same material point), bend_angle_deg (end-window
line fits on θ(s), extrapolated — integral estimators, never pointwise
derivatives), kappa_mean_m1 (INTERIOR mean; the profile's outer
stations are natural-boundary-condition artifacts and are excluded from
the scalar), kappa_profile_m1[64] (plot with that caveat), arc/proj
lengths, centerline_cov, spline_resid_px, spline_lam, contrast,
bg_median, tag_shift_px, conf (review-ordering score, NOT a
probability; accept_conf 0.75), flags
(root_lost|out_of_frame|foldback|lighting_drift|fixture_moved|no_tag).

## Registry

`specimens.csv` (utf-8-sig, atomic; status transitions only through
the app/engine): specimen_id, created_utc, geometry, material fields,
c_est_nf, ebd_ref_v_per_um/temp_c/source, status(virgin|in_test|
failed|suspended|retired), cycles_accum, actuated_s_accum,
last_run_id, failure_mode, notes. `suspended` = right-censored at
cycles_accum (Weibull enters it as a suspension — never dropped, never
a failure). `admin_caps.json` is the SAFETY file (see SAFETY.md).
