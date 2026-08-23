"""Bender side-view pipeline: silhouette -> centerline -> spline ->
tip deflection, bend angle, curvature profile.

Pure numpy/cv2/scipy, headless, no hardware -- the sldea_edge.py
discipline. Assumes BACKLIT silhouette imaging (dark specimen on light
background); the fixture spec (docs, Phase 8 bench work) is a diffuse
LED panel plus an ArUco tag on the clamp block.

Frame convention after `rotate_deg` normalization: the clamp line is
the vertical line x = clamp_x_px and the specimen extends to the RIGHT
(+x); image y points DOWN, and positive deflection/bend is toward +y.
`sign_flip` = -1 makes "up" positive if that is how the fixture is
mounted -- the operator confirms the drawn arrow once at setup.

v1 limitation, on purpose: the centerline is y(x) per-column, valid to
tip angles of ~70 degrees -- far beyond a 1.75 kV multilayer bender.
Past that, columns see multiple silhouette runs; the fold-back guard
truncates and flags rather than guessing (parametric re-tracing is the
v2 path; the corpus replay decides if it is ever needed).

`conf` follows the sldea_edge contract verbatim: a REVIEW-ORDERING
score, not a calibrated probability. Frames under `accept_conf` queue
for human review, ordered ascending.
"""
import math

import cv2
import numpy as np

try:
    from scipy.interpolate import make_smoothing_spline
except ImportError:                    # scipy is a hard dep of this app;
    make_smoothing_spline = None       # tolerated only so import works
                                       # for --help on a bare machine

KAPPA_STATIONS = 64
DEFAULT_SETTINGS = {
    'rotate_deg': 0,          # 0/90/180/270 applied before everything
    'clamp_x_px': 40,         # the clamp line (specimen extends right)
    'roi_margin_px': 4,       # ignored border inside the frame
    'morph_px': 3,            # open/close kernel
    'min_run_px': 3,          # dark runs thinner than this are speckle
    'width_tol_frac': 0.4,    # column width within +/-40% of median
    'mm_per_px': 0.1,         # used when no tag is found/configured
    'aruco_size_mm': 0.0,     # >0 arms tag-based scale
    'aruco_id': 0,
    'sign_flip': 1.0,
    'accept_conf': 0.75,
    'bg_patch': None,         # (x, y, w, h) away from motion, optional
    'tag_ref_center': None,   # (x, y) from run start, drift canary
    'tag_shift_warn_px': 4.0,
    'multi_run_frac': 0.05,   # >5% multi-run columns => foldback
}

FLAGS = ('root_lost', 'out_of_frame', 'foldback', 'lighting_drift',
         'fixture_moved', 'no_tag')


