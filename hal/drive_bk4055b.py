"""Live DriveSource: BK4055B signal generator -> Trek 610E-G.

Control-volt convention: gain 1 V(control) = 1 kV(Trek);
``trek_sign`` = -1 when the amplifier is set to invert (the whole
control waveform flips; monitors are corrected in the MonitorSource).

Cycling drive is a counted MAN-trigger burst of a unipolar waveform:

    BSWV WVTP,<w> / FRQ,f / AMP,(v_pk - v_min) / OFST,(v_pk+v_min)/2
    BTWV STPS,<idle phase>      <- RAW write: the vendored driver has
                                   no STPS support (lib/VENDOR.md)
    set_burst(ch, True, ncycles=N, trigger='MAN'); burst_trigger()

THE IDLE-LEVEL TRAP (why STPS + the executors' scope-verified gate both
exist): in SDG-dialect N-cycle burst the output idles at the carrier's
START-PHASE point. Default 0 deg on a unipolar sine = idling at the
OFFSET MIDPOINT -- half voltage held on the specimen between chunks.
Idle-at-minimum is start phase 270 for a normal Trek, 90 for an
inverted one (the waveform is mirrored, so the point nearest 0 kV is
the sine's top). BENCH_TEST section P proves the firmware honors STPS
before any specimen is energized; the executors verify the idle level
by scope regardless, every run.

``zero()`` is the kill path (audit 2026-07-25 C1/C3 lineage): captured
handle first, then any reconnected handle; output OFF before DC 0
(never trust BTWV STATE,OFF mid-burst); NEVER raises.
"""
import sldea_profile  # vendored

from . import DriveSource

# burst start phase that parks the output at the 0 kV end of the swing
IDLE_PHASE_DEG = {1.0: 270, -1.0: 90}


class BK4055BDrive(DriveSource):
    def __init__(self, sg, channel, trek_sign=1.0, sg_getter=None):
        """sg: a connected vendored instruments.BK4055B (captured
        handle). sg_getter: optional callable returning the CURRENT
        handle (a mid-run Reconnect must not strand the kill path)."""
        self._sg = sg
        self.channel = int(channel)
        self.trek_sign = 1.0 if trek_sign >= 0 else -1.0
        self._sg_getter = sg_getter
        self._dc_armed = False
        self._burst_armed = False

    def connected(self):
        return self._sg is not None

    # ---- DC path --------------------------------------------------------
    def _clamp(self, kv):
        cap = self.specimen_cap_kv
        kv = min(float(kv), sldea_profile.TREK_MAX_KV)
        return kv if cap is None else min(kv, float(cap))

    def _ensure_dc(self):
        if not self._dc_armed:
            self._sg.set_load_polarity(self.channel, load='HZ',
                                       polarity='NOR')
            self._sg.set_basic_wave(self.channel, WVTP='DC', OFST=0.0)
            self._sg.set_output(self.channel, True)
            self._dc_armed = True
            self._burst_armed = False

    def set_kv(self, kv):
        self._ensure_dc()
        control = self.trek_sign * sldea_profile.control_v_for_kv(
            self._clamp(kv))
        self._sg.set_offset(self.channel, control)

    # ---- cycling path ---------------------------------------------------
    def configure_cycle(self, waveform, freq_hz, v_pk_kv, v_min_kv):
        v_pk = self._clamp(v_pk_kv)
        v_min = float(v_min_kv)
        span = max(v_pk - v_min, 0.0)
        mid = 0.5 * (v_pk + v_min)
        s = self.trek_sign
        self._sg.set_load_polarity(self.channel, load='HZ',
                                   polarity='NOR')
        self._sg.set_basic_wave(
            self.channel, WVTP=str(waveform).upper(),
            FRQ=float(freq_hz),
            AMP=span * abs(s),          # SG amplitude is positive
            OFST=s * mid)
        # arm the counted burst (MAN; ncycles set per chunk in
        # fire_burst) -- the vendored driver's arm-order fix applies
        self._sg.set_burst(self.channel, True, ncycles=1,
                           trigger='MAN')
        # RAW STPS write (52-byte-safe): idle at the 0 kV end
        self._sg.write(f'C{self.channel}:BTWV STPS,'
                       f'{IDLE_PHASE_DEG[s]}')
        self._sg.set_output(self.channel, True)
        self._dc_armed = False
        self._burst_armed = True
        self._freq_hz = float(freq_hz)

    def fire_burst(self, n_cycles):
        n = int(n_cycles)
        # re-assert the count for THIS chunk, then fire
        self._sg.set_burst(self.channel, True, ncycles=n,
                           trigger='MAN')
        self._sg.write(f'C{self.channel}:BTWV STPS,'
                       f'{IDLE_PHASE_DEG[self.trek_sign]}')
        self._sg.burst_trigger(self.channel)
        return n / self._freq_hz

    def end_cycle(self):
        """Leave burst mode; back to DC at 0 between blocks."""
        try:
            self._sg.set_burst(self.channel, False, trigger='MAN')
        except Exception:
            pass
        self._dc_armed = False
        self._ensure_dc()
        self._sg.set_offset(self.channel, 0.0)

    # ---- the kill path --------------------------------------------------
    def zero(self):
        """Output OFF then DC 0, captured handle first then any
        reconnected one. Never raises; False = NOT_ZEROED."""
        targets = [self._sg]
        if self._sg_getter is not None:
            try:
                cur = self._sg_getter()
                if cur is not None and cur is not self._sg:
                    targets.append(cur)
            except Exception:
                pass
        for sg in targets:
            if sg is None:
                continue
            try:
                sg.set_output(self.channel, False)
                sg.set_basic_wave(self.channel, WVTP='DC', OFST=0.0)
                return True
            except Exception:
                continue
        return False

    def describe(self):
        return [f'BK4055B CH{self.channel} -> Trek 610E-G '
                f'(1 V = 1 kV, sign {self.trek_sign:+g}, burst idle '
                f'phase {IDLE_PHASE_DEG[self.trek_sign]} deg)']
