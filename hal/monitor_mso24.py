"""Live MonitorSource: Trek monitor BNCs on the Tek MSO24.

Sign convention: trek_sign applies to BOTH monitors alike (D5
2026-08-04 -- the Trek inverts V_Out and I_Out together), applied here
so everything downstream sees engineering units. Statuses pass the
vendored vocabulary through: 'offscreen' is the Tek 9.9E37 sentinel =
clipped-but-real (over-trip evidence, not monitoring loss).

check_window wraps the vendored monitor_problems/monitor_fix_plan
(2026-07-25 bench-incident lineage: CH2 at 2.6 mV/div for a 0-10 V
monitor passed silently for five runs) and ADDS the horizontal check:
the measurement window must span >= 2 carrier periods or MEAN / PK2PK /
FREQUENCY of the cycling waveform are meaningless.
"""
import sldea_profile  # vendored

from core import feasibility as _feasibility

from . import MonitorSource


class MSO24Monitor(MonitorSource):
    def __init__(self, scope, v_channel=2, i_channel=3, trek_sign=1.0):
        self.scope = scope
        self.vch = int(v_channel)
        self.ich = int(i_channel)
        self.trek_sign = 1.0 if trek_sign >= 0 else -1.0

    def connected(self):
        return self.scope is not None

    # ---- reads ----------------------------------------------------------
    def read_kv(self):
        try:
            v, st = self.scope.measure_raw('MEAN', self.vch)
        except Exception:
            return None, 'error'
        if v is None:
            return None, st
        return self.trek_sign * sldea_profile.measured_kv(v), st

    def read_ua(self):
        try:
            v, st = self.scope.measure_raw('MEAN', self.ich)
        except Exception:
            return None, 'error'
        if v is None:
            return None, st
        return self.trek_sign * sldea_profile.measured_ua(v), st

    def read_stat(self, kind, which):
        ch = self.vch if which == 'v' else self.ich
        try:
            v, st = self.scope.measure_raw(kind, ch)
        except Exception:
            return None, 'error'
        if v is None:
            return None, st
        if kind == 'FREQUENCY':          # Hz regardless of channel
            return v, st
        conv = sldea_profile.measured_kv if which == 'v' \
            else sldea_profile.measured_ua
        s = self.trek_sign
        if kind == 'PK2PK':              # span is sign-invariant
            return abs(conv(v)), st
        val = s * conv(v)
        # sign flips swap MAX and MIN; report what the caller asked for
        if s < 0 and kind in ('MAXIMUM', 'MINIMUM'):
            other = 'MINIMUM' if kind == 'MAXIMUM' else 'MAXIMUM'
            try:
                v2, st2 = self.scope.measure_raw(other, ch)
                if v2 is not None:
                    return s * conv(v2), st2
            except Exception:
                pass
        return val, st

    def capture_waveform(self, which):
        ch = self.vch if which == 'v' else self.ich
        try:
            return self.scope.get_waveform(ch)
        except Exception:
            return None

    # ---- window verification (gui.py:3284 lineage) ----------------------
    def _q(self, ch, cmd):
        try:
            return float(self.scope.ask(f'CH{ch}:{cmd}'))
        except Exception:
            return None

    def check_window(self, max_kv, trip_ua, freq_hz):
        qs = {}
        for role, ch in (('v', self.vch), ('i', self.ich)):
            for key, cmd in (('scale', 'SCALE?'),
                             ('atten', 'PROBEFUNC:EXTATTEN?'),
                             ('position', 'POSITION?'),
                             ('offset', 'OFFSET?')):
                qs[f'{role}_{key}'] = self._q(ch, cmd)
        problems = sldea_profile.monitor_problems(
            max_kv, v_scale=qs['v_scale'], v_atten=qs['v_atten'],
            i_scale=qs['i_scale'], i_atten=qs['i_atten'],
            breakdown_ua=trip_ua,
            v_position=qs['v_position'], v_offset=qs['v_offset'],
            i_position=qs['i_position'], i_offset=qs['i_offset'],
            v_sign=self.trek_sign)
        problems = list(problems)
        # NEW horizontal check: window must span >= 2 carrier periods
        h_scale = None
        try:
            h_scale = float(self.scope.ask('HORIZONTAL:SCALE?'))
        except Exception:
            pass
        if h_scale is not None and freq_hz and freq_hz > 0:
            window_s = 10.0 * h_scale        # Tek: 10 divisions
            if not _feasibility.scope_window_horizontal_ok(freq_hz,
                                                           window_s):
                problems.append(
                    f'horizontal window {window_s:g} s spans under 2 '
                    f'periods of the {freq_hz:g} Hz drive -- MEAN/'
                    f'PK2PK/FREQUENCY reads would be meaningless '
                    f'(need >= {2.0 / freq_hz:g} s)')
        plan = sldea_profile.monitor_fix_plan(
            max_kv, trip_ua, v_sign=self.trek_sign) if problems \
            else None
        if plan is not None and h_scale is not None and freq_hz:
            plan = dict(plan, h_scale=max(2.0 / freq_hz / 10.0,
                                          0.4 / freq_hz))
        return problems, plan

    def apply_fix(self, plan):
        if not plan:
            return False
        try:
            for ch, sc, pos in (
                    (self.vch, plan['v_scale'], plan['v_position']),
                    (self.ich, plan['i_scale'], plan['i_position'])):
                self.scope.write(f"CH{ch}:PROBEFUNC:EXTATTEN "
                                 f"{plan['atten']:g}")
                self.scope.write(f'CH{ch}:SCALE {sc:g}')
                self.scope.write(f'CH{ch}:OFFSET 0')
                self.scope.write(f"CH{ch}:POSITION {pos:g}")
                self.scope.write(f'CH{ch}:COUPLING DC')
                self.scope.write(f'SELECT:CH{ch} ON')
            if plan.get('h_scale'):
                self.scope.write(f"HORIZONTAL:SCALE "
                                 f"{plan['h_scale']:g}")
            return True
        except Exception:
            return False

    def setup_lines(self):
        out = []
        for role, ch, label in (('v', self.vch, 'V_Out'),
                                ('i', self.ich, 'I_Out')):
            sc = self._q(ch, 'SCALE?')
            pos = self._q(ch, 'POSITION?')
            att = self._q(ch, 'PROBEFUNC:EXTATTEN?')
            out.append(f'CH{ch} ({label}): scale '
                       f'{sc if sc is not None else "?"} V/div, '
                       f'position {pos if pos is not None else "?"}, '
                       f'ext atten {att if att is not None else "?"}')
        return out
