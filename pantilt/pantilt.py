#   Pan-tilt daemon (MOOG QuickSet QPT-90 / PTCR-96 controller, MN00162 Rev C)
#
#   Long-running service modeled on the radar app's controller.py: polls its
#   OWN config file (~/pantilt_config.yaml) every 100 ms for commands from
#   app_pantilt.py, owns the serial port to the positioner, executes
#   measurement programs and broadcasts live status on a unix socket for the
#   dashboard.
#
#   The daemon NEVER talks to the VNA itself and NEVER reads/writes the
#   radar app's config directly: a measurement is requested through
#   lib.pantilt_config.take_single_measurement() and its completion is
#   observed through lib.pantilt_config.get_status(). That module is the only
#   place in this app that touches the radar app's config file.
#
#   Crash recovery: home position, limits and program progress live in this
#   app's own config file (pantilt / pantilt_status sections). The positioner
#   has absolute position feedback, so after a power cycle no homing is
#   needed and an interrupted program simply continues.

import json
import os
import threading
import time
from datetime import datetime, timezone

from lib.configuration import retrieve_yaml_file, update_yaml_flag, update_yaml_flags, ensure_yaml_section
from lib.socket_helper import bind_local_socket, close_local_socket
from lib import pantilt_config
from lib import qpt90
from lib import pantilt_program

#   Hard caps (safety limits, not a PTCR-96 protocol constraint -- some
#   PTCR-96 platforms support continuous pan rotation): the config limits
#   can never exceed these
PAN_ABS_CAP = 180.0
TILT_ABS_CAP = 90.0

PANTILT_SOCKET_PATH = "/tmp/pantilt_socket.sock"

#   Connection retry ladder: every 10 s for the first 5 minutes, then every 30 minutes
RETRY_FAST_S = 10
RETRY_FAST_WINDOW_S = 300
RETRY_SLOW_S = 1800

#   Defaults, also used to migrate an already-deployed pan-tilt config file
PANTILT_DEFAULTS = {
    "enabled": 0, "port": "", "baud": 9600,
    "pan_min_abs": -180.0, "pan_max_abs": 180.0,
    "tilt_min_abs": -90.0, "tilt_max_abs": 90.0,
    "home_pan_abs": 0.0, "home_tilt_abs": 0.0,
    "pan_invert": 0, "tilt_invert": 0,
    "pan_max_speed": 64, "tilt_max_speed": 64,
    "speed_deg_per_s_estimate": 4.0, "settle_seconds": 2,
    "move_timeout_seconds": 120, "measure_timeout_seconds": 400,
    "min_gap_seconds": 60, "warn_margin_deg": 2.0, "update": 0,
    "step_pan": 0.0, "step_tilt": 0.0, "step_request": 0,
    "move_pan_rel": 0.0, "move_tilt_rel": 0.0, "move_request": 0,
    "set_home": 0, "clear_fault": 0, "measure_request": 0,
    "run_program": "", "pause_program": 0, "resume_program": 0, "stop_program": 0,
    "heater_config": 1,   # 1=off (kept for UI/config compat; qpt90 doesn't
                          # apply it to hardware -- see apply_heater_config)
}
PANTILT_STATUS_DEFAULTS = {
    "connected": 0, "pan_rel": 0.0, "tilt_rel": 0.0, "pan_abs": 0.0, "tilt_abs": 0.0,
    "active_program": "", "program_type": "", "program_next_index": 0,
    "program_paused": 0, "fault": "", "heater_state": 0,
}

#   Daemon state
driver = None                # lib.qpt90.Qpt90 instance while connected
serial_port = None           # the underlying pyserial port
connected_port_name = ""
retry_started = 0.0          # start of the current disconnected period
next_retry = 0.0
was_enabled = True           # tracks pantilt.enabled's previous value, to detect the falling edge

active_prog = None           # parsed program (cache of pantilt_status.active_program)
active_prog_file = ""
auto_schedule = {}           # point index -> next due time (unix s), automated only

