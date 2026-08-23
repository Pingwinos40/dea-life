"""Mock rig: the whole drive/measure chain as deterministic physics.

One MockRig owns the simulated specimen + instruments; the four HAL
sources are views onto it, so what the drive commands is what the
monitor reads and what the vision measures -- consistent by
construction. Seeded RNG + SimClock make every run byte-reproducible,
which is what the engine test suite pins its golden files to.

Degradation model (all per-1000-cycles, all overridable via `script`):
  leak_growth   multiplicative leakage growth   (default 1.02)
  amp_decay     multiplicative amplitude decay  (default 0.9995)
  c_drift       multiplicative capacitance drift (default 1.0)
  breakdown_at_cycle   None or N: past N cycles the specimen arcs --
                       current pins at `breakdown_ua` while energized,
                       which the vendored watchdog then confirms.
  zero_fail     True: MockDrive.zero() fails (NOT_ZEROED path test)

The capacitive current term is physically integrable: read_ua includes
C * dV/dt between THIS read and the previous one, so the executor's
charge-integration C estimate recovers c_nf exactly in the mock.
"""
import math
import random

import numpy as np

from . import (DriveSource, MonitorSource, Camera, EnvironmentSource,
               VisionAdapter, HALBundle)

DEFAULT_SCRIPT = {
    'leak_growth': 1.02,
    'amp_decay': 0.9995,
    'c_drift': 1.0,
    'breakdown_at_cycle': None,
    'breakdown_ua': 500.0,
    'zero_fail': False,
    'noise_ua': 0.4,
    'i_offset_ua': -16.0,       # the 07-29 campaign's stiff instrument
                                # offset -- exercises the baseline learn
    'drive_err_frac': 0.0,      # simulated amp infidelity (clipping)
}


class MockRig:
    def __init__(self, clock, c_nf=5.0, leak0_ua=2.0, amp0=100.0,
                 amp0_rest=0.0, seed=1234, script=None):
        self.clock = clock
        self.c_nf0 = float(c_nf)
        self.leak0_ua = float(leak0_ua)
        self.amp0 = float(amp0)          # modeled peak-displacement value
        self.amp0_rest = float(amp0_rest)
        self.script = dict(DEFAULT_SCRIPT, **(script or {}))
        self.rng = random.Random(seed)
        # live state
        self.kv_now = 0.0
        self.output_on = False
        self.cycles = 0
        self.cycling = None       # {'waveform','freq_hz','v_pk','v_min'}
        self._burst_end_t = None
        self._burst_t0 = None
        self._burst_dur = None
        self._burst_pending = 0
        self._last_read_t = None
        self._last_read_kv = 0.0

    # ---- physics -------------------------------------------------------
    def _advance(self):
        """Complete any burst whose nominal duration has elapsed."""
        if self._burst_end_t is not None and \
                self.clock.monotonic() >= self._burst_end_t:
            self.cycles += self._burst_pending
            self._burst_pending = 0
            self._burst_end_t = None
            self.kv_now = self.cycling['v_min'] if self.cycling else 0.0

    def cycles_effective(self):
        """Cycles INCLUDING the in-flight fraction of a running burst --
        the specimen ages continuously, so a scripted breakdown_at_cycle
        lands MID-burst (which is what the partial-credit and trip paths
        exist for), not politely at a chunk boundary."""
        self._advance()
        if self._burst_end_t is None or not self._burst_dur:
            return self.cycles
        frac = 1.0 - max(0.0, (self._burst_end_t
                               - self.clock.monotonic())) \
            / self._burst_dur
        return self.cycles + self._burst_pending * min(1.0, frac)

    def _k(self):
        return self.cycles_effective() / 1000.0

    def broken(self):
        at = self.script['breakdown_at_cycle']
        return at is not None and self.cycles_effective() >= at

    def amp_now(self):
        return self.amp0 * (self.script['amp_decay'] ** self._k()) * \
            (0.0 if self.broken() else 1.0)

    def c_now(self):
        return self.c_nf0 * (self.script['c_drift'] ** self._k())

    def leak_now(self, kv):
        if kv <= 0:
            return 0.0
        return self.leak0_ua * kv * (self.script['leak_growth']
                                     ** self._k())

    def bursting(self):
        self._advance()
        return self._burst_end_t is not None

    def instantaneous_kv(self):
        """kV at this instant: DC level, or the cycling waveform's value
        while a burst is in flight."""
        self._advance()
        if self._burst_end_t is None or not self.cycling:
            return self.kv_now
        cyc = self.cycling
        t_left = self._burst_end_t - self.clock.monotonic()
        phase = 2.0 * math.pi * cyc['freq_hz'] * t_left
        mid = 0.5 * (cyc['v_pk'] + cyc['v_min'])
        amp = 0.5 * (cyc['v_pk'] - cyc['v_min'])
        # start phase 270 deg (idles at minimum) -- the STPS convention
        return mid + amp * math.sin(1.5 * math.pi - phase)

    # ---- monitor reads -------------------------------------------------
    def read_current_ua(self):
        """One I_Out sample: leakage + capacitive dV/dt term + offset +
        noise; pinned high when broken and energized."""
        self._advance()
        now = self.clock.monotonic()
        kv = self.instantaneous_kv()
        energized = self.output_on and (kv > 0 or self.bursting())
        if self.broken() and energized:
            ua = self.script['breakdown_ua']
        else:
            i_cap = 0.0
            if self._last_read_t is not None:
                dt = now - self._last_read_t
                if dt > 0:
                    # nF * kV / s = uA
                    i_cap = self.c_now() * (kv - self._last_read_kv) / dt
            ua = self.leak_now(kv) + i_cap
        self._last_read_t = now
        self._last_read_kv = kv
        return (ua + self.script['i_offset_ua']
                + self.rng.gauss(0.0, self.script['noise_ua']))

    def read_voltage_kv(self):
        self._advance()
        kv = self.instantaneous_kv()
        err = self.script['drive_err_frac']
        return kv * (1.0 - err) + self.rng.gauss(0.0, 0.002)


