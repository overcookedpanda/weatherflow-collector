"""Prometheus remote write 1.0 payload encoding, standard library only.

A remote write body is a snappy-compressed protobuf WriteRequest:

    message WriteRequest { repeated TimeSeries timeseries = 1; }
    message TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
    message Label        { string name = 1; string value = 2; }
    message Sample       { double value = 1; int64 timestamp = 2; }

The messages are small enough to encode by hand, which keeps protobuf and a
native snappy library out of the image.
"""

import struct

CONTENT_TYPE = "application/x-protobuf"
CONTENT_ENCODING = "snappy"
PROTOCOL_VERSION = "0.1.0"


def _varint(value):
    if value < 0:
        value &= (1 << 64) - 1  # int64 two's complement, as protobuf does
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number, wire_type):
    return _varint((number << 3) | wire_type)


def _length_delimited(number, payload):
    return _field(number, 2) + _varint(len(payload)) + payload


def encode_label(name, value):
    return _length_delimited(1, name.encode("utf-8")) + _length_delimited(
        2, value.encode("utf-8")
    )


def encode_sample(value, timestamp_ms):
    return (
        _field(1, 1)
        + struct.pack("<d", float(value))
        + _field(2, 0)
        + _varint(int(timestamp_ms))
    )


def encode_write_request(series):
    """Encode [(labels, [(value, timestamp_ms), ...]), ...] as a WriteRequest.

    labels is a dict that must include __name__. Labels are sent sorted by
    name and samples sorted by time, as the protocol requires.
    """
    out = bytearray()
    for labels, samples in series:
        body = bytearray()
        for name in sorted(labels):
            body += _length_delimited(1, encode_label(name, labels[name]))
        for value, timestamp_ms in sorted(samples, key=lambda s: s[1]):
            body += _length_delimited(2, encode_sample(value, timestamp_ms))
        out += _length_delimited(1, bytes(body))
    return bytes(out)


def snappy_compress(data):
    """Return data in the snappy block format, as literals only.

    The format lets an encoder emit only literal elements, and every snappy
    decoder accepts that. It doesn't shrink the payload, but a remote write
    batch from one weather station is a few kilobytes, and it means no native
    compression library in the image.
    """
    out = bytearray(_varint(len(data)))
    chunk_size = 1 << 16
    for start in range(0, len(data), chunk_size):
        chunk = data[start : start + chunk_size]
        n = len(chunk) - 1
        if n < 60:
            out.append(n << 2)
        elif n < 1 << 8:
            out.append(60 << 2)
            out += n.to_bytes(1, "little")
        else:
            out.append(61 << 2)
            out += n.to_bytes(2, "little")
        out += chunk
    return bytes(out)
