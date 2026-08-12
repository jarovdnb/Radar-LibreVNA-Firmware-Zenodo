#   QuickSet QPT-50 pan-tilt positioner protocol driver
#   Implements the "Integrated Controller Protocol" (MN00056 Rev J).
#   Pure protocol/serial code: no knowledge of config files or the radar.
#
#   Frame:   STX  Cmd  [data...]  LRC  ETX          (host -> positioner)
#            ACK  Cmd  [data...]  LRC  ETX          (positioner -> host, NAK on error)
#   LRC:     XOR of Cmd through last data byte
#   Escape:  any data/LRC byte equal to a control char is sent as ESC, byte|0x80
#   Ints:    16-bit signed little-endian, angle x10 (0.1 degree)
#
#   Local/Windows note: find_qpt() below uses pyserial's cross-platform port
#   listing (serial.tools.list_ports) instead of the Linux-only
#   /dev/serial/by-id, /dev/ttyUSB* globs used by the deployed daemon's copy
#   of this file, so it works with Windows COM ports too.

import time

#   Control characters
STX = 0x02
ETX = 0x03
ACK = 0x06
NAK = 0x15
ESC = 0x1B
RESERVED = (STX, ETX, ACK, NAK, ESC)

#   Commands (MN00056)
CMD_STATUS_JOG   = 0x31   # get status / jog (doubles as keep-alive)
CMD_MOVE_ABS     = 0x33   # move to entered (absolute) coordinates
CMD_MOVE_DELTA   = 0x34   # move to delta (relative) coordinates
CMD_SET_MIN_SPD  = 0x93   # set minimum speeds
CMD_COMM_TIMEOUT = 0x96   # get/set communication timeout
CMD_HEATER       = 0x97   # get/set heater power sharing
CMD_SET_MAX_SPD  = 0x99   # set maximum speeds

#   97H config byte
HEATER_QUERY = 0   # read current config without changing it
HEATER_OFF   = 1   # heater disabled
HEATER_SHARE = 2   # heater cycles off while motors are moving (caps peak current)
HEATER_FULL  = 3   # heater runs concurrently with motor operation

#   31H command bitset bits
BIT_RES  = 0x01   # reset latched faults
BIT_STOP = 0x02   # stop all motion
BIT_OSL  = 0x04   # override soft limits
BIT_RU   = 0x08   # return coordinates as resolver units

#   Protocol sentinel: leave this axis where it is (raw value, not scaled)
LEAVE_AXIS = 9999


class QptError(Exception):
    pass

class QptTimeout(QptError):
    pass

class QptFrameError(QptError):
    pass

class QptNak(QptError):
    pass


#   Pure frame helpers

def escape_bytes(data):
    out = bytearray()
    for b in data:
        if b in RESERVED:
            out.append(ESC)
            out.append(b | 0x80)
        else:
            out.append(b)
    return bytes(out)


def unescape_bytes(data):
    out = bytearray()
    esc = False
    for b in data:
        if esc:
            out.append(b & 0x7F)
            esc = False
        elif b == ESC:
            esc = True
        else:
            out.append(b)
    if esc:
        raise QptFrameError("Dangling escape byte")
    return bytes(out)


def lrc(payload):
    result = 0
    for b in payload:
        result ^= b
    return result


def encode_frame(lead, cmd, data=b""):
    #   lead = STX for host frames, ACK/NAK for positioner replies
    payload = bytes([cmd]) + bytes(data)
    return bytes([lead]) + escape_bytes(payload + bytes([lrc(payload)])) + bytes([ETX])


def decode_frame(raw):
    #   Returns (lead, cmd, data); raises QptFrameError on a malformed frame
    if len(raw) < 3 or raw[0] not in (STX, ACK, NAK) or raw[-1] != ETX:
        raise QptFrameError(f"Malformed frame: {raw.hex()}")

    payload = unescape_bytes(raw[1:-1])
    if len(payload) < 2:
        raise QptFrameError(f"Frame too short: {raw.hex()}")

    #   XOR of cmd..data..LRC must be 0
    if lrc(payload) != 0:
        raise QptFrameError(f"LRC mismatch: {raw.hex()}")

    return raw[0], payload[0], payload[1:-1]