class MockDrive(DriveSource):
    def __init__(self, rig):
        self.rig = rig
        self.zero_calls = 0

    def connected(self):
        return True

    def _clamp(self, kv):
        cap = self.specimen_cap_kv
        return kv if cap is None else min(kv, float(cap))

    def set_kv(self, kv):
        self.rig._advance()
        self.rig.output_on = True
        self.rig.kv_now = self._clamp(float(kv))

    def configure_cycle(self, waveform, freq_hz, v_pk_kv, v_min_kv):
        self.rig.cycling = {'waveform': waveform,
                            'freq_hz': float(freq_hz),
                            'v_pk': self._clamp(float(v_pk_kv)),
                            'v_min': float(v_min_kv)}
        self.rig.output_on = True
        self.rig.kv_now = float(v_min_kv)      # idle at minimum (STPS)

    def fire_burst(self, n_cycles):
        cyc = self.rig.cycling
        dur = n_cycles / cyc['freq_hz']
        self.rig._burst_t0 = self.rig.clock.monotonic()
        self.rig._burst_dur = dur
        self.rig._burst_end_t = self.rig._burst_t0 + dur
        self.rig._burst_pending = int(n_cycles)
        return dur

    def end_cycle(self):
        self.rig._advance()
        self.rig.cycling = None
        self.rig.kv_now = 0.0

    def zero(self):
        self.zero_calls += 1
        if self.rig.script['zero_fail']:
            return False
        # A mid-burst kill banks the in-flight fraction the specimen
        # actually experienced; the executor credits its own f * elapsed
        # estimate independently ('estimated' counting).
        self.rig.cycles = int(self.rig.cycles_effective())
        self.rig._burst_end_t = None
        self.rig._burst_pending = 0
        self.rig.cycling = None
        self.rig.kv_now = 0.0
        self.rig.output_on = False
        return True

    def describe(self):
        return ['mock drive (deterministic rig)']


