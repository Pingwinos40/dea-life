"""Band-following centerline tracker for an edge-on bender silhouette.

A strip seen edge-on against the lit diffuser is a dark band. Starting
at its visible root (where it leaves the dark frame member), march along
the band in steps of `ds` px: predict the next point along the current
heading, sample an intensity profile across the heading, re-center on
the darkness centroid of the band, update the heading. Unlike a
per-column centerline this follows any curvature (the late-campaign
CN9018 strip curls past 70 deg), and marching a FIXED arc length taken
from the rest frame lands on the same material point (the tip) even
when the other finger closes in.

Continuity guards (2026-10-02, the "#3 tip flip" from the merged
campaign): near the root a wire runs ~16 px below the strip edge and
is sometimes the darker line, so re-centering on the darkest line in
the central half of the profile hopped onto it and back (+~30 px of
arc); past the tip the march kept going through faint shadow until
the crop margin, so each clip's rest arc length L0 measured the path
to the crop edge, not to the tip. Rest L0 was multimodal (380 / 390 /
395-400 px) and correlated with the #3 peak (r = 0.55). The guards:
look for the line only within `search` px of the prediction, treat a
re-centering larger than `max_shift` px as a lost step, and treat a
line shallower than `rel_depth` x the running median depth as lost.
search=None, max_shift=None, rel_depth=None reproduce the
merged-campaign code bit for bit (the stored 2026-10-01 traces differ
from that code itself by <= 3e-4 px, an environment effect).

Coordinates are (x, y) in whatever image is passed (y down). Pure
numpy/scipy/cv2, no hardware.
"""
import math

import cv2
import numpy as np
from scipy.ndimage import map_coordinates


def _profile(img, p, theta, half, step=1.0):
    """Intensities across the heading at point p: offsets -half..+half."""
    nx, ny = -math.sin(theta), math.cos(theta)
    s = np.arange(-half, half + step, step)
    xs = p[0] + s * nx
    ys = p[1] + s * ny
    vals = map_coordinates(img, [ys, xs], order=1, mode='nearest')
    return s, vals


def _band_center(s, vals, bg, min_depth, ridge_frac=0.3, n_sh=4,
                 search=None):
    """Offset of the dark line nearest s=0 (darkness centroid of its
    half-depth span), its width and depth; None when there is no line.

    search None: the darkest point in the central half of the profile
    (the merged-campaign behavior, which can pick a neighboring line).
    search = w: the darkest point within |s| <= w, and the half-depth
    span is clipped to the same window, so a neighbor line outside it
    can neither be picked nor pull the centroid.

    A valid line is a local minimum with BRIGHTER ground on both sides:
    each shoulder must sit at least ridge_frac * depth above the line's
    floor. Shoulders need not be the full diffuser brightness, so the
    line survives when the strip turns and its translucent face (mid
    grey) fills one side. It fails on the dark frame member (no bright
    side) and past the tip (no line)."""
    if bg is None:                       # local background
        bg = float(np.percentile(vals, 90))
    dark = bg - vals
    if search is None:
        q = len(dark) // 4
        lo_lim, hi_lim = q, 3 * q - 1
    else:
        inside = np.flatnonzero(np.abs(s) <= search)
        lo_lim, hi_lim = int(inside[0]), int(inside[-1])
    i0 = int(np.argmax(dark[lo_lim:hi_lim + 1])) + lo_lim
    if search is None:                   # span may run the whole profile
        lo_lim, hi_lim = 0, len(dark) - 1
    depth = float(dark[i0])
    if depth < min_depth:
        return None
    floor = float(vals[i0])
    if (float(np.mean(vals[:n_sh])) - floor < ridge_frac * depth or
            float(np.mean(vals[-n_sh:])) - floor < ridge_frac * depth):
        return None
    half_level = 0.5 * depth
    lo = i0
    while lo > lo_lim and dark[lo - 1] > half_level:
        lo -= 1
    hi = i0
    while hi < hi_lim and dark[hi + 1] > half_level:
        hi += 1
    w = np.clip(dark[lo:hi + 1] - half_level, 0, None)
    if w.sum() <= 0:
        return None
    c = float((s[lo:hi + 1] * w).sum() / w.sum())
    return c, float(s[hi] - s[lo]), depth


def column_center(img, x, y_lo, y_hi, bg, y_hint=None, min_depth=25):
    """Darkness-centroid y of the band in column x within [y_lo, y_hi]
    (nearest y_hint when several dark runs exist); None if no band."""
    col = img[y_lo:y_hi, int(x)].astype(np.float32)
    if bg is None:
        bg = float(np.percentile(col, 90))
    dark = bg - col
    above = dark > max(min_depth, 0.5 * float(dark.max()))
    if not above.any():
        return None
    idx = np.flatnonzero(above)
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
    cents = []
    for r in runs:
        wgt = np.clip(dark[r], 0, None)
        cents.append((y_lo + float((r * wgt).sum() / wgt.sum()), len(r)))
    if y_hint is None:
        return max(cents, key=lambda c: c[1])[0]
    return min(cents, key=lambda c: abs(c[0] - y_hint))[0]


