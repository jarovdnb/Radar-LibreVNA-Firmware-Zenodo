#   Coupled pan/tilt keep-out model: the instrument's antennas can strike the
#   horizontal mounting bar at some pan/tilt combinations but not others
#   (narrow near tilt=+90, at the bar; wide open near tilt=-90, pointing at
#   the ground). Unlike controller.py's independent per-axis pan_min_abs/
#   pan_max_abs/tilt_min_abs/tilt_max_abs box, the safe pan range here
#   depends on tilt, so it can't be expressed as a simple box.
#
#   The envelope is defined by user-measured breakpoints
#   (tilt_deg, pan_min_deg, pan_max_deg), piecewise-linearly interpolated
#   between them (flat beyond the outermost ones) -- the SAME model
#   implemented in keepout_visualizer.html, which is meant to preview
#   exactly what this module enforces.
#
#   IMPORTANT: breakpoints are measured against RAW hardware angles (what
#   lib/qpt90.py's get_status() returns directly, via debug_notebook.ipynb
#   Cells 18-20), not the logical pan_abs/tilt_abs controller.py shows in
#   the dashboard. The two only differ by the pan_invert/tilt_invert sign
#   flip (see controller.flip()) -- callers must convert before calling into
#   this module. The physical bar/antenna geometry doesn't care about a
#   software inversion setting; only the raw encoder frame is physically
#   meaningful.

import json
import os


def load_breakpoints(samples_path, bucket_size=5.0):
    """
    Load raw (tilt_deg, pan_deg) boundary samples recorded by the debug
    notebook and bucket them into (tilt, pan_min, pan_max) breakpoints,
    sorted by tilt. Returns [] if the file doesn't exist, is unreadable, or
    has fewer than 2 distinct tilt buckets -- callers should treat an empty
    list as "no keep-out restriction configured" (nothing measured yet).
    """
    if not os.path.exists(samples_path):
        return []
    try:
        with open(samples_path, "r") as f:
            samples = json.load(f)
    except (OSError, ValueError):
        return []

    buckets = {}
    for s in samples:
        try:
            tilt = float(s["tilt_deg"])
            pan = float(s["pan_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        key = round(tilt / bucket_size) * bucket_size
        buckets.setdefault(key, []).append(pan)

    breakpoints = [
        {"tilt": tilt, "pan_min": min(pans), "pan_max": max(pans)}
        for tilt, pans in buckets.items()
    ]
    breakpoints.sort(key=lambda b: b["tilt"])
    return breakpoints if len(breakpoints) >= 2 else []


def safe_range_at_tilt(tilt, breakpoints):
    """
    (pan_min, pan_max) at this tilt, linearly interpolated between the two
    nearest breakpoints (flat beyond the outermost ones). Returns
    (-180.0, 180.0) -- i.e. unrestricted -- if no breakpoints are configured.
    """
    if not breakpoints:
        return (-180.0, 180.0)
    if len(breakpoints) == 1:
        return (breakpoints[0]["pan_min"], breakpoints[0]["pan_max"])
    if tilt <= breakpoints[0]["tilt"]:
        b = breakpoints[0]
        return (b["pan_min"], b["pan_max"])
    last = breakpoints[-1]
    if tilt >= last["tilt"]:
        return (last["pan_min"], last["pan_max"])
    for a, b in zip(breakpoints, breakpoints[1:]):
        if a["tilt"] <= tilt <= b["tilt"]:
            span = b["tilt"] - a["tilt"]
            f = 0.0 if span == 0 else (tilt - a["tilt"]) / span
            return (
                a["pan_min"] + f * (b["pan_min"] - a["pan_min"]),
                a["pan_max"] + f * (b["pan_max"] - a["pan_max"]),
            )
    return (-180.0, 180.0)   # unreachable given the bounds checks above


def _normalize_pan(pan):
    return ((pan + 180.0) % 360.0 + 360.0) % 360.0 - 180.0


def is_safe(pan, tilt, breakpoints):
    """True if (pan, tilt) -- raw hardware angles -- is outside the keep-out zone."""
    lo, hi = safe_range_at_tilt(tilt, breakpoints)
    return lo <= _normalize_pan(pan) <= hi


def safe_pan_intersection_over_sweep(tilt_a, tilt_b, breakpoints, steps=36):
    """
    The pan range that stays safe at EVERY tilt between tilt_a and tilt_b
    (inclusive), sampled at `steps` intervals. Used to find a single pan
    value a tilt-only move can hold through the whole sweep without ever
    entering the keep-out zone. Returns None if no such value exists (the
    move can't be done as a straight single-axis tilt sweep at any fixed
    pan -- the caller should refuse the move rather than guess a path).
    """
    lo, hi = -180.0, 180.0
    n = max(1, steps)
    for i in range(n + 1):
        t = tilt_a + (tilt_b - tilt_a) * i / n
        r_lo, r_hi = safe_range_at_tilt(t, breakpoints)
        lo = max(lo, r_lo)
        hi = min(hi, r_hi)
        if lo > hi:
            return None
    return (lo, hi)
