# Warning! Use latest compiled software from github, not the latest release
# See e.g. https://github.com/jankae/LibreVNA/actions/runs/12914350951

import re
import socket
from asyncio import IncompleteReadError  # only import the exception class
import time
import math
import cmath
import csv
import os
from datetime import datetime, timedelta
from pathlib import Path
import sys
import RPi.GPIO as GPIO
import time

from lib.influxdb import *
from lib.configuration import *

result_dir = os.path.join(os.getcwd(), "results/vna")

class SocketStreamReader:
    def __init__(self, sock: socket.socket, default_timeout=5):
        self._sock = sock
        self._sock.setblocking(0)
        self._recv_buffer = bytearray()
        self.default_timeout = default_timeout

    def read(self, num_bytes: int = -1) -> bytes:
        raise NotImplementedError

    def readexactly(self, num_bytes: int) -> bytes:
        buf = bytearray(num_bytes)
        pos = 0
        while pos < num_bytes:
            n = self._recv_into(memoryview(buf)[pos:])
            if n == 0:
                raise IncompleteReadError(bytes(buf[:pos]), num_bytes)
            pos += n
        return bytes(buf)

    def readline(self, timeout=None) -> bytes:
        return self.readuntil(b"\n", timeout=timeout)

    def readuntil(self, separator: bytes = b"\n", timeout=None) -> bytes:
        if len(separator) != 1:
            raise ValueError("Only separators of length 1 are supported.")
        if timeout is None:
            timeout = self.default_timeout

        chunk = bytearray(4096)
        start = 0
        buf = bytearray(len(self._recv_buffer))
        bytes_read = self._recv_into(memoryview(buf))
        assert bytes_read == len(buf)

        time_limit = time.time() + timeout
        while True:
            idx = buf.find(separator, start)
            if idx != -1:
                break
            elif time.time() > time_limit:
                raise Exception("Timed out waiting for response from GUI")

            start = len(self._recv_buffer)
            bytes_read = self._recv_into(memoryview(chunk))
            buf += memoryview(chunk)[:bytes_read]

        result = bytes(buf[: idx + 1])
        self._recv_buffer = b"".join(
            (memoryview(buf)[idx + 1:], self._recv_buffer)
        )
        return result

    def _recv_into(self, view: memoryview) -> int:
        bytes_read = min(len(view), len(self._recv_buffer))
        view[:bytes_read] = self._recv_buffer[:bytes_read]
        self._recv_buffer = self._recv_buffer[bytes_read:]
        if bytes_read == len(view):
            return bytes_read
        try:
            bytes_read += self._sock.recv_into(view[bytes_read:], 0)
        except:
            pass
        return bytes_read


class _libreVNA:
    def __init__(self, host='localhost', port=19542,
                 check_cmds=True, timeout=3):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.connect((host, port))
        except:
            raise Exception("Unable to connect to LibreVNA-GUI. Make sure it is running and the TCP server is enabled.")
        self.reader = SocketStreamReader(self.sock,
                                         default_timeout=timeout)
        self.default_check_cmds = check_cmds

        self.timeout = timeout

    def __del__(self):
        self.sock.close()

    def __read_response(self, timeout=None):
        if timeout is None:
            timeout = self.timeout
        return self.reader.readline(timeout=timeout).decode().rstrip()

    def cmd(self, cmd, check=None, timeout=None):
        if timeout is None:
            timeout = self.timeout
        self.sock.sendall(cmd.encode())
        self.sock.send(b"\n")
        if check or (check is None and self.default_check_cmds):
            status = self.get_status(timeout=timeout)
            if status & 0x20:
                raise Exception("Command Error")
            if status & 0x10:
                raise Exception("Execution Error")
            if status & 0x08:
                raise Exception("Device Error")
            if status & 0x04:
                raise Exception("Query Error")
            return status
        else:
            return None

    def query(self, query, timeout=None):
        if timeout is None:
            timeout = self.timeout
        self.sock.sendall(query.encode())
        self.sock.send(b"\n")
        return self.__read_response(timeout=timeout)

    def get_status(self, timeout=None):
        if timeout is None:
            timeout = self.timeout
        resp = self.query("*ESR?", timeout=timeout)
        if not re.match(r'^\d+$', resp):
            raise Exception("Expected numeric response from *ESR? but got "
                            f"'{resp}'")
        status = int(resp)
        if status < 0 or status > 255:
            raise Exception(f"*ESR? returned invalid value {status}.")
        return status

    @staticmethod
    def parse_VNA_trace_data(data):
        ret = []
        # Remove brackets (order of data implicitly known)
        data = data.replace(']', '').replace('[', '')
        values = data.split(',')
        if int(len(values) / 3) * 3 != len(values):
            # number of values must be a multiple of three (frequency, real, imaginary)
            raise Exception("Invalid input data: expected tuples of three values each")
        for i in range(0, len(values), 3):
            freq = float(values[i])
            real = float(values[i + 1])
            imag = float(values[i + 2])
            ret.append((freq, complex(real, imag)))
        return ret

    @staticmethod
    def parse_SA_trace_data(data):
        ret = []
        # Remove brackets (order of data implicitly known)
        data = data.replace(']', '').replace('[', '')
        values = data.split(',')
        if int(len(values) / 2) * 2 != len(values):
            # number of values must be a multiple of two (frequency, dBm)
            raise Exception("Invalid input data: expected tuples of two values each")
        for i in range(0, len(values), 2):
            freq = float(values[i])
            dBm = float(values[i + 1])
            ret.append((freq, dBm))
        return ret

