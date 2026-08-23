"""Analytic silhouette renderer: bender frames with KNOWN ground truth.

Renders a constant-curvature cantilever beam as a dark silhouette on a
light (backlit) background, 4x supersampled for clean anti-aliased
edges, with optional noise / blur / illumination gradient. The math
convention matches vision/bender.py: image x to the right along the
undeflected beam, image y DOWN; positive curvature bends the tip toward
+y (down). Ground truth returned in mm alongside every frame.

For curvature kappa [1/mm] and arc length s [mm] from the root:
    x(s) = sin(kappa*s)/kappa      y(s) = (1 - cos(kappa*s))/kappa
    tangent angle(s) = kappa*s     tip deflection = y(L)
(kappa -> 0 limits: x=s, y=0.)
"""
import math

import cv2
import numpy as np

BG_LEVEL = 200
BEAM_LEVEL = 40


def centerline_mm(kappa_per_mm, length_mm, n=200):
    """(x, y) arrays [mm] from root to tip, plus tip tangent angle [rad]."""
    s = np.linspace(0.0, length_mm, n)
    if abs(kappa_per_mm) < 1e-9:
        x, y = s, np.zeros_like(s)
    else:
        k = kappa_per_mm
        x = np.sin(k * s) / k
        y = (1.0 - np.cos(k * s)) / k
    return x, y, kappa_per_mm * length_mm


def render(kappa_per_mm=0.0, length_mm=20.0, width_mm=2.0,
           mm_per_px=0.1, frame_w=320, frame_h=240, clamp_x_px=40,
           root_y_px=None, noise=0.0, blur_px=0, gradient=0.0,
           seed=0):
    """One synthetic frame + its ground truth.

    Returns (gray_uint8, truth) where truth = {'tip_x_px','tip_y_px',
    'tip_defl_mm','bend_angle_deg','kappa_per_mm','arc_len_mm'}.
    """
    rng = np.random.default_rng(seed)
    root_y = frame_h // 2 if root_y_px is None else root_y_px
    ss = 4                                   # supersample factor
    W, H = frame_w * ss, frame_h * ss
    img = np.full((H, W), float(BG_LEVEL), dtype=np.float32)

    x_mm, y_mm, tip_ang = centerline_mm(kappa_per_mm, length_mm, n=400)
    # centerline in supersampled px
    cx = (clamp_x_px + x_mm / mm_per_px) * ss
    cy = (root_y + y_mm / mm_per_px) * ss
    # build the beam polygon: offset the centerline by +/- w/2 along the
    # local normal
    dx = np.gradient(cx)
    dy = np.gradient(cy)
    norm = np.hypot(dx, dy)
    norm[norm == 0] = 1.0
    nx, ny = -dy / norm, dx / norm
    half_w = 0.5 * width_mm / mm_per_px * ss
    upper = np.stack([cx + nx * half_w, cy + ny * half_w], axis=1)
    lower = np.stack([cx - nx * half_w, cy - ny * half_w], axis=1)
    poly = np.concatenate([upper, lower[::-1]]).astype(np.int32)
    cv2.fillPoly(img, [poly], BEAM_LEVEL)
    # a clamp block occupying everything left of the clamp line
    img[:, :clamp_x_px * ss] = BEAM_LEVEL

    small = cv2.resize(img, (frame_w, frame_h),
                       interpolation=cv2.INTER_AREA)
    if gradient:
        gx = np.linspace(1.0 - gradient, 1.0 + gradient, frame_w,
                         dtype=np.float32)
        small = small * gx[None, :]
    if blur_px:
        k = blur_px | 1
        small = cv2.GaussianBlur(small, (k, k), 0)
    if noise:
        small = small + rng.normal(0.0, noise, small.shape)
    frame = np.clip(small, 0, 255).astype(np.uint8)

    truth = {
        'tip_x_px': clamp_x_px + x_mm[-1] / mm_per_px,
        'tip_y_px': root_y + y_mm[-1] / mm_per_px,
        'tip_defl_mm': float(y_mm[-1]),
        'bend_angle_deg': math.degrees(tip_ang),
        'kappa_per_mm': kappa_per_mm,
        'arc_len_mm': length_mm,
        'mm_per_px': mm_per_px,
    }
    return frame, truth


def render_out_of_frame(mm_per_px=0.1, frame_w=200, frame_h=120,
                        clamp_x_px=30):
    """A beam long enough to run off the right edge (out_of_frame case)."""
    return render(kappa_per_mm=0.0,
                  length_mm=(frame_w - clamp_x_px + 20) * mm_per_px,
                  mm_per_px=mm_per_px, frame_w=frame_w, frame_h=frame_h,
                  clamp_x_px=clamp_x_px)
