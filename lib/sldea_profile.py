#!/usr/bin/env python3
"""Single-Layer DEA (SLDEA) test profile: staircase drive, snapshot schedule,
and run layout. Pure logic -- no hardware, no Tk -- so it is unit-testable and
drives both the GUI preview and the run executor.

Drive chain: the signal generator outputs a DC *control* voltage into a Trek
HV amplifier. Gain is 1 V(control) = 1 kV(Trek); the Trek maxes at 10 kV, so
the control voltage is clamped to 10 V. There is no DMM here -- the Trek's own
monitor BNCs are read on the oscilloscope:
    V_Out : 10 V on the scope = 10 kV on the Trek   -> 1 kV per scope-volt
    I_Out : 10 V on the scope = 2000 uA on the Trek -> 200 uA per scope-volt

A run captures a webcam frame near the end of the ramp (settled) and again
just before the next step, at every landing, plus a 0 kV baseline -- for the
later edge-detection pass that traces the active DEA area vs voltage.

Headless self-test: .venv/bin/python tests/test_sldea_profile.py
"""

HV_GAIN_KV_PER_V = 1.0      # 1 V control -> 1 kV Trek output
TREK_MAX_KV = 10.0          # Trek amplifier ceiling
VMON_KV_PER_V = 1.0         # scope V_Out: 1 scope-volt -> 1 kV
IMON_UA_PER_V = 200.0       # scope I_Out: 1 scope-volt -> 200 uA (10 V = 2000 uA)


def control_v_for_kv(kv):
    """SG control voltage for a desired Trek output (kV)."""
    return kv / HV_GAIN_KV_PER_V


def measured_kv(vmon_scope_v):
    """Trek output (kV) from the scope's V_Out reading (volts)."""
    return vmon_scope_v * VMON_KV_PER_V


def measured_ua(imon_scope_v):
    """Trek current (uA) from the scope's I_Out reading (volts)."""
    return imon_scope_v * IMON_UA_PER_V


# The study compares COMPLIANT ELECTRODE MATERIALS: CNT so far, carbon
# black since 2026-08-05, liquid metal expected. Recorded per run so the
# campaign's device class lives in the data instead of in folder names,
# and so per-family detection defaults have something to key on later
# (#229). Free text is allowed -- the list is a convenience, not a
# constraint; an unrecognised entry is kept verbatim and families out to
# 'other'.
#
# The CNT inks are listed by BRAND (`#272`), not by the in-lab shorthand
# ("P2 ink", "P3 ink"): the brand is what can be re-ordered, cited, and
# matched against a datasheet, and the shorthand already lives in the run
# folder names. Generic 'CNT' stays because it is the vocabulary every
# run recorded before this list existed, and dropping it would strand
# that value.
#
# There is deliberately NO literal 'other' entry (`#272`). It is a
# non-answer that looks like an answer: a run recorded as "other" says
# only that the material is not on a list that has since changed. Typing
# the real material is now advertised on the box and costs the same
# click, and a genuinely unknown sample is BLANK -- which the run-start
# prompt already treats as a deliberate choice. 'other' remains a FAMILY
# below, so runs that recorded the literal string still canonicalise
# exactly as they always did.
ELECTRODE_CHOICES = ('', 'CNT',
                     'Carbon Solutions P3-SWNT', 'Carbon Solutions P2-SWNT',
                     'nano-c Invisicon 3900', 'nano-c Invisicon 3500',
                     'carbon black', 'eGaIn')
# Substring needles, matched against the lowercased value padded with
# spaces. None of the four brand names contains "cnt", so the CNT family
# also keys on what the products ARE: 'swnt'/'mwnt' (Carbon Solutions
# sells single-wall nanotube ink) and the 'invisicon' brand (nano-c's
# transparent CNT ink). Order matters -- the first family that matches
# wins.
_ELECTRODE_FAMILIES = (
    ('cnt', ('cnt', 'carbon nanotube', 'nanotube', 'swnt', 'mwnt',
             'invisicon', 'nano-c')),
    ('carbon_black', ('carbon black', 'carbonblack', 'cb ', ' cb', 'c-black')),
    ('liquid_metal', ('egain', 'e-gain', 'galinstan', 'liquid metal',
                      'liquidmetal')),
)


def electrode_family(text):
    """Free-text electrode -> a canonical family, or None when blank.

    'CNT' / 'carbon black' / 'eGaIn', the branded CNT inks in
    ELECTRODE_CHOICES, and their obvious spellings map to 'cnt' /
    'carbon_black' / 'liquid_metal'; anything else non-blank is 'other'.
    Nothing keys off this yet -- it exists so that when the detector needs
    per-family behaviour (#229: a mirror-bright electrode inverts the
    dark-disc assumption) there is one place that decides what family a
    run belongs to.

    Matching is case-insensitive, ignores surrounding whitespace, and is
    on substrings, so a hand-typed 'p3-swnt', 'SWNT ink' or
    'Invisicon 3900' still lands in 'cnt'. It does NOT guess from the
    campaign's device tokens alone: a bare 'P2' or 'P3' names a device,
    not a material, and is 'other'."""
    t = (text or '').strip().lower()
    if not t:
        return None
    padded = f' {t} '
    for family, needles in _ELECTRODE_FAMILIES:
        if any(nd in padded for nd in needles):
            return family
    return 'other'


