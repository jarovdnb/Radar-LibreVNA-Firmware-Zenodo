# Pan-tilt app

Aims the radar with a QuickSet QPT-50 pan-tilt positioner before each measurement. This is a
standalone app, independent of the radar app (`../`, originally by Jarne Van Mulders) that owns
the LibreVNA. The two apps run as separate processes with their own config files, own dashboard
and own systemd units; the only coupling between them is the small `lib/pantilt_config.py` bridge
described below.

## Architecture

- `pantilt.py` — the daemon (own process, own systemd unit, own Unix socket at
  `/tmp/pantilt_socket.sock`). Owns the serial port to the QPT-50, executes measurement programs,
  and polls this app's own config file every 100 ms for commands from `pantilt-dashboard/app_pantilt.py`.
- `pantilt-dashboard/app_pantilt.py` + `pantilt-dashboard/templates/index.html` — a small Flask
  dashboard (HTTP basic auth, same stack as the radar dashboard) for enabling the module,
  jogging/calibrating, and uploading/running measurement programs.
- `lib/qpt.py` — pure QPT-50 wire-protocol driver ("Integrated Controller Protocol", MN00056 Rev
  J). No config or radar knowledge at all.
- `lib/pantilt_program.py` — YAML measurement-program parsing, validation and scheduling. Takes a
  plain `pantilt_cfg` dict; no config.yaml I/O of its own.
- `lib/configuration.py` — this app's own config read/write helpers (`retrieve_yaml_file`,
  `update_yaml_flag`, `update_yaml_flags`, `ensure_yaml_section`), pointed at this app's own
  config file. Same shape as the radar app's equivalent, but a separate file — the two apps do
  not share a config I/O implementation.
- `lib/pantilt_config.py` — **the only file in this app that touches the radar app's config.**
  Everything else in this app only ever reads/writes its own config file. (Not to be confused
  with `lib/configuration.py`, this app's own config I/O, or `~/pantilt_config.yaml`, this app's
  own deployed config file — see the module's docstring for the distinction.)

### The `pantilt_config` bridge

The radar app and this app do not share a config file or any Python import. The sanctioned point of
contact is four functions in `lib/pantilt_config.py`: three read/write a small, fixed set of fields
in the *radar* app's config file, and one checks liveness via controller.py's own status socket
(read-only, never touches its data):

- `take_single_measurement()` — requests one VV+VH sweep (sets the radar's
  `measurement_status.single_measurement = 1`). Fire-and-forget; poll `get_status()` for
  completion.
- `get_status()` — read-only snapshot of the radar's state: whether its config.yaml is reachable,
  whether it's safe to request a measurement right now (`auto_measurement` off and no measurement
  already pending), and whether a previously-requested measurement has finished. Note `reachable`
  only means the *file* could be read — true from initial setup onward regardless of whether
  controller.py has ever run — so it cannot by itself tell "radar app not running" apart from
  "radar app running but idle"; that's what `radar_app_running()` is for.
- `radar_app_running()` — connects to controller.py's own liveness socket
  (`/tmp/streaming_socket.sock`, bound only while its main loop is alive) to answer "is the radar
  app actually running right now". `measure()` calls this first so a genuinely absent radar app
  fails in ~1s instead of only surfacing after the full `measure_timeout_seconds` wait (radar's
  config.yaml existing on disk does not mean anything is alive to service the request).
- `write_angle(pan, tilt)` — writes the antenna's current relative pan/tilt angle into the radar's
  `antenna_position` section, so `librevna.py` (radar side) can keep embedding it in measurement
  filenames. `write_angle(None, None)` clears it (radar interprets a null `antenna_position` as
  "no pan-tilt tag", the equivalent of the module being disabled).

`pantilt.py` calls all three; `pantilt-dashboard/app_pantilt.py` calls `get_status()` to gray out
the calibration/program cards while the radar's own automatic measurements are running, and to
refuse starting a program in that situation (this app cannot disable `auto_measurement` for you —
do that on the radar dashboard first).

### Config paths

Both apps' config paths are read from environment variables, defaulting to today's real deployed
locations so nothing needs to change for this to keep working exactly as before on a single
machine:

| App | Env var | Default |
|---|---|---|
| Radar (this app reads it via `pantilt_config`) | `RADAR_CONFIG_PATH` | `~/config.yaml` |
| This app's own config | `PANTILT_CONFIG_PATH` | `~/pantilt_config.yaml` |

If the two apps are later split into separate containers, pointing both at the same
`RADAR_CONFIG_PATH` (a shared bind-mounted volume) is the entire migration for the bridge — no
code changes needed.

The `config.yaml` in this folder is a **template**, exactly like the radar app's — it gets copied
to `~/pantilt_config.yaml` once during setup and is never touched again by the running system
(see setup step 4 below). It contains two sections:

