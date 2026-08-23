"""Censoring-correct Weibull survival analysis for cycles-to-failure.

The detail most homebrew tools get wrong, handled first-class:
specimens that reached the cycle cap without failing (or were pulled
for unrelated reasons) are RIGHT-CENSORED suspensions. They enter the
likelihood and the rank adjustment -- never dropped, never counted as
failures -- otherwise eta and B10 come out badly biased low.

Two estimators, both reported:

- **MRR** (median-rank regression): Johnson-adjusted mean order numbers
  for suspensions, Bernard's approximation F = (AR - 0.3)/(n + 0.4),
  X-on-Y regression (ln t on ln(-ln(1-F)) -- the Abernethy convention:
  time is the error-bearing variable). Stable and visual for small n.
- **MLE** with right censoring: for fixed beta, eta is closed-form;
  the beta score equation is solved with brentq. Better with heavy
  censoring; the headline when suspensions exist.

Headline number: **B10** (cycles at 10% failure probability = 90%
reliability) with a 90% likelihood-ratio confidence interval (profile
likelihood in the (beta, B10) parameterization -- LR intervals behave
far better than Wald at the n <= 10 this lab actually runs).

beta itself is a finding: beta < 1 infant mortality (workmanship),
~1 random, > 1 wear-out.
"""
import math

import numpy as np

try:
    from scipy.optimize import brentq
    from scipy.stats import chi2
except ImportError:                      # pragma: no cover
    brentq = None

CHI2_90_1DOF = 2.705543454095404         # chi2.ppf(0.90, 1)


class WeibullError(Exception):
    pass


def _clean(rows):
    """rows: [{'cycles': t, 'status': 'failed'|'suspended', ...}] ->
    (times, is_failure) sorted ascending, validation applied."""
    if not rows:
        raise WeibullError('no specimens')
    times, fail = [], []
    for r in rows:
        t = float(r['cycles'])
        if t <= 0:
            raise WeibullError(f"non-positive cycles {t} "
                               f"({r.get('specimen_id', '?')})")
        st = r['status']
        if st not in ('failed', 'suspended'):
            raise WeibullError(f"status {st!r} is neither failed nor "
                               f"suspended")
        times.append(t)
        fail.append(st == 'failed')
    order = np.argsort(times, kind='stable')
    return (np.asarray(times, float)[order],
            np.asarray(fail, bool)[order])


# ---------------------------------------------------------------------------
# median-rank regression
# ---------------------------------------------------------------------------

def median_ranks(times, fail):
    """Johnson-adjusted ranks + Bernard median ranks for the FAILURES.
    Returns (t_fail, F) arrays."""
    n = len(times)
    prev_ar = 0.0
    out_t, out_f = [], []
    for k, (t, is_f) in enumerate(zip(times, fail), start=1):
        if not is_f:
            continue
        # Johnson increment: reverse rank of this position
        inc = (n + 1 - prev_ar) / (n + 2 - k)
        ar = prev_ar + inc
        prev_ar = ar
        out_t.append(t)
        out_f.append((ar - 0.3) / (n + 0.4))
    return np.asarray(out_t), np.asarray(out_f)


def fit_mrr(rows):
    times, fail = _clean(rows)
    if fail.sum() < 2:
        raise WeibullError(f'MRR needs >= 2 failures '
                           f'(have {int(fail.sum())})')
    t_f, F = median_ranks(times, fail)
    y = np.log(-np.log(1.0 - F))
    x = np.log(t_f)
    # X-on-Y: ln t = a * y + b  (time carries the scatter)
    a, b = np.polyfit(y, x, 1)
    beta = 1.0 / a
    eta = math.exp(b)
    # correlation coefficient of the plot (goodness diagnostic)
    r = float(np.corrcoef(x, y)[0, 1]) if len(x) > 2 else 1.0
    return {'method': 'MRR', 'beta': beta, 'eta': eta,
            'b10': b_life(beta, eta, 0.10),
            'n': len(times), 'r_failures': int(fail.sum()),
            'suspensions': int((~fail).sum()), 'plot_r': r}


# ---------------------------------------------------------------------------
# maximum likelihood with right censoring
# ---------------------------------------------------------------------------

def _eta_hat(beta, times, fail):
    r = fail.sum()
    return (np.sum(times ** beta) / r) ** (1.0 / beta)


def _beta_score(beta, times, fail):
    r = fail.sum()
    tb = times ** beta
    return (1.0 / beta
            + float(np.sum(np.log(times[fail]))) / r
            - float(np.sum(tb * np.log(times)) / np.sum(tb)))


def loglik(beta, eta, times, fail):
    z = (times / eta) ** beta
    ll = -float(np.sum(z))
    tf = times[fail]
    ll += float(np.sum(np.log(beta / eta)
                       + (beta - 1.0) * np.log(tf / eta)))
    return ll


