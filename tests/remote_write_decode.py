"""Test-only decoders for snappy blocks and remote write protobufs.

They implement the full snappy block format (literals and copies), so the
tests would also accept a real compressor's output.
"""

import struct


def read_varint(data, pos):
    shift = result = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def snappy_decompress(data):
    length, pos = read_varint(data, 0)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:  # literal
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                n = int.from_bytes(data[pos : pos + extra], "little")
                pos += extra
            n += 1
            out += data[pos : pos + n]
            pos += n
            continue
        if kind == 1:
            n = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:
            n = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 2], "little")
            pos += 2
        else:
            n = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        for _ in range(n):
            out.append(out[-offset])
    assert len(out) == length, "snappy length mismatch"
    return bytes(out)


def _fields(data):
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = read_varint(data, pos)
        elif wire_type == 1:
            value = struct.unpack("<d", data[pos : pos + 8])[0]
            pos += 8
        elif wire_type == 2:
            n, pos = read_varint(data, pos)
            value = data[pos : pos + n]
            pos += n
        else:
            raise ValueError(f"unexpected wire type {wire_type}")
        yield number, value


def decode_write_request(data):
    """Return [(labels_list, [(value, ts_ms), ...]), ...]."""
    series = []
    for number, ts_bytes in _fields(data):
        assert number == 1
        labels, samples = [], []
        for inner_number, inner in _fields(ts_bytes):
            if inner_number == 1:
                parts = dict(_fields(inner))
                labels.append((parts[1].decode(), parts[2].decode()))
            elif inner_number == 2:
                parts = dict(_fields(inner))
                samples.append((parts.get(1, 0.0), parts.get(2, 0)))
        series.append((labels, samples))
    return series