def concentration_applies(electrode):
    """Is an ink concentration a meaningful thing to record here? (`#276`)

    The Concentration (mL) field records the CNT INK VOLUME -- the '2.5mL'
    in a folder name like P3_2.5mL_Triazole, which is how the campaign has
    been carrying it. Carbon black and liquid metal are not inks dispensed
    by volume, so for those it is meaningless: the field is greyed, the run
    never asks about it, and setup.txt does not carry the key at all. A CB
    run should not look like a CNT run that forgot to fill something in.

    Everything else may have one and is offered it: the CNT family, and any
    custom material the operator typed (we do not know that a material we
    have never heard of is not an ink).

    BLANK counts as applicable. "No electrode chosen yet" is not the same
    fact as "this electrode has no concentration", and greying the box
    before the operator has said what the device is would just look broken.
    """
    return electrode_family(electrode) not in ('carbon_black', 'liquid_metal')


def parse_concentration_ml(text):
    """Concentration text -> a positive float, or ValueError (`#276`).

    Junk, zero, negatives, nan and inf are all refused: this number goes
    into setup.txt as a fact about the device, and "0 mL of ink" is not a
    measurement anyone meant to record. A BLANK string is refused here
    too -- "nothing entered" is a question to put to the operator, not a
    parse result, and the run asks it separately.
    """
    import math
    s = str(text or '').strip()
    value = float(s)                       # ValueError on blank or junk
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"concentration must be a positive number of mL, got {s!r}")
    return value


def compute_levels(start_kv, end_kv, step_kv=None, n_steps=None):
    """Ordered list of landing voltages (kV).

    With step_kv: exact `step_kv` increments from start, last <= end.
    With n_steps: linspace with exact endpoints (start..end, n_steps levels).
    """
    if step_kv is not None and step_kv > 0:
        span = abs(end_kv - start_kv)
        n = int(span / step_kv + 1e-9) + 1
        sgn = 1.0 if end_kv >= start_kv else -1.0
        return [round(start_kv + sgn * i * step_kv, 6) for i in range(n)]
    if n_steps is not None and int(n_steps) >= 1:
        n = int(n_steps)
        if n == 1:
            return [end_kv]
        return [round(start_kv + (end_kv - start_kv) * i / (n - 1), 6)
                for i in range(n)]
    raise ValueError("give a positive step_kv or n_steps >= 1")


def fmt_duration(seconds):
    s = int(round(seconds))
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# Seconds between the throw-away 0 kV frame and the real baseline. The
# camera's firmware re-decides exposure on every open and walks written
# values back within ~0.5 s (webcam.apply_locked), so the FIRST frame of
# a session is the one most likely to be mis-exposed -- measured on the
# 2026-08-05 CB run, whose baseline was 73.7% saturated while its own
# ramp frames were 0.27%. Two seconds costs nothing and the staircase
# does not start until after it.
BASELINE_WARMUP_S = 2.0

SCOPE_DIVISIONS = 8          # MSO24 vertical divisions
BNC_ATTEN = 1.0              # bench convention: monitors are direct BNC


def suggested_scale(volts_needed, divisions=SCOPE_DIVISIONS):
    """A sane V/div that puts `volts_needed` on screen with headroom."""
    from math import ceil
    if volts_needed <= 0:
        return 1.0
    raw = (volts_needed * 1.3) / divisions        # 30% headroom
    for step in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0):
        if step >= raw:
            return step
    return 10.0


def monitor_problems(max_kv, v_scale=None, v_atten=None, i_scale=None,
                     i_atten=None, breakdown_ua=100.0,
                     divisions=SCOPE_DIVISIONS,
                     v_position=None, v_offset=None,
                     i_position=None, i_offset=None, v_sign=1.0):
    """Problems with the scope's Trek-monitor channel setup, as readable
    strings (empty list = ready to run).

    Bench 2026-07-25: CH2 sat at 2.6 mV/div while V_Out swings 0-10 V, so
    the MSO24 returned its invalid sentinel for nearly every reading and
    FIVE runs recorded no usable measured_kV at all; CH3 additionally
    carried a 10x attenuation factor with a plain BNC cable, inflating
    every logged current tenfold. Both are invisible from the GUI, so a
    live run now checks them up front.

    Bench 2026-07-29: the raw-span check is not enough — all three runs
    that day clipped from 4.25 kV with the check passing, because CH2 sat
    at 1 V/div with POSITION 0 (visible window ±4 V) for a 6 kV run: span
    8 V >= 6 V, but the top of screen was 4 V. When position and offset
    are known, the ACTUAL visible window (centre = offset, shifted down
    by position divisions) is checked instead of the span. Unknown values
    (query failed) are still never guessed at — the caller is expected to
    say which channels went unverified.

    `v_sign` (+1/-1) is the Trek monitor polarity: with the 'Trek
    inverts' option the amplifier is driven negative and V_Out swings
    0..-need, so the V window must contain 0 and v_sign*need — a window
    framed for a positive-going monitor would clip every reading of an
    inverted run while this check passed (review 2026-08-04).

    The I window must contain ±i_need regardless of drive polarity:
    every real breakdown in the 2026-08-04 ground-truth batch swings
    NEGATIVE (-27..-208 uA) while the old check only demanded that 0 V
    be on screen — a window 0..top would clip the one excursion the
    watchdog exists to see."""
    out = []
    v_need = float(max_kv) / VMON_KV_PER_V
    i_need = max(1.0, 2.0 * float(breakdown_ua) / IMON_UA_PER_V)
    v_lo, v_hi = (0.0, v_need) if v_sign >= 0 else (-v_need, 0.0)
    for label, scale, atten, span_need, lo, hi, pos, off in (
            ('V_Out', v_scale, v_atten, v_need, v_lo, v_hi,
             v_position, v_offset),
            ('I_Out', i_scale, i_atten, 2.0 * i_need, -i_need, i_need,
             i_position, i_offset)):
        if atten is not None and abs(float(atten) - BNC_ATTEN) > 1e-6:
            out.append(
                f"{label}: scope thinks a {float(atten):g}x probe is fitted, "
                f"but the monitors use direct BNC — readings would be "
                f"{float(atten):g}x off.")
        if scale is not None:
            span = float(scale) * divisions
            if span < span_need:
                out.append(
                    f"{label}: {float(scale):g} V/div shows only ±"
                    f"{span / 2:.3g} V, but this run needs {span_need:.3g} V "
                    f"({'up to %g kV' % max_kv if label == 'V_Out' else 'bipolar breakdown headroom'})"
                    f" — readings will go off-screen and log as blank.")
            elif pos is not None and off is not None:
                top = float(off) + (divisions / 2.0 - float(pos)) * float(scale)
                bot = float(off) - (divisions / 2.0 + float(pos)) * float(scale)
                if label == 'V_Out' and (top < hi or bot > lo):
                    out.append(
                        f"V_Out: visible window {bot:g}..{top:g} V "
                        f"({float(scale):g} V/div, position {float(pos):g} "
                        f"div, offset {float(off):g} V) cannot show "
                        f"{lo:g}..{hi:g} V"
                        f"{' (Trek inverted)' if v_sign < 0 else ''} — "
                        f"readings past the screen edge log as blank.")
                elif label == 'I_Out' and (top < hi or bot > lo):
                    out.append(
                        f"I_Out: visible window {bot:g}..{top:g} V cannot "
                        f"show ±{hi:g} V — real breakdowns swing negative "
                        f"(-27..-208 uA on the 08-04 batch); the excursion "
                        f"would clip off-screen and log as blank.")
    return out


