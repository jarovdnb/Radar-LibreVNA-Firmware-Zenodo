#   Plain-assert tests for lib/pantilt_program.py (no hardware needed).
#   Run with: python3 tests/test_pantilt_program.py

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.pantilt_program import (ProgramError, load_program, relative_limits,
                                 validate_points, validate_schedule, pair_min_gap,
                                 next_occurrence, summarize, preview,
                                 home_orientation_warnings, home_orientation_overrides,
                                 backfill_recorded_setup, dump_program)

PANTILT_CFG = {
    "pan_min_abs": -180.0, "pan_max_abs": 180.0,
    "tilt_min_abs": -90.0, "tilt_max_abs": 90.0,
    "home_pan_abs": 10.0, "home_tilt_abs": -5.0,
    "warn_margin_deg": 2.0, "min_gap_seconds": 60,
    "speed_deg_per_s_estimate": 4.0, "settle_seconds": 2,
}

SINGLE = """
schema_version: 1
type: single
defaults:
  try: 3
points:
  - pan_deg: 0
    tilt_deg: 0
  - pan_deg: 15
    tilt_deg: 0
"""

AUTOMATED = """
schema_version: 1
type: automated
initial_startdate_pantilt: '2025-04-07 12:00:00'
defaults:
  try: 3
points:
  - pan_deg: 0
    tilt_deg: 0
    repeated_minutes: 30
    offset_sec: 0
  - pan_deg: 15
    tilt_deg: 0
    repeated_minutes: 60
    offset_sec: 120
"""


def expect_error(text, fragment):
    try:
        load_program(text)
        assert False, f"expected ProgramError containing '{fragment}'"
    except ProgramError as e:
        assert fragment in str(e), f"'{fragment}' not in '{e}'"


