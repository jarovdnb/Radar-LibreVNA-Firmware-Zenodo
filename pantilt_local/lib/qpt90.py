#   MOOG QuickSet PTCR-96 embedded controller protocol driver (MN00162 Rev C).
#   Covers the Q90 (and other PTCR-96-based platforms: QPT50/90/200/500,
#   QMP/QMP-R) -- NOT the same wire format as lib/qpt.py, which targets an
#   older/different protocol revision. Key differences from qpt.py:
#     - Frames carry an Identity byte (STX, Identity, Cmd, Data, LRC, ETX);
#       the LRC covers Identity..last data byte, not just Cmd..data.
#     - Pan/tilt coordinates are 24-bit (3-byte) signed little-endian,
#       scaled x100 (0.01 deg), not 16-bit x10.
#     - The 31H status/jog command carries jog fields for two camera ports
#       (7 data bytes), not one.
#   Camera/lens/preset-table/tour commands (60H-75H, 32H, 40H-56H) and the
#   setup commands whose exact data layout wasn't available when this was
#   written (heater 97H, max speed 9CH, comm timeout 96H, identity 9FH) are
#   intentionally not implemented -- add them once their byte layout (MN00162
#   sections 2.6, 2.9.7, 2.9.8, 2.9.10, 2.9.14) is on hand.
#
#   Frame:   STX  Identity  Cmd  [data...]  LRC  ETX     (host -> PTCR)
#            ACK  Identity  Cmd  [data...]  LRC  ETX     (PTCR -> host, NAK on error)
#   LRC:     XOR of Identity through last data byte
#   Escape:  any data/LRC byte equal to a control char is sent as ESC, byte|0x80
#   Ints:    24-bit signed little-endian, angle x100 (0.01 degree)

import time

#   Control characters
STX = 0x02
ETX = 0x03
ACK = 0x06
NAK = 0x15
ESC = 0x1B
RESERVED = (STX, ETX, ACK, NAK, ESC)

#   Identity: 0 = dedicated RS-232/RS-422 or broadcast (MN00162 Sec 1.4)
BROADCAST_IDENTITY = 0x00

#   Commands (MN00162 Sec 2.1) actually implemented here
CMD_STATUS_JOG = 0x31   # get status / jog (doubles as keep-alive)
CMD_MOVE_ABS   = 0x33   # move to entered (absolute) coordinates
CMD_MOVE_DELTA = 0x34   # move to delta (relative) coordinates
CMD_MOVE_ZERO  = 0x35   # move to absolute 0/0
CMD_MOVE_HOME  = 0x36   # move to home (preset 31)

#   31H command bitset bits (Sec 2.2)
BIT_RES  = 0x01   # reset latched faults
BIT_STOP = 0x02   # stop all motion
BIT_OSL  = 0x04   # override soft limits
BIT_TSLO = 0x08   # halve tilt jog speed range
BIT_PSLO = 0x10   # halve pan jog speed range
BIT_TDIR = 0x40   # tilt jog direction: 1 = up, 0 = down
BIT_PDIR = 0x80   # pan jog direction: 1 = CW, 0 = CCW

#   "Move To Entered Coordinates" sentinel: leave this axis where it is
NO_MOVE_DEG = 999.99


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


def encode_frame(lead, identity, cmd, data=b""):
    #   lead = STX for host frames, ACK/NAK for positioner replies
    payload = bytes([identity, cmd]) + bytes(data)
    return bytes([lead]) + escape_bytes(payload + bytes([lrc(payload)])) + bytes([ETX])


def decode_frame(raw):
    #   Returns (lead, identity, cmd, data); raises QptFrameError on a malformed frame
    if len(raw) < 4 or raw[0] not in (STX, ACK, NAK) or raw[-1] != ETX:
        raise QptFrameError(f"Malformed frame: {raw.hex()}")

    payload = unescape_bytes(raw[1:-1])
    if len(payload) < 3:
        raise QptFrameError(f"Frame too short: {raw.hex()}")

    #   XOR of identity..data..LRC must be 0
    if lrc(payload) != 0:
        raise QptFrameError(f"LRC mismatch: {raw.hex()}")

    return raw[0], payload[0], payload[1], payload[2:-1]


def deg_to_i24le(deg, scale=100):
    raw = int(round(deg * scale))
    return raw.to_bytes(3, "little", signed=True)


def i24le_to_deg(data, scale=100):
    return int.from_bytes(data[:3], "little", signed=True) / scale


class Qpt90Status:
    #   Decoded response to 31H/33H/34H/35H/36H: coordinates + pan/tilt/general
    #   status bytes (MN00162 Sec 2.2). Zoom/focus/camera fields that may follow
    #   are not parsed -- this driver doesn't drive the camera/lens ports.

    def __init__(self, pan_deg, tilt_deg, pan_status, tilt_status, gen_status):
        self.pan_deg = pan_deg
        self.tilt_deg = tilt_deg
        self.pan_status = pan_status
        self.tilt_status = tilt_status
        self.gen_status = gen_status

    @property
    def continuous(self):
        #   CON: platform is continuous rotation; pan soft/hard limits are ignored
        return bool(self.gen_status & 0x80)

    @property
    def executing(self):
        return bool(self.gen_status & 0x40)

    @property
    def destination(self):
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
        #   Latched faults (cleared with the RES bit): TO/DE per axis
        names = [(0x08, "timeout (TO)"), (0x04, "direction error (DE)")]
        result = []
        for axis, status in (("pan", self.pan_status), ("tilt", self.tilt_status)):
            for bit, name in names:
                if status & bit:
                    result.append(f"{axis} {name}")
        return result


