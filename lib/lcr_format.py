#!/usr/bin/env python3
"""Pure formatting helpers for the BK 894 LCR readout (no Tk, no VISA).

The GUI readout used to hard-code nF/uH regardless of magnitude, print the
secondary value bare, and label readings by whatever the mode DROPDOWN said
rather than the mode actually applied (issue #38). This module knows, per
measurement mode, what the primary/secondary quantities are and formats both
with auto-scaled SI prefixes.

Headless self-test: .venv/bin/python tests/test_lcr_format.py
"""

# mode -> (primary unit, secondary name, secondary unit)
# '' = dimensionless; angle units are printed as-is (no SI prefix).
MODE_UNITS = {
    'CPD':  ('F', 'D',   ''),
    'CPQ':  ('F', 'Q',   ''),
    'CPG':  ('F', 'G',   'S'),
    'CPRP': ('F', 'Rp',  'Ω'),
    'CSD':  ('F', 'D',   ''),
    'CSQ':  ('F', 'Q',   ''),
    'CSRS': ('F', 'Rs',  'Ω'),
    'LSRS': ('H', 'Rs',  'Ω'),
    'LSRD': ('H', 'Rdc', 'Ω'),
    'LPRS': ('H', 'Rs',  'Ω'),
    'LPRP': ('H', 'Rp',  'Ω'),
    'RX':   ('Ω', 'X', 'Ω'),
    'ZTD':  ('Ω', 'θ', '°'),
    'ZTR':  ('Ω', 'θ', 'rad'),
}

_PREFIXES = [(1e-12, 'p'), (1e-9, 'n'), (1e-6, 'µ'), (1e-3, 'm'),
             (1.0, ''), (1e3, 'k'), (1e6, 'M'), (1e9, 'G')]

# dimensionless and angle units get a plain number, never an SI prefix
_NO_PREFIX = {'', '°', 'rad'}


def format_si(value, unit, digits=4):
    """value -> '3.3 nF': SI prefix chosen so the mantissa lands in [1, 1000).

    None -> '--' (a failed/absent reading must never crash the readout).
    Values below the smallest prefix keep it (e.g. '0.05 pF').
    """
    if value is None:
        return '--'
    value = float(value)
    if unit in _NO_PREFIX:
        return f'{value:.{digits}g} {unit}'.rstrip()
    if value == 0:
        return f'0 {unit}'
    mag = abs(value)
    scale, prefix = _PREFIXES[0]
    for s, p in _PREFIXES:
        if mag >= s:
            scale, prefix = s, p
    return f'{value / scale:.{digits}g} {prefix}{unit}'


def format_measurement(mode, primary, secondary):
    """(mode, primary, secondary) -> (primary_str, secondary_str).

    e.g. ('CPD', 3.3e-9, 0.0012) -> ('3.3 nF', 'D: 0.0012').
    Unknown modes fall back to bare numbers rather than raising.
    """
    p_unit, s_name, s_unit = MODE_UNITS.get((mode or '').upper(),
                                            ('', '', ''))
    p_str = format_si(primary, p_unit)
    s_str = format_si(secondary, s_unit)
    if s_name:
        s_str = f'{s_name}: {s_str}'
    return p_str, s_str