def _norm_gray(frame, rotate_deg):
    if frame is None:
        return None
    a = np.asarray(frame)
    if a.ndim == 3:
        a = a.mean(axis=2)
    a = a.astype(np.float32)
    k = (int(rotate_deg) // 90) % 4
    if k:
        a = np.rot90(a, -k)            # clockwise like cv2.ROTATE_*
    return a


def detect_tag(gray_u8, cfg):
    """(mm_per_px, center_xy, ok) from the clamp ArUco tag, or the
    configured fixed scale when tags are not armed / not found."""
    size_mm = float(cfg.get('aruco_size_mm') or 0.0)
    if size_mm <= 0:
        return float(cfg['mm_per_px']), None, False
    try:
        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        det = cv2.aruco.ArucoDetector(d)
        corners, ids, _rej = det.detectMarkers(gray_u8)
    except Exception:
        return float(cfg['mm_per_px']), None, False
    if ids is None:
        return float(cfg['mm_per_px']), None, False
    for c, i in zip(corners, ids.flatten()):
        if int(i) == int(cfg['aruco_id']):
            pts = c.reshape(4, 2)
            side = np.mean([np.linalg.norm(pts[k] - pts[(k + 1) % 4])
                            for k in range(4)])
            if side > 1:
                return size_mm / side, tuple(pts.mean(axis=0)), True
    return float(cfg['mm_per_px']), None, False


def analyze_frame(frame, cfg=None, ref=None):
    """One frame -> the measurement dict.

    `ref` is the analysis of the interlude's 0 V rest frame (or None
    when THIS call is producing that reference): tip deflection is the
    tip's displacement perpendicular to the REST root tangent, relative
    to the rest tip. All px fields are in the rotation-normalized frame.
    """
    cfg = dict(DEFAULT_SETTINGS, **(cfg or {}))
    out = {
        'ok': False, 'conf': 0.0, 'flags': [],
        'mm_per_px': float(cfg['mm_per_px']), 'scale_source': 'fixed',
        'tag_shift_px': None,
        'root_x_px': None, 'root_y_px': None,
        'tip_x_px': None, 'tip_y_px': None,
        'tip_defl_mm': None, 'bend_angle_deg': None,
        'kappa_mean_m1': None, 'kappa_max_m1': None,
        'kappa_profile_m1': None, 'arc_len_mm': None, 'proj_len_mm': None,
        'width_med_px': None, 'centerline_cov': 0.0,
        'spline_resid_px': None, 'spline_lam': None,
        'bg_median': None, 'contrast': None,
        'root_tangent_rad': None, 'notes': '',
    }
    gray = _norm_gray(frame, cfg['rotate_deg'])
    if gray is None:
        out['notes'] = 'no frame'
        return out
    h, w = gray.shape
    u8 = np.clip(gray, 0, 255).astype(np.uint8)

    # ---- scale + fixture drift
    mm_per_px, tag_center, tag_ok = detect_tag(u8, cfg)
    out['mm_per_px'] = mm_per_px
    out['scale_source'] = 'aruco' if tag_ok else 'fixed'
    if cfg.get('aruco_size_mm') and not tag_ok:
        out['flags'].append('no_tag')
    if tag_ok and cfg.get('tag_ref_center'):
        shift = float(np.hypot(
            tag_center[0] - cfg['tag_ref_center'][0],
            tag_center[1] - cfg['tag_ref_center'][1]))
        out['tag_shift_px'] = round(shift, 2)
        if shift > float(cfg['tag_shift_warn_px']):
            out['flags'].append('fixture_moved')

    # ---- lighting drift (flag, not correct -- backlit Otsu is robust
    # to global gain; a real drift is worth knowing about, not hiding)
    if cfg.get('bg_patch'):
        x, y, pw, ph = cfg['bg_patch']
        patch = gray[y:y + ph, x:x + pw]
        if patch.size:
            out['bg_median'] = float(np.median(patch))
            ref_bg = (ref or {}).get('bg_median')
            if ref_bg and abs(out['bg_median'] - ref_bg) / ref_bg > 0.2:
                out['flags'].append('lighting_drift')

    # ---- ROI + threshold
    m = int(cfg['roi_margin_px'])
    x0 = int(cfg['clamp_x_px'])
    roi = u8[m:h - m, x0:w - m]
    if roi.size == 0:
        out['notes'] = 'ROI empty (clamp_x/margins vs frame size)'
        return out
    thr, binary = cv2.threshold(roi, 0, 255,
                                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    lo = roi[binary > 0]
    hi = roi[binary == 0]
    if lo.size and hi.size:
        # inter-class separation in units of pooled spread
        spread = max(float(lo.std() + hi.std()), 1e-6)
        out['contrast'] = float((hi.mean() - lo.mean()) / spread)
    k = max(1, int(cfg['morph_px'])) | 1
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kern)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kern)

    # ---- component intersecting the clamp line (column 0 of the ROI)
    n_lab, labels, stats, _cent = cv2.connectedComponentsWithStats(
        binary, connectivity=8)
    root_labels = set(labels[:, 0]) - {0}
    if not root_labels:
        out['flags'].append('root_lost')
        out['notes'] = 'no dark component touches the clamp line'
        return out
    lab = max(root_labels, key=lambda i: stats[i, cv2.CC_STAT_AREA])
    mask = (labels == lab)

    # ---- out-of-frame: silhouette touching the far/top/bottom ROI edge
    if mask[:, -1].any() or mask[0, :].any() or mask[-1, :].any():
        out['flags'].append('out_of_frame')

    # ---- per-column centerline
    H, W = mask.shape
    xs, ys, widths = [], [], []
    multi = 0
    considered = 0
    for cx in range(W):
        col = mask[:, cx]
        if not col.any():
            if xs:
                break                      # past the tip
            continue
        considered += 1
        # dark runs in this column
        idx = np.flatnonzero(col)
        splits = np.flatnonzero(np.diff(idx) > 1)
        runs = np.split(idx, splits + 1)
        runs = [r for r in runs if len(r) >= int(cfg['min_run_px'])]
        if not runs:
            continue
        if len(runs) > 1:
            multi += 1
            continue                       # fold-back candidate column
        r = runs[0]
        xs.append(cx)
        ys.append(0.5 * (r[0] + r[-1]))
        widths.append(len(r))
    if considered == 0 or len(xs) < 8:
        out['flags'].append('root_lost')
        out['notes'] = 'too few valid centerline columns'
        return out
    if multi / max(considered, 1) > float(cfg['multi_run_frac']):
        out['flags'].append('foldback')

    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    widths = np.asarray(widths, dtype=np.float64)
    med_w = float(np.median(widths))
    # END-CAP TRIM before the general width filter: the slanted tip
    # face (and any clamp-junction bleed) produces end columns whose
    # run midpoint is the END FACE, not the centerline -- they sit
    # inside the +/-40% band yet bias the tip tangent by tens of
    # degrees (synthetic 15-deg arc measured -64 deg before this trim,
    # 2026-08-23). The gate is LOCAL, not global: a slanted beam's
    # apparent column width grows as w/cos(theta), so at 45 deg the
    # taper starts from ABOVE the global median and a global gate never
    # fires (measured: tail kappa station at -68% of truth). Reference
    # = median width of the window just inside the end, excluding the
    # last/first beam-width of columns (the taper span is <= w).
    wpx = max(int(med_w), 4)
    lo_i, hi_i = 0, len(widths) - 1

    def _local_med(a, b):
        seg = widths[max(0, a):max(0, b)]
        return float(np.median(seg)) if len(seg) else med_w
    tail_ref = _local_med(len(widths) - 4 * wpx, len(widths) - wpx)
    while hi_i > lo_i and widths[hi_i] < 0.9 * tail_ref:
        hi_i -= 1
    head_ref = _local_med(wpx, 4 * wpx)
    while lo_i < hi_i and widths[lo_i] < 0.9 * head_ref:
        lo_i += 1
    x_end_raw = float(xs[-1])       # pre-trim extent: the physical tip
    xs, ys, widths = xs[lo_i:hi_i + 1], ys[lo_i:hi_i + 1], \
        widths[lo_i:hi_i + 1]
    keep = np.abs(widths - med_w) <= float(cfg['width_tol_frac']) * med_w
    frac_kept = float(keep.mean()) if len(keep) else 0.0
    xs, ys = xs[keep], ys[keep]
    out['width_med_px'] = med_w
    out['centerline_cov'] = round(frac_kept * len(keep)
                                  / max(considered, 1), 3)
    if len(xs) < 8:
        out['flags'].append('root_lost')
        out['notes'] = 'centerline collapsed under width filtering'
        return out

    # ---- smoothing spline with an EXPLICIT lambda. GCV latches onto
    # the half-pixel quantization of run midpoints and interpolates the
    # staircase (measured: kappa profile oscillating +/-700 1/m against
    # a 13 1/m truth); lam=1e3 at unit column spacing gives ~10-px
    # effective bandwidth -- quantization gone, curvature kept. Two
    # honest caveats handled below: cubic smoothing splines have
    # NATURAL boundary conditions (y'' -> 0 at the ends), so tangents
    # and kappa near the ends are biased toward straight -- everything
    # end-adjacent is therefore evaluated INSET and extrapolated back
    # using interior curvature (exact for a constant-kappa arc).
    if make_smoothing_spline is None:
        out['notes'] = 'scipy missing'
        return out
    # lam=100 tuned on the synthetic gauge set (0/15/30/45 deg all
    # within 0.25 deg, kappa within 1.5%, 2026-08-23); configurable
    # per fixture -- retune against the physical gauges in BENCH_TEST
    # section U before trusting new optics.
    lam = float(cfg.get('lam') or 100.0)
    try:
        spl = make_smoothing_spline(xs, ys, lam=lam)
    except Exception as e:
        out['notes'] = f'spline failed: {e}'
        return out
    fit = spl(xs)
    out['spline_resid_px'] = float(np.sqrt(np.mean((ys - fit) ** 2)))
    out['spline_lam'] = lam

    d1 = spl.derivative(1)
    x_dense = np.linspace(xs[0], xs[-1], 400)
    yp = d1(x_dense)

    # arc length along the spline
    seg = np.sqrt(1.0 + yp[:-1] ** 2) * np.diff(x_dense)
    s_cum = np.concatenate([[0.0], np.cumsum(seg)])
    arc_len_px = float(s_cum[-1])
    sign = float(cfg['sign_flip'])

    # ---- INTEGRAL estimators. Pointwise atan(d1(x)) rides a
    # quantization-driven oscillation of up to ~0.7 deg (measured), so
    # nothing end-adjacent is read at a point:
    #  - the tangent-angle profile theta(s) = atan(y') is fitted with a
    #    LINE over each end's window and extrapolated to s=0 / s=L*
    #    (exact for a constant-curvature arc, averages the oscillation
    #    away as 1/sqrt(N));
    #  - the curvature profile is d(theta_smoothed)/ds, not
    #    y''/(1+y'^2)^1.5 of the raw spline.
    theta = np.arctan(yp)
    # skip clears the natural-BC boundary layer (~3*lam^(1/4) px, and
    # empirically up to ~8% of length at high bend angles)
    skip = min(max(3.0 * lam ** 0.25, 0.08 * arc_len_px),
               0.2 * arc_len_px)
    win = 0.30 * arc_len_px

    def _end_fit(s_lo, s_hi):
        sel = (s_cum >= s_lo) & (s_cum <= s_hi)
        if sel.sum() < 8:
            sel = np.ones_like(s_cum, dtype=bool)
        a, b = np.polyfit(s_cum[sel], theta[sel], 1)
        return a, b                       # theta(s) ~ a*s + b

    a_r, b_r = _end_fit(skip, skip + win)
    ang_root = float(b_r)                 # extrapolated to s = 0
    root_xy = (float(xs[0]) + x0, float(spl(xs[0])) + m)

    def _at_s(s):
        """(x, y) at arc length s; beyond the span, extend along the
        end-window tangent direction."""
        s_in = min(max(s, 0.0), arc_len_px)
        x = float(np.interp(s_in, s_cum, x_dense))
        y = float(spl(x))
        ds = s - s_in
        if abs(ds) > 1e-9:
            ang = float(np.interp(s_in, s_cum, theta))
            x += math.cos(ang) * ds
            y += math.sin(ang) * ds
        return x, y

    # tip: at the FIXED material arc length L*. Default L* = the
    # PHYSICAL arc length reconstructed from the pre-trim column
    # extent (the end-cap trim removed the slanted tip face from the
    # spline; the beam physically continues to about x_end_raw minus
    # the far-corner overshoot (w/2)*|sin(tip angle)|). With a ref (an
    # interlude's rest frame) its stored L* wins, so every frame
    # measures the SAME material point.
    a_t, b_t = _end_fit(arc_len_px - skip - win, arc_len_px - skip)
    ang_end = a_t * arc_len_px + b_t
    dx_ext = max(0.0, x_end_raw - float(xs[-1])
                 - 0.5 * med_w * abs(math.sin(ang_end)))
    l_own = arc_len_px + dx_ext / max(math.cos(ang_end), 0.3)
    l_star = (ref or {}).get('arc_len_star_px') or l_own
    ang_tip = float(a_t * l_star + b_t)
    _xt, _yt = _at_s(min(l_star, 1.15 * arc_len_px))
    tip_xy = (_xt + x0, _yt + m)

    # kappa stations from the smoothed tangent profile (window ~15% of
    # length: delamination localization needs ~10%-scale resolution);
    # the scalar mean is INTERIOR-only (station-window edges are MA
    # artifacts, and real clamp/tip regions are boundary-affected
    # anyway).
    n_ma = max(5, int(0.15 * len(theta))) | 1
    kern = np.ones(n_ma) / n_ma
    counts = np.convolve(np.ones_like(theta), kern, mode='same')
    theta_sm = np.convolve(theta, kern, mode='same') / counts
    kappa_dense = np.gradient(theta_sm, s_cum)        # 1/px
    s_st = np.linspace(0.0, arc_len_px, KAPPA_STATIONS)
    kappa_st_px = np.interp(s_st, s_cum, kappa_dense)
    kappa_st_m1 = sign * kappa_st_px / mm_per_px * 1000.0   # 1/m
    # interior excludes the boundary-layer + MA-half-window zone at
    # each end: those stations read kappa -> 0 by construction, not by
    # physics (profile ends stay in kappa_profile_m1 for plotting, with
    # this caveat documented in DATA_FORMATS)
    n_edge = max(6, int(round((skip / arc_len_px
                               + 0.5 * n_ma / len(theta))
                              * KAPPA_STATIONS)))
    interior = kappa_st_m1[n_edge:-n_edge] \
        if 2 * n_edge < KAPPA_STATIONS else kappa_st_m1

    chord_px = math.hypot(tip_xy[0] - root_xy[0], tip_xy[1] - root_xy[1])
    if l_star + skip + 2.0 < chord_px:
        out['notes'] = 'arc < chord (impossible geometry)'
        return out

    out.update(
        root_x_px=round(root_xy[0], 2), root_y_px=round(root_xy[1], 2),
        tip_x_px=round(tip_xy[0], 2), tip_y_px=round(tip_xy[1], 2),
        bend_angle_deg=round(sign * math.degrees(ang_tip - ang_root), 3),
        kappa_mean_m1=round(float(np.mean(interior)), 4),
        kappa_max_m1=round(float(np.max(np.abs(interior))), 4),
        kappa_profile_m1=kappa_st_m1.round(4).tolist(),
        arc_len_mm=round(arc_len_px * mm_per_px, 4),
        arc_len_star_px=float(l_star),
        proj_len_mm=round(chord_px * mm_per_px, 4),
        root_tangent_rad=ang_root,
    )

    # ---- deflection vs the rest reference
    if ref and ref.get('ok'):
        t = ref['root_tangent_rad']
        # unit normal to the REST root tangent (+y-down convention)
        nx_, ny_ = -math.sin(t), math.cos(t)
        ddx = tip_xy[0] - ref['tip_x_px']
        ddy = tip_xy[1] - ref['tip_y_px']
        out['tip_defl_mm'] = round(sign * (ddx * nx_ + ddy * ny_)
                                   * mm_per_px, 4)

    # ---- confidence: product of clamped subscores (review ordering,
    # NOT a probability -- sldea_edge contract)
    sub = []
    sub.append(min(1.0, (out['contrast'] or 0.0) / 3.0))
    sub.append(min(1.0, out['centerline_cov'] / 0.8))
    sub.append(max(0.0, 1.0 - (out['spline_resid_px'] or 9.0) / 3.0))
    sub.append(0.0 if 'root_lost' in out['flags'] else 1.0)
    sub.append(0.5 if 'foldback' in out['flags'] else 1.0)
    sub.append(0.7 if 'no_tag' in out['flags'] else 1.0)
    conf = 1.0
    for s in sub:
        conf *= max(0.0, min(1.0, s))
    hard_zero = {'root_lost'}
    if hard_zero & set(out['flags']) or 'impossible' in out['notes']:
        conf = 0.0
    out['conf'] = round(conf, 3)
    out['ok'] = conf > 0.0 and 'out_of_frame' not in out['flags']
    return out


