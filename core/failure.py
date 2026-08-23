"""Three-tier failure-detection rule engine.

FAST   -- evaluated every monitor tick (~0.5 s) during any energized
          phase. Sustained-condition semantics like the vendored
          BreakdownWatchdog: the condition must hold for `sustain_s` of
          consecutive evaluations; one below-threshold sample resets;
          None samples are ignored without resetting. The overcurrent
          rule itself IS the vendored BreakdownWatchdog (its parameters
          come from here; its semantics are untouched).
MEDIUM -- evaluated at block boundaries (per cycle block).
SLOW   -- evaluated per interlude, on ratios against the post-break-in
          baseline interlude.

Every rule: {id, tier, metric, op, threshold, action} plus
`sustain_s` (fast), `per_kcycles` (medium), `flag_at` (slow). `flag_at`
is the AMBER "potential failure" threshold: crossing it raises a soft
flag (event row + persistent status flag, run continues until
acknowledged or worse); crossing `threshold` executes the hard action.

Actions: log < flag < warn < pause < abort. The FIRST rule to fire an
abort is latched as the run's official failure_mode; evaluation and
logging continue afterwards -- the ordering of the precursors is itself
the scientific result.
"""
import copy

ACTIONS = ('log', 'flag', 'warn', 'pause', 'abort')
TIERS = ('fast', 'medium', 'slow')

# Defaults per the plan; every value per-recipe-overridable via
# failure_rules (merged by rule id). Ratio metrics are vs the
# post-break-in baseline interlude.
DEFAULT_RULES = {
    'fast': [
        # overcurrent: parameters for the vendored BreakdownWatchdog
        # (trip |I-baseline| >= threshold uA sustained sustain_s).
        {'id': 'overcurrent', 'metric': 'i_dev_ua', 'op': '>=',
         'threshold': 100.0, 'sustain_s': 3.0, 'action': 'abort'},
        # commanded-vs-readback drive error (silent clipping detector).
        {'id': 'drive_fidelity', 'metric': 'v_err_frac', 'op': '>',
         'threshold': 0.05, 'sustain_s': 5.0, 'action': 'pause'},
        # scope unreadable: 10 s -> loud BLIND warn (policy 2026-07-25,
        # replicated), 60 s -> pause at 0 kV (new default for unattended
        # lifecycle runs; a recipe may relax it back to warn).
        {'id': 'monitor_loss', 'metric': 'blind_s', 'op': '>=',
         'threshold': 60.0, 'sustain_s': 0.0, 'action': 'pause'},
    ],
    'medium': [
        {'id': 'transient_cap', 'metric': 'soft_flags_per_kcycle',
         'op': '>', 'threshold': 5.0, 'action': 'pause'},
        {'id': 'env_band', 'metric': 'env_out_of_band', 'op': '>=',
         'threshold': 1.0, 'action': 'pause'},
        {'id': 'ipk_headroom', 'metric': 'ipk_frac', 'op': '>',
         'threshold': 0.9, 'action': 'pause'},
    ],
    'slow': [
        {'id': 'amplitude', 'metric': 'disp_ratio', 'op': '<',
         'threshold': 0.80, 'flag_at': 0.90, 'action': 'abort'},
        {'id': 'capacitance', 'metric': 'c_ratio_dev', 'op': '>',
         'threshold': 0.20, 'flag_at': 0.10, 'action': 'abort'},
        {'id': 'leakage', 'metric': 'leak_ratio', 'op': '>',
         'threshold': 100.0, 'flag_at': 10.0, 'action': 'abort'},
        {'id': 'zero_drift', 'metric': 'zero_drift', 'op': '>',
         'threshold': 0.10, 'flag_at': 0.05, 'action': 'warn'},
        {'id': 'wrinkle', 'metric': 'wrinkle_idx', 'op': '>',
         'threshold': 2.0, 'flag_at': 1.5, 'action': 'flag'},
    ],
}

_OPS = {
    '>': lambda v, t: v > t,
    '>=': lambda v, t: v >= t,
    '<': lambda v, t: v < t,
    '<=': lambda v, t: v <= t,
    '==': lambda v, t: v == t,
}


class RuleConfigError(Exception):
    pass


def validate_rules(overrides):
    """Validate a recipe's failure_rules override block; returns the list
    of problems (empty = fine). Overrides are merged BY RULE ID over
    DEFAULT_RULES; a new id creates a new rule and must be complete."""
    probs = []
    if not isinstance(overrides, dict):
        return [f"failure_rules must be an object, got "
                f"{type(overrides).__name__}"]
    for tier, rules in overrides.items():
        if tier not in TIERS:
            probs.append(f"failure_rules: unknown tier '{tier}' "
                         f"(one of {TIERS})")
            continue
        if not isinstance(rules, list):
            probs.append(f"failure_rules.{tier} must be a list")
            continue
        known = {r['id'] for r in DEFAULT_RULES[tier]}
        for r in rules:
            if not isinstance(r, dict) or 'id' not in r:
                probs.append(f"failure_rules.{tier}: every rule needs an "
                             f"'id' ({r!r})")
                continue
            rid = r['id']
            new_rule = rid not in known
            need = {'metric', 'op', 'threshold', 'action'} if new_rule \
                else set()
            missing = need - set(r)
            if missing:
                probs.append(f"failure_rules.{tier}.{rid}: new rule is "
                             f"missing {sorted(missing)}")
            if 'op' in r and r['op'] not in _OPS:
                probs.append(f"failure_rules.{tier}.{rid}: op '{r['op']}' "
                             f"not one of {sorted(_OPS)}")
            if 'action' in r and r['action'] not in ACTIONS:
                probs.append(f"failure_rules.{tier}.{rid}: action "
                             f"'{r['action']}' not one of {ACTIONS}")
            allowed = {'id', 'metric', 'op', 'threshold', 'action',
                       'sustain_s', 'per_kcycles', 'flag_at'}
            extra = set(r) - allowed
            if extra:
                probs.append(f"failure_rules.{tier}.{rid}: unknown keys "
                             f"{sorted(extra)}")
    return probs


