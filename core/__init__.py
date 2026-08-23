"""SLDEA Lifecycle Manager core.

Importing this package injects ``lib/`` (the vendored Digital Multitool
modules, see lib/VENDOR.md) onto sys.path exactly once, so the vendored
flat imports (``import sldea_profile``) resolve in-process without
turning lib/ into a package -- keeping the vendored files byte-identical
to upstream is what makes re-vendoring a diff instead of a merge.
"""
import os as _os
import sys as _sys

_LIB = _os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), 'lib')
if _LIB not in _sys.path:
    _sys.path.insert(0, _LIB)
