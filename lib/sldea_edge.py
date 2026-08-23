#!/usr/bin/env python3
"""SLDEA edge detection: trace the active-area disc in run frames.

Pure analysis for the offline review GUI (sldea_edge_gui.py) -- no Tk in
here, so everything is unit-testable on synthetic images. A "run" is the
directory the Digital Multitool's SLDEA tab wrote:

    SLDEA_<ts>/setup.txt + data.csv + frames/SLDEA_sNN_XX.XXkV_tag.png

A live run also drops `telemetry.csv` there (the ~2 Hz monitor log, since
2026-08-05). NOTHING in this module reads it: `data.csv` is still the run,
`run_csv` cannot resolve to the sidecar, and a folder holding only
telemetry is not a run.

Approach: difference-imaging against the 0 kV baseline frame at three
threshold tiers, plus (2026-07-28) one candidate segmented from the
texture-ratio map -- the P3 devices activate by wrinkling with almost no
intensity displacement, and texture is the one channel where the device
outranks the electrode strips. Each outline is the merged-region contour of
the significant changed patches (activation reads as patchy wrinkling, and
the shape may be oblong -- no circle prior). Up to 3 candidates per frame
with a confidence score; frames whose best candidate is weak (or whose
candidates disagree) are queued for the human-in-the-loop pass in the GUI. Breakdown heuristics flag
suspect steps: a Trek current spike and an area collapse while voltage rises.

Since 2026-07-29 the primary channel is the BOUNDARY TRACKER
('disc-fit'): the resting disc is measured on the baseline
(baseline_disc, radial rays + robust circle fit), and each frame's
active area is the ink edge of that known object, tracked by rays and a
robust ellipse — the full responding disc, leads and the passive
wrinkle ring excluded, with a real 85% CI on area from the edge
scatter. Gated frames with a known disc report 'resting' instead of an
empty row. Same-kV pair agreement and channel hysteresis are folded
into confidence (reconcile_pairs / prev_method).

A note on 'conf': it is a review-ordering score — the strength of
internally consistent evidence — NOT a calibrated probability that the
boundary is correct. The pair bonus certifies against random error
only; correlated failure modes agree too. See SLDEA_HANDOFF.md
("Does higher conf mean more correct edges?") before leaning on it.

Areas are stored in px^2 and converted to mm^2 using the DEA's nominal
resting diameter (default 16 mm) against the baseline detection.

Headless self-test: .venv/bin/python tests/test_sldea_edge.py
"""
import csv
import math
import os
import re
import shutil

import numpy as np

# Settings persisted per-run in setup.txt (section replaced on save).
EDGE_HDR = '--- Edge Detection settings (SLDEA Edge Review) ---'
DEFAULT_SETTINGS = {
    'diam_mm': 16.0,        # DEA nominal resting active-area diameter
    'blur_px': 5,           # Gaussian blur kernel (odd)
    'diff_thresh': 0,       # fixed diff threshold; 0 = auto (Otsu)
    'min_diff': 6.0,        # diff p99 below this = "no change vs baseline"
    'min_solidity': 0.35,   # min FILL of the traced outline (changed/region)
    'roi_frac': 0.85,       # central search window (frame fraction)
    # Mask baseline px at/above this luminance; 0 = off (which also turns
    # off the texture footprint below -- one knob, both halves).
    #
    # Default RAISED 220 -> 255 (2026-08-05). The carbon-black run's
    # baseline is SATURATED (median 255), so a 220 cut masked 88% of the
    # frame including the context around the device, and detection
    # collapsed to a 1930 px sliver against a true ~134500 px. At 255 the
    # same frame traces at conf 0.99. On foil devices the change is a
    # measured no-op -- the texture footprint already covers the strips
    # and the brightness cut only ever caught the specular streaks inside
    # them (P3 areas identical to the pixel at both settings).
    #
    # Lower it per-run in the tuner if a device ever needs it; per-run
    # values live in that run's setup.txt and win over this. Note the CB
    # case is really an exposure problem (#193) that 255 works around --
    # see SLDEA_HANDOFF 2026-08-05.
    'electrode_lum': 255.0,
    'accept_conf': 0.75,    # auto-accept at/above this confidence
    'spread_pct': 12.0,     # candidate area disagreement that forces review
    # self-audit gates on acceptance: a winning boundary whose fitted arc
    # has no measurable ink step under more than audit_nostep_pct of its
    # length (the interpolated onset sectors), or whose median offset to
    # the measured step exceeds audit_bias_px (a 'resting' claim gone
    # stale -- the disc expands below the no-change gate's sensitivity in
    # the 1.5-3 kV band on all six bench runs), is capped below
    # accept_conf and tagged 'audit_nostep' / 'audit_bias'. 0 disables.
    'audit_nostep_pct': 15.0,
    'audit_bias_px': 3.0,
    # current deviation from the run's median that marks an event row
    # (2026-08-04 ground truth: true events sustain 26.5-208 uA of
    # deviation, false/borderline stay <= 14.6 -- 20 splits the margin):
    'breakdown_dev_ua': 20.0,
    'breakdown_ua': 50.0,   # gross ABSOLUTE fallback, used only when the
                            # run has < 5 parseable uA rows (no median)
    'area_jump_pct': 35.0,  # area collapse (V rising) that flags breakdown
    'wrinkle_ratio': 1.4,   # wrinkle index >= this = wrinkle-mode (active)
    'tex_seg': 1,           # also segment the texture-ratio map (0 = the
                            # intensity diff only, as before 2026-07-28)
    # photometric normalization vs the baseline: 0=off, 1=legacy scalar
    # border-band ratio, 2=gain+offset fit on ROI quantiles. 2 is the
    # default since 2026-07-28: on the bench runs the scalar left a
    # 26-gray-level pedestal that no threshold could sit above.
    'norm_bg': 2,
}
_NUM = r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?'


# ---------------------------------------------------------------------------
# settings <-> setup.txt
# ---------------------------------------------------------------------------

def load_settings(rundir):
    """DEFAULT_SETTINGS overlaid with any saved section in setup.txt; also
    picks up the run's 'DEA nominal diameter: X mm' line for diam_mm."""
    s = dict(DEFAULT_SETTINGS)
    path = os.path.join(rundir, 'setup.txt')
    try:
        # utf-8 + replace: setup.txt is bench-written UTF-8 and a stray
        # undecodable byte must degrade to defaults, not raise out of a
        # Tk callback (UnicodeDecodeError is NOT an OSError -- review
        # 2026-08-05 found it leaking a stale scale anchor via _pick_run)
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return s
    m = re.search(r'DEA nominal diameter:\s*(' + _NUM + r')\s*mm', text)
    if m:
        s['diam_mm'] = float(m.group(1))
    if EDGE_HDR in text:
        for line in text.split(EDGE_HDR, 1)[1].splitlines():
            mm = re.match(r'\s*([a-z_]+)\s*:\s*(' + _NUM + r')\s*$', line)
            if mm and mm.group(1) in s:
                s[mm.group(1)] = type(DEFAULT_SETTINGS[mm.group(1)])(
                    float(mm.group(2)))
    return s


def save_settings(rundir, settings):
    """Append/replace the edge-settings section in the run's setup.txt.

    Reads and writes UTF-8 with errors='replace' — the bare locale-codec
    open here carried the same UnicodeDecodeError hazard load_settings
    was hardened against, and a cp1252 WRITE of any replacement char
    raised right back out (audit 2026-08-05). Preserves the scale-anchor
    block (save_scale_anchor), which sits before this section."""
    path = os.path.join(rundir, 'setup.txt')
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        text = ''
    text, anchor = _split_anchor(text)
    if EDGE_HDR in text:
        text = text.split(EDGE_HDR, 1)[0].rstrip() + '\n'
    lines = [EDGE_HDR] + [f"{k}: {settings[k]:g}" for k in DEFAULT_SETTINGS]
    # Atomic (tmp + replace): the in-place truncate used to destroy the
    # run's only metadata record on a mid-write NAS failure (audit
    # 2026-07-25).
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text.rstrip() + '\n\n'
                + (anchor + '\n' if anchor else '')
                + '\n'.join(lines) + '\n')
    os.replace(tmp, path)
    return path


# The px→mm anchor Save used, persisted per run. Before 2026-08-05 the
# mandatory manual calibration was a pair of ephemeral clicks recorded
# NOWHERE: two sessions produced different absolute mm² from identical
# inputs with byte-identical notes, manual- and auto-anchored runs were
# indistinguishable in the saved data, and the anchor could never be
# audited or reused. The block sits BEFORE the edge-settings section so
# save_settings' section-replace preserves it.
ANCHOR_HDR = '--- Edge Review scale anchor (SLDEA) ---'
# rounds_px/n_rounds/spread_px/spread_pct/guard arrived with the #215
# fit-a-circle calibration (2026-08-06). EVERY key here is optional on
# read: 15 runs carry the 2026-08-05 two-click anchor with none of them,
# and two runs (P3_6_2.5mL_20260729, DOT_P3_1_20260729) predate the gate
# and carry no block at all. load_scale_anchor must keep serving both.
# cal_mode/sigma_pct/se_pct arrived with the two-point `twopoint` mode
# (2026-08-06 evening): with the round count configurable, a bare range is
# not comparable between anchors, so the n-aware conversion is recorded
# alongside it. `method` is NOT where the mode goes — mm_per_px matches it
# exactly against 'manual-calibration' to give a hand calibration priority
# over every automatic reference, so a mode suffix there would silently
# demote every twopoint anchor back below the disc fit.
# fit_*/verified_* arrived with the `verify` mode (2026-08-06 evening): an
# 'auto-verified' anchor carries NO rounds and NO spread — there is one
# automatic fit and a human who approved it — so what quantifies it is the
# fit's own quality, and what makes it auditable is who approved it when.
# Absent on every other anchor, exactly like rounds_px is absent here.
# reanchor/prev_*/reanchor_* arrived with the scale-only RE-ANCHOR
# (`#215`, 2026-08-06): correcting a run's scale and re-deriving its mm²
# from the stored px, with NO detection and NO re-review. Their whole
# purpose is that a later reader can tell "the scale was corrected" from
# "the run was reviewed again" — which nothing else in this block records,
# because every other field describes the anchor and not what was done
# with it. Absent on every anchor written by a normal Save, exactly like
# rounds_px is absent on a verify anchor.
_ANCHOR_KEYS = ('method', 'cal_mode', 'diam_px', 'diam_mm', 'mm_per_px',
                'anchor_frame', 'anchor_is_baseline', 'auto_diam_px',
                'n_rounds', 'rounds_px', 'spread_px', 'spread_pct',
                'sigma_pct', 'se_pct',
                'fit_circ', 'fit_conf', 'fit_resid_px', 'fit_arc_cov',
                'fit_n_edge', 'verified_by', 'verified_at',
                'reanchor', 'prev_diam_px', 'prev_implied_px',
                'prev_method', 'prev_cal_mode', 'reanchor_rows',
                'reanchor_blanked',
                'guard', 'saved', 'user')
_ANCHOR_FLOATS = ('diam_px', 'diam_mm', 'mm_per_px', 'auto_diam_px',
                  'spread_px', 'spread_pct', 'sigma_pct', 'se_pct',
                  'fit_circ', 'fit_conf', 'fit_resid_px', 'fit_arc_cov',
                  'prev_diam_px', 'prev_implied_px')
_ANCHOR_INTS = ('n_rounds', 'fit_n_edge', 'reanchor_rows',
                'reanchor_blanked')


def _split_anchor(text):
    """(text without the anchor block, the block or None)."""
    if text and not text.endswith('\n'):
        text += '\n'         # a header at EOF must still match HDR\n —
        # otherwise a save appends a SECOND block and the loader serves
        # the stale first one (review 2026-08-05)
    m = re.search(re.escape(ANCHOR_HDR) + r'\n(?:[a-z_]+:[^\n]*\n?)*',
                  text)
    if not m:
        return text, None
    return (text[:m.start()] + text[m.end():],
            m.group(0).rstrip() + '\n')


def save_scale_anchor(rundir, anchor):
    """Persist the anchor dict (method/diam_px/diam_mm/mm_per_px/…) into
    setup.txt, atomically, replacing any previous block. Timestamp and
    user are stamped here unless supplied."""
    import getpass
    import time
    path = os.path.join(rundir, 'setup.txt')
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        text = ''
    text, _old = _split_anchor(text)
    vals = dict(anchor)
    vals.setdefault('saved', time.strftime('%Y-%m-%dT%H:%M:%S'))
    try:
        vals.setdefault('user', getpass.getuser())
    except Exception:
        pass
    lines = [ANCHOR_HDR]
    for k in _ANCHOR_KEYS:
        v = vals.get(k)
        if v is None or v == '':
            continue
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (list, tuple)):
            # the per-round diameters (#215) ride ONE line: the block's
            # grammar is 'key: rest-of-line', so a multi-value field has
            # to be joined, never wrapped
            v = '; '.join(f"{float(x):.2f}" for x in v)
        if isinstance(v, float):
            v = f"{v:g}"
        # a stray newline in a text field (a pasted guard note) would
        # split the block and orphan everything after it
        v = str(v).replace('\r', ' ').replace('\n', ' ').strip()
        if not v:
            continue
        lines.append(f"{k}: {v}")
    block = '\n'.join(lines) + '\n'
    if EDGE_HDR in text:
        head, tail = text.split(EDGE_HDR, 1)
        body = head.rstrip() + '\n\n' + block + '\n' + EDGE_HDR + tail
    else:
        body = ((text.rstrip() + '\n\n') if text.strip() else '') + block
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body)
    os.replace(tmp, path)
    return path


def load_scale_anchor(rundir):
    """-> the recorded anchor dict or None. Shaped so it can be passed
    straight to mm_per_px as `baseline_ref` (method + diam_px), letting
    the tuner and diagnostic report the scale Edge Review actually
    saved instead of their own re-derivation.

    Every field beyond `diam_px` is OPTIONAL and absent fields are
    simply absent from the dict — no defaults are invented. That is what
    keeps the pre-#215 two-click anchors (no rounds/spread) and the
    pre-gate runs (no block at all -> None) loading and reporting."""
    path = os.path.join(rundir, 'setup.txt')
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    if text and not text.endswith('\n'):
        text += '\n'                     # same EOF corner as _split_anchor
    m = re.search(re.escape(ANCHOR_HDR) + r'\n((?:[a-z_]+:[^\n]*\n?)*)',
                  text)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        mm = re.match(r'([a-z_]+):\s*(.*)$', line)
        if not mm:
            continue
        k, v = mm.group(1), mm.group(2).strip()
        if k in _ANCHOR_FLOATS:
            try:
                out[k] = float(v)
            except ValueError:
                continue
        elif k in _ANCHOR_INTS:
            try:
                out[k] = int(float(v))
            except ValueError:
                continue
        elif k == 'rounds_px':
            # hand-edited setup.txt is a fact of bench life: keep the
            # values that parse, drop the ones that do not, and omit the
            # key entirely rather than serve a half-list as a fit record
            vals = []
            for tok in re.split(r'[;,]', v):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    vals.append(float(tok))
                except ValueError:
                    continue
            if vals:
                out[k] = vals
        elif k == 'anchor_is_baseline':
            out[k] = v not in ('0', '', 'False')
        elif k in ('cal_mode', 'prev_cal_mode'):
            # THE LEGACY LETTER RULE, applied at the boundary so no caller
            # can forget it (cal_mode_read). A block written before
            # 2026-08-06 late holds a pre-swap letter — live P3_2 holds
            # `cal_mode: C` AND `prev_cal_mode: C`, both meaning verify —
            # and under the new labels C is twopoint. Normalising here turns
            # every reader (sldea_diag, the reuse path, the status line)
            # honest at once, and the value that goes back out on a re-save
            # is the NAME, i.e. a correct translation of what the letter
            # meant rather than a letter that has changed meaning.
            #
            # `default=v` keeps an unrecognised token verbatim: a reader
            # must see what is actually on disk, not a guess.
            out[k] = cal_mode_read(v, default=v)
        else:
            out[k] = v
    return out if out.get('diam_px') else None


# ---------------------------------------------------------------------------
# the scale anchor's own arithmetic (#215) — three averaged circle fits,
# their spread, and the two independent references that can contradict them
# ---------------------------------------------------------------------------

# Rounds the operator fits per run. Three is the minimum that yields a
# spread at all; the mean of n fits shrinks the random part of the
# scale-anchor term by sqrt(n) (SLDEA_MEASUREMENT §2.1).
CAL_ROUNDS = 3
# (max-min)/mean of the fitted diameters above which the rounds
# DISAGREE and a further round is offered (#215 spread gate).
#
# NO LONGER THE ACCEPTANCE GATE (2026-08-06 evening, `#215`). The range is
# still RECORDED and still reported, but the gate is now se_ok below. Two
# reasons, both from real measurements: a range is not comparable across
# different n (the range of 5 fits is ~1.37x the range of 3 at the same
# precision — see D2_RANGE_FACTORS), and a range cannot shrink when a
# round is added, so a range gate's own remedy could never clear it.
CAL_SPREAD_PCT = 1.0

# ===========================================================================
# THE THREE CALIBRATION METHODS — recorded as NAMES, labelled with letters
# ===========================================================================
# The value that goes on disk is a self-describing NAME ('verify' /
# 'circle' / 'twopoint'); the letters A / B / C are UI labels only
# (CAL_MODE_LABELS). That split exists because the letters have already
# been RENUMBERED once, on 2026-08-06 late, at the operator's request:
#
#     before the swap        after the swap
#     A = circle             A = verify     (where the gate opens)
#     B = twopoint           B = circle
#     C = verify             C = twopoint
#
# and the old letters are ON DISK. `scale_calibration_log.txt` carries
# `mode=A|B|C` and setup.txt's anchor block carries `cal_mode:` — live
# campaign data included (P3_2_2.5mL_20260728's block reads `cal_mode: C`
# and its log holds 8 mode=C lines, all meaning VERIFY). Renumbering a
# stored letter reinterprets every one of them: a logged `mode=C` would
# flip from "verified the automatic fit" to "clicked two points", silently.
#
# So: nothing writes a letter ever again, and every read of a stored value
# goes through `cal_mode_read`, whose rule for a legacy letter is the
# PRE-SWAP meaning above. A name is immune to the next relabelling too,
# which is the actual fix — the letters are presentation and presentation
# is what operators change their minds about.
#
# ---- circle: fit a circle to the boundary (label B; was label A) --------
# The operator drove the circle mode six times on a scratch copy of
# P3_2_2.5mL_20260728 (`#215` comment, 2026-08-06). Recorded 3-round
# ranges: 1.94, 2.09, 1.62, 1.81, 1.44 % plus one under 1 %. Via d2(3)
# that is per-fit sigma ~ 1.05 % of diameter, so the 3-round mean SE is
# 0.61 % diameter / 1.21 % area against §2.1's ~0.4 % / ~0.8 % budget.
# The operator's diagnosis: "the bright green circle occludes the edges" —
# a 3 px stroke laid on the boundary hides the feature being aligned to.
# ---- twopoint: two opposite edge points (label C; was label B) ----------
# The alternative measured against it: two clicks on roughly opposite edge
# points, N rounds, the image RANDOMLY ROTATED between rounds, and markers
# that do not cover the boundary.
CAL_MODE_CIRCLE = 'circle'
CAL_MODE_TWOPOINT = 'twopoint'
# ---- verify: the machine measures, the operator VERIFIES (label A) ------
# The A/B/A' experiment settled it (`#215` comment, 2026-08-06 evening):
# eleven hand calibrations on P3_2's baseline against an automatic fit of
# 577.08 px (circ 0.999, conf 0.871, residual 2.3 px, 204 edge points).
# The automatic fit beat ALL ELEVEN on accuracy and NINE OF ELEVEN on
# precision. Per-fit human precision is sigma ~ 1.0-1.1 % of diameter
# regardless of method or stroke width, needing ~7 rounds to average down
# to the 0.4 % SE gate.
#
# The radial intensity profile of that baseline says why: the disc reads
# 166 gray, the paper 186, and that 20-level step is spread over ~60 px of
# RADIUS. There is no line to click. Asking an operator to pick "the edge"
# is asking them to pick a point inside a gradient wider than the stroke
# they draw with, and the point they pick is the outer toe (SLDEA_MEASUREMENT
# 1.3: +2.6 % diameter). baseline_disc instead takes the strongest
# dark->light step on each of ~204 surviving radial rays and fits a circle
# robustly to them.
#
# So the machine MEASURES and the operator VERIFIES, with hand measurement
# kept for the runs where the fit refuses -- as it does on
# P3_7_2.3mL_20260729.
CAL_MODE_VERIFY = 'verify'
# Ordered as the chooser shows them: verify first, because that is where
# the gate opens and the label row reads A, B, C down the row.
CAL_MODES = (CAL_MODE_VERIFY, CAL_MODE_CIRCLE, CAL_MODE_TWOPOINT)
CAL_MANUAL_MODES = (CAL_MODE_CIRCLE, CAL_MODE_TWOPOINT)
# THE UI LABELS — presentation only, never written to a record. Swapped
# 2026-08-06 late at the operator's request so the letter an operator reads
# first is the method the gate actually opens in.
CAL_MODE_LABELS = {CAL_MODE_VERIFY: 'A',
                   CAL_MODE_CIRCLE: 'B',
                   CAL_MODE_TWOPOINT: 'C'}
