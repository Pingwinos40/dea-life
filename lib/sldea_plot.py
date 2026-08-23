#!/usr/bin/env python3
"""Cross-run plotting for SLDEA run data (issue #199).

Usage:
    python sldea_plot.py RUN [RUN2 ...] [--mode area|current|power]
                         [--vs-area] [--prepost] [--mean] [--no-bands]
                         [--no-breakdown] [--out DIR] [--stem NAME]
                         [--title TEXT] [--allow-suspect-scale]
                         [--logx] [--logy] [--no-marker-key]
                         [--title-first TEXT] [--title-second TEXT]
                         [--subplots both|first|second] [--cadence-guard]
    python sldea_plot.py --from-spec FILE.figspec.json [flags to override]
    python sldea_plot.py --gui [RUN ...]        # window (see below)
    python sldea_plot.py --selftest [OUT.png]

Each RUN is a run directory, a parent full of runs (newest wins) or a bench
shortcut ('1'/'2'/'3') -- the same resolver as the tuner and diagnostic.
Writes a 300 dpi PNG, the underlying tidy per-snapshot CSV and the figspec
sidecar (below), all named after --stem (default 'sldea_plot_<mode>'), into
--out (default the current directory). Custom --stem names written inside
the repo checkout are NOT gitignored -- keep the default 'sldea_plot'
prefix there, or point --out outside the checkout.

Modes:
    area      (default) two panels: active area (mm^2) vs nominal kV, and
              the same curves normalized to expansion A/A0 (A0 = the run's
              baseline area). Needs reviewed areas; raw runs are skipped
              with a warning.
    current   measured_uA vs nominal kV, one point per snapshot. Works on
              raw runs; the run-median current baseline is drawn dotted.
    power     |nominal_kV x (measured_uA - run median)| (mW) vs nominal
              kV -- offset-corrected, mirroring breakdown_flags' median
              baseline (audit 2026-08-05: the raw product was ~100%
              instrument zero x kV on the -16 uA-idle era and rank-
              inverted real dissipation). Runs with < 5 parseable uA
              rows keep the raw product, flagged in the caption.
    --vs-area swaps the x axis to active area (current/power modes only;
              needs reviewed areas like area mode).

Rendering:
    - Default lines are the per-level MEAN of the pre/post snapshot pair;
      --prepost draws post-ramp solid and pre-ramp dashed instead, and
      --mean adds the mean line back on top of those.
    - Uncertainty bands: +-2% on machine-measured stretches, +-1% on
      hand-traced ones (--no-bands hides them). A level mixing traced and
      machine snapshots keeps the machine +-2% band. Open markers = the
      level (with --prepost: the snapshot) includes a hand-traced boundary.
    - The open/closed marker meaning is a KEY on the figure (`#267`): two
      proxy handles in their own compact legend, lower right of the area
      panel, so the run legend upper left keeps its corner and does not
      grow by two fixed rows on every plot. On by default;
      --no-marker-key hides it. Area mode only -- current/power draw one
      plain dot per snapshot with no open/closed meaning, and a key there
      would claim a distinction the figure does not make.
    - Colors are the Paul Tol bright family (colorblind-safe, house
      convention), assigned to runs in argument order.
    - Areas predating the 2026-07-28 scale fix (2.3-2.7x blob bug) are
      excluded from area axes unless the run's baseline matches the
      nominal disc; --allow-suspect-scale overrides. In current/power
      modes such runs still plot (currents are unaffected) but their area
      columns are blanked in the tidy CSV so eras cannot be mixed.

Panels (`#269` titles, `#270` selection):
    The panels a mode actually draws, in the order the flags name them:

        area            first  = active area vs nominal kV
                        second = the same curves as A/A0
        current, power  first  = the single per-snapshot panel
                        (there is no second panel in these modes)

    --title-first / --title-second replace that panel's heading; blank or
    absent keeps the built-in default, so a figure only carries a hand
    written title where somebody wrote one. --title predates these and
    has always meant the FIRST panel, so it still does: --title-first
    wins over it, and both lose to nothing (the default). --title-second
    in current/power mode is accepted and does nothing -- there is no
    second panel to title.

    --subplots picks which of them render: 'both' (default), 'first' or
    'second'. A single chosen panel is created as the figure's ONLY axes
    so it fills the canvas -- not a two-column grid with one column
    blanked, which would squeeze the survivor into half a figure beside
    white space -- and it keeps the run legend and the marker key.
    'second' outside area mode is refused (there is no second panel to
    draw); 'first' there is a no-op naming the only panel. The tidy CSV
    does NOT shrink with the selection: it is the per-snapshot evidence
    for the numbers, not a description of the layout, and the two panels
    are two views of the same areas.

Cadence guard (`#264`, OPT-IN):
    --cadence-guard asks how often a run measured CURRENT and, where that
    is slower than 1 s, draws its breakdown X hollow and states the
    spacing in the caption. The mark stays on the figure: the event is
    just as current-confirmed, it is its ONSET that is an interval rather
    than a point, and suppressing a real event because the camera was
    slow would be the P3_5 mistake pointing the other way.

    Cadence comes from telemetry.csv beside data.csv (present = the ~2 Hz
    monitor log ran, so current was sampled continuously), else from the
    MEDIAN gap between snapshot timestamps. Threshold 1 s, because that is
    the telemetry logger's slowest guaranteed kV/uA period; an unmeasurable
    cadence is never treated as coarse.

    OFF by default, deliberately, and this is a decision for the bench to
    make rather than a plotting preference. No run in the 2026-08-04
    corpus carries telemetry.csv and every one of the thirteen samples
    current between 4.6 s and 32.5 s (median), so an automatic guard would
    restyle the breakdown marks on every figure the suite has ever made.
    That is a change to how a current-confirmed event is PRESENTED, which
    under CLAUDE.md is measurement-chain behaviour and belongs in a dated
    SLDEA_HANDOFF.md decision -- not in a rendering default. Turn it on
    per figure until that decision exists.

Figspec sidecar (`#273`):
    Every export writes a THIRD file beside the PNG and the CSV:
    <stem>.figspec.json, carrying the exact options, the run directories
    that were plotted (resolved and absolute -- a bench shortcut means a
    different run tomorrow) and two versions: the app's, and the spec
    FORMAT's. A PNG travels into slide decks and papers; the command line
    that made it does not, and six months later 'which runs is this?' has
    to be answerable from the file itself.

    --from-spec FILE re-renders from one. Precedence is one rule applied
    to every option: an explicitly given command-line flag beats the
    spec's value, which beats the built-in default. Positional RUN
    arguments REPLACE the spec's run list rather than adding to it (the
    common case is 'this figure, other runs'). --out is never taken from
    a spec -- a spec describes the figure, not the folder somebody filed
    it in -- and --stem comes from the spec unless you give one.

    The one asymmetry: boolean flags are one-way switches. --prepost can
    turn a spec's false ON, --no-bands can turn a spec's true OFF, but
    there is no --bands to turn a spec's false back on. Edit the spec; it
    is JSON, and that is half the point.

    A spec is validated, not trusted: it is a file a human can edit, so
    its options go back through make_opts and an illegal combination is
    refused with the same wording as on the command line. A spec_version
    from a newer build is refused rather than half-understood.

Log scales (`#263`):
    --logx / --logy put the x / y axis on a logarithmic scale. The axis
    kind is chosen from the DATA, per axis and per panel:

      all plotted values > 0      -> plain log10
      any value <= 0              -> symlog, linear inside +-linthresh
                                     and log outside, where linthresh is
                                     the smallest nonzero |value| rounded
                                     DOWN to a power of ten

    Symlog rather than clipping is deliberate. This suite's currents are
    NEGATIVE on the whole 07-29 era (the I_Out offset idles at ~-16 uA)
    and every run's x axis starts at the 0 kV baseline row, so a plain log
    scale would drop the entire current trace and the resting tier -- and
    dropping rows to make an axis work is the one thing this tool does not
    do (see the P3_5 lesson below). Symlog keeps every point, keeps the
    sign, and the caption names the scale and the linthresh so a reader
    cannot mistake the linear-near-zero region for log.

    An axis with no finite values, or one whose values are all exactly
    zero, is left LINEAR and says so in the caption rather than raising.

Breakdown marks (the P3_5 lesson, 2026-08-05 semantics):
    X marks come from RECOMPUTING the current-based detector
    (sldea_edge.breakdown_flags) on the saved CSV -- never from the saved
    *_BREAKDOWN renames / 'breakdown?' / 'post-breakdown' notes, which can
    predate the current semantics: the old area-jump heuristic once branded
    35 healthy frames of P3_5 while the current stayed flat. Saved brands
    the recompute does not explain (branded rows BEFORE the first confirmed
    row, or any brand when nothing confirms) get a stale-brand warning. A
    confirmed row without a reviewed area is anchored to its kV as a dashed
    vertical instead of being silently dropped. Rows are NEVER dropped or
    restyled because of breakdown annotations -- 'post-breakdown' rows plot
    exactly like any other row.

Window (`#223`):
    --gui opens the point-and-click front end (sldea_plot_gui.py, also
    launchable on its own and from the app's SLDEA tab). It is a FRONT END
    to the functions below, not a second implementation: it picks runs with
    prepare_runs, draws with draw() and writes with export(), so a figure
    made in the window and the same figure made here are the same figure.
    Any RUN arguments and flags given alongside --gui preselect the window.
    The headless paths are unchanged -- batch scripting and --selftest are
    real usage and stay first-class.
"""
import csv
import datetime
import io
import math
import os
import re
import sys

