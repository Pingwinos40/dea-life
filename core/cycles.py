"""Cycle accounting: the three-axis ledger and the burst chunk planner.

Damage dose in DEAs tracks accumulated ACTUATED time, not cycle count
alone (EPFL: aging at 10-200 Hz collapsed onto actuated-seconds), so
every record carries three independent axes:

- cycles      -- what the claim is written in ("10,000 cycles")
- wall clock  -- what the calendar cost
- actuated_s  -- sum of duty * dt; the damage-dose axis

Actuated-time convention (documented in docs/DATA_FORMATS.md): duty =
mean(V/V_pk) over the segment. Unipolar SINE / SQUARE(50%) / RAMP
cycling = 0.5; a DC hold above 0 kV = 1.0; interlude staircases
integrate their own levels. Deterministic and clock-free, so mock and
live agree exactly.

Counting modes per credited block:
- 'burst'     -- hardware-anchored: the BK4055B ran a counted N-cycle
                 burst AND the idle-level + FREQUENCY verifications
                 passed.
- 'timed'     -- continuous drive credited by integrating measured
                 frequency over time.
- 'estimated' -- a partial chunk (abort mid-burst) credited f * elapsed.
"""

CHUNK_CYCLES = 1000       # = the fast-interlude cadence; also keeps any
                          # single burst well under SDG NCYC limits
FREQ_TOL = 0.02           # scope FREQUENCY within 2% of commanded

DUTY = {'SINE': 0.5, 'SQUARE': 0.5, 'RAMP': 0.5}


def waveform_duty(waveform):
    return DUTY.get(str(waveform).upper(), 0.5)


class CycleLedger:
    """The run's authoritative counters. Checkpointed every block."""

    def __init__(self, cycles=0, actuated_s=0.0, wall_s=0.0):
        self.cycles = int(cycles)
        self.actuated_s = float(actuated_s)
        self.wall_s = float(wall_s)

    def credit_cycles(self, n, freq_hz, waveform, mode):
        """Credit n cycles of cycling drive; returns the actuated seconds
        added. mode is recorded by the caller in blocks.csv."""
        n = int(n)
        self.cycles += n
        added = (n / float(freq_hz)) * waveform_duty(waveform) \
            if freq_hz > 0 else 0.0
        self.actuated_s += added
        return added

    def credit_hold(self, kv, hold_s):
        """A DC hold above 0 kV is fully actuated time (duty 1.0)."""
        if kv > 0:
            self.actuated_s += float(hold_s)

    def credit_profile(self, profile):
        """An interlude staircase's actuated time: integrate kv_at over
        its segments relative to its own peak (duty = mean(V/V_peak))."""
        peak = max(profile.levels) if profile.levels else 0.0
        if peak <= 0:
            return
        total = 0.0
        for kind, t0, t1, a, b in profile.segments:
            total += (t1 - t0) * (0.5 * (a + b)) / peak
        self.actuated_s += total

    def snapshot(self):
        return {'cycles': self.cycles,
                'actuated_s': round(self.actuated_s, 3),
                'wall_s': round(self.wall_s, 3)}


def plan_chunks(n_cycles, chunk=CHUNK_CYCLES, hw_max=None):
    """Split a cycle block into burst chunks.

    hw_max is the bench-probed NCYC ceiling for this signal generator
    (BENCH_TEST.md section P records it); None = not yet probed, planner
    stays at the default chunk which is far below any SDG family limit.
    """
    n = int(n_cycles)
    if n <= 0:
        return []
    size = int(chunk)
    if hw_max:
        size = min(size, int(hw_max))
    out = [size] * (n // size)
    if n % size:
        out.append(n % size)
    return out


def expected_burst_s(n_cycles, freq_hz):
    """Nominal burst duration + margin: the SG gives no completion
    event, so completion is timed (n/f) plus 0.5 s plus two periods."""
    return n_cycles / float(freq_hz) + 0.5 + 2.0 / float(freq_hz)


def freq_ok(measured_hz, commanded_hz, tol=FREQ_TOL):
    """Is the scope's FREQUENCY read consistent with the commanded rate?
    None (unreadable) is NOT ok -- the caller decides whether to degrade
    to timed counting or flag."""
    if measured_hz is None or commanded_hz <= 0:
        return False
    return abs(measured_hz - commanded_hz) / commanded_hz <= tol


class TimedCounter:
    """Fallback cycle crediting: integrate measured frequency over time.

    feed(t_s, f_meas) with monotonic seconds; unreadable f (None) holds
    the last credible frequency for up to `hold_s` before pausing
    accrual (a dead scope must not keep counting forever)."""

    def __init__(self, commanded_hz, hold_s=10.0):
        self.commanded_hz = float(commanded_hz)
        self.hold_s = float(hold_s)
        self.cycles = 0.0
        self.mismatch = False       # any credible read outside FREQ_TOL
        self._last_t = None
        self._last_f = None
        self._stale_since = None

    def feed(self, t_s, f_meas):
        if self._last_t is not None:
            dt = t_s - self._last_t
            f = None
            if f_meas is not None:
                f = f_meas
                self._stale_since = None
            elif self._last_f is not None:
                if self._stale_since is None:
                    self._stale_since = t_s
                if t_s - self._stale_since <= self.hold_s:
                    f = self._last_f
            if f is not None and dt > 0:
                self.cycles += f * dt
        if f_meas is not None:
            self._last_f = f_meas
            if not freq_ok(f_meas, self.commanded_hz):
                self.mismatch = True
        self._last_t = t_s

    def credited(self):
        return int(self.cycles)