# THE READ RULE for a letter found in an existing record — the PRE-SWAP
# meanings, which are the only meanings any letter on disk can have,
# because the swap and the letters-stop-being-written change landed
# together. Never extend this with post-swap letters: that would make a
# stored letter ambiguous, which is precisely what the names avoid.
CAL_MODE_LEGACY = {'A': CAL_MODE_CIRCLE,
                   'B': CAL_MODE_TWOPOINT,
                   'C': CAL_MODE_VERIFY}
# Default rounds per mode. `circle` keeps 3 so its behaviour is unchanged;
# `twopoint` defaults to 5 because at the target per-fit sigma < 0.9 %, 5
# rounds gives SE = 0.40 % and lands on §2.1's budget. `verify` has NO
# rounds: there is one automatic fit and it is either approved or it is
# not, so it is deliberately absent from this table rather than given a 1.
CAL_ROUNDS_TWOPOINT = 5
CAL_MODE_ROUNDS = {CAL_MODE_CIRCLE: CAL_ROUNDS,
                   CAL_MODE_TWOPOINT: CAL_ROUNDS_TWOPOINT}
# Which MANUAL mode the dialog falls back to. The circle, the incumbent:
# switching it would change every existing hand-calibration path silently.
CAL_DEFAULT_MODE = CAL_MODE_CIRCLE


def cal_mode_read(value, default=None):
    """The canonical mode NAME for a value read back from any record —
    setup.txt's `cal_mode:` / `prev_cal_mode:`, a `mode=` field in
    scale_calibration_log.txt, or anything else that stored one.

    THE LEGACY RULE, in one place so every reader shares it: a bare letter
    is a PRE-SWAP letter and means what it meant before 2026-08-06 late —
    **A = circle, B = twopoint, C = verify** (CAL_MODE_LEGACY). Live
    campaign data depends on this: `P3_2_2.5mL_20260728/setup.txt` records
    `cal_mode: C`, written when C meant verify, and its
    scale_calibration_log.txt holds eight `mode=C` lines with the same
    meaning. Under the new labels C is twopoint, so without this rule every
    one of those records would silently claim the operator clicked two
    points on a run where they approved an automatic fit.

    An unrecognised token comes back as `default` rather than being guessed
    at — a hand-edited setup.txt is a fact of bench life, and inventing a
    method for a typo is worse than reporting that there is none."""
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    if s in CAL_MODES:
        return s
    if s.lower() in CAL_MODES:
        return s.lower()
    return CAL_MODE_LEGACY.get(s.upper(), default)


def cal_mode_label(value):
    """The UI letter for a mode value — legacy letters included, via
    cal_mode_read. '?' when the value names no method this build knows."""
    return CAL_MODE_LABELS.get(cal_mode_read(value), '?')


def cal_mode_text(value):
    """How a mode is NAMED to a human: `A (verify)`.

    Both halves on purpose. The letter is what the operator sees on the
    dialog's radio row today; the name is what the record holds and what
    survives the next relabelling. A report that quoted only the letter
    would be unreadable after the next swap, and one that quoted only the
    name would not match the screen."""
    name = cal_mode_read(value)
    if not name:
        # not a method this build knows: quote it verbatim, flagged, rather
        # than dropping it or laundering it into a label
        return f"{str(value).strip()} (unrecognised method)" if value else ''
    return f"{CAL_MODE_LABELS[name]} ({name})"

# The two methods that produce an anchor a human is answerable for, and
# which therefore override every automatic reference at Save (_is_manual_cal).
# They are DISTINCT VALUES on purpose: anyone auditing a run later has to be
# able to tell "a human measured this" from "a human approved the machine's
# measurement", and 15 runs already carry the two-click 'manual-calibration'
# string. Never fold the calibration MODE into `method` — mm_per_px matches
# this set exactly.
ANCHOR_METHOD_MANUAL = 'manual-calibration'
ANCHOR_METHOD_VERIFIED = 'auto-verified'
ANCHOR_METHODS = (ANCHOR_METHOD_MANUAL, ANCHOR_METHOD_VERIFIED)


def cal_open_mode(auto_ref):
    """Which method the calibration gate OPENS in.

    `verify` (label A) whenever there is an automatic fit to verify, because
    on the one disc anyone has measured the machine beat every human
    attempt; the incumbent manual mode when there is not, because a fit that
    refused cannot be verified and the operator has to measure after all.
    The label row is ordered so that this mode is also the FIRST letter —
    the default and the letter an operator reads first are the same thing.

    Takes the fit itself rather than a flag so the caller cannot claim mode
    `verify` is available without holding the thing it displays."""
    return (CAL_MODE_VERIFY if (auto_ref or {}).get('diam_px')
            else CAL_DEFAULT_MODE)

# Expected range of n samples from a normal distribution, in units of
# sigma — the control-chart d2 factors. Source: ASTM E2587 "Standard
# Practice for Use of Control Charts in Statistical Process Control",
# table of factors for the range chart; identically tabulated in Duncan,
# *Quality Control and Industrial Statistics*, and the value at n=3
# (1.693) is the one SLDEA_MEASUREMENT §2.1a was originally written
# around. A LOOKUP, never a formula fitted by eye: there is no closed
# form, and an interpolated d2 would put an invented number into the
# error budget.
D2_RANGE_FACTORS = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326,
                    6: 2.534, 7: 2.704, 8: 2.847}
# The acceptance gate, on the MEAN's standard error rather than the raw
# range (2026-08-06 evening). The threshold is DERIVED FROM §2.1's
# standing scale-anchor budget of ~0.4 % diameter / ~0.8 % area — it is
# not a measured number and not a second invented one: the budget already
# existed, and SE is the quantity the budget is expressed in. Area SE is
# twice this by area ∝ diameter², i.e. 0.8 %, so one threshold covers
# both rows of §2.1.
CAL_SE_PCT = 0.4
# Anchor sanity guard (2026-08-06, added on top of #215's flow). Two
# tolerances, both generous: §2.1 puts the whole scale-anchor term at
# ~0.4 % diameter / ~0.8 % area, so 1 % is already outside the budget.
# Run P3_2_2.5mL_20260728 sat at +2.28 % diameter / -4.42 % area and
# nothing warned, because the pre-existing cross-check only fired at 3 %.
ANCHOR_GUARD_DIAM_PCT = 1.0
ANCHOR_GUARD_AREA_PCT = 1.0
# The pre-existing detect-time tier (scale gate 2026-08-05), kept as the
# threshold for a MODAL after detection. The guard above is the one that
# demands a decision, at calibration time; repeating it as a modal on
# every 1% would nag every honest run, because baseline_disc agrees with
# the by-eye measurement only to ~1% itself (SLDEA_MEASUREMENT §2.1).
ANCHOR_MODAL_DIAM_PCT = 3.0


def d2(n):
    """The expected range of `n` normal samples, in units of sigma —
    D2_RANGE_FACTORS[n], or **None** when the table has no factor for n.

    None is the whole point. §2.1a used to hard-wire the n=3 constants
    (sigma ~ R/1.693, mean SE ~ R/2.93, area ~ R/1.47); with the round
    count configurable those are simply wrong for any other n, and a
    silent fallback — nearest neighbour, an interpolation, or just reusing
    1.693 — would push a wrong error term into the budget without anyone
    seeing it happen. Every caller here refuses to convert instead."""
    try:
        return D2_RANGE_FACTORS.get(int(n))
    except (TypeError, ValueError):
        return None


def calibration_stats(diams):
    """Mean, range and the n-aware precision conversion of the per-round
    fitted diameters (#215; n-aware since 2026-08-06 evening).

    The mean is the anchor; the scatter is a free per-run measurement of
    operator repeatability, the one term of the error budget that has
    never had data (`sldea_trace` finds zero repeat pairs across 140
    labels). Plain mean of EVERY round performed — no outlier rejection,
    because silently dropping a round would edit the very number the
    spread is supposed to report. -> None for no rounds.

    The conversion (SLDEA_MEASUREMENT §2.1a):

        sigma  ~ R / d2(n)          per-fit precision — the METHOD's own
                                    property, and the only figure that is
                                    comparable between two modes run at
                                    different round counts
        SE     = sigma / sqrt(n)    precision of the MEAN, which is the
                                    number the anchor actually carries
        area SE = 2 x SE            area ∝ diameter²

    `d2`/`sigma_px`/`sigma_pct`/`se_px`/`se_pct`/`area_se_pct` are all
    **None together** when d2(n) has no factor — n=1 (no scatter to
    convert) or n outside the table. A None here is a refusal to convert,
    not a zero, and se_ok() will not call it a pass."""
    vals = [float(v) for v in (diams or []) if v]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    lo, hi = min(vals), max(vals)
    rng = hi - lo
    out = {'n': n, 'values': vals, 'mean': mean,
           'min': lo, 'max': hi, 'spread_px': rng,
           'spread_pct': (100.0 * rng / mean) if mean else 0.0,
           'd2': d2(n), 'sigma_px': None, 'sigma_pct': None,
           'se_px': None, 'se_pct': None, 'area_se_pct': None}
    if out['d2'] and mean:
        out['sigma_px'] = rng / out['d2']
        out['se_px'] = out['sigma_px'] / math.sqrt(n)
        out['sigma_pct'] = 100.0 * out['sigma_px'] / mean
        out['se_pct'] = 100.0 * out['se_px'] / mean
        out['area_se_pct'] = 2.0 * out['se_pct']
    return out


def verify_stats(ref):
    """A `calibration_stats`-shaped record for ONE automatic fit — the
    `verify` mode's
    anchor — or None without a usable fit.

    Every precision field is **None, never 0**. That is the whole point of
    this function existing rather than calling `calibration_stats([diam])`:
    a single fit has no range and no scatter, and `spread_pct = 0.0`
    (which is what `calibration_stats` would honestly return for one value)
    reads as PERFECT PRECISION to every downstream reader — the log line,
    the status line and `sldea_diag` would all print 0.00 % where the truth
    is "undefined". A refusal to state a number is not the same claim as
    zero, and this code has already been bitten once by conflating them
    (`se_ok` returns None rather than True for an unjudgeable set).

    What DOES quantify a verify anchor is the fit's own quality — its
    residual over 200-odd edge points, its circularity, its arc coverage —
    which travels in the record's `fit_*` fields, not here."""
    d = float((ref or {}).get('diam_px') or 0.0)
    if d <= 0:
        return None
    return {'n': 1, 'values': [d], 'mean': d, 'min': d, 'max': d,
            'spread_px': None, 'spread_pct': None, 'd2': None,
            'sigma_px': None, 'sigma_pct': None, 'se_px': None,
            'se_pct': None, 'area_se_pct': None,
            'single_fit': True}


def fit_resid_pct(ref):
    """The automatic fit's median edge-point residual as a % of the fitted
    DIAMETER — the honest size of an auto-verified anchor's uncertainty, or
    None when the fit did not report one.

    On P3_2's baseline that is 2.3 px on 577.08 px = **0.40 %**, which lands
    on SLDEA_MEASUREMENT §2.1's ~0.4 % diameter budget. It is NOT an
    operator-repeatability term: nobody fitted anything, so there is no
    scatter across rounds to convert, and quoting σ/SE for a verify anchor
    would be quoting a statistic of a sample of one.

    Deliberately CONSERVATIVE. This is the per-point scatter of the edge
    points about the fitted circle, not the standard error of the fitted
    radius — with n_edge points the formal SE of the radius is roughly
    sqrt(n) smaller (~0.03 % at n = 204). The per-point figure is quoted
    because it is the number the fitter actually measured and because
    over-stating this term is the safe direction: the systematic part (which
    feature the step-finder locks onto vs the true mechanical boundary) is
    not in either figure and is not measured at all."""
    d = float((ref or {}).get('diam_px') or 0.0)
    resid = (ref or {}).get('fit_resid_px')
    if d <= 0 or resid is None:
        return None
    try:
        return 100.0 * float(resid) / d
    except (TypeError, ValueError):
        return None


def verify_note(ref, who=None, when=None):
    """One ASCII line for the anchor record's `guard` field when the
    operator APPROVED the automatic fit (the `verify` mode).

    It says what was and was not checked, because for this anchor those are
    different from every other anchor's and the difference is not intuitive:

    **There is no independent cross-check available for an automatic
    anchor.** Declaring the fitted disc to be `diam_mm` makes the resting
    area π·(diam_mm/2)² *by construction*, so running `anchor_guard` on a
    verify anchor returns +0.00 % on both of its tests, always, on any
    frame, however wrong the fit is. It is not a check that passed; it is a
    check that cannot fail, and printing it would manufacture confidence out
    of an identity. (The same algebra is why `anchor_guard`'s two tests were
    already known to be one measurement in two units — see its docstring.)

    So the verification recorded here is the OPERATOR'S EYE, supported by
    the fit's own quality numbers, and the line says so in those words."""
    d = float((ref or {}).get('diam_px') or 0.0)
    bits = [f"AUTO-VERIFIED by eye: operator approved the automatic disc "
            f"fit ({d:.1f} px"]
    for key, label, fmt in (('circ', 'circ', '{:.3f}'),
                            ('conf', 'conf', '{:.3f}'),
                            ('fit_resid_px', 'resid', '{:.1f}px'),
                            ('arc_cov', 'arc', '{:.2f}')):
        v = (ref or {}).get(key)
        if v is not None:
            bits.append(f"{label} " + fmt.format(float(v)))
    n_edge = (ref or {}).get('n_edge')
    if n_edge:
        bits.append(f"{int(n_edge)} edge pts")
    body = bits[0] + ((', ' + ', '.join(bits[1:])) if len(bits) > 1 else '')
    who_when = ''
    if who or when:
        who_when = (' - verified by ' + (str(who) or '?')
                    + (f" at {when}" if when else ''))
    return (body + '). NOT cross-checked against anything independent: '
            'the anchor IS the automatic fit, so both of anchor_guard\'s '
            'references are vacuous on it (the mask-area test reduces to '
            'pi*(d/2)^2 = pi*(d/2)^2). The check is the human eye plus the '
            'fit quality above.' + who_when).replace('\r', ' ')\
        .replace('\n', ' ')


def sigma_from_range(spread_pct, n):
    """Per-fit sigma (%) from a RECORDED range (%) and its round count —
    the read-back path for an anchor already in setup.txt, where the
    individual diameters may not have survived. None when d2(n) refuses,
    and None is then carried all the way out to the operator rather than
    being papered over."""
    f = d2(n)
    if not f or spread_pct is None:
        return None
    try:
        return float(spread_pct) / f
    except (TypeError, ValueError):
        return None


def se_ok(stats, limit=CAL_SE_PCT):
    """THE ACCEPTANCE GATE (2026-08-06 evening): True when the mean's
    standard error is inside §2.1's scale-anchor budget.

    Three-valued on purpose — True / False / **None = cannot be judged**.
    None is returned for a single round and for any n the d2 table has no
    factor for, and the dialog treats it as its own gate with its own
    modal. An unjudgeable set must NOT read as a pass: `spread_ok` let
    n < 2 through vacuously, which was honest for a range (there is no
    range) but would be a lie here, because the budget this gate enforces
    is on a quantity that was never computed.

    Why SE and not the range this replaced (measured, 2026-08-06): a range
    is not comparable across n, and a range cannot shrink when a round is
    added — so the remedy a range gate offers can never clear it. SE falls
    as 1/sqrt(n), so `rounds_for_se` can name the n that WOULD clear it,
    and SE is the quantity §2.1 budgets in the first place."""
    if not stats:
        return None
    if stats.get('n', 0) < 2 or stats.get('se_pct') is None:
        return None
    return float(stats['se_pct']) <= float(limit)


def rounds_for_se(sigma_pct, limit=CAL_SE_PCT):
    """Smallest round count whose mean SE would meet `limit` at a measured
    per-fit `sigma_pct` — the remedy an SE gate can name and a range gate
    could not. None when sigma is unusable.

    Not capped to the d2 table here: a caller that gets 11 back needs to
    be told the method cannot reach budget within the table, not handed a
    quietly clipped 8."""
    try:
        s = float(sigma_pct)
    except (TypeError, ValueError):
        return None
    if s <= 0 or float(limit) <= 0:
        return None
    return max(2, int(math.ceil((s / float(limit)) ** 2)))


def spread_ok(stats, limit=CAL_SPREAD_PCT):
    """Whether the raw RANGE of the rounds is inside CAL_SPREAD_PCT.

    NO LONGER THE ACCEPTANCE GATE — `se_ok` is (2026-08-06 evening). Kept
    because the range is still the persisted statistic and still worth
    quoting, and because the property pinned here is worth keeping pinned:
    max−min NEVER shrinks when a round is added, so ADDING ROUNDS COULD
    NOT CLEAR THIS GATE. That, plus the fact that a range is not
    comparable between an n=3 and an n=5 anchor (d2(5)/d2(3) = 1.37), is
    why the gate moved to the mean's standard error — which does fall as
    1/sqrt(n) and is the quantity §2.1 budgets.

    A single round has no range, so it passes vacuously — honest for a
    range, and exactly why `se_ok` refuses to do the same."""
    return not stats or stats['n'] < 2 or stats['spread_pct'] <= limit


def rescale_pct(old_diam_px, new_diam_px):
    """% every absolute mm² already in the run MOVES when the anchor
    changes from old_diam_px to new_diam_px.

    This is the [critical] partial-re-save interaction (SLDEA_HANDOFF
    2026-08-05): unreviewed rows keep their px measurement and their
    mm²/diam are RE-DERIVED at the current save's scale, so re-reviewing
    ONE frame under a new calibration rewrites the whole run's absolute
    column. area ∝ mm_per_px² ∝ 1/diam_px², hence the square — a bigger
    diameter means a smaller mm/px and every area shrinks. None when
    either diameter is unusable."""
    if not old_diam_px or not new_diam_px:
        return None
    return 100.0 * ((float(old_diam_px) / float(new_diam_px)) ** 2 - 1.0)


def anchor_guard(mean_diam_px, auto_ref, diam_mm,
                 diam_tol=ANCHOR_GUARD_DIAM_PCT,
                 area_tol=ANCHOR_GUARD_AREA_PCT):
    """Sanity-check an accepted px→mm anchor against the two references
    the app already holds but never used to check the operator.

    1. the automatic baseline disc fit (`baseline_disc`) — an
       independent measurement of the same disc, in px;
    2. the 16 mm laser-cut application-mask anchor
       (SLDEA_MEASUREMENT §2.4), which fixes the resting area at
       π·(diam_mm/2)² = 201.06 mm² for this whole series.

    Honest about what these are: `baseline_disc` returns a CIRCLE fit,
    so its area_px is π·r² and the area test is algebraically the
    diameter test squared. They are one measurement in two units, not
    two independent votes — which is exactly why both are shown. A 1 %
    area tolerance is the binding one (~0.5 % in diameter), and the area
    figure is the one the operator's downstream numbers are quoted in.
    The guard measures ACCURACY against an external reference; the
    three-round spread measures PRECISION. A consistently biased
    operator scores a tight spread and still trips this.

    Returns a dict of the computed numbers, always, with 'warn' a list
    of operator-facing strings (empty = clear) and 'available' False
    when there is no automatic fit to check against."""
    mean = float(mean_diam_px or 0.0)
    dmm = float(diam_mm or 0.0)
    nominal = math.pi * (dmm / 2.0) ** 2 if dmm > 0 else None
    out = {'mean_diam_px': mean or None, 'auto_diam_px': None,
           'diam_pct': None, 'rest_area_mm2': None,
           'nominal_mm2': nominal, 'area_pct': None,
           'available': False, 'warn': []}
    if mean <= 0 or dmm <= 0:
        return out
    auto_px = float((auto_ref or {}).get('diam_px') or 0.0)
    if auto_px <= 0:
        return out
    out['available'] = True
    out['auto_diam_px'] = auto_px
    out['diam_pct'] = 100.0 * (mean - auto_px) / auto_px
    mm_per_px = dmm / mean
    # prefer the fit's own measured area; fall back to its circle
    area_px = float((auto_ref or {}).get('area_px') or 0.0)
    if area_px <= 0:
        area_px = math.pi * (auto_px / 2.0) ** 2
    out['rest_area_mm2'] = area_px * mm_per_px * mm_per_px
    out['area_pct'] = 100.0 * (out['rest_area_mm2'] - nominal) / nominal
    if abs(out['diam_pct']) > float(diam_tol):
        out['warn'].append(
            f"accepted diameter {mean:.1f} px is {out['diam_pct']:+.2f}% "
            f"from the automatic disc fit ({auto_px:.1f} px) — over the "
            f"{float(diam_tol):g}% guard")
    if abs(out['area_pct']) > float(area_tol):
        out['warn'].append(
            f"the resting area this scale implies is "
            f"{out['rest_area_mm2']:.2f} mm², {out['area_pct']:+.2f}% "
            f"from the {dmm:g} mm mask anchor's "
            f"{nominal:.2f} mm² — over the {float(area_tol):g}% guard")
    return out


def guard_is_vacuous(anchor):
    """True when running `anchor_guard` on this anchor could only ever
    PASS, so its result must not be shown as a cross-check anywhere.

    The case is an 'auto-verified' anchor (the `verify` mode): its diameter IS
    the automatic fit's diameter, so `diam_pct` is exactly 0, and declaring
    that fitted disc to be `diam_mm` makes the implied resting area
    π·(diam_mm/2)² by construction, so `area_pct` is exactly 0 too. Both of
    the guard's references reduce to identities. A green tick from a test
    that cannot fail is worse than no test: it is a claim of verification
    the code did not perform.

    Takes the ANCHOR (or manual_ref) rather than a boolean so no caller can
    assert vacuity, or forget to, independently of what the anchor says it
    is. Every reader of `anchor_guard`'s output must consult this first."""
    return (anchor or {}).get('method') == ANCHOR_METHOD_VERIFIED


