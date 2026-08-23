"""Manual environment attestation (v1: no chamber sensors exist yet).

The operator types chamber temperature and pressure; the engine records
WHO attested WHAT and WHEN, gates HV on the attested pressure (Paschen
band), and forces re-attestation when the entry goes stale
(recipe environment.stale_after_s) and at every wait_env block / HV
re-arm. This is the pluggable seam the future dedicated sensors (USB TC
logger, gauge controller) and the DEA-Characterization-Board drop into.
"""
from . import EnvironmentSource


class ManualEnv(EnvironmentSource):
    def __init__(self, clock):
        self.clock = clock
        self._t_c = None
        self._p_mbar = None
        self._operator = ''
        self._entered_iso = ''
        self._entered_t = None

    def attest(self, t_c, p_mbar, operator, entered_iso):
        self._t_c = float(t_c)
        self._p_mbar = float(p_mbar)
        self._operator = operator or 'operator'
        self._entered_iso = entered_iso
        self._entered_t = self.clock.monotonic()

    def current(self):
        if self._entered_t is None:
            return None
        return {'t_c': self._t_c, 'p_mbar': self._p_mbar,
                'p_pa': self._p_mbar * 100.0, 'source': 'manual',
                'operator': self._operator,
                'entered_iso': self._entered_iso,
                'age_s': self.clock.monotonic() - self._entered_t}

    def needs_attestation(self, stale_after_s):
        cur = self.current()
        return cur is None or cur['age_s'] > float(stale_after_s)
