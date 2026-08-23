#!/usr/bin/env python3
"""
Lab instrument control library, PyVISA-py backed (USB-TMC + USB-serial).

Replaces the previous kernel-usbtmc transport (os.open('/dev/usbtmc*')) with
pyvisa.ResourceManager('@py'). On the RHEL9 lab box the kernel `usbtmc` module
is blacklisted; this module talks to USB-TMC instruments via libusb (pyusb)
through PyVISA's '@py' backend. Per-instrument udev rules in
/etc/udev/rules.d/99-usbtmc.rules grant mode 0666 to the matching USB device
nodes so a non-root user can claim them.

Instrument classes auto-discover by USB VID/PID via PyVISA's resource list:

    lcr   = BK894()
    scope = TekMSO24()
    sg    = BK4055B()

Pass an explicit resource string only to override discovery (e.g. when more
than one unit of the same model is connected):

    lcr = BK894(resource='USB0::1137::10279::SOMESERIAL::0::INSTR')

The DC power supply behind the CP2102 USB-UART bridge uses serial transport
via pyserial (NOT USB-TMC); see BK9174B below.
"""
import re
import time
import struct
import threading


# --------------------------------------------------------------------------
# Shared PyVISA ResourceManager
# --------------------------------------------------------------------------

_RM = None
_RM_LOCK = threading.Lock()


def _io_lock(obj):
    """Per-instrument reentrant I/O lock, created lazily.

    SCPI is write-then-read: two threads interleaving on one session cross
    their replies (audit 2026-07-25 — the GUI runs button ops, the logging
    thread, the LCR sweep, the SLDEA worker and camera sequences against
    the same sessions; only the PSU had a lock). Lazy creation keeps
    test fakes that skip __init__ working. RLock so a compound driver
    method can hold it across its whole transaction while the primitives
    it calls re-acquire."""
    lock = obj.__dict__.get('_io_rlock')
    if lock is None:
        with _RM_LOCK:
            lock = obj.__dict__.get('_io_rlock')
            if lock is None:
                lock = obj.__dict__['_io_rlock'] = threading.RLock()
    return lock


def get_resource_manager():
    """Process-wide PyVISA ResourceManager (pyvisa-py backend).

    pyvisa is imported lazily so that merely importing this module (e.g. for the
    pure waveform/preset logic, or the hardware-free editor demo) does not
    require pyvisa to be installed -- it's only needed to actually open a device.
    """
    global _RM
    with _RM_LOCK:
        if _RM is None:
            import pyvisa
            _RM = pyvisa.ResourceManager('@py')
        return _RM


def list_usb_instruments():
    """Return [(resource_string, vid_int, pid_int)] for all USB-TMC resources."""
    rm = get_resource_manager()
    found = []
    for r in rm.list_resources():
        if not r.startswith('USB'):
            continue
        parts = r.split('::')
        try:
            found.append((r, int(parts[1]), int(parts[2])))
        except (IndexError, ValueError):
            continue
    return found


# --------------------------------------------------------------------------
# USB-TMC base class
# --------------------------------------------------------------------------

class VisaInstrument:
    """Base class for USB-TMC instruments accessed through pyvisa-py.

    Subclasses set:
      VID, PID    int   USB vendor/product IDs (hex literals OK)
      TIMEOUT_MS  int   default PyVISA I/O timeout in milliseconds
    """
    VID = None
    PID = None
    TIMEOUT_MS = 2000

    def __init__(self, resource=None, rm=None):
        rm = rm if rm is not None else get_resource_manager()
        if resource is None:
            resource = self._discover(rm)
            if resource is None:
                raise RuntimeError(
                    f"{self.__class__.__name__}: no device found with "
                    f"VID=0x{self.VID:04x} PID=0x{self.PID:04x}. "
                    f"Check that the instrument is powered and enumerated "
                    f"(lsusb | grep -i {self.VID:04x}:{self.PID:04x})."
                )
        self.resource = resource
        self.inst = rm.open_resource(resource)
        self.inst.timeout = self.TIMEOUT_MS
        self.inst.read_termination = '\n'
        self.inst.write_termination = '\n'
        # Flush any stale device output (e.g. an unread binary CURVE? response
        # from a previous run) so the first query doesn't read garbage.
        try:
            self.inst.clear()
        except Exception:
            pass
        # If IDN/post-open fails, CLOSE the session before propagating: a
        # leaked open claims the USB interface for the process lifetime,
        # making every reconnect fail until app restart (audit 2026-07-25).
        try:
            self.idn = self.inst.query('*IDN?').strip()
            self._post_open()
        except Exception:
            try:
                self.inst.close()
            except Exception:
                pass
            raise

    def _post_open(self):
        """Subclass hook for additional setup after open + IDN."""
        pass

    @classmethod
    def _discover(cls, rm):
        """Return the first USB resource string matching cls.VID/cls.PID, or None."""
        for r, vid, pid in list_usb_instruments():
            if vid == cls.VID and pid == cls.PID:
                return r
        return None

    def write(self, command):
        with _io_lock(self):
            self.inst.write(command)

    def read(self):
        with _io_lock(self):
            return self.inst.read().strip()

    def read_raw(self):
        with _io_lock(self):
            return self.inst.read_raw()

    def write_raw(self, data):
        """Write raw bytes verbatim (no termination/encoding munging).

        Needed for commands that carry binary payloads, e.g. the 4055B's
        WVDT arbitrary-waveform upload where sample bytes follow the ASCII
        header directly.
        """
        with _io_lock(self):
            return self.inst.write_raw(data)

    def write_raw_oneshot(self, data):
        """Write a large binary payload as ONE USBTMC Bulk-OUT message.

        DIAGNOSTIC ONLY -- do not use in production paths. On the 4055B any
        single USBTMC transfer longer than one 64-byte USB packet hard-wedges
        the firmware (bench-verified 2026-07-02, even for pure-ASCII queries);
        only a front-panel power cycle recovers it. Kept solely so
        ``bench/archive/test_arb_usb_probe.py`` can demonstrate the wedge.
        For real uploads use ``write_raw_single_packet``.
        """
        try:
            sess = self.inst.visalib.sessions[self.inst.session]
            ep = sess.interface.usb_send_ep
        except (AttributeError, KeyError, TypeError):
            return self.inst.write_raw(data)
        old = ep.wMaxPacketSize
        ep.wMaxPacketSize = max(old, len(data) + 64)
        try:
            return self.inst.write_raw(data)
        finally:
            ep.wMaxPacketSize = old

    def ask(self, command):
        """Write a command and return its response (newline-stripped)."""
        with _io_lock(self):
            return self.inst.query(command).strip()

    # Alias matching pyvisa convention; both ask() and query() are used here.
    query = ask

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass

    def _drain(self):
        """Discard any pending/stale bytes in the input buffer so the next
        query reads its OWN reply. More forceful than inst.clear() (which
        does not reliably flush every meter): reads with a short timeout until
        the buffer is empty. Silent."""
        with _io_lock(self):
            old = getattr(self.inst, 'timeout', None)
            try:
                self.inst.timeout = 60
                for _ in range(20):
                    self.inst.read()      # discard a stale reply
            except Exception:
                pass                      # timeout -> buffer empty
            finally:
                if old is not None:
                    try:
                        self.inst.timeout = old
                    except Exception:
                        pass

    def go_local(self):
        """Best-effort return-to-local so the front panel works again after
        the app quits. pyvisa-py ASSERTS remote on USB-TMC connect (and any
        command puts a box in remote), so without this the panel stays locked
        until a power cycle. Sends SCPI SYSTem:LOCal and then the transport's
        go-to-local (USB488 GO_TO_LOCAL / VXI-11 device_local). Silent --
        shutdown must never raise."""
        try:
            self.write('SYST:LOC')
        except Exception:
            pass
        try:
            self._visa_go_local()
        except Exception:
            pass

    def _visa_go_local(self):
        """Issue the transport-level go-to-local via the pyvisa-py session:
        USB488 GO_TO_LOCAL for USB-TMC, device_local for VXI-11. All reaches
        are guarded by the caller."""
        sess = self.inst.visalib.sessions[self.inst.session]
        iface = getattr(sess, 'interface', None)
        if iface is None:
            return
        usb_dev = getattr(iface, 'usb_dev', None)
        if usb_dev is not None:                         # USB-TMC (USB488)
            import usb.util as uu
            intf = getattr(iface, 'usb_intf', None)
            usb_dev.ctrl_transfer(
                uu.build_request_type(uu.CTRL_IN, uu.CTRL_TYPE_CLASS,
                                      uu.CTRL_RECIPIENT_INTERFACE),
                161,                                    # USBTMC GO_TO_LOCAL
                0x0000, intf.index if intf is not None else 0, 0x0001,
                timeout=1000)
        elif hasattr(iface, 'device_local') and hasattr(sess, 'link'):  # VXI-11
            iface.device_local(sess.link, 0, 1000, 1000)


