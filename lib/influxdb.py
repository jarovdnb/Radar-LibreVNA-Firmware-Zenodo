from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException
from datetime import datetime, timezone
import os
import re
import shutil
import glob

# Directory with latest results
result_vna_dir = os.path.join(os.getcwd(), "results/vna")

# Directory with backup results
result_vna_backup_dir = os.path.join(os.getcwd(), "results/vna_backup")

#   Init lists
frequencies = []
reals = []
imags = []
number_of_points = 0

def get_oldest_file(debug_name):
    files = [os.path.join(result_vna_dir, f) for f in os.listdir(result_vna_dir) if os.path.isfile(os.path.join(result_vna_dir, f))]

    if files:
        oldest_file = min(files, key=os.path.getmtime)
        # print("📄 File:", oldest_file)
        debug(debug_name, f"📄 File: {oldest_file}")
        return oldest_file
    else:
        # print("⚠️ No files found", result_vna_dir)
        debug(debug_name, f"⚠️ No files found {result_vna_dir}")
        return ""
    


def retrieve_data_from_file(file):

    with open(file, "r") as file:
        for line in file:
            try:
                freq_str, complex_str = line.strip().split(";")
                freq = float(freq_str)

                # Verwijder haakjes en zet om naar complex getal
                complex_val = complex(complex_str.replace("(", "").replace(")", ""))
                
                # Waarden toevoegen aan lijsten
                frequencies.append(freq)
                reals.append(complex_val.real)
                imags.append(complex_val.imag)
            except Exception as e:
                print(f"⚠️ Error processing rule: {line.strip()} → {e}")