def anchor_guard_note(guard, overridden):
    """One ASCII line recording what the guard said and what the
    operator did about it, for the setup.txt `guard` field. The point of
    the guard is that a deviation becomes a DECISION rather than
    silence, so the decision has to be in the run's record too.

    An UNAVAILABLE cross-check is recorded as a gap, in the same voice as
    a trip — never as a quiet nil. Review 2026-08-06 found the opposite:
    on the fallback-frame path (`_anchor_frame` finds a later frame,
    `_base_gray` refuses because the baseline row itself will not load)
    the guard returns available=False with an empty warn list and the
    dialog accepted in silence, directly after announcing that it was
    cross-checking. Nothing above OK severity ever fired."""
    if not guard or not guard.get('available'):
        return ('NOT CROSS-CHECKED: no automatic disc fit was available'
                + (' - accepted anyway by operator' if overridden else ''))
    body = (f"auto {guard['diam_pct']:+.2f}% diam, "
            f"mask {guard['area_pct']:+.2f}% area")
    if not guard.get('warn'):
        return 'clear (' + body + ')'
    return ('OVERRIDDEN by operator: ' if overridden
            else 'tripped: ') + body


# ---------------------------------------------------------------------------
# RE-ANCHOR — correcting a run's px→mm scale WITHOUT re-reviewing it
# (`#215`, 2026-08-06)
#
# The corpus-wide sweep (_analysis/auto_calibration_sweep_20260806.md) closed
# the scale chain: every one of the eleven recorded resting areas is explained
# to two decimals by its anchor's deviation from the automatic fit, and the
# eight runs that never had a manual anchor are exactly the eight that land on
# π·(diam_mm/2)² perfectly. So on three runs — P3_2_2.5mL_20260728 (−4.42 % in
# area), SLDEA_20260723_152205 (−3.38 %) and SLDEA_20260723_233451 (+2.44 %) —
# the PIXEL measurements are right and only the px→mm factor is wrong.
#
# Correcting one used to cost a full detect-and-save cycle (minutes of
# detection over 81 frames) even though no review work needed redoing, and an
# operator who accepted the corrected fit on live P3_2 closed before Save
# because of that cost — losing the correction. Re-deriving mm² from px is
# arithmetic `apply_results` already performs on every unreviewed row, so a
# re-anchor is that same arithmetic applied to EVERY row, with an empty
# results dict:
#
#     apply_results(rows, {}, new_scale, {}, None)
#
# Nothing is reimplemented. That call takes the `else` branch for every row,
# which is the rule the [critical] partial-re-save entry put in force
# (SLDEA_HANDOFF 2026-08-05): a row keeps its px and has its mm²/diam
# RE-DERIVED at this scale, and a bug-era mm² with NO px is blanked rather
# than kept on an unknowable anchor. It also leaves `notes` byte-identical
# (the branch reads the row's own note back), touches no other column, and —
# because `plan_breakdown_marks`/`apply_rename_plan` are never called — cannot
# re-apply or revert a `*_BREAKDOWN` frame rename.
#
# What lives here is the arithmetic the CONFIRMATION needs, which the Save
# path never had to compute: how many rows would blank, what the old scale
# actually was, and where the resting area lands. The functions below are
# pure — they read rows and return numbers, and mutate nothing.
# ---------------------------------------------------------------------------

# The `reanchor` field's value in the anchor block. A token, not prose: the
# point of it is that a later reader can tell "the scale was corrected" from
# "the run was reviewed again", and sldea_diag greps for it.
REANCHOR_SCALE_ONLY = 'scale-only'
# How far two rows' implied old scales may differ (% in area) before the
# column is reported as carrying MORE THAN ONE absolute scale. Rows are
# stored to 3 decimals of mm² against a 0-decimal px, so a few 1e-3 of a
# percent is pure rounding; 0.05 % is far below any real anchor change and
# far above the rounding floor. This is the [critical] mixed-scale state
# (a 56.1 % artificial step on the real-data repro) made VISIBLE rather
# than merely prevented going forward.
REANCHOR_MIXED_TOL_PCT = 0.05


def implied_scale(row):
    """The mm_per_px a single row's OWN recorded numbers were derived at,
    or None when the row carries no usable pair.

    area_mm² = area_px · scale², so scale = sqrt(mm²/px) — the same
    inversion `apply_results` performs to preserve a row's diameter
    definition across a re-scale. This is what makes a re-anchor possible
    on the eight runs that carry NO anchor block at all: the scale their
    column was derived at is recoverable from the column itself, and does
    not depend on setup.txt having recorded anything."""
    px = _num((row or {}).get('active_area_px'))
    mm2 = _num((row or {}).get('active_area_mm2'))
    if not px or px <= 0 or not mm2 or mm2 <= 0:
        return None
    return math.sqrt(mm2 / px)


def reanchor_plan(rows, new_scale, diam_mm, recorded=None,
                  mixed_tol_pct=REANCHOR_MIXED_TOL_PCT):
    """Everything a re-anchor confirmation must state, computed WITHOUT
    touching `rows`.

    `new_scale` may be None to count rows before a new anchor exists (the
    refusal check runs before the calibration dialog opens); every field
    that depends on the new scale is then None.

    The counts are deliberately computed with the SAME predicates
    `apply_results`' unreviewed branch uses — `px and px > 0` to re-derive,
    `old_mm2 or old_diam` to blank — because a confirmation that reports
    different numbers from the ones the commit produces is worse than no
    confirmation. Never a percentage against a reference that does not
    exist: with no old scale recoverable, `mult` is None and the caller
    says so rather than printing a number it made up."""
    rows = rows or []
    dmm = float(diam_mm or 0.0)
    ns = float(new_scale) if new_scale else None
    out = {'n_rows': len(rows), 'n_derive': 0, 'n_blank': 0, 'n_fresh': 0,
           'n_untouched': 0, 'new_scale': ns, 'new_diam_px': None,
           'old_scale': None, 'old_diam_px': None, 'recorded_diam_px': None,
           'anchor_matches_data': None, 'mult': None, 'diam_mult': None,
           'rest_px': None, 'rest_before': None, 'rest_after': None,
           'rest_row': None, 'rest_is_baseline': None,
           'nominal_mm2': (math.pi * (dmm / 2.0) ** 2) if dmm > 0 else None,
           'rest_dev_before': None, 'rest_dev_after': None,
           'mixed': False, 'scale_span_pct': None, 'n_scales': 0}
    scales, rest_i, first_px = [], None, None
    for i, row in enumerate(rows):
        px = _num(row.get('active_area_px'))
        old_mm2 = _num(row.get('active_area_mm2'))
        old_diam = _num(row.get('active_diam_mm'))
        if px and px > 0:
            out['n_derive'] += 1
            if first_px is None:
                first_px = i
            sc = implied_scale(row)
            if sc:
                scales.append(sc)
            else:
                # px with no previous mm²: this row GAINS an absolute area
                # for the first time, so there is no 'move' to quote for it
                out['n_fresh'] += 1
            if rest_i is None and row.get('tag') == 'baseline':
                rest_i = i
        elif old_mm2 or old_diam:
            out['n_blank'] += 1
        else:
            out['n_untouched'] += 1
    if ns and ns > 0 and dmm > 0:
        out['new_diam_px'] = dmm / ns
    if scales:
        out['n_scales'] = len(scales)
        lo, hi = min(scales), max(scales)
        # in AREA terms, which is the column being rewritten
        out['scale_span_pct'] = 100.0 * ((hi / lo) ** 2 - 1.0)
        out['mixed'] = out['scale_span_pct'] > float(mixed_tol_pct)
    # The resting row: the one tagged 'baseline'. Its recorded mm² is the
    # number the sweep table quotes and the number π·(diam_mm/2)² judges.
    # Falls back to the first row with px so a run whose baseline row was
    # rejected still gets a before→after pair, flagged as not-the-baseline.
    if rest_i is None:
        rest_i = first_px
        out['rest_is_baseline'] = False if rest_i is not None else None
    else:
        out['rest_is_baseline'] = True
    if rest_i is not None:
        out['rest_row'] = rest_i
        out['rest_px'] = _num(rows[rest_i].get('active_area_px'))
        out['rest_before'] = _num(rows[rest_i].get('active_area_mm2'))
        # THE OLD SCALE: the resting row's own, not a median. This run's
        # resting area is the quantity being corrected, so the factor
        # quoted has to be the one that moves IT.
        out['old_scale'] = implied_scale(rows[rest_i])
    if out['old_scale'] is None and scales:
        out['old_scale'] = float(np.median(scales))
    if out['old_scale'] and dmm > 0:
        out['old_diam_px'] = dmm / out['old_scale']
    rec_px = _num((recorded or {}).get('diam_px'))
    if rec_px and rec_px > 0:
        out['recorded_diam_px'] = rec_px
        if out['old_diam_px']:
            # A recorded anchor that does NOT match the scale the column was
            # actually derived at means the block and the data disagree —
            # either a hand-edited setup.txt or a save that never completed.
            # Reported, never silently preferred: the DATA wins, because the
            # data is what is being re-derived.
            out['anchor_matches_data'] = (
                abs(100.0 * (rec_px / out['old_diam_px'] - 1.0)) <= 0.05)
    if ns and out['old_scale']:
        out['mult'] = (ns / out['old_scale']) ** 2
        out['diam_mult'] = ns / out['old_scale']
    if ns and out['rest_px']:
        out['rest_after'] = out['rest_px'] * ns * ns
    nom = out['nominal_mm2']
    if nom:
        for src, dst in (('rest_before', 'rest_dev_before'),
                         ('rest_after', 'rest_dev_after')):
            if out[src] is not None:
                out[dst] = 100.0 * (out[src] - nom) / nom
    return out


def reanchor_anchor_fields(prev, plan):
    """The provenance fields that mark an anchor as written by a SCALE-ONLY
    re-anchor rather than by a review pass.

    Merged into the dict `save_scale_anchor` is given, so a re-anchored run
    keeps the FULL provenance of its new anchor (the verify mode's fit
    quality and who approved it, or a hand measurement's rounds and
    spread) and gains
    the record that no review happened and what the scale was before.

    `prev_diam_px` comes only from a recorded anchor block; `prev_implied_px`
    is the diameter the DATA itself implies. They are separate keys because
    they are separate claims, and on the eight runs with no anchor block only
    the second one exists — which is precisely the case where a later reader
    would otherwise have no way to know what scale was replaced."""
    out = {'reanchor': REANCHOR_SCALE_ONLY}
    p = prev or {}
    if _num(p.get('diam_px')):
        out['prev_diam_px'] = float(p['diam_px'])
    if p.get('method'):
        out['prev_method'] = p['method']
    if p.get('cal_mode'):
        # the NAME, never a letter — `prev` may be a block written before
        # 2026-08-06 late (live P3_2's is: `prev_cal_mode: C`, meaning
        # verify), and copying the letter forward would carry a value whose
        # meaning has since changed into a brand-new record
        out['prev_cal_mode'] = cal_mode_read(p['cal_mode'],
                                             default=p['cal_mode'])
    pl = plan or {}
    if pl.get('old_diam_px'):
        out['prev_implied_px'] = float(pl['old_diam_px'])
    if pl.get('n_derive') is not None:
        out['reanchor_rows'] = int(pl['n_derive'])
    if pl.get('n_blank'):
        out['reanchor_blanked'] = int(pl['n_blank'])
    return out


def reanchor_log_record(anchor, plan, when=None, frame=None):
    """A `calibration_log_line` record for a committed re-anchor.

    The calibration dialog already logs the round-set it produced (mode,
    σ, SE, verdict) the moment it completes, accepted or declined. This is
    the SECOND line: what was then done with that anchor to the run's
    existing column. Written through the same formatter so the whole
    history of a run's scale is one greppable file, and carrying the
    accepted anchor's own statistics so a reader does not have to pair the
    two lines up by timestamp to know what the scale came from."""
    import time                    # module-local, like save_scale_anchor
    a = anchor or {}
    p = plan or {}
    vfy = guard_is_vacuous(a)
    se_pct = _num(a.get('se_pct'))
    if vfy:
        # THE SAME record the verify mode's own round-set line is built
        # from, so the two lines describing one anchor use one vocabulary:
        # n=1, and every
        # precision field 'undefined' rather than 0.00% or a bare n=0.
        stats = verify_stats(a) or {'n': 1, 'values': [], 'mean': 0.0,
                                    'single_fit': True}
    else:
        rounds = list(a.get('rounds_px') or [])
        stats = {'n': int(a.get('n_rounds') or len(rounds) or 1),
                 # a pre-#215 two-click anchor carries no rounds; its own
                 # diameter is then the one value there is, which is true
                 # and greppable, where an empty list renders as 'diams=px'
                 'values': rounds or [float(_num(a.get('diam_px')) or 0.0)],
                 'mean': float(_num(a.get('diam_px')) or 0.0),
                 'spread_pct': _num(a.get('spread_pct')),
                 'sigma_pct': _num(a.get('sigma_pct')),
                 'se_pct': se_pct,
                 'area_se_pct': ((2.0 * se_pct) if se_pct is not None
                                 else None),
                 'single_fit': False}
    auto_px = _num(a.get('auto_diam_px'))
    rec = {'when': when or time.strftime('%Y-%m-%dT%H:%M:%S'),
           'mode': (cal_mode_read(a.get('cal_mode'))
                    or (CAL_MODE_VERIFY if vfy else '?')),
           'stats': stats, 'gate': CAL_SE_PCT,
           # se_ok is already three-valued and refuses n < 2 and a missing
           # SE, so an unjudgeable set renders 'UNJUDGEABLE' rather than
           # borrowing a pass it never earned
           'verdict': ('NOT-GATED' if vfy
                       else {True: 'PASS',
                             False: 'OVER-GATE'}.get(se_ok(stats))),
           'rot_deg': None, 'stroke': None,
           'auto_diam_px': auto_px,
           'auto_pct': (None if vfy or not auto_px else
                        100.0 * (stats['mean'] - auto_px) / auto_px),
           'outcome': 'reanchor-committed',
           'frame': frame or a.get('anchor_frame'),
           'reanchor': {'scope': a.get('reanchor') or REANCHOR_SCALE_ONLY,
                        'n_derive': p.get('n_derive'),
                        'n_blank': p.get('n_blank'),
                        'old_diam_px': p.get('old_diam_px'),
                        'prev_method': a.get('prev_method'),
                        'mult': p.get('mult'),
                        'rest_before': p.get('rest_before'),
                        'rest_after': p.get('rest_after'),
                        'rest_dev_after': p.get('rest_dev_after')}}
    if vfy:
        for k in ('fit_circ', 'fit_conf', 'fit_resid_px', 'fit_arc_cov',
                  'fit_n_edge'):
            rec[k] = a.get(k)
        rec['fit_resid_pct'] = fit_resid_pct(a)
    return rec


# ---------------------------------------------------------------------------
# the calibration log — every completed round-set, accepted or declined
#
# Why this file exists: the six circle-mode spreads that drove the whole
# two-point redesign (`#215` comment, 2026-08-06 — 1.94, 2.09, 1.62, 1.81,
# 1.44 % and one under 1 %) survive ONLY because they were read off the
# screen and typed into a chat. The run's setup.txt was never written,
# because every one of those calibrations was DECLINED at a gate, and
# setup.txt is written at Save. A declined round-set is exactly the
# measurement an A/B comparison needs — it is a fit the method produced —
# so it is written here the moment the round-set completes, whatever the
# operator then decides about it.
#
# Run data, not repo data: this lands in the run folder and .gitignore
# blocks it (repo convention — a PR adding a capture artifact extends
# .gitignore in the same PR).
# ---------------------------------------------------------------------------

CAL_LOG_NAME = 'scale_calibration_log.txt'
# The marker that says a log file's `mode=` column holds NAMES. Its absence
# in an existing file is what triggers the one-off migration note below —
# matched as a substring of the file, so it is written once and never again.
CAL_LOG_VOCAB_MARK = 'mode= holds a NAME'
CAL_LOG_HEADER = (
    '# SLDEA Edge Review scale calibrations, one line per completed '
    'round-set, accepted or declined.\n'
    '# ' + CAL_LOG_VOCAB_MARK + ': verify | circle | twopoint.\n'
    '#   verify   = the operator VERIFIED the automatic disc fit (no '
    'rounds: sigma/se/range are\n'
    '#              undefined, NOT zero -- what quantifies it is the fit '
    'resid/circ/conf/n_edge)\n'
    '#   circle   = the operator fitted a circle to the boundary, n rounds\n'
    '#   twopoint = the operator clicked two opposite edge points, n '
    'rounds, view rotated per round\n'
    '# LEGACY: a line whose mode= is a bare LETTER predates 2026-08-06 late '
    'and uses the\n'
    '#   PRE-SWAP letters -- A = circle, B = twopoint, C = verify. Read '
    'them through\n'
    '#   sldea_edge.cal_mode_read; do NOT read them against the dialog\'s '
    'current A/B/C labels.\n'
    '# sigma = per-fit precision (range/d2(n)); se = sigma/sqrt(n) on the '
    'mean; area_se = 2*se.\n'
    '# Compare methods on SIGMA -- it is the only figure that survives a '
    'different n.\n')
# Written ONCE into a log file that predates the letters->names change, so
# the file says where its own vocabulary changes instead of leaving a reader
# to notice that `mode=C` and `mode=verify` are both in it.
CAL_LOG_MIGRATION_NOTE = (
    '# --- 2026-08-06: ' + CAL_LOG_VOCAB_MARK + ' from here down: '
    'verify | circle | twopoint.\n'
    '#     Lines ABOVE use the PRE-SWAP LETTERS: A = circle, B = twopoint, '
    'C = verify.\n'
    '#     The dialog\'s labels were renumbered (A = verify, B = circle, '
    'C = twopoint), so a\n'
    '#     letter in this file must NOT be read against them. '
    'sldea_edge.cal_mode_read is the rule.\n')


def _fmt_list(vals, fmt='{:.2f}'):
    return ','.join(fmt.format(float(v)) for v in (vals or []))


def calibration_log_line(rec):
    """ONE line recording a completed round-set — the same line the dialog
    prints to stdout and appends to the run's log, so a bench operator can
    report an A/B result by copying either one.

    **`mode=` records a NAME** — `verify` / `circle` / `twopoint` — not a
    letter (2026-08-06 late). The letters were renumbered once at the
    operator's request and would have reinterpreted every `mode=` line
    already on disk; a name cannot be renumbered. FIELD ORDER IS UNCHANGED,
    so an existing splitter still finds `mode=` in the same column; only
    that field's value space moved. Lines written before the change hold a
    PRE-SWAP letter and are read through `cal_mode_read` (A = circle,
    B = twopoint, C = verify), which `append_calibration_log` also records
    as a note in any log file it has to migrate.

    Field order is the reporting order: mode and n first (what was done),
    then **sigma** (the method's per-fit precision — the only figure
    comparable between two modes at different round counts, which is why
    it leads), then the mean SE the gate judges, then the raw material.
    Deliberately one flat line of key=value: greppable out of a terminal
    scrollback with no tooling, and stable enough to parse later.

    THE `verify` MODE keeps every field, and writes the precision group as
    **`undefined`** rather than `0.00%` (2026-08-06 evening). A verify
    round-set is one automatic fit approved by a human: there is no range,
    no σ and no mean SE, and `0.00%` in those columns would read as perfect
    precision to anyone grepping this file — the opposite of the truth.
    'undefined' is used rather than the measuring modes' 'unconvertible'
    because the
    two gaps are different: 'unconvertible' means a number exists but the d₂
    table cannot convert it, while 'undefined' means the quantity does not
    exist for a sample of one. The measuring modes' lines are unchanged apart
    from the `mode=` value itself, which is now a name (see above), and are
    otherwise byte-identical to
    before.

    Pure — takes a dict, returns a str, touches no disk."""
    st = rec.get('stats') or {}
    n = st.get('n') or rec.get('n') or 0
    # normalised, so a caller (or a re-read record) holding a legacy letter
    # is still recognised as the method it was, and still WRITES the name
    mode = cal_mode_read(rec.get('mode'), default=rec.get('mode'))
    verify = (mode == CAL_MODE_VERIFY) or st.get('single_fit')
    absent = 'undefined' if verify else 'unconvertible'

    def pct(key):
        v = st.get(key)
        return f"{v:.2f}%" if v is not None else absent

    verdict = rec.get('verdict')
    bits = [
        'SLDEA-CAL',
        str(rec.get('when') or ''),
        f"mode={mode or '?'}",
        f"n={n}",
        f"sigma={pct('sigma_pct')}",
        f"se={pct('se_pct')}",
        f"area_se={pct('area_se_pct')}",
        f"gate={float(rec.get('gate', CAL_SE_PCT)):.2f}%",
        f"verdict={verdict or 'UNJUDGEABLE'}",
        f"range={pct('spread_pct')}",
        f"mean={st.get('mean', 0.0):.2f}px",
        f"diams={_fmt_list(st.get('values'))}px",
    ]
    # twopoint only; '-' rather than an empty field so the line's shape is
    # the same for both modes and a column-splitter cannot slip
    bits.append('rot=' + (_fmt_list(rec.get('rot_deg'), '{:.1f}')
                          or '-') + 'deg')
    bits.append(f"stroke={rec.get('stroke') or '-'}")
    auto = rec.get('auto_diam_px')
    if auto:
        pctd = rec.get('auto_pct')
        # In the verify mode the anchor IS the automatic fit, so a
        # deviation column would print +0.00% by construction — an
        # identity dressed as agreement. Say what it is instead.
        bits.append(f"auto={float(auto):.1f}px"
                    + ('(IS-the-anchor)' if verify else
                       f"({pctd:+.2f}%)" if pctd is not None else ''))
    else:
        bits.append('auto=none')
    # the verify mode's real quality figures, in place of a precision it
    # does not have. Omitted entirely for A/B so their lines do not change.
    if verify:
        for key, label, fmt in (('fit_circ', 'circ', '{:.3f}'),
                                ('fit_conf', 'conf', '{:.3f}'),
                                ('fit_resid_px', 'resid', '{:.1f}px'),
                                ('fit_arc_cov', 'arc', '{:.2f}'),
                                ('fit_n_edge', 'n_edge', '{:.0f}')):
            v = rec.get(key)
            if v is not None:
                bits.append(f"{label}=" + fmt.format(float(v)))
        rp = rec.get('fit_resid_pct')
        if rp is not None:
            bits.append(f"resid_pct={float(rp):.2f}%")
    # A COMMITTED RE-ANCHOR (`#215`, 2026-08-06): a scale-only correction of
    # a run that was NOT re-reviewed. Emitted only when the key is present,
    # so every round-set line of every mode keeps its field order and
    # spelling apart from the mode= value itself —
    # those formats are pinned by exact-string tests.
    rn = rec.get('reanchor')
    if rn:
        bits.append('reanchor=' + str(rn.get('scope')
                                      or REANCHOR_SCALE_ONLY))
        bits.append(f"rows={int(rn.get('n_derive') or 0)}")
        bits.append(f"blanked={int(rn.get('n_blank') or 0)}")
        # 'unknown' rather than a 0 or an omission: the previous scale is
        # the whole basis of the multiplier, and a reader must be able to
        # see that it could not be recovered
        prev_px = rn.get('old_diam_px')
        bits.append('prev=' + (f"{float(prev_px):.2f}px" if prev_px
                               else 'unknown'))
        bits.append(f"prev_method={rn.get('prev_method') or 'none-on-record'}")
        mult = rn.get('mult')
        bits.append('area_mult=' + (f"x{float(mult):.6f}" if mult
                                    else 'unknown'))
        rb, ra = rn.get('rest_before'), rn.get('rest_after')
        if rb is not None and ra is not None:
            bits.append(f"resting={float(rb):.3f}->{float(ra):.3f}mm2")
        dv = rn.get('rest_dev_after')
        if dv is not None:
            bits.append(f"resting_dev={float(dv):+.2f}%")
    bits.append(f"outcome={rec.get('outcome', '?')}")
    if rec.get('frame'):
        bits.append(f"frame={rec['frame']}")
    line = ' '.join(b for b in bits if b)
    # the block grammar of setup.txt does not apply here, but a stray
    # newline would still split one record into two
    return line.replace('\r', ' ').replace('\n', ' ')