import sldea_edge as se
import sldea_profile as sprof

# Paul Tol bright (house palette for plots -- CLAUDE.md). Canonical order:
# adjacent pairs keep CVD deltaE >= 12 (validated 2026-08-05). Cyan/yellow/
# grey sit light on white surfaces, so marks carry a darker edge and every
# figure ships with its tidy CSV as the readable fallback.
TOL_BRIGHT = ['#4477AA', '#66CCEE', '#228833', '#CCBB44',
              '#EE6677', '#AA3377', '#BBBBBB']

MODES = ('area', 'current', 'power')

# which panel(s) a figure renders (`#270`). 'first'/'second' name the same
# panels --title-first/--title-second do, so one vocabulary covers both.
SUBPLOTS = ('both', 'first', 'second')

# Figure geometry lives here rather than at each call site: the CLI's PNG,
# the window's Export and the window's on-screen canvas all size from this
# one table, so an exported figure cannot silently differ in shape from the
# command-line one (`#223`).
FIGSIZE = {'area': (12.6, 5.4), 'current': (9, 5.4), 'power': (9, 5.4)}

MACHINE_BAND_PCT = 2.0   # auto-accepted (half-height) stretches
TRACED_BAND_PCT = 1.0    # hand-traced (outer-toe) stretches
SCALE_FIX_DATE = '2026-07-28'   # active_area_mm2 before this may carry the
                                # 2.3-2.7x blob-scale bug -- never mix eras

_NOTE_RE = re.compile(r'edge:([A-Za-z_-]+) conf ([0-9.]+)(\s*\(user\))?')


def _f(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _median(vals):
    """Same even-count median as sldea_edge.breakdown_flags, so the drawn
    baseline matches the value printed in the detector's reason text."""
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_rows(rundir):
    """The run CSV as parsed per-snapshot dicts (nothing filtered out).

    Reads through se.run_csv so renamed data1.csv runs still resolve, but
    opens utf-8-sig itself: the bench writes UTF-8 and load_run's default
    decode mojibakes the notes glyphs on Windows consoles."""
    path = se.run_csv(rundir)
    if path is None:
        raise FileNotFoundError(f"no run CSV in {rundir}")
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as f:
        raw = list(csv.DictReader(f))
    rows = []
    for i, r in enumerate(raw):
        notes = (r.get('notes') or '').strip()
        m = _NOTE_RE.search(notes)
        tag = (r.get('tag') or '').strip()
        # prefix match keeps the 07-23 era ('post'/'pre') alongside the
        # current tags ('post-ramp'/'pre-ramp'), same as Edge Review's plot
        phase = next((p for p in ('baseline', 'post', 'pre')
                      if tag.startswith(p)), '')
        rows.append({
            'index': i,
            'snapshot': (r.get('snapshot') or '').strip(),
            'tag': tag, 'phase': phase,
            'kv': _f(r.get('nominal_kV')),
            'ua': _f(r.get('measured_uA')),
            'area_mm2': _f(r.get('active_area_mm2')),
            'area_px': _f(r.get('active_area_px')),
            'timestamp': (r.get('timestamp') or '').strip(),
            'frame_file': (r.get('frame_file') or '').strip(),
            'method': m.group(1) if m else '',
            'conf': float(m.group(2)) if m else None,
            'user': bool(m and m.group(3)),
            'traced': bool(m and m.group(1) == 'manual-trace'),
            'notes': notes,
            'raw': r,
        })
    return rows


def load_run(arg, warn):
    """-> run dict (name, rows, A0, flags, advisories, saved-brand rows) or
    None when the argument does not resolve. `warn` collects messages."""
    rundir = se.resolve_run(arg)
    if not rundir:
        warn(f"not a run directory: {arg}")
        return None
    rows = load_rows(rundir)
    settings = se.load_settings(rundir)

    # breakdown: ALWAYS recomputed from the saved current trace; areas go
    # in so the collapse-corroboration branch behaves exactly like a save.
    # ONE unit for the whole run: a per-row px-else-mm2 fallback can hand
    # the ratio-based collapse test a px value next to an mm2 value
    # (~1400x apart) and confirm a 100% "collapse" on a healthy device --
    # stale mm2-only rows exist in the wild from the pre-2026-07-25
    # rejected-row bug. The GUI caller passes pure px; matched here.
    area_key = ('area_px' if any(r['area_px'] for r in rows)
                else 'area_mm2')
    areas = {r['index']: r[area_key] for r in rows if r[area_key]}
    flags, advis = se.breakdown_flags([r['raw'] for r in rows],
                                      areas, settings)
    saved_brand = [r['index'] for r in rows
                   if '_BREAKDOWN' in r['frame_file']
                   or 'breakdown?' in r['notes']
                   or 'post-breakdown' in r['notes']]
    name = os.path.basename(os.path.abspath(rundir))
    # A save brands every row at/after the FIRST confirmed flag, so brands
    # from min(flags) on are explained; earlier ones -- or all of them when
    # nothing confirms -- are stale. Per-row, so a run carrying both a
    # stale brand AND a real later event still warns (review 2026-08-05).
    first = min(flags) if flags else None
    stale = [i for i in saved_brand if first is None or i < first]
    if stale:
        warn(f"{name}: saved breakdown branding on {len(stale)} of "
             f"{len(saved_brand)} branded row(s) is NOT current-confirmed "
             f"under the 2026-08-05 semantics -- stale brand, plotting "
             f"normally with no X mark")
    unbranded = sorted(set(flags) - set(saved_brand))
    if unbranded:
        warn(f"{name}: current-confirmed breakdown detected at row(s) "
             f"{unbranded} with no saved branding -- unreviewed")

    base_areas = [r['area_mm2'] for r in rows
                  if r['phase'] == 'baseline' and r['area_mm2']]
    a0 = _median(base_areas) if base_areas else None
    cadence_s, cadence_src = run_cadence(rundir, rows)
    return {'dir': rundir, 'name': name, 'rows': rows, 'a0': a0,
            # how often this run measured current (`#264`) -- what the
            # breakdown mark's position is actually resolved to
            'cadence_s': cadence_s, 'cadence_src': cadence_src,
            'settings': settings, 'flags': flags, 'advis': advis,
            # the px→mm anchor Edge Review recorded at Save (2026-08-05)
            # — cross-run absolute mm² inherits its provenance
            'anchor': se.load_scale_anchor(rundir),
            'saved_brand': saved_brand}


# How often the run measured CURRENT, above which a breakdown mark's
# position in voltage/time is an interval rather than a point (`#264`).
# 1 s because that is the telemetry logger's slowest guaranteed kV/uA
# period (sldea_profile.TELEMETRY_KV_MIN_PERIOD_S): at or under it the
# detector saw the event as it happened, above it the onset can be
# anywhere in the gap before the flagged snapshot.
CADENCE_COARSE_S = 1.0


def run_cadence(rundir, rows):
    """-> (seconds between current samples, where that came from).

    telemetry.csv beside data.csv means the ~2 Hz monitor log ran, so the
    current was sampled continuously; its presence is the answer and the
    file is not parsed (a truncated or aborted log still means the run was
    monitored). Otherwise the only current the run has is one reading per
    snapshot, and the cadence is the MEDIAN gap between snapshot
    timestamps -- median, not mean, because ramp settling makes the
    distribution lumpy (the P3 corpus runs 7 s to 57 s with a 32.5 s
    median).

    (None, 'unknown') when fewer than two timestamps parse: that is 'no
    answer', and no caller may read it as 'fine'."""
    tel = os.path.join(rundir, sprof.TELEMETRY_FILENAME)
    if os.path.exists(tel):
        return (1.0 / sprof.TELEMETRY_MAX_HZ, sprof.TELEMETRY_FILENAME)
    stamps = []
    for r in rows:
        text = r.get('timestamp') or ''
        try:
            stamps.append(datetime.datetime.fromisoformat(text))
        except (TypeError, ValueError):
            continue
    gaps = [(b - a).total_seconds()
            for a, b in zip(stamps, stamps[1:])
            if (b - a).total_seconds() > 0]
    if not gaps:
        return (None, 'unknown')
    return (_median(gaps), 'snapshot spacing')


def coarse_cadence(run, opts):
    """True when the guard is on AND this run sampled current more slowly
    than CADENCE_COARSE_S. An unknown cadence is NOT coarse: the guard
    annotates what it can measure and stays quiet about what it cannot."""
    if not opts.get('cadence_guard'):
        return False
    secs = run.get('cadence_s')
    return secs is not None and secs > CADENCE_COARSE_S


def _cadence_note(run):
    """'<run> every 6.1 s (snapshot spacing)' -- deliberately compact: it
    goes on ONE caption line, and the caption has a figure's width, not a
    console's. The console/window warning wraps this in the full
    sentence."""
    return (f"{run['name']} every {run['cadence_s']:.1f} s "
            f"({run.get('cadence_src', '?')})")


def suspect_old_scale(run):
    """True when the run's areas may carry the pre-2026-07-28 scale bug:
    old timestamps AND a baseline area far off the mask's nominal disc --
    or old timestamps with areas but NO baseline to verify against (fail
    closed: an unverifiable era must not mix in). 155425 is old but
    re-reviewed post-fix; its baseline is exactly pi*(16/2)^2, so it
    passes."""
    dates = [r['timestamp'][:10] for r in run['rows'] if r['timestamp']]
    if not dates or min(dates) >= SCALE_FIX_DATE:
        return False
    if run['a0'] is None:
        return any(r['area_mm2'] is not None for r in run['rows'])
    diam = float(run['settings'].get('diam_mm', 16.0) or 16.0)
    nominal = math.pi * (diam / 2.0) ** 2
    return abs(run['a0'] - nominal) / nominal > 0.05


# ---------------------------------------------------------------------------
# per-level aggregation (the pre/post pair collapses to one level entry)
# ---------------------------------------------------------------------------

def levels(run, value=lambda r: r['area_mm2']):
    """-> sorted [{kv, mean, post, pre, traced, all_traced, mixed,
    traced_post, traced_pre, confirmed}] over rows with a kV and a value.
    Baseline-tagged rows join their kV level like any snapshot (the
    resting tier). `traced` ORs the pair (drives the open marker on
    --prepost snapshots); `all_traced` ANDs it (drives the band width
    and, since 2026-08-05, the mean marker's fill).

    NEVER average across edge conventions (audit 2026-08-05): a
    hand-traced (outer-toe) and a machine (half-height) area differ by a
    documented +5.2-5.7% of DEFINITION, and the blended number belongs
    to neither — the old mean did it on 11 levels of the real batch and
    the caption labeled the result 'outer toe, ±1%'. A `mixed` level's
    mean now uses the machine member(s) only (the campaign's primary
    convention), plots FILLED, and keeps the machine band."""
    by_kv = {}
    for r in run['rows']:
        v = value(r)
        if r['kv'] is None or v is None:
            continue
        lv = by_kv.setdefault(round(r['kv'], 3), {
            'kv': r['kv'], 'vals': [], 'vals_machine': [],
            'post': None, 'pre': None,
            'traced': False, 'all_traced': True, 'mixed': False,
            'traced_post': False, 'traced_pre': False,
            'confirmed': False})
        lv['vals'].append(v)
        if not r['traced']:
            lv['vals_machine'].append(v)
        lv['traced'] = lv['traced'] or r['traced']
        lv['all_traced'] = lv['all_traced'] and r['traced']
        lv['confirmed'] = lv['confirmed'] or r['index'] in run['flags']
        if r['phase'] == 'post':
            lv['post'] = v
            lv['traced_post'] = r['traced']
        elif r['phase'] == 'pre':
            lv['pre'] = v
            lv['traced_pre'] = r['traced']
    out = []
    for k in sorted(by_kv):
        lv = by_kv[k]
        lv['mixed'] = lv['traced'] and not lv['all_traced']
        use = lv['vals_machine'] if lv['mixed'] else lv['vals']
        lv['mean'] = sum(use) / len(use)
        out.append(lv)
    return out


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------

def _style_axes(ax, xlabel, ylabel):
    ax.grid(alpha=0.3)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)


