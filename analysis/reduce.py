"""Trend reduction: log-spaced binning for 10^6-cycle-scale plots.

High-cycle practice (peak/valley capture + log10-increment recording):
a 10^7-cycle run costs a few thousand trend points instead of 10^7,
with constant point density on a log-x axis. Interlude rows are already
sparse (one per ~10^3 cycles); this module bins BLOCK rows and merges
interlude metrics for the report/GUI plots.
"""
import csv
import math
import os


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r', newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))


def _f(row, key):
    v = (row.get(key) or '').strip()
    try:
        return float(v)
    except ValueError:
        return None


def log_bins(max_cycles, per_decade=24):
    """Bin edges: log-spaced from 1 to max (>=1 decade)."""
    if max_cycles < 10:
        return [0, max(1, int(max_cycles))]
    decades = math.log10(max_cycles)
    n = max(2, int(decades * per_decade))
    return [0] + sorted({int(round(10 ** (decades * i / n)))
                         for i in range(1, n + 1)})


def trend_rows(run_dir, per_decade=24):
    """interludes.csv -> plot-ready trend rows (one per interlude; they
    are already the right density) + binned block summaries."""
    inter = read_csv(os.path.join(run_dir, 'interludes.csv'))
    trends = []
    for r in inter:
        trends.append({
            'cycles': _f(r, 'cycles_total'),
            'actuated_s': _f(r, 'actuated_s_total'),
            'wall_s': _f(r, 'wall_s_total'),
            'disp_ratio': _f(r, 'disp_ratio'),
            'disp_value': _f(r, 'disp_value'),
            'leak_ua': _f(r, 'leak_ua'),
            'c_est_nf': _f(r, 'c_est_nf'),
            'zero_drift': _f(r, 'zero_drift'),
            'kind': r.get('kind', ''),
            'is_baseline': r.get('is_baseline', '') == 'yes',
        })
    blocks = read_csv(os.path.join(run_dir, 'blocks.csv'))
    cyc = [b for b in blocks if b.get('type') == 'cycle']
    binned = []
    if cyc:
        edges = log_bins(max(_f(b, 'cycles_total') or 0
                             for b in cyc) or 1, per_decade)
        bi = 0
        acc = []
        for b in cyc:
            tot = _f(b, 'cycles_total') or 0
            while bi < len(edges) - 1 and tot > edges[bi + 1]:
                if acc:
                    binned.append(_bin_summary(acc, edges[bi],
                                               edges[bi + 1]))
                    acc = []
                bi += 1
            acc.append(b)
        if acc:
            binned.append(_bin_summary(
                acc, edges[min(bi, len(edges) - 2)], edges[-1]))
    return {'interludes': trends, 'block_bins': binned}


def _bin_summary(rows, lo, hi):
    def col(key):
        vals = [_f(r, key) for r in rows]
        vals = [v for v in vals if v is not None]
        return vals

    im = col('imean_ua')
    vm = col('vpk_meas_kv')
    return {
        'cycles_lo': lo, 'cycles_hi': hi,
        'n_blocks': len(rows),
        'imean_ua': sum(im) / len(im) if im else None,
        'vpk_meas_kv': sum(vm) / len(vm) if vm else None,
        'counting': ';'.join(sorted({r.get('counting', '')
                                     for r in rows})),
    }
