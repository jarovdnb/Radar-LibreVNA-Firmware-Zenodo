#   Plain-assert tests for the QPT-50 protocol driver (no hardware needed).
#   Run with: python3 tests/test_qpt_frames.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.qpt import (STX, ETX, ACK, QptFrameError, QptNak,
                     encode_frame, decode_frame, escape_bytes, unescape_bytes,
                     deg_to_i16le, i16le_to_deg, QptStatus)


def test_escaping():
    #   Every reserved byte must be escaped as ESC, byte|0x80 (manual examples: 02->1B 82, 1B->1B 9B)
    assert escape_bytes(bytes([0x02])) == bytes([0x1B, 0x82])
    assert escape_bytes(bytes([0x1B])) == bytes([0x1B, 0x9B])
    for b in (0x02, 0x03, 0x06, 0x15, 0x1B):
        assert unescape_bytes(escape_bytes(bytes([b]))) == bytes([b])
    #   Non-reserved bytes pass through untouched
    assert escape_bytes(bytes([0x00, 0x31, 0xFF])) == bytes([0x00, 0x31, 0xFF])


def test_frame_roundtrip():
    #   Round-trip with data containing all reserved bytes
    data = bytes([0x02, 0x03, 0x06, 0x15, 0x1B, 0x00, 0x7F, 0xFF])
    raw = encode_frame(STX, 0x31, data)
    lead, cmd, decoded = decode_frame(raw)
    assert lead == STX and cmd == 0x31 and decoded == data

    #   Manual worked example: keep-alive 31H with zero data -> LRC 0x31
    raw = encode_frame(STX, 0x31, bytes(5))
    assert raw == bytes([0x02, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00, 0x31, 0x03])

    #   Move-to pan +90.0, tilt 0.0: pan MSB 0x03 collides with ETX and must be escaped
    raw = encode_frame(STX, 0x33, deg_to_i16le(90.0) + deg_to_i16le(0.0))
    assert raw == bytes([0x02, 0x33, 0x84, 0x1B, 0x83, 0x00, 0x00, 0xB4, 0x03])


def test_lrc_corruption():
    raw = bytearray(encode_frame(ACK, 0x31, bytes([0x10, 0x20])))
    raw[2] ^= 0xFF   # corrupt a data byte
    try:
        decode_frame(bytes(raw))
        assert False, "corrupted frame must raise"
    except QptFrameError:
        pass


def test_int_encoding():
    #   Manual examples: 900 -> 84 03, -200 -> 38 FF
    assert deg_to_i16le(90.0) == bytes([0x84, 0x03])
    assert deg_to_i16le(-20.0) == bytes([0x38, 0xFF])
    for deg in (180.0, -180.0, 90.0, -90.0, 0.0, -0.0, 217.5, 0.1):
        assert abs(i16le_to_deg(deg_to_i16le(deg)) - deg) < 1e-9
    #   None -> 9999 sentinel (leave axis)
    assert int.from_bytes(deg_to_i16le(None), "little", signed=True) == 9999


def test_status_decode():
    #   Idle at pan 10.0 / tilt -15.0, no faults
    status = QptStatus(10.0, -15.0, 0x00, 0x00, 0x00)
    assert not status.moving and status.faults == []

    #   Moving CW + EXEC
    status = QptStatus(0, 0, 0x00, 0x00, 0x48)
    assert status.moving and status.executing

    #   Latched faults: pan TO, tilt OL
    status = QptStatus(0, 0, 0x08, 0x02, 0x00)
    assert status.faults == ["pan timeout (TO)", "tilt overload (OL)"]

    #   Limits
    assert QptStatus(0, 0, 0x20, 0x00, 0x00).hard_limit
    assert QptStatus(0, 0, 0x80, 0x00, 0x00).soft_limit


if __name__ == "__main__":
    test_escaping()
    test_frame_roundtrip()
    test_lrc_corruption()
    test_int_encoding()
    test_status_decode()
    print("✅ All qpt frame tests passed")