def log_scale_for(values):
    """-> ('log', None) | ('symlog', linthresh) | None, from the DATA.

    The `#263` policy in one function so both figures decide identically.
    None means 'leave this axis linear' -- nothing finite to scale, or an
    all-zero axis, neither of which a log scale can represent. See the
    module docstring for why nonpositive data becomes symlog rather than
    being clipped away."""
    vals = [v for v in values
            if v is not None and isinstance(v, (int, float))
            and math.isfinite(v)]
    if not vals:
        return None
    if all(v > 0 for v in vals):
        return ('log', None)
    nz = [abs(v) for v in vals if v != 0]
    if not nz:
        return None
    # floor to a decade so the linear window is a readable round number
    # and two runs of the same shape get the same axis
    return ('symlog', max(10.0 ** math.floor(math.log10(min(nz))), 1e-12))


def _apply_scales(ax, opts, xs, ys, notes):
    """Apply --logx/--logy to one axis, appending caption lines to `notes`.

    Values come from the CALLER's record of what it plotted, not from
    ax.get_lines(): axhline/axvline store one of their coordinate pairs in
    axes fractions (0..1), and letting those vote would pick a linthresh
    from a gridline rather than from the data."""
    for axis, on, vals, name in (('x', opts.get('logx'), xs, 'X'),
                                 ('y', opts.get('logy'), ys, 'Y')):
        if not on:
            continue
        pick = log_scale_for(vals)
        if pick is None:
            notes.append(f"{name} axis: log requested but the data has "
                         f"nothing a log scale can show -- left linear.")
            continue
        kind, lin = pick
        if kind == 'log':
            ax.set_xscale('log') if axis == 'x' else ax.set_yscale('log')
            notes.append(f"{name} axis: log10.")
        else:
            if axis == 'x':
                ax.set_xscale('symlog', linthresh=lin)
            else:
                ax.set_yscale('symlog', linthresh=lin)
            notes.append(f"{name} axis: symlog -- linear within ±{lin:g}, "
                         f"log outside (the data carries values ≤ 0; a "
                         f"plain log scale would drop them).")


def _panel_title(opts, which, default):
    """The heading for one panel: the panel's own option, else (first
    panel only) the legacy --title, else the built-in default.

    --title predates per-panel titles and has always meant 'the FIRST
    panel', so it keeps meaning that and --title-first is simply its
    precise name -- a script that says --title today keeps its figure.
    Blank is 'no override', never an empty heading (`#269`)."""
    text = (opts.get('title_' + which) or '').strip()
    if not text and which == 'first':
        text = (opts.get('title') or '').strip()
    return text or default


def _dedupe(items):
    """Order-preserving unique -- the two area panels share an x axis, so
    the same caption line is generated twice."""
    out = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _cadence_caption(notes):
    """The cadence-guard caption line, or '' when no run earned one.

    States the SPACING, not just 'coarse': a reader deciding whether an
    X at 6.4 kV means 'at 6.4 kV' needs the number to judge it, and 32 s
    of ramp is a very different claim from 5 s."""
    notes = _dedupe(notes)
    if not notes:
        return ''
    return (f"\nHollow X = confirmed, sampled slower than "
            f"{CADENCE_COARSE_S:g} s (onset lies before the mark).  "
            f"Sampling: " + '; '.join(notes) + '.')


def _scale_caption(notes):
    """The log-scale caption line, or '' when both axes stayed linear.

    A figure whose axis is not what a reader assumes has to SAY so on the
    figure -- the PNG travels without the command line that made it."""
    notes = _dedupe(notes)
    return ('\n' + '  '.join(notes)) if notes else ''


