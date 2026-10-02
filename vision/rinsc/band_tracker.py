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


def _band_center(s, vals, bg, min_depth, ridge_frac=0.3, n_sh=4):
    """Offset of the dark line nearest s=0 (darkness centroid of its
    half-depth span), its width and depth; None when there is no line.

    A valid line is a local minimum with BRIGHTER ground on both sides:
    each shoulder must sit at least ridge_frac * depth above the line's
    floor. Shoulders need not be the full diffuser brightness, so the
    line survives when the strip turns and its translucent face (mid
    grey) fills one side. It fails on the dark frame member (no bright
    side) and past the tip (no line)."""
    if bg is None:                       # local background
        bg = float(np.percentile(vals, 90))
    dark = bg - vals
    q = len(dark) // 4
    i0 = int(np.argmax(dark[q: 3 * q])) + q
    depth = float(dark[i0])
    if depth < min_depth:
        return None
    floor = float(vals[i0])
    if (float(np.mean(vals[:n_sh])) - floor < ridge_frac * depth or
            float(np.mean(vals[-n_sh:])) - floor < ridge_frac * depth):
        return None
    half_level = 0.5 * depth
    lo = i0
    while lo > 0 and dark[lo - 1] > half_level:
        lo -= 1
    hi = i0
    while hi < len(dark) - 1 and dark[hi + 1] > half_level:
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
          min_depth=25, max_lost=4, alpha=0.5, max_turn_deg=12.0):
    """Follow the band from p0/theta0. Stops at `length` px of arc (if
    given) or after max_lost consecutive steps without contrast. Returns
    dict: pts (N,2), s (arc length at each point), theta (heading at
    each point), widths, stopped ('length' | 'lost' | 'edge')."""
    h, w = img.shape
    p = np.array(p0, float)            # bg None = per-profile background
    th = float(theta0)
    pts, ss, ths, wids = [p.copy()], [0.0], [th], []
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
            stopped = 'edge'
            break
        s, vals = _profile(img, q, th, half)
        got = _band_center(s, vals, bg, min_depth)
        if got is None:
            lost += 1
            if lost > max_lost:
                break
            p = q                                  # coast along heading
            arc += step
            pts.append(p.copy()); ss.append(arc); ths.append(th)
            continue
        lost = 0
        c, wd, _dep = got
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
            'stopped': stopped}


def resample(pts, s, n):
    """n equally spaced nodes along the traced centerline."""
    if len(pts) < 2:
        return np.full((n, 2), np.nan)
    t = np.linspace(0, s[-1], n)
    return np.stack([np.interp(t, s, pts[:, 0]),
                     np.interp(t, s, pts[:, 1])], axis=1)


def smooth(img_u8, sigma=1.5):
    return cv2.GaussianBlur(img_u8.astype(np.float32), (0, 0), sigma)