def deg_to_i16le(deg, scale=10):
    #   None -> protocol sentinel "leave axis"
    if deg is None:
        raw = LEAVE_AXIS
    else:
        raw = int(round(deg * scale + 0.0))
    return raw.to_bytes(2, "little", signed=True)


def i16le_to_deg(data, scale=10):
    return int.from_bytes(data[:2], "little", signed=True) / scale


class QptStatus:
    #   Decoded 31H response: coordinates + pan/tilt/general status bytes

    def __init__(self, pan_deg, tilt_deg, pan_status, tilt_status, gen_status):
        self.pan_deg = pan_deg
        self.tilt_deg = tilt_deg
        self.pan_status = pan_status
        self.tilt_status = tilt_status
        self.gen_status = gen_status

    @property
    def hres(self):
        return bool(self.gen_status & 0x80)

    @property
    def executing(self):
        return bool(self.gen_status & 0x40)

    @property
    def dest_coords(self):
        #   DES: returned coordinates are the destination, not the current position
        return bool(self.gen_status & 0x20)

    @property
    def moving(self):
        #   EXEC/DES or any of the four axis move bits
        return bool(self.gen_status & 0x6F)

    @property
    def hard_limit(self):
        return bool(self.pan_status & 0x30) or bool(self.tilt_status & 0x30)

    @property
    def soft_limit(self):
        return bool(self.pan_status & 0xC0) or bool(self.tilt_status & 0xC0)

    @property
    def faults(self):
        #   Latched faults (cleared with the RES bit): TO/DE/OL per axis
        names = [(0x08, "timeout (TO)"), (0x04, "direction error (DE)"), (0x02, "overload (OL)")]
        result = []
        for axis, status in (("pan", self.pan_status), ("tilt", self.tilt_status)):
            for bit, name in names:
                if status & bit:
                    result.append(f"{axis} {name}")
        return result