def _series(ax, xs, ys, traced, color, ls, bands, band_traced=None):
    """One curve: line + per-point open/closed markers + traced-aware band.
    `traced` drives the marker fill; `band_traced` (default: same) drives
    the band width -- the mean line passes the AND-aggregate there so a
    mixed pre/post level keeps the machine +-2% band."""
    if band_traced is None:
        band_traced = traced
    ax.plot(xs, ys, ls, color=color, linewidth=1.8, zorder=3)
    for x, y, tr in zip(xs, ys, traced):
        ax.plot([x], [y], 'o', markersize=4.5, zorder=4, color=color,
                markerfacecolor='white' if tr else color,
                markeredgecolor=color, markeredgewidth=1.2)
    if bands and len(xs) > 1:
        lo = [y * (1 - (TRACED_BAND_PCT if tr else MACHINE_BAND_PCT) / 100)
              for y, tr in zip(ys, band_traced)]
        hi = [y * (1 + (TRACED_BAND_PCT if tr else MACHINE_BAND_PCT) / 100)
              for y, tr in zip(ys, band_traced)]
        ax.fill_between(xs, lo, hi, color=color, alpha=0.14, linewidth=0,
                        zorder=2)


def _cross_marks(ax, pts, color, coarse=False):
    """X at each current-confirmed breakdown point. `pts` = [(x, y)].

    `coarse` draws the X hollow (`#264`): the event is just as confirmed,
    but the run sampled current slowly enough that its onset could be
    anywhere in the interval before this snapshot, so the mark is a
    neighbourhood rather than a point. It is still DRAWN -- suppressing a
    real current-confirmed event because the camera was slow would hide
    evidence, which is the P3_5 mistake pointing the other way."""
    for x, y in pts:
        ax.plot([x], [y], 'X', markersize=9, color=color, zorder=6,
                markerfacecolor='white' if coarse else color,
                markeredgecolor=color if coarse else 'black',
                markeredgewidth=1.4 if coarse else 0.6)


def _legend(ax, run_handles, style_rows):
    """The run legend (+ the style rows this figure earned) -> the Legend.

    Returned rather than dropped: matplotlib keeps ONE ax.legend_, so the
    marker key has to re-add this one as a standalone artist before it
    creates its own or the run legend silently disappears (`#267`)."""
    from matplotlib.lines import Line2D
    handles = list(run_handles)
    for label, kw in style_rows:
        handles.append(Line2D([], [], color='#666666', label=label, **kw))
    return ax.legend(handles=handles, fontsize=8, loc='upper left',
                     framealpha=0.9)


# the two marker fills `_series` draws, as the figure's own key (`#267`).
# Area mode only: current/power draw one plain filled dot per snapshot with
# no open/closed meaning, and a key there would claim a distinction the
# figure does not make.
MARKER_KEY_ROWS = (('hand-traced (outer toe)', 'white'),
                   ('machine (half-height)', '#666666'))


def _marker_key(ax, main_legend):
    """Draw the open/closed marker key as a SECOND compact legend.

    Its own legend in the opposite corner rather than two more rows in the
    run legend: the run list is what a reader scans to tell the curves
    apart, and a fixed key that grows it pushes the runs down the figure
    on every plot. Placed lower right because area curves rise to the
    right of the panel the run legend sits in."""
    from matplotlib.lines import Line2D
    if main_legend is not None:
        ax.add_artist(main_legend)
    proxies = [Line2D([], [], linestyle='', marker='o', markersize=4.5,
                      color='#666666', markerfacecolor=fill,
                      markeredgecolor='#666666', markeredgewidth=1.2,
                      label=label)
               for label, fill in MARKER_KEY_ROWS]
    return ax.legend(handles=proxies, fontsize=7, loc='lower right',
                     framealpha=0.9, title='marker fill',
                     title_fontsize=7)


def draw_area(fig, axl, axr, runs, opts, warn=lambda m: None):
    """Draw the two-panel area figure into ALREADY-CREATED axes.

    Split out of figure_area for `#223`: the window needs to draw into its
    live Tk canvas without saving a file, and a second drawing routine
    would be a second set of plotting rules to keep in step. Everything
    that decides what a figure LOOKS like lives here; the callers only
    decide where the pixels go.

    EITHER axis may be None (`#270`): the caller creates only the panels
    opts['subplots'] asked for, and the survivor gets the whole canvas."""
    from matplotlib.lines import Line2D

    panels = [ax for ax in (axl, axr) if ax is not None]
    legend_ax = axl if axl is not None else axr
    run_handles = []
    had_x = had_fallback = had_coarse = False
    cadence_notes = []
    # what actually got plotted, per axis -- the log-scale policy reads
    # the DATA and axes-fraction gridlines must not vote (`#263`)
    xs_all, ysl_all, ysr_all = [], [], []
    for run in runs:
        color = run['color']
        lvs = levels(run)
        if not lvs:
            continue
        xs = [l['kv'] for l in lvs]
        if opts['prepost']:
            for key, ls in (('post', '-'), ('pre', '--')):
                pts = [(l['kv'], l[key], l['traced_' + key])
                       for l in lvs if l[key] is not None]
                if pts:
                    px, py, pt = zip(*pts)
                    if axl is not None:
                        _series(axl, px, py, pt, color, ls, opts['bands'])
                    if axr is not None:
                        _series(axr, px, [y / run['a0'] for y in py], pt,
                                color, ls, opts['bands'])
                    xs_all += list(px)
                    ysl_all += list(py)
                    ysr_all += [y / run['a0'] for y in py]
        if opts['mean'] or not opts['prepost']:
            # marker fill follows the CONVENTION of the plotted value:
            # a mixed level's mean uses the machine member(s) only, so
            # it plots filled — the OR-aggregate used to open-mark a
            # blended number as 'outer toe ±1%' (audit 2026-08-05)
            tr = [l['all_traced'] for l in lvs]
            band_tr = [l['all_traced'] for l in lvs]
            ys = [l['mean'] for l in lvs]
            show_bands = opts['bands'] and not opts['prepost']
            if axl is not None:
                _series(axl, xs, ys, tr, color, '-', show_bands, band_tr)
            if axr is not None:
                _series(axr, xs, [y / run['a0'] for y in ys], tr, color,
                        '-', show_bands, band_tr)
            xs_all += list(xs)
            ysl_all += list(ys)
            ysr_all += [y / run['a0'] for y in ys]
            mixed = [l['kv'] for l in lvs if l['mixed']]
            if mixed:
                warn(f"{run['name']}: {len(mixed)} level(s) mix a "
                     f"hand-traced (outer-toe) and a machine "
                     f"(half-height) snapshot "
                     f"({', '.join(f'{k:g}' for k in mixed)} kV) — the "
                     f"mean plots the machine member(s) only; conventions "
                     f"differ +5.2-5.7% area and must not be averaged "
                     f"(see --prepost for both, and the tidy "
                     f"'convention' column)")
        if opts['breakdown']:
            coarse = coarse_cadence(run, opts)
            drawn, unanchored = [], []
            for r in run['rows']:
                if r['index'] not in run['flags'] or r['kv'] is None:
                    continue
                if r['area_mm2'] is not None:
                    drawn.append((r['kv'], r['area_mm2']))
                else:
                    # confirmed but no reviewed area (e.g. frame rejected
                    # in review): anchor the event to its kV rather than
                    # silently dropping it
                    for ax in panels:
                        ax.axvline(r['kv'], color=color, linestyle='--',
                                   linewidth=0.9, alpha=0.55, zorder=1)
                    xs_all.append(r['kv'])
                    unanchored.append(r['index'])
            if axl is not None:
                _cross_marks(axl, drawn, color, coarse)
            if axr is not None:
                _cross_marks(axr, [(x, y / run['a0']) for x, y in drawn],
                             color, coarse)
            xs_all += [x for x, _ in drawn]
            ysl_all += [y for _, y in drawn]
            ysr_all += [y / run['a0'] for _, y in drawn]
            had_x = had_x or bool(drawn)
            had_coarse = had_coarse or (coarse and bool(drawn))
            had_fallback = had_fallback or bool(unanchored)
            if coarse and (drawn or unanchored):
                cadence_notes.append(_cadence_note(run))
                warn(f"{run['name']}: current sampled every "
                     f"{run['cadence_s']:.1f} s "
                     f"({run.get('cadence_src', '?')}), slower than "
                     f"{CADENCE_COARSE_S:g} s -- a breakdown's onset can "
                     f"be anywhere in the interval before the flagged "
                     f"snapshot, so its X is drawn hollow")
            if unanchored:
                warn(f"{run['name']}: confirmed breakdown row(s) "
                     f"{unanchored} have no reviewed area -- drawn as "
                     f"dashed verticals at their kV (see current mode)")
        run_handles.append(Line2D([], [], color=color, label=run['name']))

    scale_notes = []
    if axl is not None:
        _style_axes(axl, 'Nominal voltage (kV)', 'Active area (mm²)')
        _apply_scales(axl, opts, xs_all, ysl_all, scale_notes)
        axl.set_title(_panel_title(opts, 'first',
                                   'Active area vs voltage'),
                      loc='left', fontweight='bold', fontsize=11)
    if axr is not None:
        _style_axes(axr, 'Nominal voltage (kV)', 'Expansion  A / A₀')
        _apply_scales(axr, opts, xs_all, ysr_all, scale_notes)
        a0s = sorted({round(r['a0'], 1) for r in runs})
        a0txt = (f"A₀ = {a0s[0]:g} mm²" if len(a0s) == 1
                 else "per-run A₀")
        axr.set_title(_panel_title(opts, 'second',
                                   f"Normalized to baseline area "
                                   f"({a0txt})"),
                      loc='left', fontweight='bold', fontsize=11)
    style_rows = []
    if opts['prepost']:
        style_rows += [('post-ramp snapshot', {'linestyle': '-'}),
                       ('pre-ramp snapshot', {'linestyle': '--'})]
    if had_x:
        style_rows.append(('breakdown (current-confirmed)',
                           {'linestyle': '', 'marker': 'X'}))
    if had_coarse:
        style_rows.append(('breakdown, coarse current sampling',
                           {'linestyle': '', 'marker': 'X',
                            'markerfacecolor': 'white',
                            'markeredgewidth': 1.4}))
    if had_fallback:
        style_rows.append(('breakdown, no reviewed area',
                           {'linestyle': '--'}))
    main_legend = _legend(legend_ax, run_handles, style_rows)
    if opts.get('marker_key', True):
        _marker_key(legend_ax, main_legend)

    cap = ("Points = per-level pre/post snapshot pair"
           + (" (post solid, pre dashed)" if opts['prepost']
              else " mean") + ".  "
           "Open markers = hand-traced boundary (outer toe, ±1%); "
           "filled = machine half-height convention; a level mixing the "
           "two plots its machine member(s) only (conventions differ "
           "+5.5% area, never averaged)"
           + (", bands ±2% machine / ±1% traced" if opts['bands']
              else "") + ".\n"
           "X = current-confirmed breakdown (recomputed, 2026-08-05 "
           "semantics).  X axis: nominal kV (measured_kV telemetry "
           "incomplete on all runs)."
           + _cadence_caption(cadence_notes)
           + _scale_caption(scale_notes))
    fig.text(0.01, 0.005, cap, fontsize=7, color='#555555')
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    return fig


