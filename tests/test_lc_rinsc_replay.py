#!/usr/bin/env python3
"""Headless tests: RINSC corpus replay (vision/rinsc, roadmap item 8).

Synthetic ground truth only (no clips, no ffmpeg): the band tracker on
drawn bands, bend statistics on a simulated first-order drive response,
the campaign clip selection, and the machine-path handling that
replaced the hardcoded Windows defaults (2026-10-02).

Run: .venv/Scripts/python.exe tests/test_lc_rinsc_replay.py
"""
import csv
import datetime as dt
import math
import os as _os
import sys as _sys
import tempfile

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

import cv2
import numpy as np

from vision.rinsc import band_tracker as bt
from vision.rinsc import campaign as cp
from vision.rinsc import campaign_post as post
from vision.rinsc import trace_fingers as tf

FPS = 32.0
ONSETS = (2.72, 12.82, 22.92)       # in-clip drive onsets seen in the corpus
ON_S = 5.0
TAU_RISE, TAU_FALL = 0.8, 2.0
AMP = 80.0


def _band_image(pts, h=500, w=700, thick=10, bg=200, ink=50):
    img = np.full((h, w), bg, np.uint8)
    poly = np.round(np.asarray(pts)).astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(img, [poly], False, int(ink), thick, cv2.LINE_AA)
    return bt.smooth(img)


def _drive_trace(noise_px=0.5, seed=1):
    """First-order bender: rises with TAU_RISE while ON, falls with
    TAU_FALL while OFF; three 5 s pulses per 36 s clip."""
    t = np.arange(1152) / FPS
    x = np.zeros_like(t)
    for i in range(1, len(t)):
        on = any(o <= t[i] < o + ON_S for o in ONSETS)
        u, tau = (AMP, TAU_RISE) if on else (0.0, TAU_FALL)
        x[i] = u + (x[i - 1] - u) * math.exp(-1.0 / (FPS * tau))
    rng = np.random.default_rng(seed)
    return t, x + rng.normal(0.0, noise_px, len(t)), x


def test_march_straight_band_full_and_fixed_length():
    root, tip = (600.0, 200.0), (200.0, 240.0)
    img = _band_image([root, tip])
    theta0 = math.atan2(tip[1] - root[1], tip[0] - root[0])
    true_len = math.hypot(tip[0] - root[0], tip[1] - root[1])
    m = bt.march(img, root, theta0)
    assert m['stopped'] == 'lost', m['stopped']
    end = m['pts'][-1]
    assert math.hypot(end[0] - tip[0], end[1] - tip[1]) < 10, end
    assert abs(m['s'][-1] - true_len) < 10, (m['s'][-1], true_len)
    # a fixed arc length (the rest-frame length) stops there; arc grows
    # by the re-centered segment, not the nominal step, so to < 1 px
    m2 = bt.march(img, root, theta0, length=200.0)
    assert m2['stopped'] == 'length', m2['stopped']
    assert abs(m2['s'][-1] - 200.0) < 1.0, m2['s'][-1]


def test_march_follows_curvature():
    r, phi_end = 300.0, 1.0           # 1 rad of curl over 300 px of arc
    phi = np.linspace(0.0, phi_end, 200)
    pts = np.stack([600.0 - r * np.sin(phi),
                    400.0 - r * np.cos(phi)], axis=1)
    img = _band_image(pts)
    m = bt.march(img, tuple(pts[0]), math.pi, length=r * phi_end)
    end = m['pts'][-1]
    err = np.min(np.hypot(pts[:, 0] - end[0], pts[:, 1] - end[1]))
    assert err < 6, (end, err)
    # heading at the tip has turned by ~1 rad (y down: pi -> pi - 1)
    assert abs(m['theta'][-1] - (math.pi - phi_end)) < 0.12, m['theta'][-1]


LEGACY = dict(search=None, max_shift=None, rel_depth=None)


def _strip_with_wire_and_shadow():
    """Strip edge line root (600, 200) -> tip (300, 230); near the root
    a darker wire line 16 px below it; past the tip a faint shadow band
    continuing to x = 100 (the RINSC #3 geometry, 2026-10-02)."""
    img = np.full((420, 700), 200, np.uint8)
    cv2.line(img, (600, 200), (300, 230), 70, 6, cv2.LINE_AA)
    cv2.line(img, (600, 216), (540, 222), 40, 6, cv2.LINE_AA)
    cv2.line(img, (300, 230), (100, 250), 165, 6, cv2.LINE_AA)
    return bt.smooth(img)


def _strip_y(x):
    return 200.0 + (600.0 - x) * 30.0 / 300.0


