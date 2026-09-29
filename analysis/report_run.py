"""Per-run report generator: one self-contained report.html.

Everything inlined (figures as base64 PNGs) so the file mails and
shares intact. Section order per the plan: TL;DR verdict first (house
rule), then identity/config-hash, environment record, lifecycle
summary, trend plots (vs cycles AND actuated time), event log with the
failure post-mortem, life-factor advisory, compliance matrix when a
claims file is present, data inventory with SHA-256 of the CSVs.

Also usable as a CLI:
    python -m analysis.report_run <run_dir> [--claims claims.json]
"""
import base64
import csv
import hashlib
import html
import io
import json
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

from . import compliance as _compliance  # noqa: E402
from . import reduce as _reduce  # noqa: E402
from .figexport import TOL_BRIGHT, apply_style  # noqa: E402


def _esc(v):
    return html.escape(str(v)) if v is not None else ''


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r', newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))


def _fig_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=140, bbox_inches='tight')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _trend_figure(trends, x_key, x_label):
    pts = [t for t in trends['interludes']
           if t.get(x_key) is not None]
    fig, axes = plt.subplots(3, 1, figsize=(7.2, 7.5), sharex=True)
    series = (('disp_ratio', 'amplitude / baseline', axes[0],
               TOL_BRIGHT[0]),
              ('leak_ua', 'leakage [uA]', axes[1], TOL_BRIGHT[1]),
              ('c_est_nf', 'capacitance [nF]', axes[2], TOL_BRIGHT[2]))
    for key, label, ax, color in series:
        xs = [t[x_key] for t in pts if t.get(key) is not None]
        ys = [t[key] for t in pts if t.get(key) is not None]
        ax.plot(xs, ys, 'o-', color=color, ms=4)
        base_x = [t[x_key] for t in pts if t['is_baseline']]
        for bx in base_x:
            ax.axvline(bx, color='#888', ls=':', lw=1)
        apply_style(ax, ylabel=label)
        if key == 'disp_ratio':
            ax.axhline(1.0, color='#888', lw=0.8)
            ax.axhline(0.9, color='#8a5a00', lw=0.8, ls='--')
            ax.axhline(0.8, color='#a01010', lw=0.8, ls='--')
    axes[-1].set_xlabel(x_label)
    if pts and (pts[-1].get(x_key) or 0) > 3000:
        for ax in axes:
            ax.set_xscale('symlog')
    fig.suptitle(f'Trends vs {x_label}')
    return fig