def area_axes(fig, opts):
    """-> (left, right) axes for the area figure; either is None when
    `#270` switched that panel off.

    A single chosen panel is created as the figure's ONLY axes, so it
    fills the canvas -- not a two-column grid with one column blanked,
    which would leave the survivor squeezed into half a figure next to
    white space. Every caller that makes area axes goes through here so
    the window, the CLI and the PNG cannot disagree about the layout."""
    which = opts.get('subplots', 'both')
    if which == 'first':
        return fig.subplots(), None
    if which == 'second':
        return None, fig.subplots()
    return tuple(fig.subplots(1, 2))


def figure_area(runs, opts, path, warn=lambda m: None):
    """The area figure as a 300 dpi PNG (kept for direct callers/tests).
    Same drawing as the window -- see draw_area."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=FIGSIZE['area'])
    axl, axr = area_axes(fig, opts)
    draw_area(fig, axl, axr, runs, opts, warn)
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return path


def run_ua_median(run):
    """The run's median measured_uA — the same >=5-parseable-rows rule as
    breakdown_flags — or None. The per-era instrument zero this
    estimates is the ONLY current reference the suite trusts; absolute
    µA is documented untrustworthy (07-29 idles at −16 µA)."""
    uas = [r['ua'] for r in run['rows'] if r['ua'] is not None]
    return _median(uas) if len(uas) >= 5 else None


def power_mw(r, med):
    """|kV × (µA − run median)| in mW — the DEVICE's dissipation, not the
    instrument's. The raw |kV × µA| product multiplied the era's zero
    error by the voltage axis: on the P3 campaign it manufactured a
    near-perfect 155-160 mW line at 10 kV on runs whose true deviation
    never left ~5 µA of baseline, and it rank-INVERTED real dissipation
    (audit 2026-08-05). With no median (<5 parseable rows) the raw
    product is kept and the caller must say so."""
    if r['ua'] is None or r['kv'] is None:
        return None
    ua = r['ua'] - med if med is not None else r['ua']
    return abs(r['kv'] * ua)


def draw_signal(fig, ax, runs, opts, warn=lambda m: None):
    """current / power vs kV (or vs area with --vs-area), per snapshot,
    into an ALREADY-CREATED axis. Power is offset-corrected per run (see
    power_mw). The window and the CLI both draw through here (`#223`)."""
    from matplotlib.lines import Line2D

    power = opts['mode'] == 'power'

    def yval(r, med):
        if r['ua'] is None:
            return None
        if power:
            return power_mw(r, med)
        return r['ua']

    def xval(r):
        return r['area_mm2'] if opts['vs_area'] else r['kv']

    run_handles = []
    had_x = had_adv = had_coarse = False
    raw_power = []
    cadence_notes = []
    xs_all, ys_all = [], []          # what got plotted (`#263`)
    for run in runs:
        color = run['color']
        med = run_ua_median(run)
        if power and med is None:
            raw_power.append(run['name'])
            warn(f"{run['name']}: fewer than 5 parseable µA rows — no "
                 f"median baseline, power is the RAW |kV × µA| product "
                 f"(instrument offset included)")
        pts = [(xval(r), yval(r, med), r) for r in run['rows']
               if xval(r) is not None and yval(r, med) is not None]
        if not pts:
            warn(f"{run['name']}: no plottable points in this mode -- "
                 f"omitted from the figure")
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, '-', color=color, linewidth=1.4, alpha=0.85,
                zorder=3)
        ax.plot(xs, ys, 'o', color=color, markersize=3, zorder=4)
        xs_all += xs
        ys_all += ys
        if not power and not opts['vs_area']:
            uas = [r['ua'] for r in run['rows'] if r['ua'] is not None]
            if len(uas) >= 5:
                med_line = _median(uas)
                ax.axhline(med_line, color=color, linestyle=':',
                           linewidth=0.9, alpha=0.6, zorder=2)
                ys_all.append(med_line)
        if opts['breakdown']:
            coarse = coarse_cadence(run, opts)
            xpts = [(x, y) for x, y, r in pts if r['index'] in run['flags']]
            _cross_marks(ax, xpts, color, coarse)
            had_x = had_x or bool(xpts)
            had_coarse = had_coarse or (coarse and bool(xpts))
            if coarse and xpts:
                cadence_notes.append(_cadence_note(run))
                warn(f"{run['name']}: current sampled every "
                     f"{run['cadence_s']:.1f} s "
                     f"({run.get('cadence_src', '?')}), slower than "
                     f"{CADENCE_COARSE_S:g} s -- a breakdown's onset can "
                     f"be anywhere in the interval before the flagged "
                     f"snapshot, so its X is drawn hollow")
            drawn_idx = {r['index'] for _, _, r in pts}
            missing = sorted(i for i in run['flags'] if i not in drawn_idx)
            if missing:
                warn(f"{run['name']}: confirmed breakdown row(s) "
                     f"{missing} lack a plottable coordinate in this mode "
                     f"-- X omitted (see the tidy CSV / current-vs-kV)")
            for x, y, r in pts:
                if r['index'] in run['advis']:
                    ax.plot([x], [y], 'D', markersize=6, color=color,
                            markerfacecolor='white', markeredgewidth=1.2,
                            zorder=5)
                    had_adv = True
        run_handles.append(Line2D([], [], color=color, label=run['name']))

    xlabel = ('Active area (mm²)' if opts['vs_area']
              else 'Nominal voltage (kV)')
    ylabel = ('|kV × (µA − run median)|  (mW)' if power
              else 'Measured current (µA)')
    _style_axes(ax, xlabel, ylabel)
    scale_notes = []
    _apply_scales(ax, opts, xs_all, ys_all, scale_notes)
    ax.set_title(_panel_title(opts, 'first',
                              ('Power' if power else 'Current')
                              + ' -- per snapshot'),
                 loc='left', fontweight='bold', fontsize=11)
    style_rows = []
    if had_x:
        style_rows.append(('breakdown (current-confirmed)',
                           {'linestyle': '', 'marker': 'X'}))
    if had_coarse:
        style_rows.append(('breakdown, coarse current sampling',
                           {'linestyle': '', 'marker': 'X',
                            'markerfacecolor': 'white',
                            'markeredgewidth': 1.4}))
    if had_adv:
        style_rows.append(('transient / advisory',
                           {'linestyle': '', 'marker': 'D',
                            'markerfacecolor': 'white'}))
    if not power and not opts['vs_area']:
        style_rows.append(('run-median baseline', {'linestyle': ':'}))
    _legend(ax, run_handles, style_rows)
    cap = ("One point per snapshot, CSV order.  X = current-confirmed "
           "breakdown, open diamond = advisory (both recomputed).\n"
           + ("Power uses the run-median-corrected current — the raw "
              "product was ~100% instrument zero × kV on the P3 era "
              "(−16 µA idle)."
              + (f"  RAW product (no median): "
                 f"{', '.join(raw_power)}." if raw_power else "")
              if power else
              "Currents carry each era's instrument offset "
              "(07-29 ≈ −16 µA idle).")
           + _cadence_caption(cadence_notes)
           + _scale_caption(scale_notes))
    fig.text(0.01, 0.005, cap, fontsize=7, color='#555555')
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    return fig


def figure_signal(runs, opts, path, warn=lambda m: None):
    """The current/power figure as a 300 dpi PNG (kept for direct
    callers/tests). Same drawing as the window -- see draw_signal."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=FIGSIZE[opts['mode']])
    draw_signal(fig, ax, runs, opts, warn)
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return path


