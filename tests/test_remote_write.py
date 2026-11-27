"""Tests for the hand-written remote write encoder and snappy framing."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from remote_write_decode import decode_write_request, snappy_decompress  # noqa: E402
from utils import remote_write  # noqa: E402


def test_snappy_round_trip_across_literal_sizes():
    for size in (0, 1, 59, 60, 61, 255, 256, 257, 65535, 65536, 65537, 200000):
        data = bytes((i * 7) % 251 for i in range(size))
        assert snappy_decompress(remote_write.snappy_compress(data)) == data


def test_write_request_round_trip():
    series = [
        (
            {"__name__": "weatherflow_obs_air_temperature", "station_name": "Test"},
            [(21.5, 1700000001000), (21.25, 1700000000000)],
        ),
        ({"__name__": "up", "job": "x"}, [(1.0, 1700000000000)]),
    ]
    body = remote_write.snappy_compress(remote_write.encode_write_request(series))
    decoded = decode_write_request(snappy_decompress(body))
    assert decoded[0][0] == [
        ("__name__", "weatherflow_obs_air_temperature"),
        ("station_name", "Test"),
    ]
    # samples come out in time order
    assert decoded[0][1] == [(21.25, 1700000000000), (21.5, 1700000001000)]
    assert decoded[1] == ([("__name__", "up"), ("job", "x")], [(1.0, 1700000000000)])


def test_labels_are_sorted_and_utf8():
    body = remote_write.encode_write_request(
        [({"z": "é", "__name__": "m", "a": "1"}, [(0.0, 0)])]
    )
    labels = decode_write_request(body)[0][0]
    assert [name for name, _ in labels] == ["__name__", "a", "z"]
    assert dict(labels)["z"] == "é"
