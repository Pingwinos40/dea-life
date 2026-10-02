#!/usr/bin/env python3
"""Per-finger tip-displacement traces for RINSC clips (roadmap item 8).

    python vision/rinsc/trace_fingers.py [--out=DIR] [clip.h264 ...]
        default clips: every .h264 in $RINSC_SRC
        output:        --out=DIR, else $RINSC_OUT

Machine paths come from the environment, never from the repo (they
lived as Windows defaults while this was local-only, until 2026-10-02).

Method, per clip (layout from fingers.json):
  pass 1  decode the fixture crop; max |frame - frame0| on a 4x-down copy
          -> the region that moves at all during the clip
  mask    per finger: free-end part of its box, dark at rest (Otsu inside
          the box: finger vs lit diffuser), restricted to the moving
          region when the finger moves (a finger that never moves keeps
          the plain dark mask and should read ~0 -- a built-in control)
  pass 2  Shi-Tomasi corners inside each mask at frame 0, pyramidal LK
          frame to frame with a forward-backward check (<1 px); a point
          that fails once is dropped for good. Displacement = median of
          (current - rest) over surviving points, in raw-frame px.
  signed  projected on the direction of the largest displacement in the
          clip, so a bend reads positive.

Pixels only: mm needs lens undistortion + a scale reference (the 20 mm
bar in the poster figure, or the known finger length). Time = frame / 32
(1152 frames = 36.0 s, measured on the first sample).
"""
import csv
import json
import math
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(HERE))
for p in (_root, os.path.join(_root, 'lib')):
    if p not in sys.path:
        sys.path.insert(0, p)

from vision.rinsc import band_tracker as bt  # noqa: E402

CFG = json.load(open(os.path.join(HERE, 'fingers.json'), encoding='utf-8'))
SRC = os.environ.get('RINSC_SRC')
OUT = os.environ.get('RINSC_OUT')
LK = dict(winSize=(31, 31), maxLevel=3,
          criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                    30, 0.01))
FB_MAX_PX = 1.0
DOWN = 4
NODES = 16                 # centerline nodes saved per line-tracked frame


def need(value, name):
    """A required path setting, or a clean exit naming the variable."""
    if not value:
        raise SystemExit(f'set {name} (see vision/rinsc/README.md)')
    return value


def ffmpeg_exe():
    """$FFMPEG, else ffmpeg on PATH, else the static binary shipped by the
    imageio-ffmpeg wheel (pip install imageio-ffmpeg)."""
    exe = os.environ.get('FFMPEG') or shutil.which('ffmpeg')
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# tv-range luma (16..235) -> full-range gray, round((Y - 16) * 255 / 219)
# clipped. No ties exist (510 (Y - 16) is even, 219 (2k + 1) is odd).
TV_TO_FULL = np.clip(np.round((np.arange(256) - 16) * 255.0 / 219.0),
                     0, 255).astype(np.uint8)