def test_load_program():
    prog = load_program(SINGLE)
    assert prog["type"] == "single" and prog["defaults"]["try"] == 3 and len(prog["points"]) == 2

    expect_error("schema_version: 2\ntype: single\npoints: [{pan_deg: 0, tilt_deg: 0}]", "schema_version")
    expect_error("schema_version: 1\ntype: nope\npoints: [{pan_deg: 0, tilt_deg: 0}]", "type")
    expect_error("schema_version: 1\ntype: single\npoints: []", "points")
    expect_error("schema_version: 1\ntype: single\npoints: [{pan_deg: a, tilt_deg: 0}]", "pan_deg")
    START = "initial_startdate_pantilt: '2025-04-07 12:00:00'\n"
    expect_error("schema_version: 1\ntype: automated\n" + START + "points: [{pan_deg: 0, tilt_deg: 0}]", "repeated_minutes")
    expect_error("schema_version: 1\ntype: automated\n" + START +
                 "points: [{pan_deg: 0, tilt_deg: 0, repeated_minutes: 30, offset_sec: 1800}]", "offset_sec")
    expect_error("schema_version: 1\ntype: automated\npoints: [{pan_deg: 0, tilt_deg: 0, repeated_minutes: 30}]",
                 "initial_startdate_pantilt")
    expect_error("schema_version: 1\ntype: automated\ninitial_startdate_pantilt: 'not a date'\n"
                 "points: [{pan_deg: 0, tilt_deg: 0, repeated_minutes: 30}]", "initial_startdate_pantilt")
    expect_error("not: [valid", "YAML")

    #   Confirm it's parsed into a UTC epoch regardless of quoting
    prog = load_program(AUTOMATED)
    assert prog["initial_startdate_pantilt"] == "2025-04-07 12:00:00"
    from datetime import datetime, timezone
    assert prog["initial_startdate_epoch"] == int(datetime(2025, 4, 7, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def test_relative_limits():
    #   Home at abs (10, -5) shifts the reachable relative window
    limits = relative_limits(PANTILT_CFG)
    assert limits == {"pan_min": -190.0, "pan_max": 170.0, "tilt_min": -85.0, "tilt_max": 95.0}

    #   Asymmetric raw limits: inverting must negate AND swap min/max, since
    #   pan_min_abs/pan_max_abs describe the physical (raw) travel range,
    #   not the logical one. A no-op with pan_invert absent/0 (regression check).
    asym_cfg = dict(PANTILT_CFG, pan_min_abs=-90.0, pan_max_abs=170.0, home_pan_abs=0.0)
    assert relative_limits(asym_cfg)["pan_min"] == -90.0
    assert relative_limits(asym_cfg)["pan_max"] == 170.0

    inverted_cfg = dict(asym_cfg, pan_invert=1)
    inv_limits = relative_limits(inverted_cfg)
    assert inv_limits["pan_min"] == -170.0 and inv_limits["pan_max"] == 90.0

    #   Home position still shifts the (already-inverted) logical window
    inverted_home_cfg = dict(inverted_cfg, home_pan_abs=10.0)
    inv_home_limits = relative_limits(inverted_home_cfg)
    assert inv_home_limits["pan_min"] == -180.0 and inv_home_limits["pan_max"] == 80.0


def test_validate_points():
    prog = load_program("""
schema_version: 1
type: single
points:
  - {pan_deg: 0, tilt_deg: 0}       # ok
  - {pan_deg: 170, tilt_deg: 0}     # exactly on the pan limit -> warn
  - {pan_deg: 171, tilt_deg: 0}     # outside -> error
  - {pan_deg: 0, tilt_deg: 94}      # inside warn margin -> warn
""")
    reports = validate_points(prog, PANTILT_CFG)
    assert [r["status"] for r in reports] == ["ok", "warn", "error", "warn"]
    assert "outside relative limits" in reports[2]["message"]


def test_pair_min_gap():
    #   Same period, offsets 120 s apart -> 120 s
    assert pair_min_gap(1800, 0, 1800, 120) == 120
    #   30 min vs 60 min, offsets 0 and 120 -> they meet within 120 s every hour
    assert pair_min_gap(1800, 0, 3600, 120) == 120
    #   Brute-force cross-check over 48 h for random period/offset sets
    random.seed(42)
    for _ in range(50):
        p1 = random.randint(1, 48) * 60
        p2 = random.randint(1, 48) * 60
        o1 = random.randint(0, p1 - 1)
        o2 = random.randint(0, p2 - 1)
        times1 = [o1 + k * p1 for k in range(0, 48 * 3600 // p1)]
        times2 = [o2 + k * p2 for k in range(0, 48 * 3600 // p2)]
        brute = min(abs(t1 - t2) for t1 in times1 for t2 in times2)
        if p1 * p2 // math.gcd(p1, p2) + max(p1, p2) <= 48 * 3600:
            #   Window covers a full repeat cycle -> formula must match exactly
            assert pair_min_gap(p1, o1, p2, o2) == brute, (p1, o1, p2, o2)
        else:
            assert pair_min_gap(p1, o1, p2, o2) <= brute, (p1, o1, p2, o2)


def test_validate_schedule():
    #   The user's example program: offsets 120 s apart at the top of the hour
    prog = load_program(AUTOMATED)
    conflicts = validate_schedule(prog, dict(PANTILT_CFG, min_gap_seconds=60))
    assert conflicts == []

    conflicts = validate_schedule(prog, dict(PANTILT_CFG, min_gap_seconds=180))
    assert len(conflicts) == 1 and conflicts[0]["points"] == [0, 1] and conflicts[0]["gap_seconds"] == 120

    #   A point repeating faster than the minimum gap conflicts with itself
    fast = load_program("""
schema_version: 1
type: automated
initial_startdate_pantilt: '2025-04-07 12:00:00'
points:
  - {pan_deg: 0, tilt_deg: 0, repeated_minutes: 1, offset_sec: 0}
""")
    conflicts = validate_schedule(fast, dict(PANTILT_CFG, min_gap_seconds=90))
    assert len(conflicts) == 1 and conflicts[0]["points"] == [0]


def test_next_occurrence():
    point = {"repeated_minutes": 30, "offset_sec": 120}
    #   Grid: 120, 1920, 3720, ... anchored at the unix epoch (default ref_epoch=0)
    assert next_occurrence(point, 0) == 120
    assert next_occurrence(point, 119) == 120
    assert next_occurrence(point, 120) == 1920      # strictly after now
    assert next_occurrence(point, 1000000) == 1000920
    assert (next_occurrence(point, 1000000) - 120) % 1800 == 0

    #   With a program reference date, the grid shifts to be anchored there instead
    ref = 500000
    assert next_occurrence(point, ref, ref) == ref + 120
    assert next_occurrence(point, ref + 120, ref) == ref + 1920
    assert (next_occurrence(point, 1000000, ref) - (ref + 120)) % 1800 == 0


def test_summarize_and_preview():
    summary = summarize(load_program(SINGLE), PANTILT_CFG)
    assert summary["n_points"] == 2 and summary["estimated_minutes"] > 0

    summary = summarize(load_program(AUTOMATED), PANTILT_CFG)
    assert summary["measurements_per_hour"] == 3.0   # every 30 min + every 60 min

    result = preview(AUTOMATED, PANTILT_CFG)
    assert result["valid"] and len(result["timeline"]) > 0

    result = preview(SINGLE.replace("pan_deg: 15", "pan_deg: 179"), PANTILT_CFG)
    assert not result["valid"]


def test_recorded_setup():
    #   Optional per the docstring: a program with no home/orientation fields
    #   loads fine, warns about nothing, and its overrides are empty
    prog = load_program(SINGLE)
    assert "home_pan_abs" not in prog["defaults"] and "pan_orientation" not in prog["defaults"]
    assert home_orientation_warnings(prog, PANTILT_CFG) == []
    assert home_orientation_overrides(prog, PANTILT_CFG) == {}

    #   Bad values are rejected the same way any other field is
    expect_error("schema_version: 1\ntype: single\ndefaults: {home_pan_abs: not_a_number}\n"
                 "points: [{pan_deg: 0, tilt_deg: 0}]", "home_pan_abs")
    expect_error("schema_version: 1\ntype: single\ndefaults: {pan_orientation: 2}\n"
                 "points: [{pan_deg: 0, tilt_deg: 0}]", "pan_orientation")

    #   Declared and different from the current setup -> a warning per differing field
    declared = load_program("""
schema_version: 1
type: single
defaults:
  try: 1
  home_pan_abs: 20.0
  home_tilt_abs: -5.0
  pan_orientation: -1
  tilt_orientation: 1
points:
  - {pan_deg: 0, tilt_deg: 0}
""")
    cfg = dict(PANTILT_CFG, home_pan_abs=10.0, home_tilt_abs=-5.0, pan_invert=0, tilt_invert=0)
    warnings = home_orientation_warnings(declared, cfg)
    assert len(warnings) == 2  # home_pan_abs (10 -> 20) and pan_orientation (1 -> -1); tilt matches on both
    overrides = home_orientation_overrides(declared, cfg)
    assert overrides == {"home_pan_abs": 20.0, "home_tilt_abs": -5.0, "pan_invert": 1, "tilt_invert": 0}

    #   Matching setup -> no warnings, but overrides are still returned (idempotent re-apply)
    cfg_matching = dict(PANTILT_CFG, home_pan_abs=20.0, home_tilt_abs=-5.0, pan_invert=1, tilt_invert=0)
    assert home_orientation_warnings(declared, cfg_matching) == []

    #   Backfill only fills what's missing, and never touches an already-declared field
    partial = load_program("""
schema_version: 1
type: single
defaults:
  try: 1
  home_pan_abs: 99.0
points:
  - {pan_deg: 0, tilt_deg: 0}
""")
    backfill_recorded_setup(partial, dict(PANTILT_CFG, home_pan_abs=10.0, home_tilt_abs=-5.0,
                                          pan_invert=1, tilt_invert=0))
    assert partial["defaults"]["home_pan_abs"] == 99.0          # untouched: file already declared it
    assert partial["defaults"]["home_tilt_abs"] == -5.0          # backfilled from current config
    assert partial["defaults"]["pan_orientation"] == -1          # backfilled: pan_invert=1 -> -1
    assert partial["defaults"]["tilt_orientation"] == 1          # backfilled: tilt_invert=0 -> 1
    assert "recorded_settings" in partial and partial["recorded_settings"]["pan_max_speed"] == 64

    #   Round-trips through YAML and is still a valid, loadable program
    text = dump_program(partial)
    reloaded = load_program(text)
    assert reloaded["defaults"]["home_pan_abs"] == 99.0
    assert reloaded["defaults"]["pan_orientation"] == -1
    assert "initial_startdate_epoch" not in text  # internal/derived field is not persisted


if __name__ == "__main__":
    test_load_program()
    test_relative_limits()
    test_validate_points()
    test_pair_min_gap()
    test_validate_schedule()
    test_next_occurrence()
    test_summarize_and_preview()
    test_recorded_setup()
    print("✅ All pantilt program tests passed")