class Qpt90:
    #   Driver around any pyserial-like object (write/read/timeout).
    #   Enforces the "no faster than 10 packets/sec" limit from MN00162 Sec 1.

    def __init__(self, ser, identity=BROADCAST_IDENTITY, min_tx_interval=0.1):
        self.ser = ser
        self.identity = identity
        self.min_tx_interval = min_tx_interval
        self.last_tx = 0.0

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

        self.ser.write(encode_frame(STX, self.identity, cmd, data))
        self.last_tx = time.time()

        lead, identity, rcmd, rdata = decode_frame(self._read_frame(timeout))
        if lead == NAK:
            raise QptNak(f"NAK for command {cmd:02X}")
        if rcmd != cmd:
            raise QptFrameError(f"Reply command {rcmd:02X} does not match {cmd:02X}")
        return rdata

    def _parse_status(self, data):
        #   Common prefix of every 31H/33H/34H/35H/36H response: pan(3) + tilt(3)
        #   + pan_status(1) + tilt_status(1) + gen_status(1). Zoom/focus/camera
        #   bytes that may follow are ignored.
        if len(data) < 9:
            raise QptFrameError(f"Status response too short: {data.hex()}")

        return Qpt90Status(
            pan_deg=i24le_to_deg(data[0:3]),
            tilt_deg=i24le_to_deg(data[3:6]),
            pan_status=data[6],
            tilt_status=data[7],
            gen_status=data[8],
        )

    def get_status(self, stop=False, res=False, osl=False, pan_jog=0, tilt_jog=0):
        #   31H: keep-alive + status. pan_jog/tilt_jog are signed speeds
        #   (-255..255); sign selects direction (PDIR/TDIR), magnitude is the
        #   jog speed byte. 0 means "don't jog that axis".
        bitset = (BIT_STOP if stop else 0) | (BIT_RES if res else 0) | (BIT_OSL if osl else 0)
        if pan_jog > 0:
            bitset |= BIT_PDIR
        if tilt_jog > 0:
            bitset |= BIT_TDIR
        pan_speed = min(255, abs(pan_jog))
        tilt_speed = min(255, abs(tilt_jog))

        #   Trailing 4 bytes are zoom/focus jog for camera 1 and 2 (unused here)
        data = self.transact(CMD_STATUS_JOG, bytes([bitset, pan_speed, tilt_speed, 0, 0, 0, 0]))
        return self._parse_status(data)

    def move_to(self, pan_deg=None, tilt_deg=None):
        #   33H: absolute move; None leaves that axis where it is
        pan_val = NO_MOVE_DEG if pan_deg is None else pan_deg
        tilt_val = NO_MOVE_DEG if tilt_deg is None else tilt_deg
        data = self.transact(CMD_MOVE_ABS, deg_to_i24le(pan_val) + deg_to_i24le(tilt_val))
        return self._parse_status(data)

    def move_delta(self, pan_deg=0.0, tilt_deg=0.0):
        #   34H: relative move; 0 leaves that axis where it is
        data = self.transact(CMD_MOVE_DELTA, deg_to_i24le(pan_deg) + deg_to_i24le(tilt_deg))
        return self._parse_status(data)

    def move_to_zero(self):
        #   35H: move to absolute (factory center) 0/0
        return self._parse_status(self.transact(CMD_MOVE_ZERO))

    def move_to_home(self):
        #   36H: move to the "Home" preset (preset 31)
        return self._parse_status(self.transact(CMD_MOVE_HOME))

    def stop(self):
        return self.get_status(stop=True)

    def clear_faults(self):
        return self.get_status(res=True)


def open_serial(port, baud):
    #   Imported here, not at module level, so importing this module (e.g. for
    #   protocol-only unit tests) never requires pyserial to be installed
    import serial
    return serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                         timeout=0.05)


def connect(ser, identity=BROADCAST_IDENTITY, attempts=20):
    #   The unit autobauds at power-up: it needs ~125-150 bytes before it starts
    #   replying, so keep sending status polls until one is answered.
    qpt = Qpt90(ser, identity=identity)
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


def find_qpt90(port_hint="", baud=9600, identity=BROADCAST_IDENTITY):
    #   Try the configured port first, otherwise probe every detected serial port
    candidates = [port_hint] if port_hint else list_candidate_ports()

    for port in candidates:
        ser = None
        try:
            ser = open_serial(port, baud)
            qpt = connect(ser, identity=identity)
            if qpt is not None:
                return port, qpt
            ser.close()
        except Exception as e:
            print(f"Could not probe {port}: {e}")
            if ser is not None:
                ser.close()

    return None, None