class BenderVision:
    """VisionAdapter for the interlude seam (hal contract): rest frame
    establishes the reference; peak frame measures deflection.

    value      = signed tip deflection at ref_kv [mm]
    rest_value = rest tip's perpendicular offset from the clamp-anchored
                 axis [mm] -- comparable across interludes for the
                 zero-drift (creep) criterion while the fixture holds
                 still (tag_shift flags when it does not).
    """

    metric_name = 'tip_defl_mm'

    def __init__(self, cfg=None):
        self.cfg = dict(DEFAULT_SETTINGS, **(cfg or {}))

    def measure_pair(self, rest_frame, peak_frame, context):
        rest = analyze_frame(rest_frame, self.cfg)
        peak = analyze_frame(peak_frame, self.cfg, ref=rest)
        quality = 'ok'
        if not rest.get('ok') or not peak.get('ok'):
            quality = 'bad'
        elif min(rest['conf'], peak['conf']) < self.cfg['accept_conf']:
            quality = 'review'
        rest_off = None
        if rest.get('ok'):
            t = rest['root_tangent_rad']
            nx_, ny_ = -math.sin(t), math.cos(t)
            ddx = rest['tip_x_px'] - rest['root_x_px']
            ddy = rest['tip_y_px'] - rest['root_y_px']
            rest_off = round(self.cfg['sign_flip'] * (ddx * nx_
                             + ddy * ny_) * rest['mm_per_px'], 4)
        return {
            'value': peak.get('tip_defl_mm'),
            'rest_value': rest_off,
            'quality': quality,
            'wrinkle_idx': None,
            'notes': ';'.join(sorted(set(rest['flags'])
                                     | set(peak['flags']))),
            'rest_analysis': rest, 'peak_analysis': peak,
        }
