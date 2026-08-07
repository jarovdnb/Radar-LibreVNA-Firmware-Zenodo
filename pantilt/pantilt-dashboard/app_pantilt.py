from flask import Flask, render_template, request, jsonify, Response
from werkzeug.security import check_password_hash
import yaml
import os
import re
from datetime import datetime
from pathlib import Path
from filelock import FileLock
import sys
sys.path.append(os.path.abspath(".."))
from lib.socket_helper import get_pantilt_socket_info
from lib import pantilt_program
from lib import pantilt_config

# Set username and password
USERNAME = 'admin'
PASSWORD_HASH = "REDACTED_PASSWORD_HASH"

app = Flask(__name__)
app.secret_key = 'REDACTED_SECRET_KEY'

home_dir = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("PANTILT_CONFIG_PATH", home_dir + "/pantilt_config.yaml")
LOCK_PATH = CONFIG_PATH + ".lock"

def check_auth(username, password):
    """Check if the provided username and password are correct"""
    return username == USERNAME and check_password_hash(PASSWORD_HASH, password)

def authenticate():
    """Send a 401 response to trigger basic auth"""
    response = Response(
        'Access denied.\n'
        'You must provide valid credentials.', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

def requires_auth(f):
    """Decorator to require authentication for a route"""
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

@app.route('/')
@requires_auth
def protected():
    return index()

def index():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as file:
                config = yaml.safe_load(file) or {}
        return render_template('index.html', config=config)

def load_config():
    with FileLock(LOCK_PATH):
        with open(CONFIG_PATH, 'r') as file:
            return yaml.safe_load(file)

def write_config(config):
    with FileLock(LOCK_PATH):
        with open(CONFIG_PATH, 'w') as file:
            yaml.dump(config, file)

def deep_update(source, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(source.get(key), dict):
            deep_update(source[key], value)
        else:
            source[key] = value

def merge_config(overrides):
    # Read-modify-write under ONE lock so concurrent flag updates from
    # pantilt.py are never lost
    with FileLock(LOCK_PATH):
        try:
            with open(CONFIG_PATH, 'r') as file:
                current_config = yaml.safe_load(file) or {}
        except FileNotFoundError:
            current_config = {}

        deep_update(current_config, overrides)

        with open(CONFIG_PATH, 'w') as file:
            yaml.dump(current_config, file)

@app.route('/save_config', methods=['POST'])
def save_config():
    data = request.json
    try:
        # The radar's own automatic measurements run at a fixed position,
        # without pan-tilt. Manually jogging/reconfiguring the pan-tilt
        # positioner while that's active would silently change what
        # direction is being measured, so pan-tilt settings/commands are
        # locked until it's disabled on the radar dashboard (starting a
        # pan-tilt *program* is unaffected: it refuses to start instead,
        # see /pantilt/run_program).
        if "pantilt" in data.keys():
            if pantilt_config.get_status()["auto_measurement"]:
                return jsonify({"status": "error",
                                 "message": "Automatic measurements (without pan-tilt) are running on the "
                                            "radar. Pan-tilt settings are locked until they're disabled there."})

        merge_config(data)

        return jsonify({"status": "success", "message": "Config saved."})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/get_config', methods=['GET'])
def get_config():
    config = load_config()
    return jsonify(config)

# Uploaded program files are stored in pantilt/programs (pantilt.py's working directory)
PROGRAMS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "programs"))

# Live pan-tilt state, streamed by pantilt.py on its own unix socket, merged
# with a read-only snapshot of the radar app's state via the pantilt_config bridge
@app.route('/get_pantilt_vars', methods=['GET'])
def get_pantilt_vars():
    data = get_pantilt_socket_info()
    radar_status = pantilt_config.get_status()
    data['radar_auto_measurement'] = radar_status["auto_measurement"]
    data['radar_reachable'] = radar_status["reachable"]
    return jsonify(data)

# Validate an uploaded program file and return the overview; nothing is saved
@app.route('/pantilt/preview_program', methods=['POST'])
def pantilt_preview_program():
    text = request.get_data(as_text=True)
    config = load_config() or {}

    try:
        return jsonify(pantilt_program.preview(text, config.get('pantilt', {})))
    except pantilt_program.ProgramError as e:
        return jsonify({"status": "error", "message": str(e)})

# Re-validate, save the program with a timestamp suffix and hand it to pantilt.py.
@app.route('/pantilt/run_program', methods=['POST'])
def pantilt_run_program():
    text = request.get_data(as_text=True)
    config = load_config() or {}
    pantilt_cfg = config.get('pantilt', {})

    # No auto-takeover from the dashboard: the radar's own automatic
    # measurements must be explicitly stopped first, on the radar dashboard --
    # this app has no way to disable them itself (see pantilt/README.md for
    # why the bridge to the radar app is read-only except for the 3
    # pantilt_config functions).
    if pantilt_config.get_status()["auto_measurement"]:
        return jsonify({"status": "error",
                         "message": "Automatic measurements (without pan-tilt) are running on the radar. "
                                    "Stop them there first before starting a pan-tilt program."})

    try:
        result = pantilt_program.preview(text, pantilt_cfg)
    except pantilt_program.ProgramError as e:
        return jsonify({"status": "error", "message": str(e)})

    if not result["valid"]:
        return jsonify({"status": "error", "message": "Program is invalid, fix the errors first."})

    # Never trust the program name as a path
    name = re.sub(r'[^A-Za-z0-9_-]', '', result["summary"]["name"]) or "program"
    filename = f"{name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.yaml"

    # Backfill whatever setup fields the upload didn't declare, from the
    # current config, so the saved copy fully documents what it ran under
    # and a later re-run reproduces the exact same conditions.
    prog = pantilt_program.load_program(text)
    pantilt_program.backfill_recorded_setup(prog, pantilt_cfg)
    text_to_save = pantilt_program.dump_program(prog)

    Path(PROGRAMS_DIR).mkdir(parents=True, exist_ok=True)
    program_path = os.path.join(PROGRAMS_DIR, filename)
    with open(program_path, 'w') as file:
        file.write(text_to_save)

    merge_config({"pantilt": {"run_program": program_path}})

    return jsonify({"status": "success", "message": f"Program saved as {filename} and started."})

# List previously saved/uploaded programs, for the dashboard's file dropdown
@app.route('/pantilt/list_programs', methods=['GET'])
def pantilt_list_programs():
    if not os.path.isdir(PROGRAMS_DIR):
        return jsonify([])
    files = sorted(f for f in os.listdir(PROGRAMS_DIR) if f.lower().endswith(('.yaml', '.yml')))
    return jsonify(files)

# Serve the raw contents of a saved program by name (basename only, no path traversal)
@app.route('/pantilt/program_content', methods=['GET'])
def pantilt_program_content():
    name = os.path.basename(request.args.get('name', ''))
    program_path = os.path.join(PROGRAMS_DIR, name)
    if not name or not os.path.isfile(program_path):
        return jsonify({"status": "error", "message": "Unknown program file"}), 404
    with open(program_path, 'r') as file:
        return Response(file.read(), mimetype='text/plain')

# Run the webserver
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
