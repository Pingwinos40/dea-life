"""status.json + status.html: the run's read-only LAN heartbeat.

Written atomically by the engine every ~2 s (json) and at every state
change / interlude (html). A stale `updated_iso` IS the crash detector:
the GUI flips to STALE past config heartbeat_stale_s, and a phone
check-in sees the timestamp on the page.

Never a control surface: stopping HV requires bench presence (stated in
the page footer). Nothing here may raise into the engine loop.
"""
import json
import os

from . import status_html

STATUS_JSON = 'status.json'
STATUS_HTML = 'status.html'


class StatusWriter:
    def __init__(self, run_dir, clock):
        self.run_dir = run_dir
        self.clock = clock
        self.state = {}
        self._spark = {'disp_ratio': [], 'leak_ua': [], 'cycles': []}
        self.failed = False

    def update(self, **fields):
        """Merge fields and write status.json (cheap, atomic)."""
        self.state.update(fields)
        self.state['updated_iso'] = self.clock.now_iso(timespec='seconds')
        try:
            path = os.path.join(self.run_dir, STATUS_JSON)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self.state, fh, indent=1, sort_keys=True,
                          default=str)
            os.replace(tmp, path)
        except Exception:
            self.failed = True       # never raises into the engine

    def interlude_point(self, cycles, disp_ratio, leak_ua):
        """Feed the html sparklines (one point per interlude)."""
        self._spark['cycles'].append(cycles)
        self._spark['disp_ratio'].append(disp_ratio)
        self._spark['leak_ua'].append(leak_ua)

    def render_html(self):
        """Write status.html from the current state + sparkline data."""
        try:
            html = status_html.render(self.state, self._spark)
            path = os.path.join(self.run_dir, STATUS_HTML)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(html)
            os.replace(tmp, path)
        except Exception:
            self.failed = True