def merge_rules(overrides):
    """DEFAULT_RULES with per-id overrides applied. Raises on invalid."""
    probs = validate_rules(overrides or {})
    if probs:
        raise RuleConfigError('; '.join(probs))
    merged = copy.deepcopy(DEFAULT_RULES)
    for tier, rules in (overrides or {}).items():
        by_id = {r['id']: r for r in merged[tier]}
        for r in rules:
            if r['id'] in by_id:
                by_id[r['id']].update(r)
            else:
                merged[tier].append(dict(r))
    return merged


class Firing:
    """One rule outcome. is_flag=True is the AMBER soft flag."""

    def __init__(self, rule_id, tier, action, value, threshold,
                 is_flag=False, message=''):
        self.rule_id = rule_id
        self.tier = tier
        self.action = action
        self.value = value
        self.threshold = threshold
        self.is_flag = is_flag
        self.message = message or (
            f"{tier.upper()} rule '{rule_id}': value {value!r} vs "
            f"{'flag' if is_flag else 'trip'} threshold {threshold!r}")

    def __repr__(self):
        return (f"Firing({self.rule_id}, {self.action}, "
                f"flag={self.is_flag}, value={self.value!r})")


class RuleEngine:
    """Evaluates the merged rulepack. Stateless per call except FAST
    sustain tracking and the first-cause latch."""

    def __init__(self, rules, clock=None):
        self.rules = rules
        self._sustain = {}           # rule_id -> over_since (fast tier)
        self._streak_fired = set()   # fast rules fired this streak
        self.first_abort = None      # Firing latched as failure_mode
        self._flagged = set()        # slow rule ids already AMBER-flagged

    def rule(self, tier, rule_id):
        for r in self.rules[tier]:
            if r['id'] == rule_id:
                return r
        return None

    # ---- fast ----------------------------------------------------------
    def eval_fast(self, t_s, metrics):
        """metrics: {metric_name: value or None}. Returns [Firing].

        Sustained semantics per rule: condition true starts/continues the
        streak; condition false resets; value None neither (BreakdownWatchdog
        convention -- no evidence either way)."""
        out = []
        for r in self.rules['fast']:
            if r['id'] == 'overcurrent':
                continue      # lives in the vendored watchdog, reported
                              # via report_watchdog_trip()
            val = metrics.get(r['metric'])
            if val is None:
                continue
            over = _OPS[r['op']](val, r['threshold'])
            key = r['id']
            if over:
                since = self._sustain.setdefault(key, t_s)
                # >= so a sustain_s of 0 fires on its first observation
                # (monitor_loss); fires ONCE per continuous streak --
                # the condition must clear before it can fire again.
                if (t_s - since >= float(r.get('sustain_s', 0.0))
                        and key not in self._streak_fired):
                    self._streak_fired.add(key)
                    out.append(self._fire(r, 'fast', val))
            else:
                self._sustain.pop(key, None)
                self._streak_fired.discard(key)
        return out

    def report_watchdog_trip(self, value_ua):
        """The vendored BreakdownWatchdog confirmed a trip; wrap it as
        the overcurrent rule's firing so first-cause latching and the
        event log treat it like every other rule."""
        r = self.rule('fast', 'overcurrent')
        return self._fire(r, 'fast', value_ua)

    # ---- medium / slow -------------------------------------------------
    def eval_medium(self, metrics):
        return self._eval_plain('medium', metrics)

    def eval_slow(self, metrics):
        """Slow tier adds flag_at: crossing flag_at (but not threshold)
        emits an is_flag Firing with action 'flag' -- once per rule until
        the hard threshold clears it."""
        out = []
        for r in self.rules['slow']:
            val = metrics.get(r['metric'])
            if val is None:
                continue
            if _OPS[r['op']](val, r['threshold']):
                out.append(self._fire(r, 'slow', val))
            elif 'flag_at' in r and _OPS[r['op']](val, r['flag_at']):
                if r['id'] not in self._flagged:
                    self._flagged.add(r['id'])
                    out.append(Firing(r['id'], 'slow', 'flag', val,
                                      r['flag_at'], is_flag=True))
        return out

    def _eval_plain(self, tier, metrics):
        out = []
        for r in self.rules[tier]:
            val = metrics.get(r['metric'])
            if val is None:
                continue
            if _OPS[r['op']](val, r['threshold']):
                out.append(self._fire(r, tier, val))
        return out

    def _fire(self, r, tier, value):
        f = Firing(r['id'], tier, r['action'], value, r['threshold'])
        if r['action'] == 'abort' and self.first_abort is None:
            self.first_abort = f
        return f

    @property
    def failure_mode(self):
        return self.first_abort.rule_id if self.first_abort else ''
