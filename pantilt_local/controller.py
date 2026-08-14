#   Local pan-tilt-only controller.
#
#   Single-process replacement for the deployed pantilt/pantilt.py daemon:
#   owns the serial connection to the positioner (PTCR-96 protocol, MN00162),
#   runs a background loop that polls status, executes queued commands from
#   the Flask app, and drives sequence programs -- "single" (run once) and
#   "automated" (repeating schedule). There is no radar: the measurement step
#   of a sequence is simulated (a short delay with log messages) instead of
#   triggering a real VNA sweep.

import os
import queue
import threading
import time
from datetime import datetime, timezone

import yaml

from lib import qpt90
from lib import pantilt_program
from lib import keepout

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "pantilt_local_config.yaml")
PROGRAMS_DIR = os.path.join(BASE_DIR, "programs")
#   One keep-out sample file per instrument/mount (different antenna geometry
#   -> different safe envelope); which one is active is the keepout_profile
#   setting below. debug_notebook.ipynb writes into this same directory.
KEEPOUT_DIR = os.path.join(BASE_DIR, "config", "keepout")
os.makedirs(PROGRAMS_DIR, exist_ok=True)
os.makedirs(KEEPOUT_DIR, exist_ok=True)

#   Hard caps: configured limits can never exceed these
PAN_ABS_CAP = 180.0
TILT_ABS_CAP = 90.0

#   Connection retry ladder: every 10 s for the first 5 minutes, then every 30 minutes
RETRY_FAST_S = 10
RETRY_FAST_WINDOW_S = 300
RETRY_SLOW_S = 1800

MAX_LOG_LINES = 50

SETTINGS_DEFAULTS = {
    "port": "", "baud": 9600,
    "pan_min_abs": -180.0, "pan_max_abs": 180.0,
    "tilt_min_abs": -90.0, "tilt_max_abs": 90.0,
    "home_pan_abs": 0.0, "home_tilt_abs": 0.0,
    "pan_invert": 0, "tilt_invert": 0,
    "pan_max_speed": 64, "tilt_max_speed": 64,
    "speed_deg_per_s_estimate": 4.0, "settle_seconds": 2,
    "move_timeout_seconds": 120, "min_gap_seconds": 60, "warn_margin_deg": 2.0,
    "heater_config": 1, "simulated_measure_seconds": 1.0,   # 1=off in the UI's 1/2/3 convention -- see apply_heater_config
    "keepout_profile": "",   # basename (no .json) of the active file in config/keepout/; "" = unrestricted
}


class ConnectionLost(Exception):
    pass


_lock = threading.RLock()
_settings = {}
_command_queue = queue.Queue()
_stop_event = threading.Event()

#   Worker-thread-owned hardware handles
driver = None
serial_port = None
connected_port_name = ""
retry_started = 0.0
next_retry = 0.0

#   Active sequence (in memory only -- this is a single, short-lived local run)
active_prog = None
active_prog_file = ""
auto_schedule = {}   # point index -> next due time (unix s), automated only

#   Coupled pan/tilt keep-out envelope (lib/keepout.py) for whichever
#   instrument/mount is currently selected (settings.keepout_profile).
#   Empty list = no profile selected, or nothing measured yet = unrestricted
#   (keepout.is_safe always returns True).
_keepout_breakpoints = []

#   State shared with the Flask app; every mutation happens under _lock so
#   get_live() always returns a consistent snapshot
live = {
    "connected": 0, "port": "",
    "pan_rel": 0.0, "tilt_rel": 0.0, "pan_abs": 0.0, "tilt_abs": 0.0,
    "moving": 0, "measuring": 0, "fault": "", "retry_in_s": 0,
    "program_name": "", "program_type": "", "program_state": "", "program_progress": "",
    "program_paused": 0, "program_next_index": 0,
    "steps_done": 0, "steps_total": 0, "upcoming_points": [],
    "estimated_remaining_s": 0, "heater_state": 0, "log": [],
    "next_measurement_utc": "", "next_measurement_in_s": 0,
    "next_pan_rel": 0.0, "next_tilt_rel": 0.0,
    "keepout_profile": "", "keepout_breakpoints": 0,
}


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        live["log"].append(f"[{ts}] {msg}")
        del live["log"][:-MAX_LOG_LINES]
    print(msg)


def get_live():
    with _lock:
        return {**live, "log": list(live["log"]), "upcoming_points": list(live["upcoming_points"])}


