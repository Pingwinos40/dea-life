#!/usr/bin/env python3
"""Run trace_fingers over the RINSC campaign in parallel (roadmap item 8).

    python vision/rinsc/campaign.py [--workers N] [--from ISO_TIME] [--dry]

Paths come from env vars RINSC_SHARE (clip folder), RINSC_OUT (outputs),
RINSC_TMP (scratch for local clip copies), with no defaults. Tracing
needs all three; --dry and campaign_post.py need only RINSC_OUT once
OUT/clip_list.csv exists. ffmpeg: $FFMPEG, PATH, or the imageio-ffmpeg
wheel.

Selection (series A = the continuous one-per-240-s series; series B = the
extra clip 1 s before each A, present only for the first 108 h):
  A, T <= 165 h : every clip (covers the whole actuation window)
  A, T  > 165 h : every 15th clip (hourly; post-drive negative control)
  B             : every 5th clip (does B ever show the drive?)
T = hours since the first campaign clip (2025-11-17 15:17:51), which is
NOT necessarily the exposure start.

Each worker copies its clip to local disk first (the share reads at
~40 MB/s and trace_fingers decodes twice), traces it, writes
<clip>_traces.csv + <clip>_nodes.npz to OUT, and deletes the copy.
Resumable: clips whose CSV already exists are summarized, not re-traced.
campaign_summary.csv gets one row per clip x measured finger.
"""
import csv
import datetime as dt
import json
import multiprocessing as mp
import os
import re
import shutil
import sys
import time

import numpy as np

_root = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
for p in (_root, os.path.join(_root, 'lib')):
    if p not in sys.path:
        sys.path.insert(0, p)

from vision.rinsc import trace_fingers as tf  # noqa: E402
from vision.rinsc.trace_fingers import need  # noqa: E402

SHARE = os.environ.get('RINSC_SHARE')
OUT = os.environ.get('RINSC_OUT')
TMP = os.environ.get('RINSC_TMP')
MEASURES = ['CN9018_strip_line', 'CN9018_plate', 'CN9018_strip',
            'UVRSE_strip', 'UVRSE_plate']


def ts(name):
    m = re.search(r'(\d{8})_(\d{6})', name)
    return dt.datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')