def monitor_fix_plan(max_kv, breakdown_ua=100.0, divisions=SCOPE_DIVISIONS,
                     v_sign=1.0):
    """{'v_scale','i_scale','atten','v_position','i_position'} that would
    satisfy monitor_problems() for this run.

    V_Out gets position -3 (or +3 when the Trek is inverted, v_sign=-1)
    so the unipolar 0..±need swing uses the screen asymmetrically;
    I_Out gets position 0 with a scale sized for the full ±i_need swing
    — the current excursion is bipolar-negative and a -3 position left
    only one division below 0 V (review 2026-08-04)."""
    v_need = float(max_kv) / VMON_KV_PER_V
    i_need = max(1.0, 2.0 * float(breakdown_ua) / IMON_UA_PER_V)
    return {'v_scale': suggested_scale(v_need, divisions),
            'i_scale': suggested_scale(2.0 * i_need, divisions),
            'atten': BNC_ATTEN,
            'v_position': 3.0 if v_sign < 0 else -3.0,
            'i_position': 0.0}


# Baseline exposure gates, calibrated on the real corpus rather than
# guessed. Measured over all 14 baselines held on 2026-08-05: every
# healthy run sits at mean 128-190 with 0.12-3.62% of pixels at/above
# 250. The carbon-black validation run sits at mean 235, MEDIAN 255,
# 73.7% saturated -- twenty times the worst healthy run. The warn tier
# was already well placed (it fires on that run and on nothing else);
# what was missing is a tier that actually stops you.
BASELINE_DARK_MEAN = 40.0
BASELINE_BRIGHT_MEAN = 215.0
BASELINE_BRIGHT_SAT_PCT = 8.0
# The gate sits 5.5x above the worst healthy baseline and 3.7x below the
# blown-out one, so it separates the two populations with room to spare.
BASELINE_CLIP_MEAN = 225.0
BASELINE_CLIP_SAT_PCT = 20.0


def exposure_verdict(mean, sat_pct):
    """Baseline exposure -> ('ok'|'dark'|'bright'|'clipped', message).

    'clipped' is a GATE, but be precise about WHY, because the obvious
    reason turned out to be wrong when it was measured (2026-08-05, the
    CB run): a clipped BACKGROUND does not stop the detector. That run's
    baseline was 73.7% saturated and it still traced every level at conf
    0.98-0.99 with a monotonic area curve -- the electrode disc itself
    sat at median 89, nowhere near clipping, so the boundary was a
    ~165-level step and if anything easier to find than usual.

    What is actually wrong with a clipped baseline:
      * clipped pixels carry no information the gain/offset
        normalisation can recover -- `photometric_fit` can only scale
        what still varies;
      * and it is evidence the exposure was not pinned. On that same
        run the baseline was 73.7% saturated while its own ramp frames
        were 0.27%, so the reference frame was shot at a different
        exposure from everything it is differenced against.
    Both are reasons to fix the exposure. Neither is a reason to claim
    the measurement is worthless, so this no longer says that.

    Kept here, clock-free and Tk-free, so the thresholds can be tested
    against the measured corpus instead of eyeballed in a dialog."""
    m, s = float(mean), float(sat_pct)
    if s >= BASELINE_CLIP_SAT_PCT or m >= BASELINE_CLIP_MEAN:
        return ('clipped',
                f"BASELINE IS CLIPPED - {s:.0f}% of pixels are at or "
                f"above 250 (mean {m:.0f}). Clipped pixels carry nothing "
                f"the gain/offset normalisation can recover, and a "
                f"baseline exposed differently from the ramp frames is "
                f"not a comparable reference. Lower the exposure.")
    if m < BASELINE_DARK_MEAN:
        return ('dark', "⚠ looks DARK - raise exposure/lighting")
    if m > BASELINE_BRIGHT_MEAN or s > BASELINE_BRIGHT_SAT_PCT:
        return ('bright', "⚠ looks BRIGHT/clipped - lower exposure")
    return ('ok', "exposure OK")


