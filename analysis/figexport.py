"""Figure export contract, ported from sldea_plot.py: every exported
figure writes THREE files --

    <base>.png           300 dpi raster
    <base>.csv           tidy per-point data (utf-8-sig)
    <base>.figspec.json  the inputs/options that made it, so any
                         suspicious plot can be re-rendered and audited

Paul Tol bright palette (colorblind-safe, the lab-wide convention).
matplotlib Agg only -- callers embed or link, this module never opens
windows.
"""
import csv
import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

# Paul Tol bright (sldea_plot.py TOL_BRIGHT order)
TOL_BRIGHT = ('#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE',
              '#AA3377', '#BBBBBB')


def apply_style(ax, title='', xlabel='', ylabel=''):
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)


def export(fig, base_path, tidy_rows, spec, dpi=300):
    """Write the triplet; returns the three paths."""
    png = base_path + '.png'
    csv_path = base_path + '.csv'
    spec_path = base_path + '.figspec.json'
    fig.savefig(png, dpi=dpi, bbox_inches='tight')
    if tidy_rows:
        cols = list(tidy_rows[0].keys())
        with open(csv_path, 'w', newline='',
                  encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(tidy_rows)
    with open(spec_path, 'w', encoding='utf-8') as fh:
        json.dump(dict(spec, dpi=dpi,
                       matplotlib=matplotlib.__version__),
                  fh, indent=1, sort_keys=True, default=str)
    return png, csv_path, spec_path


def new_fig(w=7.0, h=4.2):
    return plt.figure(figsize=(w, h))


def close(fig):
    plt.close(fig)
