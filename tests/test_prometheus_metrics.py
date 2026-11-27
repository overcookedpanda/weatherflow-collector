"""Tests for mapping storage events to Prometheus series."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from storage.prometheus_metrics import (  # noqa: E402
    MetricStore,
    label_name,
    metric_name,
)

NOW = 1_700_000_000.0
STATION_TAGS = {
    "collector_type": "collector_udp",
    "station_name": "Test Station",
    "station_latitude": 1.5,
    "station_longitude": 2.5,
    "station_elevation": 100,
    "station_time_zone": "UTC",
}
DROP = ["station_latitude", "station_longitude", "station_elevation", "station_time_zone"]


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def make_store(clock=None, **kwargs):
    kwargs.setdefault("drop_labels", DROP)
    return MetricStore(clock=clock or Clock(), **kwargs)


def series(store):
    """{(name, labels_dict_items): value} for easy assertions."""
    return {
        (name, labels): value
        for (name, labels), (value, _o, _u) in store._latest.items()
    }


def test_names_are_sanitized():
    assert metric_name("weatherflow_obs", "air temperature") == "weatherflow_obs_air_temperature"
    assert label_name("station-name") == "station_name"
    assert label_name("1abc") == "_1abc"
    assert label_name("__name__") == "tag__name__"


def test_numeric_fields_become_gauges_and_strings_are_skipped():
    store = make_store()
    store.add_point(
        "weatherflow_obs",
        STATION_TAGS,
        {"air_temperature": 21.5, "lightning": True, "note": "x", "timestamp": NOW, "bad": float("nan")},
        NOW - 5,
    )
    names = {name for name, _ in series(store)}
    assert names == {"weatherflow_obs_air_temperature", "weatherflow_obs_lightning"}


def test_location_labels_are_dropped_by_default():
    store = make_store()
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 21.5}, NOW)
    (_name, labels), = series(store)
    assert dict(labels) == {"collector_type": "collector_udp", "station_name": "Test Station"}


def test_primary_source_gets_a_second_series():
    store = make_store(primary_source="collector_udp")
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 21.5}, NOW)
    collector_types = sorted(dict(labels)["collector_type"] for _n, labels in series(store))
    assert collector_types == ["collector_udp", "primary"]


def test_old_observations_are_skipped_and_older_never_overwrites_newer():
    store = make_store(max_sample_age=600)
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 1.0}, NOW - 3600)
    assert series(store) == {}
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 2.0}, NOW - 10)
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 3.0}, NOW - 60)
    assert list(series(store).values()) == [2.0]


def test_hourly_forecast_uses_hours_ahead():
    store = make_store()
    tags = {**STATION_TAGS, "collector_type": "collector_forecasts", "number_of_days_out": 1, "local_day": 15}
    store.add_batch(
        [
            ("weatherflow_forecast_hourly", tags, {"air_temperature": 10.0}, NOW - 3600),
            ("weatherflow_forecast_hourly", tags, {"air_temperature": 11.0}, NOW + 3600),
            ("weatherflow_forecast_hourly", tags, {"air_temperature": 12.0}, NOW + 3 * 3600),
        ]
    )
    found = {dict(labels)["hours_ahead"]: value for (_n, labels), value in series(store).items()}
    assert found == {"1": 11.0, "3": 12.0}
    for _name, labels in series(store):
        assert "number_of_days_out" not in dict(labels)
        assert "local_day" not in dict(labels)


def test_daily_forecast_uses_days_ahead():
    store = make_store()
    tags = {"collector_type": "collector_forecasts", "station_name": "Test Station"}
    store.add_batch(
        [
            ("weatherflow_forecast_daily", tags, {"air_temp_high": 20.0}, NOW + 3600),
            ("weatherflow_forecast_daily", tags, {"air_temp_high": 22.0}, NOW + 86400 + 3600),
        ]
    )
    found = {dict(labels)["days_ahead"]: value for (_n, labels), value in series(store).items()}
    assert found == {"0": 20.0, "1": 22.0}


def test_stats_batch_settles_on_latest_period():
    store = make_store()
    tags = {"collector_type": "collector_stats", "station_name": "Test Station", "time_period": "Daily"}
    store.add_batch(
        [
            ("weatherflow_stats", tags, {"temperature_high": 30.0}, NOW + 3600),  # today
            ("weatherflow_stats", tags, {"temperature_high": 25.0}, NOW - 86400 * 2),
            ("weatherflow_stats", tags, {"temperature_high": 27.0}, NOW - 86400),
        ]
    )
    assert list(series(store).values()) == [30.0]


def test_exposition_format():
    store = make_store()
    store.add_point(
        "weatherflow_obs",
        {"collector_type": "collector_udp", "station_name": 'Odd "name"\\'},
        {"air_temperature": 21.5},
        NOW,
    )
    text = store.exposition()
    assert text == (
        "# TYPE weatherflow_obs_air_temperature gauge\n"
        'weatherflow_obs_air_temperature{collector_type="collector_udp",'
        'station_name="Odd \\"name\\"\\\\"} 21.5\n'
    )


def test_stale_series_are_pruned():
    clock = Clock()
    store = make_store(clock=clock, stale_after=900)
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 21.5}, NOW)
    clock.now += 901
    assert store.exposition() == ""


def test_series_cap():
    store = make_store(max_series=2)
    store.add_point("weatherflow_obs", STATION_TAGS, {"a": 1, "b": 2, "c": 3}, NOW)
    assert store.series_count() == 2


def test_drain_dedupes_and_drops_out_of_order():
    clock = Clock()
    store = make_store(clock=clock, queue_samples=True)
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 1.0}, NOW - 2)
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 2.0}, NOW - 1)
    drained = store.drain()
    assert len(drained) == 1
    labels, samples = drained[0]
    assert labels["__name__"] == "weatherflow_obs_air_temperature"
    assert "station_latitude" not in labels
    assert samples == [(1.0, int((NOW - 2) * 1000)), (2.0, int((NOW - 1) * 1000))]
    assert store.drain() == []


def test_requeue_retries_failed_samples():
    store = make_store(queue_samples=True)
    store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": 1.0}, NOW)
    first = store.drain()
    store.requeue(first)
    assert store.drain() == first


def test_queue_is_bounded():
    clock = Clock()
    store = make_store(clock=clock, queue_samples=True, max_queued_samples=10)
    for i in range(50):
        store.add_point("weatherflow_obs", STATION_TAGS, {"air_temperature": float(i)}, NOW - 50 + i)
    assert len(store._queue) <= 10