def credible_baseline_ua(baseline_ua, trip_ua):
    """True when a learned 0 kV rest level may anchor the deviation trip.

    The bound is min(30, trip/2) uA: the worst honest instrument offset
    observed is the 07-29 campaign's stiff -16 uA, so 30 covers it with
    margin, while anything larger at 0 kV is indistinguishable from a
    standing fault current (a damaged sample leaking before the ramp).
    Anchoring |I - baseline| to a fault current would normalize the very
    signal the watchdog exists for; refusing the baseline leaves the
    absolute |I| >= trip rule, which then trips on the fault — the
    correct outcome (review 2026-08-04). Never more than trip/2 so a
    small trip level cannot be half-eaten by its own baseline."""
    if baseline_ua is None:
        return False
    return abs(float(baseline_ua)) <= min(30.0, 0.5 * float(trip_ua))


def fmt_meas(mkv, mua):
    """Status-line fragment for one snapshot's scope readings.

    Either value may be None INDEPENDENTLY — an off-screen I_Out with a
    fine V_Out is expected-by-design once a window clips — so each is
    formatted on its own ('?' for unreadable). The old single-guard
    f-string raised TypeError on (mkv ok, mua None) and killed the LIVE
    run's snapshot loop (review 2026-08-04)."""
    if mkv is None and mua is None:
        return ""
    fk = '?' if mkv is None else f"{mkv:.2f}"
    fi = '?' if mua is None else f"{mua:.0f}"
    return f"  meas {fk} kV / {fi} µA"


class BreakdownWatchdog:
    """Deliberately SLOW-to-trip breakdown detector for live runs.

    Watches the Trek current (via the scope's I_Out monitor) and only
    declares breakdown after the current has stayed at/above the trip
    level for `confirm_s` seconds of CONSECUTIVE samples -- any single
    reading below the threshold resets the streak, and unreadable samples
    (None) are ignored without resetting. The point is to be essentially
    certain before aborting a long run: a transient spike or one glitchy
    scope read never trips it.

    With `baseline_ua` (learned by the runner at 0 kV before the ramp)
    the trip is on |ua - baseline| instead of |ua|: the whole 07-29
    campaign sits on a stiff -16 uA I_Out offset, which silently made an
    absolute threshold polarity-asymmetric (34 uA of real headroom one
    way, 66 the other). Without a baseline the absolute behaviour is
    unchanged, so dry runs and scope-less tests behave exactly as before.
    """

    def __init__(self, trip_ua=100.0, confirm_s=3.0, baseline_ua=None):
        self.trip_ua = float(trip_ua)
        self.confirm_s = float(confirm_s)
        self.baseline_ua = None if baseline_ua is None else float(baseline_ua)
        self._over_since = None
        self.tripped = False
        self.last_ua = None
        # nature of the LAST evidence-bearing sample: True when it was the
        # off-screen sentinel. The trip message must not print a stale
        # below-trip last_ua when the streak that tripped was a clipping
        # current after an earlier readable sample (review 2026-08-04).
        self.last_offscreen = False

    def update(self, t_s, ua, offscreen=False):
        """Feed one sample (time in s, current in uA; ua may be None).
        Returns True the moment breakdown is CONFIRMED.

        offscreen=True marks a read where the scope reported its 9.9E37
        off-screen sentinel on the CURRENT channel: a clipping current is
        far beyond any sane trip level, so it counts as an over-trip
        sample rather than as an unreadable one."""
        if self.tripped:
            return True
        if ua is None and not offscreen:
            return False                 # unreadable: no evidence either way
        if ua is not None:
            self.last_ua = ua
        self.last_offscreen = bool(offscreen)
        if offscreen:
            over = True
        elif self.baseline_ua is not None:
            over = abs(ua - self.baseline_ua) >= self.trip_ua
        else:
            over = abs(ua) >= self.trip_ua
        if over:
            if self._over_since is None:
                self._over_since = t_s
            elif t_s - self._over_since >= self.confirm_s:
                self.tripped = True
                return True
        else:
            self._over_since = None      # dipped below: start over
        return False


TELEMETRY_FILENAME = 'telemetry.csv'
TELEMETRY_COLUMNS = ['t_s', 'timestamp', 'nominal_kV', 'measured_kV',
                     'measured_uA', 'v_status', 'i_status', 'event']
# The run loop ticks at 10 Hz and the watchdog already samples I_Out at
# 2 Hz, so 2 Hz costs nothing extra; anything faster is a NEW instrument
# duty cycle and stays bench-unverified until someone times it (#157).
TELEMETRY_MAX_HZ = 2.0
TELEMETRY_MIN_HZ = 0.1
# V_Out is sampled at most this often even when current is sampled faster:
# a kV read is a second MEASUREMENT:IMMED triple under the instrument lock,
# and nominal_kV already gives the exact commanded voltage on every row.
TELEMETRY_KV_MIN_PERIOD_S = 1.0
# A single row write+flush slower than this means the output directory is
# not behaving like local disk (the bench default lives on a network
# share). Past it the log stops flushing every row -- see TelemetryLog.
TELEMETRY_SLOW_WRITE_S = 0.25
TELEMETRY_SLOW_FLUSH_PERIOD_S = 1.0
# ...and the flush window has to CLEAR the worst write it is throttling.
# A fixed 1 s window is already expired on arrival once a single write
# costs 1 s+, so every row flushed again and the monitor tick tracked the
# share 1:1 — the throttle was inert in exactly the regime it exists for
# (review 2026-08-05, measured).
TELEMETRY_SLOW_FLUSH_MARGIN = 4.0
# The achieved rate always lands a little under target: the run loop polls
# at 10 Hz, so a 0.5 s gate actually fires at 0.5-0.6 s, and every snapshot
# steals a tick for its camera grab. Only a real inability to keep up
# should raise a warning, so the bar is 70% of target, not 80%.
TELEMETRY_SHORTFALL_FRAC = 0.7