def list_clips():
    """Clip names from the share; when the share host is offline, from
    the cached listing OUT/clip_list.csv (Name column, written from the
    2026-09-30 inventory). Refreshes the cache whenever the share is up.
    RINSC_SHARE unset also means the cache: os.listdir(None) would list
    the working directory and overwrite a good cache with nothing
    (found while moving this into the repo, 2026-10-02)."""
    cache = os.path.join(need(OUT, 'RINSC_OUT'), 'clip_list.csv')
    if SHARE:
        try:
            names = sorted(n for n in os.listdir(SHARE)
                           if n.endswith('.h264'))
            os.makedirs(OUT, exist_ok=True)
            tmp = cache + '.tmp'
            with open(tmp, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.writer(fh)
                w.writerow(['Name'])
                w.writerows([n] for n in names)
            os.replace(tmp, cache)
            return names, True
        except OSError:
            pass
    with open(cache, encoding='utf-8-sig') as fh:
        return sorted(r['Name'] for r in csv.DictReader(fh)
                      if r['Name'].endswith('.h264')), False


def select():
    names, _live = list_clips()
    recs = sorted((ts(n), n) for n in names)
    recs = [r for r in recs if r[0].year == 2025]      # campaign block only
    t0 = recs[0][0]
    out = []
    a_i = b_i = 0
    for i, (t, n) in enumerate(recs):
        nxt = recs[i + 1][0] if i + 1 < len(recs) else None
        series = 'B' if nxt and (nxt - t).total_seconds() == 1 else 'A'
        th = (t - t0).total_seconds() / 3600.0
        if series == 'A':
            keep = th <= 165.0 or a_i % 15 == 0
            a_i += 1
        else:
            keep = b_i % 5 == 0
            b_i += 1
        if keep:
            out.append({'clip': n[:-5], 'series': series,
                        't_iso': t.isoformat(), 'T_h': round(th, 4)})
    return out, t0


def work(item):
    os.environ.setdefault('RINSC_FFMPEG_THREADS', '2')  # many workers
    tf.OUT = OUT
    clip = item['clip']
    csv_path = os.path.join(OUT, f'{clip}_traces.csv')
    t_start = time.time()
    try:
        if not os.path.exists(csv_path):
            os.makedirs(TMP, exist_ok=True)
            local = os.path.join(TMP, f'{os.getpid()}_{clip}.h264')
            shutil.copyfile(os.path.join(SHARE, clip + '.h264'), local)
            try:
                rows, info = tf.trace_clip(local)
            finally:
                os.remove(local)
            tmp_csv = csv_path + '.tmp'
            with open(tmp_csv, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            nd = info.pop('_nodes')
            if len(nd['frames']):
                np.savez_compressed(os.path.join(OUT, f'{clip}_nodes.npz'),
                                    **nd)
            os.replace(tmp_csv, csv_path)          # CSV last = done marker
        rows = tf._read_rows(csv_path)
        return item, summarize(tf, rows), round(time.time() - t_start, 1), ''
    except Exception as e:                          # log and move on
        return item, [], round(time.time() - t_start, 1), f'{type(e).__name__}: {e}'


def summarize(tf, rows):
    t = np.array([r['t_s'] for r in rows])
    out = []
    for m in MEASURES:
        col = (f'{m[:-5]}_L_disp' if m.endswith('_line') else f'{m}_disp')
        if col not in rows[0]:
            continue
        d = np.array([r[col] for r in rows], float)
        b = tf.bends(t, d, tf.CFG['fps'])
        rec = {'measure': m, 'n_bends': len(b),
               'max_disp_px': round(float(np.nanmax(np.abs(d))), 1)
               if np.isfinite(d).any() else '',
               'peak_mean_px': round(float(np.mean([x['peak_px'] for x in b])), 1)
               if b else '',
               'bends': json.dumps(b)}
        for k in range(3):
            rec[f'peak{k + 1}_px'] = b[k]['peak_px'] if k < len(b) else ''
        rec['t90_1_s'] = b[0]['t90_s'] if b else ''
        rec['residual_1_px'] = b[0]['residual_px'] if b else ''
        rec['recovered_1'] = b[0]['recovered_frac'] if b else ''
        if m.endswith('_line'):
            reach = np.array([r[f'{m[:-5]}_L_reach'] for r in rows], float)
            rec['min_reach'] = round(float(np.nanmin(reach)), 3) \
                if np.isfinite(reach).any() else ''
        else:
            rec['min_reach'] = ''
        out.append(rec)
    return out


def main(argv):
    workers = max(1, (os.cpu_count() or 4) - 2)
    if '--workers' in argv:
        workers = int(argv[argv.index('--workers') + 1])
    items, t0 = select()
    if '--from' in argv:                 # finish a split run from a clip time
        cut = argv[argv.index('--from') + 1]
        items = [i for i in items if i['t_iso'] >= cut]
    counts = {s: sum(1 for i in items if i['series'] == s) for s in 'AB'}
    print(f'campaign start {t0}; selected {len(items)} clips '
          f'(A {counts["A"]}, B {counts["B"]}); workers {workers}',
          flush=True)
    if '--dry' in argv:
        return
    need(SHARE, 'RINSC_SHARE')
    need(TMP, 'RINSC_TMP')
    os.makedirs(OUT, exist_ok=True)
    summ_path = os.path.join(OUT, 'campaign_summary.csv')
    fields = ['clip', 'series', 't_iso', 'T_h', 'measure', 'n_bends',
              'max_disp_px', 'peak_mean_px', 'peak1_px', 'peak2_px',
              'peak3_px', 't90_1_s', 'residual_1_px', 'recovered_1',
              'min_reach', 'bends', 'error']
    done = 0
    t_run = time.time()
    with open(summ_path, 'w', newline='', encoding='utf-8-sig') as fh, \
            open(os.path.join(OUT, 'progress.log'), 'w',
                 encoding='utf-8') as log, mp.Pool(workers) as pool:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for item, recs, secs, err in pool.imap_unordered(work, items,
                                                         chunksize=1):
            done += 1
            if err:
                w.writerow({**item, 'error': err})
            for r in recs:
                w.writerow({**item, **r, 'error': ''})
            fh.flush()
            if done % 25 == 0 or err:
                el = time.time() - t_run
                eta = el / done * (len(items) - done)
                msg = (f'{done}/{len(items)}  elapsed {el / 60:.1f} min  '
                       f'eta {eta / 60:.1f} min' + (f'  ERR {item["clip"]}: {err}'
                                                     if err else ''))
                print(msg, flush=True)
                log.write(msg + '\n')
                log.flush()
    print(f'done: {done} clips in {(time.time() - t_run) / 60:.1f} min -> '
          f'{summ_path}', flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
