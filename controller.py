import subprocess
import time
from datetime import datetime, timedelta, timezone
import os
from lib.scheduler import Scheduler
from filelock import FileLock
from lib.influxdb import *
from lib.configuration import *

import socket
import threading
import json

# Booleans
socket_server_started = False

# Config
vna_script_path = "librevna.py"
system_script_path = "system.py"

# Initial start time
start_time = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# VNA initial interval
vna_interval = timedelta(hours=0, minutes=1, seconds=0)
temp_interval = timedelta(hours=0, minutes=1, seconds=0)

# Countdown variables in YAML
vna_countdown_vars = ["vna_countdown_hour", "vna_countdown_minute", "vna_countdown_second"]
temp_countdown_vars = ["temp_countdown_hour", "temp_countdown_minute", "temp_countdown_second"]

# Init
vna_scheduler = Scheduler("LibreVNA", vna_script_path, start_time, vna_interval, vna_countdown_vars, True)
system_scheduler = Scheduler("System", system_script_path, start_time, temp_interval, temp_countdown_vars, False)

def wait_for_network(host="8.8.8.8", port=53, timeout=3, retry_interval=5, max_wait=300):
    """
    Wacht tot netwerk beschikbaar is of tot max_wait seconden verstreken zijn.
    """
    start_time = time.time()
    
    while True:
        try:
            socket.setdefaulttimeout(timeout)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((host, port))
            print("✅ Netwerkverbinding is beschikbaar.")
            return True
        except socket.error:
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                print("❌ Timeout: netwerk niet beschikbaar binnen de toegestane tijd.")
                return False
            print(f"⏳ Geen verbinding. Probeer opnieuw in {retry_interval} seconden...")
            time.sleep(retry_interval)

def update_auto_vna_timer_settings(settings):
    start_time = datetime(settings["init_year"], settings["init_month"], settings["init_day"], settings["init_hour"], settings["init_minute"], settings["init_second"], tzinfo=timezone.utc)
    vna_interval = timedelta(hours=settings["interval_hour"], minutes=settings["interval_minute"], seconds=settings["interval_second"])

    vna_scheduler.update_parameters(start_time, vna_interval)


def update_system_timer_settings(settings):
    temp_interval = timedelta(hours=settings["temp_interval_hour"], minutes=settings["temp_interval_minute"], seconds=settings["temp_interval_second"])

    system_scheduler.update_parameters(start_time, temp_interval)


def check_device_mode(settings):
    if settings.get("measurement_status", {}).get("device_mode", 0) == 'calibration':
        if settings.get("configurations", {}).get("power", 0) > -26:
            
            # Update VNA output power
            update_yaml_flag("configurations","power", -26)

            time.sleep(0.1)

            # For debugging
            print("Calibration Mode: Power settings changed to -26dBm!")

            # Send new data to influxdb
            update_yaml_flag("configurations","update", 1)
            # send_configurations(settings, "[New VNA configurations]")


# *** *** #
SOCKET_PATH = "/tmp/streaming_socket.sock"

def stream_data(conn):
    with conn:
        try:
            data = {
                "vna_activity": vna_scheduler.activity,
                vna_countdown_vars[0]: vna_scheduler.countdown_remaining // 3600,
                vna_countdown_vars[1]: (vna_scheduler.countdown_remaining % 3600) // 60,
                vna_countdown_vars[2]: vna_scheduler.countdown_remaining % 60
            }
            conn.sendall((json.dumps(data) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError) as e:
            print(f"⚠️ Broken connection during transmission: {e}")
        except Exception as e:
            print(f"❌ Unexpected error during transmission: {e}")

def start_server():
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)

    print(f"🟢 Server luistert op {SOCKET_PATH}")

    try:
        while True:
            conn, _ = server.accept()
            # print("✅ Client connected")
            t = threading.Thread(target=stream_data, args=(conn,))
            t.daemon = True
            t.start()
    except KeyboardInterrupt:
        print("🛑 Server stopped.")
    finally:
        server.close()
        os.remove(SOCKET_PATH)
# *** *** #

def main_loop():

    #   Load YAML config file
    config = retrieve_yaml_file()

    #   Init auto measurement settings
    update_auto_vna_timer_settings(config["timer_settings"])
    update_system_timer_settings(config["timer_settings"])

    #   Check device mode and change power settings
    check_device_mode(config)

    #   Initialisation done
    print("Init done")

    system_scheduler.start()

    #   Before starting the communication with influxdb
    max_wait_time_s = 300
    if wait_for_network(max_wait=max_wait_time_s):
        print("Proceed with network-dependent tasks...")

        # During startup system 
        # --> Send current VNA configurations 
        # --> Update flag in config file
        update_yaml_flag("configurations","update", 1)
    else:
        print(f"No network connection achieved after {max_wait_time_s} minutes")

    #   Perform a single sweep to disable VNA
    update_yaml_flag("measurement_status","single_measurement", 1)
    
    #   Loop
    while True:

        time.sleep(0.1)

        #   Load YAML config file
        config = retrieve_yaml_file()

        #   Check updates in "configurations"
        if config.get("configurations", {}).get("update", 0) == 1:
            print("Configurations changed!")

            # Send new data to influxdb
            if send_configurations(config, "[New VNA configurations]"):

                # Update flag in config file
                update_yaml_flag("configurations","update", 0)


        #   Check updates in "timer_settings"
        if config.get("timer_settings", {}).get("update", 0) == 1:
            print("Timer settings changed!")

            # Todo send new timer settings to influxdb

            # Update timer settings vna measurements
            update_auto_vna_timer_settings(config["timer_settings"])

            # Update timer settings temperature measurements
            update_system_timer_settings(config["timer_settings"])
            
            # Update flag in config file
            update_yaml_flag("timer_settings","update", 0)


        #   Check automatic measurements is enabled
        if config.get("measurement_status", {}).get("auto_measurement", 0) == 1:

            #   This function checks if it was previously enabled
            vna_scheduler.start()
            #vna_scheduler.
            #start_socket_server()

        
        if config.get("measurement_status", {}).get("auto_measurement", 0) == 0:

            vna_scheduler.stop()

        #   Check single measurement is requested
        if config.get("measurement_status", {}).get("single_measurement", 0) == 1:
            print("Single measurement")
            
            # Try to execute the script
            try:
                vna_scheduler.activity = 1
                print(f"⏳ Executing {vna_script_path} at {datetime.now(timezone.utc).isoformat()}")
                subprocess.run(["python", vna_script_path])
                vna_scheduler.activity = 0
            except Exception as e:
                print(f"⚠️ Error while running {vna_script_path}: {e}")

            update_yaml_flag("measurement_status","single_measurement", 0)

if __name__ == "__main__":

    threading.Thread(target=start_server, daemon=True).start()
    
    main_loop()
