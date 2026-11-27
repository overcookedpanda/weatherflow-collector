"""Exercise the optional output wiring without starting collectors or servers."""

import asyncio
import importlib.util
from pathlib import Path
import runpy
from unittest.mock import AsyncMock, Mock, call

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def entrypoint(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC))
    monkeypatch.setenv("WEATHERFLOW_COLLECTOR_API_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location(
        "weatherflow_collector_main", SRC / "weatherflow-collector.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "influx_enabled, prometheus_enabled, websocket_enabled",
    [(True, False, False), (False, True, False), (True, True, True)],
    ids=["influx-default", "prometheus-only", "all-outputs"],
)
def test_setup_subscribes_and_starts_enabled_outputs(
    monkeypatch, entrypoint, influx_enabled, prometheus_enabled, websocket_enabled
):
    # Keep the real configuration module intact and disable unrelated services.
    settings = {
        name: False
        for name in vars(entrypoint.config)
        if name.startswith("WEATHERFLOW_COLLECTOR_") and name.endswith("_ENABLED")
    }
    settings.update(
        WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED=influx_enabled,
        WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_ENABLED=prometheus_enabled,
        WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ENABLED=websocket_enabled,
    )
    for name, value in settings.items():
        monkeypatch.setattr(entrypoint.config, name, value)

    events = Mock()
    influx = Mock()
    prometheus = Mock(start=AsyncMock())
    websocket = Mock(start_server=AsyncMock())
    for name, replacement in {
        "validate_all": Mock(return_value=True),
        "StationMetadataManager": Mock(),
        "EventManager": Mock(return_value=events),
        "CollectorDataProcessor": Mock(),
        "InfluxDBStorage": Mock(return_value=influx),
        "PrometheusStorage": Mock(return_value=prometheus),
        "WebSocketServerProvider": Mock(return_value=websocket),
    }.items():
        monkeypatch.setattr(entrypoint, name, replacement)

    async def scenario():
        await entrypoint.setup_app()
        # Let the scheduled websocket startup run.
        await asyncio.sleep(0)

    asyncio.run(scenario())

    subscriptions = events.subscribe.call_args_list
    assert [item for item in subscriptions if item.args[0] == "influxdb_storage_event"] == [
        call("influxdb_storage_event", output)
        for enabled, output in [(influx_enabled, influx), (prometheus_enabled, prometheus)]
        if enabled
    ]
    assert [item for item in subscriptions if item.args[0] == "processed_data_event"] == (
        [call("processed_data_event", websocket)] if websocket_enabled else []
    )
    assert prometheus.start.await_count == int(prometheus_enabled)
    assert websocket.start_server.await_count == int(websocket_enabled)


def test_main_propagates_startup_failure(monkeypatch, entrypoint):
    error = OSError("scrape endpoint could not bind")
    monkeypatch.setattr(entrypoint, "setup_app", AsyncMock(side_effect=error))

    with pytest.raises(OSError) as raised:
        asyncio.run(entrypoint.main_async())

    assert raised.value is error


def test_output_environment_normalizes_values_and_adapts_to_slow_polling(monkeypatch):
    prometheus = "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_"
    websocket = "WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_"
    for key in ("STALE_AFTER", "MAX_SAMPLE_AGE"):
        monkeypatch.delenv(prometheus + key, raising=False)
    for key, value in {
        prometheus + "SCRAPE_ENABLED": " TrUe ",
        prometheus + "REMOTE_WRITE_ENABLED": "false",
        prometheus + "DROP_LABELS": " station_latitude, , station_longitude ",
        prometheus + "REMOTE_WRITE_AUTH": " BASIC ",
        websocket + "ORIGINS": " https://grafana.example/, , https://other.example/ ",
        "WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_INTERVAL": "700",
        "WEATHERFLOW_COLLECTOR_COLLECTOR_REST_FORECASTS_FETCH_INTERVAL": "1200",
    }.items():
        monkeypatch.setenv(key, value)

    settings = runpy.run_path(str(SRC / "config.py"))

    assert settings[prometheus + "ENABLED"] is True
    assert settings[prometheus + "SCRAPE_ENABLED"] is True
    assert settings[prometheus + "REMOTE_WRITE_ENABLED"] is False
    assert settings[prometheus + "DROP_LABELS"] == ["station_latitude", "station_longitude"]
    assert settings[prometheus + "REMOTE_WRITE_AUTH"] == "basic"
    assert settings[prometheus + "MAX_SAMPLE_AGE"] == 1400
    assert settings[prometheus + "STALE_AFTER"] == 2400
    assert settings[websocket + "ORIGINS"] == ["https://grafana.example", "https://other.example"]
