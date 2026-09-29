#!/usr/bin/env python3
"""DEA-LIFE -- GUI launcher.

    python dea_life_gui.py

The GUI is a launcher + monitor; the run itself is a detached
lifecycle_cli.py process that survives a GUI crash. See README.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.app import main  # noqa: E402  (gui/__init__ applies tk_fontfix)

if __name__ == '__main__':
    main()
