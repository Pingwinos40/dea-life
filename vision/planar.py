"""Planar top-down adapter: vendored sldea_edge as a VisionAdapter.

The heavy lifting -- disc fitting, difference imaging, candidate
ranking, refusal semantics -- is the vendored engine's, untouched. This
adapter only wires the interlude seam: the 0 kV rest frame anchors the
resting disc (px->mm scale via the nominal diameter, the machine's-job
rule), the peak frame yields the best active-area candidate, and both
convert to mm^2.

Refusals stay refusals: when baseline_disc or candidates() decline
(unreadable baseline, gated no-change frame), value is None with the
reason in notes -- never a fabricated number (audit 2026-08-05 rule).
The full offline pass over interlude staircase sub-runs is a separate
concern: those are legacy-format run dirs, and the vendored Edge
Review / sldea_diag tools work on them directly.
"""
import math

import numpy as np

import sldea_edge as se  # vendored


class PlanarVision:
    metric_name = 'active_area_mm2'

    def __init__(self, cfg=None):
        self.settings = dict(se.DEFAULT_SETTINGS)
        self.settings.update(cfg or {})
        self.accept_conf = float(self.settings.get('accept_conf', 0.75))

    @staticmethod
    def _gray(frame):
        if frame is None:
            return None
        a = np.asarray(frame)
        if a.ndim == 3:
            a = a.mean(axis=2)
        return a.astype(np.float32)

    def measure_pair(self, rest_frame, peak_frame, context):
        out = {'value': None, 'rest_value': None, 'quality': 'bad',
               'wrinkle_idx': None, 'notes': ''}
        base = self._gray(rest_frame)
        img = self._gray(peak_frame)
        if base is None or img is None:
            out['notes'] = 'missing frame(s)'
            return out
        disc = se.baseline_disc(base, self.settings)
        if disc is None:
            out['notes'] = ('baseline disc refused: '
                            + (se.baseline_disc_refusal(base,
                                                        self.settings)
                               or 'unknown'))
            return out
        diam_px = 2.0 * math.sqrt(float(disc['area_px']) / math.pi)
        diam_mm = float(self.settings.get('diam_mm', 16.0))
        scale = diam_mm / diam_px                     # mm per px
        out['rest_value'] = round(float(disc['area_px']) * scale * scale,
                                  4)
        cands = se.candidates(base, img, self.settings)
        if not cands:
            out['notes'] = ('no active-area candidate (gated no-change '
                            'or unreadable) -- resting area stands')
            out['value'] = out['rest_value']
            out['quality'] = 'gated'
            return out
        best = cands[0]
        out['value'] = round(float(best['area_px']) * scale * scale, 4)
        out['wrinkle_idx'] = best.get('wrinkle_idx')
        conf = float(best.get('conf', 0.0))
        out['quality'] = 'ok' if conf >= self.accept_conf else 'review'
        out['notes'] = f"edge:{best.get('method', '?')} conf {conf:.2f}"
        return out
