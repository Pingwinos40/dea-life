"""Time source for the engine: real for the bench, simulated for tests.

Everything in core/ that waits or timestamps goes through one of these,
never through ``time`` directly, so a full multi-day DEA-LIFE recipe can
run in a test suite in well under a second (SimClock) while the same
code path drives real hardware (Clock). The vendored TelemetryLog takes
``clock=`` (a monotonic callable) and composes with both.
"""
import time as _time
from datetime import datetime, timedelta


class Clock:
    """Real time. monotonic()/sleep() wrap time; now_iso() is wall time."""

    simulated = False

    def monotonic(self):
        return _time.monotonic()

    def sleep(self, seconds):
        if seconds > 0:
            _time.sleep(seconds)

    def now_iso(self, timespec='milliseconds'):
        return datetime.now().isoformat(timespec=timespec)

    def now(self):
        return datetime.now()


class SimClock:
    """Simulated time: sleep() advances instantly, monotonic() follows.

    Wall-clock timestamps advance in lockstep from a fixed epoch so CSV
    timestamps in tests are deterministic and ordered. There is no speed
    factor to tune -- simulated sleeps cost nothing, which is the point:
    a 5-day recipe's timing math is exercised exactly, just not waited
    out.
    """

    simulated = True

    def __init__(self, start=None):
        self._t = 0.0
        self._epoch = start or datetime(2026, 1, 1, 0, 0, 0)

    def monotonic(self):
        return self._t

    def sleep(self, seconds):
        if seconds > 0:
            self._t += float(seconds)

    def now(self):
        return self._epoch + timedelta(seconds=self._t)

    def now_iso(self, timespec='milliseconds'):
        return self.now().isoformat(timespec=timespec)