def append_calibration_log(rundir, rec):
    """Append one round-set to <rundir>/scale_calibration_log.txt.

    -> (path, line) on success, (None, line) when the write fails. The
    line comes back either way BECAUSE it can fail: the caller prints the
    same line to stdout, so a read-only or full disk costs the file and
    not the measurement (the 2026-08-04 disk-full incident is why that is
    not hypothetical). Never raises — losing a calibration to a logging
    error would be worse than the gap this file exists to close."""
    line = calibration_log_line(rec)
    if not rundir:
        return None, line
    path = os.path.join(rundir, CAL_LOG_NAME)
    try:
        new = not os.path.exists(path)
        # A log written before the letters->names change (live P3_2 holds
        # eight `mode=C` lines) gets ONE note saying where its vocabulary
        # changes. Read rather than assumed, because appending the note
        # twice would be its own small lie about when the change happened;
        # the file is a handful of lines, so the read is free.
        migrate = False
        if not new:
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    migrate = CAL_LOG_VOCAB_MARK not in f.read()
            except OSError:
                migrate = False      # unreadable: still append the record
        with open(path, 'a', encoding='utf-8') as f:
            if new:
                f.write(CAL_LOG_HEADER)
            elif migrate:
                f.write(CAL_LOG_MIGRATION_NOTE)
            f.write(line + '\n')
        return path, line
    except OSError as e:
        print(f"calibrate: could not append to {path}: {e}")
        return None, line


# ---------------------------------------------------------------------------
# run loading
# ---------------------------------------------------------------------------

_RUN_CSV = re.compile(r'^data(\d*)\.csv$', re.IGNORECASE)


def run_csv(rundir):
    """Path to this run's data CSV, or None when the directory is not a run.

    Canonically data.csv. The bench also keeps renamed copies -- data1.csv,
    data2.csv -- because Excel refuses to open two workbooks with the same
    filename, and a renamed run is still a run. Every reader resolves the
    name through here, so one rename cannot make a folder invisible to the
    tuner, Edge Review and the diagnostic all at once (bench 2026-07-28).

    An exact data.csv always wins; otherwise the lowest number, so the
    choice is stable rather than depending on directory order."""
    try:
        names = [n for n in os.listdir(rundir) if _RUN_CSV.match(n)]
    except OSError:
        return None
    if not names:
        return None
    names.sort(key=lambda n: (n.lower() != 'data.csv',
                              int(_RUN_CSV.match(n).group(1) or 0), n))
    return os.path.join(rundir, names[0])


# Bench shortcuts (2026-07-28): the three P3 runs live at a fixed spot and
# get reopened constantly, so `1`, `2`, `3` stand in for them anywhere a run
# directory is accepted -- the tuner, the diagnostic and the Windows
# launcher. Entries whose path does not exist are skipped, so this is inert
# on the bench PC, on CI and on anyone else's machine. The runs are synced
# to more than one machine under different user profiles, so the first
# root that exists wins.
_ONEDRIVE_ROOTS = (
    r'C:\Users\anato\OneDrive - University of Connecticut'
    r'\Recordings\SLDEA_data',                       # bench PC
    r'C:\Users\Anatol Gogoj\OneDrive - University of Connecticut'
    r'\Recordings\SLDEA_data',                       # analysis PC
)
_ONEDRIVE = next((r for r in _ONEDRIVE_ROOTS if os.path.isdir(r)),
                 _ONEDRIVE_ROOTS[0])
BENCH_RUNS = {
    '1': _ONEDRIVE + r'\P3_1_2.5mL_20260728',
    '2': _ONEDRIVE + r'\P3_2_2.5mL_20260728',
    '3': _ONEDRIVE + r'\P3_3_2.5mL_20260728',
}


def _run_dirs(root):
    """Run directories DIRECTLY under `root`, sorted by name ([] on error).

    A run is ANY sub-directory holding a run CSV -- not only SLDEA_*. Bench
    folders are named things like P3_1_2.5mL_20260728, and requiring the
    prefix made them invisible to the Tune buttons while Edge Review opened
    them happily (bench 2026-07-28)."""
    try:
        subs = [os.path.join(root, n) for n in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, n))]
    except OSError:
        return []
    return [s for s in subs if run_csv(s)]


def runs_parent(root):
    """The directory to LOOK IN for runs when a caller says `root`.

    THE DESCENT RULE, stated once here because every SLDEA front end has
    to answer it the same way:

      1. a folder that IS a run wins -- settled before this by the
         caller (resolve_run's run_csv test, the pickers' own);
      2. `root` itself whenever runs sit directly in it;
      3. otherwise EXACTLY ONE level down, into the child directory
         holding the most runs (ties go to the first by name);
      4. otherwise `root` back -- never None, because the caller's own
         path is what an error message has to name.

    Never deeper than one level, so this can never turn into a walk of a
    share, and only when the level above holds nothing. It picks where to
    LOOK; it never picks the run -- that stays with newest_run and the
    pickers.

    Why it exists: a campaign upload arrives as a WRAPPER folder -- the
    2026-08-04 batch is 13 runs inside 'Upload 20260804\\SLDEA_data (1)' --
    and SCPI_SLDEA_DIR points at the wrapper, where a direct listing
    correctly finds nothing. That is what made the Windows tuner
    launcher's --resolve step exit 2, "no run found", on a machine holding
    13 of them (`#261`), and the no-argument tuner say the same (`#197`).

    Lifted from sldea_tuner, where `#197` first solved it for the run
    picker alone; newest_run and resolve_run now go through it, so the
    tuner CLI and its launcher, the app's Tune button, Edge Review, the
    plot window and the diagnostic share one answer."""
    if _run_dirs(root):
        return root
    try:
        subs = [os.path.join(root, n) for n in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, n))]
    except OSError:
        return root
    best, best_n = None, 0
    for s in subs:
        found = len(_run_dirs(s))
        if found > best_n:
            best, best_n = s, found
    return best or root


def newest_run(root):
    """Newest run directory under `root`, or None.

    `root` is read through runs_parent, so a campaign wrapper whose runs
    are nested one level down answers with the newest nested run instead
    of None (`#261`). `root` itself is never a candidate however it is
    spelled -- only sub-directories are -- which is why resolve_run tests
    run_csv(path) first and this is the fallback."""
    subs = _run_dirs(runs_parent(root))
    return max(subs, key=os.path.getmtime) if subs else None


def resolve_run(path):
    """The run to work on -> path, or None.

    Accepts a bench shortcut ('1'), a run directory, a parent full of runs
    (giving the newest), or a campaign wrapper one level above such a
    parent (`#261`, see runs_parent). One resolver for the CLI, the app's
    Tune buttons and the Windows launcher, so the three cannot disagree
    about what a given argument means.

    A folder that IS a run always wins: run_csv is tested before any
    descent, so a run holding a nested copy of itself still resolves to
    the run that was named."""
    if not path:
        return None
    shortcut = BENCH_RUNS.get(str(path).strip())
    if shortcut and run_csv(shortcut):
        return shortcut
    if run_csv(path):
        return path
    return newest_run(path)


def load_run(rundir):
    """-> {'rows': [...], 'columns': [...], 'csv_path', 'frames_dir'}.
    Rows are the data CSV's rows in order (all columns kept as strings)."""
    csv_path = run_csv(rundir)
    if csv_path is None:
        raise FileNotFoundError(
            f"no run CSV in {rundir} (expected data.csv, or data1.csv / "
            f"data2.csv / ... if the file was renamed)")
    # utf-8-sig + replace: the bench writes UTF-8, and the two readers of
    # one file must decode it identically — sldea_plot already did; the
    # locale-codec default here mojibaked the notes on the Windows
    # analysis PC (audit 2026-08-05)
    with open(csv_path, newline='', encoding='utf-8-sig',
              errors='replace') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        columns = list(reader.fieldnames or [])
    return {'rows': rows, 'columns': columns, 'csv_path': csv_path,
            'frames_dir': os.path.join(rundir, 'frames')}


def frame_path(run, row):
    name = (row.get('frame_file') or '').strip()
    return os.path.join(run['frames_dir'], name) if name else None


def load_gray(path):
    """Frame as float32 grayscale (None if unreadable)."""
    import cv2
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    return None if img is None else img.astype(np.float32)


# ---------------------------------------------------------------------------
# texture: wrinkle energy and the electrode-strip footprint
# ---------------------------------------------------------------------------

def texture_energy(gray, win=15):
    """Dense local wrinkle energy: |Laplacian| of the sigma-2.5 denoised
    frame, box-averaged over `win` px. Same denoise-then-texture recipe as
    _wrinkle_ratio (raw Laplacian is all sensor grain; the multi-pixel
    wrinkle ridges survive the blur) but per-pixel, so the map can be
    SEGMENTED rather than only score a region something else found."""
    import cv2
    blurred = cv2.GaussianBlur(np.asarray(gray, np.float32), (0, 0), 2.5)
    energy = np.abs(cv2.Laplacian(blurred, cv2.CV_32F, ksize=3))
    return cv2.boxFilter(energy, -1, (win, win))


def _fingerprint(gray):
    """Cheap content key for the per-baseline caches. id() is unsafe --
    every load_gray returns a fresh array for the same file."""
    import hashlib
    a = np.ascontiguousarray(np.asarray(gray, np.float32)[::16, ::16])
    return (gray.shape, hashlib.blake2b(a.tobytes(), digest_size=8)
            .hexdigest())


def _memo(cache, key, fn):
    """4-entry memo: one baseline per run, a couple of runs open at once."""
    if key in cache:
        return cache[key]
    val = fn()
    if len(cache) >= 4:
        cache.pop(next(iter(cache)))
    cache[key] = val
    return val


_TEXE_CACHE = {}


def _texture_energy_cached(base_gray):
    return _memo(_TEXE_CACHE, _fingerprint(base_gray),
                 lambda: texture_energy(base_gray))


# Below this local-|Laplacian| energy nothing counts as foil: on a scene
# with no textured object at all (flat synthetics, a rig with no strips in
# frame) the percentile of a near-zero map would select noise as 'foil'.
FOIL_TEX_FLOOR = 2.0

_FOIL_CACHE = {}


def foil_mask(base_gray, pct=92.0):
    """Full-resolution bool mask of the crinkled electrode strips in the
    BASELINE, from texture rather than brightness.

    Brightness cannot define the foil: on the P3 baselines the median foil
    pixel is 173 (p10 144) against paper at 176-190, so the electrode_lum
    >= 220 cut covers only 34-42% of the strips -- the specular streaks --
    and leaves the dark creases, exactly the pixels that swing when the
    foil shifts, exposed to the differencing (bench 2026-07-28). Texture
    separates cleanly: the crinkled strips are high local-Laplacian energy
    while both the paper and the resting disc are smooth. Coverage on the
    three P3 baselines: 12.7-13.3% of the frame, hugging the strips --
    verified by eye on the overlays, which is the only reason to trust it.

    Kernel sizes are tuned on the 1920x1080 bench frames; pass the
    FULL-resolution baseline and downscale the mask, not the input."""
    import cv2
    if base_gray is None:
        return None

    def build():
        tex = texture_energy(base_gray)
        thr = max(float(np.percentile(tex, pct)), FOIL_TEX_FLOOR)
        m = (tex >= thr).astype(np.uint8)
        out = np.zeros(base_gray.shape, bool)
        if not m.any():
            return out
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((41, 41), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
        n, lab, stats, _c = cv2.connectedComponentsWithStats(m)
        # Thickness gate: crinkled foil is a FILLED region of energy (the
        # P3 strips run 100+ px of inscribed depth) while a mere step edge
        # -- a sharp disc rim, a rig line -- is a band no thicker than the
        # texture window that close-41 fattened. Without this, a
        # hard-edged disc's own rim classifies as foil and the radial
        # tracer can never reach it.
        dist = cv2.distanceTransform((m > 0).astype(np.uint8),
                                     cv2.DIST_L2, 5)
        keep = np.zeros_like(m)
        for i in range(1, n):
            if (stats[i, cv2.CC_STAT_AREA] >= 0.005 * m.size
                    and float(dist[lab == i].max()) >= 30.0):
                keep[lab == i] = 1
        if keep.any():
            out = cv2.dilate(keep,
                             np.ones((15, 15), np.uint8)).astype(bool)
        return out

    return _memo(_FOIL_CACHE, (_fingerprint(base_gray), pct), build)


def _foil_small(base_full, size_wh):
    """The foil mask at the detector's working size (w, h)."""
    import cv2
    fo = foil_mask(base_full)
    if fo is None or not fo.any():
        return np.zeros((size_wh[1], size_wh[0]), bool)
    if fo.shape == (size_wh[1], size_wh[0]):
        return fo
    return cv2.resize(fo.astype(np.uint8), size_wh,
                      interpolation=cv2.INTER_NEAREST).astype(bool)


# ---------------------------------------------------------------------------
# candidate detection
# ---------------------------------------------------------------------------

def photometric_fit(base, img, roi, mask=None):
    """Gain and offset taking the baseline onto the frame: img ~ a*base + b.

    Fitted on MATCHED QUANTILES, not pixels, so a genuinely changed
    minority of the frame moves only the extreme quantiles and cannot drag
    the correction meant to remove everything else.

    Why affine rather than the scalar border-band ratio: bench runs P3_*
    (2026-07-28) sat at gain 0.72-0.82 with a +8..+41 offset against their
    own baseline, and a single ratio cannot express that. Two snapshots at
    one voltage in those runs agree to ~0.5 sigma, so the camera is steady
    -- the mismatch is between the BASELINE and the rest of the run, and it
    put a 26-gray-level pedestal under every threshold.

    Saturated highlights bias the slope down (clipped pixels cannot move),
    so a fitted gain is a lower bound when the electrodes blow out. And
    the trimming assumes the changed region is a MINORITY of the ROI: if
    more than about a third of it moves, the trim can keep the changed
    population instead. sldea_diag reports the residual, which is where
    that would show up.

    `mask` (bool array, same shape as the images): fit only on those
    pixels -- the paper background. Confirmed on the bench (2026-07-28,
    Q1): with the device inside its own fit region, run 3's gain dived
    0.77 -> 0.55 at 5.25 kV and recovered to a flat 0.78..0.85 once the
    fit was restricted to paper -- a changed region bigger than the trim
    can absorb is exactly the failure the paragraph above warns about.
    Falls back to the plain ROI when the mask keeps too few pixels for
    the quantiles to mean anything."""
    qs = np.linspace(2, 98, 49)
    if mask is not None and int(mask.sum()) >= 2000:
        xb = np.percentile(base[mask], qs)
        xi = np.percentile(img[mask], qs)
    else:
        xb = np.percentile(base[roi], qs)
        xi = np.percentile(img[roi], qs)
    keep = np.ones(xb.shape, bool)
    a, b = 1.0, 0.0
    # Trimmed refit: the changed region still shifts the quantiles it
    # occupies, and on a scene with wide dynamic range a small slope error
    # leaves a large residual at the bright end. Drop the worst-fitting
    # third and refit, so the correction is defined by the part of the
    # picture that did NOT change.
    for _pass in range(3):
        design = np.vstack([xb[keep], np.ones(int(keep.sum()))]).T
        a, b = np.linalg.lstsq(design, xi[keep], rcond=None)[0]
        resid = np.abs((a * xb + b) - xi)
        cut = np.quantile(resid, 0.67)
        nxt = resid <= max(cut, 1e-6)
        if nxt.sum() < 8 or (nxt == keep).all():
            break
        keep = nxt
    return float(a), float(b)


def _region_candidate(mask, method, offset=(0, 0)):
    """Changed-region outline for one threshold tier -> candidate dict.

    The DEA's activated area often reads as a PATCHY texture change
    (wrinkling/buckling under field), so nearby changed patches are MERGED
    with a wide morphological close and the outline is the detailed contour
    of the merged region -- finer than the earlier convex hull (which drew
    coarse straight spans and would stretch to any stray speck, e.g. an
    electrode glint) and still oblong-friendly since there is no shape prior.

    'solidity' here = FILL: changed px inside the outline / outline area.
    A real activation fills a decent fraction of its outline; scattered
    noise spans a large merged region with near-zero fill."""
    import cv2
    kernel3 = np.ones((3, 3), np.uint8)
    kernel7 = np.ones((7, 7), np.uint8)
    changed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, kernel3)
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, kernel7)
    # merge patches within ~2 kernel-widths; keeps distant regions separate
    merge_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    merged = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, merge_k)
    cnts, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    area = float(cv2.contourArea(c))
    if area < 80:
        return None
    inside = np.zeros_like(changed)
    cv2.drawContours(inside, [c], -1, 1, -1)
    denom = float((inside > 0).sum()) or 1.0
    fill = min(1.0, float(((changed > 0) & (inside > 0)).sum()) / denom)
    per = float(cv2.arcLength(c, True)) or 1.0
    circ = min(1.0, 4.0 * np.pi * area / (per * per))
    mom = cv2.moments(c)
    cx = mom['m10'] / mom['m00'] if mom['m00'] else 0.0
    cy = mom['m01'] / mom['m00'] if mom['m00'] else 0.0
    pts = c.reshape(-1, 2) + np.asarray(offset)
    return {'method': method, 'area_px': area, 'circ': circ,
            'solidity': fill,
            'cx': float(cx + offset[0]), 'cy': float(cy + offset[1]),
            'diam_px': 2.0 * np.sqrt(area / np.pi), 'contour': pts}


# Detection runs on a downscaled copy: full-res HoughCircles on a 1080p
# frame takes ~minutes (accumulator blow-up on noise) while the disc needs
# nowhere near that resolution. Results are rescaled to full-res px.
DETECT_MAX_W = 640