class MockMonitor(MonitorSource):
    def __init__(self, rig):
        self.rig = rig
        self.fail_reads = False       # test hook: simulate a dead scope

    def connected(self):
        return True

    def read_kv(self):
        if self.fail_reads:
            return None, 'error'
        return self.rig.read_voltage_kv(), 'ok'

    def read_ua(self):
        if self.fail_reads:
            return None, 'error'
        return self.rig.read_current_ua(), 'ok'

    def read_stat(self, kind, which):
        if self.fail_reads:
            return None, 'error'
        rig = self.rig
        rig._advance()
        cyc = rig.cycling
        if kind == 'FREQUENCY':
            if rig.bursting() and cyc:
                return cyc['freq_hz'], 'ok'
            return None, 'invalid'
        err = 1.0 - rig.script['drive_err_frac']
        if cyc and rig.bursting():
            mid = 0.5 * (cyc['v_pk'] + cyc['v_min']) * err
            span = (cyc['v_pk'] - cyc['v_min']) * err
            table_v = {'MEAN': mid, 'PK2PK': span,
                       'MAXIMUM': cyc['v_pk'] * err,
                       'MINIMUM': cyc['v_min'] * err}
        else:
            kv = rig.instantaneous_kv() * err
            table_v = {'MEAN': kv, 'PK2PK': 0.0, 'MAXIMUM': kv,
                       'MINIMUM': kv}
        if which == 'v':
            return table_v.get(kind), 'ok'
        ua = rig.read_current_ua()
        return {'MEAN': ua, 'PK2PK': abs(ua) * 0.1, 'MAXIMUM': ua,
                'MINIMUM': ua}.get(kind), 'ok'

    def capture_waveform(self, which):
        n = 100
        return {'t': [i * 0.01 for i in range(n)],
                'v': [self.rig.read_voltage_kv() for _ in range(n)],
                'dt': 0.01, 'npts': n}

    def check_window(self, max_kv, trip_ua, freq_hz):
        return [], None

    def setup_lines(self):
        return ['mock monitor (rig physics)']


class MockCamera(Camera):
    """Synthetic mid-gray frame with a dark disc whose radius follows
    the rig's amplitude -- enough for exposure_verdict / focus plumbing
    and for the vision seam test."""

    def __init__(self, rig, size=64):
        self.rig = rig
        self.size = size

    def available(self):
        return True

    def preflight(self):
        return True, 'mock camera: exposure ok, focus ok', self.grab()

    def grab(self):
        s = self.size
        img = np.full((s, s, 3), 150, dtype=np.uint8)
        r = max(3.0, s * 0.2 * (0.5 + 0.5 * self.rig.amp_now()
                                / max(self.rig.amp0, 1e-9)))
        yy, xx = np.mgrid[0:s, 0:s]
        mask = (yy - s / 2) ** 2 + (xx - s / 2) ** 2 <= r * r
        img[mask] = 60
        return img


class MockEnv(EnvironmentSource):
    """Auto-attested ambient conditions (mock mode needs no operator)."""

    def __init__(self, clock, t_c=23.0, p_mbar=1013.0):
        self.clock = clock
        self.t_c = t_c
        self.p_mbar = p_mbar
        self._entered_t = clock.monotonic()

    def current(self):
        return {'t_c': self.t_c, 'p_mbar': self.p_mbar,
                'p_pa': self.p_mbar * 100.0, 'source': 'mock',
                'operator': 'mock', 'entered_iso': self.clock.now_iso(),
                'age_s': self.clock.monotonic() - self._entered_t}

    def needs_attestation(self, stale_after_s):
        return False


class MockVision(VisionAdapter):
    metric_name = 'mock_amp'

    def __init__(self, rig):
        self.rig = rig

    def measure_pair(self, rest_frame, peak_frame, context):
        amp = self.rig.amp_now() + self.rig.rng.gauss(
            0.0, 0.002 * max(self.rig.amp0, 1e-9))
        return {'value': amp, 'rest_value': self.rig.amp0_rest,
                'quality': 'mock', 'wrinkle_idx': 1.0, 'notes': ''}


def build(clock, seed=1234, script=None, c_nf=5.0, leak0_ua=2.0,
          amp0=100.0):
    """The full mock HAL bundle around one rig."""
    rig = MockRig(clock, c_nf=c_nf, leak0_ua=leak0_ua, amp0=amp0,
                  seed=seed, script=script)
    bundle = HALBundle('mock', MockDrive(rig), MockMonitor(rig),
                       MockCamera(rig), MockEnv(clock), MockVision(rig))
    bundle.rig = rig
    return bundle
