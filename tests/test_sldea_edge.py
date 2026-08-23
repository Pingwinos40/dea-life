#!/usr/bin/env python3
"""Headless tests for sldea_edge (synthetic frames, no camera/instruments).

Run: .venv/bin/python tests/test_sldea_edge.py
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
import csv
import os
import shutil
import tempfile

import numpy as np

import sldea_edge as se


def _disc_frame(r, level=40.0, size=240, base=100.0):
    """Synthetic frame: uniform background `base` + a disc `level` brighter."""
    img = np.full((size, size), base, np.float32)
    yy, xx = np.mgrid[0:size, 0:size]
    img[(xx - size / 2) ** 2 + (yy - size / 2) ** 2 <= r * r] += level
    return img


def _fake_run(d, rows):
    os.makedirs(os.path.join(d, 'frames'), exist_ok=True)
    cols = ['snapshot', 'step', 'tag', 'nominal_kV', 'control_V',
            'measured_kV', 'measured_uA', 't_planned_s', 'timestamp',
            'frame_file', 'active_area_px', 'active_area_mm2',
            'active_diam_mm', 'notes']
    with open(os.path.join(d, 'data.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: '' for c in cols}, **r})


def test_candidates_find_synthetic_disc():
    base = _disc_frame(0, level=0)              # flat baseline
    img = _disc_frame(40)                        # r=40 disc
    cands = se.candidates(base, img, dict(se.DEFAULT_SETTINGS))
    assert cands, "no candidates on a clean synthetic disc"
    best = cands[0]
    true_area = np.pi * 40 * 40
    assert abs(best['area_px'] - true_area) / true_area < 0.10, best
    assert best['solidity'] > 0.9
    assert best['conf'] > 0.75
    assert not se.needs_review(cands, se.DEFAULT_SETTINGS)
    assert all(c['method'].startswith('diff') for c in cands), \
        "hough must be gone (it fabricated circles on real frames)"


def test_oblong_shape_scores_like_a_circle():
    # An ellipse (2:1) must NOT be punished -- the DEA expansion can be oblong
    base = _disc_frame(0, level=0)
    img = np.full((240, 240), 100.0, np.float32)
    yy, xx = np.mgrid[0:240, 0:240]
    img[((xx - 120) / 60.0) ** 2 + ((yy - 120) / 30.0) ** 2 <= 1] += 40
    cands = se.candidates(base, img, dict(se.DEFAULT_SETTINGS))
    assert cands, "no candidates on the ellipse"
    best = cands[0]
    true_area = np.pi * 60 * 30
    assert abs(best['area_px'] - true_area) / true_area < 0.10, best
    assert best['solidity'] > 0.9, "solidity must not punish oblong shapes"
    assert best['conf'] > 0.75


def test_wrinkled_region_outranks_bigger_smooth_one():
    # Lab definition: the WRINKLED region is the active area. A patch filled
    # with high-frequency stripes must outrank a bigger, SEPARATE smooth blob.
    yy, xx = np.mgrid[0:300, 0:300]
    # mildly textured baseline (a flat base makes the texture ratio
    # degenerate; real frames always carry sensor grain)
    base = (100.0 + 2.0 * ((xx // 8 + yy // 8) % 2)).astype(np.float32)
    img = base.copy()
    smooth = (xx - 90) ** 2 + (yy - 150) ** 2 <= 55 * 55      # big faint blob
    img[smooth] += 12
    wr = (xx - 215) ** 2 + (yy - 150) ** 2 <= 35 * 35         # wrinkled patch
    img[wr] += 15 + 40 * ((xx[wr] // 4) % 2)                  # strong stripes
    cands = se.candidates(base, img, dict(se.DEFAULT_SETTINGS))
    assert cands, "no candidates"
    best = cands[0]
    assert best['wrinkle'] > 1.4, best
    # best outline centres on the wrinkled patch, not the smooth blob
    assert abs(best['cx'] - 215) < 45, \
        f"best candidate is not the wrinkled patch (cx={best['cx']:.0f})"


def test_wrinkle_onset_annotations():
    rows = [{} for _ in range(5)]
    results = {1: {'wrinkle': 1.1}, 2: {'wrinkle': 1.9},
               3: {'wrinkle': 2.4}, 4: None}
    onset, annos = se.wrinkle_onset(rows, results, se.DEFAULT_SETTINGS)
    assert onset == 2
    assert 'onset' in annos[2] and 'onset' not in annos[3]
    assert 1 not in annos and 4 not in annos


def test_apply_results_writes_wrinkle_and_annos():
    rows = [{'tag': 'post-ramp', 'nominal_kV': '5'}]
    results = {0: {'area_px': 100.0, 'diam_px': 11.3, 'conf': 0.9,
                   'method': 'diff-hi', 'wrinkle': 2.1}}
    se.apply_results(rows, results, None, {},
                     {0: 'wrinkle-mode onset (idx 2.1)'})
    assert rows[0]['wrinkle_idx'] == '2.10'
    assert 'wrinkle-mode onset' in rows[0]['notes']


def test_no_change_gate_returns_empty():
    # Identical frame (plus faint noise) => no candidates, not a fabricated
    # outline -- this was the 'randomly placed circle' failure mode.
    rng = np.random.default_rng(3)
    base = np.clip(rng.normal(100, 2, (240, 240)), 0,
                   255).astype(np.float32)
    img = np.clip(base + rng.normal(0, 1.5, base.shape), 0,
                  255).astype(np.float32)
    s = dict(se.DEFAULT_SETTINGS)
    s['min_diff'] = 10.0            # explicit: gate semantics under test
    assert se.candidates(base, img, s) == []


def test_electrode_glints_are_masked_out():
    # A near-saturated strip (copper electrode) whose glint shifts between
    # baseline and frame must NOT become the detection; the mid-grey disc
    # elsewhere must win.
    base = np.full((240, 240), 100.0, np.float32)
    base[15:225, 105:135] = 240.0                 # bright electrode strip
    base[15:135, 105:135] = 255.0                 # glint on the TOP half
    img = base.copy()
    img[15:135, 105:135] = 195.0                  # glint moved away...
    img[135:225, 105:135] = 255.0                 # ...to the BOTTOM
    yy, xx = np.mgrid[0:240, 0:240]
    disc = (xx - 55) ** 2 + (yy - 120) ** 2 <= 28 * 28
    img[disc] += 35                                # the real change
    s = dict(se.DEFAULT_SETTINGS)
    cands = se.candidates(base, img, s)
    assert cands, "no candidates with the disc present"
    best = cands[0]
    assert abs(best['cx'] - 55) < 25, \
        f"electrode strip won instead of the disc (cx={best['cx']:.0f})"
    # with masking disabled the strip dominates -> proves the mask is what
    # excluded it
    s2 = dict(s)
    s2['electrode_lum'] = 0
    c2 = se.candidates(base, img, s2)
    assert c2 and abs(c2[0]['cx'] - 120) < 30, \
        "strip should dominate unmasked; test scene too weak"


def test_norm_bg_neutralizes_global_brightness_drift():
    # The DFK's internal auto-gain drifts global brightness a few percent;
    # without normalization the WHOLE frame diffs as fake change.
    base = _disc_frame(0, level=0, base=120.0)
    img = _disc_frame(40, level=30, base=120.0) * 1.10   # +10% global drift
    s = dict(se.DEFAULT_SETTINGS)
    cands = se.candidates(base, np.clip(img, 0, 255), s)
    assert cands, "no candidates with normalization on"
    true_area = np.pi * 40 * 40
    assert abs(cands[0]['area_px'] - true_area) / true_area < 0.15, \
        f"norm_bg failed to isolate the disc: {cands[0]['area_px']:.0f} px"


def test_affine_norm_survives_a_gain_and_offset_baseline_mismatch():
    """Bench runs P3_* (2026-07-28): every frame sat at gain 0.72-0.82 with
    a +8..+41 offset against its own baseline, leaving a 26-gray-level
    pedestal that no threshold could sit above. Two snapshots at one
    voltage agreed to ~0.5 sigma, so this is the baseline vs the run, not
    the camera. A scalar ratio cannot express it; gain+offset can."""
    # A scene with real dynamic range: a gradient plus a bright strip, the
    # way a lit dish and its electrodes look. A FLAT baseline would leave
    # the fit no range to work with and is not what any frame looks like.
    yy, xx = np.mgrid[0:240, 0:240]
    base = (60.0 + 140.0 * xx / 239.0).astype(np.float32)
    base[:, 8:20] = 235.0
    img = base.copy()
    img[(xx - 120) ** 2 + (yy - 120) ** 2 <= 40 * 40] += 30
    # the mismatch: the frame is a compressed, lifted version of the scene
    img = np.clip(img * 0.78 + 12.0, 0, 255)

    s = dict(se.DEFAULT_SETTINGS)
    assert s['norm_bg'] == 2, "affine normalization must be the default"
    cands = se.candidates(base, img, s)
    assert cands, "no candidates under a gain+offset mismatch"
    true_area = np.pi * 40 * 40
    err = abs(cands[0]['area_px'] - true_area) / true_area
    assert err < 0.15, f"affine norm missed the disc: {cands[0]['area_px']:.0f}"

    # and the fit itself recovers the transform it was given
    roi = (slice(18, 222), slice(18, 222))
    a, b = se.photometric_fit(base, img, roi)
    assert abs(a - 0.78) < 0.06, a

    # Compare the corrections where NOTHING changed -- outside the disc,
    # inside the ROI. There the residual is pure artifact, so a correct
    # model drives it to zero while a wrong one leaves the pedestal. Over
    # the whole ROI the disc's real signal floors both and hides the
    # difference, which is exactly the confusion the bench ran into.
    still = np.zeros(base.shape, bool)
    still[roi] = True
    still[(xx - 120) ** 2 + (yy - 120) ** 2 <= 50 * 50] = False
    scalar = np.clip(img * (np.median(base) / np.median(img)), 0, 255)
    raw_resid = float(np.abs(img - base)[still].mean())
    scalar_resid = float(np.abs(scalar - base)[still].mean())
    affine_resid = float(np.abs(((img - b) / a) - base)[still].mean())
    # affine removes ~87% of the artifact here, the scalar ~57%. Neither
    # reaches zero: adding a region reorders the intensity distribution
    # rather than only shifting its tail, so quantile matching can close
    # most of the gap but not all of it.
    assert affine_resid < 0.25 * raw_resid, (affine_resid, raw_resid)
    assert affine_resid < 0.5 * scalar_resid, (affine_resid, scalar_resid)


def test_legacy_scalar_norm_is_still_reachable():
    """A run tuned under norm_bg: 1 must reprocess the way it was tuned."""
    base = _disc_frame(0, level=0, base=120.0)
    img = np.clip(_disc_frame(40, level=30, base=120.0) * 1.10, 0, 255)
    s = dict(se.DEFAULT_SETTINGS)
    s['norm_bg'] = 1
    cands = se.candidates(base, img, s)
    assert cands, "legacy scalar path stopped detecting"
    true_area = np.pi * 40 * 40
    assert abs(cands[0]['area_px'] - true_area) / true_area < 0.15


def test_baseline_disc_traces_resting_dea():
    # Non-diff detector for the px->mm scale (audit 2026-07-25): the
    # resting disc is DARKER than the membrane; electrode strips cross it.
    # This frame is the BRIGHT-electrode case (strip at 250), so it must
    # be given the bright-electrode setting -- since 2026-08-05 the
    # DEFAULT is 255 (mask off) for dark carbon-black electrodes.
    img = np.full((480, 640), 200.0, np.float32)          # membrane
    yy, xx = np.mgrid[0:480, 0:640]
    disc = (xx - 320) ** 2 + (yy - 240) ** 2 <= 100 * 100
    img[disc] = 165.0                                     # resting disc
    img[:, 312:326] = 250.0                               # electrode strip
    bright = dict(se.DEFAULT_SETTINGS, electrode_lum=220.0)
    ref = se.baseline_disc(img, bright)
    assert ref is not None, "disc not found on baseline"
    assert ref['method'] == 'baseline-disc'
    # the strip splits the dark disc; merged contour must still recover a
    # diameter near 200 px
    assert abs(ref['diam_px'] - 200) / 200 < 0.20, ref['diam_px']
    # flat frame (no disc) must yield None, never a fabrication
    flat = np.full((480, 640), 200.0, np.float32)
    assert se.baseline_disc(flat, bright) is None


def test_electrode_mask_255_only_costs_a_flat_synthetic_strip():
    """What the 220 -> 255 default change does and does NOT cost.

    A flat synthetic bright rectangle is the worst case, and the ONLY
    case where the change bites: `foil_mask` is texture-derived, so it
    does not recognise a painted rectangle as foil, leaving the
    brightness cut as the only thing rejecting it. Remove that and the
    strip's own edge becomes a strong dark->light step, so the fit
    refuses rather than returning a wrong diameter.

    On REAL data it costs nothing, which is the point of this test's
    name. Measured across all 12 readable runs of the 2026-08-05 batch
    (P3_1/2/3/5/6/7, DOT_P3_1, the four 07-23 runs, 104531): the
    brightness cut at 220 is a SUBSET of the texture footprint on every
    P3 run (0.00-0.09% of frame lost by the change), and the
    resting-disc cross-check moves by at most ONE pixel anywhere in the
    batch (578->578, 577->577, 584->584, 606->606, 543->543, 527->527,
    and 370->371 / 361->362 on the 07-23 optics). The texture footprint
    is what actually covers the strips -- the brightness cut only ever
    caught the specular streaks inside them."""
    img = np.full((480, 640), 200.0, np.float32)
    yy, xx = np.mgrid[0:480, 0:640]
    img[(xx - 320) ** 2 + (yy - 240) ** 2 <= 100 * 100] = 165.0
    img[:, 312:326] = 250.0                               # BRIGHT strip
    assert se.baseline_disc(img, dict(se.DEFAULT_SETTINGS)) is None
    assert se.baseline_disc(
        img, dict(se.DEFAULT_SETTINGS, electrode_lum=220.0)) is not None
    # The gain side — that 255 lets edge detection work on a dark
    # carbon-black electrode — is a BENCH observation (2026-08-05) and is
    # deliberately NOT asserted here: it has not been reproduced on
    # synthetic frames, and a test that pretended otherwise would be the
    # kind of measurement that lies. See SLDEA_HANDOFF.
    assert se.DEFAULT_SETTINGS['electrode_lum'] == 255.0


def test_mm_per_px_prefers_baseline_ref_over_first_accept():
    rows = [{'tag': 'baseline'}, {'tag': 'post-ramp'}]
    results = {1: {'diam_px': 100.0, 'area_px': 1.0}}   # activated frame
    s = dict(se.DEFAULT_SETTINGS); s['diam_mm'] = 16.0
    # without a baseline ref: falls back to the activated frame (documented)
    assert abs(se.mm_per_px(results, rows, s) - 0.16) < 1e-9
    # with the baseline-disc ref: the ref wins
    ref = {'method': 'baseline-disc', 'diam_px': 200.0}
    assert abs(se.mm_per_px(results, rows, s, baseline_ref=ref) - 0.08) < 1e-9
    assert 'baseline-disc' in se.scale_source(results, rows,
                                              baseline_ref=ref)
    assert 'FIRST ACCEPTED' in se.scale_source(results, rows)


def test_mm_per_px_manual_calibration_overrides_baseline_row():
    # Revised 2026-08-05 (flagged major): the operator's 📏 calibration
    # used to be silently IGNORED whenever the baseline row had an
    # accepted result — the old order put that result first.
    rows = [{'tag': 'baseline'}, {'tag': 'post-ramp'}]
    results = {0: {'diam_px': 100.0, 'area_px': 7854.0}}   # baseline row
    s = dict(se.DEFAULT_SETTINGS); s['diam_mm'] = 16.0
    # no ref: the baseline-row result anchors (unchanged)
    assert abs(se.mm_per_px(results, rows, s) - 0.16) < 1e-9
    # manual calibration OUTRANKS the baseline-row result
    manual = {'method': 'manual-calibration', 'diam_px': 200.0}
    assert abs(se.mm_per_px(results, rows, s, baseline_ref=manual)
               - 0.08) < 1e-9
    assert 'manual-calibration' in se.scale_source(results, rows,
                                                   baseline_ref=manual)
    # an AUTOMATIC ref keeps the old order: the baseline row still wins
    auto = {'method': 'baseline-disc', 'diam_px': 200.0}
    assert abs(se.mm_per_px(results, rows, s, baseline_ref=auto)
               - 0.16) < 1e-9
    assert 'baseline row' in se.scale_source(results, rows,
                                             baseline_ref=auto)


def test_apply_results_reprocess_blanks_and_dedups():
    # audit 2026-07-25: rejected rows kept stale mm2/wrinkle; repeated
    # saves duplicated breakdown notes.
    rows = [{'tag': 'post-ramp', 'nominal_kV': '5',
             'active_area_mm2': '12.3', 'active_diam_mm': '4.0',
             'wrinkle_idx': '1.80', 'notes': ''}]
    se.apply_results(rows, {0: None}, None, {})          # reviewed+rejected
    assert rows[0]['active_area_mm2'] == '' and rows[0]['wrinkle_idx'] == ''
    # accepted with NO scale blanks mm2/diam instead of keeping stale ones
    rows2 = [{'active_area_mm2': '9.9', 'active_diam_mm': '3.3',
              'notes': ''}]
    se.apply_results(rows2, {0: {'area_px': 100.0, 'diam_px': 11.3,
                                 'conf': 0.9, 'method': 'diff-hi'}},
                     None, {})
    assert rows2[0]['active_area_px'] == '100'
    assert rows2[0]['active_area_mm2'] == ''
    # flags applied twice -> note appears once
    rows3 = [{'notes': ''}]
    res = {0: {'area_px': 1.0, 'diam_px': 1.0, 'conf': 0.5,
               'method': 'diff-hi'}}
    se.apply_results(rows3, res, None, {0: 'BREAKDOWN? current spike'})
    se.apply_results(rows3, res, None, {0: 'BREAKDOWN? current spike'})
    assert rows3[0]['notes'].count('BREAKDOWN? current spike') == 1


def test_weak_fallback_candidate_reaches_review():
    # A change that fails the fill filter must still surface ONE candidate
    # for human review, not silently vanish -- and a fallback must never
    # auto-accept, however good its other metrics look.
    base = _disc_frame(0, level=0)
    img = _disc_frame(40)                       # clean disc...
    s = dict(se.DEFAULT_SETTINGS)
    s['min_solidity'] = 1.01                    # impossible: force the fallback
    cands = se.candidates(base, img, s)
    assert len(cands) == 1, "fallback must keep exactly the best candidate"
    assert cands[0].get('fallback') is True
    assert se.needs_review(cands, s), "fallback must always go to review"


def test_candidates_downscaled_frame_rescales_to_full_res():
    # A 1280-wide frame is detected at DETECT_MAX_W but must report
    # full-resolution px quantities.
    base = _disc_frame(0, level=0, size=1280)
    img = _disc_frame(200, size=1280)
    cands = se.candidates(base, img, dict(se.DEFAULT_SETTINGS))
    assert cands, "no candidates on the large synthetic disc"
    best = cands[0]
    true_area = np.pi * 200 * 200
    assert abs(best['area_px'] - true_area) / true_area < 0.10, best['area_px']
    assert abs(best['diam_px'] - 400) / 400 < 0.06, best['diam_px']
    # contour points are in full-res coordinates too
    xs = [p[0] for p in best['contour']]
    assert max(xs) > se.DETECT_MAX_W, "contour still in downscaled coords"


def test_mark_breakdown_files_renames_from_first_flag():
    d = tempfile.mkdtemp(prefix='edge_bd_')
    try:
        names = ['b.png', 'f1.png', 'f2.png', 'f3.png']
        _fake_run(d, [{'snapshot': i + 1, 'step': i,
                       'tag': 'baseline' if i == 0 else 'post',
                       'nominal_kV': str(float(i)), 'frame_file': n}
                      for i, n in enumerate(names)])
        for n in names:
            open(os.path.join(d, 'frames', n), 'wb').write(b'x')
        run = se.load_run(d)
        renamed = se.mark_breakdown_files(run, {2: 'breakdown? I=90uA'})
        assert renamed == 2                       # f2 + f3, not b/f1
        frames = sorted(os.listdir(os.path.join(d, 'frames')))
        assert 'f2_BREAKDOWN.png' in frames and 'f3_BREAKDOWN.png' in frames
        assert 'f1.png' in frames                 # pre-breakdown untouched
        assert run['rows'][2]['frame_file'] == 'f2_BREAKDOWN.png'
        assert 'post-breakdown' in run['rows'][3]['notes']
        assert 'post-breakdown' not in (run['rows'][1].get('notes') or '')
        # idempotent: nothing further to rename
        assert se.mark_breakdown_files(run, {2: 'x'}) == 0
        # RETRACTED flags un-brand (audit 2026-08-05): the current-
        # confirmed semantics exist to retract false flags, and the
        # retraction must reach the files, the CSV links and the notes —
        # before this, P3_5's 35 falsely-branded frames stayed branded
        # forever
        assert se.mark_breakdown_files(run, {}) == 2
        frames = sorted(os.listdir(os.path.join(d, 'frames')))
        assert 'f2.png' in frames and 'f3.png' in frames
        assert not any('_BREAKDOWN' in f for f in frames)
        assert run['rows'][2]['frame_file'] == 'f2.png'
        assert run['rows'][3]['frame_file'] == 'f3.png'
        assert 'post-breakdown' not in (run['rows'][3].get('notes') or '')
        # and that too is idempotent
        assert se.mark_breakdown_files(run, {}) == 0
    finally:
        shutil.rmtree(d)


def test_needs_review_on_weak_or_empty():
    assert se.needs_review([], se.DEFAULT_SETTINGS)
    weak = [{'conf': 0.4, 'spread_pct': 5.0}]
    assert se.needs_review(weak, se.DEFAULT_SETTINGS)
    disagree = [{'conf': 0.9, 'spread_pct': 40.0}]
    assert se.needs_review(disagree, se.DEFAULT_SETTINGS)


def test_settings_roundtrip_and_diam_from_setup():
    d = tempfile.mkdtemp(prefix='edge_')
    try:
        with open(os.path.join(d, 'setup.txt'), 'w') as f:
            f.write("SLDEA Test -- x\nDEA nominal diameter: 12.5 mm\n")
        s = se.load_settings(d)
        assert s['diam_mm'] == 12.5              # picked up from the run header
        s['breakdown_ua'] = 75.0
        s['blur_px'] = 7
        se.save_settings(d, s)
        se.save_settings(d, s)                   # idempotent (section replaced)
        text = open(os.path.join(d, 'setup.txt')).read()
        assert text.count(se.EDGE_HDR) == 1
        assert 'DEA nominal diameter' in text    # original header kept
        s2 = se.load_settings(d)
        assert s2['breakdown_ua'] == 75.0 and s2['blur_px'] == 7
        assert isinstance(s2['blur_px'], int)
    finally:
        shutil.rmtree(d)


def test_breakdown_flags_current_and_collapse():
    """Semantics changed 2026-08-04: breakdown_flags returns (confirmed,
    advisory) and the median-deviation rule needs >= 5 parseable uA rows.
    This 4-row fixture now pins the LEGACY FALLBACK path (no median):
    absolute breakdown_ua spike + uncorroborated collapse both stay
    confirmed, exactly the pre-2026-08-04 behaviour."""
    rows = [{'nominal_kV': '1', 'measured_uA': '2'},
            {'nominal_kV': '2', 'measured_uA': '120'},     # current spike
            {'nominal_kV': '3', 'measured_uA': '3'},
            {'nominal_kV': '4', 'measured_uA': '4'}]       # area collapse
    areas = {0: 1000.0, 2: 1050.0, 3: 300.0}
    flags, advis = se.breakdown_flags(rows, areas, se.DEFAULT_SETTINGS)
    assert 1 in flags and 'uA' in flags[1]
    assert 3 in flags and 'collapse' in flags[3]
    assert 0 not in flags and 2 not in flags
    assert advis == {}


def _ua_rows(uas, kv_step=0.25):
    """Rows with a staircase nominal_kV and the given measured_uA values
    (None -> blank cell, as a dry-run/glitched snapshot writes it)."""
    return [{'nominal_kV': f"{(i + 1) * kv_step:g}",
             'measured_uA': '' if ua is None else str(ua)}
            for i, ua in enumerate(uas)]


def test_breakdown_staircase_confirms_at_first_event_row():
    # SLDEA_20260723_233451: median +0.35, staircase -5 -> -27 -> -62 ->
    # -207 uA then recovery (sample burned open). Confirmed via the
    # consecutive-rows clause; onset = first row with dev >= 20.
    uas = [0.8, 0.4, 0.3, 0.5, 0.2, -5.0, -26.7, -61.9, -123.4, -207.7,
           -3.3, -4.1]
    flags, advis = se.breakdown_flags(_ua_rows(uas), {},
                                      se.DEFAULT_SETTINGS)
    assert sorted(flags) == [6, 7, 8, 9]      # -5.0 (dev 5.4) is no event
    assert min(flags) == 6                     # onset at the -26.7 row
    assert 'breakdown?' in flags[6] and 'dev' in flags[6]
    assert advis == {}                         # recovery rows are clean


def test_breakdown_terminal_event_confirms_without_recovery_evidence():
    # SLDEA_20260723_155425: quiet run whose LAST parseable row is a
    # single -57 uA event -- no recovery sample exists, so it confirms
    # (unlike a mid-run one-row spike, which recovers and is advisory).
    uas = [0.9, 0.9, 1.0, 0.9, 0.8, 0.9, -57.4]
    flags, advis = se.breakdown_flags(_ua_rows(uas), {},
                                      se.DEFAULT_SETTINGS)
    assert list(flags) == [6] and advis == {}
    # trailing blank rows (aborted capture) do not hide the terminal event
    uas2 = uas + [None, None]
    flags2, _ = se.breakdown_flags(_ua_rows(uas2), {}, se.DEFAULT_SETTINGS)
    assert list(flags2) == [6]


def test_breakdown_single_recovered_spike_is_advisory_only():
    # SLDEA_20260729_104531: one -153 uA sample (dev 137 vs the -16 uA
    # baseline) then healthy to 10 kV. Magnitude alone must NOT confirm:
    # any tier low enough to keep 155425 also fires here (research 2026-
    # 08-04, rule 2 rejected). Advisory note, zero renames.
    uas = [-16.0, -15.9, -16.1, -16.0, -153.4, -15.8, -16.0, -15.9]
    flags, advis = se.breakdown_flags(_ua_rows(uas), {},
                                      se.DEFAULT_SETTINGS)
    assert flags == {}
    assert list(advis) == [4]
    assert 'transient discharge?' in advis[4] and '137' in advis[4]


def test_breakdown_median_baseline_handles_both_campaign_offsets():
    # The same +30 uA excursion must read identically on a -16 uA-offset
    # run (07-29 campaign) and a +0.9 uA-offset run (07-23): the absolute
    # rule was 34/66 uA asymmetric on the former.
    for base in (-16.0, 0.9):
        uas = [base] * 6 + [base + 30.0, base + 31.0] + [base] * 2
        flags, _ = se.breakdown_flags(_ua_rows(uas), {},
                                      se.DEFAULT_SETTINGS)
        assert sorted(flags) == [6, 7], (base, flags)


def test_breakdown_blank_row_breaks_consecutiveness():
    # A blank-uA row between two event rows breaks 'consecutive' --
    # deliberate conservatism: at ~30 s/row there is no evidence the
    # excursion spanned the gap, so each side stays a recovered
    # transient (advisory), not a confirmed breakdown.
    uas = [0.0, 0.1, -0.1, 0.0, 0.1, 50.0, None, 51.0, 0.0]
    flags, advis = se.breakdown_flags(_ua_rows(uas), {},
                                      se.DEFAULT_SETTINGS)
    assert flags == {}
    assert sorted(advis) == [5, 7]


def test_area_collapse_without_current_signature_is_advisory():
    # P3_5_2.5mL_0729: 36% area 'collapse' from the manual-trace ->
    # disc-fit method switch while the current never left +-11 uA of its
    # -16 uA baseline. The old rule renamed 35 healthy frames; now it is
    # an advisory note and mark_breakdown_files never sees it.
    uas = [-16.0, -16.1, -15.9, -16.0, -16.2, -16.0, -16.1, -16.0]
    rows = _ua_rows(uas)
    areas = {2: 1000.0, 3: 990.0, 5: 620.0}          # -37% at row 5
    flags, advis = se.breakdown_flags(rows, areas, se.DEFAULT_SETTINGS)
    assert flags == {}
    assert 5 in advis and 'no current signature' in advis[5]
    assert 'collapse' in advis[5]


def test_area_collapse_with_current_event_at_same_level_confirms():
    # Same collapse, but a row at the SAME nominal kV carries a >= 20 uA
    # deviation -- current corroboration keeps it a confirmed breakdown.
    uas = [-16.0, -16.1, -15.9, -16.0, -16.2, -80.0, -16.1, -16.0]
    rows = _ua_rows(uas)
    rows[5]['nominal_kV'] = rows[4]['nominal_kV']    # same level pair
    areas = {2: 1000.0, 3: 990.0, 4: 620.0}          # -37% at row 4
    flags, advis = se.breakdown_flags(rows, areas, se.DEFAULT_SETTINGS)
    assert 4 in flags and 'collapsed' in flags[4]
    assert 4 not in advis
    # the single recovered event ON the collapse row itself: its
    # 'transient discharge?' advisory is written before the collapse pass
    # can confirm the row, and used to survive next to the flag -- a
    # confirmed row must supersede its own advisories (review 2026-08-04)
    rows2 = _ua_rows(uas)                            # event row 5 as-is
    areas2 = {2: 1000.0, 3: 990.0, 5: 620.0}         # -37% at row 5
    flags2, advis2 = se.breakdown_flags(rows2, areas2, se.DEFAULT_SETTINGS)
    assert 5 in flags2 and 'collapsed' in flags2[5]
    assert 5 not in advis2, advis2


def test_old_setup_txt_without_dev_key_loads_and_roundtrips():
    """Settings compat: a pre-2026-08-04 setup.txt knows only
    breakdown_ua; it must load with the new breakdown_dev_ua default,
    and the new key must survive a save/load round-trip."""
    d = tempfile.mkdtemp(prefix='edge_compat_')
    try:
        with open(os.path.join(d, 'setup.txt'), 'w') as f:
            f.write("SLDEA Test -- x\n\n" + se.EDGE_HDR +
                    "\nbreakdown_ua: 75\narea_jump_pct: 30\n")
        s = se.load_settings(d)
        assert s['breakdown_ua'] == 75.0 and s['area_jump_pct'] == 30.0
        assert s['breakdown_dev_ua'] == \
            se.DEFAULT_SETTINGS['breakdown_dev_ua']
        s['breakdown_dev_ua'] = 25.0
        se.save_settings(d, s)
        s2 = se.load_settings(d)
        assert s2['breakdown_dev_ua'] == 25.0
        assert s2['breakdown_ua'] == 75.0
    finally:
        shutil.rmtree(d)


def test_scale_apply_and_write_back():
    d = tempfile.mkdtemp(prefix='edge_')
    try:
        _fake_run(d, [
            {'snapshot': 1, 'step': 0, 'tag': 'baseline', 'nominal_kV': '0.0',
             'frame_file': 'b.png'},
            {'snapshot': 2, 'step': 1, 'tag': 'post', 'nominal_kV': '1.0',
             'frame_file': 'f1.png'}])
        run = se.load_run(d)
        # baseline detected at diam 100 px; nominal diam 16 mm -> 0.16 mm/px
        results = {0: {'area_px': np.pi * 50 * 50, 'diam_px': 100.0,
                       'circ': 0.9, 'conf': 0.9, 'method': 'diff-otsu'},
                   1: {'area_px': np.pi * 60 * 60, 'diam_px': 120.0,
                       'circ': 0.9, 'conf': 0.8, 'method': 'diff-otsu',
                       'chosen_by': 'user'}}
        s = dict(se.DEFAULT_SETTINGS)
        scale = se.mm_per_px(results, run['rows'], s)
        assert abs(scale - 0.16) < 1e-9
        se.apply_results(run['rows'], results, scale,
                         {1: 'breakdown? I=90uA > 50uA'})
        assert run['rows'][0]['active_area_px'] == f"{np.pi*50*50:.0f}"
        assert abs(float(run['rows'][1]['active_diam_mm']) - 19.2) < 1e-6
        assert 'user' in run['rows'][1]['notes']
        assert 'breakdown?' in run['rows'][1]['notes']
        se.write_back(d, run)
        assert os.path.exists(os.path.join(d, 'data.csv.bak'))
        with open(os.path.join(d, 'data.csv')) as f:
            rows2 = list(csv.DictReader(f))
        assert rows2[1]['active_area_mm2'] != ''
    finally:
        shutil.rmtree(d)


def test_rejected_row_marked():
    rows = [{'tag': 'post', 'nominal_kV': '1'}]
    se.apply_results(rows, {0: None}, None, {})
    assert rows[0]['notes'] == 'rejected (no reliable edge)'


def test_run_csv_accepts_a_renamed_data_csv():
    """Excel will not open two workbooks with the same filename, so the
    bench renames copies data1.csv, data2.csv... A renamed run is still a
    run, and must not go invisible to the tuner, Edge Review and the
    diagnostic at once (bench 2026-07-28)."""
    d = tempfile.mkdtemp(prefix='runcsv_')
    assert se.run_csv(d) is None                  # not a run yet
    _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
    os.rename(os.path.join(d, 'data.csv'), os.path.join(d, 'data2.csv'))
    assert os.path.basename(se.run_csv(d)) == 'data2.csv'
    run = se.load_run(d)
    assert len(run['rows']) == 1
    assert run['csv_path'].endswith('data2.csv')

    # an exact data.csv always wins over a numbered copy
    _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
    assert os.path.basename(se.run_csv(d)) == 'data.csv'


def test_resolve_run_accepts_the_run_folder_itself():
    """`newest_run` only looks at SUB-directories, so handed a run folder
    it returns None even though data.csv is right there. The SLDEA tab's
    Tune button used it and told the operator "no finished runs
    (data.csv)" about a directory containing data.csv (bench 2026-08-05).
    `resolve_run` is the shared resolver and takes either shape."""
    parent = tempfile.mkdtemp(prefix='resolve_')
    d = os.path.join(parent, 'SLDEA_20260805_101500')
    os.makedirs(d)
    _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
    # the run folder itself: newest_run cannot see it, resolve_run can
    assert se.newest_run(d) is None
    assert se.resolve_run(d) == d
    # a parent full of runs: both work, and agree
    assert se.newest_run(parent) == d
    assert se.resolve_run(parent) == d
    # and a folder that is neither still resolves to nothing
    empty = tempfile.mkdtemp(prefix='resolve_empty_')
    assert se.resolve_run(empty) is None


def _wrapper_fixture():
    """(wrapper, inner, [older, newer]) -- the campaign layout: runs one
    level down inside 'SLDEA_data (1)', with sibling folders that are not
    runs, exactly as SCPI_SLDEA_DIR sees it on the analysis PC."""
    wrapper = tempfile.mkdtemp(prefix='wrapper_')
    inner = os.path.join(wrapper, 'SLDEA_data (1)')
    older = os.path.join(inner, 'P3_1_2.5mL_20260728')
    newer = os.path.join(inner, 'P3_2_2.5mL_20260728')
    for d in (older, newer):
        os.makedirs(d)
        _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
    # the newest is decided by mtime, so push the other one back rather
    # than trusting two writes inside one clock tick to differ
    t = os.path.getmtime(newer)
    os.utime(older, (t - 60, t - 60))
    for junk in ('_analysis', '_baselines', os.path.join(inner, 'plots')):
        os.makedirs(os.path.join(wrapper, junk), exist_ok=True)
    return wrapper, inner, older, newer


def test_runs_parent_descends_one_level_into_a_campaign_wrapper():
    """`#261`: SCPI_SLDEA_DIR points at the campaign WRAPPER and the runs
    are nested one level down, where a direct listing correctly finds
    nothing -- so the Windows tuner launcher's --resolve step exited 2,
    "no run found", on a machine holding 13 runs.

    The rule is one level, only when the level above holds nothing."""
    wrapper, inner, older, newer = _wrapper_fixture()
    try:
        assert se.runs_parent(wrapper) == inner
        # a directory that holds runs itself is never rewritten
        assert se.runs_parent(inner) == inner
        # ...and both resolvers now answer for the wrapper, which is the fix
        assert se.newest_run(wrapper) == newer
        assert se.resolve_run(wrapper) == newer
        assert se.newest_run(inner) == newer     # unchanged, and it agrees

        # EXACTLY ONE LEVEL: a run two levels down does not move the parent
        deep = tempfile.mkdtemp(prefix='wrapper_deep_')
        d = os.path.join(deep, 'a', 'b', 'R1')
        os.makedirs(d)
        _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
        assert se.runs_parent(deep) == deep
        assert se.newest_run(deep) is None
        assert se.resolve_run(deep) is None
        shutil.rmtree(deep, ignore_errors=True)

        # nothing anywhere: the caller's own path back, never None -- the
        # error message has to be able to name it
        barren = tempfile.mkdtemp(prefix='wrapper_barren_')
        assert se.runs_parent(barren) == barren
        assert se.resolve_run(barren) is None
        os.makedirs(os.path.join(barren, 'notes'))
        assert se.runs_parent(barren) == barren
        shutil.rmtree(barren, ignore_errors=True)

        # unreadable / missing: the path back, and no exception
        assert se.runs_parent(os.path.join(barren, 'gone')) == \
            os.path.join(barren, 'gone')
    finally:
        shutil.rmtree(wrapper, ignore_errors=True)


def test_a_folder_that_is_a_run_wins_over_anything_nested_in_it():
    """The descent may never take the run out of the operator's hands: a
    named run resolves to ITSELF even when a copy sits inside it, because
    resolve_run tests run_csv before it looks down (`#261`)."""
    parent = tempfile.mkdtemp(prefix='wrapper_selfwins_')
    try:
        d = os.path.join(parent, 'SLDEA_20260805_101500')
        nested = os.path.join(d, 'copy_of_me')
        os.makedirs(nested)
        _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
        _fake_run(nested, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
        assert se.resolve_run(d) == d
        # runs_parent is where to LOOK, so on a run it stays on the run
        # itself -- it holds one, so there is nothing to descend for
        assert se.runs_parent(d) == d
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_the_descent_picks_the_child_holding_the_most_runs():
    """Ties and near-misses are decided here rather than by directory
    order, so two machines with the same share resolve the same run."""
    root = tempfile.mkdtemp(prefix='wrapper_pick_')
    try:
        for child, n in (('one_run', 1), ('three_runs', 3), ('none', 0)):
            os.makedirs(os.path.join(root, child), exist_ok=True)
            for i in range(n):
                d = os.path.join(root, child, f'R{i}')
                os.makedirs(d)
                _fake_run(d, [{'step': 0, 'tag': 'baseline',
                               'nominal_kV': '0'}])
        assert se.runs_parent(root) == os.path.join(root, 'three_runs')
        # a tie goes to the first by NAME, not to whatever listdir says
        tie = tempfile.mkdtemp(prefix='wrapper_tie_')
        for child in ('bbb', 'aaa'):
            d = os.path.join(tie, child, 'R0')
            os.makedirs(d)
            _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
        assert se.runs_parent(tie) == os.path.join(tie, 'aaa')
        shutil.rmtree(tie, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_electrode_mask_defaults_to_off_for_dark_electrodes():
    """255 = effectively off. The mask keys on BRIGHTNESS (copper/foil),
    but a carbon-black electrode is the darkest thing in frame, so the
    old 220 default masked paper and glint instead and broke detection
    outright (bench 2026-08-05). Conservative is the safe default; lower
    it per-run in the tuner for a bright electrode."""
    assert se.DEFAULT_SETTINGS['electrode_lum'] == 255.0
    # the two baseline-disc fallbacks must not re-introduce the old
    # default for a caller that omits the key
    import inspect
    src = inspect.getsource(se)
    assert "'electrode_lum', 220" not in src, \
        "a hardcoded 220 fallback is back — keep one source of truth"


def test_telemetry_sidecar_is_never_mistaken_for_the_run_csv():
    """The live runner now writes telemetry.csv beside data.csv (#189
    increment 2). It is a second CSV in every run folder, so pin that no
    reader can resolve to it: a run without data.csv is still not a run,
    and a run with both still loads its frame rows."""
    import sldea_profile as sp
    parent = tempfile.mkdtemp(prefix='runcsv_tel_')
    d = os.path.join(parent, 'SLDEA_20260805_120000')
    os.makedirs(d)
    with open(os.path.join(d, sp.TELEMETRY_FILENAME), 'w',
              newline='', encoding='utf-8') as f:
        f.write(','.join(sp.TELEMETRY_COLUMNS) + '\n0,ts,0,0,0,ok,ok,\n')
    assert se.run_csv(d) is None                  # sidecar alone != a run
    assert se.newest_run(parent) is None          # ...and not a run to open
    _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
    assert os.path.basename(se.run_csv(d)) == 'data.csv'
    assert len(se.load_run(d)['rows']) == 1


def test_write_back_updates_the_file_the_run_was_read_from():
    d = tempfile.mkdtemp(prefix='runcsv_wb_')
    _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0',
                   'frame_file': 'f0.png'}])
    os.rename(os.path.join(d, 'data.csv'), os.path.join(d, 'data3.csv'))
    run = se.load_run(d)
    run['rows'][0]['notes'] = 'edited'
    path = se.write_back(d, run)
    assert path.endswith('data3.csv'), path
    # no stray data.csv conjured beside it, and the backup follows the name
    assert not os.path.exists(os.path.join(d, 'data.csv'))
    assert os.path.exists(os.path.join(d, 'data3.csv.bak'))
    with open(path) as f:
        assert 'edited' in f.read()


def test_bench_shortcuts_resolve_and_stay_inert_elsewhere():
    """`1`, `2`, `3` stand in for the three P3 runs the bench reopens all
    day. On any machine without those paths the shortcut must resolve to
    nothing rather than to a wrong directory."""
    assert set(se.BENCH_RUNS) == {'1', '2', '3'}
    for key, path in se.BENCH_RUNS.items():
        assert 'P3_' + key in path, (key, path)
    # Holds on both kinds of machine. Where the runs are present -- the
    # bench PC, or anywhere they have been extracted for analysis -- a
    # shortcut resolves to its own directory; where they are absent it
    # resolves to nothing. What it must never do is resolve to some
    # OTHER run, which is what a bare newest_run() fallback would give.
    for key, path in se.BENCH_RUNS.items():
        assert se.resolve_run(key) in (None, path), (key, se.resolve_run(key))

    parent = tempfile.mkdtemp(prefix='shortcut_')
    d = os.path.join(parent, 'P3_9')
    os.makedirs(os.path.join(d, 'frames'), exist_ok=True)
    _fake_run(d, [{'step': 0, 'tag': 'baseline', 'nominal_kV': '0'}])
    saved = dict(se.BENCH_RUNS)
    try:
        se.BENCH_RUNS['9'] = d
        assert se.resolve_run('9') == d
        assert se.resolve_run(' 9 ') == d          # stray whitespace is fine
        se.BENCH_RUNS['8'] = os.path.join(parent, 'nope')
        assert se.resolve_run('8') is None         # points nowhere: not used
    finally:
        se.BENCH_RUNS.clear()
        se.BENCH_RUNS.update(saved)


def _crinkle(shape_hw, rng, lo=120.0, hi=255.0, cell=3):
    """Foil stand-in: dense blocky speckle spanning the strip's real range
    (P3: p10 144, median 173, p90 239 -- most of it DIMMER than paper)."""
    h, w = shape_hw
    r = rng.uniform(lo, hi, (int(np.ceil(h / cell)),
                             int(np.ceil(w / cell)))).astype(np.float32)
    return np.kron(r, np.ones((cell, cell), np.float32))[:h, :w]


def _bridged_scene(with_disc=True, seed=7):
    """The REAL P3 failure shape (2026-07-28): low-contrast disc, crinkled
    strips contacting it, smooth CNT traces and under-strip shadows
    bridging the dark class from strip to strip. Region-growing merged all
    of it into one 85%-of-frame blob at conf 0.93."""
    rng = np.random.default_rng(seed)
    h, w = 540, 960
    img = np.full((h, w), 190.0, np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    if with_disc:
        img[(xx - 480) ** 2 + (yy - 270) ** 2 <= 100 * 100] = 172.0
    img[230:310, 0:330] = _crinkle((80, 330), rng)      # left strip
    img[230:310, 630:960] = _crinkle((80, 330), rng)    # right strip
    img[262:278, 330:392] = 174.0                       # CNT traces bridge
    img[262:278, 568:630] = 174.0                       # ...to the disc
    img[310:352, 270:345] = 165.0                       # under-strip
    img[310:352, 615:690] = 165.0                       # shadows
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.float32)


def test_foil_mask_covers_crinkle_and_refuses_edges_and_flat():
    """Brightness cannot define the foil (median foil pixel 173 vs paper
    176-190); texture can -- but only texture that fills a REGION. A flat
    scene, sensor grain, and a hard-edged flat rectangle (whose rim is a
    band exactly one box window wide) must all yield an empty mask."""
    rng = np.random.default_rng(9)
    flat = np.full((480, 640), 180.0, np.float32)
    assert not se.foil_mask(flat).any()
    noisy = flat + rng.normal(0, 2.0, flat.shape).astype(np.float32)
    assert not se.foil_mask(noisy).any()
    rect = flat.copy()
    rect[200:280, 100:540] = 250.0
    assert not se.foil_mask(rect).any(), "a flat rectangle's rim is not foil"
    crink = flat.copy()
    crink[195:285, 60:580] = _crinkle((90, 520), rng)
    fo = se.foil_mask(crink)
    strip = np.zeros(crink.shape, bool)
    strip[195:285, 60:580] = True
    import cv2
    grown = cv2.dilate(strip.astype(np.uint8),
                       np.ones((71, 71), np.uint8)).astype(bool)
    assert fo[strip].mean() > 0.9, fo[strip].mean()
    assert not fo[~grown].any(), "foil mask leaked far off the strip"


def test_baseline_disc_survives_bridging_tendrils_and_shadows():
    """The exact failure the frames exposed: dark tendrils and shadows
    bridge the disc to both strips, so any region-grown dark class spans
    the frame. The radial trace must recover the disc itself -- and the
    px->mm scale with it."""
    base = _bridged_scene(with_disc=True)
    ref = se.baseline_disc(base, dict(se.DEFAULT_SETTINGS))
    assert ref is not None, "disc not found on the bridged scene"
    assert abs(ref['diam_px'] - 200) / 200 < 0.08, ref['diam_px']
    assert abs(ref['cx'] - 480) < 15 and abs(ref['cy'] - 270) < 15, \
        (ref['cx'], ref['cy'])
    assert ref['circ'] > 0.9, ref['circ']
    assert ref['conf'] >= 0.6, ref['conf']


def test_baseline_disc_refuses_when_there_is_no_disc():
    """Strips, tendrils and shadows but NO disc: the old code returned the
    merged blob at conf 0.93 and silently corrupted every mm^2 written.
    Refusal is the contract -- mm falls back loudly, not wrongly."""
    base = _bridged_scene(with_disc=False)
    assert se.baseline_disc(base, dict(se.DEFAULT_SETTINGS)) is None


def test_photometric_fit_mask_excludes_a_large_changed_region():
    """Q1 (bench 2026-07-28): run 3's gain dived 0.77->0.55 at 5.25 kV
    because the device sat inside its own fit region; restricting the fit
    to paper flattened it. The trim cannot save a fit whose changed
    region is too big -- the mask can."""
    yy, xx = np.mgrid[0:240, 0:240]
    base = (60.0 + 140.0 * xx / 239.0).astype(np.float32)
    img = np.clip(base * 0.78 + 12.0, 0, 255)
    disc = (xx - 120) ** 2 + (yy - 120) ** 2 <= 78 * 78   # ~46% of the ROI
    img[disc] = np.clip(img[disc] - 45, 0, 255)
    roi = (slice(18, 222), slice(18, 222))
    a_un, _b_un = se.photometric_fit(base, img, roi)
    mask = np.zeros(base.shape, bool)
    mask[roi] = True
    mask[disc] = False
    a_m, _b_m = se.photometric_fit(base, img, roi, mask=mask)
    assert abs(a_m - 0.78) < 0.05, a_m
    assert abs(a_un - 0.78) > 2 * abs(a_m - 0.78), (a_un, a_m)


def test_paper_mask_excludes_disc_and_foil():
    base = _bridged_scene(with_disc=True)
    pm = se._paper_mask(base, (360, 640), dict(se.DEFAULT_SETTINGS))
    assert pm is not None
    fo = se._foil_small(base, (640, 360))
    assert not (pm & fo).any(), "fit region overlaps the foil"
    assert not pm[180, 320], "fit region includes the disc centre"
    assert pm[40, 60], "paper inside the ROI must stay in the fit region"


def test_texture_channel_detects_pure_wrinkle_the_gate_would_drop():
    """The P3 activation mode: the disc WRINKLES with almost no intensity
    displacement, sep_intensity reads 0.000, and the downscaled diff sits
    under min_diff -- the honest gate then dropped frames that are
    visibly active. The texture channel must catch exactly this, and
    turning it off must restore the plain gate behaviour."""
    rng = np.random.default_rng(4)
    h, w = 720, 1280
    base = (np.full((h, w), 180.0, np.float32)
            + rng.normal(0, 1.5, (h, w)).astype(np.float32))
    yy, xx = np.mgrid[0:h, 0:w]
    img = base.copy()
    disc = (xx - 640) ** 2 + (yy - 360) ** 2 <= 150 * 150
    img[disc] += 10.0 * np.sin((xx[disc] + yy[disc]) / 2.8)
    img = np.clip(img, 0, 255).astype(np.float32)
    s = dict(se.DEFAULT_SETTINGS)
    prep = se.prepared_diff(base, img, s)
    assert float(np.percentile(prep['sub'], 99)) < float(s['min_diff']), \
        "scene not gated -- the wrinkle must be subtler for this test"
    cands = se.candidates(base, img, s)
    assert cands, "texture channel found nothing on a wrinkled disc"
    assert cands[0]['method'] == 'tex-ratio', cands[0]['method']
    assert abs(cands[0]['cx'] - 640) < 60 and abs(cands[0]['cy'] - 360) < 60
    s2 = dict(s)
    s2['tex_seg'] = 0
    assert se.candidates(base, img, s2) == [], \
        "with tex_seg off the gate must drop the frame as before"


def _bridged_pair(r_active=112, gain=0.9, offset=5.0, seed=7):
    """Baseline + activated frame of the bridged scene: the ink disc
    EXPANDS (the boundary feature is the ink edge itself), its interior
    ripples, the passive surround stays paper, and the whole frame sits
    at a photometric mismatch the detector must normalize away."""
    rng = np.random.default_rng(seed)
    base = _bridged_scene(with_disc=True, seed=seed)
    h, w = base.shape
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.full((h, w), 190.0, np.float32)
    disc = (xx - 480) ** 2 + (yy - 270) ** 2 <= r_active * r_active
    img[disc] = 172.0 + 6.0 * np.sin((xx[disc] + yy[disc]) / 2.5)
    img[230:310, 0:330] = base[230:310, 0:330]
    img[230:310, 630:960] = base[230:310, 630:960]
    img[262:278, 330:392] = 174.0
    img[262:278, 568:630] = 174.0
    img[310:352, 270:345] = 165.0
    img[310:352, 615:690] = 165.0
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    img = np.clip(img * gain + offset, 0, 255).astype(np.float32)
    return base, img


def test_disc_fit_tracks_the_moving_ink_edge():
    """The active area is the FULL responding disc, and its boundary
    feature is the ink edge on the normalized frame (2026-07-28 bench:
    at 4.25 kV the edge visibly moved ~80 px and the intensity profiles
    confirm it). The fit must recover the expanded radius from the known
    resting disc, at high confidence, with a tight CI -- and win."""
    base, img = _bridged_pair(r_active=112)
    s = dict(se.DEFAULT_SETTINGS)
    cands = se.candidates(base, img, s)
    assert cands, "no candidates on an expanding disc"
    best = cands[0]
    assert best['method'] == 'disc-fit', best['method']
    assert abs(best['diam_px'] - 224) / 224 < 0.06, best['diam_px']
    assert abs(best['cx'] - 480) < 12 and abs(best['cy'] - 270) < 12
    assert best['conf'] >= 0.75, best['conf']
    assert best['spread_pct'] == best['ci85_pct'] < 4.0, best
    assert not se.needs_review(cands, s)


def test_resting_candidate_states_the_known_area_on_gated_frames():
    """A gated frame with a known resting disc is not 'nothing': the
    honest measurement is that the area equals the resting area. It must
    auto-accept -- low-kV frames used to queue for review over frames
    that show no change at all."""
    rng = np.random.default_rng(11)
    base = _bridged_scene(with_disc=True)
    img = np.clip(base + rng.normal(0, 1.0, base.shape), 0,
                  255).astype(np.float32)
    s = dict(se.DEFAULT_SETTINGS)
    cands = se.candidates(base, img, s)
    assert len(cands) == 1 and cands[0]['method'] == 'resting', cands
    ref = se.baseline_disc(base, s)
    assert abs(cands[0]['area_px'] - ref['area_px']) < 1e-6
    assert cands[0]['conf'] >= 0.75
    assert not se.needs_review(cands, s)
    # and with no baseline disc there is nothing to state: gated frames
    # stay empty exactly as before (the no-change-gate test pins that)


def test_disc_fit_adaptive_cut_keeps_a_uniformly_faint_edge():
    """The P3 ink sits only 10-25 gray levels below paper and its top arc
    is fainter still; a fixed 4-level step cut drops those rays. The cut
    now adapts to the scene's own ink contrast: a uniformly faint edge
    (~3 levels) must still be tracked, because the scene says that IS
    the contrast, not noise."""
    rng = np.random.default_rng(7)
    base = _bridged_scene(with_disc=True, seed=7)
    h, w = base.shape
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.full((h, w), 190.0, np.float32)
    r_act = 112
    disc = (xx - 480) ** 2 + (yy - 270) ** 2 <= r_act * r_act
    img[disc] = 187.0                                  # 3 levels below paper
    core = (xx - 480) ** 2 + (yy - 270) ** 2 <= (0.8 * r_act) ** 2
    img[core] += 6.0 * np.sin((xx[core] + yy[core]) / 2.5)   # responding
    img[230:310, 0:330] = base[230:310, 0:330]
    img[230:310, 630:960] = base[230:310, 630:960]
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.float32)
    s = dict(se.DEFAULT_SETTINGS)
    prep = se.prepared_diff(base, img, s)
    ref = se.baseline_disc(base, s)
    c = se._disc_fit_candidate(prep, s, ref)
    assert c is not None, "faint ink edge lost"
    f = prep['base_small'].shape[1] / float(base.shape[1])
    diam_full = c['diam_px'] / f
    assert abs(diam_full - 2 * r_act) / (2 * r_act) < 0.10, diam_full


def test_hysteresis_bonus_favors_the_incumbent_channel():
    """Single-frame tier flips between near-tied channels caused most
    same-kV pair mismatches. The incumbent method gets +0.05 -- visible,
    tagged, and only when that channel produced a candidate at all."""
    base, img = _bridged_pair(r_active=112)
    s = dict(se.DEFAULT_SETTINGS)
    plain = se.candidates(base, img, s)
    held = se.candidates(base, img, s, prev_method='disc-fit')
    p = next(c for c in plain if c['method'] == 'disc-fit')
    hcand = next(c for c in held if c['method'] == 'disc-fit')
    assert abs(hcand['conf'] - min(0.99, p['conf'] + 0.05)) < 1e-9
    assert hcand.get('hyst_bonus') == 0.05
    # and the boundary fit leads in both calls: a contained tex patch is
    # supporting evidence for the recorded area, never the better answer
    assert plain[0]['method'] == 'disc-fit', plain[0]['method']
    assert held[0]['method'] == 'disc-fit', held[0]['method']
    tex = next((c for c in plain if c['method'] == 'tex-ratio'), None)
    assert tex is not None and tex.get('capped_by') == 'disc-fit'
    # a previous method with no candidate this frame changes nothing
    ghost = se.candidates(base, img, s, prev_method='no-such-channel')
    assert [c['conf'] for c in ghost] == [c['conf'] for c in plain]


def test_contained_diff_patch_cannot_outrank_the_boundary_fit():
    """Operator spot-read (155425 @ 5.25 kV post, 2026-07-29): the diff
    tiers outlined a strong interior patch at half the fit's area and
    outranked the correct disc-fit boundary. Per the active-area ruling
    the recorded area is the BOUNDARY's, so a diff patch contained in a
    valid fit is supporting evidence, never the better answer -- the
    same containment cap the tex channel already gets. The failure
    shape: the disc expands with a faint (but trackable) ink edge while
    the diff is dominated by a compact dark patch at its centre."""
    rng = np.random.default_rng(7)
    base = _bridged_scene(with_disc=True, seed=7)
    h, w = base.shape
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.full((h, w), 190.0, np.float32)
    r_act = 112
    img[(xx - 480) ** 2 + (yy - 270) ** 2 <= r_act * r_act] = 183.0
    img[(xx - 480) ** 2 + (yy - 270) ** 2 <= 55 * 55] = 140.0
    img[230:310, 0:330] = base[230:310, 0:330]
    img[230:310, 630:960] = base[230:310, 630:960]
    img[262:278, 330:392] = 174.0
    img[262:278, 568:630] = 174.0
    img[310:352, 270:345] = 165.0
    img[310:352, 615:690] = 165.0
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.float32)
    s = dict(se.DEFAULT_SETTINGS)
    cands = se.candidates(base, img, s)
    best = cands[0]
    assert best['method'] == 'disc-fit', [c['method'] for c in cands]
    exp = np.pi * r_act * r_act
    assert abs(best['area_px'] - exp) / exp < 0.08, best['area_px']
    # the patch tier is still offered (rank kept), but tagged and capped
    # just below the fit -- the tag certifies it OUTRANKED the fit before
    # the cap, exactly the 5.25 kV shape
    patch = [c for c in cands if c['method'].startswith('diff')
             and c['area_px'] <= 0.5 * best['area_px']]
    assert patch, [(c['method'], c['area_px']) for c in cands]
    for c in patch:
        assert c.get('capped_by') == 'disc-fit', c
        assert c['conf'] == round(best['conf'] - 0.01, 3), c
    # and the incumbent's +0.05 cannot ride a contained patch back over
    # the boundary: hysteresis applies before the cap
    held = se.candidates(base, img, s, prev_method=patch[0]['method'])
    assert held[0]['method'] == 'disc-fit', [c['method'] for c in held]


def test_reconcile_pairs_boosts_agreement_and_caps_contradiction():
    rows = [{'nominal_kV': '1'}, {'nominal_kV': '1'},
            {'nominal_kV': '2'}, {'nominal_kV': '2'}]
    cands = {
        0: [{'method': 'disc-fit', 'area_px': 100000.0, 'conf': 0.80,
             'ci85_pct': 0.5}],
        1: [{'method': 'disc-fit', 'area_px': 101000.0, 'conf': 0.78,
             'ci85_pct': 0.5}],
        2: [{'method': 'disc-fit', 'area_px': 140000.0, 'conf': 0.92,
             'ci85_pct': 0.4}],
        3: [{'method': 'diff-lo', 'area_px': 60000.0, 'conf': 0.88,
             'ci85_pct': None}],
    }
    stats = se.reconcile_pairs(rows, cands, dict(se.DEFAULT_SETTINGS))
    assert stats == {'confirmed': 2, 'capped': 2}, stats
    assert cands[0][0]['conf'] == 0.85 and cands[0][0]['pair_confirmed']
    assert cands[1][0]['conf'] == 0.83
    # the contradiction can no longer auto-accept on either side
    acc = se.DEFAULT_SETTINGS['accept_conf']
    assert cands[2][0]['conf'] == round(acc - 0.01, 3)
    assert cands[3][0]['conf'] == round(acc - 0.01, 3)
    assert cands[2][0]['pair_mismatch_pct'] == cands[3][0]['pair_mismatch_pct'] == 80.0


def test_audit_boundary_measures_bias_and_circled_noise():
    """The self-audit is the correctness check conf cannot provide: a
    true boundary audits to ~0 bias; the same boundary shifted +5 px
    audits to ~+5; a boundary with nothing under it reports a large
    no-step arc ('circled the noise')."""
    base, img = _bridged_pair(r_active=112)
    s = dict(se.DEFAULT_SETTINGS)
    prep = se.prepared_diff(base, img, s)
    cands = se.candidates(base, img, s)
    best = next(c for c in cands if c['method'] == 'disc-fit')
    a = se.audit_boundary(prep, best, s)
    assert a and a['bias_px'] is not None
    assert abs(a['bias_px']) < 2.0, a
    assert a['nostep_pct'] <= 10.0, a
    shifted = dict(best)
    shifted['contour'] = (np.asarray(best['contour'], float)
                          - [best['cx'], best['cy']]) * (117.0 / 112.0) \
        + [best['cx'], best['cy']]
    a2 = se.audit_boundary(prep, shifted, s)
    assert a2 and a2['bias_px'] is not None
    assert 3.0 < a2['bias_px'] < 7.0, a2
    rng = np.random.default_rng(3)
    flat = np.clip(np.full(base.shape, 190.0, np.float32)
                   + rng.normal(0, 1.5, base.shape), 0,
                   255).astype(np.float32)
    prep3 = se.prepared_diff(base, flat, s)
    a3 = se.audit_boundary(prep3, best, s)
    assert a3 and a3['nostep_pct'] > 50.0, a3


def _washed_arc_pair(wedge_deg=45.0):
    """The onset failure shape (self-audit 2026-07-29, runs 152205 /
    155425): the disc responds, but over a wedge of the boundary the ink
    washes out entirely -- no dark->light step anywhere near the fitted
    radius -- and the ellipse INTERPOLATES that arc from the sectors it
    could measure."""
    base, img = _bridged_pair(r_active=112)
    h, w = img.shape
    yy, xx = np.mgrid[0:h, 0:w]
    ang = np.degrees(np.arctan2(yy - 270.0, xx - 480.0))
    rr = np.hypot(xx - 480.0, yy - 270.0)
    wedge = (np.abs(ang + 90.0) <= wedge_deg) & (rr >= 40) & (rr <= 170)
    img = img.copy()
    img[wedge] = 190.0 * 0.9 + 5.0     # ink -> paper, same photometry
    return base, img


def test_audit_nostep_caps_acceptance_on_interpolated_arc():
    """The self-audit is folded into ACCEPTANCE: a winning boundary with
    no measurable ink step under > audit_nostep_pct of its arc keeps its
    rank and area (it is still the best measurement on offer) but loses
    the right to auto-accept -- capped below accept_conf, tagged, sent
    to review. Exactly the interpolated-arc onset frames."""
    base, img = _washed_arc_pair()
    s = dict(se.DEFAULT_SETTINGS)
    cands = se.candidates(base, img, s)
    best = cands[0]
    assert best['method'] == 'disc-fit', best['method']
    aud = best.get('audit')
    assert aud and aud['nostep_pct'] > 15.0, aud
    # no feature bias where the step exists -- the arc is the problem
    assert aud['bias_px'] is not None and abs(aud['bias_px']) < 2.0, aud
    assert best.get('audit_nostep') == aud['nostep_pct']
    assert best['conf'] == round(s['accept_conf'] - 0.01, 3), best['conf']
    assert se.needs_review(cands, s)
    # the cap, not a weak fit, is what forces review: with the gate off
    # the same frame auto-accepts on the same fit
    s0 = dict(se.DEFAULT_SETTINGS)
    s0['audit_nostep_pct'] = 0.0
    c0 = se.candidates(base, img, s0)
    assert c0[0]['method'] == 'disc-fit'
    assert c0[0].get('audit_nostep') is None
    assert c0[0]['conf'] >= s0['accept_conf'], c0[0]['conf']
    assert not se.needs_review(c0, s0)
    # a clean boundary carries its audit but no tag, and still accepts
    cb, ci = _bridged_pair(r_active=112)
    cc = se.candidates(cb, ci, s)
    assert cc[0]['method'] == 'disc-fit'
    a2 = cc[0].get('audit')
    assert a2 and a2['nostep_pct'] <= 10.0, a2
    assert cc[0].get('audit_nostep') is None
    assert not se.needs_review(cc, s)


def _crept_resting_scene():
    """The stale-resting scene: the disc has crept from r=100 to r=106
    -- a shift far below what the downscaled diff can gate on when
    min_diff is raised to match a noisy bench scene, but plainly
    visible to the boundary audit. -> (base, img, settings)"""
    base = _bridged_scene(with_disc=True)
    h, w = base.shape
    yy, xx = np.mgrid[0:h, 0:w]
    rng = np.random.default_rng(5)
    img = np.full((h, w), 190.0, np.float32)
    img[(xx - 480) ** 2 + (yy - 270) ** 2 <= 106 * 106] = 172.0
    img[230:310, 0:330] = base[230:310, 0:330]
    img[230:310, 630:960] = base[230:310, 630:960]
    img[262:278, 330:392] = 174.0
    img[262:278, 568:630] = 174.0
    img[310:352, 270:345] = 165.0
    img[310:352, 615:690] = 165.0
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.float32)
    s = dict(se.DEFAULT_SETTINGS)
    s['min_diff'] = 30.0                 # the gate stays blind to the creep
    return base, img, s


def test_bias_tripped_resting_is_refit_to_the_moved_edge():
    """On all six bench runs the 1.5-3 kV band audits at bias -4..-11 px
    while auto-accepting 'area = resting area' -- the disc expands below
    the no-change gate's sensitivity and the claim goes stale. Since
    calibration round 4 verified the creep is real (operator traces:
    +4.0/+6.5% beyond the definitional baseline, matching the audit's
    prediction), a bias trip REFITS: the fitter tracks the ink step
    where it actually is, is audited itself, and takes the frame; the
    capped resting claim stays as the tagged runner-up."""
    base, img, s = _crept_resting_scene()
    cands = se.candidates(base, img, s)
    best = cands[0]
    assert best['method'] == 'disc-fit' and best.get('resting_refit'), \
        [(c['method'], c['conf']) for c in cands]
    # the fit measured the crept edge, not the stale circle
    r_meas = best['diam_px'] / 2.0
    assert 103.0 <= r_meas <= 109.0, r_meas       # true creep: 100 -> 106
    ref = se.baseline_disc(base, s)
    assert best['area_px'] > 1.06 * ref['area_px']
    # audited clean on its own boundary -> the frame leaves review
    assert best.get('audit') and best.get('audit_bias') is None, best
    assert best['conf'] >= s['accept_conf'], best['conf']
    assert not se.needs_review(cands, s)
    # the stale claim survives as the capped, tagged runner-up
    rest = next(c for c in cands if c['method'] == 'resting')
    assert rest.get('audit_bias') is not None and rest['audit_bias'] < -3.0
    assert rest['conf'] == round(s['accept_conf'] - 0.01, 3), rest['conf']
    # with the bias gate off the stale claim auto-accepts unchallenged --
    # the gate is what makes the refit possible at all
    s0 = dict(s)
    s0['audit_bias_px'] = 0.0
    c0 = se.candidates(base, img, s0)
    assert c0[0]['method'] == 'resting'
    assert c0[0].get('audit_bias') is None
    assert c0[0]['conf'] >= s0['accept_conf'], c0[0]['conf']
    assert not se.needs_review(c0, s0)
    assert not any(c.get('resting_refit') for c in c0)


def test_refit_refusal_keeps_the_capped_resting_claim():
    """A refused refit (washed edge, blocked arc -- anything the fitter
    honestly cannot measure) must change nothing: the stale claim stays
    capped in review exactly as before the refit existed. Pinned by
    refusing only the assume_responding call."""
    base, img, s = _crept_resting_scene()
    orig = se._disc_fit_candidate

    def refuse(prep, settings, ref, assume_responding=False):
        if assume_responding:
            return None
        return orig(prep, settings, ref, assume_responding)

    se._disc_fit_candidate = refuse
    try:
        cands = se.candidates(base, img, s)
    finally:
        se._disc_fit_candidate = orig
    best = cands[0]
    assert best['method'] == 'resting', best['method']
    aud = best.get('audit')
    assert aud and aud['bias_px'] is not None and aud['bias_px'] < -3.0, aud
    assert best.get('audit_bias') == aud['bias_px']
    assert best['conf'] == round(s['accept_conf'] - 0.01, 3), best['conf']
    assert se.needs_review(cands, s)
    assert not any(c.get('resting_refit') for c in cands)


def test_pair_agreement_cannot_lift_an_audit_capped_boundary():
    """Two snapshots interpolated over the same washed-out arc agree
    beautifully -- correlated error, the one thing pair agreement cannot
    certify against. The +0.05 confirmation bonus must not carry an
    audit_nostep frame back over accept_conf."""
    rows = [{'nominal_kV': '5'}, {'nominal_kV': '5'}]
    acc = se.DEFAULT_SETTINGS['accept_conf']
    capped = round(acc - 0.01, 3)
    cands = {
        0: [{'method': 'disc-fit', 'area_px': 100000.0, 'conf': capped,
             'ci85_pct': 0.5, 'audit_nostep': 26.8}],
        1: [{'method': 'disc-fit', 'area_px': 100500.0, 'conf': 0.93,
             'ci85_pct': 0.5}],
    }
    stats = se.reconcile_pairs(rows, cands, dict(se.DEFAULT_SETTINGS))
    assert stats['confirmed'] == 2, stats
    assert cands[0][0]['pair_confirmed'] and cands[1][0]['pair_confirmed']
    # the audited member stays below accept; its clean partner still gains
    assert cands[0][0]['conf'] == capped, cands[0][0]['conf']
    assert cands[1][0]['conf'] == 0.98, cands[1][0]['conf']


def test_ramp_consistency_flags_pairs_and_dips():
    rows = [{'nominal_kV': '1'}, {'nominal_kV': '1'},
            {'nominal_kV': '2'}, {'nominal_kV': '2'},
            {'nominal_kV': '3'}, {'nominal_kV': '3'}]
    results = {0: {'area_px': 100.0}, 1: {'area_px': 102.0},
               2: {'area_px': 140.0}, 3: {'area_px': 90.0},
               4: {'area_px': 60.0}, 5: {'area_px': 61.0}}
    annos = se.ramp_consistency(rows, results)
    assert 2 in annos and 'pair mismatch' in annos[2]
    assert 3 in annos and 'pair mismatch' in annos[3]
    assert 4 in annos and 'dip' in annos[4]
    assert 0 not in annos and 1 not in annos
    # agreeing, monotone results raise nothing
    ok = {i: {'area_px': 100.0 + 10 * (i // 2)} for i in range(6)}
    assert se.ramp_consistency(rows, ok) == {}


def test_load_run_says_what_is_missing():
    d = tempfile.mkdtemp(prefix='runcsv_none_')
    try:
        se.load_run(d)
    except FileNotFoundError as e:
        assert 'data1.csv' in str(e), str(e)     # names the rename it accepts
    else:
        raise AssertionError("load_run accepted a directory with no CSV")


# ---------------------------------------------------------------------------
# audit 2026-08-05 regressions
# ---------------------------------------------------------------------------

def test_partial_resave_carries_exactly_one_scale():
    """CRITICAL (audit 2026-08-05): a Save that leaves rows unreviewed
    used to keep the PREVIOUS session's mm² on them while rewriting the
    rest at the new manual anchor — two absolute scales in one column,
    a 56.1% artificial area step on the real-data repro, invisible to
    breakdown_flags and sldea_plot (both read px). Every row's mm² must
    imply THE SAME mm/px after any save."""
    old_scale = 0.04306                 # the shipped 155425 anchor
    new_scale = 0.05333                 # the audit's session-2 anchor
    rows = []
    for px in (136439.0, 136679.0, 134304.0, 132715.0, 154688.0):
        rows.append({'tag': 'post-ramp', 'nominal_kV': '3.5',
                     'active_area_px': f"{px:.0f}",
                     'active_area_mm2': f"{px * old_scale ** 2:.3f}",
                     'active_diam_mm':
                         f"{2 * (px / np.pi) ** 0.5 * old_scale:.3f}",
                     'wrinkle_idx': '1.20',
                     'notes': 'edge:disc-fit conf 0.91'})
    # a bug-era row: mm2 with NO px — unscalable, must be blanked, not
    # left on a foreign anchor
    rows.append({'tag': 'post-ramp', 'nominal_kV': '4.0',
                 'active_area_px': '', 'active_area_mm2': '123.456',
                 'active_diam_mm': '9.999', 'wrinkle_idx': '',
                 'notes': ''})
    # this session re-reviews only rows 0 and 1
    results = {0: {'area_px': 140000.0, 'diam_px': 422.2, 'conf': 0.9,
                   'method': 'disc-fit', 'wrinkle': 1.4},
               1: None}
    se.apply_results(rows, results, new_scale, {})
    implied = []
    for r in rows:
        px = float(r['active_area_px']) if r['active_area_px'] else None
        mm2 = (float(r['active_area_mm2'])
               if r['active_area_mm2'] else None)
        if px and mm2:
            implied.append((mm2 / px) ** 0.5)
    assert implied, "no scaled rows survived"
    assert all(abs(s - new_scale) / new_scale < 1e-4 for s in implied), \
        f"mixed scales in one column: {implied}"
    # untouched rows keep their px (real measurements are preserved)...
    assert rows[2]['active_area_px'] == '134304'
    # ...their diam rescaled consistently with the mm²...
    d = float(rows[2]['active_diam_mm'])
    want = 2 * (134304.0 / np.pi) ** 0.5 * new_scale
    assert abs(d - want) / want < 1e-3, (d, want)
    # ...their notes/wrinkle untouched (they describe the kept px)...
    assert rows[2]['notes'] == 'edge:disc-fit conf 0.91'
    assert rows[2]['wrinkle_idx'] == '1.20'
    # ...and the unscalable bug-era row is blanked, not kept foreign
    assert rows[5]['active_area_mm2'] == ''
    assert rows[5]['active_diam_mm'] == ''


def test_candidates_refuse_without_baseline():
    """CRITICAL (audit 2026-08-05): with base_gray None, prepared_diff's
    fallback thresholded the raw photograph — every honest channel
    refused and the Otsu tiers auto-accepted the ROI *background* at
    conf 0.85-0.90 (2.74x the true area). No baseline, no candidates."""
    img = _disc_frame(40)               # a scene the tiers WOULD outline
    assert se.candidates(None, img, dict(se.DEFAULT_SETTINGS)) == []


def test_accepted_result_without_wrinkle_blanks_stale_wrinkle_idx():
    """audit 2026-08-05: a re-measured row whose new result carries no
    wrinkle (e.g. 'resting') used to keep the previous pass's
    wrinkle_idx next to the new area."""
    rows = [{'active_area_px': '100', 'wrinkle_idx': '1.90', 'notes': ''}]
    se.apply_results(rows, {0: {'area_px': 100.0, 'diam_px': 11.3,
                                'conf': 0.9, 'method': 'resting',
                                'wrinkle': None}}, None, {})
    assert rows[0]['wrinkle_idx'] == ''


def test_norm_bg_affine_survives_roi_frac_full_frame():
    """audit 2026-08-05: bx/by gated ALL photometric normalization, so
    roi_frac=1.0 — the tuner slider's own maximum — silently disabled
    the affine fit and the residual pedestal auto-accepted a 5.3x
    outline. Only the legacy scalar needs the border band. Same scene as
    test_affine_norm_survives_a_gain_and_offset_baseline_mismatch."""
    yy, xx = np.mgrid[0:240, 0:240]
    base = (60.0 + 140.0 * xx / 239.0).astype(np.float32)
    base[:, 8:20] = 235.0
    img = base.copy()
    img[(xx - 120) ** 2 + (yy - 120) ** 2 <= 40 * 40] += 30
    img = np.clip(img * 0.78 + 12.0, 0, 255)
    truth = np.pi * 40 * 40
    areas = {}
    for rf, mode in ((0.85, 2), (1.0, 2), (1.0, 0)):
        s = dict(se.DEFAULT_SETTINGS)
        s['roi_frac'] = rf
        s['norm_bg'] = mode
        cands = se.candidates(base, img, s)
        areas[(rf, mode)] = cands[0]['area_px'] if cands else 0.0
        if mode == 2:
            assert cands, f"no candidates at roi_frac {rf}"
            err = abs(cands[0]['area_px'] - truth) / truth
            assert err < 0.15, (rf, cands[0]['method'],
                                cands[0]['area_px'], truth)
    # and norm_bg=0 is still genuinely OFF — the pedestal-driven outline
    # differs grossly from the corrected one, pinning the modes apart
    off = areas[(1.0, 0)]
    assert off == 0.0 or abs(off - truth) / truth > 0.5, \
        f"norm_bg=0 indistinguishable from the affine fit: {off}"


def test_wrinkle_index_is_photometric_gain_invariant():
    """audit 2026-08-05: the GUI's hand-trace path measured the wrinkle
    index on the RAW frame; |Laplacian| is linear in gain, so traced
    rows carried the correct value times the run's photometric gain
    (20-30% off on the P3 campaign) while machine rows were normalized —
    one column, two normalizations. se.wrinkle_index must measure like
    the detector."""
    yy, xx = np.mgrid[0:240, 0:240]
    # multi-pixel baseline texture (survives the wrinkle blur) so the
    # ratio has a real denominator and stays below its 9.99 cap
    base = np.clip(150 + 6 * np.sin(2 * np.pi * xx / 14)
                   * np.sin(2 * np.pi * yy / 14), 0,
                   255).astype(np.float32)
    disc = (xx - 120) ** 2 + (yy - 120) ** 2 <= 50 * 50
    img = base.copy()
    img[disc] += (14 * np.sin(2 * np.pi * xx / 11)
                  * np.sin(2 * np.pi * yy / 11))[disc]
    img = np.clip(img, 0, 255)
    gained = np.clip(0.75 * img + 20, 0, 255)   # the P3-style mismatch
    contour = np.array([[75, 75], [165, 75], [165, 165], [75, 165]],
                       np.int32)
    s = dict(se.DEFAULT_SETTINGS)
    w_plain = se.wrinkle_index(base, img, contour, s)
    w_gained = se.wrinkle_index(base, gained, contour, s)
    raw_plain = se._wrinkle_ratio(base, img, contour)
    raw_gained = se._wrinkle_ratio(base, gained, contour)
    assert 1.3 < w_plain < 9.0, w_plain
    # the bug's mechanism: |Laplacian| is linear in gain, so the RAW
    # ratio carries the photometric gain as a multiplicative error
    assert abs(raw_gained / raw_plain - 0.75) < 0.03, (raw_plain,
                                                       raw_gained)
    # the fix: the normalized index shrugs the same gain off
    assert abs(w_gained - w_plain) / w_plain < 0.10, (w_plain, w_gained)


def test_breakdown_branding_heals_both_directions():
    """audit 2026-08-05: (a) frame_file was rewritten to a _BREAKDOWN
    name even when the file was missing everywhere — a permanently
    dangling link, live on the shipped 155425 row 48; (b) the
    CSV-branded/disk-unbranded state short-circuited before any disk
    check and was unrepairable in either direction."""
    d = tempfile.mkdtemp(prefix='edge_heal_')
    try:
        names = ['b.png', 'f1.png', 'f2.png', 'f3.png']
        _fake_run(d, [{'snapshot': i + 1, 'step': i,
                       'tag': 'baseline' if i == 0 else 'post',
                       'nominal_kV': str(float(i)), 'frame_file': n}
                      for i, n in enumerate(names)])
        for n in ('b.png', 'f1.png', 'f2.png'):   # f3 missing on disk
            open(os.path.join(d, 'frames', n), 'wb').write(b'x')
        run = se.load_run(d)
        # the 155425 state: CSV branded, disk plain, flag confirmed
        run['rows'][2]['frame_file'] = 'f2_BREAKDOWN.png'
        renamed = se.mark_breakdown_files(run, {2: 'breakdown? terminal'})
        assert renamed == 1                     # f2 healed FORWARD
        assert os.path.exists(os.path.join(d, 'frames',
                                           'f2_BREAKDOWN.png'))
        assert run['rows'][2]['frame_file'] == 'f2_BREAKDOWN.png'
        # f3 is missing under BOTH names: frame_file must stay put,
        # never pointed at a ghost
        assert run['rows'][3]['frame_file'] == 'f3.png'
        assert 'post-breakdown' in run['rows'][3]['notes']
        # retraction: flags empty -> disk un-branded, links + notes clean
        renamed = se.mark_breakdown_files(run, {})
        assert renamed == 1
        assert os.path.exists(os.path.join(d, 'frames', 'f2.png'))
        assert run['rows'][2]['frame_file'] == 'f2.png'
        assert 'post-breakdown' not in (run['rows'][3]['notes'] or '')
        # dangling branded CSV name + plain file on disk + no flags ->
        # the link heals back without a rename
        run['rows'][1]['frame_file'] = 'f1_BREAKDOWN.png'
        run['rows'][1]['notes'] = ('edge:disc-fit conf 0.96; '
                                   'post-breakdown')
        assert se.mark_breakdown_files(run, {}) == 0
        assert run['rows'][1]['frame_file'] == 'f1.png'
        assert run['rows'][1]['notes'] == 'edge:disc-fit conf 0.96'
        # REVERSE orphan (review 2026-08-05): CSV plain while the disk
        # holds only the branded twin (an un-brand whose CSV committed
        # but whose rename failed). This direction had NO repair path —
        # the row read UNREADABLE forever while the code promised
        # 'self-heals in either direction'.
        os.replace(os.path.join(d, 'frames', 'f1.png'),
                   os.path.join(d, 'frames', 'f1_BREAKDOWN.png'))
        assert se.mark_breakdown_files(run, {}) == 1
        assert os.path.exists(os.path.join(d, 'frames', 'f1.png'))
        assert run['rows'][1]['frame_file'] == 'f1.png'
        # no-clobber (review 2026-08-05): when BOTH twins exist, a brand
        # fixes the link only — renaming would overwrite the branded
        # file's bytes, violating 'renamed, never deleted'
        open(os.path.join(d, 'frames', 'f2_BREAKDOWN.png'),
             'wb').write(b'branded-bytes')
        assert se.mark_breakdown_files(run, {2: 'breakdown? x'}) == 0
        assert run['rows'][2]['frame_file'] == 'f2_BREAKDOWN.png'
        assert open(os.path.join(d, 'frames', 'f2_BREAKDOWN.png'),
                    'rb').read() == b'branded-bytes'
        assert os.path.exists(os.path.join(d, 'frames', 'f2.png'))
    finally:
        shutil.rmtree(d)


def test_scale_anchor_roundtrip_and_mutual_preservation():
    """audit 2026-08-05: the mandatory manual anchor was a pair of
    clicks recorded NOWHERE — manual- and auto-anchored runs were
    indistinguishable on disk and no session could reproduce another's
    absolute mm². The anchor persists in setup.txt, survives
    save_settings, and loads shaped for mm_per_px."""
    d = tempfile.mkdtemp(prefix='edge_anchor_')
    try:
        with open(os.path.join(d, 'setup.txt'), 'w') as f:
            f.write("SLDEA Test\nDEA nominal diameter: 16 mm\n")
        assert se.load_scale_anchor(d) is None
        se.save_scale_anchor(d, {
            'method': 'manual-calibration', 'diam_px': 371.5,
            'diam_mm': 16.0, 'mm_per_px': 0.043062,
            'anchor_frame': 'SLDEA_s00_00.00kV_baseline.png',
            'anchor_is_baseline': True, 'auto_diam_px': 371.0})
        a = se.load_scale_anchor(d)
        assert a and a['diam_px'] == 371.5
        assert a['method'] == 'manual-calibration'
        assert a['anchor_is_baseline'] is True
        assert a['anchor_frame'] == 'SLDEA_s00_00.00kV_baseline.png'
        assert 'saved' in a
        # loadable anchor IS a usable mm_per_px reference
        scale = se.mm_per_px({}, [], {'diam_mm': 16.0}, baseline_ref=a)
        assert abs(scale - 16.0 / 371.5) < 1e-12
        # save_settings must not eat the block; a re-save replaces it
        se.save_settings(d, dict(se.DEFAULT_SETTINGS))
        assert se.load_scale_anchor(d)['diam_px'] == 371.5
        se.save_scale_anchor(d, {'method': 'manual-calibration',
                                 'diam_px': 380.0, 'diam_mm': 16.0,
                                 'mm_per_px': 16.0 / 380.0})
        text = open(os.path.join(d, 'setup.txt'),
                    encoding='utf-8').read()
        assert text.count(se.ANCHOR_HDR) == 1
        assert text.count(se.EDGE_HDR) == 1
        assert se.load_scale_anchor(d)['diam_px'] == 380.0
        assert 'DEA nominal diameter' in text
        # settings still round-trip cleanly around it
        s = se.load_settings(d)
        assert s['blur_px'] == se.DEFAULT_SETTINGS['blur_px']
    finally:
        shutil.rmtree(d)


def test_setup_txt_with_non_ascii_never_raises():
    """audit 2026-08-05: a hand-annotated setup.txt with any byte cp1252
    cannot decode (an emoji, pasted lab-notebook text) raised
    UnicodeDecodeError out of save_settings / _diam_recorded — the
    latter mid-dialog-build, permanently bricking the scale gate.
    Every setup.txt reader/writer must survive it."""
    d = tempfile.mkdtemp(prefix='edge_uni_')
    try:
        with open(os.path.join(d, 'setup.txt'), 'w',
                  encoding='utf-8') as f:
            f.write("SLDEA Test \U0001F4CF ruler\n"
                    "DEA nominal diameter: 16 mm\n")
        s = se.load_settings(d)
        assert s['diam_mm'] == 16.0
        se.save_settings(d, s)                   # must not raise
        se.save_scale_anchor(d, {'method': 'manual-calibration',
                                 'diam_px': 100.0, 'diam_mm': 16.0,
                                 'mm_per_px': 0.16})
        assert se.load_scale_anchor(d)['diam_px'] == 100.0
        assert se.load_settings(d)['diam_mm'] == 16.0
    finally:
        shutil.rmtree(d)


def test_writers_are_atomic_under_replace_failure():
    """audit 2026-08-05 (mutation finding): replacing tmp+os.replace
    with an in-place truncating write survived every test. Pin the
    atomicity: when os.replace raises, the destination is byte-
    identical and the payload sits in a .tmp — proof the write never
    touched the destination."""
    d = tempfile.mkdtemp(prefix='edge_atomic_')
    try:
        _fake_run(d, [{'snapshot': 1, 'step': 0, 'tag': 'baseline',
                       'nominal_kV': '0', 'frame_file': 'b.png',
                       'notes': 'precious'}])
        with open(os.path.join(d, 'setup.txt'), 'w') as f:
            f.write("SLDEA Test\nDEA nominal diameter: 16 mm\n")
        run = se.load_run(d)
        run['rows'][0]['notes'] = 'edited'
        csv_before = open(run['csv_path'], 'rb').read()
        setup_before = open(os.path.join(d, 'setup.txt'), 'rb').read()

        real_replace = se.os.replace

        def boom(src, dst):
            raise OSError(28, 'No space left on device')

        se.os.replace = boom
        try:
            for call in (lambda: se.write_back(d, run),
                         lambda: se.save_settings(
                             d, dict(se.DEFAULT_SETTINGS)),
                         lambda: se.save_scale_anchor(
                             d, {'method': 'manual-calibration',
                                 'diam_px': 100.0})):
                try:
                    call()
                except OSError:
                    pass
                else:
                    raise AssertionError("writer swallowed the failure")
        finally:
            se.os.replace = real_replace
        assert open(run['csv_path'], 'rb').read() == csv_before
        assert open(os.path.join(d, 'setup.txt'),
                    'rb').read() == setup_before
        assert os.path.exists(run['csv_path'] + '.tmp')
        assert os.path.exists(os.path.join(d, 'setup.txt.tmp'))
    finally:
        shutil.rmtree(d)


def test_write_back_extends_columns_for_era_rows():
    """audit 2026-08-05: apply_results writes wrinkle_idx into rows a
    07-23-era 14-column CSV never declared, and DictWriter then raised
    ValueError MID-SAVE (after the renames, before the CSV). write_back
    now inserts unknown keys before 'notes' instead."""
    d = tempfile.mkdtemp(prefix='edge_era_')
    try:
        _fake_run(d, [{'snapshot': 1, 'step': 0, 'tag': 'baseline',
                       'nominal_kV': '0', 'frame_file': 'b.png'}])
        run = se.load_run(d)
        assert 'wrinkle_idx' not in run['columns']    # 14-col era fixture
        se.apply_results(run['rows'],
                         {0: {'area_px': 100.0, 'diam_px': 11.3,
                              'conf': 0.9, 'method': 'diff-hi',
                              'wrinkle': 1.7}}, None, {})
        path = se.write_back(d, run)                  # must not raise
        with open(path, newline='') as f:
            r = csv.DictReader(f)
            cols = r.fieldnames
            row = next(iter(r))
        assert 'wrinkle_idx' in cols
        assert cols.index('wrinkle_idx') == cols.index('notes') - 1
        assert row['wrinkle_idx'] == '1.70'
    finally:
        shutil.rmtree(d)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == '__main__':
    _run()
