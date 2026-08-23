#!/usr/bin/env python3
"""Offline bender analysis over a recording: video file or frame dir.

    python vision/bender_replay.py <video.mp4 | frames_dir> \
        --config fixture.json [--out results.csv] [--every N] \
        [--rest-frame path.png]

Purpose-built for the 159 h radiation-campaign corpus (plan roadmap
item 8): the same pure pipeline the live interludes use, run over
recorded material, producing one CSV row per analyzed frame. The
--rest-frame (a 0 V pose) arms deflection; without it, angles and
curvature still come out.

Config JSON = vision.bender.DEFAULT_SETTINGS overrides.
"""
import argparse
import csv
import json
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_root, os.path.join(_root, 'lib')):
    if p not in sys.path:
        sys.path.insert(0, p)

import cv2  # noqa: E402

from vision.bender import analyze_frame  # noqa: E402

COLUMNS = [
    'frame_idx', 'source', 'tip_defl_mm', 'bend_angle_deg',
    'kappa_mean_m1', 'kappa_max_m1', 'arc_len_mm', 'proj_len_mm',
    'tip_x_px', 'tip_y_px', 'root_x_px', 'root_y_px', 'mm_per_px',
    'scale_source', 'tag_shift_px', 'width_med_px', 'centerline_cov',
    'spline_resid_px', 'spline_lam', 'bg_median', 'contrast', 'conf',
    'flags', 'notes',
]


def _frames(path, every):
    if os.path.isdir(path):
        names = sorted(n for n in os.listdir(path)
                       if n.lower().endswith(('.png', '.jpg', '.jpeg',
                                              '.bmp', '.tif', '.tiff')))
        for i, n in enumerate(names):
            if i % every:
                continue
            img = cv2.imread(os.path.join(path, n))
            if img is not None:
                yield i, n, img[:, :, ::-1]      # BGR -> RGB
        return
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f'cannot open {path}')
    i = -1
    while True:
        ok, img = cap.read()
        if not ok:
            break
        i += 1
        if i % every:
            continue
        yield i, f'frame{i:07d}', img[:, :, ::-1]
    cap.release()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('source')
    ap.add_argument('--config', default=None,
                    help='JSON of bender.DEFAULT_SETTINGS overrides')
    ap.add_argument('--out', default='bender_results.csv')
    ap.add_argument('--every', type=int, default=1,
                    help='analyze every Nth frame')
    ap.add_argument('--rest-frame', default=None,
                    help='0 V pose image -- arms tip deflection')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args(argv)

    cfg = {}
    if args.config:
        with open(args.config, 'r', encoding='utf-8') as fh:
            cfg = json.load(fh)
    ref = None
    if args.rest_frame:
        img = cv2.imread(args.rest_frame)
        if img is None:
            raise SystemExit(f'cannot read {args.rest_frame}')
        ref = analyze_frame(img[:, :, ::-1], cfg)
        if not ref.get('ok'):
            print(f"WARNING: rest frame did not analyze cleanly "
                  f"(conf {ref['conf']}, flags {ref['flags']}) -- "
                  f"deflection disabled", file=sys.stderr)
            ref = None

    n = ok_n = 0
    with open(args.out, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction='ignore')
        w.writeheader()
        for idx, name, rgb in _frames(args.source, max(1, args.every)):
            got = analyze_frame(rgb, cfg, ref=ref)
            got['frame_idx'] = idx
            got['source'] = name
            got['flags'] = ';'.join(got['flags'])
            w.writerow(got)
            n += 1
            if got['ok']:
                ok_n += 1
            if args.limit and n >= args.limit:
                break
            if n % 200 == 0:
                print(f'  {n} frames ({ok_n} ok)...', file=sys.stderr)
    print(f'{n} frames analyzed ({ok_n} ok, {n - ok_n} flagged/low-conf)'
          f' -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
