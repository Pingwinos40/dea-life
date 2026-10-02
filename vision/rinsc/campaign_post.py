#!/usr/bin/env python3
"""Post-process the campaign traces into per-clip metrics vs dose
(roadmap item 8). Reads <clip>_traces.csv (+ _nodes.npz) written by
campaign.py from $RINSC_OUT; nothing is re-traced.

    python vision/rinsc/campaign_post.py [--plot-only]
        -> campaign_post.csv + campaign_vs_dose.png in $RINSC_OUT

Per clip:
  #3 (CN9018 strip, 310 mm): tip displacement NORMAL to the rest strip
     axis (axis = rest centerline root -> tip from the frame-0 nodes).
     This drops the along-strip tip flicker seen in no-drive clips
     (~15 px back-and-forth where the tip's contrast is weak).
  #4 (CN9018, 330 mm): LK free-end displacement (signed, as traced).
  #1, #2 (UV-RSE): max |LK displacement| (controls; expected ~0).
mm = px x mm_per_px_approx (0.075, no fisheye correction: +/-10%).
Dose = finger rate x hours since the first clip (= exposure start, per
the author).
"""
import csv
import glob
import json
import os
import sys

import numpy as np

_root = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
for p in (_root, os.path.join(_root, 'lib')):
    if p not in sys.path:
        sys.path.insert(0, p)

from vision.rinsc import campaign as cp  # noqa: E402
from vision.rinsc import trace_fingers as tf  # noqa: E402

OUT = cp.OUT
CFG = tf.CFG
MMPX = CFG['mm_per_px_approx']
RATE = {k: v['dose_rate_gy_h'] for k, v in CFG['fingers'].items()}


def col(rows, key):
    return np.array([float(r[key]) for r in rows], float)


def normal_disp(clip, rows):
    """#3 tip displacement normal to the rest axis, px (bend positive)."""
    ddx, ddy = col(rows, 'CN9018_strip_L_ddx'), col(rows, 'CN9018_strip_L_ddy')
    axis = None
    npz = os.path.join(OUT, f'{clip}_nodes.npz')
    if os.path.exists(npz):
        nd = np.load(npz)
        n0 = nd['CN9018_strip'][0]
        if np.isfinite(n0).all():
            axis = n0[-1] - n0[0]
    if axis is None or not np.isfinite(axis).all() or np.hypot(*axis) == 0:
        axis = np.array([-1.0, 0.0])          # strips point left at rest
    axis = axis / np.hypot(*axis)
    nrm = np.array([-axis[1], axis[0]])
    v = ddx * nrm[0] + ddy * nrm[1]
    if np.nanmax(v) < -np.nanmin(v):          # orient: bend reads positive
        v = -v
    return v


ON_S = 5.0          # drive ON per pulse (Pi Zero code: 5 s ON / 5 s OFF)


