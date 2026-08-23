#!/usr/bin/env python3
"""USB webcam capture: device probing, focus metric, and capture planning.

Capture uses OpenCV (``cv2.VideoCapture``); the GUI displays frames via Pillow.
Everything that does NOT need the heavy deps (device probing, focus metric,
filename + voltage-step planning) is import-safe without cv2/numpy/PIL, so this
module loads on any bench and the GUI tab can degrade gracefully when the
capture deps or a camera are missing.

cv2/numpy are imported lazily inside the functions that need them.
"""
import glob
import math
import os
import re
import select
import shutil
import subprocess

# Quiet OpenCV's noisy V4L2/FFMPEG probe warnings (read by cv2 at import time;
# we import cv2 lazily, after this is set).
os.environ.setdefault('OPENCV_LOG_LEVEL', 'ERROR')


# -- capture dependency probe ----------------------------------------------

def deps_available():
    """Return (ok, reason). ok=True iff cv2, numpy and PIL all import."""
    missing = []
    for mod in ('cv2', 'numpy', 'PIL'):
        try:
            __import__(mod)
        except Exception:
            missing.append('Pillow' if mod == 'PIL' else mod)
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, "ok"


def list_cameras(max_index=8):
    """Return a list of available camera indices.

    On Linux, V4L2 exposes cameras as /dev/videoN; we map those device numbers
    to indices. Falls back to probing cv2 indices when no /dev/video* exist.
    """
    devs = sorted(glob.glob('/dev/video*'))
    if devs:
        idxs = []
        for d in devs:
            tail = d.replace('/dev/video', '')
            if tail.isdigit():
                idxs.append(int(tail))
        # Even-numbered /dev/video nodes are usually the capture device on
        # multi-node UVC cameras, but return all so the user can pick.
        return sorted(set(idxs))
    # No sysfs nodes (non-Linux or unusual setup): probe cv2 indices.
    try:
        import cv2
    except Exception:
        return []
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if cap is not None and cap.isOpened():
            found.append(i)
            cap.release()
    return found


# -- focus / sharpness metric ----------------------------------------------

# Central "area of interest" for the focus metric: a Gaussian centred on
# the frame, sigma as a fraction of the smaller dimension. Small == strong
# == only the very middle counts. The preview draws a green circle at
# ~2 sigma (FOCUS_AOI_RADIUS_FRAC of the smaller dimension) to show it.
FOCUS_AOI_SIGMA_FRAC = 0.11
FOCUS_AOI_RADIUS_FRAC = 2.0 * FOCUS_AOI_SIGMA_FRAC

_focus_weight_cache = {}


def _focus_weight(h, w, sigma_frac):
    import numpy as np
    key = (h, w, round(sigma_frac, 4))
    W = _focus_weight_cache.get(key)
    if W is None:
        sigma = max(1.0, sigma_frac * min(h, w))
        yy, xx = np.ogrid[:h, :w]
        d2 = (yy - (h - 1) / 2.0) ** 2 + (xx - (w - 1) / 2.0) ** 2
        W = np.exp(-d2 / (2.0 * sigma * sigma))
        _focus_weight_cache[key] = W
    return W