```yaml
pantilt:
  # settings (port, baud, limits, speeds, ...) + one-shot command flags the
  # dashboard writes and pantilt.py consumes/clears (step_request, move_request,
  # set_home, clear_fault, run_program, pause_program, resume_program, stop_program, ...)
pantilt_status:
  # daemon-owned telemetry, dashboard-read (connected, pan_rel, tilt_rel,
  # pan_abs, tilt_abs, active_program, program_type, fault, heater_state, ...)
```

There is no `mock` mode: `pantilt.py` always talks to real positioner hardware.

## Setup

1. Make sure user `pi` can open serial ports: `sudo usermod -a -G dialout pi` (logout/login
   afterwards).
2. Connect the QPT-50 controller via the USB RS-232 adapter; `pantilt.py` scans
   `/dev/serial/by-id/*` and `/dev/ttyUSB*` automatically.
3. Optional: give the adapter a fixed name with a udev rule
   (`sudo nano /etc/udev/rules.d/52-qpt.rules`), filling in the vendor/product id from `lsusb`:
   ```
   SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="qpt50", MODE:="0666"
   ```
   and set `/dev/qpt50` as the serial port in the dashboard's pan-tilt settings.
4. Copy this folder's `config.yaml` template to `~/pantilt_config.yaml` and fill in the real
   serial port / limits / home position for your setup:
   ```
   cp ~/Radar-LibreVNA-Firmware/pantilt/config.yaml ~/pantilt_config.yaml
   ```
5. Install the required python packages (in addition to the radar app's):
   ```
   sudo python3 -m pip install pyyaml --break-system-packages
   sudo python3 -m pip install filelock --break-system-packages
   sudo python3 -m pip install flask --break-system-packages
   sudo python3 -m pip install pyserial --break-system-packages
   ```
6. Install and start the services:
   ```
   sudo cp ~/Radar-LibreVNA-Firmware/pantilt/pantilt.service /lib/systemd/system/
   sudo cp ~/Radar-LibreVNA-Firmware/pantilt/app_pantilt.service /lib/systemd/system/
   sudo systemctl enable pantilt.service app_pantilt.service
   sudo systemctl start pantilt.service app_pantilt.service
   ```
   With `pantilt.enabled: 0` in `~/pantilt_config.yaml` the daemon just sleeps — the radar app
   works fully standalone regardless of whether this app is running at all.
7. Protocol/driver + program-engine unit tests (no hardware needed):
   ```
   python3 tests/test_qpt_frames.py
   python3 tests/test_pantilt_program.py
   ```

## Internal structure

```
lib/qpt.py             STX…ETX framing, escaping/LRC, Qpt driver class, port discovery (find_qpt)
lib/pantilt_program.py load_program, validate_points/validate_schedule, scheduling math, preview()
lib/configuration.py   this app's own config.yaml I/O (PyYAML + filelock)
lib/pantilt_config.py  the only bridge into the radar app's config.yaml (see above)

pantilt.py:
  stream_data / start_server           Unix socket server (/tmp/pantilt_socket.sock)
  effective_limits / target_allowed    raw<->logical limit enforcement (inversion negates+swaps)
  set_fault / flip                     fault latch + axis-inversion helper
  poll_status / persist_position       live telemetry + local config position cache + pantilt_config.write_angle
  apply_heater_config / apply_positioner_settings
  try_connect / disconnect             serial lifecycle
  wait_move_done / move_abs / settle
  measure                              radar_app_running fast-fail, then take_single_measurement + get_status polling
  run_point                            per-point retry loop
  clear_program / pause_program / load_active_program / start_program
  run_single_series_tick / run_automated_series_tick
  process_commands                     reads pending flags from this app's own config each tick
  main_loop                            top-level scheduler; blanks radar's antenna_position on disable
```

## Cross-cutting concerns worth knowing before touching this code

- **Axis inversion**: `pan_min_abs`/`pan_max_abs`/`tilt_min_abs`/`tilt_max_abs` are always the
  *raw hardware* travel range. Every consumer (`pantilt.py:effective_limits`,
  `pantilt_program.py:relative_limits`) must apply the same raw→logical conversion (`flip`/invert
  negates **and swaps** min/max) or limit enforcement and inversion will silently disagree.
- **Program locking**: while a pan-tilt program is active, the radar's VNA/measurement settings
  are no longer automatically locked from the radar dashboard (that would require this app's
  config to be readable from the radar side, which the bridge deliberately does not allow) —
  don't change VNA sweep settings while a pan-tilt program is running. The reverse direction is
  still enforced: this app's `/save_config` and `/pantilt/run_program` refuse changes/starts
  while the radar's `auto_measurement` is on.
- **No auto-takeover**: starting a program used to automatically disable the radar's
  `auto_measurement`. Since the bridge only has the 3 functions above (none of which can write
  `auto_measurement`), it now refuses to start instead, with a message asking you to disable it on
  the radar dashboard first.
- **Recorded setup backfill**: a program YAML's `home_pan_abs`/`home_tilt_abs`/
  `pan_orientation`/`tilt_orientation` are optional and independently specifiable. Undeclared ones
  are backfilled from the live config on first run (never overwritten later); declared ones are
  applied to the live pan-tilt config *before* validation when the program starts.