def enqueue_command(cmd):
    _command_queue.put(cmd)


#   Settings (persisted YAML: connection, limits, home, speeds, ...)

def _write_settings_file(data):
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)


def load_settings():
    data = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            data = yaml.safe_load(f) or {}
    changed = False
    for key, value in SETTINGS_DEFAULTS.items():
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        _write_settings_file(data)
    with _lock:
        _settings.clear()
        _settings.update(data)
        return dict(_settings)


def get_settings():
    with _lock:
        return dict(_settings)


def update_settings(updates):
    with _lock:
        _settings.update(updates)
        snapshot = dict(_settings)
    _write_settings_file(snapshot)
    return snapshot


def keepout_profile_path(profile):
    #   Basename only -- never trust a profile name as a path
    name = os.path.basename(profile or "")
    return os.path.join(KEEPOUT_DIR, f"{name}.json") if name else ""


def list_keepout_profiles():
    if not os.path.isdir(KEEPOUT_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(KEEPOUT_DIR) if f.lower().endswith(".json"))


def load_keepout(cfg):
    #   Re-run whenever keepout_profile changes (see the "update_settings"
    #   command) as well as at startup -- cheap (a small JSON file), and the
    #   profile is meant to be switched between instruments without
    #   restarting the app. The debug notebook writes samples offline
    #   (app.py and the notebook can't hold the serial port at the same
    #   time), so there's no need to hot-reload the SAME file mid-session.
    global _keepout_breakpoints
    profile = cfg.get("keepout_profile", "")
    path = keepout_profile_path(profile)

    if not path:
        _keepout_breakpoints = []
        with _lock:
            live["keepout_profile"] = ""
            live["keepout_breakpoints"] = 0
        log("No keep-out profile selected -- pan/tilt moves are unrestricted by antenna geometry")
        return

    _keepout_breakpoints = keepout.load_breakpoints(path)
    with _lock:
        live["keepout_profile"] = profile
        live["keepout_breakpoints"] = len(_keepout_breakpoints)

    if _keepout_breakpoints:
        log(f"Keep-out envelope loaded: profile '{profile}', {len(_keepout_breakpoints)} breakpoint(s)")
    else:
        log(f"Keep-out profile '{profile}' has no/insufficient samples yet ({path}) -- "
            "pan/tilt moves are unrestricted by antenna geometry")


#   Limits & fault helpers

def effective_limits(cfg):
    #   pan_min_abs/pan_max_abs describe the RAW (hardware) travel range; clip
    #   to the hard caps in that raw frame, then convert to the logical frame
    #   (pan_abs/tilt_abs) everything else is validated against -- inversion
    #   negates AND swaps min/max.
    raw_pan_min = max(float(cfg.get("pan_min_abs", -PAN_ABS_CAP)), -PAN_ABS_CAP)
    raw_pan_max = min(float(cfg.get("pan_max_abs", PAN_ABS_CAP)), PAN_ABS_CAP)
    raw_tilt_min = max(float(cfg.get("tilt_min_abs", -TILT_ABS_CAP)), -TILT_ABS_CAP)
    raw_tilt_max = min(float(cfg.get("tilt_max_abs", TILT_ABS_CAP)), TILT_ABS_CAP)

    pan_min, pan_max = (-raw_pan_max, -raw_pan_min) if cfg.get("pan_invert", 0) == 1 else (raw_pan_min, raw_pan_max)
    tilt_min, tilt_max = (-raw_tilt_max, -raw_tilt_min) if cfg.get("tilt_invert", 0) == 1 else (raw_tilt_min, raw_tilt_max)
    return (pan_min, pan_max, tilt_min, tilt_max)


def target_allowed(pan_abs, tilt_abs, cfg):
    pan_min, pan_max, tilt_min, tilt_max = effective_limits(cfg)
    return pan_min <= pan_abs <= pan_max and tilt_min <= tilt_abs <= tilt_max


def flip(value, inverted):
    #   Sign convention between "logical" pan/tilt (home, limits, programs)
    #   and the QPT's own raw angle, for units mounted upside-down/reversed.
    return -value if inverted else value


def set_fault(text):
    with _lock:
        changed = live["fault"] != text
        live["fault"] = text
    if changed:
        log(f"Fault: {text}" if text else "Fault cleared")


#   Connection handling