# --------------------------------------------------------------------------
# B&K Precision 894 LCR Meter (USB-TMC)
# --------------------------------------------------------------------------

class BK894(VisaInstrument):
    """B&K Precision 894 LCR Meter.

    NOTE: this meter's USB serial-number descriptor is malformed (returns
    non-ASCII garbage). PyVISA enumerates it anyway since pyvisa-py addresses
    by bus/device under the hood. The true serial number is exposed only
    through `*IDN?`.
    """
    VID = 0x0471
    PID = 0x2827
    TIMEOUT_MS = 2000

    MODES = {
        'CPD':  'Capacitance + Dissipation',
        'CPQ':  'Capacitance + Q Factor',
        'CPG':  'Capacitance + Conductance',
        'CPRP': 'Capacitance + Parallel Resistance',
        # Series-capacitance pairs: the bench meter reported itself in
        # 'csrs' on 2026-07-10, so these exist on this unit and were
        # missing here (readouts fell back to bare numbers).
        'CSD':  'Capacitance (series) + Dissipation',
        'CSQ':  'Capacitance (series) + Q Factor',
        'CSRS': 'Capacitance (series) + Series Resistance',
        'LSRS': 'Inductance (series) + Resistance (series)',
        'LSRD': 'Inductance (series) + Resistance (DC)',
        'LPRS': 'Inductance (parallel) + Resistance (series)',
        'LPRP': 'Inductance (parallel) + Resistance (parallel)',
        'RX':   'Resistance + Reactance',
        'ZTD':  'Impedance + Phase (degrees)',
        'ZTR':  'Impedance + Phase (radians)',
    }

    def set_mode(self, mode):
        """Set measurement mode (e.g. 'CPD', 'LSRS'). See MODES dict."""
        if mode.upper() not in self.MODES:
            raise ValueError(f"Invalid mode. Choose from: {list(self.MODES.keys())}")
        self.write(f':FUNC:IMP {mode.upper()}')

    def set_frequency(self, freq_hz):
        """Set test frequency (100 Hz to 500 kHz)."""
        if not 100 <= freq_hz <= 500000:
            raise ValueError("Frequency must be 100 Hz to 500 kHz")
        self.write(f':FREQ {freq_hz}')

    def set_voltage(self, voltage):
        """Set AC test voltage (0.01 to 2.0 V).

        The BK 894's AC test-level command is plain ``:VOLT`` (set and query),
        bench-verified 2026-06-16 -- NOT ``:LEV:VOLT`` nor the E4980A-style
        ``:VOLT:LEV``; both are silently rejected on this unit. The meter
        clamps the level at 2.0 V, so higher values are out of range.
        """
        if not 0.01 <= voltage <= 2.0:
            raise ValueError("Voltage must be 0.01 to 2.0 V")
        self.write(f':VOLT {voltage}')

    def measure(self):
        """Return (primary, secondary, status). status 0 = good."""
        with _io_lock(self):
            result = self.ask(':FETC?')
        primary, secondary, status = result.split(',')
        return float(primary), float(secondary), int(status)

    def get_config(self):
        """Read mode/frequency/voltage, resilient to a stale/desynced reply
        (the mode string 'CSRS' bleeding into a numeric field -- an off-by-one
        where every query returns the previous query's answer). On a bad
        numeric field it DRAINS the input buffer (which realigns the meter --
        inst.clear() alone does not reliably flush this unit) and re-reads, up
        to 3 tries. Never raises: a field that still won't parse comes back
        None so the config sync degrades instead of erroring in the footer."""
        mode = freq = volt = None
        for attempt in range(3):
            if attempt:
                self._drain()
            mode = self.ask(':FUNC:IMP?')
            freq = self._try_float(':FREQ?')
            volt = self._try_float(':VOLT?')
            if freq is not None and volt is not None:
                break
        return {'mode': mode, 'frequency': freq, 'voltage': volt}

    def _try_float(self, cmd):
        raw = self.ask(cmd)
        m = re.match(r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?',
                     (raw or '').strip())
        return float(m.group()) if m else None

    # -- DC bias / aperture / range / fixture correction (issue #44) ------
    # Query forms bench-verified 2026-07-10 on this unit (fw 1.0.5):
    #   :BIAS:VOLT? -> '0.00000e+00'   :BIAS:STAT? -> '0'
    #   :APER?      -> 'MED,1'         :FUNC:IMP:RANG:AUTO? -> '1'
    #   :CORR:OPEN:STAT? / :CORR:SHOR:STAT? -> '1'
    # NOTE this meter answers *OPC? with '0' immediately (non-standard), so
    # completion of a long operation is detected by issuing the next query
    # with a long timeout instead.

    APERTURE_SPEEDS = ('SLOW', 'MED', 'FAST')

    def set_bias_voltage(self, volts):
        """Set the internal DC bias level (takes effect while bias is ON).

        The 894 clamps to its supported bias range; read get_bias() back to
        see what it accepted (same approach as the AC level).
        """
        if not -5.0 <= volts <= 5.0:
            raise ValueError("Bias must be within +/-5 V")
        self.write(f':BIAS:VOLT {volts}')

    def set_bias_enabled(self, on):
        """Switch the internal DC bias source on/off."""
        self.write(f':BIAS:STAT {1 if on else 0}')

    def get_bias(self):
        """-> {'volts': float, 'on': bool}."""
        return {'volts': float(self.ask(':BIAS:VOLT?')),
                'on': self.ask(':BIAS:STAT?').strip() in ('1', 'ON')}

    @staticmethod
    def parse_aperture(raw):
        """':APER?' response 'MED,1' -> ('MED', 1)."""
        parts = [p.strip() for p in raw.split(',')]
        speed = parts[0].upper()
        avg = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        return speed, avg

    def set_aperture(self, speed, avg=1):
        """Measurement speed SLOW/MED/FAST + averaging count (1..256)."""
        speed = speed.upper()
        if speed not in self.APERTURE_SPEEDS:
            raise ValueError(f"Speed must be one of {self.APERTURE_SPEEDS}")
        if not 1 <= int(avg) <= 256:
            raise ValueError("Averaging count must be 1 to 256")
        self.write(f':APER {speed},{int(avg)}')

    def get_aperture(self):
        """-> ('MED', 1)-style (speed, averaging count) tuple."""
        return self.parse_aperture(self.ask(':APER?'))

    def set_range_auto(self, on):
        """Auto-ranging on/off. Hold the range (off) for glitch-free sweeps
        on a fixed DUT; remember to re-enable for unknown parts."""
        self.write(f':FUNC:IMP:RANG:AUTO {1 if on else 0}')

    def get_correction_states(self):
        """-> {'open': bool, 'short': bool} -- whether each correction is
        currently being APPLIED to measurements."""
        return {'open': self.ask(':CORR:OPEN:STAT?').strip() in ('1', 'ON'),
                'short': self.ask(':CORR:SHOR:STAT?').strip() in ('1', 'ON')}

    def run_correction(self, kind):
        """Run an open or short fixture-correction sweep, then enable it.

        kind: 'open' (fixture must be EMPTY) or 'short' (terminals must be
        SHORTED with a shorting bar/wire). The meter sweeps every test
        frequency, which takes tens of seconds and blocks its front panel;
        the follow-up state query is issued with a long timeout and returns
        only when the sweep is done. Call from a worker thread.
        """
        base = {'open': ':CORR:OPEN', 'short': ':CORR:SHOR'}[kind]
        old_to = self.inst.timeout
        self.inst.timeout = max(old_to or 0, 90000)
        try:
            self.write(base)
            self.ask(f'{base}:STAT?')      # answers once the sweep finishes
            self.write(f'{base}:STAT 1')   # apply the fresh correction
        finally:
            self.inst.timeout = old_to


