#   Plain-assert tests for lib/keepout.py (no hardware needed).
#   Run with: python3 tests/test_keepout.py

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.keepout import (load_breakpoints, safe_range_at_tilt, is_safe,
                         safe_pan_intersection_over_sweep, profile_path, list_profiles)

#   A simple taper: wide open at tilt=-90, pinched to a narrow slot at tilt=90
BREAKPOINTS = [
    {"tilt": -90.0, "pan_min": -180.0, "pan_max": 180.0},
    {"tilt": 0.0, "pan_min": -30.0, "pan_max": 30.0},
    {"tilt": 90.0, "pan_min": -5.0, "pan_max": 5.0},
]


def test_load_breakpoints_missing_or_empty_file():
    assert load_breakpoints("/nonexistent/path.json") == []

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "empty.json")
        with open(path, "w") as f:
            json.dump([], f)
        assert load_breakpoints(path) == []


def test_load_breakpoints_single_bucket_is_unrestricted():
    #   Only one distinct tilt bucket -- not enough to interpolate, so the
    #   loader treats it the same as "nothing measured yet"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "one_bucket.json")
        with open(path, "w") as f:
            json.dump([{"tilt_deg": 10.0, "pan_deg": 5.0}, {"tilt_deg": 11.0, "pan_deg": -5.0}], f)
        assert load_breakpoints(path) == []


def test_load_breakpoints_buckets_and_sorts():
    samples = [
        {"tilt_deg": 0.4, "pan_deg": 20.0}, {"tilt_deg": -0.4, "pan_deg": -25.0},
        {"tilt_deg": 50.2, "pan_deg": 10.0}, {"tilt_deg": 49.8, "pan_deg": -8.0},
        {"tilt_deg": -50.0, "pan_deg": -40.0},
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "samples.json")
        with open(path, "w") as f:
            json.dump(samples, f)
        breakpoints = load_breakpoints(path, bucket_size=5.0)

    assert [b["tilt"] for b in breakpoints] == sorted(b["tilt"] for b in breakpoints)
    assert len(breakpoints) == 3
    #   The tilt=0 bucket merges the two samples near 0 into one min/max pair
    zero_bucket = next(b for b in breakpoints if b["tilt"] == 0.0)
    assert zero_bucket["pan_min"] == -25.0 and zero_bucket["pan_max"] == 20.0

    #   Malformed/missing fields are skipped, not fatal
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "malformed.json")
        with open(path, "w") as f:
            json.dump(samples + [{"tilt_deg": "not a number", "pan_deg": 1.0}, {"pan_deg": 1.0}], f)
        assert load_breakpoints(path, bucket_size=5.0) == breakpoints


def test_safe_range_at_tilt_unrestricted_when_empty():
    assert safe_range_at_tilt(0.0, []) == (-180.0, 180.0)


def test_safe_range_at_tilt_interpolates():
    lo, hi = safe_range_at_tilt(45.0, BREAKPOINTS)   # halfway between tilt=0 and tilt=90
    assert abs(lo - (-17.5)) < 1e-9 and abs(hi - 17.5) < 1e-9

    #   Exactly on a breakpoint
    assert safe_range_at_tilt(0.0, BREAKPOINTS) == (-30.0, 30.0)


def test_safe_range_at_tilt_flat_beyond_outermost():
    assert safe_range_at_tilt(-120.0, BREAKPOINTS) == (-180.0, 180.0)
    assert safe_range_at_tilt(120.0, BREAKPOINTS) == (-5.0, 5.0)


def test_is_safe():
    assert is_safe(0.0, 0.0, BREAKPOINTS)
    assert not is_safe(100.0, 0.0, BREAKPOINTS)   # outside +-30 at tilt=0
    assert is_safe(0.0, 0.0, [])   # unrestricted when nothing is configured

    #   Pan wraparound: 350 deg is the same physical angle as -10 deg
    assert is_safe(350.0, 0.0, BREAKPOINTS)


def test_safe_pan_intersection_over_sweep():
    #   Sweeping tilt 0 -> 90 must stay within the tightest range crossed,
    #   i.e. the tilt=90 breakpoint's narrow +-5 deg slot
    lo, hi = safe_pan_intersection_over_sweep(0.0, 90.0, BREAKPOINTS)
    assert abs(lo - (-5.0)) < 1e-9 and abs(hi - 5.0) < 1e-9

    #   A degenerate sweep (same start/end tilt) is just that tilt's range
    lo, hi = safe_pan_intersection_over_sweep(0.0, 0.0, BREAKPOINTS)
    assert (lo, hi) == (-30.0, 30.0)

    #   No breakpoints -> always unrestricted
    assert safe_pan_intersection_over_sweep(-90.0, 90.0, []) == (-180.0, 180.0)


def test_safe_pan_intersection_returns_none_when_impossible():
    #   Two envelopes that don't overlap at all -> no single pan value works
    disjoint = [
        {"tilt": 0.0, "pan_min": -30.0, "pan_max": -20.0},
        {"tilt": 10.0, "pan_min": 20.0, "pan_max": 30.0},
    ]
    assert safe_pan_intersection_over_sweep(0.0, 10.0, disjoint) is None


def test_profile_path_and_list_profiles():
    with tempfile.TemporaryDirectory() as d:
        assert profile_path(d, "") == ""
        assert profile_path(d, "radar_v1") == os.path.join(d, "radar_v1.json")
        #   Never trust a profile name as a path
        assert profile_path(d, "../../etc/passwd") == os.path.join(d, "passwd.json")

        assert list_profiles(d) == []
        for name in ("radar_v1", "lidar_unit"):
            with open(os.path.join(d, f"{name}.json"), "w") as f:
                json.dump([], f)
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("not a profile")
        assert list_profiles(d) == ["lidar_unit", "radar_v1"]

    assert list_profiles("/nonexistent/dir") == []


if __name__ == "__main__":
    test_load_breakpoints_missing_or_empty_file()
    test_load_breakpoints_single_bucket_is_unrestricted()
    test_load_breakpoints_buckets_and_sorts()
    test_safe_range_at_tilt_unrestricted_when_empty()
    test_safe_range_at_tilt_interpolates()
    test_safe_range_at_tilt_flat_beyond_outermost()
    test_is_safe()
    test_safe_pan_intersection_over_sweep()
    test_safe_pan_intersection_returns_none_when_impossible()
    test_profile_path_and_list_profiles()
    print("✅ All keepout tests passed")