def poll_status(cfg):
    try:
        status = driver.get_status()
    except (qpt90.QptError, OSError) as e:
        raise ConnectionLost(str(e))

    pan_deg = flip(status.pan_deg, cfg.get("pan_invert", 0) == 1)
    tilt_deg = flip(status.tilt_deg, cfg.get("tilt_invert", 0) == 1)
    home_pan = float(cfg.get("home_pan_abs", 0.0))
    home_tilt = float(cfg.get("home_tilt_abs", 0.0))

    with _lock:
        live["pan_abs"] = round(pan_deg, 1)
        live["tilt_abs"] = round(tilt_deg, 1)
        live["pan_rel"] = round(pan_deg - home_pan, 1)
        live["tilt_rel"] = round(tilt_deg - home_tilt, 1)
        live["moving"] = 1 if status.moving else 0

    if status.faults and not live["fault"]:
        set_fault("Positioner fault: " + ", ".join(status.faults))

    return status


def apply_heater_config(cfg):
    #   97H: set the desired mode, then re-query independently to confirm the
    #   unit actually accepted it (a mismatch means no heater is fitted, or a
    #   fault prevented the change) rather than trusting only the set's own ACK.
    #
    #   The UI/config heater_config field is 1=Off/2=Share/3=Full (unchanged
    #   convention, shared with the old removed driver's byte values), but
    #   qpt90's actual wire format (MN00162 Sec 2.9.7) is 0=No Heat/1=Share/
    #   2=Full Heat with a separate Query bit -- translate by -1/+1 at this
    #   boundary and keep the UI-facing 1/2/3 convention everywhere else.
    desired_ui = int(cfg.get("heater_config", 1))
    desired = max(qpt90.HEATER_OFF, min(qpt90.HEATER_FULL, desired_ui - 1))
    try:
        driver.set_heater_config(desired)
        confirmed = driver.get_heater_config()
    except (qpt90.QptError, OSError) as e:
        log(f"Heater config could not be applied: {e}")
        with _lock:
            live["heater_state"] = 0
        set_fault(f"Heater config could not be applied: {e}")
        return False

    confirmed_ui = confirmed + 1
    with _lock:
        live["heater_state"] = confirmed_ui

    if confirmed != desired:
        set_fault(f"Heater confirmed in mode {confirmed_ui}, requested mode {desired_ui}")
        return False

    return True


def apply_comm_timeout():
    #   96H: hardware backstop -- the positioner halts itself if the daemon
    #   dies or the link drops for more than this many seconds. Fixed value,
    #   not user-configurable (matches the old removed driver's behavior).
    try:
        driver.set_comm_timeout(2)
    except (qpt90.QptError, OSError) as e:
        log(f"Comm timeout could not be applied: {e}")
        set_fault(f"Comm timeout could not be applied: {e}")
        return False
    return True


def apply_max_speed(cfg):
    #   9CH: caps automated-move speed per axis. Session-only (not written to
    #   non-volatile memory) so every reconnect re-applies whatever the
    #   config currently says, same as heater/comm-timeout.
    pan_max = int(cfg.get("pan_max_speed", 64))
    tilt_max = int(cfg.get("tilt_max_speed", 64))
    try:
        driver.set_max_speed(pan_max, tilt_max)
    except (qpt90.QptError, OSError) as e:
        log(f"Max speed could not be applied: {e}")
        set_fault(f"Max speed could not be applied: {e}")
        return False
    return True


def apply_positioner_settings(cfg):
    apply_comm_timeout()
    apply_max_speed(cfg)
    apply_heater_config(cfg)


def try_connect(cfg):
    global driver, serial_port, connected_port_name

    connected_port_name, driver = qpt90.find_qpt90(cfg.get("port", ""), int(cfg.get("baud", 9600)))
    serial_port = driver.ser if driver else None

    if driver is None:
        return False

    try:
        apply_positioner_settings(cfg)
        poll_status(cfg)
    except (ConnectionLost, qpt90.QptError, OSError) as e:
        log(f"Connection lost during setup: {e}")
        disconnect()
        return False

    with _lock:
        live["connected"] = 1
        live["port"] = connected_port_name
        live["retry_in_s"] = 0
    log(f"Connected on {connected_port_name}")
    return True