def root_center(img, x, y_hint, search=4.0, min_depth=25, half=30):
    """Darkness-centroid y of the line nearest y_hint in column x, looked
    for within +/-search px of the hint; None if no line there.

    column_center merges adjacent dark runs: at the RINSC strip root the
    strip edge (~1052-1055), a middle line (~1060-1063) and a wire
    (~1070-1072) form one run whose centroid (~1064-1066) sits between
    lines, so the march started off-line (2026-10-02). This seeds on the
    line itself."""
    col_x = float(x)
    s = np.arange(-half, half + 1.0)
    vals = map_coordinates(img, [y_hint + s, np.full_like(s, col_x)],
                           order=1, mode='nearest')
    got = _band_center(s, vals, None, min_depth, ridge_frac=0.0,
                       search=search)
    return None if got is None else float(y_hint + got[0])


def root_start(img, x_root, y_lo, y_hi, bg=None, dir_sign=-1, fit_px=30):
    """Locate the band at column x_root within [y_lo, y_hi] and its
    heading from the band centers over the next fit_px columns (marching
    in direction dir_sign along x). Returns (p, theta, width) or None."""
    if bg is None:
        bg = float(np.percentile(img[y_lo:y_hi, :], 90))
    xs, ys, ws = [], [], []
    for k in range(0, fit_px + 1, 3):
        x = int(x_root + dir_sign * k)
        col = img[y_lo:y_hi, x].astype(np.float32)
        dark = bg - col
        i0 = int(np.argmax(dark))
        if dark[i0] < 25:
            continue
        half = 0.5 * dark[i0]
        lo = i0
        while lo > 0 and dark[lo - 1] > half:
            lo -= 1
        hi = i0
        while hi < len(dark) - 1 and dark[hi + 1] > half:
            hi += 1
        xs.append(x)
        ys.append(y_lo + 0.5 * (lo + hi))
        ws.append(hi - lo + 1)
    if len(xs) < 3:
        return None
    a, b = np.polyfit(xs, ys, 1)
    theta = math.atan2(dir_sign * a, dir_sign)
    p = (float(x_root), float(a * x_root + b))
    return p, theta, float(np.median(ws))


def march(img, p0, theta0, length=None, ds=4.0, half=30, bg=None,
          min_depth=25, max_lost=4, alpha=0.5, max_turn_deg=12.0,
          search=6.0, max_shift=5.0, rel_depth=0.5, n_depth_ref=5):
    """Follow the band from p0/theta0. Stops at `length` px of arc (if
    given) or after max_lost consecutive steps without contrast. Returns
    dict: pts (N,2), s (arc length at each point), theta (heading at
    each point), widths, depths, stopped ('length' | 'lost' | 'edge').

    Continuity guards (module docstring): `search` bounds where the line
    is looked for, a re-centering beyond `max_shift` px counts as a lost
    step, and once n_depth_ref steps are accepted a line shallower than
    rel_depth x their median depth counts as lost. None disables each
    (all three None = the merged-campaign behavior)."""
    h, w = img.shape
    p = np.array(p0, float)            # bg None = per-profile background
    th = float(theta0)
    pts, ss, ths, wids, deps = [p.copy()], [0.0], [th], [], []
    arc = 0.0
    lost = 0
    max_turn = math.radians(max_turn_deg)
    stopped = 'lost'
    while True:
        step = ds if length is None else min(ds, length - arc)
        if step <= 1e-6:
            stopped = 'length'
            break
        q = p + step * np.array([math.cos(th), math.sin(th)])
        if not (half < q[0] < w - half and half < q[1] < h - half):
            # coasting into the margin after losing the line is a 'lost'
            # stop (the tail is dropped below), not a band cut by the edge
            stopped = 'lost' if lost else 'edge'
            break
        s, vals = _profile(img, q, th, half)
        got = _band_center(s, vals, bg, min_depth, search=search)
        if got is not None and max_shift is not None and \
                abs(got[0]) > max_shift:
            got = None                             # a jump, not our line
        if got is not None and rel_depth is not None and \
                len(deps) >= n_depth_ref and \
                got[2] < rel_depth * float(np.median(deps)):
            got = None                             # faded: past the tip
        if got is None:
            lost += 1
            if lost > max_lost:
                break
            p = q                                  # coast along heading
            arc += step
            pts.append(p.copy()); ss.append(arc); ths.append(th)
            continue
        lost = 0
        c, wd, dep = got
        deps.append(dep)
        nx, ny = -math.sin(th), math.cos(th)
        new = q + c * np.array([nx, ny])
        d = new - p
        th_meas = math.atan2(d[1], d[0])
        dth = (th_meas - th + math.pi) % (2 * math.pi) - math.pi
        dth = max(-max_turn, min(max_turn, dth))
        th = th + alpha * dth
        seg = float(np.hypot(*d))
        arc += seg
        p = new
        pts.append(p.copy()); ss.append(arc); ths.append(th); wids.append(wd)
    if lost and len(pts) > lost:                   # drop the coasted tail
        pts, ss, ths = pts[:-lost], ss[:-lost], ths[:-lost]
    return {'pts': np.array(pts), 's': np.array(ss),
            'theta': np.array(ths), 'widths': np.array(wids),
            'depths': np.array(deps), 'stopped': stopped}


def resample(pts, s, n):
    """n equally spaced nodes along the traced centerline."""
    if len(pts) < 2:
        return np.full((n, 2), np.nan)
    t = np.linspace(0, s[-1], n)
    return np.stack([np.interp(t, s, pts[:, 0]),
                     np.interp(t, s, pts[:, 1])], axis=1)


def smooth(img_u8, sigma=1.5):
    return cv2.GaussianBlur(img_u8.astype(np.float32), (0, 0), sigma)
