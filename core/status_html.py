"""Pure renderer: status dict -> self-contained status.html.

No matplotlib in the engine hot path -- sparklines are inline SVG built
from the interlude summary points. Auto-refreshes every 60 s. Read-only
by design; the footer says why.
"""
import html as _html

_STATE_COLORS = {'GREEN': '#2e7d32', 'AMBER': '#8a5a00', 'RED': '#a01010'}


def _esc(v):
    return _html.escape(str(v)) if v is not None else ''


def _svg_spark(xs, ys, width=280, height=48, color='#1f3a5f'):
    """Inline SVG polyline sparkline; empty-safe."""
    pts = [(x, y) for x, y in zip(xs, ys)
           if isinstance(y, (int, float))]
    if len(pts) < 2:
        return ('<svg width="%d" height="%d"><text x="4" y="28" '
                'font-size="11" fill="#888">(awaiting interludes)'
                '</text></svg>' % (width, height))
    x0, x1 = pts[0][0], pts[-1][0]
    ylo = min(p[1] for p in pts)
    yhi = max(p[1] for p in pts)
    if x1 == x0:
        x1 = x0 + 1
    if yhi == ylo:
        yhi = ylo + 1e-9
    coords = []
    for x, y in pts:
        px = 4 + (width - 8) * (x - x0) / (x1 - x0)
        py = height - 6 - (height - 12) * (y - ylo) / (yhi - ylo)
        coords.append(f'{px:.1f},{py:.1f}')
    return (f'<svg width="{width}" height="{height}">'
            f'<polyline points="{" ".join(coords)}" fill="none" '
            f'stroke="{color}" stroke-width="1.6"/>'
            f'<text x="4" y="10" font-size="9" fill="#888">'
            f'{ylo:.3g} .. {yhi:.3g}</text></svg>')


def render(state, spark):
    health = state.get('health', 'GREEN')
    color = _STATE_COLORS.get(health, '#1f3a5f')
    cycles = state.get('cycles_total', 0)
    cap = state.get('cycles_target') or 0
    pct = 100.0 * cycles / cap if cap else 0.0
    flags = state.get('flags') or []
    events = state.get('recent_events') or []

    flag_html = ''.join(
        f'<div class="flag">&#9888; {_esc(f.get("message", f))}</div>'
        for f in flags) or '<div class="ok">no active flags</div>'
    ev_html = ''.join(
        f'<tr><td>{_esc(e.get("t_iso", ""))}</td>'
        f'<td>{_esc(e.get("rule_id", ""))}</td>'
        f'<td>{_esc(e.get("message", ""))}</td></tr>'
        for e in events[-10:][::-1])

    rows = [
        ('State', f"{_esc(state.get('state', '?'))} "
                  f"({'DRY RUN' if state.get('mode') == 'dry' else _esc(state.get('mode', ''))})"),
        ('Cycles', f'{cycles:,} / {cap:,}  ({pct:.1f}%)'),
        ('Actuated time', f"{state.get('actuated_s_total', 0):,.0f} s"),
        ('Block', f"{_esc(state.get('block_idx', ''))} / "
                  f"{_esc(state.get('n_blocks', ''))}  "
                  f"{_esc(state.get('block_desc', ''))}"),
        ('V_pk cmd / readback',
         f"{_esc(state.get('kv_cmd', ''))} / "
         f"{_esc(state.get('kv_meas', ''))} kV"),
        ('Last current', f"{_esc(state.get('ua_last', ''))} uA"),
        ('Last interlude',
         f"amp {_esc(state.get('disp_ratio_pct', '?'))}% of baseline, "
         f"leak {_esc(state.get('leak_ua_last', '?'))} uA"),
        ('Environment (attested)',
         f"{_esc(state.get('env_t_c', '?'))} C, "
         f"{_esc(state.get('env_p_mbar', '?'))} mbar "
         f"(entered {_esc(state.get('env_entered', '?'))})"),
        ('ETA', _esc(state.get('eta_iso', ''))),
        ('Updated', _esc(state.get('updated_iso', ''))),
    ]
    row_html = ''.join(f'<tr><th>{k}</th><td>{v}</td></tr>'
                       for k, v in rows)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>{_esc(state.get('run_id', 'DEA-LIFE run'))}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 1.2em; color: #222;
        background: #fafafa; max-width: 720px; }}
 .chip {{ display: inline-block; padding: .3em 1em; border-radius: 6px;
         color: #fff; background: {color}; font-size: 1.6em;
         font-weight: 700; }}
 .bar {{ background: #ddd; border-radius: 4px; height: 14px;
        overflow: hidden; margin: .6em 0; }}
 .bar div {{ background: {color}; height: 100%; width: {min(pct, 100.0):.1f}%; }}
 table {{ border-collapse: collapse; margin-top: .8em; }}
 th {{ text-align: left; padding: .25em .8em .25em 0; color: #555;
      font-weight: 600; white-space: nowrap; vertical-align: top; }}
 td {{ padding: .25em 0; }}
 .flag {{ background: #fff3cd; color: #8a5a00; padding: .4em .8em;
         border-radius: 4px; margin: .3em 0; font-weight: 600; }}
 .ok {{ color: #2e7d32; }}
 .spark {{ margin: .6em 1.6em .6em 0; display: inline-block; }}
 .cap {{ font-size: .8em; color: #666; }}
 footer {{ margin-top: 2em; font-size: .8em; color: #888;
          border-top: 1px solid #ddd; padding-top: .6em; }}
 .ev td {{ font-size: .85em; padding: .15em .6em .15em 0; }}
</style></head><body>
<h2>{_esc(state.get('specimen_id', ''))} &mdash;
    {_esc(state.get('run_id', ''))}</h2>
<span class="chip">{_esc(health)}</span>
<span style="font-size:1.3em; margin-left:.6em">
  {_esc(state.get('state', ''))}</span>
<div class="bar"><div></div></div>
{flag_html}
<table>{row_html}</table>
<div>
 <span class="spark">{_svg_spark(spark['cycles'], spark['disp_ratio'])}
   <div class="cap">amplitude ratio vs cycles</div></span>
 <span class="spark">{_svg_spark(spark['cycles'], spark['leak_ua'],
                                 color='#8a5a00')}
   <div class="cap">leakage uA vs cycles</div></span>
</div>
<h3>Recent events</h3>
<table class="ev">{ev_html}</table>
<footer>Read-only status page (auto-refreshes every 60 s). This page
cannot stop the run: stopping HV requires bench presence or the
control-side GUI/CLI on the bench host.</footer>
</body></html>
"""