def disconnect(stop_motion=False):
    global driver, serial_port, connected_port_name

    if driver is not None and stop_motion:
        try:
            driver.stop()
        except Exception:
            pass
    if serial_port is not None:
        try:
            serial_port.close()
        except Exception:
            pass

    driver = None
    serial_port = None
    connected_port_name = ""
    with _lock:
        live["connected"] = 0
        live["port"] = ""
        live["moving"] = 0
        live["heater_state"] = 0


#   Blocking motion / simulated-measurement primitives. They keep polling the
#   positioner (keep-alive) while waiting and abort early if a sequence stop
#   is requested (_stop_event).

def wait_move_done(cfg):
    #   Returns "done", "aborted" or "fault"
    deadline = time.time() + float(cfg.get("move_timeout_seconds", 120))

    while time.time() < deadline:
        if _stop_event.is_set():
            driver.stop()
            return "aborted"
        time.sleep(0.1)
        status = poll_status(cfg)
        if status.faults:
            return "fault"
        if not status.moving:
            return "done"

    driver.stop()
    set_fault(f"Move timed out after {cfg.get('move_timeout_seconds', 120)} s")
    return "fault"


def _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, cfg):
    #   Sends ONE move to the driver and waits for it to finish. pan_abs/
    #   tilt_abs (logical/display values) are only used for the fault
    #   message if the positioner itself rejects the move.
    try:
        driver.move_to(raw_pan, raw_tilt)
    except qpt90.QptNak:
        set_fault(f"Positioner rejected move to pan {pan_abs}° / tilt {tilt_abs}°")
        return "fault"
    except (qpt90.QptError, OSError) as e:
        raise ConnectionLost(str(e))
    return wait_move_done(cfg)


def move_abs(pan_abs, tilt_abs, cfg):
    #   Validated absolute move; returns "done", "aborted" or "fault".
    #   Two independent safety checks: the existing box limits (pan/tilt
    #   min/max_abs) and the coupled antenna/bar keep-out envelope
    #   (lib/keepout.py) -- see that module's docstring for why the keep-out
    #   check operates on RAW hardware angles, not these logical ones.
    if not target_allowed(pan_abs, tilt_abs, cfg):
        pan_min, pan_max, tilt_min, tilt_max = effective_limits(cfg)
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: outside absolute limits "
                  f"(pan [{pan_min}°, {pan_max}°], tilt [{tilt_min}°, {tilt_max}°])")
        return "fault"

    pan_inverted = cfg.get("pan_invert", 0) == 1
    tilt_inverted = cfg.get("tilt_invert", 0) == 1
    raw_pan = flip(pan_abs, pan_inverted)
    raw_tilt = flip(tilt_abs, tilt_inverted)

    if not keepout.is_safe(raw_pan, raw_tilt, _keepout_breakpoints):
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: inside the antenna/bar keep-out zone")
        return "fault"

    if not _keepout_breakpoints:
        return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, cfg)

    with _lock:
        cur_raw_pan = flip(live["pan_abs"], pan_inverted)
        cur_raw_tilt = flip(live["tilt_abs"], tilt_inverted)

    if cur_raw_pan == raw_pan or cur_raw_tilt == raw_tilt:
        #   Already a single-axis move -- the endpoint check above already
        #   covers the whole path, since only one coordinate is changing.
        return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, cfg)

    #   Coupled move: the protocol doesn't guarantee a straight-line path
    #   between two diagonal pan/tilt targets -- each axis has its own motor
    #   and speed with no coordinated trajectory described in MN00162, so a
    #   single diagonal move_to could cut through the keep-out zone even if
    #   both endpoints are individually safe. Sequence it as three
    #   single-axis legs instead, through a pan value proven safe across
    #   every tilt the move will cross.
    raw_pan_min = max(float(cfg.get("pan_min_abs", -PAN_ABS_CAP)), -PAN_ABS_CAP)
    raw_pan_max = min(float(cfg.get("pan_max_abs", PAN_ABS_CAP)), PAN_ABS_CAP)
    safe_range = keepout.safe_pan_intersection_over_sweep(cur_raw_tilt, raw_tilt, _keepout_breakpoints)
    if safe_range is not None:
        lo = max(safe_range[0], raw_pan_min)
        hi = min(safe_range[1], raw_pan_max)
        safe_range = (lo, hi) if lo <= hi else None
    if safe_range is None:
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: no single-axis-safe path "
                  f"avoids the keep-out zone between the current and target tilt")
        return "fault"
    waypoint_pan = min(max(cur_raw_pan, safe_range[0]), safe_range[1])

    result = _send_move(waypoint_pan, cur_raw_tilt, pan_abs, tilt_abs, cfg)
    if result != "done":
        return result
    result = _send_move(waypoint_pan, raw_tilt, pan_abs, tilt_abs, cfg)
    if result != "done":
        return result
    return _send_move(raw_pan, raw_tilt, pan_abs, tilt_abs, cfg)

    return wait_move_done(cfg)


