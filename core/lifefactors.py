"""Life-test factor calculators: how many cycles must be demonstrated.

Pure math, advisory only -- printed into setup.txt at VALIDATE and shown
in the GUI so the operator sees "this recipe demonstrates X of the Y
required for claim Z". Two published conventions, materially different:

NASA-STD-5017 (Design & Development Requirements for Mechanisms) 4.13.3:
  non-human-rated: N_test = 2 x operational + 4 x ground
                            + 4 x (functional + environmental + run-in)
  human-rated:     4 x everything
  5.7(c): any margin cycles are added BEFORE the factor is applied.

ECSS-E-ST-33-01C Rev.2 Table 4-4: piecewise tiered factors, accumulated
per bin (NOT one factor on the total), ground testing minimum 10 cycles:
  ground:   1..1e3 x4      | 1e3+1..1e5 x2 | >1e5 x1.25
  in-orbit: 1..10  x10     | 11..1e3   x4  | 1e3+1..1e5 x2 | >1e5 x1.25
Worked example from the standard: 15 ground + 100 in-orbit ->
15*4 + (10*10 + 90*4) = 60 + 460 = 520.
"""

ECSS_GROUND_MIN_CYCLES = 10
# (bin upper edge inclusive, factor); float('inf') closes each table.
_ECSS_GROUND = ((1_000, 4.0), (100_000, 2.0), (float('inf'), 1.25))
_ECSS_ORBIT = ((10, 10.0), (1_000, 4.0), (100_000, 2.0),
               (float('inf'), 1.25))


def nasa5017(operational, ground=0, functional=0, human_rated=False,
             margin=0):
    """Required demonstrated cycles per NASA-STD-5017 4.13.3 / 5.7(c).

    `functional` bundles functional + environmental + run-in cycles (the
    standard applies the same 4x to all three). `margin` is added to the
    operational count BEFORE the factor (5.7(c))."""
    op = float(operational) + float(margin)
    op_factor = 4.0 if human_rated else 2.0
    total = op_factor * op + 4.0 * float(ground) + 4.0 * float(functional)
    return {
        'convention': 'NASA-STD-5017 '
                      + ('human-rated' if human_rated else 'non-human-rated'),
        'required_cycles': int(round(total)),
        'terms': {
            f'{op_factor:g} x (operational {operational:g} '
            f'+ margin {margin:g})': op_factor * op,
            f'4 x ground {ground:g}': 4.0 * float(ground),
            f'4 x functional/env/run-in {functional:g}':
                4.0 * float(functional),
        },
    }


def _tiered(n, table):
    """Piecewise-accumulated factored cycles over one ECSS tier table."""
    n = float(n)
    total = 0.0
    lower = 0.0
    for upper, factor in table:
        if n <= lower:
            break
        span = min(n, upper) - lower
        total += span * factor
        lower = upper
    return total


def ecss(ground=0, orbit=0):
    """Required demonstrated cycles per ECSS-E-ST-33-01C Rev.2 Table 4-4.

    Ground testing carries a 10-cycle minimum before factoring (the
    standard's own Example 2: 2 ground cycles are counted as 10)."""
    g = max(float(ground), float(ECSS_GROUND_MIN_CYCLES)) if ground else \
        float(ECSS_GROUND_MIN_CYCLES)
    g_total = _tiered(g, _ECSS_GROUND)
    o_total = _tiered(orbit, _ECSS_ORBIT)
    return {
        'convention': 'ECSS-E-ST-33-01C Rev.2 Table 4-4',
        'required_cycles': int(round(g_total + o_total)),
        'terms': {
            f'ground {ground:g} (min {ECSS_GROUND_MIN_CYCLES}) tiered':
                g_total,
            f'in-orbit {orbit:g} tiered': o_total,
        },
    }


def advisory_lines(recipe_max_cycles, operational, ground=0, functional=0,
                   orbit=None):
    """setup.txt / GUI lines comparing the recipe's cycle cap against
    both conventions. orbit defaults to the operational count (the usual
    reading for an on-orbit actuation mechanism)."""
    n5017 = nasa5017(operational, ground, functional)
    necss = ecss(ground=ground, orbit=operational if orbit is None
                 else orbit)
    lines = []
    for res in (n5017, necss):
        req = res['required_cycles']
        ok = recipe_max_cycles >= req
        lines.append(
            f"{res['convention']}: requires {req:,} demonstrated cycles "
            f"-- recipe cap {recipe_max_cycles:,} "
            f"{'SATISFIES' if ok else 'FALLS SHORT of'} it")
    return lines
