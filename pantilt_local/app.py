#   Local pan-tilt-only web app: jog the QPT-50 positioner and run
#   move-only sequences (measurement is simulated). No radar/LibreVNA code,
#   no auth -- intended to run on localhost only.
#
#   Usage:
#       pip install -r requirements.txt
#       python app.py
#       open http://127.0.0.1:5001

import os
import re
from datetime import datetime

from flask import Flask, jsonify, render_template, request, Response

import controller
from lib import pantilt_program

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(controller.get_live())


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(controller.get_settings())


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    values = request.get_json(force=True) or {}
    controller.enqueue_command({"type": "update_settings", "values": values})
    return jsonify({"status": "success"})


@app.route("/api/jog", methods=["POST"])
def api_jog():
    data = request.get_json(force=True) or {}
    controller.enqueue_command({"type": "jog", "pan": data.get("pan", 0.0), "tilt": data.get("tilt", 0.0)})
    return jsonify({"status": "success"})


@app.route("/api/move", methods=["POST"])
def api_move():
    data = request.get_json(force=True) or {}
    controller.enqueue_command({"type": "move_rel", "pan_rel": data.get("pan_rel", 0.0),
                                 "tilt_rel": data.get("tilt_rel", 0.0)})
    return jsonify({"status": "success"})


@app.route("/api/home", methods=["POST"])
def api_home():
    controller.enqueue_command({"type": "set_home"})
    return jsonify({"status": "success"})


@app.route("/api/clear_fault", methods=["POST"])
def api_clear_fault():
    controller.enqueue_command({"type": "clear_fault"})
    return jsonify({"status": "success"})


@app.route("/api/heater", methods=["POST"])
def api_heater():
    data = request.get_json(force=True) or {}
    controller.enqueue_command({"type": "set_heater", "value": data.get("value", 1)})
    return jsonify({"status": "success"})


@app.route("/api/measure", methods=["POST"])
def api_measure():
    controller.enqueue_command({"type": "manual_measure"})
    return jsonify({"status": "success"})


#   Sequence ("program") endpoints -- reuses the pantilt_program YAML schema,
#   single-series only

@app.route("/api/program/preview", methods=["POST"])
def api_program_preview():
    text = request.get_data(as_text=True)
    cfg = controller.get_settings()
    try:
        result = pantilt_program.preview(text, cfg)
    except pantilt_program.ProgramError as e:
        return jsonify({"status": "error", "message": str(e)})

    if result["summary"]["type"] != "single":
        result["valid"] = False
        result.setdefault("home_warnings", []).append(
            "Only 'single' sequences are supported in the local tool")
    return jsonify(result)


@app.route("/api/program/run", methods=["POST"])
def api_program_run():
    text = request.get_data(as_text=True)
    cfg = controller.get_settings()

    try:
        result = pantilt_program.preview(text, cfg)
    except pantilt_program.ProgramError as e:
        return jsonify({"status": "error", "message": str(e)})

    if result["summary"]["type"] != "single":
        return jsonify({"status": "error", "message": "Only 'single' sequences are supported in the local tool"})
    if not result["valid"]:
        return jsonify({"status": "error", "message": "Sequence is invalid, fix the errors first."})

    #   Never trust the program name as a path
    name = re.sub(r"[^A-Za-z0-9_-]", "", result["summary"]["name"]) or "program"
    filename = f"{name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.yaml"

    prog = pantilt_program.load_program(text)
    pantilt_program.backfill_recorded_setup(prog, cfg)
    text_to_save = pantilt_program.dump_program(prog)

    program_path = os.path.join(controller.PROGRAMS_DIR, filename)
    with open(program_path, "w") as f:
        f.write(text_to_save)

    controller.enqueue_command({"type": "run_program", "path": program_path})
    return jsonify({"status": "success", "message": f"Sequence saved as {filename} and started."})


@app.route("/api/program/pause", methods=["POST"])
def api_program_pause():
    controller.enqueue_command({"type": "pause_program"})
    return jsonify({"status": "success"})


@app.route("/api/program/resume", methods=["POST"])
def api_program_resume():
    controller.enqueue_command({"type": "resume_program"})
    return jsonify({"status": "success"})


@app.route("/api/program/stop", methods=["POST"])
def api_program_stop():
    controller.enqueue_command({"type": "stop_program"})
    return jsonify({"status": "success"})


@app.route("/api/program/list", methods=["GET"])
def api_program_list():
    if not os.path.isdir(controller.PROGRAMS_DIR):
        return jsonify([])
    files = sorted(f for f in os.listdir(controller.PROGRAMS_DIR) if f.lower().endswith((".yaml", ".yml")))
    return jsonify(files)


@app.route("/api/program/content", methods=["GET"])
def api_program_content():
    name = os.path.basename(request.args.get("name", ""))
    program_path = os.path.join(controller.PROGRAMS_DIR, name)
    if not name or not os.path.isfile(program_path):
        return jsonify({"status": "error", "message": "Unknown sequence file"}), 404
    with open(program_path, "r") as f:
        return Response(f.read(), mimetype="text/plain")


if __name__ == "__main__":
    controller.start_worker()
    app.run(host="127.0.0.1", port=5001, debug=False)
