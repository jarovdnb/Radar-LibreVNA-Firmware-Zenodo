# pantilt_local

Local, pan-tilt-only test app for the QPT-50 positioner. No radar/LibreVNA
code — this is for bench-testing the positioner by itself.

- `app.py` — local Flask app with jog controls and a sequence ("program")
  runner. The measurement step of a sequence is **simulated** (a short
  delay with "Taking virtual measurement..." / "Measurement done" log
  messages) instead of triggering a real radar sweep.
- `debug_notebook.ipynb` — one command per cell (connect, get status, turn
  pan/tilt, home, clear faults, get/set heater config, ...) for
  troubleshooting outside the web UI.

Ported from `Radar-LibreVNA-Firmware-Zenodo/pantilt/`, with the radar
coupling (`lib/pantilt_config.py`) removed and the daemon+dashboard
two-process design collapsed into a single Flask process.

## Setup

A dedicated conda environment, `env_pantilt_local` (Python 3.10, matching
`env_wavetrax_local`), already has everything installed — activate it and
you're ready to go:

```
conda activate env_pantilt_local
```

It has `flask`, `pyyaml`, `pyserial`, `jupyter` and `ipykernel` installed,
and its Jupyter kernel is registered as "Python (env_pantilt_local)" so it
shows up in VS Code's notebook kernel picker for `debug_notebook.ipynb`.

To (re)create it from scratch instead:

```
conda create -n env_pantilt_local python=3.10
conda activate env_pantilt_local
pip install -r requirements.txt jupyter ipykernel
python -m ipykernel install --user --name env_pantilt_local --display-name "Python (env_pantilt_local)"
```

## Finding your COM port

Leaving `port` blank in the settings (the default) makes the app probe
every serial port Windows reports until one responds like a QPT-50 — this
usually just works. If you want to pin a specific port, check Windows
Device Manager under "Ports (COM & LPT)", or run the first cell of
`debug_notebook.ipynb`, which prints every candidate port it tried.

## Running the web app

```
python app.py
```

Then open http://127.0.0.1:5001 in a browser. The app is local-only
(binds to `127.0.0.1`, no login) — do not expose it beyond your own
machine.

The app connects to the positioner on startup (retrying every 10s for 5
minutes, then every 30 minutes) and works fine with no hardware attached —
the status panel just shows "Not connected" until one shows up.

### Jogging

Use the step-size dropdown and pan/tilt +/- buttons, or type a relative
pan/tilt target and press Move. "Home" returns to the recorded home
position (0°/0° relative).

### Running a sequence

Sequences reuse the same YAML format as the original pantilt module (only
`type: single` is supported here — a straight-through list of points run
once):

```yaml
schema_version: 1
name: bench-test
type: single
defaults:
  try: 3
points:
  - pan_deg: 10.0
    tilt_deg: 0.0
  - pan_deg: -10.0
    tilt_deg: 0.0
```

Upload the file (or pick a previously-run one from the dropdown), click
Preview to validate it against the current limits/home position, then Run.
Each point moves, settles, then "measures" for
`simulated_measure_seconds` (default 1s, adjustable in Advanced Settings)
before moving to the next point. Pause/Resume/Stop work mid-run. Progress
and per-step log messages show up in the Log panel.

## Debug notebook

Open `debug_notebook.ipynb` (VS Code or `jupyter notebook`). Run the first
cell to connect, then run any other cell independently — get status, turn
pan/tilt ±1°, move to an absolute position, stop, clear faults, get/set
heater config, set max speeds. Run the last cell to close the connection
when you're done.

**`app.py` and the notebook can't use the serial port at the same time** —
stop one before starting the other.
