# Warning! Use latest compiled software from github, not the latest release
# See e.g. https://github.com/jankae/LibreVNA/actions/runs/12914350951

import re
import socket
from asyncio import IncompleteReadError  # only import the exception class
import time
from datetime import datetime, timedelta
from pathlib import Path
import time
from filelock import FileLock

from lib.influxdb import *


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


class LibreVNA:
    def __init__(self, ip_address='localhost', port=1234):#10.128.68.13
        self.ip_address = ip_address
        self.port = port
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

    def get_temp(self):
        time.sleep(0.1)
        t = self.vna.query(":DEV:INF:TEMP?")
        temperatures = [float(value) for value in t.split('/')]

        self.vna.cmd("*CLS")

        return temperatures

    def close(self):
        del self.vna     
   
    def debug(self, string):
        print(f"[LibreVNA TEMP] {string}")