def test_march_guards_stay_on_strip_and_stop_at_tip():
    img = _strip_with_wire_and_shadow()
    th0 = math.atan2(30.0, -300.0)
    seed = (600.0, 207.0)                  # between strip and wire lines
    y0 = bt.root_center(img, seed[0], seed[1] - 4.0)
    assert abs(y0 - 200.0) < 1.5, y0
    new = bt.march(img, (600.0, y0), th0)
    dev = [abs(y - _strip_y(x)) for x, y in new['pts']]
    assert max(dev) < 3.0, max(dev)
    tip = new['pts'][-1]
    assert abs(tip[0] - 300.0) < 10, tip      # stops at the real tip
    assert new['stopped'] == 'lost', new['stopped']
    # the merged-campaign settings show both failures on the same image
    old = bt.march(img, seed, th0, **LEGACY)
    dev_old = [abs(y - _strip_y(x)) for x, y in old['pts'] if x > 520]
    assert max(dev_old) > 10, max(dev_old)    # hopped onto the wire
    assert old['pts'][-1][0] < 200, old['pts'][-1]   # ran on into shadow


def test_root_center_vs_merged_run():
    img = np.full((300, 100), 200, np.uint8)
    for y, ink in ((100, 80), (108, 95), (117, 70)):   # strip, mid, wire
        cv2.line(img, (0, y), (99, y), ink, 5)
    sm = bt.smooth(img)
    merged = bt.column_center(sm, 50, 60, 160, None, y_hint=100)
    assert merged - 100.0 > 2.5, merged    # centroid between the lines
    y = bt.root_center(sm, 50, 101.0)
    assert abs(y - 100.0) < 1.5, y
    assert bt.root_center(sm, 50, 250.0) is None


def test_column_lines_finds_every_line_darkest_first():
    img = np.full((300, 100), 200, np.uint8)
    for y, ink in ((100, 80), (130, 40), (170, 120)):  # strip, wire, faint
        cv2.line(img, (0, y), (99, y), ink, 5)
    ys = bt.column_lines(bt.smooth(img), 50, 60, 220)
    assert len(ys) == 3, ys
    assert abs(ys[0] - 130) <= 1 and abs(ys[1] - 100) <= 1 \
        and abs(ys[2] - 170) <= 1, ys
    assert bt.column_lines(np.full((300, 100), 200.0, np.float32),
                           50, 60, 220) == []


def test_tv_to_full_lut():
    lut = tf.TV_TO_FULL
    assert lut.dtype == np.uint8 and lut.shape == (256,)
    assert lut[16] == 0 and lut[235] == 255     # tv-range endpoints
    assert lut[0] == 0 and lut[255] == 255      # clipped outside
    assert lut[126] == 128                      # 110 * 255 / 219 = 128.08
    assert np.all(np.diff(lut.astype(int)) >= 0)
    # no ties: every value sits >= 1/219 away from a .5 boundary
    v = (np.arange(16, 236) - 16) * 255.0 / 219.0
    assert np.abs(v - np.floor(v) - 0.5).min() > 1.0 / 220


def test_at_arc_interpolates_and_clamps():
    m = {'pts': np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]),
         's': np.array([0.0, 10.0, 20.0])}
    assert np.allclose(tf._at_arc(m, 15.0), [10.0, 5.0])
    assert np.allclose(tf._at_arc(m, 25.0), [10.0, 10.0])   # short march
    assert np.allclose(tf._at_arc(m, -3.0), [0.0, 0.0])


def test_column_center_and_resample():
    img = _band_image([(650.0, 200.0), (100.0, 200.0)])
    y = bt.column_center(img, 600, 150, 250, None)
    assert abs(y - 200.0) < 1.0, y
    assert bt.column_center(np.full((300, 300), 200.0, np.float32),
                            100, 50, 250, None) is None
    pts = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])
    s = np.array([0.0, 10.0, 20.0])
    nodes = bt.resample(pts, s, 5)
    assert np.allclose(nodes[2], [10.0, 0.0]), nodes
    assert np.allclose(nodes[-1], [10.0, 10.0]), nodes
    assert np.isnan(bt.resample(pts[:1], s[:1], 4)).all()


def test_bends_on_simulated_drive():
    t, d, clean = _drive_trace()
    b = tf.bends(t, d, FPS)
    assert len(b) == 3, b
    for got, on in zip(b, ONSETS):
        assert abs(got['onset_s'] - on) < 0.15, (got['onset_s'], on)
        i0, i1 = int(on * FPS), int((on + ON_S) * FPS)
        assert abs(got['peak_px'] - clean[i0:i1 + 2].max()) < 2.0, got
    # first-order rise from rest: t90 = tau ln 10
    assert abs(b[0]['t90_s'] - TAU_RISE * math.log(10)) < 0.15, b[0]
    # the OFF phase leaves a residual that the stats report
    expect_res = AMP * math.exp(-(10.1 - ON_S) / TAU_FALL)
    assert abs(b[0]['residual_px'] - expect_res) < 3.0, b[0]