def draw(fig, runs, opts, warn=lambda m: None):
    """Draw the whole figure for opts['mode'] into a bare Figure, creating
    the axes it needs. THE entry point for anything that renders: the
    window's live canvas calls it on every toggle, and save_figure() calls
    it for the PNG, so what you see on screen is what lands in the file."""
    if opts['mode'] == 'area':
        axl, axr = area_axes(fig, opts)
        return draw_area(fig, axl, axr, runs, opts, warn)
    return draw_signal(fig, fig.subplots(), runs, opts, warn)


def save_figure(runs, opts, path, warn=lambda m: None):
    """Write the 300 dpi PNG at the canonical geometry, WITHOUT pyplot.

    figure_area/figure_signal force the Agg backend, which in a process
    that already owns a live Tk canvas means switching backends underneath
    it. This path never imports pyplot at all, so the window can export
    while its preview stays alive -- and headless callers get the same
    bytes, because it is the same Figure, the same draw() and the same
    dpi."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=FIGSIZE[opts['mode']])
    FigureCanvasAgg(fig)
    draw(fig, runs, opts, warn)
    fig.savefig(path, dpi=300)
    return path


# ---------------------------------------------------------------------------
# tidy CSV
# ---------------------------------------------------------------------------

TIDY_COLS = ['run', 'snapshot', 'nominal_kV', 'phase', 'tag', 'area_mm2',
             'convention', 'expansion_A_A0', 'measured_uA', 'power_mW',
             'traced', 'method', 'conf', 'user_reviewed',
             'breakdown_confirmed', 'breakdown_advisory',
             'saved_breakdown_brand', 'notes']


def write_tidy(runs, path):
    """Per-snapshot tidy export. EVERY row of every run is written --
    including 'post-breakdown'-annotated ones (the P3_5 rule). Runs kept
    despite a suspect pre-scale-fix era ('suspect_kept', current/power
    modes) get their area columns blanked so bug-era areas cannot leak
    into downstream analysis.

    'convention' names each area's edge definition ('half-height'
    machine / 'outer-toe' hand trace; +5.2-5.7% apart — never compare
    absolute mm² across them), and power_mW is the run-median-corrected
    product (see power_mw): both audit 2026-08-05."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(TIDY_COLS)
        for run in runs:
            hide_areas = run.get('suspect_kept', False)
            med = run_ua_median(run)
            for r in run['rows']:
                area = None if hide_areas else r['area_mm2']
                conv = ('' if area is None
                        else 'outer-toe' if r['traced']
                        else 'half-height' if r['method'] else '')
                exp = (area / run['a0'] if area and run['a0'] else '')
                pw = power_mw(r, med)
                w.writerow([
                    run['name'], r['snapshot'],
                    '' if r['kv'] is None else r['kv'],
                    r['phase'], r['tag'],
                    '' if area is None else area,
                    conv,
                    f"{exp:.4f}" if exp != '' else '',
                    '' if r['ua'] is None else r['ua'],
                    f"{pw:.3f}" if pw is not None else '',
                    r['traced'], r['method'],
                    '' if r['conf'] is None else r['conf'],
                    r['user'],
                    run['flags'].get(r['index'], ''),
                    run['advis'].get(r['index'], ''),
                    r['index'] in run['saved_brand'],
                    r['notes'],
                ])
    return path


# ---------------------------------------------------------------------------
# the shared front-end surface: options, output paths, run preparation
#
# Everything below is what the CLI's main() and the window (`#223`) BOTH go
# through. Keeping it here rather than in the window is the whole point:
# a run the command line excludes is excluded in the window too, for the
# same reason and with the same warning text, and neither front end can
# grow its own idea of what a figure's options or filenames are.
# ---------------------------------------------------------------------------

def default_stem(mode):
    """The output stem when nobody chose one."""
    return f"sldea_plot_{mode}"


def make_opts(mode='area', vs_area=False, prepost=False, mean=False,
              bands=True, breakdown=True, title=None,
              logx=False, logy=False, marker_key=True,
              title_first=None, title_second=None, subplots='both',
              cadence_guard=False):
    """-> (opts dict, error message or None).

    The CLI builds this from its flags and the window from its tick boxes,
    so an illegal combination is refused identically in both. Error strings
    are the CLI's own wording -- main() prints them verbatim.

    Every key added after `#223` defaults to the behaviour that existed
    before it, so an options dict built with no arguments still describes
    the original figure -- with ONE deliberate exception, `marker_key`,
    which `#267` asked for on by default (--no-marker-key restores the
    older figure exactly, and the test suite proves that byte for byte)."""
    if mode not in MODES:
        return None, f"unknown --mode {mode} (area | current | power)"
    if vs_area and mode == 'area':
        return None, '--vs-area applies to current/power modes only'
    subplots = subplots or 'both'
    if subplots not in SUBPLOTS:
        return None, (f"unknown --subplots {subplots} "
                      f"(both | first | second)")
    if subplots == 'second' and mode != 'area':
        # 'first' in a single-panel mode is a harmless no-op (it names the
        # only panel), but 'second' would ask for a panel that does not
        # exist -- that is a mistake, not a preference
        return None, ('--subplots second applies to area mode only '
                      '(current/power draw a single panel)')
    return {'mode': mode, 'vs_area': bool(vs_area),
            'prepost': bool(prepost), 'mean': bool(mean),
            'bands': bool(bands), 'breakdown': bool(breakdown),
            'title': title or None,
            'logx': bool(logx), 'logy': bool(logy),
            'marker_key': bool(marker_key),
            'title_first': title_first or None,
            'title_second': title_second or None,
            'subplots': subplots,
            'cadence_guard': bool(cadence_guard)}, None


def needs_areas(opts):
    """True when this figure reads reviewed areas -- area mode, or an area
    x axis. Current/power vs kV work on RAW runs, which is the distinction
    the window has to surface (`#223`: it was buried in --help)."""
    return opts['mode'] == 'area' or opts['vs_area']


def output_paths(out_dir, stem, mode):
    """-> (png_path, csv_path).

    The tidy CSV is derived from the PNG's stem and written beside it, and
    that is not a convenience: the per-snapshot CSV is the figure's
    evidence, and a figure that cannot be traced back to its numbers is not
    citable (`#223`). Derived in ONE place so no caller can drift."""
    stem = (stem or '').strip() or default_stem(mode)
    return (os.path.join(out_dir, stem + '.png'),
            os.path.join(out_dir, stem + '.csv'))