def settle(cfg):
    until = time.time() + float(cfg.get("settle_seconds", 2))
    while time.time() < until:
        if _stop_event.is_set():
            return
        time.sleep(0.1)
        poll_status(cfg)


def simulate_measurement(cfg):
    #   Stand-in for a real radar sweep: no hardware/network involved, just a
    #   short delay so a sequence "feels" like it's really measuring.
    log("Taking virtual measurement...")
    with _lock:
        live["measuring"] = 1

    deadline = time.time() + float(cfg.get("simulated_measure_seconds", 1.0))
    result = "done"
    while time.time() < deadline:
        if _stop_event.is_set():
            result = "aborted"
            break
        time.sleep(0.1)

    with _lock:
        live["measuring"] = 0
    if result == "done":
        log("Measurement done")
    return result


def run_point(point, cfg, tries):
    #   Move to a sequence point and simulate a measurement there.
    #   Returns "done", "aborted" or "fault".
    pan_abs = float(cfg.get("home_pan_abs", 0.0)) + float(point["pan_deg"])
    tilt_abs = float(cfg.get("home_tilt_abs", 0.0)) + float(point["tilt_deg"])

    result = "fault"
    for attempt in range(tries):
        if _stop_event.is_set():
            return "aborted"
        if attempt > 0:
            driver.clear_faults()
        result = move_abs(pan_abs, tilt_abs, cfg)
        if result != "fault":
            break
        log(f"Move attempt {attempt + 1}/{tries} failed")
    if result != "done":
        return result

    settle(cfg)
    if _stop_event.is_set():
        return "aborted"

    return simulate_measurement(cfg)


#   Sequence ("program") handling -- "single" type only

def clear_program():
    global active_prog, active_prog_file, auto_schedule
    active_prog = None
    active_prog_file = ""
    auto_schedule = {}
    with _lock:
        live["program_name"] = ""
        live["program_type"] = ""
        live["program_state"] = ""
        live["program_progress"] = ""
        live["program_paused"] = 0
        live["program_next_index"] = 0
        live["steps_done"] = 0
        live["steps_total"] = 0
        live["upcoming_points"] = []
        live["estimated_remaining_s"] = 0
        live["next_measurement_utc"] = ""
        live["next_measurement_in_s"] = 0
        live["next_pan_rel"] = 0.0
        live["next_tilt_rel"] = 0.0


def pause_program(reason):
    with _lock:
        live["program_paused"] = 1
        live["program_state"] = "paused"
    if reason:
        set_fault(reason)
    log("Sequence paused" + (f": {reason}" if reason else ""))


def start_program(program_path, cfg):
    global active_prog, active_prog_file, auto_schedule

    if active_prog is not None:
        set_fault("A sequence is already running -- stop it first")
        return None

    try:
        with open(program_path, "r") as f:
            prog = pantilt_program.load_program(f.read())
    except (OSError, pantilt_program.ProgramError) as e:
        set_fault(f"Cannot start sequence '{program_path}': {e}")
        return None

    #   Apply the program's recorded home position / axis orientation (if
    #   any) before validating against limits
    overrides = pantilt_program.home_orientation_overrides(prog, cfg)
    if overrides:
        cfg = update_settings(overrides)

    reports = pantilt_program.validate_points(prog, cfg)
    conflicts = pantilt_program.validate_schedule(prog, cfg)
    if any(r["status"] == "error" for r in reports) or conflicts:
        set_fault(f"Sequence '{program_path}' is invalid for the current limits/home position")
        return None

    set_fault("")
    active_prog = prog
    active_prog_file = program_path
    with _lock:
        live["program_name"] = os.path.basename(program_path)
        live["program_type"] = prog["type"]
        live["program_state"] = "running"
        live["program_paused"] = 0
        live["program_next_index"] = 0
        live["steps_total"] = len(prog["points"])
        live["steps_done"] = 0

    if prog["type"] == "automated":
        now_s = int(time.time())
        ref_epoch = prog["initial_startdate_epoch"]
        auto_schedule = {i: pantilt_program.next_occurrence(p, now_s, ref_epoch) for i, p in enumerate(prog["points"])}
        log(f"Automated sequence started: {os.path.basename(program_path)} ({len(prog['points'])} scheduled point(s))")
    else:
        auto_schedule = {}
        log(f"Sequence started: {os.path.basename(program_path)} ({len(prog['points'])} points)")

    return prog


