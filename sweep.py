from librevna import *
from lib.influxdb import *
from lib.configuration import *

if __name__ == "__main__":
    # Read configuration file
    config = retrieve_yaml_file()

    # Print number of measurements
    print(f"[LibreVNA] Number of measurements: {config["configurations"]["measurements"]}")

    # Execute measurements
    for i in range(0, int(number_of_measurements)):
        print(f"[LibreVNA] Starting measurement {i + 1}")
        try:
            # Create LibreVNA class
            vna = LibreVNA()

            # Setup VNA
            vna.setup(config)

            # Measure with VNA
            vna.measure()
            
            # Close VNA connection
            vna.close()

            # Send data to influxdb
            send_vna_data(config, "[LibreVNA]")

            print("[LibreVNA] Done")
        except Exception as e:
            print(e)