def _wrinkle_ratio(base_full, img_full, contour):
    """Wrinkle index for one outline, at FULL resolution.

    Denoise-then-texture: Gaussian blur (sigma 2.5) kills the sensor grain,
    then mean |Laplacian| inside the outline, frame vs the same region of the
    baseline. Wrinkling/buckling under field is the activated DEA state --
    the lab defines the wrinkled region AS the active area. The pre-blur is
    essential on the bench camera: raw Laplacian is dominated by pixel noise
    (which the baseline has everywhere) while the wrinkle ridges are
    multi-pixel and survive the blur (bench 2026-07-23: raw full-res ratio
    read ~0.9 on obviously wrinkled frames; blurred reads cleanly > 1).
    Cropped to the outline's bounding box, so it stays cheap."""
    import cv2
    if base_full is None:
        return 1.0
    pts = np.asarray(contour, np.int32)
    h, w = img_full.shape
    x0 = max(int(pts[:, 0].min()) - 8, 0)
    x1 = min(int(pts[:, 0].max()) + 8, w)
    y0 = max(int(pts[:, 1].min()) - 8, 0)
    y1 = min(int(pts[:, 1].max()) + 8, h)
    if x1 - x0 < 16 or y1 - y0 < 16:
        return 1.0
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.drawContours(mask, [pts - np.array([x0, y0])], -1, 255, -1)
    if not (mask > 0).any():
        return 1.0
    bi = cv2.GaussianBlur(img_full[y0:y1, x0:x1], (0, 0), 2.5)
    bb = cv2.GaussianBlur(base_full[y0:y1, x0:x1], (0, 0), 2.5)
    e_img = float(np.abs(cv2.Laplacian(bi, cv2.CV_32F,
                                       ksize=3))[mask > 0].mean())
    e_base = float(np.abs(cv2.Laplacian(bb, cv2.CV_32F,
                                        ksize=3))[mask > 0].mean())
    return round(min(e_img / max(e_base, 1e-3), 9.99), 2)


def wrinkle_index(base_gray, img_gray, contour, settings):
    """Wrinkle index for an arbitrary outline, measured EXACTLY as
    candidates() measures it: on the photometrically normalized frame
    (prepared_diff maps the frame into the baseline's space first).

    The one entry point for callers outside the detector — the GUI's
    hand-trace path used the raw frame, and |Laplacian| is linear in
    gain, so traced rows carried the correct index times the run's
    photometric gain: 20-30% understated on the P3 campaign (gain
    0.72-0.83), the other direction on DOT_P3_1 — and the saved
    wrinkle_idx column silently mixed two normalizations (audit
    2026-08-05; P3_5's traced wash-out rows lost their wrinkle-mode
    onset annotation to it)."""
    if base_gray is None or img_gray is None:
        return None
    prep = prepared_diff(base_gray, img_gray, settings)
    return _wrinkle_ratio(prep['base_full'], prep['img_full'],
                          np.asarray(contour, np.int32))


def _paper_mask(base_full, small_shape, settings):
    """Detector-scale mask of the PAPER background -- the ROI minus the
    foil strips minus the resting disc -- for the photometric fit under
    norm_bg 2.

    The fit's trimming assumes the changed region is a minority of its fit
    region; the disc is not always one, and the foil is the dominant
    variance either way. Measured on the bench (2026-07-28, Q1): run 3's
    ~5 kV gain excursion was an artifact of the device sitting inside its
    own fit region and vanished under this restriction, while run 2's
    survived it -- two causes the full-ROI fit could not separate.

    None when there is nothing to exclude (no foil, no disc found): the
    caller then fits on the plain ROI exactly as before, so scenes
    without strips reprocess identically."""
    import cv2
    h, w = small_shape
    fo = _foil_small(base_full, (w, h))
    ref = baseline_disc(base_full, settings)
    if not fo.any() and ref is None:
        return None
    rf = min(1.0, max(0.2, float(settings.get('roi_frac', 0.85))))
    bx = int(w * (1 - rf) / 2)
    by = int(h * (1 - rf) / 2)
    mask = np.zeros(small_shape, bool)
    mask[by:h - by or None, bx:w - bx or None] = True
    mask &= ~fo
    if ref is not None:
        f = w / float(base_full.shape[1])
        cx, cy = ref['cx'] * f, ref['cy'] * f
        r = 0.5 * ref['diam_px'] * f * 1.12     # margin for the soft edge
        yy, xx = np.ogrid[0:h, 0:w]
        mask &= ((xx - cx) ** 2 + (yy - cy) ** 2) > r * r
    return mask


def prepared_diff(base_gray, img_gray, settings):
    """The difference map candidates() actually thresholds.

    -> {sub, x0, y0, f, img_full, base_full}: the ROI-cropped,
    normalized, electrode-masked, blurred difference at the
    detector's own scale, plus what is needed to map results back to
    full resolution.

    Extracted so the diagnostic can report the no-change gate and the
    Otsu threshold on the SAME map the detector cuts. Reporting them
    on a raw difference the detector never sees was misleading once
    normalization started doing real work (bench 2026-07-28).
    """
    import cv2
    img_full, base_full = img_gray, base_gray   # full-res kept for wrinkle
    h0, w0 = img_gray.shape
    f = 1.0
    if w0 > DETECT_MAX_W:
        f = DETECT_MAX_W / float(w0)
        size = (DETECT_MAX_W, max(1, int(round(h0 * f))))
        img_gray = cv2.resize(img_gray, size, interpolation=cv2.INTER_AREA)
        if base_gray is not None:
            base_gray = cv2.resize(base_gray, size,
                                   interpolation=cv2.INTER_AREA)
    # Photometric normalization: the DFK's internal auto-gain (no UVC off
    # switch) drifts global brightness a few %, which would light up the
    # whole diff as fake change. Scale the frame so the BORDER band (outside
    # the ROI -- static membrane/holder) matches the baseline's, before
    # differencing; the wrinkle ratio uses the same scale.
    h, w = img_gray.shape
    rf = min(1.0, max(0.2, float(settings.get('roi_frac', 0.85))))
    bx = int(w * (1 - rf) / 2)
    by = int(h * (1 - rf) / 2)
    # The border-band widths gate only the LEGACY scalar mode: its band
    # is empty at roi_frac 1.0. The affine fit works on ROI quantiles and
    # needs no band — gating it on bx/by silently switched ALL
    # photometric correction off at the tuner slider's own maximum, and
    # the residual pedestal auto-accepted a 5.3x-too-large outline
    # (audit 2026-08-05).
    mode = int(settings.get('norm_bg', 2) or 0)
    if base_gray is not None and mode and (mode >= 2 or (bx and by)):
        if mode >= 2:
            # Affine (gain+offset) fit on ROI quantiles, then map the frame
            # back into the baseline's photometric space. On the bench runs
            # this took the mean ROI difference from 26 gray levels to 1.6
            # -- below the sensor noise -- while leaving a residual that
            # still grows with voltage, so it removes the artifact and not
            # the device. Fitted on the PAPER background only (ROI minus
            # disc minus foil) when the scene has those, so the device
            # cannot drag its own correction (2026-07-28, Q1).
            roi = (slice(by, h - by), slice(bx, w - bx))
            paper = _paper_mask(base_full, img_gray.shape, settings)
            a, b = photometric_fit(base_gray, img_gray, roi, mask=paper)
            if a > 0.2 and (abs(a - 1.0) > 0.005 or abs(b) > 0.5):
                img_gray = np.clip((img_gray - b) / a, 0, 255)
                img_full = np.clip((img_full - b) / a, 0, 255)
        else:
            # legacy scalar: border-band median ratio (norm_bg: 1). Kept so
            # a run tuned under it reprocesses identically.
            band = np.ones_like(img_gray, bool)
            band[by:h - by, bx:w - bx] = False
            med_i = float(np.median(img_gray[band])) or 1.0
            med_b = float(np.median(base_gray[band]))
            scale = min(1.4, max(0.7, med_b / med_i))
            if abs(scale - 1.0) > 0.005:
                img_gray = np.clip(img_gray * scale, 0, 255)
                img_full = np.clip(img_full * scale, 0, 255)
    img_small = img_gray
    k = int(settings.get('blur_px', 5)) | 1
    diff = cv2.absdiff(img_gray, base_gray).astype(np.uint8) \
        if base_gray is not None else img_gray.astype(np.uint8)
    # Electrode suppression: the copper/foil strips read bright and their
    # glints SHIFT with voltage, lighting up the diff although the membrane
    # there is hidden anyway. Mask pixels that are bright in the BASELINE --
    # the electrodes are static objects, so the baseline knows where they
    # are. (Never mask on the current frame: the wrinkled activated region
    # saturates too, and masking it would erase the very signal we score --
    # bench 2026-07-23, 5.5 kV wrinkle index dropped 1.9 -> 1.2 that way.)
    # The brightness cut alone covered only 34-42% of the strips -- the
    # specular streaks -- while the dark creases, the pixels that actually
    # swing when the foil shifts, stayed in the diff and won 83-100% of
    # the detected area (bench 2026-07-28). The texture-derived footprint
    # covers the whole strip; the same knob turns both off.
    lum = float(settings.get('electrode_lum', 0) or 0)
    if lum > 0 and base_gray is not None:
        glint = (base_gray >= lum).astype(np.uint8)
        glint = cv2.dilate(glint, np.ones((7, 7), np.uint8))
        glint[_foil_small(base_full, (diff.shape[1], diff.shape[0]))] = 1
        diff[glint > 0] = 0
    diff = cv2.GaussianBlur(diff, (k, k), 0)
    # central ROI: the DEA sits mid-frame; electrode glare lives at the edges
    h, w = diff.shape
    rf = min(1.0, max(0.2, float(settings.get('roi_frac', 0.85))))
    x0 = int(w * (1 - rf) / 2)
    y0 = int(h * (1 - rf) / 2)
    sub = diff[y0:h - y0 or h, x0:w - x0 or w]
    return {'sub': sub, 'x0': x0, 'y0': y0, 'f': f,
            'img_full': img_full, 'base_full': base_full,
            'base_small': base_gray, 'img_small': img_small,
            'diff_small': diff}


def _ratio_small(prep, settings):
    """Texture-ratio map at the detector's scale, foil and glint
    neutralized. Energies are computed at FULL resolution (the wrinkle
    ridges do not survive downscale), the ratio is then resized once and
    shared by the texture candidate and the disc-boundary fit.

    Regularized: on smooth paper both energies are near zero and a bare
    quotient is noise amplified; +median(base energy) pins the quiet
    regions to ~1 while a real wrinkle still clears the threshold.
    Floored at 0.5 -- the energy scale of real sensor grain -- so a
    noise-free synthetic baseline cannot make faint edge tails read as
    arbitrarily large ratios."""
    import cv2
    base_full, img_full = prep['base_full'], prep['img_full']
    if base_full is None:
        return None
    eb = _texture_energy_cached(base_full)
    ei = texture_energy(img_full)
    c0 = max(float(np.median(eb)), 0.5)
    ratio = (ei + c0) / (eb + c0)
    small = prep['base_small']
    h, w = small.shape
    if ratio.shape != small.shape:
        ratio = cv2.resize(ratio, (w, h), interpolation=cv2.INTER_AREA)
    neutral = _foil_small(base_full, (w, h)).copy()
    lum = float(settings.get('electrode_lum', 0) or 0)
    if lum > 0:
        glint = cv2.dilate((small >= lum).astype(np.uint8),
                           np.ones((7, 7), np.uint8))
        neutral |= glint > 0
    ratio[neutral] = 1.0
    return ratio


def _fit_ellipse_robust(pts):
    """Trimmed ellipse fit -> ((cx, cy), (A, B), phi_deg, keep) or None.
    Residual is radial about the ellipse centre, so a bulge confined to a
    few sectors -- an electrode lead lighting up -- is trimmed away
    instead of dragging the boundary."""
    import cv2
    if len(pts) < 20:
        return None
    keep = np.ones(len(pts), bool)
    ell = None
    for _ in range(3):
        if keep.sum() < 20:
            return None
        ell = cv2.fitEllipse(pts[keep].astype(np.float32))
        (xc, yc), (A, B), phi = ell
        a, b = max(A, 1e-3) / 2.0, max(B, 1e-3) / 2.0
        th = np.arctan2(pts[:, 1] - yc, pts[:, 0] - xc) - np.radians(phi)
        r_ell = (a * b) / np.sqrt((b * np.cos(th)) ** 2
                                  + (a * np.sin(th)) ** 2)
        resid = np.abs(np.hypot(pts[:, 0] - xc, pts[:, 1] - yc) - r_ell)
        sig = max(float(np.std(resid[keep])), 1.0)
        nxt = resid <= 2.5 * sig
        if (nxt == keep).all():
            break
        keep = nxt
    return ell, keep


def _disc_fit_candidate(prep, settings, ref, assume_responding=False):
    """Boundary of the RESPONDING DISC, tracked from the known resting
    disc -- the active area as the lab records it (2026-07-28 decision:
    the full responding disc, wrinkled and non-wrinkled together, leads
    excluded).

    `assume_responding` (the resting-refit path, 2026-07-30): waive the
    two change-map "is the disc responding" gates -- the whole point of
    that path is a frame whose change map is BELOW the no-change gate
    while the audit has already measured the ink step off the claimed
    circle, so the change map is known-silent and the step is known-
    measurable. Every ink-profile quality gate (adaptive step cut, ray
    count, arc coverage, ellipse residual/shape/size) still applies.

    The blob channels answer 'which changed region is biggest'; this one
    answers 'where is the edge of the object we know is there'. Rays are
    cast from the resting-disc centre over a fused change map (sigma-
    scaled intensity diff OR texture ratio, so displacement and wrinkle
    both count); each ray takes the OUTERMOST sustained change edge, so
    interior wrinkle gaps do not matter. Rays through the strips are
    excluded by azimuth (the electrode leads feed the tape in those same
    sectors, and the lab says leads are not active area); a lead bulge
    that survives is trimmed by the robust ellipse fit. Area comes from
    the FITTED shape, not a pixel blob -- no merge-close area steps.

    spread_pct on this candidate is the fit's own 85% confidence
    interval on area (percent): the cross-tier area spread it replaces
    measured threshold sensitivity, and for a boundary fit the honest
    analogue is the measurement's own dispersion."""
    import cv2
    if ref is None:
        return None
    small, diff = prep['base_small'], prep['diff_small']
    base_full = prep['base_full']
    if small is None or base_full is None:
        return None
    h, w = small.shape
    f = w / float(base_full.shape[1])
    cx0, cy0 = ref['cx'] * f, ref['cy'] * f
    r0 = 0.5 * ref['diam_px'] * f
    if r0 < 12:
        return None
    ratio = _ratio_small(prep, settings)
    fo = _foil_small(base_full, (w, h))
    paper = _paper_mask(base_full, small.shape, settings)
    dref = diff[paper] if (paper is not None and paper.any()) else diff
    med_p = float(np.median(dref))
    sigma = max(1.4826 * float(np.median(np.abs(dref - med_p))), 0.5)
    thr = max(1.05, float(settings.get('wrinkle_ratio', 1.4)))
    # EXCESS change above the paper's own residual: at high kV the
    # photometric fit leaves a few gray levels across the whole scene
    # (the residual that grows with voltage), and a map normalized to raw
    # diff reads the paper itself as ~0.3 'change' -- the edge walk then
    # rides that plateau out to the vignetting and reports the disc at a
    # physically impossible 2x area. UNCLIPPED: the valley detector below
    # needs the dynamic range a saturating map destroys.
    dn = np.maximum((diff.astype(np.float32) - med_p) / (4.0 * sigma),
                    0.0)
    tn = np.maximum((ratio - 1.0) / (thr - 1.0), 0.0) \
        if ratio is not None else np.zeros_like(dn)
    change_raw = np.maximum(dn, tn)
    change = np.minimum(change_raw, 1.0)

    rs = np.arange(max(4.0, 0.45 * r0), 1.8 * r0, 1.0)
    nray = 360
    th = np.linspace(0, 2 * np.pi, nray, endpoint=False)
    xs = (cx0 + np.outer(np.cos(th), rs)).astype(np.float32)
    ys = (cy0 + np.outer(np.sin(th), rs)).astype(np.float32)
    prof = cv2.remap(change_raw, xs, ys, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    fop = cv2.remap(fo.astype(np.uint8), xs, ys, cv2.INTER_NEAREST,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=1)
    # strips (and the leads that feed them) block whole SECTORS, +-10 deg
    blocked = fop.any(axis=1)
    bi = np.where(blocked)[0]
    for k in bi:
        blocked[max(0, k - 10):k + 11] = True
        if k < 10:
            blocked[nray + k - 10:] = True
        if k > nray - 11:
            blocked[:k + 11 - nray] = True
    # is the disc responding at all? judged inside 0.85 r0
    yy, xx = np.ogrid[0:h, 0:w]
    inner = ((xx - cx0) ** 2 + (yy - cy0) ** 2 <= (0.85 * r0) ** 2) & ~fo
    if int(inner.sum()) < 100:
        return None
    # p75, not median: a partially responding disc (wrinkle patches over
    # part of the interior) still counts as responding
    i_lvl = float(np.percentile(change[inner], 75))
    if i_lvl < 0.2 and not assume_responding:
        return None                     # no response above the noise scale
    level = max(0.45 * i_lvl, 0.10)
    sm_prof = cv2.blur(prof, (1, 7))
    # The boundary feature is the INK EDGE of the electrode itself, on
    # the photometrically NORMALIZED current frame -- the same dark->
    # light step baseline_disc measures at 0 kV, tracked as it moves.
    # The change map cannot mark the boundary: the passive membrane ring
    # outside the electrode hoop-wrinkles as the disc expands (real
    # response, but not active area -- same ruling as the leads), and
    # the taut rim of the active disc barely changes, so change-based
    # edges land either on the ring's outer edge (1.6x area) or on the
    # rim's inner side (shrinking with kV). The ink moves with the
    # membrane and stays darker than everything around it; the change
    # map's job here is only to say the disc is RESPONDING at all.
    sm_img = cv2.GaussianBlur(cv2.medianBlur(
        np.clip(prep['img_small'], 0, 255).astype(np.uint8), 3),
        (0, 0), 2.0).astype(np.float32)
    iprof = cv2.remap(sm_img, xs, ys, cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_REPLICATE)
    istep = np.diff(iprof, axis=1)
    nr = len(rs)
    in_lo = int(np.searchsorted(rs, 0.55 * r0))
    in_hi = int(np.searchsorted(rs, 0.80 * r0))
    w_lo = int(np.searchsorted(rs, 0.80 * r0))
    w_hi = min(int(np.searchsorted(rs, 1.38 * r0)), nr - 6)
    if in_hi <= in_lo or w_hi <= w_lo + 4:
        return None
    # Two passes over the rays: measure every sustained step first, then
    # cut at a threshold derived from the scene's own ink contrast. A
    # fixed cut cannot serve both campaigns -- the P3 ink sits 10-25 gray
    # levels below paper with a faint top arc, while the 07-23 devices
    # step 40+ and their spurious lead/shadow edges alone reach 6-8. The
    # adaptive cut keeps a uniformly faint edge and rejects junk edges on
    # a high-contrast scene, in the same rule.
    raw = []
    for k in range(nray):
        if blocked[k]:
            continue
        if not assume_responding and \
                float(np.median(sm_prof[k, in_lo:in_hi])) < level:
            continue                    # this sector is not responding
        gi = w_lo + int(np.argmax(istep[k, w_lo:w_hi]))
        ins = iprof[k, max(gi - 20, 0):gi - 4]
        outs = iprof[k, gi + 5:gi + 21]
        if ins.size < 4 or outs.size < 4:
            continue
        stepc = float(np.median(outs)) - float(np.median(ins))
        if stepc <= 2.0:
            continue                    # no sustained dark->light edge
        raw.append((k, gi, stepc))
    if len(raw) < 8:
        return None
    cut = max(3.0, 0.35 * float(np.median([s for _k, _g, s in raw])))
    pts = []
    steps = []
    for k, gi, stepc in raw:
        if stepc < cut:
            continue
        # sub-pixel edge: parabolic refinement over the neighboring step
        # values (istep[gi] is the rise between rs[gi] and rs[gi]+1)
        s0 = float(istep[k, gi])
        sm1 = float(istep[k, gi - 1]) if gi > 0 else s0
        sp1 = float(istep[k, gi + 1]) if gi < istep.shape[1] - 1 else s0
        den = sm1 - 2.0 * s0 + sp1
        delta = 0.5 * (sm1 - sp1) / den if abs(den) > 1e-9 else 0.0
        delta = min(0.75, max(-0.75, delta))
        redge = rs[gi] + 0.5 + delta
        pts.append((cx0 + redge * np.cos(th[k]),
                    cy0 + redge * np.sin(th[k])))
        steps.append(stepc)
    pts = np.asarray(pts, np.float64)
    open_sectors = int((~blocked).sum())
    if len(pts) < 40 or open_sectors < 90:
        return None
    fit = _fit_ellipse_robust(pts)
    if fit is None:
        return None
    (exc, eyc), (A, B), phi = fit[0]
    keep = fit[1]
    pin = pts[keep]
    a, b = A / 2.0, B / 2.0
    r_eq = float(np.sqrt(a * b))
    # gates: the responding disc is the resting disc, slightly deformed.
    # The observed full-ramp expansion is ~1.25x AREA (1.12x radius); a
    # fit claiming more than 1.3x radius is tracking something else --
    # the halo, the vignetting, the annulus -- not the device.
    if not 0.9 <= r_eq / r0 <= 1.3:
        return None
    if np.hypot(exc - cx0, eyc - cy0) > 0.15 * r0:
        return None
    circ = min(a, b) / max(max(a, b), 1e-6)
    if circ < 0.55:
        return None
    thp = np.arctan2(pin[:, 1] - eyc, pin[:, 0] - exc) - np.radians(phi)
    r_ell = (a * b) / np.sqrt((b * np.cos(thp)) ** 2
                              + (a * np.sin(thp)) ** 2)
    resid = np.abs(np.hypot(pin[:, 0] - exc, pin[:, 1] - eyc) - r_ell)
    med_res = float(np.median(resid))
    if med_res > 0.06 * r_eq:
        return None
    cov = len(pin) / float(open_sectors)
    # 85% CI on area from the edge scatter: dA = perimeter * dr, and the
    # mean-radius error is sigma_r / sqrt(n)
    se_r = 1.4826 * med_res / np.sqrt(max(len(pin), 1))
    ci85 = 100.0 * 1.44 * 2.0 * se_r / r_eq
    # boundary strength: the ink edge's own sustained dark->light step
    # (10-25 gray levels on the P3 devices), on the kept rays
    steps = np.asarray(steps, np.float64)
    contrast = max(0.0, min(1.0, float(np.median(steps[keep])) / 12.0))
    ring_in = ((xx - exc) ** 2 + (yy - eyc) ** 2 <= (0.92 * r_eq) ** 2) \
        & ~fo
    fill = float((change[ring_in] >= level).mean()) if ring_in.any() else 0.0
    conf = min(0.99, 0.40 * min(1.0, cov / 0.75) + 0.30 * contrast
               + 0.30 * (1.0 - min(1.0, med_res / (0.06 * r_eq))))
    th2 = np.linspace(0, 2 * np.pi, 72, endpoint=False)
    ex = exc + a * np.cos(th2) * np.cos(np.radians(phi)) \
        - b * np.sin(th2) * np.sin(np.radians(phi))
    ey = eyc + a * np.cos(th2) * np.sin(np.radians(phi)) \
        + b * np.sin(th2) * np.cos(np.radians(phi))
    return {'method': 'disc-fit', 'area_px': float(np.pi * a * b),
            'diam_px': float(2 * r_eq), 'cx': float(exc), 'cy': float(eyc),
            'circ': round(float(circ), 3), 'solidity': round(fill, 3),
            'contour': np.stack([ex, ey], axis=1),
            'contrast': round(contrast, 3), 'conf_own': round(conf, 3),
            'ci85_pct': round(float(ci85), 2), 'n_edge': int(len(pin)),
            'arc_cov': round(cov, 2)}


def _resting_candidate(prep, settings, ref):
    """The no-change frame, stated instead of blanked: with the resting
    disc known and the frame showing no detectable change against the
    baseline, the honest measurement is 'the active area equals the
    resting area' -- not an empty row. Confidence grows with the margin
    below the gate (a frame at half the gate is more certainly unchanged
    than one brushing it). Low-kV frames then auto-accept with a real
    area instead of queueing for review over nothing."""
    if ref is None:
        return None
    p99 = float(np.percentile(prep['sub'], 99))
    md = float(settings.get('min_diff', 10)) or 1.0
    margin = max(0.0, min(1.0, 1.0 - p99 / md))
    c = dict(ref)
    c['method'] = 'resting'
    c['conf_own'] = round(min(0.95, 0.70 + 0.25 * margin), 3)
    c['ci85_pct'] = 0.0
    c['contrast'] = 0.0        # its contour is FULL-res already: keep it
    c['fullres'] = True        # out of the det-scale contrast/rescale paths
    c['wrinkle'] = None
    return c


def _texture_candidate(prep, settings):
    """Candidate from the TEXTURE-RATIO map: local wrinkle energy of the
    frame over the baseline's, foil and glint neutralized, thresholded at
    `wrinkle_ratio`.

    The lab defines the wrinkled region AS the active area, and texture is
    the one channel where the device outranks the electrodes instead of
    losing to them by an order of magnitude: on the P3 baselines the disc
    sits 10-25 gray levels from the paper while the strips span 120-255,
    but the wrinkled disc is the only thing whose LOCAL energy rises
    against its own baseline (bench 2026-07-28). The threshold is a
    physical ratio against the frame's own baseline, not a gray-level
    constant, so it transfers across runs, cameras and exposures where an
    Otsu-derived cut does not (the per-frame Otsu swings ~2x over one
    ramp).

    Energies are computed at FULL resolution -- the wrinkle ridges are
    multi-pixel there and downscaling would average them into the grain --
    then the ratio is segmented at the detector's scale with its own
    morphology."""
    import cv2
    base_full = prep['base_full']
    ratio = _ratio_small(prep, settings)
    if ratio is None:
        return None
    small = prep['base_small']
    h, w = small.shape
    x0, y0 = prep['x0'], prep['y0']
    rsub = ratio[y0:h - y0 or h, x0:w - x0 or w]
    thr = max(1.05, float(settings.get('wrinkle_ratio', 1.4)))
    m = rsub >= thr
    # a global texture change (focus loss, gross motion) is not an
    # activation, and segmenting it would outline the whole ROI
    if not m.any() or float(m.mean()) > 0.5:
        return None
    c = _region_candidate(m.astype(np.uint8), 'tex-ratio', offset=(x0, y0))
    if c is None:
        return None
    mm = np.zeros_like(m, np.uint8)
    cv2.drawContours(mm, [np.asarray(c['contour'] - np.array([x0, y0]),
                                     np.int32)], -1, 255, -1)
    if not (mm > 0).any():
        return None
    # A region is an activation only if it is wrinkled THROUGH. A step
    # edge (a rim, a rig line, anything that moved or brightened) draws a
    # high-ratio band exactly one box-window-plus-blur wide around a
    # smooth interior. Erode the region by that band width: a hollow ring
    # then reads its own quiet hole -- or vanishes -- while genuine
    # wrinkling keeps a textured core. An annular activation thinner than
    # the band is below this map's resolution and is refused as
    # indistinguishable from the artifact.
    sc = w / float(base_full.shape[1])
    e = max(5, int(round(27.0 * sc)))
    core = cv2.erode(mm, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                   (2 * e + 1, 2 * e + 1)))
    if not (core > 0).any():
        return None
    if float(np.median(rsub[core > 0])) < 1.0 + 0.5 * (thr - 1.0):
        return None
    # boundary contrast on ITS OWN map (the diff map may be silent on a
    # pure wrinkle, which is the case this channel exists for)
    ring = cv2.dilate(mm, np.ones((15, 15), np.uint8)) & ~mm
    inside = float(rsub[mm > 0].mean())
    outside = float(rsub[ring > 0].mean()) if (ring > 0).any() else 1.0
    c['contrast'] = max(0.0, min(1.0, (inside - outside)
                                 / max(thr - 1.0, 0.1)))
    return c


