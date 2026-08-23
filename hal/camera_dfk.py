"""Live Camera: The Imaging Source DFK 37BUX250 via the vendored
webcam.py (raw-Bayer v4l2 path on the Linux bench, cv2 fallback
elsewhere).

The exposure-lock discipline is the vendored one: the DFK's firmware
walks written values back within ~0.5 s, so LOCKED_CONTROLS are
re-stamped before every grab, and the module-global lock is SAVED and
RESTORED around a run (a lifecycle run must not silently redefine the
Webcam tab's operator lock -- gui.py:3789 lineage).
"""
import contextlib

import numpy as np

import sldea_profile  # vendored
import webcam  # vendored

from . import Camera


class DFKCamera(Camera):
    def __init__(self, index=0, exposure=6, gain=60):
        self.index = int(index)
        self.exposure = exposure
        self.gain = gain
        self._spec = None

    def _resolve(self):
        if self._spec is None:
            try:
                self._spec = webcam.resolve_camera(self.index)
            except Exception:
                self._spec = None
        return self._spec

    def available(self):
        return self._resolve() is not None

    def grab(self):
        spec = self._resolve()
        if spec is None:
            return None
        for _attempt in range(2):            # one retry (upstream)
            try:
                frame = webcam.oneshot_rgb(spec, count=3)
            except Exception:
                frame = None
            if frame is not None:
                return frame
        return None

    def preflight(self):
        frame = self.grab()
        if frame is None:
            return False, 'camera: no frame (device missing?)', None
        gray = np.asarray(frame).mean(axis=2)
        mean = float(gray.mean())
        sat_pct = 100.0 * float((gray >= 250).mean())
        verdict = sldea_profile.exposure_verdict(mean, sat_pct)
        focus = None
        try:
            focus = webcam.focus_score(frame)
        except Exception:
            pass
        ok = 'clip' not in str(verdict).lower()
        txt = (f'exposure: {verdict} (mean {mean:.0f}, sat '
               f'{sat_pct:.1f}%)'
               + (f', focus score {focus:.0f}' if focus is not None
                  else ''))
        return ok, txt, frame

    @contextlib.contextmanager
    def locked(self, exposure=None, gain=None):
        """Stamp manual exposure controls; restore the previous lock on
        exit (never redefine the operator's own lock silently)."""
        spec = self._resolve()
        saved = None
        try:
            if spec is not None and spec.get('device'):
                dev = spec['device']
                exp = self.exposure if exposure is None else exposure
                gn = self.gain if gain is None else gain
                for ctrl, val in (('auto_exposure', 1),
                                  ('white_balance_automatic', 0),
                                  ('exposure_time_absolute', exp),
                                  ('gain', gn)):
                    try:
                        webcam.set_control(dev, ctrl, val)
                    except Exception:
                        pass
                saved = dict(webcam.LOCKED_CONTROLS)
                webcam.set_locked(dict(saved, auto_exposure=1,
                                       white_balance_automatic=0,
                                       exposure_time_absolute=exp,
                                       gain=gn))
            yield self
        finally:
            if saved is not None:
                try:
                    webcam.set_locked(saved)
                except Exception:
                    pass
