"""Hardware abstraction layer: the four sources the engine drives.

Interfaces only -- implementations live beside this file
(drive_bk4055b, monitor_mso24, camera_dfk, env_manual, mock). The
engine and executors code against these ABCs and never import an
implementation directly; ``build()`` in each implementation module plus
the HAL_PLUGINS registry is the wiring point, which is what lets the
DEA-Characterization-Board (laser displacement + 24-bit leakage over
TCP :5555) slot in later without engine changes.

Sign convention: MonitorSource implementations return SIGN-CORRECTED
engineering units (trek_sign applied inside, to BOTH monitors alike --
D5 2026-08-04). Statuses follow the vendored TekMSO24 vocabulary:
'ok' | 'offscreen' (9.9E37 sentinel: clipped-but-real, counts as
over-trip evidence) | 'invalid' | 'error'.
"""
import abc
import contextlib

# Future plugin slots: name -> factory(config) returning a partial HAL
# overlay. 'charboard' (DEA-Characterization-Board), 'thermocouple_usb',
# 'vacuum_gauge' are the planned entries (plan roadmap items 2-3).
HAL_PLUGINS = {}


class DriveSource(abc.ABC):
    """The path that makes voltage: SG control voltage -> Trek -> DEA.

    Implementations clamp EVERY commanded level at specimen_cap_kv --
    defense in depth below the recipe-validation check. `zero()` is the
    kill path: it may NEVER raise, returns False on total failure
    (engine escalates to NOT_ZEROED), and must try a captured handle
    before any reconnected one (audit 2026-07-25 C1/C3)."""

    specimen_cap_kv = None      # injected at session start

    @abc.abstractmethod
    def connected(self):
        ...

    @abc.abstractmethod
    def set_kv(self, kv):
        """DC control path (staircase, holds, ramps)."""

    @abc.abstractmethod
    def configure_cycle(self, waveform, freq_hz, v_pk_kv, v_min_kv):
        """Arm the cycling waveform + counted burst (MAN trigger).
        Must leave the output IDLE AT v_min (the STPS trap)."""

    @abc.abstractmethod
    def fire_burst(self, n_cycles):
        """Fire one counted chunk; returns nominal duration n/f [s]."""

    @abc.abstractmethod
    def end_cycle(self):
        """Leave burst mode and return to DC at 0 (between blocks)."""

    @abc.abstractmethod
    def zero(self):
        """Guaranteed shutdown: output off + 0 V. NEVER raises."""

    def hw_interlock(self):
        """Slot for the Trek remote-TTL interlock (roadmap item 1).
        No-op in v1: software zero is the only kill, docs/SAFETY.md
        says so out loud."""

    def describe(self):
        return []


class MonitorSource(abc.ABC):
    """Trek monitor BNCs on the scope (or the mock's physics)."""

    @abc.abstractmethod
    def connected(self):
        ...

    @abc.abstractmethod
    def read_kv(self):
        """-> (kV | None, status). Sign-corrected."""

    @abc.abstractmethod
    def read_ua(self):
        """-> (uA | None, status). Sign-corrected."""

    @abc.abstractmethod
    def read_stat(self, kind, which):
        """kind in MEAN|PK2PK|MAXIMUM|MINIMUM|FREQUENCY, which in
        'v'|'i'. -> (engineering value | None, status). FREQUENCY is Hz
        regardless of channel. (MSO24 trap: the token is FREQUENCY,
        never FREQ.)"""

    @abc.abstractmethod
    def capture_waveform(self, which):
        """-> {'t','v','dt','npts'} | None. For event freezes; pulled
        AFTER HV is zeroed (the scope's own memory is the pre-trigger
        buffer)."""

    def check_window(self, max_kv, trip_ua, freq_hz):
        """-> (problems: [str], fixplan | None)."""
        return [], None

    def apply_fix(self, plan):
        return False

    def setup_lines(self):
        return []


class Camera(abc.ABC):
    @abc.abstractmethod
    def available(self):
        ...

    @abc.abstractmethod
    def preflight(self):
        """-> (ok, verdict_text, frame | None)."""

    @abc.abstractmethod
    def grab(self):
        """One RGB frame (ndarray) or None."""

    @contextlib.contextmanager
    def locked(self, exposure=None, gain=None):
        """Exposure/gain lock for the duration; restore on exit."""
        yield


class EnvironmentSource(abc.ABC):
    """Chamber conditions. v1 = manual attestation; sensors later."""

    @abc.abstractmethod
    def current(self):
        """-> {'t_c','p_mbar','p_pa','source','operator','entered_iso',
        'age_s'} or None when never attested."""

    @abc.abstractmethod
    def needs_attestation(self, stale_after_s):
        ...


class VisionAdapter(abc.ABC):
    """Interlude displacement measurement from captured frames. The
    planar adapter (vendored sldea_edge) and the bender pipeline
    implement this; the mock returns its rig's modeled truth."""

    metric_name = 'disp'

    @abc.abstractmethod
    def measure_pair(self, rest_frame, peak_frame, context):
        """-> {'value': float|None, 'rest_value': float|None,
        'quality': str, 'wrinkle_idx': float|None, 'notes': str}."""


class HALBundle:
    """What the engine actually receives: the five sources + a name."""

    def __init__(self, name, drive, monitor, camera, env, vision):
        self.name = name
        self.drive = drive
        self.monitor = monitor
        self.camera = camera
        self.env = env
        self.vision = vision