def estimate_remaining_seconds(prog, index, pan_rel, tilt_rel, cfg):
    speed = max(float(cfg.get("speed_deg_per_s_estimate", 4.0)), 0.1)
    settle_s = float(cfg.get("settle_seconds", 2))
    measure_estimate = pantilt_program.MEASURE_SECONDS_ESTIMATE

    duration = 0.0
    pan, tilt = pan_rel, tilt_rel
    for point in prog["points"][index:]:
        duration += max(abs(point["pan_deg"] - pan), abs(point["tilt_deg"] - tilt)) / speed
        duration += settle_s + measure_estimate
        pan, tilt = point["pan_deg"], point["tilt_deg"]
    return duration


def run_program_tick(cfg):
    prog = active_prog
    with _lock:
        index = live["program_next_index"]
        paused = live["program_paused"] == 1
        pan_rel, tilt_rel = live["pan_rel"], live["tilt_rel"]

    if paused:
        return

    if index >= len(prog["points"]):
        log("Sequence finished")
        clear_program()
        return

    point = prog["points"][index]
    with _lock:
        live["program_progress"] = f"{index + 1}/{len(prog['points'])}"
        live["steps_done"] = index
        live["upcoming_points"] = [{"pan_rel": p["pan_deg"], "tilt_rel": p["tilt_deg"]}
                                    for p in prog["points"][index:index + 3]]
        live["estimated_remaining_s"] = int(estimate_remaining_seconds(prog, index, pan_rel, tilt_rel, cfg))

    log(f"Point {index + 1}/{len(prog['points'])}: moving to pan {point['pan_deg']}° / "
        f"tilt {point['tilt_deg']}° (relative to home)")
    result = run_point(point, cfg, prog["defaults"]["try"])

    if result == "done":
        with _lock:
            live["program_next_index"] = index + 1
        if index + 1 >= len(prog["points"]):
            log("Sequence finished")
            clear_program()
    elif result == "aborted":
        clear_program()
        log("Sequence stopped")
    elif result == "fault":
        pause_program(live["fault"] or f"Point {index + 1} failed")


