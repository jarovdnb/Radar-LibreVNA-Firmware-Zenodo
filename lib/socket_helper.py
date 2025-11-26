import socket
import json

SOCKET_PATH = "/tmp/streaming_socket.sock"

def get_local_socket_info():
    countdown_dict = {}

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(SOCKET_PATH)

        response = client.recv(1024)
        client.close()

        if response:
            try:
                countdown_dict = json.loads(response.decode())
                print("✅ Ontvangen:", countdown_dict)
            except json.JSONDecodeError as e:
                print(f"❌ JSON decode error: {e}")
        else:
            print("⚠️ Geen data ontvangen")

    except FileNotFoundError:
        print("❌ Socket bestaat niet. Draait de server?")
    except ConnectionRefusedError:
        print("❌ Verbinding geweigerd. Controleer of de server actief is.")
    except Exception as e:
        print(f"❌ Onverwachte fout: {e}")

    return countdown_dict

def check_measurement_active():
    countdown_dict = get_local_socket_info()

    return countdown_dict.get("vna_activity", 0)

def check_next_measurement():
    countdown_dict = get_local_socket_info()

    hour = countdown_dict.get("vna_countdown_hour", 0)
    min = countdown_dict.get("vna_countdown_minute", 0) 
    sec = countdown_dict.get("vna_countdown_second", 0)

    return hour*3600 + min*60 + sec