def calculate_magnitude_phase(complex_number):
    magnitude = 20*math.log10(abs(complex_number))
    phase = math.degrees(cmath.phase(complex_number))
    return magnitude, phase

class LibreVNA:
    def __init__(self, ip_address='localhost', port=1234):#10.128.68.13
        self.ip_address = ip_address
        self.port = port
        self.idn = None
        self.vna = None
        self.last_filename_vh = None
        self.last_filename_vv = None
        self.temperature_filename = None
        self.config = None
        self.polarisation_inverted = 0
        self.connect()


    def connect(self, ip_address=None, port=None):
        if ip_address is None:
            ip_address = self.ip_address
        if port is None:
            port = self.port
        try:
            self.vna = _libreVNA(ip_address, port)
            #time.sleep(5)
            self.vna.cmd("DEV:CONN")
            #time.sleep(5)
            dev = self.vna.query("DEV:CONN?")
            if dev == "Not connected":
                self.debug("Not connected to any device, aborting")
                return
            else:
                self.debug("Connected to " + dev)
        except Exception as e:
            self.debug("Failed to connect to Instrument")
            self.debug(e)
            self.debug("-----")

    def setup(self, config):

        self.config = config

        # Get GPIO pin to control the RF switch
        self.rf_switch_gpio_pin = self.config['fixed_configurations']['rf_switch_pin']
        self.debug(self.rf_switch_gpio_pin)

        # Check if polarisation should be inverted (RX antennas are connected in opposite way)
        self.polarisation_inverted = self.config['fixed_configurations']['polarisation_inverted']
        if self.polarisation_inverted:
            self.debug("Polarisation is inverted!")

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.rf_switch_gpio_pin, GPIO.OUT)
        
        # Select config file
        settings = self.config['configurations']

        self.vna.cmd("DEV:MODE VNA")
        self.vna.cmd("DEV:REF:IN INT")
        self.vna.cmd(f"VNA:FREQ:CENT {settings['center']}")#5400000000
        self.vna.cmd(f"VNA:FREQ:SPAN {settings['span']}")#1950000000
        self.vna.cmd(f"VNA:ACQ:POINTS {settings['points']}")
        self.vna.cmd(f"VNA:STIM:LVL {settings['power']}")
        self.vna.cmd(f"VNA:ACQ:AVG {settings['sweeps']}")
        self.vna.cmd(f"VNA:ACQ:IFBW {settings['ifbw']}")

        self.debug(f"Device mode: {self.vna.query('DEV:MODE?')}")
        self.debug(f"Reference clock: {self.vna.query('DEV:REF:IN?')}")
        self.debug(f"Center frequency: {self.vna.query('VNA:FREQ:CENT?')} Hz")#5400000000
        self.debug(f"Span: {self.vna.query('VNA:FREQ:SPAN?')} Hz")#1950000000
        self.debug(f"Points: {self.vna.query('VNA:ACQ:POINTS?')}")
        self.debug(f"Level: {self.vna.query('VNA:STIM:LVL?')} dBm")
        self.debug(f"Number of sweeps: {self.vna.query('VNA:ACQ:AVG?')}")
        self.debug(f"IFBW: {self.vna.query('VNA:ACQ:IFBW?')} Hz")

    def measure(self, filename=None):

        # |*****************************************|
        # |     ***     Measurement VV      ***     |
        # |*****************************************|
        
        self.debug("Start measurement VV")

        # Change RF switch
        if self.polarisation_inverted:
            GPIO.output(self.rf_switch_gpio_pin, GPIO.LOW)
        else:
            GPIO.output(self.rf_switch_gpio_pin, GPIO.HIGH)
        
        # Get timestamp
        now = datetime.now()

        # Define filename
        if filename is None:
            self.last_filename_vv = now.strftime("%Y-%m-%d_%H-%M-%S") + "_dataset_VV"
        else:
            self.last_filename_vv = now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + filename

        self.vna.cmd("VNA:ACQ:SINGLE TRUE")

        while self.vna.query("VNA:ACQ:FIN?") == "FALSE":
            time.sleep(0.1)

        trace = self.vna.query(f":VNA:TRAC:DATA? {self.config['configurations']['parameter']}")
        data = self.vna.parse_VNA_trace_data(trace)

        Path(result_dir).mkdir(parents=True, exist_ok=True)

        with open(f"{result_dir}/{self.last_filename_vv}.txt", 'w') as fp:
            fp.write('\n'.join('{};{}'.format(x[0], x[1]) for x in data))

        # |*****************************************|
        # |     ***     Measurement VH      ***     |
        # |*****************************************|

        self.debug("Start measurement VH")

        # Change RF switch
        if self.polarisation_inverted:
            GPIO.output(self.rf_switch_gpio_pin, GPIO.HIGH)
        else:
            GPIO.output(self.rf_switch_gpio_pin, GPIO.LOW)
        
        # Prevent two files with the same timestamp
        if now.replace(microsecond=0) == datetime.now().replace(microsecond=0):
            now = now + timedelta(seconds=1)
        else:
            now = datetime.now()
        
        # Define filename
        if filename is None:
            self.last_filename_vh = now.strftime("%Y-%m-%d_%H-%M-%S") + "_dataset_VH"
        else:
            self.last_filename_vh = now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + filename

        self.vna.cmd("VNA:ACQ:SINGLE TRUE")
        while self.vna.query("VNA:ACQ:FIN?") == "FALSE":
            time.sleep(0.1)

        trace = self.vna.query(f":VNA:TRAC:DATA? {self.config['configurations']['parameter']}")
        data = self.vna.parse_VNA_trace_data(trace)

        Path(result_dir).mkdir(parents=True, exist_ok=True)

        with open(f"{result_dir}/{self.last_filename_vh}.txt", 'w') as fp:
            fp.write('\n'.join('{};{}'.format(x[0], x[1]) for x in data))


    def convert(self, filename=None):
        if filename is None:
            filename = self.last_filename_vh

        data = []
        with open(f"{result_dir}/{filename}.txt", 'r') as file:
            lines = file.readlines()

        for line in lines:
            line = line.strip()

            if line:
                numbers = line.split(';')
                data.append((float(numbers[0]), complex(numbers[1])))

        processed_data = [
            (frequency, *calculate_magnitude_phase(complex_number))
            for frequency, complex_number in data
        ]

        Path(result_dir).mkdir(parents=True, exist_ok=True)

        with open(f"{result_dir}/{filename}.csv", 'w', newline='') as csvfile:
            csvwriter = csv.writer(csvfile)
            csvwriter.writerow(['Frequency [Hz]', 'Magnitude [dB]', 'Phase [deg]'])
            for row in processed_data:
                csvwriter.writerow(row)

    def close(self):
        del self.vna

    def get_last_csv(self):
        files_ = []
        
        if self.last_filename_vv is not None:
            files_.append(result_dir + "/" + self.last_filename_vv +".csv")
        if self.last_filename_vh is not None:
            files_.append(result_dir + "/" +self.last_filename_vh +".csv")
        self.debug(files_)
        return files_

    def get_last_txt(self):
        files_ = []
        
        if self.last_filename_vv is not None:
            files_.append(result_dir + "/" + self.last_filename_vv +".txt")
        if self.last_filename_vh is not None:
            files_.append(result_dir + "/" +self.last_filename_vh +".txt")
        self.debug(files_)
        return files_
    
    def debug(self, string):
        print(f"[LibreVNA] {string}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        number_of_measurements = sys.argv[1]
    else:
        number_of_measurements = 1
    print(f"[LibreVNA] Number of measurements: {number_of_measurements}")
    for i in range(0, int(number_of_measurements)):
        print(f"[LibreVNA] Starting measurement {i + 1}")
        try:
            # Read configuration file
            config = retrieve_yaml_file()

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

