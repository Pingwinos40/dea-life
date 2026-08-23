"""Compliance matrix: clause-by-clause claims, evaluated -- not a badge.

None of the space standards contemplate a DEA; the honest, defensible
output is a matrix of requirement / source clause / achieved value /
verdict, with every deviation carrying a rationale. Statuses:

- comply    -- the comparison PASSES against real achieved data
- deviate   -- achieved missed the requirement, or no data exists;
               AUTOMATIC (a missed plateau must downgrade itself, never
               silently read as a pass)
- tailored  -- set BY HAND in the claims file, mandatory
               tailoring_note (e.g. GEVS mechanism-screen relief)
- pending   -- evaluation deferred (marked so in the file)

The achieved value is resolved by DOTTED PATH into a plain context
dict -- no eval, ever.
"""
import json

OPS = {
    '>=': lambda a, b: a >= b,
    '>': lambda a, b: a > b,
    '<=': lambda a, b: a <= b,
    '<': lambda a, b: a < b,
    '==': lambda a, b: a == b,
}
STATUSES = ('comply', 'deviate', 'tailored', 'pending')


class ClaimsError(Exception):
    pass


def load_claims(path):
    with open(path, 'r', encoding='utf-8') as fh:
        d = json.load(fh)
    probs = []
    if not isinstance(d.get('claims'), list):
        probs.append("claims file needs a 'claims' list")
    for i, c in enumerate(d.get('claims') or []):
        where = f'claims[{i}]'
        for key in ('req_id', 'source_clause', 'statement'):
            if not str(c.get(key, '')).strip():
                probs.append(f'{where}: missing {key}')
        if c.get('status') == 'tailored' and \
                not str(c.get('tailoring_note', '')).strip():
            probs.append(f"{where}: status 'tailored' requires a "
                         f"tailoring_note (who approved what, and why)")
        if c.get('op') and c['op'] not in OPS:
            probs.append(f"{where}: op {c.get('op')!r} not in "
                         f"{sorted(OPS)}")
    if probs:
        raise ClaimsError('; '.join(probs))
    return d


def resolve_path(context, dotted):
    """Dotted lookup ('campaign.min_cycles') into nested dicts; None
    when any hop is missing. Data access only -- never eval."""
    cur = context
    for part in str(dotted).split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate(claims_doc, context):
    """-> list of evaluated claim rows (copies + achieved + status)."""
    out = []
    for c in claims_doc.get('claims', []):
        row = dict(c)
        # hand-set tailored/pending stand as written
        if c.get('status') in ('tailored', 'pending'):
            out.append(row)
            continue
        achieved = resolve_path(context, c.get('achieved_path', ''))
        row['achieved'] = achieved
        op = c.get('op')
        req = c.get('required')
        if achieved is None or op is None or req is None:
            row['status'] = 'deviate'
            row['auto_note'] = ('no achieved data resolved -- '
                                'auto-downgraded (a missing measurement '
                                'is never a pass)')
        else:
            try:
                ok = OPS[op](float(achieved), float(req))
            except (TypeError, ValueError):
                ok = achieved == req
            row['status'] = 'comply' if ok else 'deviate'
            if not ok:
                row['auto_note'] = (f'achieved {achieved!r} fails '
                                    f'{op} {req!r} -- auto-downgraded')
        out.append(row)
    return out


def html_table(rows):
    """Color-coded HTML fragment for the report."""
    colors = {'comply': '#2e7d32', 'deviate': '#a01010',
              'tailored': '#8a5a00', 'pending': '#666666'}
    body = []
    for r in rows:
        c = colors.get(r.get('status'), '#666')
        note = r.get('tailoring_note') or r.get('auto_note') or ''
        body.append(
            f"<tr><td>{r.get('req_id', '')}</td>"
            f"<td>{r.get('source_clause', '')}</td>"
            f"<td>{r.get('statement', '')}</td>"
            f"<td>{r.get('required', '')}&nbsp;{r.get('unit', '')}</td>"
            f"<td>{r.get('achieved', '')}</td>"
            f"<td style='color:{c};font-weight:700'>"
            f"{r.get('status', '')}</td>"
            f"<td>{note}</td></tr>")
    return ("<table class='compliance'><tr><th>Req</th><th>Clause</th>"
            "<th>Statement</th><th>Required</th><th>Achieved</th>"
            "<th>Status</th><th>Note</th></tr>"
            + ''.join(body) + '</table>')
