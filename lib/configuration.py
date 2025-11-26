import yaml
import os
from filelock import FileLock

home_dir = os.path.expanduser("~")
CONFIG_PATH = home_dir + "/config.yaml"
LOCK_PATH = CONFIG_PATH + ".lock"

def retrieve_yaml_file():
    config = {}
    try:
        with FileLock(LOCK_PATH):
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, 'r') as file:
                    config = yaml.safe_load(file) or {}
    except Exception as e:
        print(f"⚠️ Error reading YAML file: {e}")

    return config

#   YAML functions
def update_yaml_flag(TAGlvl1, TAGlvl2, value):
    try:
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as f:
                config = yaml.safe_load(f) or {}

            # Check of de eerste en tweede tag bestaan
            if TAGlvl1 not in config or TAGlvl2 not in config[TAGlvl1]:
                raise KeyError(f"'{TAGlvl1}' or '{TAGlvl2}' not found in config")

            # Pas de waarde aan
            config[TAGlvl1][TAGlvl2] = value

            # Schrijf het bestand terug
            with open(CONFIG_PATH, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False)

    except Exception as e:
        print(f"⚠️ Failed to update config: {e}")