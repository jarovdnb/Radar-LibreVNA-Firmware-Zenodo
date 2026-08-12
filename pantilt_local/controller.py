#   Local pan-tilt-only controller.
#
#   Single-process replacement for the deployed pantilt/pantilt.py daemon:
#   owns the serial connection to the QPT-50, runs a background loop that
#   polls status, executes queued commands from the Flask app, and drives
#   "single" sequence programs. There is no radar: the measurement step of a
#   sequence is simulated (a short delay with log messages) instead of
#   triggering a real VNA sweep.

import os
import queue
import threading
import time
from datetime import datetime

import yaml

from lib import qpt
from lib import pantilt_program

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "pantilt_local_config.yaml")
PROGRAMS_DIR = os.path.join(BASE_DIR, "programs")
os.makedirs(PROGRAMS_DIR, exist_ok=True)

#   Hard caps (QPT-50 protocol range): configured limits can never exceed these
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
    "heater_config": qpt.HEATER_OFF, "simulated_measure_seconds": 1.0,
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

#   State shared with the Flask app; every mutation happens under _lock so
#   get_live() always returns a consistent snapshot
live = {
    "connected": 0, "port": "",
    "pan_rel": 0.0, "tilt_rel": 0.0, "pan_abs": 0.0, "tilt_abs": 0.0,
    "moving": 0, "measuring": 0, "fault": "", "retry_in_s": 0,
    "program_name": "", "program_state": "", "program_progress": "",
    "program_paused": 0, "program_next_index": 0,
    "steps_done": 0, "steps_total": 0, "upcoming_points": [],
    "estimated_remaining_s": 0, "heater_state": 0, "log": [],
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
    except (qpt.QptError, OSError) as e:
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
    desired = int(cfg.get("heater_config", qpt.HEATER_OFF))
    try:
        driver.set_heater_config(desired)
        confirmed = driver.get_heater_config()
    except (qpt.QptError, OSError) as e:
        log(f"Heater config could not be applied: {e}")
        with _lock:
            live["heater_state"] = 0
        set_fault(f"Heater config could not be applied: {e}")
        return False

    with _lock:
        live["heater_state"] = confirmed
    if confirmed != desired:
        set_fault(f"Heater confirmed in mode {confirmed}, requested mode {desired}")
        return False
    return True


def apply_positioner_settings(cfg):
    driver.set_max_speeds(int(cfg.get("pan_max_speed", 64)), int(cfg.get("tilt_max_speed", 64)))
    #   Hardware backstop: the unit halts by itself if this process dies
    driver.set_comm_timeout(2)
    apply_heater_config(cfg)


def try_connect(cfg):
    global driver, serial_port, connected_port_name

    connected_port_name, driver = qpt.find_qpt(cfg.get("port", ""), int(cfg.get("baud", 9600)))
    serial_port = driver.ser if driver else None

    if driver is None:
        return False

    try:
        apply_positioner_settings(cfg)
        poll_status(cfg)
    except (ConnectionLost, qpt.QptError, OSError) as e:
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


def move_abs(pan_abs, tilt_abs, cfg):
    #   Validated absolute move; returns "done", "aborted" or "fault"
    if not target_allowed(pan_abs, tilt_abs, cfg):
        pan_min, pan_max, tilt_min, tilt_max = effective_limits(cfg)
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: outside absolute limits "
                  f"(pan [{pan_min}°, {pan_max}°], tilt [{tilt_min}°, {tilt_max}°])")
        return "fault"

    raw_pan = flip(pan_abs, cfg.get("pan_invert", 0) == 1)
    raw_tilt = flip(tilt_abs, cfg.get("tilt_invert", 0) == 1)

    try:
        driver.move_to(raw_pan, raw_tilt)
    except qpt.QptNak:
        set_fault(f"Positioner rejected move to pan {pan_abs}° / tilt {tilt_abs}°")
        return "fault"
    except (qpt.QptError, OSError) as e:
        raise ConnectionLost(str(e))

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
    global active_prog, active_prog_file
    active_prog = None
    active_prog_file = ""
    with _lock:
        live["program_name"] = ""
        live["program_state"] = ""
        live["program_progress"] = ""
        live["program_paused"] = 0
        live["program_next_index"] = 0
        live["steps_done"] = 0
        live["steps_total"] = 0
        live["upcoming_points"] = []
        live["estimated_remaining_s"] = 0


def pause_program(reason):
    with _lock:
        live["program_paused"] = 1
        live["program_state"] = "paused"
    if reason:
        set_fault(reason)
    log("Sequence paused" + (f": {reason}" if reason else ""))


def start_program(program_path, cfg):
    global active_prog, active_prog_file

    if active_prog is not None:
        set_fault("A sequence is already running -- stop it first")
        return None

    try:
        with open(program_path, "r") as f:
            prog = pantilt_program.load_program(f.read())
    except (OSError, pantilt_program.ProgramError) as e:
        set_fault(f"Cannot start sequence '{program_path}': {e}")
        return None

    if prog["type"] != "single":
        set_fault("Only 'single' sequences are supported in the local tool")
        return None

    #   Apply the program's recorded home position / axis orientation (if
    #   any) before validating against limits
    overrides = pantilt_program.home_orientation_overrides(prog, cfg)
    if overrides:
        cfg = update_settings(overrides)

    reports = pantilt_program.validate_points(prog, cfg)
    if any(r["status"] == "error" for r in reports):
        set_fault(f"Sequence '{program_path}' is invalid for the current limits/home position")
        return None

    set_fault("")
    active_prog = prog
    active_prog_file = program_path
    with _lock:
        live["program_name"] = os.path.basename(program_path)
        live["program_state"] = "running"
        live["program_paused"] = 0
        live["program_next_index"] = 0
        live["steps_total"] = len(prog["points"])
        live["steps_done"] = 0
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
            cfg = update_settings(cmd.get("values", {}))
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
            cfg = update_settings({"heater_config": int(cmd.get("value", qpt.HEATER_OFF))})
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

    load_settings()
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
                run_program_tick(cfg)
        except (ConnectionLost, qpt.QptError, OSError) as e:
            log(f"Connection lost: {e}")
            disconnect()
            retry_started = time.time()
            next_retry = time.time()


def start_worker():
    t = threading.Thread(target=run_forever, daemon=True)
    t.start()
    return t
