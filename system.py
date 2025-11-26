import time
import psutil
import subprocess
from lib.influxdb import *
from lib.configuration import *
from librevna_temp import *
from lib.ds18b20 import *
from lib.socket_helper import *

def get_cpu_temp():
    try:
        output = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        return float(output.replace("temp=", "").replace("'C\n", ""))
    except Exception:
        return None

def get_cpu_load():
    try:
        return psutil.cpu_percent(interval=1)
    except Exception:
        return None

def get_disk_usage():
    try:
        usage = psutil.disk_usage('/')
        return round(usage.percent, 2)  # in %
    except Exception:
        return None 

def read_vna_temp(config):
    while 1:
        if check_vna_ready(config):
            try:
                # Create LibreVNA class
                vna = LibreVNA()

                # Setup VNA
                vna_temperature = vna.get_temp()
                # print(vna_temperature)

                time.sleep(1)

                # Close VNA connection
                vna.close()

                return vna_temperature
            except Exception as e:
                print(e)
        else:
            time.sleep(1)
            print("[System] Waiting for the end of the ongoing measurement!")

def check_vna_ready(config):
    if config["measurement_status"]["auto_measurement"] == 1:
        if not check_measurement_active() and check_next_measurement() > 10:
            print(check_measurement_active())
            print(check_next_measurement())
            return True
        else:
            return False
    else:
        if not check_measurement_active():
            return True
        else:
            return False


if __name__ == "__main__":

    system_data = {
        "system-cpu-temp": get_cpu_temp(),
        "system-cpu-load": get_cpu_load(),
        "system-disk": get_disk_usage()
    }

    # Get config file
    config = retrieve_yaml_file()

    # temps = asyncio.run(read_vna_temperature())
    # print("Temperaturen:", temps)

    vna_temperature = [0,0,0]

    # Read VNA temperature
    vna_temperature = read_vna_temp(config)

    # Read DS18B20 sensors
    try:
        ds18b20 = read_ds18b20_sensors()
    except Exception as e:
        print(e)

    # Create dict wih temperature data
    temperature_data = {
        "vna-source": vna_temperature[0],
        "vna-lo": vna_temperature[1],
        "vna-cpu": vna_temperature[2],
        "case-outside": ds18b20[0],
        "case-inside": ds18b20[1]
    }

    # Send data to influxdb server
    send_system_info(config, "[System]", temperature_data, system_data)


    



