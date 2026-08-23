#!/usr/bin/env python3
"""Headless tests: live HAL drivers against recording stubs -- every
SCPI interaction the bench will see, pinned before any hardware is
touched. BENCH_TEST sections P/Q then verify the FIRMWARE side.

Run: .venv/Scripts/python.exe tests/test_lc_live_hal.py
"""
import os as _os
import sys as _sys

_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for p in (_root, _os.path.join(_root, 'lib')):
    if p not in _sys.path:
        _sys.path.insert(0, p)

from hal.drive_bk4055b import BK4055BDrive
from hal.monitor_mso24 import MSO24Monitor


class StubSG:
    """Records the vendored-driver calls BK4055BDrive makes."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def _rec(self, name, *a, **kw):
        if self.fail:
            raise IOError('link down')
        self.calls.append((name,) + a + (tuple(sorted(kw.items())),))

    def set_load_polarity(self, ch, load=None, polarity=None):
        self._rec('load_pol', ch, load, polarity)

    def set_basic_wave(self, ch, **kw):
        self._rec('bswv', ch, **kw)

    def set_offset(self, ch, v):
        self._rec('offset', ch, round(v, 6))

    def set_output(self, ch, on):
        self._rec('output', ch, on)

    def set_burst(self, ch, on, ncycles=1, trigger='MAN',
                  period_s=None):
        self._rec('burst', ch, on, ncycles, trigger)

    def burst_trigger(self, ch):
        self._rec('mtrig', ch)

    def write(self, cmd):
        self._rec('raw', cmd)

    def has(self, name):
        return [c for c in self.calls if c[0] == name]


class StubScope:
    def __init__(self, values=None, asks=None):
        self.values = values or {}
        self.asks = asks or {}
        self.writes = []

    def measure_raw(self, kind, ch):
        return self.values.get((kind, ch), (None, 'invalid'))

    def ask(self, cmd):
        if cmd in self.asks:
            return self.asks[cmd]
        raise IOError('no such query')

    def write(self, cmd):
        self.writes.append(cmd)

    def get_waveform(self, ch):
        return {'t': [0, 1], 'v': [0.0, 1.0], 'dt': 1, 'npts': 2}


# ---- drive ---------------------------------------------------------------

def test_configure_cycle_writes_bswv_and_stps270():
    sg = StubSG()
    d = BK4055BDrive(sg, 1, trek_sign=1.0)
    d.specimen_cap_kv = 10.0
    d.configure_cycle('SINE', 5.0, 1.5, 0.0)
    bswv = sg.has('bswv')[0]
    kw = dict(bswv[-1])
    assert kw['WVTP'] == 'SINE' and kw['FRQ'] == 5.0
    assert abs(kw['AMP'] - 1.5) < 1e-9      # span
    assert abs(kw['OFST'] - 0.75) < 1e-9    # midpoint
    raws = [c[1] for c in sg.has('raw')]
    assert 'C1:BTWV STPS,270' in raws       # idle at MINIMUM = 0 kV
    assert sg.has('burst')[0][4] == 'MAN'   # never INT


def test_inverted_trek_flips_offset_and_idle_phase():
    sg = StubSG()
    d = BK4055BDrive(sg, 1, trek_sign=-1.0)
    d.specimen_cap_kv = 10.0
    d.configure_cycle('SINE', 2.0, 1.75, 0.0)
    kw = dict(sg.has('bswv')[0][-1])
    assert kw['OFST'] < 0                   # mirrored control waveform
    raws = [c[1] for c in sg.has('raw')]
    assert 'C1:BTWV STPS,90' in raws        # idle at the 0 kV end


def test_specimen_cap_clamped_in_every_set():
    sg = StubSG()
    d = BK4055BDrive(sg, 1)
    d.specimen_cap_kv = 2.0
    d.set_kv(5.0)                           # recipe bug / bad caller
    assert sg.has('offset')[-1][2] == 2.0   # clamped, not passed through
    d.configure_cycle('SINE', 1.0, 5.0, 0.0)
    kw = dict(sg.has('bswv')[-1][-1])
    assert abs(kw['OFST'] - 1.0) < 1e-9     # mid of clamped 0..2


def test_fire_burst_counts_and_duration():
    sg = StubSG()
    d = BK4055BDrive(sg, 1)
    d.configure_cycle('SINE', 5.0, 1.0, 0.0)
    dur = d.fire_burst(1000)
    assert abs(dur - 200.0) < 1e-9
    assert sg.has('burst')[-1][3] == 1000
    assert sg.has('mtrig')


def test_zero_dual_handle_and_not_zeroed():
    dead = StubSG(fail=True)
    alive = StubSG()
    d = BK4055BDrive(dead, 1, sg_getter=lambda: alive)
    assert d.zero() is True                 # fell through to the getter
    assert alive.has('output')[0][2] is False
    d2 = BK4055BDrive(dead, 1, sg_getter=lambda: dead)
    assert d2.zero() is False               # NOT_ZEROED path
    ok = BK4055BDrive(alive, 1)
    assert ok.zero() is True
    # order: output OFF before the DC zero (never mid-burst STATE,OFF)
    names = [c[0] for c in alive.calls[-2:]]
    assert names == ['output', 'bswv']


# ---- monitor -------------------------------------------------------------

def test_monitor_sign_and_units():
    sc = StubScope(values={('MEAN', 2): (1.5, 'ok'),
                           ('MEAN', 3): (0.1, 'ok'),
                           ('FREQUENCY', 2): (5.02, 'ok')})
    m = MSO24Monitor(sc, 2, 3, trek_sign=1.0)
    kv, st = m.read_kv()
    assert abs(kv - 1.5) < 1e-9 and st == 'ok'
    ua, st = m.read_ua()
    assert abs(ua - 20.0) < 1e-9            # 0.1 V * 200 uA/V
    f, _ = m.read_stat('FREQUENCY', 'v')
    assert abs(f - 5.02) < 1e-9             # Hz, unconverted
    inv = MSO24Monitor(sc, 2, 3, trek_sign=-1.0)
    kv, _ = inv.read_kv()
    assert abs(kv + 1.5) < 1e-9


def test_monitor_offscreen_passthrough():
    sc = StubScope(values={('MEAN', 3): (None, 'offscreen')})
    m = MSO24Monitor(sc, 2, 3)
    ua, st = m.read_ua()
    assert ua is None and st == 'offscreen'


def test_check_window_catches_the_2026_07_25_bug():
    # CH2 at 2.6 mV/div for a 0-10 V monitor; CH3 with a 10x factor
    asks = {'CH2:SCALE?': '0.0026', 'CH2:PROBEFUNC:EXTATTEN?': '1',
            'CH2:POSITION?': '0', 'CH2:OFFSET?': '0',
            'CH3:SCALE?': '1', 'CH3:PROBEFUNC:EXTATTEN?': '10',
            'CH3:POSITION?': '0', 'CH3:OFFSET?': '0',
            'HORIZONTAL:SCALE?': '0.1'}
    m = MSO24Monitor(StubScope(asks=asks), 2, 3)
    problems, plan = m.check_window(10.0, 100.0, 5.0)
    assert problems, 'the bench bug must be caught'
    assert plan is not None
    assert m.apply_fix(plan)


def test_check_window_horizontal_rule():
    asks = {'CH2:SCALE?': '2', 'CH2:PROBEFUNC:EXTATTEN?': '1',
            'CH2:POSITION?': '-4', 'CH2:OFFSET?': '0',
            'CH3:SCALE?': '0.2', 'CH3:PROBEFUNC:EXTATTEN?': '1',
            'CH3:POSITION?': '0', 'CH3:OFFSET?': '0',
            'HORIZONTAL:SCALE?': '0.01'}     # 0.1 s window
    m = MSO24Monitor(StubScope(asks=asks), 2, 3)
    problems, plan = m.check_window(10.0, 100.0, 5.0)
    horiz = [p for p in problems if 'horizontal' in p]
    assert horiz, problems                   # 0.1 s < 2/5 Hz = 0.4 s


def test_camera_unavailable_paths():
    import webcam
    from hal.camera_dfk import DFKCamera
    orig = webcam.resolve_camera
    webcam.resolve_camera = lambda i: None
    try:
        cam = DFKCamera(0)
        assert not cam.available()
        assert cam.grab() is None
        ok, txt, frame = cam.preflight()
        assert not ok and frame is None
        with cam.locked():
            pass                             # no device: still safe
    finally:
        webcam.resolve_camera = orig


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_')]
    for fn in fns:
        fn()
        print(f'ok  {fn.__name__}')
    print(f'\n{len(fns)} tests passed')


if __name__ == '__main__':
    _run()