# --------------------------------------------------------------------------
# Tektronix MSO24 Oscilloscope (USB-TMC)
# --------------------------------------------------------------------------

class TekMSO24(VisaInstrument):
    """Tektronix MSO24 Oscilloscope."""
    VID = 0x0699
    PID = 0x0105
    TIMEOUT_MS = 5000

    def _post_open(self):
        # Binary waveform transfer for speed
        self.write('DATA:ENCDG RIBINARY')
        self.write('DATA:WIDTH 2')

    def reset(self):
        self.write('*RST')
        time.sleep(1)

    def autoset(self):
        self.write('AUTOSET EXECUTE')
        time.sleep(2)

    def set_channel_enable(self, channel, enable=True):
        state = 'ON' if enable else 'OFF'
        self.write(f'SELECT:CH{channel} {state}')

    def set_vertical(self, channel, scale, position=0, coupling='DC'):
        """scale = volts/div, position in divisions, coupling 'AC'|'DC'|'GND'."""
        self.write(f'CH{channel}:SCALE {scale}')
        self.write(f'CH{channel}:POSITION {position}')
        self.write(f'CH{channel}:COUPLING {coupling}')

    def set_horizontal(self, scale, position=0):
        """scale = seconds/div, position in seconds."""
        self.write(f'HORIZONTAL:SCALE {scale}')
        self.write(f'HORIZONTAL:POSITION {position}')

    def set_trigger_edge(self, source='CH1', level=0, slope='RISE'):
        self.write('TRIGGER:A:TYPE EDGE')
        self.write(f'TRIGGER:A:EDGE:SOURCE {source}')
        self.write(f'TRIGGER:A:LEVEL:{source} {level}')
        self.write(f'TRIGGER:A:EDGE:SLOPE {slope}')

    def single(self):
        self.write('ACQUIRE:STOPAFTER SEQUENCE')
        self.write('ACQUIRE:STATE RUN')

    def run(self):
        self.write('ACQUIRE:STOPAFTER RUNSTOP')
        self.write('ACQUIRE:STATE RUN')

    def stop(self):
        self.write('ACQUIRE:STATE STOP')

    def measure(self, meas_type, channel):
        """Automated measurement. Returns float or None for invalid signals.
        Callers that must tell WHY it failed use measure_raw."""
        return self.measure_raw(meas_type, channel)[0]

    def measure_raw(self, meas_type, channel):
        """Automated measurement with its failure mode: (value, status),
        status one of 'ok' | 'offscreen' | 'invalid'.

        'offscreen' is the Tek 9.9E37 invalid-measurement sentinel — on
        this scope it means the trace left the visible vertical window
        (bench 2026-07-25 / 07-29: V_Out walked off a too-small window and
        logged blank from 4.25 kV). The SLDEA runner treats it as a
        clipped-but-real signal, unlike 'invalid' (unparseable reply).

        Holds the instrument lock across the whole TYPE/SOURCE/VALUE?
        triple: it programs GLOBAL state, so an interleaved caller used to
        read the other thread's channel/type (audit 2026-07-25 — e.g. the
        SLDEA watchdog reading V_Out as current)."""
        with _io_lock(self):
            self.write(f'MEASUREMENT:IMMED:TYPE {meas_type}')
            self.write(f'MEASUREMENT:IMMED:SOURCE CH{channel}')
            result = self.ask('MEASUREMENT:IMMED:VALUE?')
        try:
            val = float(result)
        except ValueError:
            return None, 'invalid'
        if abs(val) > 1e30:
            return None, 'offscreen'
        return val, 'ok'

    def get_all_measurements(self, channel):
        # The MSO24 frequency measurement token is FREQUENCY -- 'FREQ' is
        # silently accepted but returns a garbage value (~2 Hz). Keys stay
        # short for callers. (Root cause of the GUI's frequency issue.)
        types = (('freq', 'FREQUENCY'), ('period', 'PERIOD'), ('mean', 'MEAN'),
                 ('pk2pk', 'PK2PK'), ('rms', 'RMS'), ('amplitude', 'AMPLITUDE'))
        with _io_lock(self):
            return {key: self.measure(scpi, channel) for key, scpi in types}

    def get_waveform(self, channel):
        """Acquire a channel waveform. Returns {'t', 'v', 'dt', 'npts'}."""
        with _io_lock(self):
            return self._get_waveform_locked(channel)

    def _get_waveform_locked(self, channel):
        self.write(f'DATA:SOURCE CH{channel}')
        time.sleep(0.1)

        nr_pt = int(self.ask('WFMPRE:NR_PT?'))
        xincr = float(self.ask('WFMPRE:XINCR?'))
        pt_off = int(self.ask('WFMPRE:PT_OFF?'))
        xzero = float(self.ask('WFMPRE:XZERO?'))
        ymult = float(self.ask('WFMPRE:YMULT?'))
        yoff = float(self.ask('WFMPRE:YOFF?'))
        yzero = float(self.ask('WFMPRE:YZERO?'))

        self.write('CURVE?')
        time.sleep(0.2)
        raw_data = self.read_raw()

        # IEEE 488.2 definite-length block: '#<N><N-digit byte count><data>'.
        # Parse the declared length (robust to a trailing newline or none) and
        # complete the read if the block spans more than one chunk.
        ndig = int(chr(raw_data[1]))
        nbytes = int(raw_data[2:2 + ndig])
        start = 2 + ndig
        while len(raw_data) < start + nbytes:
            more = self.read_raw()
            if not more:
                break
            raw_data += more
        data_bytes = raw_data[start:start + nbytes]
        data_bytes = data_bytes[:len(data_bytes) // 2 * 2]   # whole int16 samples

        samples = struct.unpack(f'>{len(data_bytes)//2}h', data_bytes)
        voltages = [(s - yoff) * ymult + yzero for s in samples]
        times = [xzero + (i * xincr) for i in range(len(voltages))]

        return {'t': times, 'v': voltages, 'dt': xincr, 'npts': len(voltages)}


# --------------------------------------------------------------------------
# B&K Precision 4055B Signal Generator (USB-TMC, Siglent SDG-class OEM)
# --------------------------------------------------------------------------

class BK4055B(VisaInstrument):
    """B&K Precision 4055B Signal Generator.

    SCPI syntax matches the Siglent SDG2000X series (which the 4055B is OEM-
    rebadged from). Channel index in SCPI is 1 or 2.

    USB TRANSPORT LIMIT (firmware 1.01.01.33R3, bench-verified 2026-07-02):
    the box hard-wedges on any USBTMC Bulk-OUT transfer spanning more than
    one 64-byte USB packet -- even a padded pure-ASCII ``*IDN?`` -- and only
    a front-panel power cycle recovers it. Chaining a command across several
    single-packet USBTMC messages does NOT help: the firmware processes the
    first message as the whole command and silently drops the continuations
    (a 2 KB WVDT upload stores as its first ~24 data bytes). Net effect:
    **USB commands are capped at 52 bytes** (64 minus the 12-byte USBTMC
    header), so arb upload over USB is impossible and long text commands
    must be split -- ``write`` raises rather than wedge, ``set_basic_wave``
    auto-splits, ``upload_arb`` requires a LAN resource::

        sg = BK4055B(resource='TCPIP0::<ip>::INSTR')   # or ::5025::SOCKET

    Reads (Bulk-IN) are unaffected: multi-KB query responses work over USB.

    NOTE: command syntax is conservatively chosen to match the Siglent SDG
    programming manual; verify against the BK 4055B-specific manual before
    relying on edge-case behavior.
    """
    VID = 0xf4ec
    PID = 0xee38
    # 2 s proved too tight for BSWV? read-back right after a multi-parameter
    # write (VI_ERROR_TMO seen on the bench, 2026-06-11).
    TIMEOUT_MS = 5000

    # Max bytes (incl. the '\n' terminator) the firmware accepts per USB
    # command: one 64-byte packet minus the 12-byte USBTMC header.
    USB_MAX_CMD = 52

    def _usb_transport(self):
        """True when talking over USBTMC (where the 52-byte cap applies)."""
        return getattr(self, 'resource', '').startswith('USB')

    def write(self, command):
        if self._usb_transport() and len(command) + 1 > self.USB_MAX_CMD:
            raise ValueError(
                f"SCPI command is {len(command) + 1} bytes incl. newline; the "
                f"4055B USB firmware wedges on anything over "
                f"{self.USB_MAX_CMD} (needs a front-panel power cycle). "
                f"Split the command or connect via LAN. "
                f"Offending command: {command[:60]!r}"
            )
        super().write(command)

    WAVEFORMS = ('SINE', 'SQUARE', 'RAMP', 'PULSE', 'NOISE', 'ARB', 'DC')

    # BSWV keys whose values are numeric (carry a unit suffix on read-back).
    # WVTP and any other key are kept as raw strings by get_basic_wave_dict.
    _BSWV_NUMERIC = (
        'FRQ', 'PERI', 'AMP', 'AMPVRMS', 'OFST', 'HLEV', 'LLEV',
        'PHSE', 'DUTY', 'SYM', 'RISE', 'FALL', 'DLY', 'WIDTH',
        'STDEV', 'MEAN',   # NOISE params -- read back as '0.2V'/'0V';
                           # leaving them raw broke Apply after any sync
    )

    @staticmethod
    def _fmt_param(value):
        """Format a numeric SCPI parameter the firmware parses reliably.

        The 4055B mis-parses some decimal-suffixed values: 'DUTY,50.0' lands
        as 5% duty (bench-verified 2026-06-11; the box appears to read the
        digits in 0.01% units). Send plain decimal notation with no trailing
        '.0' and no scientific exponent: 50.0 -> '50', 1.68e-08 ->
        '0.0000000168'. Non-floats pass through unchanged.
        """
        if isinstance(value, float):
            return f'{value:.12f}'.rstrip('0').rstrip('.') or '0'
        return str(value)

    @staticmethod
    def _strip_unit(value):
        """Parse a BSWV/OUTP numeric value, dropping any unit suffix.

        Values come back like '100HZ', '0.01S', '2V', '1.41421Vrms'. Leading
        sign, digits, decimal point and exponent form the number; everything
        after is the unit. Returns a float, or the original string if it does
        not start with a number.
        """
        m = re.match(r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?', value.strip())
        return float(m.group()) if m else value

    def set_waveform(self, channel, wave):
        if wave.upper() not in self.WAVEFORMS:
            raise ValueError(f"Waveform must be one of {self.WAVEFORMS}")
        self.write(f'C{channel}:BSWV WVTP,{wave.upper()}')

    def set_frequency(self, channel, freq_hz):
        self.write(f'C{channel}:BSWV FRQ,{self._fmt_param(freq_hz)}')

    def set_amplitude_vpp(self, channel, amp_vpp):
        self.write(f'C{channel}:BSWV AMP,{self._fmt_param(amp_vpp)}')

    def set_offset(self, channel, offset_v):
        self.write(f'C{channel}:BSWV OFST,{self._fmt_param(offset_v)}')

    def set_basic_wave(self, channel, **params):
        """Set several basic-wave parameters in one command.

        Keys are BSWV parameter names (case-insensitive), e.g.
        set_basic_wave(1, WVTP='SINE', FRQ=1000, AMP=2, OFST=0). Pushing them
        together is one bus round-trip rather than one per setter.

        Over USB the chain is auto-split into multiple BSWV commands of at
        most USB_MAX_CMD bytes each (parameter order preserved, WVTP first as
        passed) -- a single long chain like WVTP,SQUARE,...,DUTY,50,PHSE,0
        exceeds the firmware's 52-byte USB command cap and would wedge the
        box. Incremental BSWV sets are natively supported by the SDG dialect.
        """
        if not params:
            return
        if 'WVTP' in {k.upper() for k in params}:
            wave = next(v for k, v in params.items() if k.upper() == 'WVTP')
            if str(wave).upper() not in self.WAVEFORMS:
                raise ValueError(f"Waveform must be one of {self.WAVEFORMS}")
        tokens = [f'{k.upper()},{self._fmt_param(v)}' for k, v in params.items()]
        prefix = f'C{channel}:BSWV '
        if not self._usb_transport():
            self.write(prefix + ','.join(tokens))
            return
        # Greedily pack tokens into commands that fit the USB cap.
        batch = []
        for tok in tokens:
            trial = prefix + ','.join(batch + [tok])
            if batch and len(trial) + 1 > self.USB_MAX_CMD:
                self.write(prefix + ','.join(batch))
                batch = [tok]
            else:
                batch.append(tok)
        if batch:
            self.write(prefix + ','.join(batch))

    def set_output(self, channel, on):
        self.write(f'C{channel}:OUTP {"ON" if on else "OFF"}')

    def set_output_full(self, channel, on, load=None, polarity=None):
        """Set output state plus optional load and polarity in one command.

        load: a resistance in ohms (e.g. 50) or 'HZ' for high impedance.
        polarity: 'NOR' (normal) or 'INVT' (inverted).
        """
        parts = ['ON' if on else 'OFF']
        if load is not None:
            parts.append(f'LOAD,{load}')
        if polarity is not None:
            parts.append(f'PLRT,{polarity.upper()}')
        self.write(f'C{channel}:OUTP {",".join(parts)}')

    def set_load_polarity(self, channel, load=None, polarity=None):
        """Set output load and/or polarity WITHOUT changing the on/off state.

        The SDG dialect accepts OUTP with only LOAD/PLRT arguments, leaving the
        output state untouched (e.g. 'C1:OUTP LOAD,50'). Used by the GUI so
        Apply can configure the channel while a separate button gates the
        output.
        """
        parts = []
        if load is not None:
            parts.append(f'LOAD,{load}')
        if polarity is not None:
            parts.append(f'PLRT,{polarity.upper()}')
        if parts:
            self.write(f'C{channel}:OUTP {",".join(parts)}')

    def get_basic_wave(self, channel):
        """Raw query of the current basic-wave settings on a channel."""
        return self.ask(f'C{channel}:BSWV?')

    def get_basic_wave_dict(self, channel):
        """Parse the current basic-wave settings into a dict.

        The instrument echoes e.g.
            C1:BSWV WVTP,SINE,FRQ,100HZ,PERI,0.01S,AMP,2V,OFST,0V,...
        The leading 'C1:BSWV ' echo is stripped, the remainder split into
        KEY,VALUE pairs; numeric values (see _BSWV_NUMERIC) are converted to
        float with their unit suffix removed. WVTP and unknown keys are kept as
        raw strings. Tolerant of the variable, wave-type-dependent key list.
        """
        raw = self.get_basic_wave(channel)
        body = raw.split(' ', 1)[1] if ' ' in raw else raw
        tokens = [t for t in body.split(',') if t != '']
        out = {}
        for key, value in zip(tokens[0::2], tokens[1::2]):
            key = key.upper()
            out[key] = self._strip_unit(value) if key in self._BSWV_NUMERIC else value
        return out

    def get_output_dict(self, channel):
        """Parse output state into {'state': bool, 'load': str, 'polarity': str}.

        The instrument echoes e.g. 'C1:OUTP ON,LOAD,HZ,PLRT,NOR'. Load is kept
        as a string ('HZ' or a numeric ohm value like '50').
        """
        raw = self.ask(f'C{channel}:OUTP?')
        body = raw.split(' ', 1)[1] if ' ' in raw else raw
        tokens = [t for t in body.split(',') if t != '']
        state = tokens[0].upper() == 'ON' if tokens else False
        pairs = dict(zip(tokens[1::2], tokens[2::2]))
        return {
            'state': state,
            'load': pairs.get('LOAD', 'HZ'),
            'polarity': pairs.get('PLRT', 'NOR'),
        }

    # -- Burst / sync output (issue #45; short commands, USB-safe) ---------
    # Response forms bench-captured 2026-07-10 (fw 1.01.01.33R3):
    #   C1:BTWV? -> 'C1:BTWV STATE,OFF'      C1:SYNC? -> 'C1:SYNC OFF'

    _BTWV_NUMERIC = {'TIME', 'PRD', 'DLAY'}

    def set_burst(self, channel, on, ncycles=1, trigger='MAN', period_s=None):
        """Configure N-cycle burst on a channel.

        Each trigger emits exactly `ncycles` cycles of the channel's basic
        wave and then idles -- bounded energy per shot, the safest way to
        exercise a high-voltage amplifier. trigger: 'MAN' (fire via
        burst_trigger() / front panel), 'INT' (auto-repeat every `period_s`
        seconds), 'EXT' (rear Aux In). Sent as separate short messages,
        each far under the 52-byte USB cap.
        """
        if not on:
            self.write(f'C{channel}:BTWV STATE,OFF')
            return
        trigger = trigger.upper()
        if trigger not in ('MAN', 'INT', 'EXT'):
            raise ValueError("Burst trigger must be MAN, INT or EXT")
        if int(ncycles) < 1:
            raise ValueError("Burst cycle count must be >= 1")
        # Set the trigger source (and cycle count) BEFORE enabling the
        # burst. The box defaults to an INTERNAL trigger, so enabling
        # STATE,ON first fired one cycle immediately -- an actuator got hit
        # on every Apply even with MAN selected (bench report 2026-07-20).
        # Arming last, already in the requested trigger mode, avoids that.
        self.write(f'C{channel}:BTWV TRSR,{trigger}')
        self.write(f'C{channel}:BTWV GATE_NCYC,NCYC')
        self.write(f'C{channel}:BTWV TIME,{int(ncycles)}')
        if trigger == 'INT' and period_s:
            self.write(f'C{channel}:BTWV PRD,{period_s:g}')
        self.write(f'C{channel}:BTWV STATE,ON')
        # Re-assert the source after enabling, in case this firmware only
        # accepts TRSR once the burst state is on.
        self.write(f'C{channel}:BTWV TRSR,{trigger}')

    def burst_trigger(self, channel):
        """Fire one manual burst (burst must be ON with trigger MAN)."""
        self.write(f'C{channel}:BTWV MTRIG')

    def get_burst_dict(self, channel):
        """Parse BTWV? -> dict, e.g. {'STATE': 'ON', 'TRSR': 'MAN',
        'TIME': 5.0}. Burst off replies just 'C1:BTWV STATE,OFF'."""
        raw = self.ask(f'C{channel}:BTWV?')
        body = raw.split(' ', 1)[1] if ' ' in raw else raw
        tokens = [t for t in body.split(',') if t != '']
        out = {}
        for key, value in zip(tokens[0::2], tokens[1::2]):
            key = key.upper()
            out[key] = (self._strip_unit(value)
                        if key in self._BTWV_NUMERIC else value)
        return out

    def set_sync(self, channel, on):
        """Sync/marker output for the channel (rear Aux/Sync BNC): one
        hardware edge per waveform period -- scope trigger for slow arbs."""
        self.write(f'C{channel}:SYNC {"ON" if on else "OFF"}')

    def get_sync(self, channel):
        """-> bool from 'C1:SYNC ON'/'C1:SYNC OFF' (extra fields tolerated)."""
        raw = self.ask(f'C{channel}:SYNC?')
        body = raw.split(' ', 1)[1] if ' ' in raw else raw
        return body.split(',')[0].strip().upper() == 'ON'

    # -- arbitrary waveforms -------------------------------------------------

    ARB_MAX_POINTS = 16384      # editor design cap / DDS table depth
    ARB_POINTS = 16384          # legacy fixed DDS buffer length (TrueArb does
                                # NOT need this -- kept for set_legacy callers)
    ARB_DEFAULT_POINTS = 1024   # default TrueArb upload depth: a short buffer
                                # (~2 KB) uploads fast and is far less likely to
                                # wedge the USBTMC endpoint than a 32 KB DDS one.

    @staticmethod
    def _resample(samples, n):
        """Circularly resample a periodic waveform to n points (linear)."""
        m = len(samples)
        if m == n:
            return list(samples)
        if m == 0:
            return [0.0] * n
        if m == 1:
            return [float(samples[0])] * n
        out = []
        for i in range(n):
            x = i * m / n              # phase in input samples [0, m)
            lo = int(x) % m
            frac = x - int(x)
            hi = (lo + 1) % m
            out.append(samples[lo] * (1 - frac) + samples[hi] * frac)
        return out

    @staticmethod
    def samples_to_int16(samples):
        """Normalise float samples to full-scale signed 16-bit LE bytes.

        Values are scaled so the largest |sample| maps to full scale when any
        |sample| exceeds 1.0; otherwise [-1, 1] maps directly. Full scale is
        +/-32767 (symmetric), little-endian two's complement per the Siglent
        SDG arb format.
        """
        if not samples:
            raise ValueError("samples must be a non-empty sequence")
        peak = max(abs(float(s)) for s in samples)
        scale = 32767.0 / peak if peak > 1.0 else 32767.0
        out = bytearray()
        for s in samples:
            v = int(round(float(s) * scale))
            v = max(-32768, min(32767, v))
            out += struct.pack('<h', v)
        return bytes(out)

    def build_wvdt(self, channel, name, samples, points=None, freq_hz=None,
                   amp_vpp=None, offset_v=None, phase_deg=None):
        """Build the raw ``WVDT`` upload bytes (no I/O). Returns (clean, blob).

        Uses the MINIMAL header -- the only form this firmware accepts::

            C<n>:WVDT WVNM,<name>,WAVEDATA,<int16 LE>

        The full-field form from the Siglent 16-bit-arb app note
        (``FREQ,...,TYPE,8,AMPL,...,OFST,...,PHASE,...``) is silently
        REJECTED by the 4055B: the box stays alive but stores nothing
        (bench-verified 2026-07-02; the tinylabs SDG2000X library uses the
        minimal form for the same reason). Frequency, amplitude and offset
        are set separately via ``SRATE``/``BSWV`` after ``select_arb``. The
        ``freq_hz``/``amp_vpp``/``offset_v``/``phase_deg`` kwargs are
        accepted for API compatibility but intentionally unused. Sample
        bytes follow ``WAVEDATA,`` directly (not an IEEE block); no trailing
        terminator.

        Split out from upload_arb so it can be unit-tested headless and verified
        with ``bench/test_arb_scope.py --readback`` without driving the bus.
        """
        clean = re.sub(r'[^A-Za-z0-9_]', '_', str(name))[:16]
        if not clean:
            raise ValueError(f"unusable arb name {name!r}")
        if not samples:
            raise ValueError("samples must be a non-empty sequence")
        n = points if points is not None else min(len(samples),
                                                  self.ARB_DEFAULT_POINTS)
        n = max(8, min(int(n), self.ARB_MAX_POINTS))
        data = self.samples_to_int16(self._resample(samples, n))
        header = f'C{channel}:WVDT WVNM,{clean},WAVEDATA,'
        return clean, header.encode('latin1') + data

    def upload_arb(self, channel, name, samples, freq_hz=None, amp_vpp=None,
                   offset_v=None, phase_deg=None, points=None):
        """Upload an arbitrary waveform to the instrument's user memory.

        REQUIRES A LAN RESOURCE. Over USB this firmware caps every command at
        52 bytes: a one-message upload wedges the box (front-panel power cycle
        to recover) and chained single-packet messages silently truncate the
        waveform to its first ~24 data bytes (bench-verified 2026-07-02 via
        ``WVDT?`` readback -- the name registers, the data does not). So this
        method refuses to even try over USB. Open the box via LAN instead::

            sg = BK4055B(resource='TCPIP0::<ip>::INSTR')

        Defaults to a SHORT buffer (``ARB_DEFAULT_POINTS`` = 1024 points,
        ~2 KB) in the minimal WVDT form (see ``build_wvdt``). Pass ``points``
        to override the resample depth (capped to ARB_MAX_POINTS).

        Pair with ``set_sample_rate(channel, 'TARB', samples*freq)`` so the box
        plays the exact buffer at the right rate, then ``select_arb``. Output
        level goes via ``set_basic_wave`` (the WVDT header cannot carry it --
        the full-field form is rejected, see ``build_wvdt``).

        name is sanitised to [A-Za-z0-9_], max 16 chars; returns the clean name.
        """
        if self._usb_transport():
            raise RuntimeError(
                "Arb upload over USB is impossible on this 4055B firmware: "
                "USB commands are capped at 52 bytes (longer transfers wedge "
                "the box; chained messages are silently truncated). Connect "
                "the instrument via Ethernet and open it as "
                "BK4055B(resource='TCPIP0::<ip>::INSTR')."
            )
        clean, blob = self.build_wvdt(channel, name, samples, points=points,
                                      freq_hz=freq_hz, amp_vpp=amp_vpp,
                                      offset_v=offset_v, phase_deg=phase_deg)
        # Give the box headroom to finish storing before the next command.
        old_to = self.inst.timeout
        self.inst.timeout = max(old_to or 0, 20000)
        try:
            self.write_raw(blob)
        finally:
            self.inst.timeout = old_to
        return clean

    def set_sample_rate(self, channel, mode=None, value=None):
        """Set the arb playback mode (DDS vs TrueArb) and/or sample rate.

        ``C<n>:SRATE MODE,<DDS|TARB>,VALUE,<Sa/s>``. In TrueArb mode the box
        plays the uploaded buffer point-by-point at <value> Sa/s, so the output
        frequency = sample_rate / num_points -- this is the SDG2000X/4055B's
        dedicated path for short custom arbs and avoids the DDS 16k resample.
        The SDG2000X supports MODE and VALUE but not the INTER (interpolation)
        parameter, so it is intentionally not exposed here.
        """
        parts = []
        if mode is not None:
            parts.append(f'MODE,{mode}')
        if value is not None:
            parts.append(f'VALUE,{self._fmt_param(value)}')
        if not parts:
            raise ValueError("set_sample_rate needs a mode and/or value")
        self.write(f'C{channel}:SRATE ' + ','.join(parts))

    def get_sample_rate_dict(self, channel):
        """Parse 'C1:SRATE MODE,TARB,VALUE,1000000Sa/s,...' into a dict.

        Returns {'mode': 'TARB'|'DDS'|None, 'value': float|None}.
        """
        raw = self.ask(f'C{channel}:SRATE?')
        body = raw.split(' ', 1)[1] if ' ' in raw else raw
        tokens = [t for t in body.split(',') if t != '']
        pairs = {k.upper(): v for k, v in zip(tokens[0::2], tokens[1::2])}
        value = pairs.get('VALUE')
        if value is not None:
            try:
                value = self._strip_unit(value)
            except Exception:
                pass
        return {'mode': pairs.get('MODE'), 'value': value}

    def select_arb(self, channel, name):
        """Put a channel in ARB mode playing the named user waveform."""
        self.write(f'C{channel}:BSWV WVTP,ARB')
        self.write(f'C{channel}:ARWV NAME,{name}')

    def get_arb_dict(self, channel):
        """Parse 'C1:ARWV INDEX,2,NAME,wave1' -> {'index': 2, 'name': 'wave1'}.

        Either key may be missing depending on firmware; absent keys are
        returned as None.
        """
        raw = self.ask(f'C{channel}:ARWV?')
        body = raw.split(' ', 1)[1] if ' ' in raw else raw
        tokens = [t for t in body.split(',') if t != '']
        pairs = {k.upper(): v for k, v in zip(tokens[0::2], tokens[1::2])}
        index = pairs.get('INDEX')
        try:
            index = int(index) if index is not None else None
        except ValueError:
            pass
        return {'index': index, 'name': pairs.get('NAME')}


# --------------------------------------------------------------------------
# Serial-attached DC power supply (CP2102 USB-UART bridge)
# --------------------------------------------------------------------------

class BK9174B:
    """B&K Precision 9174B dual-output, dual-range programmable DC supply.

    Serial transport over the built-in CP2102 USB-UART bridge (VID 0x10c4,
    PID 0xea60, s/n 503B22108) -> /dev/ttyUSB0. This is a virtual COM port,
    NOT USB-TMC, so it does not appear in list_usb_instruments(). Transport
    parameters are taken from B&K's own example code
    (github.com/bkprecisioncorp/9170-and-9180_Series, stepExample9174B.py):
    57600 baud, 8N1, CR+LF terminators.

    Two independent outputs, each dual-range:
        <=35 V -> up to 3.0 A     (105 W)
        35-70 V -> up to 1.5 A    (105 W)
    apply() enforces this envelope; set_voltage/set_current guard the
    absolute bounds. Channel index is 1 or 2.

    Requires pyserial (imported lazily so this module loads without it).

    CHANNEL ADDRESSING (from the 9170B/9180B series manual section 4.2, and
    verified read-only against this unit 2026-07-22): the 9174B has NO
    channel-select command. Channel 1 uses the bare command; channel 2 uses
    a '2'-suffixed token. So CH1 vs CH2 is VOLT/VOLT2, CURR/CURR2,
    MEAS:VOLT?/MEAS:VOLT2?, MEAS:CURR?/MEAS:CURR2?, OUT/OUT2, and
    PROT:OVP:LEV/PROT:OVP2:LEV. The [SOURce] prefix is optional (bare VOLT?
    reads back correctly). _sfx(ch) returns that suffix and drives every
    command -- there is no modal channel state to leak between calls.
    """
    DEFAULT_PORT = '/dev/ttyUSB0'
    DEFAULT_BAUD = 57600            # confirmed from BK's stepExample9174B.py
    WRITE_TERM = '\r\n'            # 9174B example uses b'...\r\n'
    CHANNELS = (1, 2)

    # Per-channel commands are built from a '' / '2' suffix (see _sfx); only
    # the channel-agnostic ones are named constants.
    CMD_IDN = '*IDN?'
    CMD_REMOTE = 'SYST:REM'      # front-panel Local key returns to local
    CMD_ERROR = 'SYST:ERR?'      # -> "<code>,<text>"; code 0 = no error

    # Dual-range safety envelope (per output).
    V_MAX = 70.0
    RANGE_LOW_V = 35.0        # boundary between the two current ranges
    I_LOW_RANGE = 3.0         # V <= 35 -> up to 3 A (also the absolute I max)
    I_HIGH_RANGE = 1.5        # 35 < V <= 70 -> up to 1.5 A

    def __init__(self, port=None, baud=None, timeout=1.0, identify=True,
                 transport=None):
        """Open the serial port and (optionally) read *IDN?.

        transport: an already-open serial-like object (write(bytes)/
        readline()->bytes/close()); used by the headless tests to avoid
        touching real hardware. When None a pyserial port is opened.
        """
        self.port = port or self.DEFAULT_PORT
        self.baud = baud or self.DEFAULT_BAUD
        self.resource = self.port      # lets the GUI label the transport
        self.idn = ''
        if transport is not None:
            self.ser = transport
        else:
            import serial  # lazy import; keeps this module importable without it
            self.ser = serial.Serial(self.port, baudrate=self.baud,
                                     timeout=timeout)
        if identify:
            try:
                self.idn = self.query(self.CMD_IDN)
            except Exception:
                self.idn = ''

    # --- transport ---------------------------------------------------------
    def write(self, command):
        with _io_lock(self):
            self.ser.write((command + self.WRITE_TERM).encode('ascii'))

    def read(self):
        with _io_lock(self):
            return self.ser.readline().decode('ascii', errors='replace').strip()

    def query(self, command):
        # one lock across write+read: the reply must be OURS
        with _io_lock(self):
            self.write(command)
            return self.read()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _fmt(value):
        """Plain decimal, no exponent, no trailing zeros: 12.0->'12',
        0.3->'0.3', 0.0001->'0.0001'."""
        return f'{float(value):.4f}'.rstrip('0').rstrip('.') or '0'

    @staticmethod
    def _to_float(raw):
        """Parse a numeric reply, dropping any unit suffix ('12.003V')."""
        m = re.match(r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?',
                     (raw or '').strip())
        if not m:
            raise ValueError(f"expected a number, got {raw!r}")
        return float(m.group())

    def _check_channel(self, ch):
        if ch not in self.CHANNELS:
            raise ValueError(f"channel must be one of {self.CHANNELS}, "
                             f"got {ch!r}")
        return ch

    def _check_envelope(self, voltage_v, current_a):
        """Raise unless (V, I) fits the dual-range envelope."""
        v = float(voltage_v)
        a = float(current_a)
        if not 0 <= v <= self.V_MAX:
            raise ValueError(f"voltage {v} V out of range 0..{self.V_MAX} V")
        limit = self.I_LOW_RANGE if v <= self.RANGE_LOW_V else self.I_HIGH_RANGE
        if not 0 <= a <= limit:
            raise ValueError(
                f"current {a} A exceeds the {limit} A limit at {v} V "
                f"(dual-range: <=35 V -> 3 A, 35-70 V -> 1.5 A)")

    # --- channel addressing ------------------------------------------------
    @staticmethod
    def _sfx(ch):
        """SCPI token suffix for a channel: '' for CH1, '2' for CH2. The
        9174B has no channel-select -- outputs are addressed by name."""
        return '' if ch == 1 else '2'

    # --- control -----------------------------------------------------------
    def set_remote(self):
        self.write(self.CMD_REMOTE)

    def go_local(self):
        """Return the front panel to local control (best-effort)."""
        try:
            self.write('SYST:LOC')
        except Exception:
            pass

    def set_voltage(self, ch, voltage_v):
        self._check_channel(ch)
        v = float(voltage_v)
        if not 0 <= v <= self.V_MAX:
            raise ValueError(f"voltage {v} V out of range 0..{self.V_MAX} V")
        self.write(f'VOLT{self._sfx(ch)} {self._fmt(v)}')

    def set_current(self, ch, current_a):
        self._check_channel(ch)
        a = float(current_a)
        if not 0 <= a <= self.I_LOW_RANGE:
            raise ValueError(f"current {a} A exceeds absolute max "
                             f"{self.I_LOW_RANGE} A")
        self.write(f'CURR{self._sfx(ch)} {self._fmt(a)}')

    def set_output(self, ch, on):
        self._check_channel(ch)
        self.write(f'OUT{self._sfx(ch)} {"ON" if on else "OFF"}')

    def apply(self, ch, voltage_v, current_a, output=None):
        """Set V and I limit together with the dual-range envelope enforced.

        output: True/False to also switch the output, or None to leave the
        output state untouched (the safe default -- a PSU may be powering a
        live DUT)."""
        self._check_channel(ch)
        self._check_envelope(voltage_v, current_a)
        s = self._sfx(ch)
        self.write(f'VOLT{s} {self._fmt(voltage_v)}')
        self.write(f'CURR{s} {self._fmt(current_a)}')
        if output is not None:
            self.write(f'OUT{s} {"ON" if output else "OFF"}')

    def set_ovp(self, ch, voltage_v):
        """Set the over-voltage trip level and arm OVP for the channel."""
        self._check_channel(ch)
        s = self._sfx(ch)
        self.write(f'PROT:OVP{s}:LEV {self._fmt(voltage_v)}')
        self.write(f'PROT:OVP{s} ON')

    def set_ocp(self, ch, current_a):
        """Set the over-current trip level and arm OCP for the channel."""
        self._check_channel(ch)
        s = self._sfx(ch)
        self.write(f'PROT:OCP{s}:LEV {self._fmt(current_a)}')
        self.write(f'PROT:OCP{s} ON')

    def clear_protection(self):
        """Clear a tripped OVP/OCP latch (PROT:CLE, whole supply)."""
        self.write('PROT:CLE')

    # --- reads (safe: queries only) ---------------------------------------
    def get_setpoint_voltage(self, ch):
        self._check_channel(ch)
        return self._to_float(self.query(f'VOLT{self._sfx(ch)}?'))

    def get_setpoint_current(self, ch):
        self._check_channel(ch)
        return self._to_float(self.query(f'CURR{self._sfx(ch)}?'))

    def get_output(self, ch):
        self._check_channel(ch)
        return self.query(f'OUT{self._sfx(ch)}?').strip().upper() \
            in ('1', 'ON', 'TRUE')

    def measure_voltage(self, ch):
        self._check_channel(ch)
        return self._to_float(self.query(f'MEAS:VOLT{self._sfx(ch)}?'))

    def measure_current(self, ch):
        self._check_channel(ch)
        return self._to_float(self.query(f'MEAS:CURR{self._sfx(ch)}?'))

    def measure_power(self, ch):
        return self.measure_voltage(ch) * self.measure_current(ch)

    def get_error(self):
        """(code, raw) from SYST:ERR?. code 0 = no error; 1 command,
        2 execution, 3 query, 4 input-range; -1 if unparseable."""
        raw = self.query(self.CMD_ERROR)
        try:
            code = int(float(raw.split(',')[0]))
        except (ValueError, IndexError):
            code = -1
        return code, raw

    def read_channel(self, ch):
        """One poll of a channel for logging: setpoint voltage + measured
        voltage/current, with calculated power (P = V_meas * I_meas)."""
        self._check_channel(ch)
        s = self._sfx(ch)
        set_v = self._to_float(self.query(f'VOLT{s}?'))
        meas_v = self._to_float(self.query(f'MEAS:VOLT{s}?'))
        meas_i = self._to_float(self.query(f'MEAS:CURR{s}?'))
        return {'channel': ch, 'set_voltage_v': set_v,
                'meas_voltage_v': meas_v, 'meas_current_a': meas_i,
                'power_w': meas_v * meas_i}


class BK5493C:
    """B&K Precision 5493C 6.5-digit bench DMM over LAN.

    NOT USB-TMC / VXI-11: the 5493C's LAN SCPI is a plain TCP socket on the
    non-standard **port 45454** (this unit's USB enumeration failed -- issue
    #5 -- so it is driven over Ethernet). Opened through pyvisa-py as a SOCKET
    resource. The meter's IP is set on its LAN menu ("DHCP Once" puts it on
    the bench 192.168.68.0/22); override the default with SCPI_DMM_ADDR.

        dmm = BK5493C(addr='192.168.68.58')
        dmm.measure('DC Voltage')   # -> float volts

    Verified 2026-07-22: IDN 'BK Precision,5493C,W117229033,V1.4.19'.
    """
    DEFAULT_ADDR = '192.168.68.58'
    PORT = 45454
    # (label, SCPI MEASure query, unit) -- MEAS auto-ranges + returns a reading
    FUNCTIONS = (
        ('DC Voltage', 'MEAS:VOLT:DC?', 'V'),
        ('AC Voltage', 'MEAS:VOLT:AC?', 'V'),
        ('DC Current', 'MEAS:CURR:DC?', 'A'),
        ('AC Current', 'MEAS:CURR:AC?', 'A'),
        ('2W Resistance', 'MEAS:RES?', 'ohm'),
        ('4W Resistance', 'MEAS:FRES?', 'ohm'),
        ('Frequency', 'MEAS:FREQ?', 'Hz'),
        ('Capacitance', 'MEAS:CAP?', 'F'),
    )
    _QUERY = {label: q for label, q, u in FUNCTIONS}
    _UNIT = {label: u for label, q, u in FUNCTIONS}

    def __init__(self, addr=None, rm=None, resource=None, transport=None,
                 identify=True):
        if transport is not None:                 # injected fake for tests
            self.inst = transport
            self.resource = resource or 'FAKE::DMM'
        else:
            rm = rm if rm is not None else get_resource_manager()
            self.resource = resource or \
                f'TCPIP0::{addr or self.DEFAULT_ADDR}::{self.PORT}::SOCKET'
            self.inst = rm.open_resource(self.resource)
            self.inst.read_termination = '\n'
            self.inst.write_termination = '\n'
            self.inst.timeout = 5000
        try:
            self.idn = self.inst.query('*IDN?').strip() if identify else ''
        except Exception:
            # close on failed identify -- the DMM's socket accepts a single
            # client, so a leaked session blocks every reconnect
            try:
                self.inst.close()
            except Exception:
                pass
            raise

    @classmethod
    def function_labels(cls):
        return [label for label, q, u in cls.FUNCTIONS]

    def unit(self, function):
        return self._UNIT.get(function, '')

    def measure(self, function):
        """Trigger + read one measurement of `function`; float, or None if the
        meter returns a non-numeric (e.g. overload)."""
        q = self._QUERY.get(function)
        if q is None:
            raise ValueError(f"unknown DMM function {function!r}")
        with _io_lock(self):
            raw = self.inst.query(q).strip()
        try:
            return float(raw)
        except ValueError:
            return None

    def query(self, command):
        """Raw SCPI passthrough (e.g. SYST:ERR?)."""
        return self.inst.query(command).strip()

    def go_local(self):
        """Return the meter's front panel to local control (best-effort)."""
        try:
            self.inst.write('SYST:LOC')
        except Exception:
            pass

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Smoke test
# --------------------------------------------------------------------------

if __name__ == '__main__':
    print("--- Enumerated USB-TMC resources ---")
    for r, vid, pid in list_usb_instruments():
        print(f"  {r}  (VID=0x{vid:04x} PID=0x{pid:04x})")

    print()
    for cls in (BK894, TekMSO24, BK4055B):
        print(f"--- {cls.__name__} ---")
        try:
            inst = cls()
            print(f"  IDN: {inst.idn}")
            if isinstance(inst, BK4055B):
                for ch in (1, 2):
                    print(f"  CH{ch} BSWV: {inst.get_basic_wave_dict(ch)}")
            inst.close()
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")
