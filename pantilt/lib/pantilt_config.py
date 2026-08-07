#   The ONLY bridge between the pan-tilt app and the radar app. Reads and
#   writes a small, fixed set of fields in the RADAR app's config.yaml.
#   Radar's own code never imports this file and never needs to know the
#   pan-tilt app exists, beyond the `antenna_position` section in its config.
#
#   Not to be confused with lib/configuration.py (this app's own config.yaml
#   I/O) or ~/pantilt_config.yaml (this app's own deployed config file) --
#   this module is specifically the read/write path INTO the radar app's
#   config, named for the app that calls it, not the config it touches.
#
#   RADAR_CONFIG_PATH defaults to "~/config.yaml" -- the radar app's
#   existing, unchanged deployed location -- but can be overridden by the
#   RADAR_CONFIG_PATH environment variable. Once both apps run in separate
#   containers sharing one bind-mounted volume, setting this one variable
#   identically for both is the entire migration -- no code change needed.

import os
from typing import Optional

import yaml
from filelock import FileLock, Timeout

from lib.socket_helper import RADAR_SOCKET_PATH, connect_local_socket

RADAR_CONFIG_PATH = os.environ.get("RADAR_CONFIG_PATH", os.path.expanduser("~") + "/config.yaml")
RADAR_LOCK_PATH = RADAR_CONFIG_PATH + ".lock"

#   Deliberately bounded, unlike lib/configuration.py's unbounded FileLock
#   default: a hang or crash while radar holds the lock must not be able to
#   hang the pan-tilt app's own control loop or dashboard requests.
LOCK_TIMEOUT_SECONDS = 5

#   Errors that mean "radar's config could not be reached right now" --
#   logged and reported via reachable=False, never raised to the caller.
_UNREACHABLE_ERRORS = (FileNotFoundError, PermissionError, Timeout, yaml.YAMLError, OSError)


def _read_radar_config():
    with FileLock(RADAR_LOCK_PATH, timeout=LOCK_TIMEOUT_SECONDS):
        with open(RADAR_CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}


def _write_radar_config(mutator):
    #   Read-modify-write under one lock acquisition; mutator(config) edits
    #   the dict in place.
    with FileLock(RADAR_LOCK_PATH, timeout=LOCK_TIMEOUT_SECONDS):
        with open(RADAR_CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f) or {}
        mutator(config)
        with open(RADAR_CONFIG_PATH, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False)


def take_single_measurement() -> bool:
    """
    Fire-and-forget: request ONE VV+VH sweep from the radar app by setting
    measurement_status.single_measurement = 1 in radar's config.yaml.

    Does NOT check whether the radar is currently busy (call get_status()
    first and decide) and does NOT wait for the sweep to complete (poll
    get_status() afterward). Kept deliberately unconditional so all policy
    -- timeouts, what "aborted" means, whether pantilt.enabled matters --
    stays in pantilt.py, the only code that knows about those pan-tilt-side
    concepts. A blocking version of this function would have to duplicate
    that policy inside the bridge, which is exactly the coupling this
    module exists to avoid.

    Returns True if the write succeeded, False if radar's config.yaml
    could not be read/written (logged); callers should treat False as
    "the request probably did not go through."
    """
    try:
        def _set_pending(config):
            config.setdefault("measurement_status", {})["single_measurement"] = 1

        _write_radar_config(_set_pending)
        return True
    except _UNREACHABLE_ERRORS as e:
        print(f"⚠️ pantilt_config.take_single_measurement: radar config not reachable: {e}")
        return False


