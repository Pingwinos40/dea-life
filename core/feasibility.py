"""Drive-feasibility math: can this rig actually deliver the recipe?

Pure functions, no hardware. The Trek 610E-G supplies at most +/-2 mA
into the load; a cycling DEA is a capacitor, so the peak charging
current I_pk = 2*pi*f*C*V_pk consumes that budget. Silently clipping the
drive is a classic way to invalidate a week-long lifetime run -- the
commanded waveform simply does not reach the specimen and every
"survived N cycles" claim inherits the error. So requests are checked
BEFORE the run (here) and the delivered waveform is checked DURING it
(drive-fidelity rule in core/failure.py).

Numbers are the 610E datasheet's; the -G variant is assumed identical
until the bench manual check (plan risk #6). All overridable per call.
"""
import math

# Trek 610E-G limits (datasheet; confirm -G variant on the bench).
TREK_I_LIMIT_UA = 2000.0      # +/-2 mA output current
TREK_SLEW_V_PER_US = 35.0     # >35 V/us at the HV output
TREK_LS_BW_HZ = 1200.0        # large-signal bandwidth, DC..1.2 kHz
# Budget fractions: warn when the peak current uses most of the limit,
# refuse when there is effectively no headroom for leakage + tolerance.
WARN_FRAC = 0.60
REFUSE_FRAC = 0.90


def peak_current_ua(freq_hz, c_nf, v_pk_kv, v_min_kv=0.0):
    """Peak capacitive charging current in uA for a sinusoidal cycle.

    For a unipolar sine between v_min and v_pk the AC amplitude is half
    the span; I_pk = 2*pi*f * C * V_amp. Units: nF * kV = uC, so
    nF * kV * (rad/s) = uA when f is in Hz.
    """
    v_amp_kv = 0.5 * abs(float(v_pk_kv) - float(v_min_kv))
    return 2.0 * math.pi * float(freq_hz) * float(c_nf) * v_amp_kv


def slew_v_per_us(freq_hz, v_pk_kv, v_min_kv=0.0):
    """Worst-case output slew for a sine cycle, in V/us."""
    v_amp_v = 0.5 * abs(float(v_pk_kv) - float(v_min_kv)) * 1000.0
    return 2.0 * math.pi * float(freq_hz) * v_amp_v / 1e6


def max_feasible_hz(c_nf, v_pk_kv, v_min_kv=0.0, leak_margin_ua=0.0,
                    i_limit_ua=TREK_I_LIMIT_UA, frac=REFUSE_FRAC):
    """Highest cycling frequency that stays under frac of the current
    limit after reserving leak_margin_ua for resistive leakage."""
    v_amp_kv = 0.5 * abs(float(v_pk_kv) - float(v_min_kv))
    if v_amp_kv <= 0 or c_nf <= 0:
        return float('inf')
    budget = max(0.0, frac * i_limit_ua - float(leak_margin_ua))
    return budget / (2.0 * math.pi * float(c_nf) * v_amp_kv)


def check_drive(freq_hz, c_nf, v_pk_kv, v_min_kv=0.0, leak_margin_ua=50.0,
                i_limit_ua=TREK_I_LIMIT_UA):
    """Feasibility verdict for one cycling condition.

    Returns a dict: verdict 'ok' | 'warn' | 'refuse', the numbers behind
    it, and human-readable messages. c_nf None or <= 0 means the specimen
    capacitance is unknown -- that is a WARN, not a pass: the check
    cannot clear a drive it cannot compute.
    """
    msgs = []
    if c_nf is None or float(c_nf) <= 0:
        return {
            'verdict': 'warn', 'i_pk_ua': None, 'i_frac': None,
            'slew_v_per_us': slew_v_per_us(freq_hz, v_pk_kv, v_min_kv),
            'max_feasible_hz': None,
            'msgs': ["specimen capacitance unknown -- peak-current check "
                     "SKIPPED; record C in the registry (measured or "
                     "geometry estimate) to arm it"],
        }
    c_nf = float(c_nf)
    ipk = peak_current_ua(freq_hz, c_nf, v_pk_kv, v_min_kv)
    total = ipk + float(leak_margin_ua)
    frac = total / float(i_limit_ua)
    slew = slew_v_per_us(freq_hz, v_pk_kv, v_min_kv)
    fmax = max_feasible_hz(c_nf, v_pk_kv, v_min_kv, leak_margin_ua,
                           i_limit_ua)
    verdict = 'ok'
    if frac >= REFUSE_FRAC:
        verdict = 'refuse'
        msgs.append(
            f"I_pk {ipk:.0f} uA + {leak_margin_ua:g} uA leakage margin = "
            f"{100 * frac:.0f}% of the +/-{i_limit_ua / 1000:g} mA Trek "
            f"limit -- REFUSED. Max feasible frequency at this C and "
            f"voltage: {fmax:.1f} Hz")
    elif frac >= WARN_FRAC:
        verdict = 'warn'
        msgs.append(
            f"I_pk {ipk:.0f} uA uses {100 * frac:.0f}% of the Trek "
            f"current limit -- little headroom for leakage growth; the "
            f"ipk_headroom rule will pause the run past 90%")
    if slew > TREK_SLEW_V_PER_US:
        verdict = 'refuse'
        msgs.append(f"required slew {slew:.1f} V/us exceeds the Trek's "
                    f"{TREK_SLEW_V_PER_US:g} V/us")
    if freq_hz > TREK_LS_BW_HZ:
        verdict = 'refuse'
        msgs.append(f"{freq_hz:g} Hz is beyond the Trek's large-signal "
                    f"bandwidth ({TREK_LS_BW_HZ:g} Hz)")
    return {'verdict': verdict, 'i_pk_ua': ipk, 'i_frac': frac,
            'slew_v_per_us': slew, 'max_feasible_hz': fmax, 'msgs': msgs}


def scope_window_horizontal_ok(freq_hz, window_s):
    """True when the scope's acquisition window spans >= 2 carrier
    periods -- below that, MEAN / PK2PK / FREQUENCY measurements of the
    cycling waveform are not meaningful. New check (the vendored
    monitor_problems covers only the vertical window)."""
    if freq_hz <= 0:
        return True
    return float(window_s) >= 2.0 / float(freq_hz)


def estimate_run(blocks_cycles, freq_hz, n_fast, fast_s, n_full, full_s):
    """Rough duration + disk estimate for the setup screen / setup.txt.

    blocks_cycles: total cycles across cycle blocks. Frames dominate
    disk: ~1.5 MB/frame at preview resolution (measured on the bench
    camera's PNGs).
    """
    cyc_s = blocks_cycles / float(freq_hz) if freq_hz > 0 else 0.0
    total_s = cyc_s + n_fast * fast_s + n_full * full_s
    frames = n_fast * 3 + n_full * 40          # rest+peak+leak, staircase
    return {'duration_s': total_s,
            'disk_mb': frames * 1.5,
            'frames_est': frames}