def _telemetry_num(value, places):
    """Number for a telemetry cell, or '' when it is not one.

    Blank, never an exception and never a bogus number: a caller that
    hands over None, a NaN measurement or a stray string costs that one
    cell, not the whole record of the run."""
    if value is None:
        return ''
    try:
        val = float(value)
    except (TypeError, ValueError):
        return ''
    if val != val or val in (float('inf'), float('-inf')):
        return ''
    return round(val, places)


def clamp_telemetry_hz(hz):
    """Requested telemetry rate -> the rate actually offered (Hz).

    Capped at TELEMETRY_MAX_HZ: the samples above that rate do not exist
    yet — they would be new scope round-trips inside the live run loop,
    which is bench-verified territory, not a desk decision."""
    try:
        val = float(hz)
    except (TypeError, ValueError):
        return TELEMETRY_MAX_HZ
    if val != val:                       # NaN
        return TELEMETRY_MAX_HZ
    return min(TELEMETRY_MAX_HZ, max(TELEMETRY_MIN_HZ, val))


class TelemetryLog:
    """Sidecar CSV of the live monitor samples, beside data.csv.

    The watchdog already reads I_Out at ~2 Hz on every live run and throws
    every sample away; nothing electrical is recorded between snapshots, so
    a breakdown cannot even be DATED after the fact (#157/#189). This
    writes those samples to their own file: `data.csv` keeps its one-row-
    per-frame schema and every downstream reader (`sldea_edge.load_run`,
    Edge Review, the tuner, `sldea_diag`) is untouched.

    Clock-free like `psu_logger` -- the caller passes elapsed seconds and
    the ISO timestamp -- so the whole thing is unit-testable with no Tk, no
    hardware and no sleeping.

    Statuses are recorded, not just values: a blank `measured_kV` with
    `v_status=offscreen` (the Tek 9.9E37 sentinel, i.e. clipped off the
    visible window) is a different fact from `invalid` (unparseable) and
    from `skipped` (not sampled on this row), and the 07-29 dropout
    investigation was ambiguous precisely because that distinction was
    never written down (#159).

    NOTHING here may raise into the live run loop: a full disk or a
    yanked USB stick must cost the record, never the HV shutdown path.
    Write failures set `.failed` and are reported once by the caller.

    For the same reason the per-row flush is not unconditional. Flushing
    every row is what makes an aborted run's file complete, but the run
    loop is also the thread that services ■ Abort, and the bench's
    default output directory is a network share -- one stalled flush is
    the ramp-down waiting on the file system. So each write is TIMED, and
    a directory that behaves like a slow share flushes only periodically
    (`.slow`), on a window sized to CLEAR the worst write seen so far --
    a fixed window shorter than the write is no throttle at all. The
    window in force is said out loud in `summary()`.

    `hold_flush` is the harder version of the same idea, for the seconds
    between a confirmed breakdown and the SG reaching 0 V: rows are still
    written (they are the record of the event) but nothing is flushed
    until `close()`, which the runner calls after the HV is down.

    This bounds the flush DUTY CYCLE, not the worst single stall: one
    unlucky flush still parks the run thread for its full duration.
    Taking that out needs the write off this thread entirely -- a queue
    drained after the SG is zeroed -- which is a follow-up, not this
    change. `max_write_s` is deliberately monotonic (the window never
    re-tightens), so one pathological write keeps the file lazily
    flushed for the rest of the run; `close()` still commits everything
    on any ordinary end, abort or exception.
    """

    def __init__(self, path, target_hz=TELEMETRY_MAX_HZ,
                 kv_min_period_s=TELEMETRY_KV_MIN_PERIOD_S,
                 slow_write_s=TELEMETRY_SLOW_WRITE_S, clock=None):
        import csv as _csv
        import time as _time
        self.path = path
        self._clock = clock or _time.monotonic
        self.slow_write_s = float(slow_write_s)
        self.slow = False              # degraded to periodic flushing
        self.hold_flush = False        # HV-shutdown path: buffer only
        self.max_write_s = 0.0
        self._last_flush_t = None
        self.target_hz = clamp_telemetry_hz(target_hz)
        self.period_s = 1.0 / self.target_hz
        # never ask for kV faster than the samples themselves
        self.kv_period_s = max(float(kv_min_period_s), self.period_s)
        self.rows = 0                    # every row, event rows included
        self.samples = 0                 # periodic samples only (rate math)
        self.kv_rows = 0
        self.unreadable = 0              # i_status invalid/error
        self.offscreen = 0               # i_status offscreen
        self.failed = False
        self.last_error = None
        self.max_gap_s = 0.0
        self._first_t = None
        self._last_t = None
        self._next_due = 0.0
        self._next_kv_due = 0.0
        # utf-8 explicitly (not the platform codec): the bench writes on
        # Linux and the analysis PC reads on Windows, and the same file
        # must decode identically on both (audit 2026-08-05, data.csv).
        # `event` is ASCII-clamped on the way in, so the file stays plain
        # ASCII in practice and Excel is happy either way.
        self._f = open(path, 'w', newline='', encoding='utf-8')
        self._w = _csv.DictWriter(self._f, fieldnames=TELEMETRY_COLUMNS)
        self._w.writeheader()
        self._f.flush()

    # ---- schedule -------------------------------------------------------
    def due(self, t_s):
        """Is a periodic sample due at elapsed time `t_s`?"""
        return not self.failed and float(t_s) >= self._next_due

    def kv_due(self, t_s):
        """Should this sample also spend a round-trip on V_Out?

        Asked BEFORE the read so a skipped kV costs nothing at all."""
        return not self.failed and float(t_s) >= self._next_kv_due

    # ---- writing --------------------------------------------------------
    def sample(self, t_s, timestamp, nominal_kv, ua=None, i_status='',
               kv=None, v_status='', event=''):
        """Write one periodic sample and advance the schedule.

        Unreadable samples are written too -- a gap in the record is the
        evidence that monitoring was lost, and blanking it silently is the
        bug #159 was filed about. Returns True when the row was written."""
        t = float(t_s)
        if self._last_t is not None:
            self.max_gap_s = max(self.max_gap_s, t - self._last_t)
        # schedule from the ACTUAL sample time, like the watchdog's own
        # gate: a stalled scope must not leave a burst of catch-up rows.
        self._next_due = t + self.period_s
        if v_status and v_status != 'skipped':
            self._next_kv_due = t + self.kv_period_s
        ok = self._write(t, timestamp, nominal_kv, ua, i_status, kv,
                         v_status or 'skipped', event)
        if ok:
            self.samples += 1
            self._first_t = t if self._first_t is None else self._first_t
            self._last_t = t
            if v_status and v_status != 'skipped':
                self.kv_rows += 1
            if i_status == 'offscreen':
                self.offscreen += 1
            elif i_status in ('invalid', 'error'):
                self.unreadable += 1
        return ok

    def event(self, t_s, timestamp, nominal_kv, event, ua=None, i_status='',
              kv=None, v_status=''):
        """Write a row that marks something (a snapshot, the breakdown
        trip) without disturbing the periodic schedule or the rate math --
        it reuses readings that were taken for another purpose."""
        return self._write(float(t_s), timestamp, nominal_kv, ua,
                           i_status or 'skipped', kv, v_status or 'skipped',
                           event)

    def _write(self, t_s, timestamp, nominal_kv, ua, i_status, kv, v_status,
               event):
        if self.failed:
            return False
        # Flush every row while the file system is keeping up (an aborted
        # run then has a complete file); once it is not, only once per
        # flush window, so the run loop is not the thing waiting on the
        # share. On the HV-shutdown path, not at all until close().
        do_flush = (not self.hold_flush
                    and (not self.slow or self._last_flush_t is None
                         or t_s - self._last_flush_t
                         >= self.flush_period_s()))
        started = self._clock()
        try:
            self._w.writerow({
                't_s': round(t_s, 3),
                'timestamp': timestamp,
                'nominal_kV': _telemetry_num(nominal_kv, 4),
                'measured_kV': _telemetry_num(kv, 4),
                'measured_uA': _telemetry_num(ua, 2),
                'v_status': v_status,
                'i_status': i_status or 'skipped',
                # ASCII-clamped so the file cannot depend on the caller's
                # vocabulary staying ASCII (the run log's own is not: ⚡, µA)
                'event': str(event).encode('ascii', 'replace').decode(),
            })
            if do_flush:
                self._f.flush()
                self._last_flush_t = t_s
        except Exception as e:       # disk full, device gone, encoding...
            self.failed = True
            self.last_error = e
            return False
        took = self._clock() - started
        if took > self.max_write_s:
            self.max_write_s = took
        if took >= self.slow_write_s:
            self.slow = True
        self.rows += 1
        return True

    # ---- reporting ------------------------------------------------------
    def flush_period_s(self):
        """Seconds between flushes while degraded.

        Sized to clear the worst write seen: a window shorter than a
        single write has always already expired by the next row, which
        made the throttle a no-op on a badly stalled share (measured,
        review 2026-08-05). `summary()` reports THIS number rather than
        the constant, so the run log cannot claim a throttle that is not
        in force."""
        return max(TELEMETRY_SLOW_FLUSH_PERIOD_S,
                   TELEMETRY_SLOW_FLUSH_MARGIN * self.max_write_s)

    def achieved_hz(self):
        """Mean sample rate actually achieved, or None with < 2 samples."""
        if self.samples < 2 or self._first_t is None:
            return None
        span = self._last_t - self._first_t
        return (self.samples - 1) / span if span > 0 else None

    def summary(self):
        """One log line: what was recorded and whether the rate held.

        #157's acceptance asks for the ACHIEVED rate to be visible, so a
        run where the hardware could not keep up says so in run.log
        instead of quietly producing a sparse file."""
        if self.failed and self.rows == 0:
            return (f"telemetry: FAILED before any row was written "
                    f"({self.last_error})")
        hz = self.achieved_hz()
        parts = [f"telemetry: {self.samples} samples"]
        if hz is not None:
            parts.append(f"{hz:.2f} Hz achieved (target {self.target_hz:g})")
        parts.append(f"max gap {self.max_gap_s:.1f} s")
        parts.append(f"{self.kv_rows} with kV")
        if self.offscreen:
            parts.append(f"{self.offscreen} off-screen")
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable")
        if self.slow:
            parts.append(f"SLOW DISK ({self.max_write_s:.2f} s worst write) "
                         f"— flushed every {self.flush_period_s():.1f} s, "
                         f"not per row")
        if self.failed:
            parts.append(f"STOPPED EARLY ({self.last_error})")
        return ", ".join(parts) + f" -> {TELEMETRY_FILENAME}"

    def rate_shortfall(self):
        """True when the achieved rate missed the target badly enough to
        mention -- the loop or the scope could not keep up. See
        TELEMETRY_SHORTFALL_FRAC for why the bar is not tighter."""
        hz = self.achieved_hz()
        return (hz is not None
                and hz < TELEMETRY_SHORTFALL_FRAC * self.target_hz)

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