def _apply_audit_gates(cand, aud, settings):
    """Attach the boundary self-audit and cap the candidate below
    accept_conf when either gate trips (the acceptance fold,
    2026-07-29). -> True when capped."""
    cand['audit'] = aud
    lim = float(settings.get('audit_nostep_pct', 15.0) or 0)
    btol = float(settings.get('audit_bias_px', 3.0) or 0)
    capped = False
    if lim and aud['nostep_pct'] > lim:
        cand['audit_nostep'] = aud['nostep_pct']
        capped = True
    if (btol and aud.get('bias_px') is not None
            and abs(aud['bias_px']) > btol):
        cand['audit_bias'] = aud['bias_px']
        capped = True
    acc = float(settings.get('accept_conf', 0.75))
    if capped and cand['conf'] > acc - 0.01:
        cand['conf'] = round(acc - 0.01, 3)
    return capped


def _resting_refit(prep, settings, ref):
    """The fitter run on a bias-tripped gated frame (2026-07-30,
    calibration round 4): 'resting' claimed the baseline circle while
    the audit measured the ink step off it -- the disc creeping out
    (or the circle sitting off the ink) below the no-change gate's
    sensitivity. Measuring beats asserting: track the step where it
    actually is. Change-map responding gates are waived (the audit
    already proved a measurable step; the change map is silent by
    construction on a gated frame), so `solidity` -- change-fill of
    the ring interior -- reads ~0 here and is NOT filtered on; the
    fit's evidence is its arc coverage, step contrast and residual,
    plus its own audit, applied by the caller. Post-processing mirrors
    the main candidates() path exactly."""
    c = _disc_fit_candidate(prep, settings, ref, assume_responding=True)
    if c is None:
        return None
    f = prep['f']
    if f != 1.0 and not c.pop('fullres', False):
        inv = 1.0 / f
        c['area_px'] *= inv * inv
        c['diam_px'] *= inv
        c['cx'] *= inv
        c['cy'] *= inv
        c['contour'] = (np.asarray(c['contour'], float) * inv).astype(int)
    c['wrinkle'] = _wrinkle_ratio(prep['base_full'], prep['img_full'],
                                  c['contour'])
    # no diff tiers exist on a gated frame: agreement term is moot, the
    # fit's own quality score stands (same as agree_fit=1 in the main path)
    c['conf'] = round(min(0.99, c.pop('conf_own')), 3)
    c['spread_pct'] = c['ci85_pct']
    c['resting_refit'] = True
    return c


