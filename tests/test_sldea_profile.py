#!/usr/bin/env python3
"""Headless tests for sldea_profile (no hardware).

Run: .venv/bin/python tests/test_sldea_profile.py
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))

from sldea_profile import (SldeaProfile, BreakdownWatchdog, compute_levels,
                           monitor_problems, monitor_fix_plan,
                           control_v_for_kv, measured_kv, measured_ua,
                           credible_baseline_ua, fmt_meas, TREK_MAX_KV)


def test_monitor_problems_catches_the_2026_07_25_bench_bug():
    # The exact scope state that silently ruined five runs: CH2 at
    # 2.6 mV/div for a 0-10 V monitor, CH3 with a 10x factor on a BNC.
    probs = monitor_problems(10.0, v_scale=0.0026, v_atten=1.0,
                             i_scale=0.09, i_atten=10.0)
    joined = ' | '.join(probs)
    assert len(probs) == 3, probs
    assert 'V_Out' in joined and 'off-screen' in joined
    assert '10x probe' in joined                      # attenuation flagged
    # a correctly set up scope is silent
    assert monitor_problems(10.0, v_scale=2.0, v_atten=1.0,
                            i_scale=2.0, i_atten=1.0) == []
    # unknown values (query failed) are never guessed at
    assert monitor_problems(10.0) == []


def test_monitor_fix_plan_satisfies_its_own_check():
    """Re-pinned 2026-08-04: the plan carries SEPARATE positions now —
    V_Out keeps the -3 div headroom shift (+3 when the Trek inverts),
    I_Out sits at position 0 with a scale sized for the full ±i_need
    swing, because every ground-truthed breakdown is negative-going and
    the old shared -3 left one division below 0 V."""
    for kv in (1.0, 5.5, 10.0):
        for sign in (1.0, -1.0):
            plan = monitor_fix_plan(kv, v_sign=sign)
            assert plan['i_position'] == 0.0
            assert plan['v_position'] == (-3.0 if sign > 0 else 3.0)
            assert monitor_problems(kv, v_scale=plan['v_scale'],
                                    v_atten=plan['atten'],
                                    i_scale=plan['i_scale'],
                                    i_atten=plan['atten'],
                                    v_position=plan['v_position'],
                                    v_offset=0.0,
                                    i_position=plan['i_position'],
                                    i_offset=0.0, v_sign=sign) == []
    # and it does not over-range: 1 kV must not pick the 10 V/div step
    assert monitor_fix_plan(1.0)['v_scale'] < 1.0


def test_monitor_window_math_catches_the_2026_07_29_clipping():
    # The exact hole that let all three 07-29 runs clip at 4.25 kV with a
    # PASSING check: CH2 at 1 V/div, position 0 -> span 8 V >= 6 V (old
    # check happy) but the visible window is +-4 V, so V_Out left the
    # screen between 4.00 and 4.25 V and logged blank to the end.
    probs = monitor_problems(6.0, v_scale=1.0, v_atten=1.0,
                             v_position=0.0, v_offset=0.0)
    assert len(probs) == 1 and 'visible window' in probs[0], probs
    # position -3 (the fix plan's) restores the headroom: window -1..7 V
    assert monitor_problems(6.0, v_scale=1.0, v_atten=1.0,
                            v_position=-3.0, v_offset=0.0) == []
    # a window that excludes 0 V (huge offset) fails too: the 0 kV
    # baseline row would read off-screen
    probs = monitor_problems(6.0, v_scale=1.0, v_atten=1.0,
                             v_position=-3.0, v_offset=20.0)
    assert len(probs) == 1 and 'visible window' in probs[0]
    # I_Out must cover the FULL ±i_need swing (real breakdowns are
    # negative-going, -27..-208 uA on the 08-04 batch): a window shifted
    # clean off zero flags...
    probs = monitor_problems(6.0, i_scale=0.5, i_atten=1.0,
                             i_position=0.0, i_offset=10.0)
    assert len(probs) == 1 and 'I_Out' in probs[0] and 'clip' in probs[0]
    # ...and so does the OLD fix plan's -3 position: window -0.5..3.5 V
    # shows the positive half only, exactly the side breakdowns avoid
    probs = monitor_problems(6.0, i_scale=0.5, i_atten=1.0,
                             i_position=-3.0, i_offset=0.0)
    assert len(probs) == 1 and 'I_Out' in probs[0] and 'negative' in probs[0]
    # centred (position 0, offset 0) the same scale shows ±2 V: clean
    assert monitor_problems(6.0, i_scale=0.5, i_atten=1.0,
                            i_position=0.0, i_offset=0.0) == []
    # unknown position/offset (query failed): never guessed at -- the
    # span check alone applies, as before
    assert monitor_problems(6.0, v_scale=1.0, v_atten=1.0) == []


def test_monitor_v_window_is_polarity_aware():
    # 'Trek inverts' (gui sldea_trek_inv): the drive is negative and V_Out
    # swings 0..-need. Framed for a positive monitor, the -3 position
    # passes the check while the whole negative swing sits off-screen; the
    # v_sign=-1 framing must flag it and accept the mirrored +3 setup.
    probs = monitor_problems(6.0, v_scale=1.0, v_atten=1.0,
                             v_position=-3.0, v_offset=0.0, v_sign=-1.0)
    assert len(probs) == 1 and 'Trek inverted' in probs[0], probs
    assert monitor_problems(6.0, v_scale=1.0, v_atten=1.0,
                            v_position=3.0, v_offset=0.0, v_sign=-1.0) == []
    # and the +3 window that saves an inverted run fails an upright one
    probs = monitor_problems(6.0, v_scale=1.0, v_atten=1.0,
                             v_position=3.0, v_offset=0.0)
    assert len(probs) == 1 and 'visible window' in probs[0], probs


def test_watchdog_trips_only_after_sustained_current():
    wd = BreakdownWatchdog(trip_ua=100, confirm_s=3.0)
    assert not wd.update(0.0, 150)          # first over-sample starts streak
    assert not wd.update(1.0, 160)
    assert not wd.update(2.9, 170)          # 2.9 s < confirm window
    assert wd.update(3.1, 155)              # sustained >= 3 s -> CONFIRMED
    assert wd.tripped and wd.update(4.0, 5) # latched once tripped


def test_watchdog_dip_resets_streak():
    wd = BreakdownWatchdog(trip_ua=100, confirm_s=3.0)
    wd.update(0.0, 150)
    wd.update(2.0, 150)
    assert not wd.update(2.5, 40)           # dip below -> reset
    wd.update(3.0, 150)                     # new streak starts here
    assert not wd.update(5.5, 150)          # only 2.5 s into new streak
    assert wd.update(6.1, 150)


def test_watchdog_ignores_unreadable_samples():
    wd = BreakdownWatchdog(trip_ua=100, confirm_s=2.0)
    wd.update(0.0, 150)
    assert not wd.update(1.0, None)         # glitch: no reset, no evidence
    assert wd.update(2.2, 150)              # streak survived the None
    wd2 = BreakdownWatchdog(trip_ua=100, confirm_s=2.0)
    assert not wd2.update(0.0, None)        # None never starts a streak
    assert not wd2.tripped


def test_watchdog_baseline_makes_the_trip_deviation_based():
    # The 07-29 bench sits at a stiff -16 uA I_Out offset: with
    # baseline_ua the trip is |ua - baseline|, so the offset stops eating
    # a third of the threshold in one polarity.
    wd = BreakdownWatchdog(trip_ua=100, confirm_s=2.0, baseline_ua=-16.0)
    assert not wd.update(0.0, -116)         # dev exactly 100: streak starts
    assert wd.update(2.5, -120)             # sustained -> CONFIRMED
    # |ua| = 105 would trip the absolute rule, but dev is only 89: clean
    wd2 = BreakdownWatchdog(trip_ua=100, confirm_s=2.0, baseline_ua=-16.0)
    wd2.update(0.0, -105)
    assert not wd2.update(2.5, -105) and not wd2.tripped
    # no baseline -> absolute behaviour unchanged (dry runs, old tests)
    wd3 = BreakdownWatchdog(trip_ua=100, confirm_s=2.0)
    wd3.update(0.0, -105)
    assert wd3.update(2.5, -105)


def test_watchdog_offscreen_counts_as_over_trip():
    # A 9.9E37 sentinel on the CURRENT channel means the current clipped
    # off-screen -- far beyond any trip level. It must count as an
    # over-trip sample, not as an unreadable one.
    wd = BreakdownWatchdog(trip_ua=100, confirm_s=2.0, baseline_ua=-16.0)
    assert not wd.update(0.0, None, offscreen=True)     # streak starts
    assert not wd.update(1.0, None, offscreen=True)
    assert wd.update(2.2, None, offscreen=True)         # sustained clip
    # a readable below-trip sample still resets an offscreen streak
    wd2 = BreakdownWatchdog(trip_ua=100, confirm_s=2.0)
    wd2.update(0.0, None, offscreen=True)
    assert not wd2.update(1.0, 5)                        # recovered
    wd2.update(1.5, None, offscreen=True)
    assert not wd2.update(3.0, None)                     # plain None: 1.5 s
    assert wd2.update(3.6, None, offscreen=True)         # 2.1 s streak
    # the NATURE of the tripping evidence is tracked: a readable 5 uA
    # followed by an off-screen streak must report OFF-SCREEN, not print
    # the stale below-trip 5 uA as the alarm current (review 2026-08-04)
    wd3 = BreakdownWatchdog(trip_ua=100, confirm_s=2.0)
    assert not wd3.update(0.0, 5)                        # readable, quiet
    assert not wd3.update(1.0, None, offscreen=True)
    assert not wd3.update(2.0, None, offscreen=True)
    assert wd3.update(3.1, None, offscreen=True)
    assert wd3.last_offscreen and wd3.last_ua == 5
    # while a trip on readable current keeps the number authoritative
    wd4 = BreakdownWatchdog(trip_ua=100, confirm_s=2.0)
    wd4.update(0.0, 150)
    assert wd4.update(2.5, 150)
    assert not wd4.last_offscreen and wd4.last_ua == 150


def test_exposure_verdict_separates_the_real_corpus():
    """Thresholds calibrated on measured baselines, not eyeballed.

    Every (mean, %>=250) pair below is the real baseline frame of a run
    held on 2026-08-05. The 13 healthy ones must not be gated; the
    carbon-black validation run, whose baseline is MEDIAN 255, must be.

    Note WHY it is gated, because the intuitive reason is wrong: that
    run detects fine (conf 0.98-0.99 at every level, monotonic areas) --
    the electrode is dark and unclipped, so the boundary is a ~165-level
    step. It is gated because clipped pixels cannot be photometrically
    normalised, and because its baseline was 73.7% saturated while its
    own ramp frames were 0.27% -- the reference was shot at a different
    exposure from everything it is differenced against."""
    from sldea_profile import exposure_verdict, BASELINE_CLIP_SAT_PCT
    healthy = [
        (180.1, 0.17), (131.4, 0.17), (178.6, 0.63), (173.9, 1.21),
        (184.7, 0.85), (168.2, 1.33), (132.0, 0.44), (128.5, 0.12),
        (189.5, 3.60), (189.5, 3.62), (160.3, 2.92), (160.1, 2.91),
        (167.4, 1.36),
    ]
    for mean, sat in healthy:
        level, _msg = exposure_verdict(mean, sat)
        assert level == 'ok', (mean, sat, level)
    level, msg = exposure_verdict(235.3, 73.74)
    assert level == 'clipped', (level, msg)
    assert 'CLIPPED' in msg
    # and it must not claim the measurement is worthless -- measured
    # false on that very run
    assert 'nothing to work with' not in msg
    # the warn tier still exists between the two populations
    assert exposure_verdict(200.0, 12.0)[0] == 'bright'
    assert exposure_verdict(220.0, 1.0)[0] == 'bright'
    assert exposure_verdict(20.0, 0.0)[0] == 'dark'
    # and the gate is clear of BOTH populations by a wide margin
    assert max(s for _m, s in healthy) < 0.25 * BASELINE_CLIP_SAT_PCT
    assert 73.74 > 3.0 * BASELINE_CLIP_SAT_PCT


def test_watchdog_baseline_credibility_bound():
    # A learned '0 kV rest level' beyond min(30, trip/2) uA is not an
    # instrument offset (worst honest one observed: -16 uA, 07-29) but a
    # standing fault current -- a sample already leaking before the ramp.
    # Anchoring |I - baseline| to it would normalize the fault; refusing
    # it keeps the absolute rule, which then trips on the fault. Correct.
    assert credible_baseline_ua(-16.0, 100.0)       # the 07-29 offset
    assert credible_baseline_ua(30.0, 100.0)        # at the cap: allowed
    assert not credible_baseline_ua(-80.0, 100.0)   # standing fault
    assert not credible_baseline_ua(31.0, 100.0)
    assert not credible_baseline_ua(-25.0, 40.0)    # trip/2 = 20 binds
    assert credible_baseline_ua(19.0, 40.0)
    assert not credible_baseline_ua(None, 100.0)


def test_fmt_meas_survives_partial_readings():
    # An off-screen I_Out beside a fine V_Out is expected-by-design once
    # a window clips; the old single-guard f-string raised TypeError on
    # (kV ok, uA None) and killed the LIVE run's snapshot loop.
    assert fmt_meas(4.25, -153.4) == "  meas 4.25 kV / -153 µA"
    assert fmt_meas(4.25, None) == "  meas 4.25 kV / ? µA"
    assert fmt_meas(None, -16.0) == "  meas ? kV / -16 µA"
    assert fmt_meas(None, None) == ""


def test_scaling():
    assert control_v_for_kv(8.0) == 8.0            # 1 kV per control-volt
    assert measured_kv(7.5) == 7.5                 # 1 kV per scope-volt
    assert measured_ua(10.0) == 2000.0             # 10 V -> 2000 uA
    assert measured_ua(1.0) == 200.0


def test_levels_from_step_and_n():
    assert compute_levels(0, 10, step_kv=2.0) == [0, 2, 4, 6, 8, 10]
    # non-divisible step: exact increments, last <= end
    lv = compute_levels(0, 10, step_kv=3.0)
    assert lv == [0, 3, 6, 9]
    # n_steps: exact endpoints
    lv = compute_levels(0, 10, n_steps=5)
    assert lv == [0, 2.5, 5.0, 7.5, 10.0]


def test_timeline_simple_up():
    p = SldeaProfile(start_kv=0, end_kv=10, step_kv=2.0, ramp_s=5,
                     landing_s=60, settle_s=2, snap_lead_s=1)
    assert p.levels == [2, 4, 6, 8, 10]            # 0 kV is baseline, not held
    assert p.n_levels == 5
    # the staircase starts after the camera warm-up frame
    assert p.total_duration_s == p.baseline_warmup_s + 5 * (5 + 60)
    # warm-up + baseline + 2 per landing
    assert p.n_frames == 2 + 2 * 5


def test_snapshot_times_and_tags():
    p = SldeaProfile(start_kv=0, end_kv=4, step_kv=2.0, ramp_s=5,
                     landing_s=60, settle_s=2, snap_lead_s=1)
    w = p.baseline_warmup_s
    tags = [s['tag'] for s in p.snapshots]
    # a throw-away frame at t=0 lets the camera settle; the SECOND 0 kV
    # frame is the reference everything is differenced against
    assert tags[0] == 'warmup' and p.snapshots[0]['t'] == 0.0
    assert tags[1] == 'baseline' and p.snapshots[1]['t'] == w
    # first landing is 2 kV (0 kV is baseline): ramp w->w+5, hold to w+65
    post = next(s for s in p.snapshots if s['step'] == 1 and s['tag'] == 'post-ramp')
    pre = next(s for s in p.snapshots if s['step'] == 1 and s['tag'] == 'pre-ramp')
    assert post['t'] == w + 5 + 2 and post['nominal_kv'] == 2
    assert pre['t'] == w + 65 - 1                            # hold_end - lead
    # second landing (4 kV)
    post2 = next(s for s in p.snapshots if s['step'] == 2 and s['tag'] == 'post-ramp')
    assert post2['t'] == w + 65 + 5 + 2
    assert post2['nominal_kv'] == 4


def test_no_voltage_is_commanded_during_the_camera_warm_up():
    """HV SAFETY. kv_at() falls through to `segments[-1][4]` when no
    segment matches, so once the staircase stopped starting at t=0 that
    fall-through would have commanded the SG to the FINAL level for the
    whole warm-up window. Nothing in the loop would have questioned it:
    the runner just writes kv_at(el) to the SG."""
    p = SldeaProfile(start_kv=0, end_kv=8, step_kv=2.0, ramp_s=5,
                     landing_s=60, settle_s=2, snap_lead_s=1)
    w = p.baseline_warmup_s
    assert w > 0
    for t in (0.0, 0.01, w / 2, w - 1e-6):
        assert p.kv_at(t) == 0.0, (t, p.kv_at(t))
    assert p.kv_at(w) == 0.0                       # ramp starts here
    assert 0.0 < p.kv_at(w + 2.5) < 2.0            # mid first ramp
    # and a profile with the warm-up disabled behaves exactly as before
    q = SldeaProfile(start_kv=0, end_kv=8, step_kv=2.0, ramp_s=5,
                     landing_s=60, settle_s=2, snap_lead_s=1,
                     baseline_warmup_s=0)
    assert [s['tag'] for s in q.snapshots][0] == 'baseline'
    assert q.snapshots[0]['t'] == 0.0
    assert q.segments[0][1] == 0.0


def test_electrode_family_maps_the_campaign_materials():
    from sldea_profile import electrode_family
    assert electrode_family('CNT') == 'cnt'
    assert electrode_family(' carbon nanotube ') == 'cnt'
    assert electrode_family('Carbon Black') == 'carbon_black'
    assert electrode_family('CB') == 'carbon_black'
    assert electrode_family('eGaIn') == 'liquid_metal'
    assert electrode_family('liquid metal') == 'liquid_metal'
    assert electrode_family('Galinstan') == 'liquid_metal'
    # anything else non-blank is a real answer, just not a known family
    assert electrode_family('PEDOT:PSS') == 'other'
    # blank is NOT 'other' -- "not specified" and "something else" are
    # different facts and the run asks about the first one
    assert electrode_family('') is None
    assert electrode_family('   ') is None
    assert electrode_family(None) is None


def test_every_branded_cnt_ink_in_the_dropdown_families_to_cnt():
    """`#272`. The family is the future group-by key (#229), so a device
    built with a named CNT ink must land in the same bucket as one recorded
    as plain 'CNT' -- and NONE of the four brand names contains the
    substring 'cnt', so this cannot work by accident."""
    from sldea_profile import ELECTRODE_CHOICES, electrode_family
    branded = ('Carbon Solutions P3-SWNT', 'Carbon Solutions P2-SWNT',
               'nano-c Invisicon 3900', 'nano-c Invisicon 3500')
    for name in branded:
        assert name in ELECTRODE_CHOICES, f"{name} missing from the dropdown"
        assert 'cnt' not in name.lower(), "test premise: no literal 'cnt'"
        assert electrode_family(name) == 'cnt', name
    # plain CNT is kept: it is what every run recorded before the brands
    assert 'CNT' in ELECTRODE_CHOICES
    assert electrode_family('CNT') == 'cnt'


def test_electrode_family_tolerates_the_obvious_typed_variants():
    """What the matcher is documented to accept for the branded inks:
    any case, surrounding whitespace, and a substring of the name."""
    from sldea_profile import electrode_family
    for text in ('carbon solutions p3-swnt', 'CARBON SOLUTIONS P2-SWNT',
                 '  Carbon Solutions P3-SWNT  ', 'P3-SWNT', 'p2 swnt',
                 'SWNT', 'swnt ink', 'MWNT'):
        assert electrode_family(text) == 'cnt', text
    for text in ('nano-c invisicon 3900', 'NANO-C INVISICON 3500',
                 'Invisicon 3900', 'invisicon', ' nano-c  '):
        assert electrode_family(text) == 'cnt', text
    # and what it deliberately does NOT guess from: the campaign's device
    # tokens name a DEVICE, not a material
    assert electrode_family('P3') == 'other'
    assert electrode_family('P2') == 'other'
    assert electrode_family('P3_2.5mL_Triazole') == 'other'
    # the new needles must not steal the other families
    assert electrode_family('carbon black') == 'carbon_black'
    assert electrode_family('eGaIn') == 'liquid_metal'


def test_the_dropdown_no_longer_offers_a_literal_other():
    """`#272`. 'other' as a stored VALUE is a non-answer that looks like an
    answer; blank is how "unknown" is recorded and the run asks about it.
    The FAMILY bucket is untouched, so runs that stored the old literal
    still canonicalise the same way."""
    from sldea_profile import ELECTRODE_CHOICES, electrode_family
    assert 'other' not in ELECTRODE_CHOICES
    assert electrode_family('other') == 'other'
    assert electrode_family('PEDOT:PSS') == 'other'
    assert electrode_family('') is None


def test_a_branded_ink_reaches_setup_txt_verbatim():
    """The brand string is what gets cited and re-ordered, so setup.txt
    must carry it exactly as chosen -- not normalised to its family."""
    p = SldeaProfile(start_kv=0, end_kv=4, step_kv=2, ramp_s=5, landing_s=60)
    for brand in ('Carbon Solutions P3-SWNT', 'nano-c Invisicon 3900'):
        txt = p.setup_text('r', 'ts', 1, 2, 3, True, electrode=brand)
        assert f'Compliant electrode: {brand}' in txt, brand
        assert 'Electrode family: cnt' in txt, brand


def test_concentration_applies_only_where_an_ink_volume_means_something():
    """`#276`. CNT-family and unrecognised materials may have an ink
    volume; carbon black and liquid metal do not."""
    from sldea_profile import concentration_applies
    for yes in ('CNT', 'Carbon Solutions P3-SWNT', 'Carbon Solutions P2-SWNT',
                'nano-c Invisicon 3900', 'nano-c Invisicon 3500',
                'p3-swnt', 'some bespoke ink', 'PEDOT:PSS'):
        assert concentration_applies(yes) is True, yes
    for no in ('carbon black', 'Carbon Black', 'CB', 'eGaIn', 'egain',
               'Galinstan', 'liquid metal'):
        assert concentration_applies(no) is False, no
    # blank is applicable: "no electrode chosen yet" is not "this electrode
    # has no concentration", and grey-before-you-choose looks broken
    for blank in ('', '   ', None):
        assert concentration_applies(blank) is True, repr(blank)


def test_parse_concentration_ml_accepts_only_a_positive_number():
    """`#276`. The value lands in setup.txt as fact, so junk is refused at
    Run rather than written down."""
    from sldea_profile import parse_concentration_ml
    assert parse_concentration_ml('2.5') == 2.5
    assert parse_concentration_ml(' 1 ') == 1.0
    assert parse_concentration_ml('0.75') == 0.75
    assert parse_concentration_ml(2.5) == 2.5
    for junk in ('', '   ', None, 'abc', '2.5mL', '2,5', '0', '-1', '-0.5',
                 'nan', 'inf', '-inf'):
        try:
            parse_concentration_ml(junk)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{junk!r} was accepted")


def test_setup_text_records_the_concentration_beside_the_electrode():
    p = SldeaProfile(start_kv=0, end_kv=4, step_kv=2, ramp_s=5, landing_s=60)
    txt = p.setup_text('r', 'ts', 1, 2, 3, True,
                       electrode='Carbon Solutions P3-SWNT',
                       concentration_ml='2.5')
    assert 'Ink concentration: 2.5 mL' in txt
    # beside the electrode, not adrift somewhere else in the file
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    i = lines.index('Compliant electrode: Carbon Solutions P3-SWNT')
    assert lines[i + 1] == 'Electrode family: cnt'
    assert lines[i + 2] == 'Ink concentration: 2.5 mL'


def test_setup_text_omits_the_concentration_key_for_a_non_ink_electrode():
    """A carbon-black run must not carry a CNT-ink key AT ALL -- not even
    an empty one. An empty key reads as "an ink run that forgot to fill
    something in"; an absent key reads as "not an ink"."""
    p = SldeaProfile(start_kv=0, end_kv=4, step_kv=2, ramp_s=5, landing_s=60)
    for material in ('carbon black', 'eGaIn'):
        txt = p.setup_text('r', 'ts', 1, 2, 3, True, electrode=material)
        assert 'concentration' not in txt.lower(), material
        # and a stale value in the greyed box cannot leak in either
        stale = p.setup_text('r', 'ts', 1, 2, 3, True, electrode=material,
                             concentration_ml='2.5')
        assert 'concentration' not in stale.lower(), material
        assert '2.5' not in stale, material


def test_setup_text_says_not_specified_when_an_ink_run_declines():
    """Where it DOES apply, blank is written rather than dropped -- the
    same rule the electrode line follows, so "declined to answer" and
    "predates the field" stay different facts."""
    p = SldeaProfile(start_kv=0, end_kv=4, step_kv=2, ramp_s=5, landing_s=60)
    txt = p.setup_text('r', 'ts', 1, 2, 3, True, electrode='CNT')
    assert 'Ink concentration: (not specified)' in txt
    blank_electrode = p.setup_text('r', 'ts', 1, 2, 3, True)
    assert 'Ink concentration: (not specified)' in blank_electrode


def test_setup_text_records_the_electrode_and_the_warm_up():
    p = SldeaProfile(start_kv=0, end_kv=4, step_kv=2, ramp_s=5, landing_s=60)
    txt = p.setup_text('r', 'ts', 1, 2, 3, True, electrode='carbon black')
    assert 'Compliant electrode: carbon black' in txt
    assert 'Electrode family: carbon_black' in txt
    assert "tagged 'warmup'" in txt
    # blank says so explicitly rather than omitting the line -- an absent
    # line reads as an older run that never had the field
    blank = p.setup_text('r', 'ts', 1, 2, 3, True)
    assert 'Compliant electrode: (not specified)' in blank
    assert 'Electrode family' not in blank


def test_updown_and_repeat():
    p = SldeaProfile(start_kv=0, end_kv=6, step_kv=2.0, updown=True,
                     landing_s=10, ramp_s=1, settle_s=1, snap_lead_s=1)
    # up 2,4,6 then back down 4,2  (0 kV is baseline; peak held once)
    assert p.sequence() == [2, 4, 6, 4, 2]
    p2 = SldeaProfile(start_kv=0, end_kv=4, step_kv=2.0, repeat=3,
                      landing_s=10, ramp_s=1, settle_s=1, snap_lead_s=1)
    assert p2.sequence() == [2, 4] * 3
    assert p2.n_levels == 6


def test_kv_at_interpolates_ramp_and_hold():
    p = SldeaProfile(start_kv=0, end_kv=10, step_kv=10.0, ramp_s=10,
                     landing_s=20, settle_s=1, snap_lead_s=1, baseline=False)
    # single landing: ramp 0->10 over 0..10, hold 10..30
    assert p.kv_at(0) == 0.0
    assert p.kv_at(5) == 5.0                         # mid-ramp
    assert p.kv_at(10) == 10.0
    assert p.kv_at(20) == 10.0                        # mid-hold


def test_validation():
    for kw in (dict(start_kv=0, end_kv=12, step_kv=1),         # > Trek max
               dict(start_kv=-1, end_kv=5, step_kv=1),          # < 0
               dict(start_kv=0, end_kv=5, step_kv=1, landing_s=2,
                    settle_s=1, snap_lead_s=2),                 # settle+lead>=landing
               dict(start_kv=0, end_kv=5, landing_s=0, step_kv=1)):  # bad landing
        try:
            SldeaProfile(**kw)
            raise AssertionError(f"expected ValueError for {kw}")
        except ValueError:
            pass


def test_naming_and_csv_columns():
    import datetime
    dt = datetime.datetime(2026, 7, 23, 14, 5, 9)
    assert SldeaProfile.run_dirname(dt) == 'SLDEA_20260723_140509'
    assert SldeaProfile.frame_filename(7, 1.6, 'pre') == \
        'SLDEA_s07_01.60kV_pre.png'
    assert SldeaProfile.frame_filename(0, 0.0, 'baseline') == \
        'SLDEA_s00_00.00kV_baseline.png'
    # the edge-detection columns exist but are meant to start empty
    for col in ('active_area_px', 'active_area_mm2', 'active_diam_mm', 'notes'):
        assert col in SldeaProfile.CSV_COLUMNS
    # data.csv's schema is consumed by Edge Review, the tuner, the
    # diagnostic and the plot tool, and the 2026-08-05 telemetry sidecar
    # exists precisely so continuous logging does NOT grow it. Membership
    # checks cannot see an added column, so the list is pinned exactly.
    assert SldeaProfile.CSV_COLUMNS == [
        'snapshot', 'step', 'tag', 'nominal_kV', 'control_V', 'measured_kV',
        'measured_uA', 't_planned_s', 'timestamp', 'frame_file',
        'active_area_px', 'active_area_mm2', 'active_diam_mm', 'wrinkle_idx',
        'notes']


def test_setup_text_covers_key_facts():
    p = SldeaProfile(start_kv=0, end_kv=8, step_kv=0.2, ramp_s=5, landing_s=60)
    txt = p.setup_text('SLDEA_x', '2026-07-23T14:00', sg_ch=2, vmon_ch=1,
                       imon_ch=2, dry_run=True, cam_info='exp 6, WB off',
                       dea_diam_mm=16)
    assert 'DRY RUN' in txt
    assert 'CH2' in txt and 'CH1' in txt
    assert '2000 uA' in txt
    assert 'exp 6, WB off' in txt
    assert 'DEA nominal diameter: 16 mm' in txt


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == '__main__':
    _run()