def test_bends_flat_trace_is_empty():
    rng = np.random.default_rng(2)
    t = np.arange(1152) / FPS
    assert tf.bends(t, rng.normal(0.0, 1.0, len(t)), FPS) == []


def test_windowed_bends_match_detection():
    t, d, _clean = _drive_trace()
    det = tf.bends(t, d, FPS)
    win = post.windowed_bends(t, d, [x['onset_s'] for x in det])
    assert len(win) == 3, win
    for a, b in zip(det, win):
        assert abs(a['peak_px'] - b['peak_px']) < 1.0, (a, b)
    assert post.windowed_bends(t, d, []) == []


def _names():
    t0 = dt.datetime(2025, 11, 17, 15, 17, 51)
    stamp = lambda t: f'actuation_{t:%Y%m%d_%H%M%S}.h264'
    a = [t0 + dt.timedelta(seconds=240 * i) for i in range(2600)]
    b = [a[i] - dt.timedelta(seconds=1) for i in range(1, 11)]
    late = [dt.datetime(2026, 2, 2, 12, 0, 0)]
    return [stamp(x) for x in a + b + late]


def test_select_series_and_sampling():
    saved = cp.list_clips
    cp.list_clips = lambda: (_names(), True)
    try:
        items, t0 = cp.select()
    finally:
        cp.list_clips = saved
    assert t0 == dt.datetime(2025, 11, 17, 15, 17, 51)
    a = [i for i in items if i['series'] == 'A']
    b = [i for i in items if i['series'] == 'B']
    assert all(i['t_iso'].startswith('2025') for i in items)
    assert len(b) == 2, b                       # every 5th of 10
    # A: all 2476 clips through T = 165 h, then every 15th index
    late_idx = [i for i in range(2476, 2600) if i % 15 == 0]
    assert len(a) == 2476 + len(late_idx), len(a)
    assert a[0]['T_h'] == 0.0
    assert max(i['T_h'] for i in a if i['T_h'] <= 165.0) == 165.0


def test_need_names_the_variable():
    try:
        tf.need(None, 'RINSC_OUT')
    except SystemExit as e:
        assert 'RINSC_OUT' in str(e), e
    else:
        raise AssertionError('need(None) did not exit')
    assert tf.need('x', 'RINSC_OUT') == 'x'


def test_list_clips_without_share_keeps_cache():
    saved = cp.SHARE, cp.OUT
    with tempfile.TemporaryDirectory() as out, \
            tempfile.TemporaryDirectory() as share:
        cache = _os.path.join(out, 'clip_list.csv')
        with open(cache, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(['Name'])
            w.writerows([['actuation_20251117_151751.h264'],
                         ['notes.txt']])
        try:
            # unset share: read the cache, never list the cwd over it
            cp.SHARE, cp.OUT = None, out
            names, live = cp.list_clips()
            assert names == ['actuation_20251117_151751.h264'], names
            assert live is False
            with open(cache, encoding='utf-8-sig') as fh:
                assert 'notes.txt' in fh.read()
            # reachable share: list it and refresh the cache atomically
            for n in ('actuation_20251117_152151.h264', 'x.mp4'):
                open(_os.path.join(share, n), 'w').close()
            cp.SHARE = share
            names, live = cp.list_clips()
            assert names == ['actuation_20251117_152151.h264'], names
            assert live is True
            with open(cache, encoding='utf-8-sig') as fh:
                assert [r['Name'] for r in csv.DictReader(fh)] == names
            assert not _os.path.exists(cache + '.tmp')
        finally:
            cp.SHARE, cp.OUT = saved


def test_fingers_layout_consistent():
    cfg = tf.CFG
    x0, y0, x1, y1 = cfg['crop']
    assert cfg['fps'] == 32.0
    for name, f in cfg['fingers'].items():
        bx0, by0, bx1, by1 = f['box']
        assert x0 <= bx0 < bx1 <= x1 and y0 <= by0 < by1 <= y1, name
        assert 0.0 < f['tip_frac'] < 1.0, name
        assert f['dose_rate_gy_h'] > 0, name
    # dose rate falls with distance from the source
    by_dist = sorted(cfg['fingers'].values(), key=lambda f: f['source_mm'])
    rates = [f['dose_rate_gy_h'] for f in by_dist]
    assert rates == sorted(rates, reverse=True), rates


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