# ---------------------------------------------------------------------------
# the figspec sidecar (`#273`)
#
# A PNG travels; the command line that made it does not. The figspec is the
# third file every export writes: the exact options, the run directories
# that were actually plotted, and the versions that plotted them -- enough
# to re-render the figure, and enough to say what a figure in a slide deck
# came from six months later. JSON beside the PNG rather than PNG metadata
# so it is greppable, diffable and survives a crop.
# ---------------------------------------------------------------------------

SPEC_VERSION = 1        # the figspec FORMAT version, not the app's


def figspec_path(png_path):
    """The spec that belongs to a PNG. Derived from the PNG in ONE place
    so no caller can invent a different neighbour."""
    return os.path.splitext(png_path)[0] + '.figspec.json'


def _app_version():
    """v<semver>[+<short hash>], or 'unknown' off a checkout. Best effort
    on purpose: provenance is worth recording, but not worth failing an
    export over."""
    try:
        import version
        return version.version_string()
    except Exception:
        return 'unknown'


def build_figspec(runs, opts, stem):
    """-> the spec dict for this figure.

    Runs are stored RESOLVED and absolute, not as the arguments that were
    typed: a bench shortcut ('1') or a parent-of-runs path means something
    different on another machine or on another day, and a spec that
    re-renders a different run is worse than no spec at all."""
    return {
        'spec_version': SPEC_VERSION,
        'tool': 'sldea_plot',
        'app_version': _app_version(),
        'stem': stem,
        'runs': [os.path.abspath(r['dir']) for r in runs],
        'opts': dict(opts),
    }


def write_figspec(runs, opts, path, stem):
    import json
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(build_figspec(runs, opts, stem), f, indent=2,
                  sort_keys=True)
        f.write('\n')
    return path


def load_figspec(path):
    """-> (spec dict, None) or (None, error message the CLI prints).

    Validated rather than trusted: a spec is a file a human can edit, and
    the failure mode of a silently half-understood spec is a figure that
    claims to be a re-render and is not."""
    import json
    try:
        with open(path, encoding='utf-8') as f:
            spec = json.load(f)
    except OSError as e:
        return None, f"cannot read --from-spec {path}: {e}"
    except ValueError as e:
        return None, f"--from-spec {path} is not valid JSON: {e}"
    if not isinstance(spec, dict):
        return None, f"--from-spec {path}: expected a JSON object"
    ver = spec.get('spec_version')
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        return None, (f"--from-spec {path}: 'spec_version' must be a "
                      f"positive integer")
    if ver > SPEC_VERSION:
        return None, (f"--from-spec {path}: spec_version {ver} was written "
                      f"by a newer build (this one reads up to "
                      f"{SPEC_VERSION})")
    if not isinstance(spec.get('opts'), dict):
        return None, f"--from-spec {path}: no 'opts' object"
    runs = spec.get('runs')
    if not isinstance(runs, list) or not all(isinstance(r, str)
                                             for r in runs):
        return None, (f"--from-spec {path}: 'runs' must be a list of "
                      f"run directories")
    return spec, None


def export(runs, opts, out_dir, stem, warn=lambda m: None):
    """Render the figure, write its tidy CSV AND its figspec -> (png, csv).

    The single write path for both front ends. There is deliberately no
    way to ask for the PNG alone: on-screen preview is free, but anything
    that lands on disk lands with its numbers and with the spec that says
    where they came from. The return stays (png, csv) -- the spec's path
    is figspec_path(png), so existing callers keep working unchanged."""
    os.makedirs(out_dir, exist_ok=True)
    png, tidy = output_paths(out_dir, stem, opts['mode'])
    save_figure(runs, opts, png, warn)
    write_tidy(runs, tidy)
    # the EFFECTIVE stem (output_paths defaults a blank one), so a
    # re-render from this spec lands on the same filenames
    write_figspec(runs, opts, figspec_path(png),
                  os.path.splitext(os.path.basename(png))[0])
    return png, tidy


def prepare_runs(args, opts, warn=lambda m: None, allow_suspect=False,
                 load=None):
    """Resolve, load, era-guard, filter and colour the runs -> list.

    Returns [] when nothing is plottable (the caller reports the collected
    warnings). Every guard the CLI grew lives here, so the window inherits
    them instead of reimplementing them: the pre-2026-07-28 scale-bug era,
    the reviewed-areas requirement, the missing-baseline case, the palette
    wrap and the cross-run anchor-provenance advisory.

    `load` overrides load_run. The window redraws on every tick box and
    re-reading each run's CSV (and recomputing its breakdown flags) that
    often made it feel broken, so it passes a cache -- the guards below
    still run every time, because which runs are plottable depends on the
    mode."""
    load = load or load_run
    uses_areas = needs_areas(opts)
    runs = []
    for a in args:
        run = load(a, warn)
        if run is None:
            continue
        # explicit rather than defaulted: a run dict reused across two
        # renders (the window redraws on every toggle) must not carry a
        # previous mode's blanking decision into this one
        run['suspect_kept'] = False
        if suspect_old_scale(run):
            if allow_suspect:
                warn(f"{run['name']}: pre-{SCALE_FIX_DATE} areas "
                     f"KEPT on --allow-suspect-scale")
            elif uses_areas:
                warn(f"{run['name']}: EXCLUDED -- areas predate the "
                     f"{SCALE_FIX_DATE} scale fix and cannot be verified "
                     f"against the nominal disc (2.3-2.7x bug era). "
                     f"Reprocess, or override with --allow-suspect-scale")
                continue
            else:
                # currents are unaffected by the blob-scale bug -- keep
                # the run, but keep its bug-era areas out of the export
                run['suspect_kept'] = True
                warn(f"{run['name']}: areas predate the "
                     f"{SCALE_FIX_DATE} scale fix -- run kept for "
                     f"{opts['mode']} mode (currents unaffected); area "
                     f"columns blanked in the tidy CSV")
        runs.append(run)
    if uses_areas:
        kept = []
        for run in runs:
            has_areas = any(r['area_mm2'] is not None for r in run['rows'])
            if has_areas and (opts['mode'] != 'area' or run['a0']):
                kept.append(run)
            elif not has_areas:
                warn(f"{run['name']}: no reviewed areas -- skipped "
                     f"({'area mode' if opts['mode'] == 'area' else '--vs-area'} "
                     f"needs them; current/power vs kV work on raw "
                     f"runs)")
            else:
                warn(f"{run['name']}: areas present but no "
                     f"baseline A0 -- cannot normalize, skipped in "
                     f"area mode")
        runs = kept
    if not runs:
        return runs
    if len(runs) > len(TOL_BRIGHT):
        warn(f"{len(runs)} runs > {len(TOL_BRIGHT)} palette "
             f"colors -- colors repeat; consider fewer runs per "
             f"figure")
    if uses_areas and len(runs) > 1:
        # cross-run absolute mm² inherits each run's anchor provenance
        # (audit 2026-08-05): a recorded manual 📏 anchor and a pre-gate
        # automatic one are different instruments
        with_anchor = [r['name'] for r in runs if r.get('anchor')]
        without = [r['name'] for r in runs if not r.get('anchor')]
        if with_anchor and without:
            warn(f"absolute-scale provenance differs across runs: "
                 f"{len(with_anchor)} carry a recorded manual anchor "
                 f"({', '.join(with_anchor)}), {len(without)} predate it "
                 f"({', '.join(without)}) — cross-run absolute mm² "
                 f"comparisons inherit that difference (A/A0 is safe)")
    for i, run in enumerate(runs):
        run['color'] = TOL_BRIGHT[i % len(TOL_BRIGHT)]
    return runs


# ---------------------------------------------------------------------------
# selftest (synthetic runs, no bench data -- run data never enters the repo)
# ---------------------------------------------------------------------------