def candidates(base_gray, img_gray, settings, prev_method=None):
    """Up to 3 candidate outlines for the active area, best first.

    `prev_method`: the winning method of the PREVIOUS frame in ramp
    order, when the caller iterates one. The incumbent channel gets a
    +0.05 hysteresis bonus, so a challenger must beat it by a margin
    rather than by a coin flip -- single-frame tier flips between two
    near-tied channels caused most same-kV pair mismatches on the bench
    runs (2026-07-29). The bonus is tagged on the candidate
    ('hyst_bonus') so a reader can see exactly what moved.

    Difference-imaging only (bench 2026-07-23: the old HoughCircles candidate
    fabricated a confident circle on EVERY frame -- it teleported around the
    image with a hard-coded high score -- and the DEA expansion is not
    necessarily circular anyway, so the circle prior is wrong):

      |img - baseline| -> blur -> central ROI -> three thresholds
      (0.6*Otsu / Otsu / 1.5*Otsu, or the fixed diff_thresh) -> morphological
      open+close -> largest contour each.

    Plus, since 2026-07-28, one candidate from the texture-ratio map
    (`tex_seg`, method 'tex-ratio'): the P3 runs activate by WRINKLING
    with almost no intensity displacement, so the intensity diff has
    nothing separable to find (sep 0.000 on all 72 frames) while the
    wrinkle energy does. It competes on confidence like any other tier.

    And, when the resting disc is known (baseline_disc), the boundary
    tracker (method 'disc-fit'): rays from the resting centre over a
    fused diff/texture change map, robust ellipse fit, leads and strips
    excluded -- the active area as the lab defines it, the full
    responding disc. Its spread_pct is its own 85% CI on area. On a
    gated frame with a known disc, a 'resting' candidate states that the
    area equals the resting area instead of leaving an empty row -- and
    when the audit measures the ink step OFF that circle (audit_bias),
    the fitter re-measures the boundary (resting-refit) instead of
    letting the stale claim stand.

    Honest no-change gate: if the ROI diff's 99th percentile is below
    min_diff, the intensity tiers return nothing (low-kV frames really
    look identical -- inventing an outline there was the old failure
    mode). The texture and boundary channels are consulted either way: a
    fine wrinkle can be invisible to the downscaled diff yet real, and
    dropping it with the gate would re-create the old blindness in the
    one case those channels exist for.

    Confidence = 0.3*solidity + 0.3*boundary-contrast + 0.2*cross-method
    agreement + 0.2*wrinkle bonus; solidity (not circularity) so slightly
    oblong expansions score fully. Frames wider than DETECT_MAX_W are
    detected at reduced scale and every px quantity is rescaled to full
    resolution.
    """
    import cv2
    if base_gray is None:
        # REFUSE-DON'T-FABRICATE (audit 2026-08-05, critical): without a
        # readable baseline there is no difference to image. The old
        # base-less fallback thresholded the raw photograph; every honest
        # channel (disc-fit / resting / tex-ratio) refused, and the
        # surviving Otsu tiers outlined the ROI *background* at conf
        # 0.85-0.90, spread 0.0 — auto-accepting a 2.74x-wrong area with
        # a trustworthy-looking note. No baseline, no candidates;
        # callers surface the refusal to the operator.
        return []
    prep = prepared_diff(base_gray, img_gray, settings)
    sub, x0, y0, f = (prep['sub'], prep['x0'], prep['y0'],
                      prep['f'])
    img_full, base_full = prep['img_full'], prep['base_full']
    ref = baseline_disc(base_gray, settings) if base_gray is not None \
        else None
    min_sol = float(settings.get('min_solidity', 0))
    gated = (float(np.percentile(sub, 99))
             < float(settings.get('min_diff', 10)))
    out = []
    weak = []
    if not gated:
        otsu_t, _m = cv2.threshold(sub, 0, 255,
                                   cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        fixed = float(settings.get('diff_thresh') or 0)
        threshes = ([('diff-fixed', fixed)] if fixed else []) + \
            [('diff-lo', max(3.0, 0.6 * otsu_t)),
             ('diff-otsu', max(3.0, otsu_t)),
             ('diff-hi', max(4.0, 1.5 * otsu_t))]
        for method, t in threshes[:3]:
            _t, m = cv2.threshold(sub, t, 255, cv2.THRESH_BINARY)
            c = _region_candidate(m, method, offset=(x0, y0))
            if c is None:
                continue
            if c['solidity'] >= min_sol:
                out.append(c)
            else:
                weak.append(c)
    tc = None
    if int(settings.get('tex_seg', 1) or 0):
        tc = _texture_candidate(prep, settings)
        if tc is not None:
            if tc['solidity'] >= min_sol:
                out.append(tc)
            else:
                weak.append(tc)
    # a gated frame with no texture response has nothing for a boundary
    # fit to track -- that is the resting case, not a detection
    dfc = _disc_fit_candidate(prep, settings, ref) \
        if (not gated or tc is not None) else None
    if dfc is not None:
        if dfc['solidity'] >= min_sol:
            out.append(dfc)
        else:
            weak.append(dfc)
    if gated and not out and not weak:
        # no change, and the resting disc is known: state the resting
        # area instead of an empty row (see _resting_candidate)
        rc = _resting_candidate(prep, settings, ref)
        if rc is not None:
            out.append(rc)
    if not out and weak:
        # Nothing passed the fill filter, but SOMETHING changed (diff gate
        # passed, or the texture map lit up) -- keep the best weak
        # candidate so the human gets to judge it instead of a silent
        # auto-reject (user 2026-07-23: subtle changes are visible by
        # eye). Tagged so it can never auto-accept.
        best_weak = max(weak, key=lambda c: c['solidity'])
        best_weak['fallback'] = True
        out = [best_weak]
    if not out:
        return []
    # boundary contrast (downscaled): diff level inside vs just outside.
    # The tex-ratio candidate arrives with contrast measured on its own
    # map -- the diff can be silent on a pure wrinkle, and scoring it
    # there would punish exactly the case the channel exists for.
    ker = np.ones((15, 15), np.uint8)
    for c in out:
        if 'contrast' in c:
            c.pop('mask_local', None)
            c.pop('_thresh', None)
            continue
        m = np.zeros_like(sub)
        cv2.drawContours(m, [np.asarray(c['contour'] -
                                        np.array([x0, y0]), np.int32)],
                         -1, 255, -1)
        ring = cv2.dilate(m, ker) & ~m
        inside = float(sub[m > 0].mean()) if (m > 0).any() else 0.0
        outside = float(sub[ring > 0].mean()) if (ring > 0).any() else 0.0
        c['contrast'] = max(0.0, min(1.0, (inside - outside) / 20.0))
        c.pop('mask_local', None)
        c.pop('_thresh', None)
    if f != 1.0:                    # rescale every px quantity to full-res
        inv = 1.0 / f
        for c in out:
            if c.pop('fullres', False):
                continue            # resting: born in full-res coordinates
            c['area_px'] *= inv * inv
            c['diam_px'] *= inv
            c['cx'] *= inv
            c['cy'] *= inv
            c['contour'] = (np.asarray(c['contour'], float) * inv).astype(int)
    # wrinkle index at full resolution (contours are full-res now)
    for c in out:
        c['wrinkle'] = _wrinkle_ratio(base_full, img_full, c['contour'])
    # Corroboration by SEMANTICS (2026-07-28): the diff tiers all measure
    # the full changed region, so their mutual spread measures threshold
    # sensitivity, as always. The tex candidate outlines the wrinkled
    # INTERIOR -- a subset by definition, so its area belongs in no
    # spread; its corroboration is sitting inside the boundary fit. The
    # disc-fit reports its own fit CI as spread_pct (an area measurement's
    # honest dispersion), lightly modulated by whether the tiers land
    # near its boundary. Mixing all areas into one spread made every
    # channel accuse every other of disagreement over a difference of
    # DEFINITION, and 24/24 frames went to review on it.
    tiers = [c for c in out if c['method'].startswith('diff')]
    dfit = next((c for c in out if c['method'] == 'disc-fit'), None)
    areas = np.array([c['area_px'] for c in tiers], float)
    spread = float(areas.std() / areas.mean()) if len(tiers) > 1 else 0.0
    agreement = max(0.0, 1.0 - spread)
    for c in out:
        m = c['method']
        if m == 'disc-fit':
            agree_fit = 1.0
            if len(areas):
                agree_fit = max(0.0, 1.0 - min(1.0, abs(
                    float(np.median(areas)) - c['area_px'])
                    / max(c['area_px'], 1.0)))
            c['conf'] = round(min(0.99, c.pop('conf_own')
                                  * (0.85 + 0.15 * agree_fit)), 3)
            c['spread_pct'] = c['ci85_pct']
            continue
        if m == 'resting':
            c['conf'] = c.pop('conf_own')
            c['spread_pct'] = 0.0
            continue
        # wrinkle bonus: a candidate whose interior is wrinkle-textured
        # outranks a bigger smooth one (the wrinkled region is active).
        wbonus = max(0.0, min(1.0, (c['wrinkle'] - 1.0) / 1.5))
        agr = agreement
        if m == 'tex-ratio' and dfit is not None:
            inside = (np.hypot(c['cx'] - dfit['cx'], c['cy'] - dfit['cy'])
                      <= 0.5 * dfit['diam_px'])
            agr = (1.0 if inside
                   and c['area_px'] <= 1.2 * dfit['area_px'] else 0.3)
        c['conf'] = round(0.3 * c['solidity'] + 0.3 * c['contrast']
                          + 0.2 * agr + 0.2 * wbonus, 3)
        c['spread_pct'] = round(100 * spread, 1)
    if prev_method:
        for c in out:
            if c['method'] == prev_method:
                c['hyst_bonus'] = 0.05
                c['conf'] = round(min(0.99, c['conf'] + 0.05), 3)
                break
    # The recorded quantity is the BOUNDARY's area (2026-07-28 ruling), so
    # a changed or wrinkled patch sitting inside a valid boundary fit is
    # supporting evidence, never the better answer: cap it just below the
    # fit. Tex-only at first; the diff tiers joined 2026-07-29 (operator
    # spot-read, 155425 @ 5.25 kV post: diff-hi outlined an interior patch
    # at half the fit's area and outranked the correct boundary). A diff
    # region LARGER than the fit is a disagreement, not containment, and
    # still competes; where the fit refuses (event frames), tex and diff
    # still win outright.
    if dfit is not None:
        for c in out:
            if ((c['method'] == 'tex-ratio'
                 or c['method'].startswith('diff'))
                    and c['conf'] >= dfit['conf']
                    and np.hypot(c['cx'] - dfit['cx'],
                                 c['cy'] - dfit['cy']) <= 0.5 * dfit['diam_px']
                    and c['area_px'] <= 1.2 * dfit['area_px']):
                c['conf'] = round(max(0.0, dfit['conf'] - 0.01), 3)
                c['capped_by'] = 'disc-fit'
    out.sort(key=lambda c: c['conf'], reverse=True)
    # The self-audit is folded into ACCEPTANCE (2026-07-29): the winner
    # keeps its rank and area (it is still the best measurement on
    # offer), but it loses the right to auto-accept when the audit
    # contradicts it. Two gates. Near wrinkle onset the ink locally
    # washes out and the ellipse INTERPOLATES those sectors: no-step arc
    # > audit_nostep_pct. And a 'resting' claim goes STALE when the disc
    # expands below the no-change gate's sensitivity -- on all six bench
    # runs the 1.5-3 kV band audits at bias -4..-11 px while auto-
    # accepting at conf 0.84-0.99: |bias| > audit_bias_px. Tagged, so
    # the review queue says exactly why.
    best = out[0]
    if best['method'] in ('disc-fit', 'resting'):
        aud = audit_boundary(prep, best, settings)
        if aud is not None:
            capped = _apply_audit_gates(best, aud, settings)
            # Resting-refit (2026-07-30, calibration round 4): a bias-
            # tripped 'resting' claim means the ink step is measurably
            # off the claimed circle -- operator-verified at 2.0 kV
            # (+4.0/+6.5% area beyond the trace's own definitional
            # baseline, matching the audit's predicted creep). The
            # fitter tracks that ink step directly, so measure the
            # boundary instead of asserting it. The refit is audited
            # like any winner and takes the frame only on its own
            # merits; the capped resting claim stays as runner-up for
            # the human, and a refused or audit-dirty fit changes
            # nothing.
            if (capped and best['method'] == 'resting'
                    and best.get('audit_bias') is not None):
                rf = _resting_refit(prep, settings, ref)
                if rf is not None:
                    aud2 = audit_boundary(prep, rf, settings)
                    if aud2 is not None:
                        _apply_audit_gates(rf, aud2, settings)
                        out.append(rf)
                        out.sort(key=lambda c: c['conf'], reverse=True)
    return out[:3]


def needs_review(cands, settings):
    """True when the human should choose (weak/absent/disagreeing edges).
    A fill-filter fallback candidate ALWAYS goes to review -- it exists
    precisely because the detection was not sure."""
    if not cands:
        return True
    if cands[0].get('fallback'):
        return True
    if cands[0]['conf'] < float(settings['accept_conf']):
        return True
    return cands[0]['spread_pct'] > float(settings['spread_pct'])


# ---------------------------------------------------------------------------
# breakdown heuristics
# ---------------------------------------------------------------------------

def breakdown_flags(rows, accepted_areas, settings):
    """-> (confirmed, advisory), both {row_index: reason}. Only `confirmed`
    drives mark_breakdown_files / post-breakdown branding; `advisory` is
    notes-only and must never rename a frame.

    Current rule (rebuilt 2026-08-04, ground-truthed on the 13-run batch):
    baseline = per-run MEDIAN of every parseable measured_uA (robust even
    at the worst observed 31% event contamination); an event row deviates
    from it by >= breakdown_dev_ua. CONFIRMED needs two event rows on
    ADJACENT csv rows, or the run's last parseable-uA row being an event
    (terminal, no recovery evidence -- 152205/155425 both end mid-event).
    A single event row followed by recovery is a self-clearing transient
    (104531: one -153 uA sample, then healthy to 10 kV) -> advisory only.
    A blank-uA row between two event rows BREAKS adjacency -- deliberate:
    at ~30 s between snapshots there is no evidence the excursion spanned
    the gap. Absolute thresholds cannot do this job: the 07-29 campaign
    sits on a stiff -16 uA instrument offset, so abs(ua) > 50 was really
    34 uA more-negative / 66 uA positive depending on the run. With < 5
    parseable rows there is no meaningful median and the legacy absolute
    breakdown_ua rule applies unchanged (dry runs, sparse logs).

    Area rule: a collapse > area_jump_pct while nominal_kV did not
    decrease is CONFIRMED only when corroborated by a current event at
    the same nominal-kV level; uncorroborated it is demoted to advisory
    (P3_5: a 36% "collapse" from the manual-trace -> disc-fit method
    switch renamed 35 healthy frames while the current never left
    +-11 uA of baseline). In the no-median fallback the legacy behaviour
    (collapse alone confirms) is kept."""
    flags, advis = {}, {}
    ua_lim = float(settings['breakdown_ua'])
    dev_lim = float(settings.get('breakdown_dev_ua',
                                 DEFAULT_SETTINGS['breakdown_dev_ua']))
    jump = float(settings['area_jump_pct'])

    def _adv(i, msg):
        advis[i] = (advis[i] + '; ' + msg) if i in advis else msg

    uas = {}
    for i, row in enumerate(rows):
        try:
            uas[i] = float(row.get('measured_uA') or '')
        except (TypeError, ValueError):
            pass
    median = None
    events = {}                    # event rows: i -> signed deviation
    if len(uas) >= 5:
        vals = sorted(uas.values())
        n = len(vals)
        median = (vals[n // 2] if n % 2 else
                  0.5 * (vals[n // 2 - 1] + vals[n // 2]))
        events = {i: ua - median for i, ua in uas.items()
                  if abs(ua - median) >= dev_lim}
        confirmed = set()
        ev = sorted(events)
        for a, b in zip(ev, ev[1:]):
            if b - a == 1:                       # adjacent rows, no gap
                confirmed.update((a, b))
        if max(uas) in events:                   # terminal: run ends over
            confirmed.add(max(uas))
        for i in ev:
            d = abs(events[i])
            if i in confirmed:
                flags[i] = (f"breakdown? I dev {d:.0f}uA >= {dev_lim:g}uA "
                            f"(baseline {median:.1f}uA)")
            else:
                _adv(i, f"transient discharge? I dev {d:.0f}uA")
    else:
        for i, ua in uas.items():
            if abs(ua) > ua_lim:
                flags[i] = f"breakdown? I={ua:.0f}uA > {ua_lim:g}uA"

    def _kv(row):
        try:
            return float(row.get('nominal_kV') or '')
        except (TypeError, ValueError):
            return None

    prev_area = prev_kv = None
    for i, row in enumerate(rows):
        area = accepted_areas.get(i)
        kv = _kv(row)
        if (area and prev_area and kv is not None and prev_kv is not None
                and kv >= prev_kv
                and area < prev_area * (1.0 - jump / 100.0)):
            pct = 100 * (1 - area / prev_area)
            corroborated = median is None or any(
                (k := _kv(rows[j])) is not None and abs(k - kv) < 1e-9
                for j in events)
            if corroborated:
                flags.setdefault(i, f"breakdown? area collapsed {pct:.0f}%")
            elif i not in flags:
                _adv(i, f"collapse? area -{pct:.0f}% (no current signature)")
        if area:
            prev_area, prev_kv = area, kv
    # A confirmed row supersedes its own advisories: a single recovered
    # current event that corroborates an area collapse ON THE SAME ROW
    # landed in both dicts (the transient note is written before the
    # collapse pass can confirm the row), and 'breakdown? area collapsed'
    # next to 'transient discharge?' on one row reads as a contradiction
    # (review 2026-08-04).
    for i in flags:
        advis.pop(i, None)
    return flags, advis


def wrinkle_onset(rows, results, settings):
    """Wrinkle-MODE detection over the accepted results.

    The wrinkle index (stored on each candidate by `candidates`) compares
    high-frequency texture inside the outline to the same region of the
    baseline. At/above `wrinkle_ratio` the DEA is in its wrinkled (activated)
    state. Returns (onset_row_index_or_None, {row_index: annotation}):
    the first wrinkled row is annotated 'wrinkle-mode onset', later ones
    'wrinkle-mode'. These are informational notes -- NOT breakdown flags, so
    they never trigger the _BREAKDOWN file renaming."""
    lim = float(settings.get('wrinkle_ratio', 1.6))
    onset = None
    annos = {}
    for i in range(len(rows)):
        r = results.get(i)
        if not r:
            continue
        w = float(r.get('wrinkle') or 0)
        if w >= lim:
            if onset is None:
                onset = i
                annos[i] = f"wrinkle-mode onset (idx {w:g})"
            else:
                annos[i] = f"wrinkle-mode (idx {w:g})"
    return onset, annos


def reconcile_pairs(rows, cands_by_idx, settings):
    """Fold same-kV pair agreement into confidence, BEFORE auto-accept.

    The two snapshots of one step are independent detections of one
    physical state -- the strongest per-frame evidence the run offers.
    When their best candidates agree within a tolerance derived from
    their own fit CIs, both gain +0.05 (tagged 'pair_confirmed'); when
    they disagree past twice that tolerance, both are capped just below
    accept_conf (tagged 'pair_mismatch_pct'), so a confident-looking
    tier flip can never auto-accept on both sides of a contradiction.
    Candidates without a CI (blob tiers) get a 6% default tolerance each,
    matching ramp_consistency's 12% pair rule. Mutates the best
    candidates in place; -> {'confirmed': n, 'capped': n}.

    A member tagged 'audit_nostep' or 'audit_bias' stays capped below
    accept_conf even when its pair agrees: two snapshots interpolated
    over the same washed-out arc (or stated resting while the edge sat
    outside the circle in both) agree beautifully -- that is the
    correlated-error case pair agreement cannot certify against, and
    the audit's verdict about THIS boundary outranks consistency
    between two of them."""
    acc = float(settings.get('accept_conf', 0.75))
    by_kv = {}
    for i, row in enumerate(rows):
        try:
            kv = float(row.get('nominal_kV') or '')
        except (TypeError, ValueError):
            continue
        cl = cands_by_idx.get(i)
        if cl:
            by_kv.setdefault(kv, []).append(cl[0])
    stats = {'confirmed': 0, 'capped': 0}
    for kv, members in sorted(by_kv.items()):
        if len(members) < 2:
            continue
        areas = [float(b['area_px']) for b in members]
        lo, hi = min(areas), max(areas)
        mid = (hi + lo) / 2.0
        if mid <= 0:
            continue
        rel = (hi - lo) / mid
        tol = max(0.04, sum(
            (1.5 * b['ci85_pct'] / 100.0)
            if b.get('ci85_pct') is not None else 0.06 for b in members))
        if rel <= tol:
            for b in members:
                b['pair_confirmed'] = True
                cap = round(acc - 0.01, 3) \
                    if (b.get('audit_nostep') or b.get('audit_bias')) \
                    else 0.99
                b['conf'] = round(min(cap, b['conf'] + 0.05), 3)
            stats['confirmed'] += len(members)
        elif rel > 2.0 * tol:
            for b in members:
                b['pair_mismatch_pct'] = round(100 * rel, 1)
                if b['conf'] > acc - 0.01:
                    b['conf'] = round(acc - 0.01, 3)
            stats['capped'] += len(members)
    return stats


def audit_boundary(prep, cand, settings):
    """Self-audit of an ACCEPTED boundary: is there really an ink step
    under it, and is the fit centred on that step?

    Along the candidate's own contour (per-angle radius, so an ellipse
    is not penalized), each open ray reports the SIGNED offset between
    the fitted boundary and the measured step position (+ = fit outside
    the step, full-res px), or 'no step' when nothing under the boundary
    clears the scene's adaptive cut. 'Circled the noise' reads as a
    large no-step arc; a systematically wrong feature reads as nonzero
    bias with small MAD. conf certifies consistency; THIS is the
    correctness check that costs the operator nothing (2026-07-29).
    -> {'bias_px', 'mad_px', 'nostep_pct', 'n_rays'} or None."""
    import cv2
    if cand is None or cand.get('method') not in ('disc-fit', 'resting'):
        return None
    small, base_full = prep['base_small'], prep['base_full']
    if small is None or base_full is None:
        return None
    h, w = small.shape
    f = w / float(base_full.shape[1])
    cx, cy = cand['cx'] * f, cand['cy'] * f
    pts = np.asarray(cand['contour'], np.float64) * f
    ang = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
    rad = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
    order = np.argsort(ang)
    ang, rad = ang[order], rad[order]
    r0 = float(np.median(rad))
    if r0 < 8:
        return None
    sm = cv2.GaussianBlur(cv2.medianBlur(
        np.clip(prep['img_small'], 0, 255).astype(np.uint8), 3),
        (0, 0), 2.0).astype(np.float32)
    fo = _foil_small(base_full, (w, h))
    win = max(6.0, 0.3 * r0)
    nray = 180
    th = np.linspace(-np.pi, np.pi, nray, endpoint=False)
    r_th = np.interp(th, ang, rad, period=2 * np.pi)
    offs, steps, nostep = [], [], 0
    for k in range(nray):
        rr = np.arange(max(2.0, r_th[k] - win), r_th[k] + win, 1.0)
        xs = (cx + rr * np.cos(th[k])).astype(np.float32)[None, :]
        ys = (cy + rr * np.sin(th[k])).astype(np.float32)[None, :]
        rej = cv2.remap(fo.astype(np.uint8), xs, ys, cv2.INTER_NEAREST,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=1)[0]
        if rej.any():
            continue
        prof = cv2.remap(sm, xs, ys, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)[0]
        st = np.diff(prof)
        gi = int(np.argmax(st))
        ins = prof[max(gi - 16, 0):gi - 3]
        outs = prof[gi + 4:gi + 17]
        stepc = (float(np.median(outs)) - float(np.median(ins))
                 if ins.size >= 3 and outs.size >= 3 else 0.0)
        s0 = float(st[gi])
        sm1 = float(st[gi - 1]) if gi > 0 else s0
        sp1 = float(st[gi + 1]) if gi < len(st) - 1 else s0
        den = sm1 - 2 * s0 + sp1
        d = 0.5 * (sm1 - sp1) / den if abs(den) > 1e-9 else 0.0
        offs.append((r_th[k] - (rr[gi] + 0.5 + min(0.75, max(-0.75, d))))
                    / f)
        steps.append(stepc)
    if len(steps) < 12:
        return None
    cut = max(3.0, 0.35 * float(np.median([s for s in steps if s > 2.0]
                                          or [3.0])))
    good = [o for o, s in zip(offs, steps) if s >= cut]
    nostep = sum(1 for s in steps if s < cut)
    if len(good) < 6:
        return {'bias_px': None, 'mad_px': None,
                'nostep_pct': round(100.0 * nostep / len(steps), 1),
                'n_rays': len(steps)}
    b = float(np.median(good))
    return {'bias_px': round(b, 2),
            'mad_px': round(float(np.median(np.abs(np.asarray(good) - b))),
                            2),
            'nostep_pct': round(100.0 * nostep / len(steps), 1),
            'n_rays': len(steps)}


def ramp_consistency(rows, results, settings=None,
                     pair_tol=0.12, dip_slack=0.10):
    """Annotations for the physics a per-frame detector cannot see.
    -> {row_index: note}

    Two invariants the rig guarantees: the two snapshots of one step
    (pre-ramp / post-ramp) photograph the same state, so their areas must
    agree; and the area cannot shrink while the voltage rises, short of
    breakdown. Violations are ANNOTATED for review, never averaged away:
    a pair mismatch usually means the two frames' detections picked
    different objects or tiers, and a dip usually IS the interesting
    event (pull-in, breakdown) or a detection failure -- both are for a
    human. `results` maps row index -> accepted candidate dict (the same
    shape apply_results takes)."""
    def _area(i):
        r = results.get(i)
        try:
            a = float(r.get('area_px')) if r else None
        except (TypeError, ValueError):
            return None
        return a if a and a > 0 else None

    def _kv(row):
        try:
            return float(row.get('nominal_kV') or '')
        except (TypeError, ValueError):
            return None

    annos = {}
    by_step = {}
    for i, row in enumerate(rows):
        kv = _kv(row)
        if kv is not None and _area(i) is not None:
            by_step.setdefault(kv, []).append(i)
    for kv, idxs in sorted(by_step.items()):
        if len(idxs) < 2:
            continue
        vals = [_area(i) for i in idxs]
        lo, hi = min(vals), max(vals)
        mid = (hi + lo) / 2.0
        if mid > 0 and (hi - lo) / mid > pair_tol:
            for i in idxs:
                annos[i] = (f"pair mismatch "
                            f"{100 * (hi - lo) / mid:.0f}% at {kv:g} kV")
    prev_a = prev_kv = None
    for i, row in enumerate(rows):
        kv, a = _kv(row), _area(i)
        if kv is None or a is None:
            continue
        if (prev_a and prev_kv is not None and kv >= prev_kv
                and a < prev_a * (1.0 - dip_slack)):
            annos.setdefault(i, f"area dip {100 * (1 - a / prev_a):.0f}% "
                                f"vs previous step")
        prev_a, prev_kv = a, kv
    return annos


# ---------------------------------------------------------------------------
# scale + write-back
# ---------------------------------------------------------------------------

def _fit_circle(pts):
    """Trimmed Kasa circle fit -> (cx, cy, r, keep_mask). Refits after
    dropping >2.5-sigma residuals so a few edge points on the wrong
    feature (a shadow, the annulus) cannot steer the circle."""
    keep = np.ones(len(pts), bool)
    ux = uy = r = 0.0
    for _ in range(4):
        x, y = pts[keep, 0], pts[keep, 1]
        design = np.column_stack([x, y, np.ones(x.size)])
        rhs = x * x + y * y
        sol, *_ = np.linalg.lstsq(design, rhs, rcond=None)
        ux, uy = sol[0] / 2.0, sol[1] / 2.0
        r = float(np.sqrt(max(sol[2] + ux * ux + uy * uy, 1e-9)))
        resid = np.abs(np.hypot(pts[:, 0] - ux, pts[:, 1] - uy) - r)
        sig = max(float(np.std(resid[keep])), 1.0)
        nxt = resid <= 2.5 * sig
        if (nxt == keep).all() or nxt.sum() < 24:
            break
        keep = nxt
    return float(ux), float(uy), r, keep


_DISC_CACHE = {}
# Why the fit refused, keyed exactly like _DISC_CACHE and evicted with it.
# A parallel dict rather than a tuple in _DISC_CACHE so the cached VALUE
# keeps its old shape: several readers do `dict(hit)` on it.
_DISC_WHY = {}


def baseline_disc(base_gray, settings):
    """Trace the RESTING DEA disc on the baseline frame itself (no diff).

    Difference imaging can never calibrate off the baseline (a self-diff
    yields nothing), so the px→mm scale keys off this. The resting disc
    reads DARKER than its surround (P3 baselines: disc 162-167, paper
    176-190), and this is the one detection where a circle prior is
    legitimate: the resting active area is circular by construction,
    which is exactly what the nominal-diameter scale assumes.

    Region-growing cannot do this job. On all three P3 baselines the dark
    class is bridged to both electrode strips by smooth CNT traces and
    under-strip shadows that neither the brightness cut nor the texture
    footprint covers, and the 21x21 merge close then returned one blob
    spanning 85% of the frame width with circ 0.32 -- at conf 0.93 --
    and mm_per_px silently inherited a 1.5-1.6x diameter error, putting
    every recorded active_area_mm2 off by 2.3-2.7x (bench 2026-07-28).

    So: seed on the biggest central dark region, cast radial rays, take
    the strongest dark->light step on each ray whose edge neighborhood is
    clear of foil and glint (rays measuring through the strips are
    excluded, not repaired), and fit a circle robustly to the edge points
    -- twice, because an off-centre seed truncates the far side. Refuses (None) unless the accepted edge
    covers >= ~120 degrees of arc, the fit residual stays under 6% of the
    radius, the circle is actually filled with the dark class, and the
    diameter is a plausible fraction of the ROI; conf is built from those
    same three quantities, so a shape that barely passes cannot read as
    certainty. Verified against the by-eye measurement on the three P3
    baselines (579/578/586 px): agreement within ~1%.

    Returns a candidate-like dict (method 'baseline-disc') or None. When
    it refuses, WHICH gate refused is recorded and readable through
    `baseline_disc_refusal` — the calibration dialog's verify mode falls back
    to a hand measurement on a refusal and has to be able to say why
    (`#215`, 2026-08-06 evening)."""
    key = _disc_key(base_gray, settings)
    if key in _DISC_CACHE:
        hit = _DISC_CACHE[key]
        return dict(hit) if hit is not None else None
    ref, why = _baseline_disc_uncached(base_gray, settings)
    if len(_DISC_CACHE) >= 4:
        old = next(iter(_DISC_CACHE))
        _DISC_CACHE.pop(old)
        _DISC_WHY.pop(old, None)
    _DISC_CACHE[key] = ref
    _DISC_WHY[key] = why
    return dict(ref) if ref is not None else None


def _disc_key(base_gray, settings):
    return (_fingerprint(base_gray) if base_gray is not None else None,
            float(settings.get('roi_frac', 0.85)),
            float(settings.get('electrode_lum',
                               DEFAULT_SETTINGS['electrode_lum'])
                  or DEFAULT_SETTINGS['electrode_lum']))


def baseline_disc_refusal(base_gray, settings):
    """Which of `baseline_disc`'s own gates refused this baseline, as one
    operator-facing sentence — or None when the fit succeeded (or when the
    fit has never been attempted for this frame/settings pair).

    `baseline_disc` returns a bare None, which is the right contract for
    every automatic caller but useless to an operator being told to
    measure by hand instead. The reason is produced by the fit itself and
    cached beside its result, so asking costs nothing after the fit has
    run once and asking NEVER re-runs it on a different frame than the
    answer describes.

    Deliberately NOT a promise that the fit is retried: a caller that has
    not called `baseline_disc` for this key gets None, which reads the same
    as "no refusal on record". The dialog calls the fit first, always."""
    return _DISC_WHY.get(_disc_key(base_gray, settings))


def _baseline_disc_uncached(base_gray, settings):
    """-> (candidate dict or None, refusal reason or None).

    Every refusal names the gate it failed. There are eleven of them and
    they are not interchangeable: 'the disc is off the size range' sends
    the operator to check diam_mm and the camera zoom, while 'the arc is
    only 22% covered' sends them to look at what is lying across the
    frame. A single 'refused' would send them to guess."""
    import cv2
    if base_gray is None:
        return None, 'there is no readable baseline frame to fit'
    g = np.asarray(base_gray, np.float32)
    h0, w0 = g.shape
    f = 1.0
    if w0 > DETECT_MAX_W:
        f = DETECT_MAX_W / float(w0)
        g = cv2.resize(g, (DETECT_MAX_W, max(1, int(round(h0 * f)))),
                       interpolation=cv2.INTER_AREA)
    h, w = g.shape
    rf = min(1.0, max(0.2, float(settings.get('roi_frac', 0.85))))
    x0 = int(w * (1 - rf) / 2)
    y0 = int(h * (1 - rf) / 2)
    sub = g[y0:h - y0 or None, x0:w - x0 or None]
    hs, ws = sub.shape
    if min(hs, ws) < 48:
        return None, (f"the central search window is only {ws}x{hs} px "
                      f"(under 48) — too small to cast rays across")
    # denoise at this scale (the by-eye measurement used median 9 +
    # sigma 6 at full resolution; a third of that at a third the size)
    sm = cv2.GaussianBlur(cv2.medianBlur(sub.astype(np.uint8), 3),
                          (0, 0), 2.0).astype(np.float32)
    lum = float(settings.get('electrode_lum',
                             DEFAULT_SETTINGS['electrode_lum'])
                or DEFAULT_SETTINGS['electrode_lum'])
    reject = _foil_small(base_gray, (w, h))[y0:h - y0 or None,
                                            x0:w - x0 or None].copy()
    reject |= sm >= lum
    free = ~reject
    if int(free.sum()) < 400:
        return None, (f"only {int(free.sum())} px of this baseline are "
                      f"free of foil and glint (need 400) — the frame is "
                      f"probably overexposed, or electrode_lum is too low")
    paper = float(np.median(sm[free]))
    # seed: biggest, most central dark region (disc sits 10-25 gray
    # levels below the paper; 5 keeps a faint top edge in the class)
    dark = ((sm < paper - 5) & free).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(dark)
    seed, score = None, 0.0
    for i in range(1, n):
        a = float(stats[i, cv2.CC_STAT_AREA])
        if a < 0.002 * sub.size:
            continue
        cx, cy = cent[i]
        d2 = ((cx - ws / 2) / ws) ** 2 + ((cy - hs / 2) / hs) ** 2
        s = a * max(0.05, 1.0 - 4.0 * d2)
        if s > score:
            seed, score = (float(cx), float(cy)), s
    if seed is None:
        return None, (f"no central dark region big enough to seed on "
                      f"(the paper reads {paper:.0f} gray and nothing "
                      f"below it covers 0.2% of the search window)")

    r_hi = 0.55 * min(hs, ws)
    rs = np.arange(6.0, r_hi, 1.0)
    nray = 360

    def cast(cx0, cy0):
        """Edge points: per ray, the strongest dark->light step whose
        EDGE NEIGHBORHOOD is clear of foil and glint. Contamination is
        judged around the step, not along the whole path: an electrode
        crossing the disc interior occludes the middle of many rays whose
        edge measurement is perfectly clean, and origin-to-edge rejection
        would throw all of them away."""
        th = np.linspace(0, 2 * np.pi, nray, endpoint=False)
        xs = (cx0 + np.outer(np.cos(th), rs)).astype(np.float32)
        ys = (cy0 + np.outer(np.sin(th), rs)).astype(np.float32)
        prof = cv2.remap(sm, xs, ys, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
        rej = cv2.remap(reject.astype(np.uint8), xs, ys, cv2.INTER_NEAREST,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=1)
        rejcum = np.cumsum(rej > 0, axis=1)
        step = np.diff(prof, axis=1)
        pts = []
        nr = len(rs)
        for k in range(nray):
            gi = int(np.argmax(step[k]))
            if gi < 8 or gi > nr - 9:
                continue
            if (rejcum[k, min(gi + 10, nr - 1)]
                    - rejcum[k, max(gi - 15, 0)]) > 0:
                continue
            ins = prof[k, max(gi - 20, 0):gi - 4]
            outs = prof[k, gi + 5:gi + 21]
            if ins.size < 4 or outs.size < 4:
                continue
            if float(np.median(outs)) - float(np.median(ins)) < 4.0:
                continue
            redge = rs[gi] + 0.5
            pts.append((cx0 + redge * np.cos(th[k]),
                        cy0 + redge * np.sin(th[k])))
        return np.asarray(pts, np.float64)

    pts = cast(*seed)
    if len(pts) < 40:
        return None, (f"only {len(pts)} of 360 radial rays found a clean "
                      f"dark→light ink step (need 40) — the disc edge is "
                      f"too faint, or the electrodes cover too much of it")
    cx1, cy1, _r1, _k1 = _fit_circle(pts)
    if not (0 <= cx1 <= ws and 0 <= cy1 <= hs):
        return None, ("the first circle fit put the centre outside the "
                      "search window — the edge points are not a disc")
    pts = cast(cx1, cy1)                # re-cast from the fitted centre
    if len(pts) < 40:
        return None, (f"only {len(pts)} of 360 rays found a clean ink step "
                      f"on the re-cast from the fitted centre (need 40)")
    cx, cy, r, keep = _fit_circle(pts)
    pin = pts[keep]
    if len(pin) < 40 or not (0 <= cx <= ws and 0 <= cy <= hs):
        return None, (f"{len(pin)} edge points survived the robust fit's "
                      f"outlier trim (need 40), or its centre fell outside "
                      f"the search window")
    resid = float(np.median(np.abs(
        np.hypot(pin[:, 0] - cx, pin[:, 1] - cy) - r)))
    ang = np.degrees(np.arctan2(pin[:, 1] - cy, pin[:, 0] - cx)) % 360.0
    cov = len(np.unique((ang // 10).astype(int))) / 36.0
    yy, xx = np.ogrid[0:hs, 0:ws]
    inside = ((xx - cx) ** 2 + (yy - cy) ** 2 <= (0.9 * r) ** 2) & free
    if int(inside.sum()) < 200:
        return None, (f"only {int(inside.sum())} px inside the fitted "
                      f"circle are free of foil and glint (need 200), so "
                      f"the fill test could not be applied")
    fill = float(((sm < paper - 4) & inside).sum()) / float(inside.sum())
    dmin = float(min(hs, ws))
    # The four documented gates, named individually: 'refused' sends the
    # operator to guess, while 'the arc is only 22% covered' sends them to
    # look at what is lying across the frame (`#215` verify mode, 2026-08-06).
    if cov < 0.34:
        return None, (f"the accepted edge covers only {360 * cov:.0f}° of "
                      f"arc (needs ≥ 120°) — something is lying across the "
                      f"disc, or most of its boundary has no ink step")
    if resid > 0.06 * r:
        return None, (f"the fit residual is {resid * 100.0 / r:.1f}% of the "
                      f"radius (limit 6%) — the surviving edge points are "
                      f"not on one circle")
    if fill < 0.55:
        return None, (f"only {100 * fill:.0f}% of the fitted circle's "
                      f"interior reads as the dark class (needs ≥ 55%) — "
                      f"the circle is not sitting on the disc")
    if not 0.06 * dmin <= 2 * r <= 0.85 * dmin:
        return None, (f"the fitted diameter {2 * r / f:.0f} px is outside "
                      f"the plausible range {0.06 * dmin / f:.0f}–"
                      f"{0.85 * dmin / f:.0f} px for this search window — "
                      f"check the camera zoom and roi_frac")
    try:
        ell = cv2.fitEllipse(pin.astype(np.float32))
        circ = round(min(ell[1]) / max(max(ell[1]), 1e-6), 3)
    except cv2.error:
        circ = 0.0
    # The one gate the old code lacked: a resting disc is ROUND. The blob
    # it used to return read circ 0.32; a shadow rectangle's trimmed fit
    # still only reaches ~0.7. The P3 discs measure 0.966-0.999.
    if circ < 0.85:
        return None, (f"the fitted shape's circularity is {circ:.2f} "
                      f"(needs ≥ 0.85) — a resting disc is round, and this "
                      f"is not; the P3 discs measure 0.966–0.999")
    conf = min(0.99, 0.4 * fill + 0.35 * cov
               + 0.25 * (1.0 - min(1.0, resid / (0.06 * r))))
    inv = 1.0 / f
    ccx = (cx + x0) * inv
    ccy = (cy + y0) * inv
    rr = r * inv
    th2 = np.linspace(0, 2 * np.pi, 90, endpoint=False)
    contour = np.stack([ccx + rr * np.cos(th2),
                        ccy + rr * np.sin(th2)], axis=1)
    return {'method': 'baseline-disc', 'area_px': float(np.pi * rr * rr),
            'diam_px': float(2 * rr), 'cx': ccx, 'cy': ccy,
            'circ': circ, 'solidity': round(fill, 3), 'contour': contour,
            'conf': round(conf, 3), 'wrinkle': None, 'spread_pct': 0.0,
            'arc_cov': round(cov, 2), 'fit_resid_px': round(resid * inv, 1),
            'n_edge': int(len(pin)), 'paper_lum': round(paper, 1)}, None


def _is_manual_cal(baseline_ref):
    """Is this anchor one a HUMAN put their name to — and therefore the
    one that overrides every automatic reference at Save?

    Two methods qualify (`#215` verify mode, 2026-08-06 evening):
    'manual-calibration' (the operator measured the disc themselves) and
    'auto-verified' (the operator was shown the automatic fit and its
    quality numbers and approved it). Both are decisions; the difference
    between them is provenance, not authority, and it is recorded in
    `method` so an audit can tell them apart.

    Why 'auto-verified' has to be in here rather than left to fall through
    to the automatic branch below: an approved fit must keep BEATING an
    accepted result on the baseline row, exactly as a hand measurement
    does. Falling through would let a baseline-row detection silently
    outrank the anchor the operator actually signed off — the same class of
    bug the 2026-08-05 audit found for hand calibrations."""
    return bool(baseline_ref
                and baseline_ref.get('method') in ANCHOR_METHODS
                and baseline_ref.get('diam_px'))


def mm_per_px(results, rows, settings, baseline_ref=None):
    """Scale from the DEA's nominal resting diameter.

    Preference order (audit 2026-07-25, revised 2026-08-05, extended
    2026-08-06): an anchor a HUMAN signed off — `baseline_ref` whose method
    is in `ANCHOR_METHODS`, i.e. 'manual-calibration' (they measured it) or
    'auto-verified' (they were shown the automatic fit with its quality
    numbers and approved it, the verify mode) — beats everything. The operator
    decided; the old order silently ignored those clicks whenever the
    baseline row had an accepted result (the status line claimed otherwise;
    flagged major).
    Then: an accepted result on the row tagged 'baseline' → an automatic
    `baseline_ref` (baseline_disc() detection) → the first accepted result
    (last resort — its outline is an ACTIVATED region, so the scale may be
    off; callers should record the source). None when nothing is usable."""
    ref = baseline_ref if _is_manual_cal(baseline_ref) else None
    if ref is None:
        for i, row in enumerate(rows):
            if results.get(i) and (row.get('tag') == 'baseline'):
                ref = results[i]
                break
    if ref is None and baseline_ref and baseline_ref.get('diam_px'):
        ref = baseline_ref
    if ref is None:
        for i in sorted(results):
            if results[i]:
                ref = results[i]
                break
    if not ref or not ref.get('diam_px'):
        return None
    return float(settings['diam_mm']) / float(ref['diam_px'])


def scale_source(results, rows, baseline_ref=None):
    """Human-readable description of which reference mm_per_px would use."""
    if _is_manual_cal(baseline_ref):
        # the METHOD, not a fixed word: 'auto-verified' and
        # 'manual-calibration' both win here and an audit has to be able
        # to tell them apart from the status line alone
        return (f"{baseline_ref.get('method')} "
                f"({baseline_ref['diam_px']:.0f} px)")
    for i, row in enumerate(rows):
        if results.get(i) and (row.get('tag') == 'baseline'):
            return f"baseline row (idx {i})"
    if baseline_ref and baseline_ref.get('diam_px'):
        return baseline_ref.get('method', 'baseline-disc') + \
            f" ({baseline_ref['diam_px']:.0f} px)"
    for i in sorted(results):
        if results[i]:
            return (f"FIRST ACCEPTED frame (idx {i}) — activated region, "
                    f"scale may be off")
    return "none"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # a hand-damaged cell like '1e999' parses to inf and used to abort
    # the whole save with a bare ZeroDivisionError (review 2026-08-05)
    return f if np.isfinite(f) else None


def apply_results(rows, results, scale, flags, annos=None):
    """Fill the active_area_* / wrinkle_idx / notes columns in `rows`
    (in place). `annos` are informational notes (e.g. wrinkle-mode) appended
    alongside the breakdown flags but never treated as breakdown.

    Reprocess-safe (audit 2026-07-25): rejected rows blank EVERY derived
    column (a previous pass's mm²/wrinkle used to survive next to the
    'rejected' note), accepted rows blank mm²/diam when no scale is
    available (instead of keeping stale ones), and flag/anno notes are not
    appended twice on repeated saves.

    ONE SCALE PER SAVE (audit 2026-08-05, critical): a row NOT in
    `results` (still in the review queue at Save) keeps its previous
    pass's px measurement, but its mm² / diam are RE-DERIVED from the
    preserved active_area_px at THIS save's scale. Under the manual
    scale gate the anchor is a fresh pair of clicks per session, so a
    partial re-save used to write two different absolute scales into one
    active_area_mm2 column with nothing marking the boundary — a 56.1%
    artificial area step on the real-data repro, larger than the 35%
    collapse threshold. A stale mm² whose px is missing (pre-2026-07-25
    bug era) is blanked rather than left on a foreign scale."""
    annos = annos or {}
    for i, row in enumerate(rows):
        r = results.get(i)
        if r:
            row['active_area_px'] = f"{r['area_px']:.0f}"
            if scale:
                row['active_area_mm2'] = f"{r['area_px'] * scale * scale:.3f}"
                row['active_diam_mm'] = f"{r['diam_px'] * scale:.3f}"
            else:
                row['active_area_mm2'] = ''
                row['active_diam_mm'] = ''
            w = r.get('wrinkle')
            if w is not None:
                row['wrinkle_idx'] = f"{float(w):.2f}"
            elif 'wrinkle_idx' in row:
                # this measurement carries no wrinkle index (e.g.
                # 'resting'): a previous pass's value must not survive
                # next to the new area (audit 2026-08-05)
                row['wrinkle_idx'] = ''
            note = f"edge:{r['method']} conf {r['conf']:.2f}"
            if r.get('chosen_by'):
                note += f" ({r['chosen_by']})"
        elif i in results:                 # explicitly reviewed + rejected
            for col in ('active_area_px', 'active_area_mm2',
                        'active_diam_mm', 'wrinkle_idx'):
                if col in row or col == 'active_area_px':
                    row[col] = ''
            note = 'rejected (no reliable edge)'
        else:
            note = row.get('notes') or ''
            if scale:
                px = _num(row.get('active_area_px'))
                old_mm2 = _num(row.get('active_area_mm2'))
                old_diam = _num(row.get('active_diam_mm'))
                if px and px > 0:
                    row['active_area_mm2'] = f"{px * scale * scale:.3f}"
                    if old_mm2 and old_mm2 > 0 and old_diam:
                        # preserve the row's own diam definition exactly:
                        # rescale by the ratio of the two anchors
                        old_scale = (old_mm2 / px) ** 0.5
                        row['active_diam_mm'] = \
                            f"{old_diam * scale / old_scale:.3f}"
                    elif 'active_diam_mm' in row:
                        # every detector reports the equivalent diameter
                        row['active_diam_mm'] = \
                            f"{2.0 * (px / np.pi) ** 0.5 * scale:.3f}"
                elif old_mm2 or old_diam:
                    # no px to re-derive from: refuse to keep an absolute
                    # number on an unknowable previous anchor
                    for col in ('active_area_mm2', 'active_diam_mm'):
                        if col in row:
                            row[col] = ''
        for extra in (flags.get(i), annos.get(i)):
            if extra and extra not in note:
                note = (note + '; ' if note else '') + extra
        row['notes'] = note
    return rows


def _strip_brand(note):
    """Remove breakdown branding tokens ('post-breakdown', 'breakdown? …')
    from a notes string — the un-branding half of the marking pass."""
    parts = [p for p in (note or '').split('; ')
             if p and p != 'post-breakdown'
             and not p.startswith('breakdown?')]
    return '; '.join(parts)


def plan_breakdown_marks(run, flags):
    """Bring every row's branding in line with the CURRENT `flags`
    (mutates notes + frame_file in the rows) and return the file-rename
    plan [(src, dst)] for apply_rename_plan.

    Split from the disk half so save() can commit data.csv BEFORE any
    frame is renamed — a failed CSV write used to leave renamed frames,
    a stale CSV and a dialog promising a .bak that was never made
    (audit 2026-08-05).

    Symmetric since 2026-08-05. Rows at/after the first flag are branded
    (rename + frame_file + 'post-breakdown' note) as before; rows
    OUTSIDE that zone — including every row when `flags` is empty — are
    UN-branded: the '_BREAKDOWN' suffix comes back off the file and the
    CSV link, and stale 'post-breakdown' / 'breakdown?' note tokens are
    stripped even on rows the operator never re-reviewed. The
    current-confirmed semantics exist precisely to RETRACT flags the old
    heuristics raised (P3_5: 35 healthy frames), and a retraction must
    reach the artifacts the previous save created.

    frame_file is only ever rewritten to a name that exists on disk or
    is about to via the plan — never to a name nothing answers to (the
    unconditional rewrite made a missing frame's row permanently
    unmeasurable; live on SLDEA_20260723_155425 row 48). The
    CSV-branded/disk-unbranded state heals in whichever direction the
    flags say, so no state is unrepairable.

    Takes only the CONFIRMED dict from breakdown_flags — advisory rows
    (transient discharge, uncorroborated collapse) must never seed the
    renaming or the post-breakdown branding (P3_5, 2026-08-04)."""
    start = min(flags) if flags else None
    plan = []
    frames_dir = run['frames_dir']
    for i, row in enumerate(run['rows']):
        name = (row.get('frame_file') or '').strip()
        if start is not None and i >= start:
            if i not in flags:
                note = row.get('notes') or ''
                if 'post-breakdown' not in note:
                    row['notes'] = (note + '; ' if note
                                    else '') + 'post-breakdown'
            if not name:
                continue
            if '_BREAKDOWN' in name:
                # CSV already branded: heal a disk that is not (this
                # state used to short-circuit before any disk check and
                # was permanently unrepairable)
                if not os.path.exists(os.path.join(frames_dir, name)):
                    plain = name.replace('_BREAKDOWN', '')
                    if os.path.exists(os.path.join(frames_dir, plain)):
                        plan.append((os.path.join(frames_dir, plain),
                                     os.path.join(frames_dir, name)))
                continue
            base, ext = os.path.splitext(name)
            new = base + '_BREAKDOWN' + ext
            src = os.path.join(frames_dir, name)
            dst = os.path.join(frames_dir, new)
            # dst-exists first: when BOTH twins exist (a restore raced a
            # brand), renaming would CLOBBER the branded file's bytes —
            # 'renamed, never deleted' means prefer the link fix and
            # leave both files intact (review 2026-08-05)
            if os.path.exists(dst):
                row['frame_file'] = new    # disk already renamed: heal
            elif os.path.exists(src):
                plan.append((src, dst))
                row['frame_file'] = new
        else:
            note = row.get('notes') or ''
            stripped = _strip_brand(note)
            if stripped != note:
                row['notes'] = stripped
            if not name:
                continue
            if '_BREAKDOWN' not in name:
                # heal the REVERSE orphan too: CSV plain, disk holding
                # only the branded twin (a rename that outlived a failed
                # or interrupted un-brand save). Without this the plain
                # name dangles forever and the row reads UNREADABLE on
                # every later pass — 'self-heals in either direction'
                # was only true for the branded zone (review 2026-08-05).
                base, ext = os.path.splitext(name)
                branded = os.path.join(frames_dir,
                                       base + '_BREAKDOWN' + ext)
                if (not os.path.exists(os.path.join(frames_dir, name))
                        and os.path.exists(branded)):
                    plan.append((branded,
                                 os.path.join(frames_dir, name)))
                continue
            plain = name.replace('_BREAKDOWN', '')
            src = os.path.join(frames_dir, name)
            dst = os.path.join(frames_dir, plain)
            if os.path.exists(dst):
                row['frame_file'] = plain  # disk already plain: heal
            elif os.path.exists(src):
                plan.append((src, dst))
                row['frame_file'] = plain
    return plan


def apply_rename_plan(plan):
    """Execute the renames from plan_breakdown_marks. Per-file tolerant:
    one unrenamable frame (share hiccup, Explorer lock) must not abandon
    the rest — failures are collected, not raised, and the next Save's
    plan self-heals whatever stayed out of line.
    -> (n_renamed, [error strings])."""
    renamed, errors = 0, []
    for src, dst in plan:
        try:
            os.replace(src, dst)
            renamed += 1
        except OSError as e:
            errors.append(f"{os.path.basename(src)}: {e}")
    return renamed, errors


def mark_breakdown_files(run, flags):
    """plan_breakdown_marks + apply_rename_plan in one call (the
    pre-2026-08-05 API). Files are renamed, never deleted (they stay
    useful, e.g. as ML training data). Idempotent. Returns the number of
    files renamed (in either direction); raises OSError when a rename
    fails."""
    renamed, errors = apply_rename_plan(plan_breakdown_marks(run, flags))
    if errors:
        raise OSError('; '.join(errors))
    return renamed


def write_back(rundir, run):
    """Rewrite data.csv (backup first) with the filled columns.

    Atomic: writes data.csv.tmp then os.replace — a NAS hiccup mid-write
    used to leave a truncated data.csv with the frames already renamed
    (audit 2026-07-25). Writes back to the file the run was READ from, so
    a renamed CSV is updated in place rather than sprouting a second one.

    Column-safe (audit 2026-08-05): any row key missing from the header
    (wrinkle_idx on a 07-23-era 14-column CSV) is inserted before
    'notes' instead of letting DictWriter raise mid-save — that
    ValueError used to land AFTER the frame renames, leaving the run
    half-saved. Written UTF-8, matching sldea_plot's decode."""
    csv_path = (run.get('csv_path') or run_csv(rundir)
                or os.path.join(rundir, 'data.csv'))
    shutil.copy2(csv_path, csv_path + '.bak')
    cols = run['columns']
    extra = []
    for row in run['rows']:
        for k in row:
            if k is not None and k not in cols and k not in extra:
                extra.append(k)
    if extra:
        at = cols.index('notes') if 'notes' in cols else len(cols)
        cols[at:at] = extra
    tmp = csv_path + '.tmp'
    # utf-8-sig, not bare utf-8: the bench opens data.csv in Excel,
    # which mojibakes BOM-less UTF-8 punctuation; both in-suite readers
    # (load_run, sldea_plot) already decode utf-8-sig (review 2026-08-05)
    with open(tmp, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in run['rows']:
            w.writerow(row)
    os.replace(tmp, csv_path)
    return csv_path