def focus_score(image, sigma_frac=FOCUS_AOI_SIGMA_FRAC):
    """Center-weighted, noise-robust sharpness score (higher = sharper).

    Two departures from a plain variance-of-Laplacian, both fixing real
    problems seen on the bench camera (2026-07-20):

    * **Block-downsample first.** Sensor noise (bad at high gain) is
      per-pixel and UNcorrelated, so a raw Laplacian variance rewards a
      noisy defocused frame over a clean focused one -- the score ran
      backwards. Averaging ~6x6 blocks cancels the uncorrelated noise but
      keeps real, spatially-correlated edges, so higher now really means
      sharper.
    * **Weight by a strong central Gaussian** so the score reflects the
      middle "area of interest" (the subject) rather than the periphery.

    Accepts a 2-D grayscale or 3-D RGB/BGR array (or nested lists). numpy.
    """
    import numpy as np
    a = np.asarray(image, dtype=np.float64)
    if a.ndim == 3:
        a = a.mean(axis=2)
    if a.ndim != 2 or min(a.shape) < 5:
        raise ValueError("image must be at least 5x5 grayscale/colour")
    h, w = a.shape
    # block-average downsample (the denoise); keep a >=8 px working image
    k = max(1, min(round(min(h, w) / 180.0), min(h, w) // 8))
    if k > 1:
        h2, w2 = h // k, w // k
        a = a[:h2 * k, :w2 * k].reshape(h2, k, w2, k).mean(axis=(1, 3))
    lap = (-4.0 * a[1:-1, 1:-1]
           + a[:-2, 1:-1] + a[2:, 1:-1]
           + a[1:-1, :-2] + a[1:-1, 2:])
    W = _focus_weight(lap.shape[0], lap.shape[1], sigma_frac)
    wsum = W.sum()
    return float((W * lap * lap).sum() / wsum) if wsum else 0.0


# -- capture planning (pure) -----------------------------------------------

def frange(start, stop, step):
    """Inclusive numeric range for stepped (e.g. voltage) capture.

    Returns [start, start+step, ..., stop] (last point included when it lands
    within a small tolerance). Works for ascending or descending ranges as long
    as ``step`` points from start toward stop. Raises on a zero or wrong-sign
    step.
    """
    start, stop, step = float(start), float(stop), float(step)
    if step == 0:
        raise ValueError("step must be non-zero")
    span = stop - start
    if span == 0:
        return [start]
    if (span > 0) != (step > 0):
        raise ValueError("step sign must move start toward stop")
    n = int(math.floor(span / step + 1e-9))
    return [start + i * step for i in range(n + 1)]


def parse_level_list(text):
    """'0.2, 0.4 0.6' -> [0.2, 0.4, 0.6]; empty/None -> None; junk raises.

    Commas and/or whitespace separate values (so a spreadsheet row pastes
    straight in). None means "no explicit list" -- callers fall back to
    start/stop/step.
    """
    if not text or not text.strip():
        return None
    parts = [p for chunk in text.split(',') for p in chunk.split()]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(
            f"Levels must be numbers separated by commas/spaces, got: {text!r}")
    return values or None


def capture_filename(prefix, index, value=None, ext='png', ts=None,
                     unit='V'):
    """Build a capture filename: '<prefix>_<NNNN>[_<ts>][_<value><unit>].<ext>'.

    value is rendered filename-safe (sign as 'm', decimal point as 'p') with
    `unit` appended: (-2.5, 'V') -> 'm2p5V', (30, 's') -> '30s'. ts is an
    optional datetime.
    """
    safe_prefix = ''.join(c if (c.isalnum() or c in '-_') else '_'
                          for c in str(prefix)) or 'cap'
    parts = [safe_prefix, f"{int(index):04d}"]
    if ts is not None:
        parts.append(ts.strftime('%Y%m%d-%H%M%S'))
    if value is not None:
        v = f"{float(value):g}{unit}".replace('-', 'm').replace('.', 'p')
        parts.append(v)
    return "_".join(parts) + "." + str(ext).lstrip('.')


def timed_delays(explicit=None, start=None, interval=None, count=None):
    """Resolve a capture schedule to a sorted, unique list of delays (s).

    An explicit list (parsed by parse_level_list) wins; otherwise build a
    regular schedule start, start+interval, ... (count points). Negative
    delays are rejected -- a delay is time after the t=0 trigger.
    """
    values = parse_level_list(explicit) if explicit else None
    if values is None:
        start = float(start if start not in (None, '') else 0)
        interval = float(interval if interval not in (None, '') else 0)
        count = int(float(count if count not in (None, '') else 0))
        if count < 1:
            raise ValueError("count must be >= 1")
        if count > 1 and interval <= 0:
            raise ValueError("interval must be > 0 for more than one shot")
        values = [start + i * interval for i in range(count)]
    if any(v < 0 for v in values):
        raise ValueError("delays must be >= 0 (time after the trigger)")
    return sorted(set(values))


# -- V4L2 / Bayer support --------------------------------------------------
# Industrial UVC cameras expose raw Bayer only. The Imaging Source
# DFK 37BUX250 offers exactly RGGB8 and RG16 -- no YUYV, no MJPEG -- and
# OpenCV's V4L2 backend has no SRGGB8 support, so it refuses to open the
# device at all ("can't open camera by index", bench-verified 2026-07-20)
# even though v4l2-ctl streams from it happily. Those cameras are therefore
# driven through v4l2-ctl and debayered here.

# V4L2 fourcc -> OpenCV conversion attribute. NOTE the deliberate shift:
# OpenCV names a Bayer pattern after the 2x2 block starting at the SECOND
# row/column, so a V4L2 RGGB sensor is OpenCV's BayerBG. Getting this wrong
# swaps red and blue (it still looks like a picture, which is why it is easy
# to miss).
BAYER_CV_CODE = {
    'RGGB': 'COLOR_BayerBG2BGR',
    'BGGR': 'COLOR_BayerRG2BGR',
    'GRBG': 'COLOR_BayerGB2BGR',
    'GBRG': 'COLOR_BayerGR2BGR',
}


def v4l2_available():
    """True when the v4l2-ctl helper binary is installed."""
    return shutil.which('v4l2-ctl') is not None


def _v4l2(*args, device=None, timeout=10):
    """Run v4l2-ctl and return stdout ('' on any failure)."""
    cmd = ['v4l2-ctl']
    if device:
        cmd += ['-d', device]
    cmd += list(args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout if out.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def parse_formats(text):
    """'--list-formats' output -> list of fourcc strings, in order."""
    return re.findall(r"^\s*\[\d+\]:\s*'(\w{4})'", text, re.MULTILINE)


def parse_frame_sizes(text, fourcc):
    """'--list-formats-ext' output -> [(w, h), ...] for one fourcc."""
    sizes, active = [], False
    for line in text.splitlines():
        fmt = re.match(r"\s*\[\d+\]:\s*'(\w{4})'", line)
        if fmt:
            active = fmt.group(1) == fourcc
            continue
        if active:
            m = re.match(r"\s*Size:\s*Discrete\s+(\d+)x(\d+)", line)
            if m:
                sizes.append((int(m.group(1)), int(m.group(2))))
    return sizes


def device_formats(device):
    """Fourccs a /dev/videoN node offers (empty if it is not a capture node)."""
    return parse_formats(_v4l2('--list-formats', device=device))


def choose_bayer(formats):
    """Bayer fourcc to use when a device offers ONLY Bayer formats.

    A camera that also offers YUYV/MJPEG is left to OpenCV, which handles
    those natively and faster; None means "not our problem".
    """
    if not formats:
        return None
    bayer = [f for f in formats if f in BAYER_CV_CODE]
    ordinary = [f for f in formats if f not in BAYER_CV_CODE
                and not f.startswith('RG1')]      # RG16 = 16-bit Bayer
    return bayer[0] if bayer and not ordinary else None


def choose_size(sizes, max_width=1920):
    """Largest offered frame size at or below `max_width`.

    Full sensor resolution (2448x2048 on the DFK 37BUX250) costs USB
    bandwidth and debayer time for no benefit in a preview.
    """
    if not sizes:
        return None
    usable = [s for s in sizes if s[0] <= max_width] or sizes
    return max(usable, key=lambda s: s[0] * s[1])


def bayer_format(device):
    """The device's Bayer fourcc when it offers ONLY Bayer, else None."""
    return choose_bayer(device_formats(device))


# ---- full camera-control lock (user knobs) --------------------------------
# The DFK re-converges/persists whatever its auto algorithms last chose, and
# nothing used to re-assert the user's values when a stream (re)opened -- so
# hand-set exposure/WB kept "drifting back". The GUI's Apply & Lock stores
# every control here, and EVERY capture path (preview stream open + each
# one-shot grab) stamps them onto the device first. Locked once = locked
# everywhere. Persisted per-bench in CAMERA_SETTINGS_PATH.

LOCKED_CONTROLS = {}
# autos must be written before their dependent values (manual exposure gates
# exposure_time_absolute; WB-auto gates red/blue_balance)
_CTRL_ORDER = ('auto_exposure', 'white_balance_automatic')
CAMERA_SETTINGS_PATH = os.path.join(
    os.path.expanduser('~'), '.local', 'share', 'scpi_control',
    'camera_controls.json')
# Fallback: the launcher's cache dir is created BY the user at every launch,
# so it is always writable -- unlike ~/.local/share/scpi_control, which the
# root-run desktop installer created root-owned in one user's home (bench
# 2026-07-24: Apply & Lock -> Errno 13 for robotincubator).
CAMERA_SETTINGS_FALLBACK = os.path.join(
    os.path.expanduser('~'), '.cache', 'scpi_control',
    'camera_controls.json')


def set_locked(controls):
    """Replace the locked-control set ({} clears the lock)."""
    LOCKED_CONTROLS.clear()
    LOCKED_CONTROLS.update({k: int(v) for k, v in (controls or {}).items()})
    return dict(LOCKED_CONTROLS)


def apply_locked(device, exclude=None):
    """Stamp the locked controls onto the device (autos first). Silent
    no-op when nothing is locked; returns how many controls were set.

    `exclude` is a set of control names to skip -- used by the live preview
    re-stamp to leave 'gain' alone: the DFK has a firmware auto-gain that
    overrides any written value within ~0.5 s, so re-writing it every second
    only produces visible flicker (bench 2026-07-24). Gain is instead held
    stable by pinning it at its floor at Apply time, where the AGC clamps."""
    if not LOCKED_CONTROLS:
        return 0
    exclude = exclude or set()
    n = 0
    for name in list(_CTRL_ORDER) + [k for k in LOCKED_CONTROLS
                                     if k not in _CTRL_ORDER]:
        if name in LOCKED_CONTROLS and name not in exclude:
            try:
                set_control(device, name, LOCKED_CONTROLS[name])
                n += 1
            except Exception:
                pass
    return n


def _write_settings(controls, path):
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({k: int(v) for k, v in controls.items()}, f, indent=2,
                  sort_keys=True)
    os.replace(tmp, path)
    return path


def save_camera_settings(controls, path=None):
    """Persist the locked controls (JSON) so they survive restarts.

    With no explicit path, tries CAMERA_SETTINGS_PATH then falls back to
    CAMERA_SETTINGS_FALLBACK when the primary is unwritable (e.g. a
    root-owned dir left by the installer). Returns the path actually
    written; raises only if every candidate fails."""
    if path is not None:
        return _write_settings(controls, path)
    last = None
    for cand in (CAMERA_SETTINGS_PATH, CAMERA_SETTINGS_FALLBACK):
        try:
            return _write_settings(controls, cand)
        except OSError as e:
            last = e
    raise last


def load_camera_settings(path=None):
    """Previously saved controls, or {}. Checks the fallback location too."""
    import json
    paths = [path] if path is not None else [CAMERA_SETTINGS_PATH,
                                             CAMERA_SETTINGS_FALLBACK]
    for p in paths:
        try:
            with open(p) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError):
            continue
    return {}


_CTRL_LINE = re.compile(
    r'^\s*([a-z0-9_]+)\s+0x[0-9a-f]+\s+\((int|bool|menu)\)\s*:\s*(.*)$')
_MENU_LINE = re.compile(r'^\s*(\d+):\s*(.+?)\s*$')


def parse_controls(text):
    """Parse `v4l2-ctl --list-ctrls-menus` output into a list of dicts:
    {'name','type','min','max','default','value','menu':{int:label}} --
    ranges absent for bool/menu keep min/max None."""
    out = []
    for line in (text or '').splitlines():
        m = _CTRL_LINE.match(line)
        if m:
            name, typ, rest = m.groups()
            d = {'name': name, 'type': typ, 'min': None, 'max': None,
                 'default': None, 'value': None, 'menu': {}}
            for key in ('min', 'max', 'default', 'value'):
                mm = re.search(rf'{key}=(-?\d+)', rest)
                if mm:
                    d[key] = int(mm.group(1))
            out.append(d)
        elif out:
            mm = _MENU_LINE.match(line)
            if mm and out[-1]['type'] == 'menu':
                out[-1]['menu'][int(mm.group(1))] = mm.group(2)
    return out


def list_controls(device):
    """All V4L2 controls of `device` (parsed), or [] when unavailable."""
    if not v4l2_available() or not os.path.exists(device):
        return []
    try:
        return parse_controls(_v4l2('--list-ctrls-menus', device=device))
    except Exception:
        return []


def get_control(device, name):
    """Integer value of a V4L2 control, or None."""
    out = _v4l2(f'--get-ctrl={name}', device=device)
    m = re.search(r':\s*(-?\d+)', out)
    return int(m.group(1)) if m else None


def set_control(device, name, value):
    """Set a V4L2 control; returns True when it took."""
    _v4l2(f'--set-ctrl={name}={int(value)}', device=device)
    return get_control(device, name) == int(value)


def set_manual_exposure(device, exposure=None, gain=None):
    """Switch the camera to manual exposure and optionally set the values.

    Industrial cameras often ship with auto-exposure that never converges
    over UVC -- this one produced a pure black frame at its defaults while
    happily exposing under manual control. Auto white-balance is also turned
    off here: left on, it continuously re-balances red/blue gain and shifts
    the DUT's apparent brightness, so a hand-set exposure never "sticks".
    """
    set_control(device, 'auto_exposure', 1)          # 1 = manual (UVC)
    set_control(device, 'white_balance_automatic', 0)  # stop auto re-balancing
    if exposure is not None:
        set_control(device, 'exposure_time_absolute', exposure)
    if gain is not None:
        set_control(device, 'gain', gain)


def grab_raw(device, fourcc, width, height, count=3):
    """Grab `count` frames and return the LAST one's raw bytes (or None).

    Short-lived capture used for exposure hunting; the first frames after a
    control change still carry the old settings, hence count > 1.
    """
    cmd = ['v4l2-ctl', '-d', device,
           f'--set-fmt-video=width={width},height={height},'
           f'pixelformat={fourcc}',
           '--stream-mmap', f'--stream-count={count}', '--stream-to=-']
    try:
        out = subprocess.run(cmd, capture_output=True,
                             timeout=30 + count * 5)
    except (OSError, subprocess.SubprocessError):
        return None
    n = width * height
    return out.stdout[-n:] if len(out.stdout) >= n else None


def auto_exposure(device, fourcc, width, height, target=128,
                  candidates=(20, 50, 100, 150, 200, 400, 800, 1600, 3200)):
    """Hunt for an exposure whose average level lands near `target`.

    Returns (exposure, mean) for the best candidate, or (None, None).
    This exists because the camera's own auto-exposure does not converge
    over UVC on this unit -- it sits at a pure black frame -- so "the
    camera is broken" is usually just a bad exposure.
    """
    import numpy as np
    set_manual_exposure(device)
    best = (None, None, None)
    for exp in candidates:
        if not set_control(device, 'exposure_time_absolute', exp):
            continue
        data = grab_raw(device, fourcc, width, height)
        if not data:
            continue
        mean = float(np.frombuffer(data, dtype=np.uint8).mean())
        err = abs(mean - target)
        if best[2] is None or err < best[2]:
            best = (exp, mean, err)
    if best[0] is not None:
        set_control(device, 'exposure_time_absolute', best[0])
    return best[0], best[1]


# -- one-shot capture (timed / stepped) ------------------------------------
# A continuously running stream consumed LAZILY (the timed/stepped workers
# sleep seconds between shots) goes stale -- v4l2-ctl blocks on the full
# pipe, so a read returns an old frame, and reads are gated to the stream's
# frame period. A fresh, frame-aligned one-shot grab is ~90 ms on the bench
# camera (measured 2026-07-20) and always current, which is what phase-
# accurate capture needs. The device must be free (no preview stream open).

def resolve_camera(index):
    """Describe how to one-shot grab camera `index`, probed once.

    -> {'kind': 'bayer', 'device', 'fourcc', 'w', 'h'} for a Bayer-only
    device, else {'kind': 'cv2', 'index'} for an ordinary webcam.
    """
    device = f'/dev/video{index}'
    if v4l2_available() and os.path.exists(device):
        fourcc = bayer_format(device)
        if fourcc:
            w, h = choose_size(parse_frame_sizes(
                _v4l2('--list-formats-ext', device=device), fourcc)) \
                or (640, 480)
            return {'kind': 'bayer', 'device': device, 'fourcc': fourcc,
                    'w': w, 'h': h}
        # An ordinary webcam still has V4L2 controls even though it is
        # captured through cv2. Carrying the device path is what lets the
        # locked exposure be re-stamped before each grab -- without it the
        # cv2 path took whatever the firmware chose on every open, and the
        # SLDEA tab's exposure/gain fields were silently ignored
        # (2026-08-05: the carbon-black run's baseline came out saturated).
        return {'kind': 'cv2', 'index': int(index), 'device': device}
    return {'kind': 'cv2', 'index': int(index)}


def oneshot_rgb(spec, count=2):
    """One fresh RGB frame from a resolve_camera() spec, or None.

    Opens and closes the capture each call, so nothing has to stay warm and
    no frame can be stale. `count` frames are streamed and the last kept
    (the first settles after any control change).
    """
    import cv2
    import numpy as np
    if spec.get('kind') == 'bayer':
        apply_locked(spec['device'])   # user-locked knobs win on every grab
        data = grab_raw(spec['device'], spec['fourcc'], spec['w'], spec['h'],
                        count=count)
        if not data:
            return None
        raw = np.frombuffer(data, dtype=np.uint8).reshape(spec['h'], spec['w'])
        code = getattr(cv2, BAYER_CV_CODE.get(spec['fourcc'],
                                              'COLOR_BayerBG2BGR'))
        return cv2.cvtColor(cv2.cvtColor(raw, code), cv2.COLOR_BGR2RGB)
    # Same contract as the bayer path: the locked knobs win on EVERY grab.
    # This call opens a fresh VideoCapture, so anything the firmware
    # re-decided since the last grab is undone here rather than half a
    # second earlier -- the DFK's auto-gain overrides a written value
    # within ~0.5 s (see apply_locked), so a lock applied in advance is a
    # lock the firmware has time to walk back before the shutter.
    if spec.get('device'):
        apply_locked(spec['device'])
    cap = cv2.VideoCapture(spec['index'])
    try:
        if not cap.isOpened():
            return None
        for _ in range(max(1, count)):
            cap.grab()          # flush any buffered frames -> fresh
        ok, frame = cap.read()
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if ok else None
    finally:
        cap.release()


class V4L2BayerCamera:
    """Streams raw Bayer frames from v4l2-ctl and debayers them to BGR.

    Same read()/read_rgb()/close() surface as Camera, so the GUI does not
    care which backend it got.
    """

    def __init__(self, device, fourcc, width=None, height=None, fps=15):
        self.device = device
        self.fourcc = fourcc
        self.fps = fps
        if width and height:
            self.width, self.height = width, height
        else:
            sizes = parse_frame_sizes(
                _v4l2('--list-formats-ext', device=device), fourcc)
            self.width, self.height = choose_size(sizes) or (640, 480)
        self._proc = None

    @property
    def frame_bytes(self):
        return self.width * self.height          # 8-bit Bayer: 1 byte/pixel

    @property
    def is_open(self):
        return self._proc is not None and self._proc.poll() is None

    def open(self):
        # Set the frame rate in a SEPARATE call: --set-parm prints
        # "Frame rate set to N fps" (29 bytes, an ODD count) on stdout, and
        # with --stream-to=- that lands in front of the pixel data. The
        # frames then sit at an odd byte offset, which shifts the Bayer
        # phase by one column -- the picture survives but comes out as a
        # magenta checkerboard. --silent does not suppress it.
        # (bench-diagnosed 2026-07-20)
        apply_locked(self.device)      # user-locked knobs win on every open
        _v4l2(f'--set-parm={self.fps}', device=self.device)
        cmd = ['v4l2-ctl', '-d', self.device,
               f'--set-fmt-video=width={self.width},height={self.height},'
               f'pixelformat={self.fourcc}',
               '--stream-mmap', '--stream-count=0', '--stream-to=-']
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,
                                      bufsize=0)
        return self

    def _read_exactly(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def read(self):
        """Latest BGR frame, or None. Drains the pipe so preview stays live."""
        if not self.is_open:
            return None
        import cv2
        import numpy as np
        data = self._read_exactly(self.frame_bytes)
        if data is None:
            return None
        # Skip any frames already queued behind this one, else the preview
        # falls further and further behind the camera.
        while select.select([self._proc.stdout], [], [], 0)[0]:
            nxt = self._read_exactly(self.frame_bytes)
            if nxt is None:
                break
            data = nxt
        raw = np.frombuffer(data, dtype=np.uint8).reshape(self.height,
                                                          self.width)
        code = getattr(cv2, BAYER_CV_CODE.get(self.fourcc, 'COLOR_BayerBG2BGR'))
        return cv2.cvtColor(raw, code)

    def read_rgb(self):
        import cv2
        frame = self.read()
        return None if frame is None else cv2.cvtColor(frame,
                                                       cv2.COLOR_BGR2RGB)

    def save(self, path, frame=None):
        import cv2
        if frame is None:
            frame = self.read()
        return False if frame is None else bool(cv2.imwrite(path, frame))

    def close(self):
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


# -- camera wrapper --------------------------------------------------------

class Camera:
    """Thin cv2.VideoCapture wrapper. Lazy-imports cv2 on open().

    Not thread-safe for concurrent reads -- the GUI pauses live preview while a
    capture sequence owns the camera.
    """
    def __init__(self, index=0, width=None, height=None):
        self.index = index
        self.width = width
        self.height = height
        self._cap = None
        self._bayer = None          # V4L2BayerCamera when cv2 cannot cope

    @property
    def is_open(self):
        if self._bayer is not None:
            return self._bayer.is_open
        return self._cap is not None and self._cap.isOpened()

    def open(self):
        import cv2
        self._cap = cv2.VideoCapture(self.index)
        if not self._cap.isOpened():
            self._cap = None
            # OpenCV cannot open Bayer-only industrial cameras at all; use
            # the v4l2-ctl backend for those before giving up.
            device = f'/dev/video{self.index}'
            if v4l2_available() and os.path.exists(device):
                fourcc = bayer_format(device)
                if fourcc:
                    self._bayer = V4L2BayerCamera(
                        device, fourcc, self.width, self.height).open()
                    return self
            raise RuntimeError(f"could not open camera index {self.index}")
        if self.width:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return self

    def read(self):
        """Return a BGR frame (numpy array) or None on failure."""
        if self._bayer is not None:
            return self._bayer.read()
        if not self.is_open:
            return None
        ok, frame = self._cap.read()
        return frame if ok else None

    def read_rgb(self):
        """Return an RGB frame (numpy array) or None -- ready for PIL."""
        import cv2
        frame = self.read()
        if frame is None:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def save(self, path, frame=None):
        """Write a frame (or a freshly-read one) to ``path``. Returns bool."""
        import cv2
        if frame is None:
            frame = self.read()
        if frame is None:
            return False
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        return bool(cv2.imwrite(path, frame))

    def close(self):
        if self._bayer is not None:
            self._bayer.close()
            self._bayer = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