class Qpt:
    #   Driver around any pyserial-like object (write/read/timeout).
    #   Enforces the >=120 ms spacing between transmissions from MN00056.

    def __init__(self, ser, min_tx_interval=0.12):
        self.ser = ser
        self.min_tx_interval = min_tx_interval
        self.last_tx = 0.0
        self.scale = 10   # 1/10 degree; switched to 100 if the HRES bit is seen

    def _read_frame(self, timeout):
        #   Collect bytes until a raw ETX (a real ETX is always a frame end thanks to escaping)
        deadline = time.time() + timeout
        raw = bytearray()
        while time.time() < deadline:
            byte = self.ser.read(1)
            if not byte:
                continue
            #   Skip garbage before the frame start
            if not raw and byte[0] not in (ACK, NAK):
                continue
            raw += byte
            if byte[0] == ETX:
                return bytes(raw)
        raise QptTimeout("No response from positioner")

    def transact(self, cmd, data=b"", timeout=1.0):
        #   Rate limit
        wait = self.last_tx + self.min_tx_interval - time.time()
        if wait > 0:
            time.sleep(wait)

        if hasattr(self.ser, "reset_input_buffer"):
            self.ser.reset_input_buffer()

        self.ser.write(encode_frame(STX, cmd, data))
        self.last_tx = time.time()

        lead, rcmd, rdata = decode_frame(self._read_frame(timeout))
        if lead == NAK:
            raise QptNak(f"NAK for command {cmd:02X}")
        if rcmd != cmd:
            raise QptFrameError(f"Reply command {rcmd:02X} does not match {cmd:02X}")
        return rdata

    def get_status(self, stop=False, res=False, osl=False, pan_jog=0, tilt_jog=0):
        #   31H: keep-alive + status; jog bytes are speed(0-127)<<1 | direction
        bitset = (BIT_STOP if stop else 0) | (BIT_RES if res else 0) | (BIT_OSL if osl else 0)
        data = self.transact(CMD_STATUS_JOG, bytes([bitset, pan_jog, tilt_jog, 0, 0]))
        if len(data) < 7:
            raise QptFrameError(f"Status response too short: {data.hex()}")

        status = QptStatus(
            pan_deg=i16le_to_deg(data[0:2], self.scale),
            tilt_deg=i16le_to_deg(data[2:4], self.scale),
            pan_status=data[4],
            tilt_status=data[5],
            gen_status=data[6],
        )

        #   High resolution unit (PTHR-90) reports angles x100
        if status.hres and self.scale != 100:
            self.scale = 100
            status.pan_deg = i16le_to_deg(data[0:2], self.scale)
            status.tilt_deg = i16le_to_deg(data[2:4], self.scale)

        return status

    def move_to(self, pan_deg=None, tilt_deg=None):
        #   33H: absolute move; None leaves that axis where it is
        self.transact(CMD_MOVE_ABS, deg_to_i16le(pan_deg, self.scale) + deg_to_i16le(tilt_deg, self.scale))

    def move_delta(self, pan_deg=0.0, tilt_deg=0.0):
        #   34H: relative move
        self.transact(CMD_MOVE_DELTA, deg_to_i16le(pan_deg, self.scale) + deg_to_i16le(tilt_deg, self.scale))

    def stop(self):
        return self.get_status(stop=True)

    def clear_faults(self):
        return self.get_status(res=True)

    def set_max_speeds(self, pan, tilt):
        #   99H: caps automated move speed (1-255 per axis)
        self.transact(CMD_SET_MAX_SPD, bytes([pan & 0xFF, tilt & 0xFF]))

    def set_min_speeds(self, pan, tilt):
        #   93H: motor speed floor (0-255 per axis)
        self.transact(CMD_SET_MIN_SPD, bytes([pan & 0xFF, tilt & 0xFF]))

    def set_comm_timeout(self, seconds):
        #   96H: positioner halts if no frame arrives within the timeout (0 = disabled)
        self.transact(CMD_COMM_TIMEOUT, bytes([seconds & 0x7F]))

    def get_heater_config(self):
        #   97H with Config=0 (query): returns the current mode without changing it
        data = self.transact(CMD_HEATER, bytes([HEATER_QUERY]))
        if not data:
            raise QptFrameError("Heater query response was empty")
        return data[0]

    def set_heater_config(self, config):
        #   97H: 1=off, 2=share (off while moving), 3=full (concurrent with motion).
        #   The unit echoes back the mode it actually accepted; a mismatch means
        #   the request was not honored (e.g. no heater fitted on this unit).
        if config not in (HEATER_OFF, HEATER_SHARE, HEATER_FULL):
            raise ValueError(f"heater config must be {HEATER_OFF}, {HEATER_SHARE} or {HEATER_FULL}")
        data = self.transact(CMD_HEATER, bytes([config]))
        if not data:
            raise QptFrameError("Heater set response was empty")
        return data[0]


def open_serial(port, baud):
    #   Imported here, not at module level, so importing this module (e.g. for
    #   protocol-only unit tests) never requires pyserial to be installed
    import serial
    return serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                         timeout=0.05)


def connect(ser, attempts=20):
    #   The unit autobauds at power-up: it needs ~125-150 bytes before it starts
    #   replying, so keep sending status polls until one is answered.
    qpt = Qpt(ser)
    for _ in range(attempts):
        try:
            qpt.get_status()
            return qpt
        except (QptTimeout, QptFrameError, QptNak):
            continue
    return None


def list_candidate_ports():
    #   Cross-platform port discovery (Windows COM ports, Linux /dev/tty*,
    #   macOS /dev/cu.*) via pyserial's own device listing.
    from serial.tools import list_ports
    return sorted(p.device for p in list_ports.comports())


def find_qpt(port_hint="", baud=9600):
    #   Try the configured port first, otherwise probe every detected serial port
    candidates = [port_hint] if port_hint else list_candidate_ports()

    for port in candidates:
        ser = None
        try:
            ser = open_serial(port, baud)
            qpt = connect(ser)
            if qpt is not None:
                return port, qpt
            ser.close()
        except Exception as e:
            print(f"Could not probe {port}: {e}")
            if ser is not None:
                ser.close()

    return None, None