def find_timestamp_in_filename(file_path):
    filename = os.path.basename(file_path)

    # Search file name
    match = re.search(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", filename)
    if match:
        timestamp_str = match.group()
        # Convert to datetime
        dt = datetime.strptime(timestamp_str, "%Y-%m-%d_%H-%M-%S")

        # Parse to datetime object
        ts = datetime.strptime(str(dt), '%Y-%m-%d %H:%M:%S')

        # Convert to ISO 8601 with 'Z' suffix before UTC (Z = Zulu Time = UTC)
        ts_influx = ts.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    else:
        ts_influx = None
        print("❌ No timestamp found")

    return ts_influx

def find_polarisation_in_filename(file_path):
    filename = os.path.basename(file_path)

    # Search for _VV of _VH in filename
    match = re.search(r"_([A-Z]{2})\.txt$", filename)
    if match:
        polarisation = match.group(1)
        # print("📡 Polarisation found:", polarisation)
    else:
        print("❌ No polarisation found")
        polarisation = ""
    
    return polarisation


# def send_data_influxdb(config, debug_name, ts, pol):

#     global frequencies, reals, imags

#     client = InfluxDBClient(url=config['influxdb']['url'], token=config['influxdb']['token'], org=config['influxdb']['org'])

#     write_api = client.write_api(write_options=SYNCHRONOUS)

#     number_of_points = len(frequencies)

#     # Create points
#     points = []
#     for i in range(number_of_points):
#         point = (
#             Point("radar_measurement")
#             .time(ts)
#             .tag("radar", config['fixed_configurations']['radar_name'])
#             .tag("pol", pol)
#             .tag("frequency", str(frequencies[i]))
#             .field("real", reals[i])
#             .field("imag", imags[i])
#         )

#         points.append(point)

#     # Write data in batch
#     write_api.write(bucket=config['influxdb']['bucket'], org=config['influxdb']['org'], record=points)

#     # print(f"✓ VNA sweep written with {number_of_points} points.")
#     debug(debug_name, f"✓ VNA sweep written with {number_of_points} points.")

#     client.close()

#     # Clear global buffers
#     frequencies = []
#     reals = []
#     imags = []
#     number_of_points = 0

def send_data_influxdb(config, debug_name, ts, pol):
    global frequencies, reals, imags

    try:
        client = InfluxDBClient(
            url=config['influxdb']['url'],
            token=config['influxdb']['token'],
            org=config['influxdb']['org']
        )
        write_api = client.write_api(write_options=SYNCHRONOUS)

        debug(debug_name, f"✓ Write API open.")

        number_of_points = len(frequencies)

        # Create points
        points = []
        for i in range(number_of_points):
            point = (
                Point("radar_measurement")
                .time(ts)
                .tag("radar", config['fixed_configurations']['radar_name'])
                .tag("pol", pol)
                .tag("frequency", str(frequencies[i]))
                .field("real", reals[i])
                .field("imag", imags[i])
            )
            points.append(point)

        debug(debug_name, f"✓ Try to send points.")

        # Write data
        write_api.write(
            bucket=config['influxdb']['bucket'],
            org=config['influxdb']['org'],
            record=points
        )

        debug(debug_name, f"✓ VNA sweep written with {number_of_points} points.")
        return True

    except ApiException as e:
        debug(debug_name, f"❗ InfluxDB API error: {e}")
        return False
    except Exception as e:
        debug(debug_name, f"❗ Failed to write to InfluxDB: {e}")
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass  # If client is undefined due to earlier failure

        # Clear global buffers
        frequencies = []
        reals = []
        imags = []

def debug(debug_name, string):
    print(f"{debug_name} {string}")

# ***************
def send_vna_data(config, debug_name):

    while(len(glob.glob(os.path.join(result_vna_dir, "*.txt"))) > 0):

        # Get oldest file
        file = get_oldest_file(debug_name)

        if file != "":
            # Get data
            retrieve_data_from_file(file)

            # Find timestamp in filename
            timestamp = find_timestamp_in_filename(file)

            # Find polarisation in filename
            polarisation = find_polarisation_in_filename(file)
            
            # Send data to influxdb
            status = send_data_influxdb(config, debug_name, timestamp, polarisation)

            # Check influxdb transmission succeeded
            if status:
                # Join right folders
                filename = os.path.basename(file)
                dst = os.path.join(result_vna_backup_dir, filename)

                # If target file already exists → add suffix
                if os.path.exists(dst):
                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(os.path.join(result_vna_backup_dir, f"{base}_{counter}{ext}")):
                        counter += 1
                    dst = os.path.join(result_vna_backup_dir, f"{base}_{counter}{ext}")

                # Move file to backup directory
                shutil.move(file, dst)
            else:
                debug(debug_name, f"Connection error! Data file stays in folder!")
                break


def send_configurations(config, debug_name):
    client = InfluxDBClient(url=config['influxdb']['url'], token=config['influxdb']['token'], org=config['influxdb']['org'])

    write_api = client.write_api(write_options=SYNCHRONOUS)

    # Timestamp
    ts = datetime.now()

    vna_config = config["configurations"]

    center = int(vna_config["center"])
    span = int(vna_config["span"])
    power = int(vna_config["power"])
    sweeps = int(vna_config["sweeps"])
    points = int(vna_config["points"])
    ifbw = int(vna_config["ifbw"])
    param = int(vna_config["parameter"][1:])

    # Create point
    point = (
        Point("radar_configuration")
        .time(ts)
        .tag("radar", config['fixed_configurations']['radar_name'])
        .field("center", center)
        .field("span", span)
        .field("start", center - span / 2)
        .field("stop", center + span / 2)
        .field("power", power)
        .field("sweeps", sweeps)
        .field("points", points)
        .field("ifbw", ifbw)
        .field("parameter", param)  # This is a string
    )

    # Write data in batch
    try:
        write_api.write(bucket=config['influxdb']['bucket'], org=config['influxdb']['org'], record=point)
    except Exception as e:
        print(f"[Warning] Could not write to InfluxDB: {e}")
        client.close()
        return False
    
    debug(debug_name, f"✓ Configuration sended.")

    client.close()

    return True


def send_system_info(config, debug_name, temperature_data, system_data):

    client = InfluxDBClient(url=config['influxdb']['url'], token=config['influxdb']['token'], org=config['influxdb']['org'])

    write_api = client.write_api(write_options=SYNCHRONOUS)

    # Timestamp
    ts = datetime.now()

    # Create point
    point = (
        Point("system_data")
        .time(ts)
        .tag("radar", config['fixed_configurations']['radar_name'])
        .field("vna-source", temperature_data["vna-source"])
        .field("vna-lo", temperature_data["vna-lo"])
        .field("vna-cpu", temperature_data["vna-cpu"])
        .field("case-inside", float(temperature_data["case-inside"]))
        .field("case-outside", float(temperature_data["case-outside"]))
        .field("system-cpu-temp", system_data["system-cpu-temp"])
        .field("system-cpu-load", system_data["system-cpu-load"])
        .field("system-disk", system_data["system-disk"])
    )

    # Write data in batch
    write_api.write(bucket=config['influxdb']['bucket'], org=config['influxdb']['org'], record=point)
    debug(debug_name, f"✓ System data sended.")

    client.close()


# # For testing
# from configuration import *

# config = retrieve_yaml_file()
# send_vna_data(config)