def frames(path, keyframes_only=False):
    """Gray crop frames (uint8, full range) from a raw H.264 clip.

    Default decode: the Y plane of yuv420p through TV_TO_FULL. ffmpeg's
    format=gray is build dependent: on one clip the gyan.dev git build
    (2025-12-18) read a mean 3.39 and up to 12 levels darker than
    ffmpeg 7.1 (it mixes in chroma), while the raw Y plane was
    bit-identical across both builds and TV_TO_FULL(Y) equals 7.1's
    gray exactly (2026-10-02; RHEL9's 7.0.2 matched that formula on
    2026-10-01). RINSC_DECODE=gray restores the build's format=gray,
    which is what the merged 2026-10-01 campaign used."""
    x0, y0, x1, y1 = CFG['crop']
    w, h = x1 - x0, y1 - y0
    legacy = os.environ.get('RINSC_DECODE', 'y') == 'gray'
    if not legacy and (w % 2 or h % 2 or x0 % 2 or y0 % 2):
        raise ValueError('crop must be even for the yuv420p Y plane')
    # raw H.264 has no timestamps: without a nominal input rate and
    # passthrough, ffmpeg emits ~3 frames per clip. keyframes_only decodes
    # just the I-frames (every 30th frame in this corpus, 39 per clip):
    # ~30x cheaper, enough for the moving-region pre-pass.
    skip = ['-skip_frame', 'nokey'] if keyframes_only else []
    vf = f'crop={w}:{h}:{x0}:{y0}' + (',format=gray' if legacy else '')
    fmt = [] if legacy else ['-pix_fmt', 'yuv420p']
    cmd = [ffmpeg_exe(), '-v', 'error',
           '-threads', os.environ.get('RINSC_FFMPEG_THREADS', '0'),
           *skip, '-framerate', '30', '-i', path,
           '-fps_mode', 'passthrough', '-vf', vf, *fmt,
           '-f', 'rawvideo', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = w * h
    frame_bytes = n if legacy else n * 3 // 2
    try:
        while True:
            buf = p.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            y = np.frombuffer(buf, np.uint8, count=n).reshape(h, w)
            yield y if legacy else TV_TO_FULL[y]
    finally:
        p.stdout.close()
        p.wait()


def _at_arc(m, s_ref):
    """Point of a band march at arc length s_ref, clamped to the traced
    arc (a march that stopped short returns its last point)."""
    s = m['s']
    s_ref = min(max(float(s_ref), 0.0), float(s[-1]))
    return np.array([np.interp(s_ref, s, m['pts'][:, 0]),
                     np.interp(s_ref, s, m['pts'][:, 1])])


def box_in_crop(box):
    cx, cy = CFG['crop'][0], CFG['crop'][1]
    x0, y0, x1, y1 = box
    return x0 - cx, y0 - cy, x1 - cx, y1 - cy


def moving_region(path):
    f0 = None
    acc = None
    for fr in frames(path, keyframes_only=True):
        small = cv2.resize(fr, None, fx=1 / DOWN, fy=1 / DOWN,
                           interpolation=cv2.INTER_AREA).astype(np.int16)
        if f0 is None:
            f0, full0 = small, fr.copy()
            acc = np.zeros_like(small)
            continue
        np.maximum(acc, np.abs(small - f0), out=acc)
    moving = (acc > 25).astype(np.uint8)
    moving = cv2.morphologyEx(moving, cv2.MORPH_CLOSE,
                              np.ones((5, 5), np.uint8))
    moving = cv2.resize(moving, (full0.shape[1], full0.shape[0]),
                        interpolation=cv2.INTER_NEAREST)
    return full0, cv2.dilate(moving, np.ones((15, 15), np.uint8))


def finger_masks(f0, moving):
    masks = {}
    for name, f in CFG['fingers'].items():
        x0, y0, x1, y1 = box_in_crop(f['box'])
        roi = f0[y0:y1, x0:x1]
        _t, dark = cv2.threshold(roi, 0, 1,
                                 cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        m = np.zeros_like(f0, dtype=np.uint8)
        m[y0:y1, x0:x1] = dark
        xt = int(x0 + f['tip_frac'] * (x1 - x0))
        m[:, xt:] = 0                          # free end only (clamp right)
        m = cv2.erode(m, np.ones((3, 3), np.uint8))
        mv = m & moving
        masks[name] = (mv if mv.sum() > 400 else m, bool(mv.sum() > 400))
    return masks


def trace_clip(path):
    f0, moving = moving_region(path)
    masks = finger_masks(f0, moving)
    names = list(CFG['fingers'])
    pts0, owner = [], []
    for k, name in enumerate(names):
        p = cv2.goodFeaturesToTrack(f0, maxCorners=150, qualityLevel=0.01,
                                    minDistance=6, mask=masks[name][0])
        if p is not None:
            pts0.append(p.reshape(-1, 2))
            owner += [k] * len(p)
    pts0 = np.concatenate(pts0).astype(np.float32)
    owner = np.array(owner)
    pts = pts0.copy()
    alive = np.ones(len(pts), bool)
    rows = []
    prev = None
    cx0, cy0 = CFG['crop'][0], CFG['crop'][1]
    lines = {k: dict(v) for k, v in CFG.get('lines', {}).items()}
    line_state = {}
    node_frames, node_data = [], {k: [] for k in lines}
    for n, fr in enumerate(frames(path)):
        if lines:
            sm_img = bt.smooth(fr)
        for lname, lc in lines.items():
            xr = lc['x_root'] - cx0
            ylo, yhi = lc['y_lo'] - cy0, lc['y_hi'] - cy0
            st_ = line_state.get(lname)
            if lc.get('y_root_hint') is None:      # merged-campaign seeding
                yh = None if st_ is None else st_['root_y']
                y0 = bt.column_center(sm_img, xr, ylo, yhi, None, y_hint=yh)
            else:
                # seed on the strip line itself, never on the centroid of
                # the merged strip/middle/wire run (2026-10-02)
                yh = (lc['y_root_hint'] - cy0 if st_ is None
                      else st_['root_y'])
                y0 = bt.root_center(sm_img, xr, yh)
            if y0 is None:
                y0 = yh
            m = None
            if y0 is not None:
                m = bt.march(sm_img, (xr, y0), math.radians(lc['th0_deg']),
                             length=None if st_ is None else st_['L0'],
                             **lc.get('march', {}))
            ok = m is not None and len(m['pts']) >= 3
            # the tracked "tip" is the material point tip_inset_px inboard
            # of the rest tip: the band end itself rounds off and fades,
            # and once the march stops there its last point jitters
            # (y sd 1.1 px in no-drive clips vs 0.3 px one node inboard,
            # 2026-10-02 validation); 0 = the band end (merged campaign)
            inset = float(lc.get('tip_inset_px', 0.0))
            if st_ is None:                # frame 0 = rest reference
                if not ok:
                    raise RuntimeError(f'{lname}: no rest centerline')
                L0 = float(m['s'][-1])
                st_ = line_state[lname] = {
                    'L0': L0, 'tip0': (m['pts'][-1].copy() if inset == 0
                                       else _at_arc(m, L0 - inset)),
                    'th_tip0': float(m['theta'][-1]), 'root_y': y0}
            rec = {}
            if ok:
                st_['root_y'] = y0
                tip = (m['pts'][-1] if inset == 0
                       else _at_arc(m, st_['L0'] - inset))
                k_root = min(10, len(m['theta']) - 1)
                ang = math.degrees(float(m['theta'][-1] - m['theta'][k_root]))
                d = tip - st_['tip0']
                rec = {'tipx': float(tip[0] + cx0), 'tipy': float(tip[1] + cy0),
                       'ddx': float(d[0]), 'ddy': float(d[1]),
                       'angle': ang,
                       'reach': float(m['s'][-1] / st_['L0'])}
            for key in ('tipx', 'tipy', 'ddx', 'ddy', 'angle', 'reach'):
                line_state.setdefault('_row', {})[f'{lname}_L_{key}'] = \
                    rec.get(key, float('nan'))
            if n % 2 == 0:
                nodes = (bt.resample(m['pts'], m['s'], NODES) if ok
                         else np.full((NODES, 2), np.nan))
                node_data[lname].append(nodes + [cx0, cy0])
        if lines and n % 2 == 0:
            node_frames.append(n)
        if prev is not None:
            p1, st, _e = cv2.calcOpticalFlowPyrLK(
                prev, fr, pts.reshape(-1, 1, 2), None, **LK)
            pb, stb, _e = cv2.calcOpticalFlowPyrLK(
                fr, prev, p1, None, **LK)
            fb = np.linalg.norm(pb.reshape(-1, 2) - pts, axis=1)
            good = (st.ravel() == 1) & (stb.ravel() == 1) & (fb < FB_MAX_PX)
            alive &= good
            pts = np.where(good[:, None], p1.reshape(-1, 2), pts)
        row = {'frame': n, 't_s': round(n / CFG['fps'], 4)}
        for k, name in enumerate(names):
            sel = alive & (owner == k)
            if sel.sum() >= 3:
                d = np.median(pts[sel] - pts0[sel], axis=0)
                row[f'{name}_dx'] = float(d[0])
                row[f'{name}_dy'] = float(d[1])
            else:
                row[f'{name}_dx'] = row[f'{name}_dy'] = float('nan')
            row[f'{name}_n'] = int(sel.sum())
        row.update(line_state.get('_row', {}))
        rows.append(row)
        prev = fr
    # signed displacement along each finger's dominant motion direction
    for name in names:
        d = np.array([[r[f'{name}_dx'], r[f'{name}_dy']] for r in rows])
        mag = np.hypot(d[:, 0], d[:, 1])
        if np.all(np.isnan(mag)):
            u = np.array([0.0, 1.0])
        else:
            i = int(np.nanargmax(mag))
            u = d[i] / mag[i] if mag[i] > 0 else np.array([0.0, 1.0])
        s = d @ u
        for r, v in zip(rows, s):
            r[f'{name}_disp'] = float(v)
    # line-tracked tips: signed along the dominant displacement direction
    for lname in lines:
        d = np.array([[r[f'{lname}_L_ddx'], r[f'{lname}_L_ddy']]
                      for r in rows])
        mag = np.hypot(d[:, 0], d[:, 1])
        u = np.array([0.0, 1.0])
        if not np.all(np.isnan(mag)):
            i = int(np.nanargmax(mag))
            if mag[i] > 0:
                u = d[i] / mag[i]
        for r, v in zip(rows, d @ u):
            r[f'{lname}_L_disp'] = float(v)
    info = {name: {'n_pts0': int((owner == k).sum()),
                   'moving_mask': masks[name][1]}
            for k, name in enumerate(names)}
    info['_nodes'] = {'frames': np.array(node_frames),
                      **{k: np.array(v, np.float32)
                         for k, v in node_data.items()}}
    info['_lines'] = {k: {'L0_px': v['L0']} for k, v in line_state.items()
                      if not k.startswith('_')}
    return rows, info


def bends(t, disp, fps):
    """Per-bend stats from a signed trace (rest = 0 at clip start).

    onset    first frame above the pre-bend base + 5% of the clip max
    peak     max of a 5-frame running median within onset..onset+5.5 s
             (the drive is ON for 5 s; the fall starts at the peak)
    t90      onset -> base + 90% of (peak - base)
    residual median of the 0.3 s before the next onset (or clip end)
    recovered_frac = 1 - residual / peak  (both measured from rest)
    """
    d = np.asarray(disp, float)
    gmax = np.nanmax(d)
    if not np.isfinite(gmax) or gmax < 5:
        return []
    k = 5
    sm = np.array([np.nanmedian(d[max(0, i - k // 2):i + k // 2 + 1])
                   for i in range(len(d))])
    thr_hi = 0.3 * gmax
    ups = np.where((sm[1:] >= thr_hi) & (sm[:-1] < thr_hi))[0] + 1
    crossings = []
    for i in ups:
        if not crossings or i - crossings[-1] > 2 * fps:
            crossings.append(int(i))
    out = []
    for j, c in enumerate(crossings):
        lo = crossings[j - 1] + int(5.5 * fps) if j else 0
        # the floor before this rise (rest, or the end of the previous
        # OFF); onset = last frame within 5% of the clip max above it
        floor = float(np.nanmin(sm[lo:c])) if c > lo else float(sm[c])
        on = c
        while on > lo and sm[on - 1] > floor + 0.05 * gmax:
            on -= 1
        base = float(np.nanmedian(sm[max(lo, on - int(0.3 * fps)):on + 1]))
        hi = min(on + int(5.5 * fps), len(d))
        pk_i = on + int(np.nanargmax(sm[on:hi]))
        pk = float(sm[pk_i])
        t90 = next((m - on for m in range(on, pk_i + 1)
                    if sm[m] - base >= 0.9 * (pk - base)), None)
        # end of this OFF phase: the local minimum before the next rise
        # (walk back from the next 30% crossing while still descending)
        # or the clip end
        if j + 1 < len(crossings):
            nxt_on = crossings[j + 1]
            while nxt_on - 1 > pk_i and sm[nxt_on - 1] < sm[nxt_on]:
                nxt_on -= 1
        else:
            nxt_on = len(d)
        res = float(np.nanmedian(
            sm[max(pk_i + 1, nxt_on - int(0.3 * fps)):nxt_on]))
        out.append({'onset_s': round(t[on], 3),
                    'on_to_peak_s': round((pk_i - on) / fps, 2),
                    'base_px': round(base, 1), 'peak_px': round(pk, 1),
                    'amp_px': round(pk - base, 1),
                    't90_s': None if t90 is None else round(t90 / fps, 2),
                    'residual_px': round(res, 1),
                    'recovered_frac': round(1 - res / pk, 3) if pk > 0
                    else None})
    return out


def _read_rows(path):
    with open(path, encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in r.items():
            r[k] = float(v) if k not in ('frame',) else int(v)
    return rows


def main(argv):
    global OUT
    summarize_only = '--summarize' in argv
    argv = [a for a in argv if a != '--summarize']
    for a in list(argv):
        if a.startswith('--out='):
            OUT = a.split('=', 1)[1]
            argv.remove(a)
    OUT = need(OUT, 'RINSC_OUT or --out=DIR')
    if not argv:
        src = need(SRC, 'RINSC_SRC or pass clip paths')
        argv = sorted(os.path.join(src, n) for n in os.listdir(src)
                      if n.endswith('.h264'))
    paths = argv
    os.makedirs(OUT, exist_ok=True)
    summary = []
    for p in paths:
        clip = os.path.basename(p)[:-5]
        csv_path = os.path.join(OUT, f'{clip}_traces.csv')
        if summarize_only:
            rows = _read_rows(csv_path)
            names = list(CFG['fingers'])
            info = {n: {'n_pts0': int(rows[0][f'{n}_n'])} for n in names}
        else:
            rows, info = trace_clip(p)
            with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            nd = info.pop('_nodes')
            if len(nd['frames']):
                np.savez_compressed(os.path.join(OUT, f'{clip}_nodes.npz'),
                                    **nd)
            info.pop('_lines', None)
        t = np.array([r['t_s'] for r in rows])
        for lname in CFG.get('lines', {}):
            if f'{lname}_L_disp' not in rows[0]:
                continue
            disp = np.array([r[f'{lname}_L_disp'] for r in rows], float)
            ang = np.array([r[f'{lname}_L_angle'] for r in rows], float)
            reach = np.array([r[f'{lname}_L_reach'] for r in rows], float)
            b = bends(t, disp, CFG['fps'])
            summary.append({'clip': clip, 'finger': f'{lname}_line',
                            'n_pts0': '', 'n_pts_end': '',
                            'max_disp_px': round(float(np.nanmax(np.abs(disp))), 1),
                            'n_bends': len(b), 'bends': json.dumps(b),
                            'max_angle_deg': round(float(np.nanmax(np.abs(ang))), 1),
                            'min_reach': round(float(np.nanmin(reach)), 3)})
        for name in CFG['fingers']:
            disp = np.array([r[f'{name}_disp'] for r in rows])
            b = bends(t, disp, CFG['fps'])
            summary.append({'clip': clip, 'finger': name,
                            'n_pts0': info[name]['n_pts0'],
                            'n_pts_end': rows[-1][f'{name}_n'],
                            'max_disp_px': round(float(np.nanmax(np.abs(disp))), 1),
                            'n_bends': len(b), 'bends': json.dumps(b),
                            'max_angle_deg': '', 'min_reach': ''})
        print(f'{clip}: ' + ', '.join(
            f"{s['finger']} max {s['max_disp_px']} px, {s['n_bends']} bends, "
            f"pts {s['n_pts0']}->{s['n_pts_end']}"
            for s in summary if s['clip'] == clip), flush=True)
    with open(os.path.join(OUT, 'summary.csv'), 'w', newline='',
              encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)
    print(f'-> {OUT}')


if __name__ == '__main__':
    main(sys.argv[1:])