def run_automated_series_tick(cfg):
    #   Fire the earliest due point; between occurrences the loop just idles.
    #   Ported from pantilt/pantilt.py's run_automated_series_tick, adapted to
    #   this module's _lock/log()/simulate_measurement() conventions.
    global auto_schedule

    prog = active_prog
    with _lock:
        paused = live["program_paused"] == 1
    if paused or not auto_schedule:
        return

    now_s = time.time()
    index = min(auto_schedule, key=auto_schedule.get)
    due = auto_schedule[index]
    point = prog["points"][index]

    with _lock:
        live["next_measurement_utc"] = datetime.fromtimestamp(due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        live["next_measurement_in_s"] = max(0, int(due - now_s))
        live["next_pan_rel"] = point["pan_deg"]
        live["next_tilt_rel"] = point["tilt_deg"]

    if now_s < due:
        return

    period = point["repeated_minutes"] * 60
    lateness = now_s - due
    ref_epoch = prog["initial_startdate_epoch"]

    if lateness > min(float(cfg.get("min_gap_seconds", 60)), period / 2):
        #   Too late (previous point overran, paused, ...): skip this occurrence
        log(f"Skipping point {index + 1}: {int(lateness)} s late")
        auto_schedule[index] = pantilt_program.next_occurrence(point, int(now_s), ref_epoch)
        return

    with _lock:
        live["program_progress"] = f"point {index + 1}/{len(prog['points'])}"
    log(f"Point {index + 1}/{len(prog['points'])}: moving to pan {point['pan_deg']}° / "
        f"tilt {point['tilt_deg']}° (relative to home)")
    result = run_point(point, cfg, prog["defaults"]["try"])
    auto_schedule[index] = pantilt_program.next_occurrence(point, int(time.time()), ref_epoch)

    if result == "aborted":
        clear_program()
        log("Sequence stopped")
    elif result == "fault":
        pause_program(live["fault"] or f"Point {index + 1} failed")


#   Dashboard commands (queued by app.py, drained here)

def process_commands():
    global active_prog

    cfg = get_settings()

    while True:
        try:
            cmd = _command_queue.get_nowait()
        except queue.Empty:
            break

        kind = cmd.get("type")

        #   Works even while disconnected (e.g. configuring the COM port)
        if kind == "update_settings":
            values = cmd.get("values", {})
            cfg = update_settings(values)
            if "keepout_profile" in values:
                load_keepout(cfg)
            if driver is not None:
                apply_positioner_settings(cfg)
                if not target_allowed(live["pan_abs"], live["tilt_abs"], cfg):
                    set_fault("Current position is outside the new absolute limits -- move back inside them")
            continue

        if driver is None:
            set_fault("Not connected to the positioner")
            continue

        if kind == "stop_program":
            if active_prog is not None:
                _stop_event.set()
                driver.stop()
                clear_program()
                log("Sequence stopped")
                _stop_event.clear()
            continue

        if kind == "pause_program":
            with _lock:
                already_paused = live["program_paused"] == 1
            if active_prog is not None and not already_paused:
                pause_program("")
            continue

        if kind == "resume_program":
            with _lock:
                is_paused = live["program_paused"] == 1
            if active_prog is not None and is_paused:
                driver.clear_faults()
                set_fault("")
                with _lock:
                    live["program_paused"] = 0
                    live["program_state"] = "running"
                log("Sequence resumed")
            continue

        if kind == "run_program":
            start_program(cmd.get("path"), cfg)
            continue

        #   Manual (jog/home/measure/heater) commands: not while a sequence
        #   is actively running (pause it first)
        with _lock:
            program_running = active_prog is not None and live["program_paused"] == 0
        if program_running:
            set_fault("Pause or stop the active sequence before issuing manual commands")
            continue

        if kind == "jog":
            with _lock:
                pan_now, tilt_now = live["pan_abs"], live["tilt_abs"]
            move_abs(pan_now + float(cmd.get("pan", 0.0)), tilt_now + float(cmd.get("tilt", 0.0)), cfg)

        elif kind == "move_rel":
            move_abs(float(cfg.get("home_pan_abs", 0.0)) + float(cmd.get("pan_rel", 0.0)),
                     float(cfg.get("home_tilt_abs", 0.0)) + float(cmd.get("tilt_rel", 0.0)), cfg)

        elif kind == "set_home":
            with _lock:
                moving, pan_now, tilt_now = live["moving"], live["pan_abs"], live["tilt_abs"]
            if moving:
                set_fault("Cannot set the home position while moving")
            else:
                cfg = update_settings({"home_pan_abs": pan_now, "home_tilt_abs": tilt_now})
                log(f"Home position set to pan {pan_now}° / tilt {tilt_now}° (absolute)")

        elif kind == "clear_fault":
            driver.clear_faults()
            set_fault("")

        elif kind == "set_heater":
            cfg = update_settings({"heater_config": int(cmd.get("value", 1))})
            apply_heater_config(cfg)

        elif kind == "manual_measure":
            with _lock:
                moving = live["moving"]
            if moving:
                set_fault("Cannot measure while moving")
            else:
                simulate_measurement(cfg)


#   Main loop

def run_forever():
    global retry_started, next_retry

    cfg = load_settings()
    load_keepout(cfg)
    retry_started = time.time()
    next_retry = time.time()

    while True:
        time.sleep(0.1)
        process_commands()
        cfg = get_settings()

        if driver is None:
            with _lock:
                live["retry_in_s"] = max(0, int(next_retry - time.time()))
            if time.time() >= next_retry:
                if not try_connect(cfg):
                    interval = RETRY_FAST_S if time.time() - retry_started < RETRY_FAST_WINDOW_S else RETRY_SLOW_S
                    next_retry = time.time() + interval
                    log(f"Positioner not found, retrying in {interval} s")
            continue

        try:
            poll_status(cfg)
            if active_prog is not None:
                if active_prog["type"] == "single":
                    run_program_tick(cfg)
                else:
                    run_automated_series_tick(cfg)
        except (ConnectionLost, qpt90.QptError, OSError) as e:
            log(f"Connection lost: {e}")
            disconnect()
            retry_started = time.time()
            next_retry = time.time()


def start_worker():
    t = threading.Thread(target=run_forever, daemon=True)
    t.start()
    return t
