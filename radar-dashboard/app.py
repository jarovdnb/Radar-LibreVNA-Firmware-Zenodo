from flask import Flask, session, render_template, request, jsonify, Response, redirect, url_for
from werkzeug.security import check_password_hash
import yaml
import os
from filelock import FileLock
import sys
sys.path.append(os.path.abspath("..")) 
from lib.socket_helper import *

# Set username and password
USERNAME = 'admin'
PASSWORD_HASH = "REDACTED_PASSWORD_HASH"

app = Flask(__name__)
app.secret_key = '3c6d3b49babfc5e2fdf06dc410e84f90'

home_dir = os.path.expanduser("~")
CONFIG_PATH = home_dir + "/config.yaml"
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
    # Voeg headers toe om caching te voorkomen
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
    # session['logged_in'] = True
    return index()

def index():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as file:
                config = yaml.safe_load(file) or {}
        return render_template('index.html', config=config)

@app.route('/save_config', methods=['POST'])
def save_config():
    data = request.json  # De nieuwe data van de POST request
    print(data)
    try:
        # Lees de bestaande configuratie uit het YAML-bestand
        try:
            with FileLock(LOCK_PATH):
                with open(CONFIG_PATH, 'r') as file:
                    current_config = yaml.safe_load(file) or {}
        except FileNotFoundError:
            current_config = {}

        # Gebruik de deep_update-functie i.p.v. .update()
        deep_update(current_config, data)

        # Schrijf de gecombineerde configuratie terug naar het bestand
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'w') as file:
                yaml.dump(current_config, file)

        return jsonify({"status": "success", "message": "Config saved."})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

def deep_update(source, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(source.get(key), dict):
            deep_update(source[key], value)
        else:
            source[key] = value

def load_config():
    with FileLock(LOCK_PATH):
        with open(CONFIG_PATH, 'r') as file:
            return yaml.safe_load(file)

def save_config(config):
    with FileLock(LOCK_PATH):
        with open(CONFIG_PATH, 'w') as file:
            yaml.dump(config, file)

@app.route('/get_config', methods=['GET'])
def get_config():
    config = load_config()
    return jsonify(config)

@app.route('/update_config', methods=['POST'])
def update_config():
    data = request.json
    config = load_config()

    save_config(config)
    return jsonify({'message': 'Config updated'}), 200

# Change the color of the header based on this GET request
@app.route('/get_countdown_vars', methods=['GET'])
def get_countdown_vars():
    countdown_dict = get_local_socket_info()

    return jsonify({'vna_activity': countdown_dict.get("vna_activity", 0), 
                    'vna_ch': countdown_dict.get("vna_countdown_hour", 0),
                    'vna_cm': countdown_dict.get("vna_countdown_minute", 0), 
                    'vna_cs': countdown_dict.get("vna_countdown_second", 0)
                    })

# Run the webserver
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