def get_status() -> dict:
    """
    Read-only snapshot of the radar app's state, as far as the pan-tilt app
    needs to see it. Never raises. Returns a dict, all keys always present:

      reachable                   bool  False if radar's config.yaml could
                                         not be read just now (missing file,
                                         permission error, lock timeout,
                                         YAML parse error).

      auto_measurement             bool  measurement_status.auto_measurement == 1.

      single_measurement_pending   bool  measurement_status.single_measurement == 1
                                         -- a request is queued, or the sweep
                                         it triggered hasn't finished yet.

      safe_to_request               bool  not (auto_measurement or
                                         single_measurement_pending).
                                         Deliberately NOT gated on `reachable`
                                         -- fails OPEN (True) when radar's
                                         config can't be read right now, the
                                         same fail-open behavior the old
                                         socket-based check had whenever the
                                         socket was absent/refused.

    IMPORTANT for callers: when reachable is False, auto_measurement and
    single_measurement_pending both default to False too (matching "not
    busy"). That default is correct for the *pre-request* safety check
    (safe_to_request), but it is WRONG to treat single_measurement_pending
    == False as "measurement finished" during the *post-request* completion
    poll unless reachable is also True -- a transient lock timeout must not
    be misread as "done". pantilt.py's measure() guards against this
    explicitly: it only accepts completion when `reachable and not
    single_measurement_pending`.
    """
    try:
        config = _read_radar_config()
        status = config.get("measurement_status", {})
        auto_measurement = status.get("auto_measurement", 0) == 1
        single_measurement_pending = status.get("single_measurement", 0) == 1
        return {
            "reachable": True,
            "auto_measurement": auto_measurement,
            "single_measurement_pending": single_measurement_pending,
            "safe_to_request": not (auto_measurement or single_measurement_pending),
        }
    except _UNREACHABLE_ERRORS as e:
        print(f"⚠️ pantilt_config.get_status: radar config not reachable: {e}")
        return {
            "reachable": False,
            "auto_measurement": False,
            "single_measurement_pending": False,
            "safe_to_request": True,
        }


def radar_app_running(timeout_seconds: float = 1.0) -> bool:
    """
    True if controller.py's own liveness socket answers right now.

    This is a DIFFERENT question from get_status()["reachable"]: that only
    proves radar's config.yaml can be read, which is true from initial setup
    onward whether or not controller.py has ever run -- it can't tell "app
    not running" apart from "app running but idle". This connects to
    controller.py's socket instead (bound only while its main loop is alive),
    so an absent radar app is detected in ~timeout_seconds instead of only
    surfacing after measure()'s full measure_timeout_seconds wait for a
    single_measurement flag nothing will ever clear.

    Never raises; any connection failure (socket file/port absent, connection
    refused, timeout) means False.
    """
    try:
        client = connect_local_socket(RADAR_SOCKET_PATH)
        try:
            client.settimeout(timeout_seconds)
            return bool(client.recv(1024))
        finally:
            client.close()
    except OSError as e:
        print(f"⚠️ pantilt_config.radar_app_running: radar app not reachable: {e}")
        return False


def write_angle(pan: Optional[float], tilt: Optional[float]) -> bool:
    """
    Write the antenna's current relative pan/tilt angle (degrees, float,
    full precision -- no rounding here) into radar's config.yaml
    (antenna_position.pan / antenna_position.tilt), so librevna.py can embed
    it in the next measurement's filename exactly as it does today.
    Rounding-to-nearest-degree for the filename stays librevna.py's own
    responsibility, unchanged, just reading a new location.

    Call write_angle(None, None) to CLEAR the position (writes YAML null to
    both fields). librevna.py treats a null antenna_position as "no
    pan-tilt tag" -- the direct replacement for today's pantilt.enabled == 0
    gate, without radar's config needing to know a flag called "enabled"
    exists. pantilt.py calls write_angle(None, None) exactly once, on the
    falling edge of pantilt.enabled (module switched off).

    Unlike take_single_measurement() (which assumes measurement_status
    already exists -- true for every deployed config.yaml since Jarne's
    original commit), write_angle() creates the antenna_position section if
    it's missing, since it's brand new surface that won't exist on an
    unmigrated deployed config.yaml.

    Returns True if the write succeeded, False otherwise (logged).
    """
    try:
        def _set_angle(config):
            config.setdefault("antenna_position", {})
            config["antenna_position"]["pan"] = pan
            config["antenna_position"]["tilt"] = tilt

        _write_radar_config(_set_angle)
        return True
    except _UNREACHABLE_ERRORS as e:
        print(f"⚠️ pantilt_config.write_angle: radar config not reachable: {e}")
        return False