def windowed_bends(t, d, onsets, fps=CFG['fps']):
    """#3 bend stats inside KNOWN drive windows (onset .. onset + 5 s,
    onsets taken from #4), instead of threshold-crossing detection on the
    #3 trace. #3's line tracker flickers ~13 px at the tip; detection on
    it invented 1-15 bends in 89 post-drive clips and dropped real ones
    at T 125-145 h (RHEL9 run report + merge, 2026-10-01). Same dict keys
    as trace_fingers.bends()."""
    d = np.asarray(d, float)
    k = 5
    sm = np.array([np.nanmedian(d[max(0, i - k // 2):i + k // 2 + 1])
                   for i in range(len(d))])
    idx = [int(np.searchsorted(t, o)) for o in onsets]
    out = []
    for j, i0 in enumerate(idx):
        i1 = min(int(np.searchsorted(t, onsets[j] + ON_S)), len(d))
        if i1 - i0 < 3:
            continue
        pre = sm[max(0, i0 - int(0.3 * fps)):i0]
        base = float(np.nanmedian(pre)) if len(pre) else float(sm[i0])
        seg = sm[i0:i1]
        if not np.isfinite(seg).any():
            continue
        pk_i = i0 + int(np.nanargmax(seg))
        pk = float(sm[pk_i])
        t90 = next((m - i0 for m in range(i0, pk_i + 1)
                    if sm[m] - base >= 0.9 * (pk - base)), None)
        nxt = idx[j + 1] if j + 1 < len(idx) else len(d)
        tail = sm[max(pk_i + 1, nxt - int(0.3 * fps)):nxt]
        res = float(np.nanmedian(tail)) if len(tail) else np.nan
        out.append({'onset_s': round(float(onsets[j]), 3),
                    'on_to_peak_s': round((pk_i - i0) / fps, 2),
                    'base_px': round(base, 1), 'peak_px': round(pk, 1),
                    'amp_px': round(pk - base, 1),
                    't90_s': None if t90 is None else round(t90 / fps, 2),
                    'residual_px': round(res, 1),
                    'recovered_frac': round(1 - res / pk, 3) if pk > 0
                    else None})
    return out


def stats(t, d, onsets=None):
    """onsets None: detect bends on this trace (used for #4, which is
    clean). onsets given: measure inside those windows (used for #3)."""
    b = (tf.bends(t, d, CFG['fps']) if onsets is None
         else windowed_bends(t, d, onsets))
    out = {'n_bends': len(b), 'onsets': [x['onset_s'] for x in b]}
    if b:
        out['peak_px'] = float(np.mean([x['peak_px'] for x in b]))
        out['peak1_px'] = b[0]['peak_px']
        out['t90_1_s'] = b[0]['t90_s']
        out['residual_1_px'] = b[0]['residual_px']
        out['recovered_1'] = b[0]['recovered_frac']
    return out


def main():
    items, t0 = cp.select()
    meta = {i['clip']: i for i in items}
    prov = os.path.join(OUT, 'rhel9_clips.txt')     # clips traced on RHEL9
    rhel = set()
    if os.path.exists(prov):
        rhel = {ln.strip().lstrip('\ufeff') for ln in
                open(prov, encoding='utf-8-sig') if ln.strip()}
    rows_out = []
    for path in sorted(glob.glob(os.path.join(OUT, '*_traces.csv'))):
        clip = os.path.basename(path)[:-len('_traces.csv')]
        if clip not in meta:
            continue
        rows = tf._read_rows(path)
        t = col(rows, 't_s')
        m = meta[clip]
        rec = {'clip': clip, 'series': m['series'], 'T_h': m['T_h'],
               'machine': 'rhel9' if clip in rhel else 'win'}
        # #4 (clean) decides whether the clip was driven and when each
        # pulse began; #3 is measured inside those windows only
        s4 = stats(t, col(rows, 'CN9018_plate_disp'))
        s3 = stats(t, normal_disp(clip, rows), onsets=s4['onsets'])
        for tag, s in (('a3', s3), ('a4', s4)):
            rec[f'{tag}_n_bends'] = s['n_bends']
            for k in ('peak_px', 'peak1_px', 't90_1_s', 'residual_1_px',
                      'recovered_1'):
                rec[f'{tag}_{k}'] = s.get(k, '')
        rec['a3_min_reach'] = float(np.nanmin(col(rows, 'CN9018_strip_L_reach')))
        rec['a1_max_px'] = float(np.nanmax(np.abs(col(rows, 'UVRSE_plate_disp'))))
        rec['a2_max_px'] = float(np.nanmax(np.abs(col(rows, 'UVRSE_strip_disp'))))
        rec['dose3_gy'] = round(RATE['CN9018_strip'] * m['T_h'], 2)
        rec['dose4_gy'] = round(RATE['CN9018_plate'] * m['T_h'], 2)
        rows_out.append(rec)
    rows_out.sort(key=lambda r: (r['series'], r['T_h']))
    p = os.path.join(OUT, 'campaign_post.csv')
    with open(p, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
        w.writeheader()
        w.writerows(rows_out)
    print(f'{len(rows_out)} clips -> {p}')
    table(rows_out)
    plot(rows_out)


def table(rows, bin_h=5.0):
    """Binned medians of the series-A metrics, printed for reports."""
    A = [r for r in rows if r['series'] == 'A']
    if not A:
        return
    print(f"{'T bin [h]':>13} {'n':>4} {'#3 pk mm':>9} {'#4 pk mm':>9} "
          f"{'#4 res mm':>9} {'t90 #3':>7} {'t90 #4':>7} {'#3 bends':>8} "
          f"{'#1 max mm':>9} {'#2 max mm':>9}")
    t = np.array([f(r['T_h']) for r in A])
    for lo in np.arange(np.floor(t.min() / bin_h) * bin_h, t.max() + 1e-9,
                        bin_h):
        s = [r for r, ti in zip(A, t) if lo <= ti < lo + bin_h]
        if not s:
            continue

        def med(k, scale=1.0):
            v = np.array([f(r[k]) for r in s]) * scale
            v = v[np.isfinite(v)]
            return np.median(v) if len(v) else np.nan
        print(f'{lo:6.1f}-{lo + bin_h:6.1f} {len(s):4d} '
              f'{med("a3_peak_px", MMPX):9.2f} {med("a4_peak_px", MMPX):9.2f} '
              f'{med("a4_residual_1_px", MMPX):9.2f} {med("a3_t90_1_s"):7.2f} '
              f'{med("a4_t90_1_s"):7.2f} {med("a3_n_bends"):8.1f} '
              f'{med("a1_max_px", MMPX):9.3f} {med("a2_max_px", MMPX):9.3f}')


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def rolling(x, y, win):
    out = np.full_like(y, np.nan)
    for i, xi in enumerate(x):
        sel = np.abs(x - xi) <= win / 2
        v = y[sel][np.isfinite(y[sel])]
        if len(v):
            out[i] = np.median(v)
    return out


def plot(rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    A = [r for r in rows if r['series'] == 'A']
    # series B is excluded (2026-09-30): its content matches the
    # early-campaign state (rest tip, amplitude) under later timestamps,
    # so its time/dose axis can't be trusted. See README.
    B = []
    C3, C4, CU = '#4477AA', '#EE6677', '#228833'
    fig, ax = plt.subplots(4, 1, figsize=(12, 13), sharex=True)
    specs = [('peak_px', MMPX, 'peak tip displacement\n(mean of 3 bends) [mm]'),
             ('residual_1_px', MMPX, 'residual at end of 5 s OFF\n(after bend 1) [mm]'),
             ('t90_1_s', 1.0, 'rise time t90, bend 1 [s]')]
    for k, (key, scale, lab) in enumerate(specs):
        for tag, dose_key, c, name in (('a3', 'dose3_gy', C3, '#3 CN9018 (310 mm)'),
                                       ('a4', 'dose4_gy', C4, '#4 CN9018 (330 mm)')):
            x = np.array([f(r[dose_key]) for r in A])
            y = np.array([f(r[f'{tag}_{key}']) for r in A]) * scale
            ax[k].plot(x, y, '.', ms=2, color=c, alpha=0.3)
            ax[k].plot(x, rolling(x, y, 2.5), '-', color=c, lw=1.5,
                       label=f'{name}, series A')
            xb = np.array([f(r[dose_key]) for r in B])
            yb = np.array([f(r[f'{tag}_{key}']) for r in B]) * scale
            if np.isfinite(yb).any():
                ax[k].plot(xb, yb, 'x', ms=3, color=c, alpha=0.7,
                           label=f'{name}, series B')
        ax[k].set_ylabel(lab)
    for tag, rk, c in (('a1', 'UVRSE_plate', CU), ('a2', 'UVRSE_strip', '#CCBB44')):
        x = np.array([RATE[rk] * f(r['T_h']) for r in A])
        y = np.array([f(r[f'{tag}_max_px']) for r in A]) * MMPX
        ax[3].plot(x, y, '.', ms=2, color=c, alpha=0.5,
                   label=f'#{tag[1]} UV-RSE ({CFG["fingers"][rk]["source_mm"]} mm), max |disp|')
    ax[3].set_ylabel('controls: max |disp| [mm]')
    for a in ax:
        for rk, c in (('CN9018_strip', C3), ('CN9018_plate', C4)):
            a.axvline(RATE[rk] * 158.8, color=c, ls='--', lw=0.8, alpha=0.6)
        a.legend(fontsize=7, loc='upper left')
    ax[-1].set_xlabel('dose at each finger [Gy]  '
                      '(dashed: drive stops at 158.8 h for #3 / #4)')
    t_max = max(f(r['T_h']) for r in A)
    fig.suptitle(f'RINSC Cs-137, series A (T+0 to {t_max:.0f} h): CN9018 bending vs dose '
                 '(mm at ~0.075 mm/px, fisheye uncorrected)', y=0.995)
    fig.tight_layout()
    p = os.path.join(OUT, 'campaign_vs_dose.png')
    fig.savefig(p, dpi=110)
    print(p)


if __name__ == '__main__':
    OUT = tf.need(OUT, 'RINSC_OUT')
    if '--plot-only' in sys.argv:
        with open(os.path.join(OUT, 'campaign_post.csv'),
                  encoding='utf-8-sig') as fh:
            plot(list(csv.DictReader(fh)))
    else:
        main()
