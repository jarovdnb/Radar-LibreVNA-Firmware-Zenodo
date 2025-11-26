import glob
import time

def read_temp_raw(device_file):
    with open(device_file, 'r') as f:
        return f.readlines()

def read_temp(device_file):
    lines = read_temp_raw(device_file)
    # Wait for a valid read
    while lines[0].strip()[-3:] != 'YES':
        time.sleep(0.2)
        lines = read_temp_raw(device_file)

    # Extract temperature
    equals_pos = lines[1].find('t=')
    if equals_pos != -1:
        temp_string = lines[1][equals_pos + 2:]
        temp_c = float(temp_string) / 1000.0
        return temp_c
    return None

# Locate all DS18B20 devices
base_dir = '/sys/bus/w1/devices/'
device_folders = glob.glob(base_dir + '28-*')  # DS18B20 starts with '28-'

def read_ds18b20_sensors():

    readings = [0, 0]

    if len(device_folders) < 2:
        print("Less than 2 DS18B20 sensors found!")
    else:
        # print("Found DS18B20 sensors:")
        for folder in device_folders[:2]:  # Just take first two
            # print(f"  {folder}")
            None

        for i, folder in enumerate(device_folders[:2]):
            device_file = folder + '/w1_slave'
            temp = read_temp(device_file)
            # print(f"Sensor {i+1}: {temp:.2f} °C")

            readings[i] = temp

    return readings