#   Live state broadcast on the pantilt socket (written by the main loop only)
live = {
    "pantilt_enabled": 0, "connected": 0, "port": "",
    "pan_rel": 0.0, "tilt_rel": 0.0, "pan_abs": 0.0, "tilt_abs": 0.0,
    "moving": 0, "measuring": 0, "fault": "", "retry_in_s": 0,
    "program_name": "", "program_type": "", "program_state": "",
    "program_progress": "", "next_measurement_utc": "", "next_measurement_in_s": 0,
    "next_pan_rel": 0.0, "next_tilt_rel": 0.0,
    "steps_done": 0, "steps_total": 0, "upcoming_points": [], "estimated_remaining_s": 0,
    "heater_state": 0,
}


class ConnectionLost(Exception):
    pass


# *** *** #
def stream_data(conn):
    with conn:
        try:
            conn.sendall((json.dumps(live) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError) as e:
            print(f"⚠️ Broken connection during transmission: {e}")
        except Exception as e:
            print(f"❌ Unexpected error during transmission: {e}")

def start_server():
    server = bind_local_socket(PANTILT_SOCKET_PATH)
    server.listen(1)

    print(f"🟢 Pan-tilt server luistert op {PANTILT_SOCKET_PATH}")

    try:
        while True:
            conn, _ = server.accept()
            t = threading.Thread(target=stream_data, args=(conn,))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        print("🛑 Pan-tilt server stopped.")
    finally:
        close_local_socket(server, PANTILT_SOCKET_PATH)
# *** *** #


#   Limits & fault helpers

def effective_limits(pantilt_cfg):
    #   pan_min_abs/pan_max_abs describe the RAW (hardware) travel range — a
    #   physical fact that doesn't change just because pan/tilt_invert flips
    #   the software sign convention. Clip to the hard caps in that raw frame,
    #   then convert to the logical frame (pan_abs/tilt_abs) everything else
    #   is validated against: inversion negates AND swaps min/max.
    raw_pan_min = max(float(pantilt_cfg.get("pan_min_abs", -PAN_ABS_CAP)), -PAN_ABS_CAP)
    raw_pan_max = min(float(pantilt_cfg.get("pan_max_abs", PAN_ABS_CAP)), PAN_ABS_CAP)
    raw_tilt_min = max(float(pantilt_cfg.get("tilt_min_abs", -TILT_ABS_CAP)), -TILT_ABS_CAP)
    raw_tilt_max = min(float(pantilt_cfg.get("tilt_max_abs", TILT_ABS_CAP)), TILT_ABS_CAP)

    pan_min, pan_max = (-raw_pan_max, -raw_pan_min) if pantilt_cfg.get("pan_invert", 0) == 1 else (raw_pan_min, raw_pan_max)
    tilt_min, tilt_max = (-raw_tilt_max, -raw_tilt_min) if pantilt_cfg.get("tilt_invert", 0) == 1 else (raw_tilt_min, raw_tilt_max)

    return (pan_min, pan_max, tilt_min, tilt_max)


def target_allowed(pan_abs, tilt_abs, pantilt_cfg):
    pan_min, pan_max, tilt_min, tilt_max = effective_limits(pantilt_cfg)
    return pan_min <= pan_abs <= pan_max and tilt_min <= tilt_abs <= tilt_max


def set_fault(text):
    if live["fault"] != text:
        print(f"⚠️ Pan-tilt fault: {text}" if text else "✅ Pan-tilt fault cleared")
        live["fault"] = text
        update_yaml_flag("pantilt_status", "fault", text)


#   Connection handling

def flip(value, inverted):
    #   Sign convention between our "logical" pan/tilt (what home position,
    #   limits, programs and filenames are expressed in) and the QPT's own raw
    #   angle. Mounting the unit upside-down or reversed flips this mapping
    #   without touching the positioner's own calibration (same approach as
    #   fixed_configurations.polarisation_inverted in librevna.py).
    return -value if inverted else value


def poll_status(pantilt_cfg):
    #   Status request = keep-alive; also refreshes the live position.
    #   Raises ConnectionLost when the serial link is gone.
    try:
        status = driver.get_status()
    except (qpt90.QptError, OSError) as e:
        raise ConnectionLost(str(e))

    pan_deg = flip(status.pan_deg, pantilt_cfg.get("pan_invert", 0) == 1)
    tilt_deg = flip(status.tilt_deg, pantilt_cfg.get("tilt_invert", 0) == 1)

    home_pan = float(pantilt_cfg.get("home_pan_abs", 0.0))
    home_tilt = float(pantilt_cfg.get("home_tilt_abs", 0.0))

    live["pan_abs"] = round(pan_deg, 1)
    live["tilt_abs"] = round(tilt_deg, 1)
    live["pan_rel"] = round(pan_deg - home_pan, 1)
    live["tilt_rel"] = round(tilt_deg - home_tilt, 1)
    live["moving"] = 1 if status.moving else 0

    #   Latched positioner faults (TO/DE/OL) are surfaced, never auto-cleared
    if status.faults and not live["fault"]:
        set_fault("Positioner fault: " + ", ".join(status.faults))

    return status


def persist_position(pantilt_cfg):
    #   Written only when motion has settled (not at poll rate): these are the
    #   angles librevna.py puts in the measurement filenames, so the radar
    #   app's config.yaml needs its own copy via the pantilt_config bridge.
    update_yaml_flags("pantilt_status", {
        "pan_rel": live["pan_rel"], "tilt_rel": live["tilt_rel"],
        "pan_abs": live["pan_abs"], "tilt_abs": live["tilt_abs"],
    })
    pantilt_config.write_angle(live["pan_rel"], live["tilt_rel"])


def apply_heater_config(pantilt_cfg):
    #   The old QPT-50 driver's 97H set/query round-trip (set desired mode,
    #   re-query to confirm the unit actually accepted it) has no equivalent
    #   here yet: qpt90 (PTCR-96) doesn't implement heater control -- the
    #   command's data layout (MN00162 Sec 2.9.7) wasn't available when the
    #   driver was written. Accept the setting so the dashboard's heater
    #   dropdown still works and round-trips through config, but don't send
    #   anything to hardware and don't claim a mode was confirmed.
    update_yaml_flag("pantilt_status", "heater_state", 0)
    live["heater_state"] = 0
    return True


def apply_positioner_settings(pantilt_cfg):
    #   The old QPT-50 driver also set max speeds (99H) and a comm-timeout
    #   hardware backstop (96H, unit halts itself if the daemon dies) here.
    #   qpt90 doesn't implement either command yet (MN00162 Sec 2.9.8/2.9.10
    #   weren't available when the driver was written) -- pan_max_speed/
    #   tilt_max_speed are still accepted and recorded in config for later,
    #   but nothing is sent to hardware, and the comm-timeout safety backstop
    #   is NOT currently in effect on this positioner.
    apply_heater_config(pantilt_cfg)


def try_connect(config):
    global driver, serial_port, connected_port_name

    pantilt_cfg = config.get("pantilt", {})

    connected_port_name, driver = qpt90.find_qpt90(pantilt_cfg.get("port", ""), int(pantilt_cfg.get("baud", 9600)))
    serial_port = driver.ser if driver else None

    if driver is None:
        return False

    try:
        apply_positioner_settings(pantilt_cfg)
        poll_status(pantilt_cfg)
        persist_position(pantilt_cfg)
    except (ConnectionLost, qpt90.QptError, OSError) as e:
        print(f"⚠️ Pan-tilt connection lost during setup: {e}")
        disconnect()
        return False

    live["connected"] = 1
    live["port"] = connected_port_name
    live["retry_in_s"] = 0
    update_yaml_flag("pantilt_status", "connected", 1)
    print(f"✅ Pan-tilt connected on {connected_port_name}")
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
    live["connected"] = 0
    live["port"] = ""
    live["moving"] = 0
    live["heater_state"] = 0
    update_yaml_flag("pantilt_status", "connected", 0)
    update_yaml_flag("pantilt_status", "heater_state", 0)


#   Blocking motion / measurement primitives.
#   They keep polling the positioner (keep-alive) while waiting and abort when
#   the module is disabled. Pause/stop of a program take effect between points.

def wait_move_done(pantilt_cfg):
    #   Returns "done", "aborted" (module disabled) or "fault"
    deadline = time.time() + float(pantilt_cfg.get("move_timeout_seconds", 120))
    last_config_check = 0.0

    while time.time() < deadline:
        time.sleep(0.1)
        status = poll_status(pantilt_cfg)

        if status.faults:
            return "fault"
        if not status.moving:
            persist_position(pantilt_cfg)
            return "done"

        #   React to the module being switched off mid-move
        if time.time() - last_config_check > 1.0:
            last_config_check = time.time()
            config = retrieve_yaml_file()
            if config.get("pantilt", {}).get("enabled", 0) == 0:
                driver.stop()
                return "aborted"

    driver.stop()
    set_fault(f"Move timed out after {pantilt_cfg.get('move_timeout_seconds', 120)} s")
    return "fault"


def move_abs(pan_abs, tilt_abs, pantilt_cfg):
    #   Validated absolute move; returns "done", "aborted" or "fault"
    if not target_allowed(pan_abs, tilt_abs, pantilt_cfg):
        pan_min, pan_max, tilt_min, tilt_max = effective_limits(pantilt_cfg)
        set_fault(f"Move to pan {pan_abs}° / tilt {tilt_abs}° refused: outside absolute limits "
                  f"(pan [{pan_min}°, {pan_max}°], tilt [{tilt_min}°, {tilt_max}°])")
        return "fault"

    raw_pan = flip(pan_abs, pantilt_cfg.get("pan_invert", 0) == 1)
    raw_tilt = flip(tilt_abs, pantilt_cfg.get("tilt_invert", 0) == 1)

    try:
        driver.move_to(raw_pan, raw_tilt)
    except qpt90.QptNak:
        set_fault(f"Positioner rejected move to pan {pan_abs}° / tilt {tilt_abs}°")
        return "fault"
    except (qpt90.QptError, OSError) as e:
        raise ConnectionLost(str(e))

    return wait_move_done(pantilt_cfg)


def settle(pantilt_cfg):
    #   Dwell after a move so vibrations die out before measuring
    until = time.time() + float(pantilt_cfg.get("settle_seconds", 2))
    while time.time() < until:
        time.sleep(0.1)
        poll_status(pantilt_cfg)


def measure(pantilt_cfg):
    #   Trigger one VV+VH sweep on the radar app (via pantilt_config) and wait
    #   for completion. Returns "done", "aborted" (module disabled) or
    #   "radar_unreachable" (radar app absent, or didn't finish in time).
    #   Deliberately NOT "fault": callers treat radar_unreachable as "skip
    #   this point, keep the schedule going" rather than pausing for a human
    #   -- an absent radar app is an expected, self-healing field condition,
    #   unlike an actual positioner fault (which never reaches this function;
    #   run_point() returns early on those, before measure() is called).

    #   Fail fast if controller.py isn't actually running: radar's config.yaml
    #   being readable (get_status()["reachable"]) does NOT mean anything is
    #   alive to service the request -- without this check, the only way to
    #   discover that is the full measure_timeout_seconds wait below (~7 min
    #   by default) for a single_measurement flag nothing will ever clear.
    if not pantilt_config.radar_app_running():
        set_fault("Radar app is not running/reachable -- skipping this point")
        return "radar_unreachable"

    #   Phase 1 -- wait up to 60s for the radar app to report it's safe to
    #   request. Fails open: if radar's config can't be reached at all,
    #   safe_to_request reads True, so this exits immediately rather than
    #   blocking on it -- the phase-2 deadline below is what surfaces
    #   radar_unreachable if radar is genuinely stuck.
    deadline = time.time() + 60
    status = pantilt_config.get_status()
    while time.time() < deadline and not status["safe_to_request"]:
        poll_status(pantilt_cfg)
        time.sleep(0.5)
        status = pantilt_config.get_status()

    #   Phase 2 -- request, then wait for completion
    pantilt_config.take_single_measurement()

    deadline = time.time() + float(pantilt_cfg.get("measure_timeout_seconds", 400))
    while time.time() < deadline:
        time.sleep(0.5)
        poll_status(pantilt_cfg)

        status = pantilt_config.get_status()
        #   A transient unreachable blip must not be misread as "done" --
        #   only trust single_measurement_pending == False when reachable.
        if status["reachable"] and not status["single_measurement_pending"]:
            if live["fault"]:
                #   A sweep just actually completed, so any radar_unreachable
                #   fault left over from an earlier point is stale now.
                set_fault("")
            return "done"
        if retrieve_yaml_file().get("pantilt", {}).get("enabled", 0) == 0:
            #   The running sweep finishes on its own; the daemon stops waiting
            return "aborted"

    set_fault(f"Measurement did not complete within {pantilt_cfg.get('measure_timeout_seconds', 400)} s "
              "(is the radar app running and reachable?) -- skipping this point")
    return "radar_unreachable"


def run_point(point, pantilt_cfg, tries):
    #   Move to a program point and measure there, both with retries.
    #   Returns "done", "aborted", "fault" (move/positioner problem -- from
    #   move_abs, before measure() is ever reached) or "radar_unreachable"
    #   (from measure(); see its docstring for why that's kept distinct).
    pan_abs = float(pantilt_cfg.get("home_pan_abs", 0.0)) + float(point["pan_deg"])
    tilt_abs = float(pantilt_cfg.get("home_tilt_abs", 0.0)) + float(point["tilt_deg"])

    for attempt in range(tries):
        if attempt > 0:
            #   A latched fault blocks motion until it is reset
            driver.clear_faults()
        result = move_abs(pan_abs, tilt_abs, pantilt_cfg)
        if result != "fault":
            break
        print(f"⚠️ Move attempt {attempt + 1}/{tries} failed")
    if result != "done":
        return result

    settle(pantilt_cfg)

    for attempt in range(tries):
        result = measure(pantilt_cfg)
        if result not in ("fault", "radar_unreachable"):
            break
        print(f"⚠️ Measurement attempt {attempt + 1}/{tries} failed ({result})")
    return result


#   Program handling

def clear_program():
    global active_prog, active_prog_file, auto_schedule
    active_prog = None
    active_prog_file = ""
    auto_schedule = {}
    update_yaml_flags("pantilt_status", {"active_program": "", "program_type": "",
                                         "program_next_index": 0, "program_paused": 0})


def pause_program(reason):
    update_yaml_flag("pantilt_status", "program_paused", 1)
    if reason:
        set_fault(reason)


def load_active_program(config):
    #   Cache the parsed program; (re)load and re-validate when the active file
    #   changes (daemon start, run_program, or after a limits/home change)
    global active_prog, active_prog_file, auto_schedule

    program_file = config.get("pantilt_status", {}).get("active_program", "")
    if not program_file:
        active_prog = None
        active_prog_file = ""
        return None

    if program_file == active_prog_file:
        return active_prog

    active_prog = None
    active_prog_file = program_file
    auto_schedule = {}

    try:
        with open(program_file, "r") as f:
            prog = pantilt_program.load_program(f.read())
    except (OSError, pantilt_program.ProgramError) as e:
        pause_program(f"Cannot load program '{program_file}': {e}")
        return None

    #   Recovering an already-running program (daemon restart): re-apply its
    #   recorded home/orientation the same way a fresh start would
    pantilt_cfg = config.get("pantilt", {})
    overrides = pantilt_program.home_orientation_overrides(prog, pantilt_cfg)
    if overrides:
        update_yaml_flags("pantilt", overrides)
        pantilt_cfg = dict(pantilt_cfg, **overrides)

    reports = pantilt_program.validate_points(prog, pantilt_cfg)
    conflicts = pantilt_program.validate_schedule(prog, pantilt_cfg)
    if any(r["status"] == "error" for r in reports) or conflicts:
        pause_program(f"Program '{program_file}' is invalid for the current limits/home position")
        return None

    #   A pan-tilt program and the radar's own automatic measurements must
    #   never run together. We can no longer disable auto_measurement for the
    #   user (the pantilt_config bridge has no function that writes it) --
    #   refuse instead and ask for a manual step on the radar dashboard.
    if pantilt_config.get_status()["auto_measurement"]:
        pause_program("The radar's automatic measurements are running; stop them on the radar "
                      "dashboard before this program can proceed")
        return None

    if prog["type"] == "automated":
        now_s = int(time.time())
        ref_epoch = prog["initial_startdate_epoch"]
        auto_schedule = {i: pantilt_program.next_occurrence(p, now_s, ref_epoch) for i, p in enumerate(prog["points"])}

    active_prog = prog
    print(f"▶️ Program loaded: {program_file} ({prog['type']}, {len(prog['points'])} points)")
    return active_prog


def estimate_remaining_seconds(prog, index, pan_rel, tilt_rel, pantilt_cfg):
    #   Same distance/settle/measure model as pantilt_program.summarize(), but
    #   starting from the current live position instead of home, over only
    #   the points not yet done
    speed = max(float(pantilt_cfg.get("speed_deg_per_s_estimate", 4.0)), 0.1)
    settle = float(pantilt_cfg.get("settle_seconds", 2))
    measure_estimate = pantilt_program.MEASURE_SECONDS_ESTIMATE

    duration = 0.0
    pan, tilt = pan_rel, tilt_rel
    for point in prog["points"][index:]:
        duration += max(abs(point["pan_deg"] - pan), abs(point["tilt_deg"] - tilt)) / speed
        duration += settle + measure_estimate
        pan, tilt = point["pan_deg"], point["tilt_deg"]
    return duration


def run_single_series_tick(config):
    #   Execute ONE point per main-loop pass, so pause/stop/disable are handled
    #   between points by the normal loop
    pantilt_cfg = config.get("pantilt", {})
    prog = active_prog
    index = int(config.get("pantilt_status", {}).get("program_next_index", 0))

    if index >= len(prog["points"]):
        print("✅ Single series finished")
        clear_program()
        return

    live["program_progress"] = f"{index + 1}/{len(prog['points'])}"
    live["steps_done"] = index
    live["steps_total"] = len(prog["points"])
    live["upcoming_points"] = [{"pan_rel": p["pan_deg"], "tilt_rel": p["tilt_deg"]}
                               for p in prog["points"][index:index + 3]]
    live["estimated_remaining_s"] = int(estimate_remaining_seconds(
        prog, index, live["pan_rel"], live["tilt_rel"], pantilt_cfg))

    result = run_point(prog["points"][index], pantilt_cfg, prog["defaults"]["try"])

    if result == "radar_unreachable":
        print(f"⚠️ Point {index + 1}: radar unreachable, skipping to the next point")

    if result in ("done", "radar_unreachable"):
        update_yaml_flag("pantilt_status", "program_next_index", index + 1)
        if index + 1 >= len(prog["points"]):
            print("✅ Single series finished")
            clear_program()
    elif result == "fault":
        pause_program(live["fault"] or f"Point {index + 1} failed")


def run_automated_series_tick(config):
    #   Fire the earliest due point; between occurrences the loop just idles
    pantilt_cfg = config.get("pantilt", {})
    prog = active_prog
    now_s = time.time()

    if not auto_schedule:
        return

    index = min(auto_schedule, key=auto_schedule.get)
    due = auto_schedule[index]
    live["next_measurement_utc"] = datetime.fromtimestamp(due, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    live["next_measurement_in_s"] = max(0, int(due - now_s))
    live["next_pan_rel"] = prog["points"][index]["pan_deg"]
    live["next_tilt_rel"] = prog["points"][index]["tilt_deg"]

    if now_s < due:
        return

    point = prog["points"][index]
    period = point["repeated_minutes"] * 60
    lateness = now_s - due

    ref_epoch = prog["initial_startdate_epoch"]

    if lateness > min(float(pantilt_cfg.get("min_gap_seconds", 60)), period / 2):
        #   Too late (previous point overran, pause, reboot): skip this occurrence
        print(f"⚠️ Skipping point {index + 1}: {int(lateness)} s late")
        auto_schedule[index] = pantilt_program.next_occurrence(point, int(now_s), ref_epoch)
        return

    live["program_progress"] = f"point {index + 1}/{len(prog['points'])}"
    result = run_point(point, pantilt_cfg, prog["defaults"]["try"])
    auto_schedule[index] = pantilt_program.next_occurrence(point, int(time.time()), ref_epoch)

    if result == "radar_unreachable":
        print(f"⚠️ Point {index + 1}: radar unreachable, will retry at the next scheduled occurrence")
    elif result == "fault":
        pause_program(live["fault"] or f"Point {index + 1} failed")


#   Dashboard one-shot commands (writer sets the flag, the daemon executes and clears it)

def process_commands(config):
    pantilt_cfg = config.get("pantilt", {})
    status_cfg = config.get("pantilt_status", {})
    program_active = status_cfg.get("active_program", "") != ""
    program_paused = status_cfg.get("program_paused", 0) == 1

    #   Settings changed (limits, speeds, ...)
    if pantilt_cfg.get("update", 0) == 1:
        update_yaml_flag("pantilt", "update", 0)
        apply_positioner_settings(pantilt_cfg)

        if not target_allowed(live["pan_abs"], live["tilt_abs"], pantilt_cfg):
            set_fault("Current position is outside the new absolute limits — move back inside them")
        if program_active:
            #   Force re-validation of the active program against the new limits
            global active_prog_file
            active_prog_file = ""

    if pantilt_cfg.get("clear_fault", 0) == 1:
        update_yaml_flag("pantilt", "clear_fault", 0)
        driver.clear_faults()
        set_fault("")

    if pantilt_cfg.get("stop_program", 0) == 1:
        update_yaml_flag("pantilt", "stop_program", 0)
        if program_active:
            driver.stop()
            clear_program()
            print("🛑 Program stopped")
        return

    if pantilt_cfg.get("pause_program", 0) == 1:
        update_yaml_flag("pantilt", "pause_program", 0)
        if program_active and not program_paused:
            pause_program("")
            print("⏸️ Program paused")

    if pantilt_cfg.get("resume_program", 0) == 1:
        update_yaml_flag("pantilt", "resume_program", 0)
        if program_active and program_paused:
            driver.clear_faults()
            set_fault("")
            update_yaml_flag("pantilt_status", "program_paused", 0)
            print("▶️ Program resumed")

    if pantilt_cfg.get("run_program", ""):
        program_file = pantilt_cfg["run_program"]
        update_yaml_flag("pantilt", "run_program", "")
        start_program(program_file, config)
        return

    #   Manual (calibration) commands: not while a program is actively running
    if program_active and not program_paused:
        return

    if pantilt_cfg.get("set_home", 0) == 1:
        update_yaml_flag("pantilt", "set_home", 0)
        if program_active:
            set_fault("Stop the active program before changing the home position")
        elif live["moving"]:
            set_fault("Cannot set the home position while moving")
        else:
            update_yaml_flags("pantilt", {"home_pan_abs": live["pan_abs"], "home_tilt_abs": live["tilt_abs"]})
            print(f"🏠 Home position set to pan {live['pan_abs']}° / tilt {live['tilt_abs']}° (absolute)")
            poll_status(retrieve_yaml_file().get("pantilt", {}))
            persist_position(pantilt_cfg)

    if pantilt_cfg.get("step_request", 0) == 1:
        update_yaml_flag("pantilt", "step_request", 0)
        move_abs(live["pan_abs"] + float(pantilt_cfg.get("step_pan", 0.0)),
                 live["tilt_abs"] + float(pantilt_cfg.get("step_tilt", 0.0)), pantilt_cfg)

    if pantilt_cfg.get("move_request", 0) == 1:
        update_yaml_flag("pantilt", "move_request", 0)
        move_abs(float(pantilt_cfg.get("home_pan_abs", 0.0)) + float(pantilt_cfg.get("move_pan_rel", 0.0)),
                 float(pantilt_cfg.get("home_tilt_abs", 0.0)) + float(pantilt_cfg.get("move_tilt_rel", 0.0)),
                 pantilt_cfg)

    if pantilt_cfg.get("measure_request", 0) == 1:
        update_yaml_flag("pantilt", "measure_request", 0)
        if live["moving"]:
            set_fault("Cannot measure while moving")
        else:
            #   Same blocking primitive a program uses per point (measure_timeout_seconds);
            #   set_fault/"aborted" handling for a bad outcome already lives inside measure().
            live["measuring"] = 1
            result = measure(pantilt_cfg)
            live["measuring"] = 0
            if result == "done":
                print(f"✅ Manual measurement done at pan {live['pan_abs']}° / tilt {live['tilt_abs']}°")


def start_program(program_file, config):
    #   Validate before activating; refuse invalid programs with a clear fault
    pantilt_cfg = config.get("pantilt", {})
    try:
        with open(program_file, "r") as f:
            prog = pantilt_program.load_program(f.read())
    except (OSError, pantilt_program.ProgramError) as e:
        set_fault(f"Cannot start program '{program_file}': {e}")
        return None

    #   Apply the program's recorded home position / axis orientation (if any)
    #   BEFORE validating against limits: the program was designed for that
    #   setup, and the dashboard has already warned the user this would happen
    overrides = pantilt_program.home_orientation_overrides(prog, pantilt_cfg)
    if overrides:
        update_yaml_flags("pantilt", overrides)
        pantilt_cfg = dict(pantilt_cfg, **overrides)

    reports = pantilt_program.validate_points(prog, pantilt_cfg)
    conflicts = pantilt_program.validate_schedule(prog, pantilt_cfg)
    if any(r["status"] == "error" for r in reports) or conflicts:
        set_fault(f"Program '{program_file}' is invalid for the current limits/home position")
        return None

    #   Never together with the radar's own automatic measurements. We can no
    #   longer disable auto_measurement for the user (see load_active_program) --
    #   refuse instead.
    if pantilt_config.get_status()["auto_measurement"]:
        set_fault("Cannot start program: the radar's automatic measurements are running. "
                  "Stop them on the radar dashboard first.")
        return None

    set_fault("")
    update_yaml_flags("pantilt_status", {"active_program": program_file, "program_type": prog["type"],
                                         "program_next_index": 0, "program_paused": 0})
    return prog["type"]


def update_live_program(config):
    status_cfg = config.get("pantilt_status", {})
    live["program_name"] = os.path.basename(status_cfg.get("active_program", ""))
    live["program_type"] = status_cfg.get("program_type", "")
    live["fault"] = status_cfg.get("fault", live["fault"])

    if not status_cfg.get("active_program", ""):
        live["program_state"] = ""
        live["program_progress"] = ""
        live["next_measurement_utc"] = ""
        live["next_measurement_in_s"] = 0
        live["next_pan_rel"] = 0.0
        live["next_tilt_rel"] = 0.0
        live["steps_done"] = 0
        live["steps_total"] = 0
        live["upcoming_points"] = []
        live["estimated_remaining_s"] = 0
    elif status_cfg.get("program_paused", 0) == 1:
        live["program_state"] = "paused"
    else:
        live["program_state"] = "running"


def main_loop():
    global retry_started, next_retry, was_enabled

    #   Migrate an already-deployed pan-tilt config file
    ensure_yaml_section("pantilt", PANTILT_DEFAULTS)
    ensure_yaml_section("pantilt_status", PANTILT_STATUS_DEFAULTS)
    update_yaml_flag("pantilt_status", "connected", 0)

    retry_started = time.time()
    next_retry = time.time()
    #   Seeded True so daemon startup while already disabled also counts as a
    #   falling edge and blanks radar's antenna_position below.
    was_enabled = True

    while True:

        time.sleep(0.1)

        config = retrieve_yaml_file()
        pantilt_cfg = config.get("pantilt", {})
        enabled = pantilt_cfg.get("enabled", 0) == 1
        live["pantilt_enabled"] = 1 if enabled else 0

        #   Module switched off: radar works standalone, program state is kept
        if not enabled:
            if driver is not None:
                print("🔌 Pan-tilt module disabled")
                disconnect(stop_motion=True)
            if was_enabled:
                pantilt_config.write_angle(None, None)   # blank radar's antenna_position
                was_enabled = False
            retry_started = time.time()
            next_retry = time.time()
            continue

        was_enabled = True

        #   Not connected: retry ladder (10 s for 5 minutes, then every 30 minutes)
        if driver is None:
            live["retry_in_s"] = max(0, int(next_retry - time.time()))
            if time.time() < next_retry:
                continue
            if not try_connect(config):
                interval = RETRY_FAST_S if time.time() - retry_started < RETRY_FAST_WINDOW_S else RETRY_SLOW_S
                next_retry = time.time() + interval
                print(f"🔎 Pan-tilt positioner not found, retrying in {interval} s")
            continue

        #   Connected
        try:
            poll_status(pantilt_cfg)
            update_live_program(config)
            process_commands(config)

            #   Program execution (pause/stop/disable are handled between points)
            config = retrieve_yaml_file()
            status_cfg = config.get("pantilt_status", {})
            if status_cfg.get("active_program", "") and status_cfg.get("program_paused", 0) == 0:
                prog = load_active_program(config)
                if prog is not None:
                    if prog["type"] == "single":
                        run_single_series_tick(config)
                    else:
                        run_automated_series_tick(config)

        except (ConnectionLost, qpt90.QptError, OSError) as e:
            print(f"❌ Pan-tilt connection lost: {e}")
            disconnect()
            retry_started = time.time()
            next_retry = time.time()


if __name__ == "__main__":

    threading.Thread(target=start_server, daemon=True).start()

    main_loop()