def _selftest(out_png):
    import tempfile
    tmp = tempfile.mkdtemp(prefix='sldea_plot_selftest_')
    healthy = os.path.join(tmp, 'HEALTHY_20260805')
    broken = os.path.join(tmp, 'BREAKDOWN_20260805')
    for d in (healthy, broken):
        os.makedirs(os.path.join(d, 'frames'), exist_ok=True)
    cols = ['snapshot', 'step', 'tag', 'nominal_kV', 'control_V',
            'measured_kV', 'measured_uA', 't_planned_s', 'timestamp',
            'frame_file', 'active_area_px', 'active_area_mm2',
            'active_diam_mm', 'wrinkle_idx', 'notes']

    def write(d, rows):
        with open(os.path.join(d, 'data.csv'), 'w', newline='',
                  encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({**{c: '' for c in cols}, **r})

    def rows_for(peak_kv, ua_events):
        rows = [{'snapshot': 1, 'tag': 'baseline', 'nominal_kV': 0,
                 'measured_uA': -16.0, 'active_area_mm2': 201.062,
                 'timestamp': '2026-08-05T10:00:00',
                 'notes': 'edge:resting conf 0.95'}]
        n = 2
        for step in range(1, 17):
            kv = step * 0.5
            bulge = math.exp(-((kv - peak_kv) / 2.2) ** 2)
            for phase in ('pre-ramp', 'post-ramp'):
                area = 201.062 * (1 + 1.4 * bulge)
                traced = 4.0 <= kv <= 5.5
                rows.append({
                    'snapshot': n, 'tag': phase, 'nominal_kV': kv,
                    'measured_uA': ua_events.get(n, -16.0),
                    'active_area_mm2': round(area, 3),
                    'timestamp': '2026-08-05T10:%02d:00' % n,
                    'notes': ('edge:manual-trace conf 1.00 (user)'
                              if traced else 'edge:disc-fit conf 0.93'),
                })
                n += 1
        return rows

    write(healthy, rows_for(5.0, {}))
    write(broken, rows_for(5.5, {29: -80.0, 30: -140.0, 31: -205.0,
                                 32: -210.0, 33: -190.0}))
    warns = []
    runs = [load_run(healthy, warns.append), load_run(broken, warns.append)]
    for i, run in enumerate(runs):
        run['color'] = TOL_BRIGHT[i]
    assert runs[0]['flags'] == {}, runs[0]['flags']
    assert runs[1]['flags'], 'synthetic breakdown not confirmed'
    opts, _ = make_opts(mode='area', mean=True, title='selftest')
    figure_area(runs, opts, out_png, warns.append)
    write_tidy(runs, os.path.splitext(out_png)[0] + '.csv')
    print(f"selftest ok -- wrote {out_png}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_BOOL_FLAGS = ('--vs-area', '--prepost', '--mean', '--no-bands',
               '--no-breakdown', '--allow-suspect-scale', '--selftest',
               '--gui', '--logx', '--logy', '--no-marker-key',
               '--cadence-guard')
_VALUED_FLAGS = ('--mode', '--out', '--stem', '--title',
                 '--title-first', '--title-second', '--subplots',
                 '--from-spec')

_orig_stdout = None     # keeps the replaced wrapper alive: a GC'd
                        # TextIOWrapper closes the buffer it shares with
                        # the replacement (found by the test suite)


def _utf8_stdout():
    """Windows cp1252 consoles choke on the micro sign in run notes."""
    global _orig_stdout
    out = sys.stdout
    if (out is None or not hasattr(out, 'buffer')
            or (out.encoding or '').lower().replace('-', '') == 'utf8'):
        return
    _orig_stdout = out
    sys.stdout = io.TextIOWrapper(out.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)


def _usage():
    print(__doc__.strip().split('\n\n')[1])


def _parse_argv(argv):
    """-> (positionals, bool_flag_set, valued_flag_dict) or None on a bad
    invocation (message already printed). One consuming left-to-right pass:
    later duplicate valued flags win, a valued flag missing its value or a
    misspelled --flag errors out instead of leaking into positionals
    (review 2026-08-05: `--out a --out b` used to send 'b' run-hunting)."""
    args, flags, vals = [], set(), {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _BOOL_FLAGS:
            flags.add(a)
        elif a in _VALUED_FLAGS:
            if i + 1 >= len(argv) or argv[i + 1].startswith('--'):
                print(f"{a} requires a value")
                _usage()
                return None
            vals[a] = argv[i + 1]
            i += 1
        elif a.startswith('--'):
            print(f"unknown flag: {a}")
            _usage()
            return None
        else:
            args.append(a)
        i += 1
    return args, flags, vals


def _cli_opts(flags, vals, base=None):
    """Build the options dict from the parsed command line, over an
    optional `base` (a figspec's opts) -> (opts, error).

    ONE precedence rule, applied to every option: an explicitly given
    command-line flag wins, else the spec's value, else the built-in
    default. With no base this is exactly what the CLI did before
    --from-spec existed.

    Boolean flags are one-way switches, which is the one asymmetry worth
    knowing: --prepost can turn prepost ON over a spec that says false,
    and --no-bands can turn bands OFF over a spec that says true, but
    there is no --bands to turn a spec's `false` back on. To flip a spec
    value the other way, edit the spec -- it is a JSON file, and that is
    half the point of it being one."""
    base = base or {}

    def val(flag, key, default):
        return vals[flag] if flag in vals else base.get(key, default)

    def on(flag, key):
        """a plain --flag: present turns it on, absent inherits"""
        return True if flag in flags else bool(base.get(key, False))

    def off(flag, key):
        """a --no-... flag: present turns it off, absent inherits"""
        return False if flag in flags else bool(base.get(key, True))

    return make_opts(mode=val('--mode', 'mode', 'area'),
                     vs_area=on('--vs-area', 'vs_area'),
                     prepost=on('--prepost', 'prepost'),
                     mean=on('--mean', 'mean'),
                     bands=off('--no-bands', 'bands'),
                     breakdown=off('--no-breakdown', 'breakdown'),
                     title=val('--title', 'title', None),
                     logx=on('--logx', 'logx'),
                     logy=on('--logy', 'logy'),
                     marker_key=off('--no-marker-key', 'marker_key'),
                     title_first=val('--title-first', 'title_first', None),
                     title_second=val('--title-second', 'title_second',
                                      None),
                     subplots=val('--subplots', 'subplots', 'both'),
                     cadence_guard=on('--cadence-guard', 'cadence_guard'))


def main(argv):
    _utf8_stdout()
    parsed = _parse_argv(argv)
    if parsed is None:
        return 2
    args, flags, vals = parsed
    if '--selftest' in flags:
        return _selftest(args[0] if args else 'sldea_plot_selftest.png')

    spec = None
    if '--from-spec' in vals:
        spec, spec_err = load_figspec(vals['--from-spec'])
        if spec is None:
            print(spec_err)
            return 2
    # a spec's options are re-validated through make_opts rather than
    # trusted: it is an editable file, and an illegal combination must be
    # refused with the same wording wherever it came from
    opts, err = _cli_opts(flags, vals, spec['opts'] if spec else None)
    run_args = args or (list(spec['runs']) if spec else [])
    if '--gui' in flags:
        # the window is a front end to everything below, and it does its own
        # run picking -- so unlike the headless paths it does NOT require
        # run arguments; any given preselect it (`#223`)
        if err:
            print(err)
            return 2
        import sldea_plot_gui
        return sldea_plot_gui.launch(
            run_args, opts=opts, out_dir=vals.get('--out'),
            stem=vals.get('--stem') or (spec['stem'] if spec else None))
    # order preserved from before the `#223` refactor: a bare invocation
    # prints usage, and only an invocation that HAS runs gets told its mode
    # or --vs-area is wrong
    if not run_args:
        _usage()
        return 2
    if err:
        print(err)
        return 2

    out_dir = vals.get('--out', '.')
    os.makedirs(out_dir, exist_ok=True)
    # --out is deliberately NOT carried in a spec: a spec describes the
    # figure, not the folder somebody filed it in
    stem = vals.get('--stem') or (spec['stem'] if spec else None) \
        or default_stem(opts['mode'])

    warns = []
    runs = prepare_runs(run_args, opts, warns.append,
                        allow_suspect='--allow-suspect-scale' in flags)
    if not runs:
        for w in warns:
            print('warning:', w)
        print('nothing to plot')
        return 2

    png, tidy = export(runs, opts, out_dir, stem, warns.append)

    for run in runs:
        n_traced = sum(1 for r in run['rows'] if r['traced'])
        bd = (f"breakdown row(s) {sorted(run['flags'])}" if run['flags']
              else 'no confirmed breakdown')
        print(f"{run['name']}: {len(run['rows'])} rows, "
              f"{n_traced} traced, {bd}")
    if opts['mode'] == 'area' and len(runs) > 1:
        print('note: cross-run ABSOLUTE mm2 comparability needs the batch '
              'control round; the A/A0 panel is the safe comparison '
              '(SLDEA_MEASUREMENT.md)')
    for w in warns:
        print('warning:', w)
    print(f"wrote {png} + {tidy} + {figspec_path(png)}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