class SldeaProfile:
    """A full staircase test built from the tab's input fields."""

    CSV_COLUMNS = [
        'snapshot', 'step', 'tag', 'nominal_kV', 'control_V',
        'measured_kV', 'measured_uA', 't_planned_s', 'timestamp', 'frame_file',
        # left empty at capture time; filled by the edge-detection pass later
        # (wrinkle_idx = high-freq texture vs baseline; wrinkled = active):
        'active_area_px', 'active_area_mm2', 'active_diam_mm', 'wrinkle_idx',
        'notes',
    ]

    def __init__(self, start_kv=0.0, end_kv=10.0, step_kv=0.25, n_steps=None,
                 ramp_s=5.0, landing_s=60.0, settle_s=2.0, snap_lead_s=1.0,
                 repeat=1, updown=False, baseline=True,
                 snap_post=True, snap_pre=True,
                 baseline_warmup_s=BASELINE_WARMUP_S):
        for name, val in (('start_kv', start_kv), ('end_kv', end_kv)):
            if not 0.0 <= val <= TREK_MAX_KV:
                raise ValueError(f"{name}={val} kV out of range 0..{TREK_MAX_KV}")
        if ramp_s < 0:
            raise ValueError("ramp_s must be >= 0")
        if landing_s <= 0:
            raise ValueError("landing_s must be > 0")
        if settle_s < 0 or snap_lead_s < 0:
            raise ValueError("settle_s and snap_lead_s must be >= 0")
        if not (snap_post or snap_pre or baseline):
            raise ValueError("no snapshots requested")
        if snap_post and settle_s >= landing_s:
            raise ValueError("settle_s must be < landing_s")
        if snap_pre and snap_lead_s >= landing_s:
            raise ValueError("snap_lead_s must be < landing_s")
        if snap_post and snap_pre and settle_s + snap_lead_s >= landing_s:
            raise ValueError("settle_s + snap_lead_s must be < landing_s")

        self.levels = compute_levels(start_kv, end_kv, step_kv, n_steps)
        # 0 kV is captured as the baseline, not held as a landing -- a 0 kV
        # hold does nothing to a DEA, so drop a leading zero level.
        if self.levels and abs(self.levels[0]) < 1e-9:
            self.levels = self.levels[1:]
        if not self.levels:
            raise ValueError("no non-zero landing levels to run")
        if max(self.levels) > TREK_MAX_KV + 1e-9:
            raise ValueError(f"a level exceeds the Trek max {TREK_MAX_KV} kV")

        self.start_kv, self.end_kv = start_kv, end_kv
        self.step_kv, self.n_steps_req = step_kv, n_steps
        self.ramp_s, self.landing_s = float(ramp_s), float(landing_s)
        self.settle_s, self.snap_lead_s = float(settle_s), float(snap_lead_s)
        self.repeat = max(1, int(repeat))
        self.updown, self.baseline = bool(updown), bool(baseline)
        self.snap_post, self.snap_pre = bool(snap_post), bool(snap_pre)
        self.baseline_warmup_s = max(0.0, float(baseline_warmup_s))
        self._build()

    def sequence(self):
        """The full ordered list of landing voltages (with up/down + repeat)."""
        seq = list(self.levels)
        if self.updown and len(self.levels) > 1:
            seq = seq + list(reversed(self.levels[:-1]))
        return seq * self.repeat

    def _build(self):
        self.segments = []    # (kind, t0, t1, from_kv, to_kv), kind ramp|hold
        self.snapshots = []   # {t, step, nominal_kv, tag}
        t = 0.0
        if self.baseline:
            # TWO frames at 0 kV when a warm-up is configured. The first
            # is thrown at the camera to make it settle; the SECOND is
            # the reference every area in the run is differenced against.
            # The 2026-08-05 CB run is why: its baseline came out 73.7%
            # saturated while its own ramp frames were 0.27%, i.e. the
            # reference was exposed differently from everything compared
            # against it. Tagged 'warmup', NOT 'baseline-warmup' --
            # sldea_plot phases rows with tag.startswith('baseline'), so
            # a baseline-prefixed tag would be averaged into A0.
            if self.baseline_warmup_s > 0:
                self.snapshots.append(
                    {'t': 0.0, 'step': 0, 'nominal_kv': 0.0,
                     'tag': 'warmup'})
                t = self.baseline_warmup_s
            self.snapshots.append(
                {'t': t, 'step': 0, 'nominal_kv': 0.0, 'tag': 'baseline'})
        prev = 0.0
        for step, lvl in enumerate(self.sequence(), start=1):
            self.segments.append(('ramp', t, t + self.ramp_s, prev, lvl))
            t_ramp_end = t + self.ramp_s
            t_hold_end = t_ramp_end + self.landing_s
            self.segments.append(('hold', t_ramp_end, t_hold_end, lvl, lvl))
            if self.snap_post:
                self.snapshots.append(
                    {'t': t_ramp_end + self.settle_s, 'step': step,
                     'nominal_kv': lvl, 'tag': 'post-ramp'})
            if self.snap_pre:
                self.snapshots.append(
                    {'t': t_hold_end - self.snap_lead_s, 'step': step,
                     'nominal_kv': lvl, 'tag': 'pre-ramp'})
            prev = lvl
            t = t_hold_end
        self.total_duration_s = t
        self.n_levels = len(self.sequence())
        self.n_frames = len(self.snapshots)

    def kv_at(self, t):
        """Target Trek voltage (kV) at time t -- for the runner's ramp and the
        preview curve.

        SAFETY: t before the first segment returns 0.0. With a warm-up
        baseline the staircase no longer starts at t=0, and the
        fall-through below returns the FINAL level -- which would have
        commanded the SG to full scale for the whole warm-up window."""
        if t <= 0:
            return 0.0
        if self.segments and t < self.segments[0][1]:
            return 0.0
        for kind, t0, t1, a, b in self.segments:
            if t0 <= t <= t1:
                if kind == 'hold' or t1 == t0:
                    return b
                return a + (b - a) * (t - t0) / (t1 - t0)
        return self.segments[-1][4] if self.segments else 0.0

    # ---- run layout / naming -------------------------------------------
    @staticmethod
    def run_dirname(dt):
        """Auto directory name from the run start datetime."""
        return dt.strftime("SLDEA_%Y%m%d_%H%M%S")

    @staticmethod
    def frame_filename(step, nominal_kv, tag):
        return f"SLDEA_s{int(step):02d}_{float(nominal_kv):05.2f}kV_{tag}.png"

    def summary(self):
        return (f"{len(self.levels)} levels "
                f"{self.start_kv:g}->{self.end_kv:g} kV"
                f"{' (up/down)' if self.updown else ''}"
                f"{f' x{self.repeat}' if self.repeat > 1 else ''}: "
                f"{self.n_levels} landings, {self.n_frames} frames, "
                f"total {fmt_duration(self.total_duration_s)}")

    def setup_text(self, run_name, started_iso, sg_ch, vmon_ch, imon_ch,
                   dry_run, cam_info='', dea_diam_mm=None, electrode=None,
                   concentration_ml=None):
        step_desc = (f"{self.step_kv:g} kV/step" if self.step_kv
                     else f"{self.n_steps_req} steps")
        return "\n".join([
            f"SLDEA Test  --  {run_name}",
            f"Started: {started_iso}",
            "MODE: *** DRY RUN (HV output OFF) ***" if dry_run
            else "MODE: LIVE (HV energized)",
            "",
            "--- Drive ---",
            f"HV gain: {HV_GAIN_KV_PER_V:g} V(control) = "
            f"{HV_GAIN_KV_PER_V:g} kV(Trek);  Trek max {TREK_MAX_KV:g} kV",
            f"SG: CH{sg_ch} DC control voltage (High-Z)",
            f"Sweep: {self.start_kv:g} -> {self.end_kv:g} kV, {step_desc}, "
            f"{len(self.levels)} levels"
            f"{', up/down' if self.updown else ''}"
            f"{f', repeat x{self.repeat}' if self.repeat > 1 else ''}",
            f"Ramp {self.ramp_s:g}s | Landing {self.landing_s:g}s | "
            f"Settle {self.settle_s:g}s | Snap-lead {self.snap_lead_s:g}s",
            f"Total: {fmt_duration(self.total_duration_s)}  "
            f"({self.n_levels} landings, {self.n_frames} frames)",
            "",
            "--- Measurement (Trek monitors on scope) ---",
            f"V_Out: scope CH{vmon_ch}  ({VMON_KV_PER_V:g} kV per scope-volt)",
            f"I_Out: scope CH{imon_ch}  ({IMON_UA_PER_V:g} uA per scope-volt; "
            f"10 V = 2000 uA)",
            "",
            "--- Camera ---",
            cam_info or "(settings not recorded)",
            ""] + ([f"DEA nominal diameter: {dea_diam_mm:g} mm", ""]
                   if dea_diam_mm else []) + (
            [f"Compliant electrode: {str(electrode).strip()}",
             f"Electrode family: {electrode_family(electrode)}"]
            if str(electrode or '').strip()
            else ["Compliant electrode: (not specified)"]) + (
            # Ink concentration (`#276`), beside the electrode it belongs
            # to. OMITTED ENTIRELY when the electrode is not an ink: a
            # carbon-black run must not carry a CNT-ink key at all, empty
            # or otherwise. When it DOES apply, a blank answer is written
            # as "(not specified)" rather than dropped -- the same rule the
            # electrode line follows, so a run that declined to answer and
            # a run that predates the field stay distinguishable.
            [f"Ink concentration: {str(concentration_ml).strip()} mL"
             if str(concentration_ml or '').strip()
             else "Ink concentration: (not specified)"]
            if concentration_applies(electrode) else []) + ["",
            "--- Snapshots ---",
            ("baseline @ 0 kV"
             + (f" (after a {self.baseline_warmup_s:g}s camera warm-up "
                f"frame tagged 'warmup'; the staircase starts at "
                f"{self.baseline_warmup_s:g}s)"
                if self.baseline_warmup_s > 0 else ""))
            if self.baseline else "(no baseline)",
            "per landing: "
            + ", ".join(([f"post-ramp (ramp-end + {self.settle_s:g}s)"]
                         if self.snap_post else [])
                        + ([f"pre-ramp (landing-end - {self.snap_lead_s:g}s, "
                            f"just before the next ramp)"]
                           if self.snap_pre else [])),
        ]) + "\n"