def fit_mle(rows):
    if brentq is None:
        raise WeibullError('scipy required for MLE')
    times, fail = _clean(rows)
    r = int(fail.sum())
    if r < 2:
        raise WeibullError(f'MLE needs >= 2 failures (have {r})')
    # Normalize by the geometric mean: beta is scale-invariant and raw
    # cycle counts (1e4..1e6) overflow t**beta long before the bracket
    # edge (beta=80 gave NaN, 2026-08-23). eta rescales back after.
    tau = float(np.exp(np.mean(np.log(times))))
    u = times / tau
    # bracket the score root
    lo, hi = 0.02, 80.0
    with np.errstate(over='ignore', invalid='ignore'):
        slo, shi = _beta_score(lo, u, fail), _beta_score(hi, u, fail)
    if not (np.isfinite(slo) and np.isfinite(shi)) or slo * shi > 0:
        raise WeibullError('beta score has no root in [0.02, 80] -- '
                           'degenerate data (all equal times?)')
    beta = brentq(_beta_score, lo, hi, args=(u, fail), xtol=1e-10)
    eta = _eta_hat(beta, u, fail) * tau
    ll = loglik(beta, eta, times, fail)
    out = {'method': 'MLE', 'beta': float(beta), 'eta': float(eta),
           'b10': b_life(beta, eta, 0.10), 'loglik': ll,
           'n': len(times), 'r_failures': r,
           'suspensions': int((~fail).sum())}
    try:
        out['b10_ci90'] = b10_lr_interval(times, fail, beta, eta, ll)
    except Exception:
        out['b10_ci90'] = (None, None)
    return out


def b_life(beta, eta, frac=0.10):
    """B-life: time at cumulative failure fraction `frac`."""
    return float(eta * (-math.log(1.0 - frac)) ** (1.0 / beta))


def _profile_ll_b10(b10, times, fail):
    """Max log-likelihood with B10 held fixed (profile over beta)."""
    k10 = -math.log(0.9)

    def nll(beta):
        eta = b10 / (k10 ** (1.0 / beta))
        return -loglik(beta, eta, times, fail)

    # golden-section over log-beta (1-D, smooth)
    lo, hi = math.log(0.02), math.log(80.0)
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c = b - phi * (b - a)
    d = a + phi * (b - a)
    fc, fd = nll(math.exp(c)), nll(math.exp(d))
    for _ in range(80):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - phi * (b - a)
            fc = nll(math.exp(c))
        else:
            a, c, fc = c, d, fd
            d = a + phi * (b - a)
            fd = nll(math.exp(d))
    return -min(fc, fd)


def b10_lr_interval(times, fail, beta_hat, eta_hat, ll_hat,
                    conf_chi2=CHI2_90_1DOF):
    """90% likelihood-ratio bounds on B10."""
    b10_hat = b_life(beta_hat, eta_hat, 0.10)

    def g(b10):
        return 2.0 * (ll_hat - _profile_ll_b10(b10, times, fail)) \
            - conf_chi2

    lo = hi = None
    # search outward for sign changes
    for f in (0.5, 0.2, 0.05, 0.01):
        if g(b10_hat * f) > 0:
            lo = brentq(g, b10_hat * f, b10_hat, xtol=b10_hat * 1e-5)
            break
    for f in (2.0, 5.0, 20.0, 100.0):
        if g(b10_hat * f) > 0:
            hi = brentq(g, b10_hat, b10_hat * f, xtol=b10_hat * 1e-5)
            break
    return (None if lo is None else float(lo),
            None if hi is None else float(hi))


# ---------------------------------------------------------------------------
# the combined report + probability-plot data
# ---------------------------------------------------------------------------

def analyze(rows):
    """Both fits + the probability-plot data structure. MLE is the
    headline when suspensions exist (and both are shown regardless)."""
    times, fail = _clean(rows)
    out = {'n': len(times), 'r_failures': int(fail.sum()),
           'suspensions': int((~fail).sum()),
           'mrr': None, 'mle': None, 'headline': None}
    errs = []
    for name, fit in (('mrr', fit_mrr), ('mle', fit_mle)):
        try:
            out[name] = fit(rows)
        except WeibullError as e:
            errs.append(f'{name}: {e}')
    out['errors'] = errs
    out['headline'] = out['mle'] or out['mrr']
    # plot data
    t_f, F = (median_ranks(times, fail) if fail.sum() else
              (np.array([]), np.array([])))
    out['plot'] = {
        'fail_t': t_f.tolist(), 'fail_F': F.tolist(),
        'susp_t': times[~fail].tolist(),
    }
    return out


def summary_line(res):
    h = res.get('headline')
    if not h:
        return ('no Weibull fit possible: '
                + '; '.join(res.get('errors') or ['insufficient data']))
    ci = h.get('b10_ci90') or (None, None)
    ci_txt = ''
    if ci[0] is not None:
        ci_txt = f' (90% CI {ci[0]:,.0f}..{ci[1]:,.0f})' \
            if ci[1] is not None else f' (90% LCB {ci[0]:,.0f})'
    beta = h['beta']
    regime = ('infant mortality' if beta < 0.8 else
              'random' if beta < 1.2 else 'wear-out')
    return (f"B10 = {h['b10']:,.0f} cycles{ci_txt}  |  "
            f"beta {beta:.2f} ({regime}), eta {h['eta']:,.0f}  |  "
            f"{h['method']}, n={h['n']} "
            f"({h['r_failures']} failures, {h['suspensions']} susp)")