def generate(run_dir, claims_path=None, out_name='report.html',
             milestone=None):
    """Write the report; returns its path. `milestone` (a cycle count)
    marks a mid-run snapshot written by the engine: the TL;DR says the
    run is still in progress instead of reading the live status as an
    outcome."""
    status = {}
    sp = os.path.join(run_dir, 'status.json')
    if os.path.exists(sp):
        status = json.load(open(sp, encoding='utf-8'))
    recipe = {}
    rp = os.path.join(run_dir, 'recipe.json')
    if os.path.exists(rp):
        recipe = json.load(open(rp, encoding='utf-8'))
    events = _read_csv(os.path.join(run_dir, 'events.csv'))
    blocks = _read_csv(os.path.join(run_dir, 'blocks.csv'))
    inter = _read_csv(os.path.join(run_dir, 'interludes.csv'))
    trends = _reduce.trend_rows(run_dir)

    run_id = status.get('run_id') or os.path.basename(run_dir)
    disposition = status.get('disposition', status.get('state', '?'))
    failure_mode = status.get('failure_mode', '')
    cycles = status.get('cycles_total', 0)
    actuated = status.get('actuated_s_total', 0)

    # ---- TL;DR
    if milestone is not None:
        verdict = (f"MILESTONE SNAPSHOT at {cycles:,} cycles (milestone "
                   f"{int(milestone):,}) -- the run is still in "
                   f"progress; this is not its final verdict.")
        color = '#1f4e79'
    elif disposition == 'failed':
        verdict = (f"FAILED at {cycles:,} cycles -- first-tripped "
                   f"criterion: {failure_mode}.")
        color = '#a01010'
    elif disposition == 'NOT_ZEROED':
        verdict = ('RUN ENDED WITH HV NOT ZEROED -- treat all data '
                   'after the last clean event with suspicion and '
                   'check the amplifier log.')
        color = '#a01010'
    elif disposition in ('complete', 'suspended'):
        verdict = (f"{'Reached its stop cap' if disposition == 'complete' else 'Suspended (right-censored)'} "
                   f"at {cycles:,} cycles without a hard failure "
                   f"criterion tripping -- enters survival analysis as "
                   f"a SUSPENSION.")
        color = '#2e7d32'
    else:
        verdict = f'Run ended: {disposition} at {cycles:,} cycles.'
        color = '#8a5a00'
    amber = status.get('flags') or []
    tldr = [verdict,
            f"{cycles:,} cycles | {actuated:,.0f} s actuated | "
            f"{len(inter)} interludes | {len(events)} events.",
            ('Active AMBER flags at end: '
             + ', '.join(f.get('id', '?') for f in amber) + '.')
            if amber else 'No unacknowledged AMBER flags at end.']

    # ---- figures
    figs = []
    if inter:
        figs.append(('Trends vs cycles',
                     _fig_b64(_trend_figure(trends, 'cycles',
                                            'cycles'))))
        figs.append(('Trends vs actuated time',
                     _fig_b64(_trend_figure(trends, 'actuated_s',
                                            'actuated seconds'))))

    # ---- events table (level-coloured)
    ev_rows = []
    for e in events:
        act = e.get('action', '')
        c = {'abort': '#a01010', 'pause': '#8a5a00',
             'warn': '#8a5a00', 'flag': '#8a5a00'}.get(act, '#444')
        ev_rows.append(
            f"<tr style='color:{c}'><td>{_esc(e.get('t_iso'))}</td>"
            f"<td>{_esc(e.get('cycles_total'))}</td>"
            f"<td>{_esc(e.get('tier'))}/{_esc(e.get('rule_id'))}</td>"
            f"<td>{_esc(act)}</td><td>{_esc(e.get('message'))}</td>"
            f"<td>{_esc(e.get('trace_file'))}</td></tr>")

    # ---- environment record
    env_rows = [e for e in events
                if e.get('rule_id') in ('env_entry', 'wait_env')
                or e.get('rule_id') == 'env_band']

    # ---- compliance
    comp_html = ''
    if claims_path and os.path.exists(claims_path):
        try:
            doc = _compliance.load_claims(claims_path)
            ctx = {'run': {'cycles': cycles, 'actuated_s': actuated,
                           'disposition': disposition}}
            comp_html = _compliance.html_table(
                _compliance.evaluate(doc, ctx))
        except _compliance.ClaimsError as e:
            comp_html = f'<p>claims file invalid: {_esc(e)}</p>'

    # ---- data inventory
    inv_rows = []
    for name in sorted(os.listdir(run_dir)):
        p = os.path.join(run_dir, name)
        if os.path.isfile(p) and name.endswith(('.csv', '.json')):
            inv_rows.append(
                f'<tr><td>{_esc(name)}</td>'
                f'<td>{os.path.getsize(p):,}</td>'
                f'<td style="font-family:monospace;font-size:.75em">'
                f'{_sha256(p)}</td></tr>')

    fig_html = ''.join(
        f'<h3>{_esc(t)}</h3><img src="data:image/png;base64,{b}" '
        f'style="max-width:100%">' for t, b in figs)

    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{_esc(run_id)} report</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 1.5em auto;
        max-width: 900px; color: #222; }}
 .tldr {{ border-left: 6px solid {color}; background: #f6f6f6;
         padding: .8em 1em; margin: 1em 0; }}
 .tldr p {{ margin: .3em 0; }}
 table {{ border-collapse: collapse; width: 100%; margin: .6em 0; }}
 th, td {{ text-align: left; padding: .25em .5em; border-bottom:
          1px solid #ddd; font-size: .9em; vertical-align: top; }}
 th {{ background: #f0f0f0; }}
 h2 {{ border-bottom: 2px solid #1f3a5f; padding-bottom: .2em; }}
 .meta td:first-child {{ color: #555; white-space: nowrap; }}
 .compliance td:nth-child(3) {{ max-width: 22em; }}
</style></head><body>
<h1>{_esc(status.get('specimen_id', ''))} &mdash; {_esc(run_id)}</h1>
<div class="tldr">{''.join(f'<p>{_esc(t)}</p>' for t in tldr)}</div>

<h2>Identity</h2>
<table class="meta">
<tr><td>Specimen</td><td>{_esc(status.get('specimen_id'))}</td></tr>
<tr><td>Recipe</td><td>{_esc(recipe.get('name'))}</td></tr>
<tr><td>Recipe sha256</td><td style="font-family:monospace;
 font-size:.8em">{_esc(recipe.get('sha256'))}</td></tr>
<tr><td>Mode</td><td>{_esc(status.get('mode'))}</td></tr>
<tr><td>Disposition</td><td>{_esc(disposition)}
 {(' / failure mode: ' + _esc(failure_mode)) if failure_mode else ''}</td></tr>
<tr><td>Drive</td><td>{_esc((recipe.get('drive') or {}).get('waveform'))}
 {_esc((recipe.get('drive') or {}).get('freq_hz'))} Hz,
 {_esc((recipe.get('drive') or {}).get('v_pk_kv'))} kV pk</td></tr>
<tr><td>Cycle counting</td><td>{_esc(recipe.get('counting'))}</td></tr>
</table>

<h2>Environment record (attested)</h2>
<table><tr><th>t</th><th>event</th><th>detail</th></tr>
{''.join(f"<tr><td>{_esc(e.get('t_iso'))}</td>"
         f"<td>{_esc(e.get('rule_id'))}</td>"
         f"<td>{_esc(e.get('message'))}</td></tr>" for e in env_rows)
 or '<tr><td colspan=3>(none recorded)</td></tr>'}
</table>

<h2>Lifecycle summary</h2>
<p>{len(blocks)} block rows, {len(inter)} interludes
 ({sum(1 for r in inter if r.get('is_baseline') == 'yes')} baseline).
 Counting modes seen:
 {_esc(sorted({b.get('counting') for b in blocks if b.get('counting')}))}
</p>
{fig_html or '<p>(no interlude data for trend plots)</p>'}

<h2>Event log</h2>
<table><tr><th>t</th><th>cycles</th><th>rule</th><th>action</th>
<th>message</th><th>trace</th></tr>{''.join(ev_rows)}</table>

{('<h2>Compliance matrix</h2>' + comp_html) if comp_html else ''}

<h2>Data inventory</h2>
<table><tr><th>file</th><th>bytes</th><th>sha256</th></tr>
{''.join(inv_rows)}</table>

<footer style="margin-top:2em;color:#888;font-size:.8em">
Generated by SLDEA Lifecycle Manager analysis.report_run.
</footer>
</body></html>
"""
    out = os.path.join(run_dir, out_name)
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(doc)
    os.replace(tmp, out)
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('run_dir')
    ap.add_argument('--claims', default=None)
    args = ap.parse_args(argv)
    out = generate(args.run_dir, claims_path=args.claims)
    print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
