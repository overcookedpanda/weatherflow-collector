# test_forecast_and_websocket_fixes.py

"""
Tests for two collector fixes:

- Hourly forecasts no longer keep the API's "time" value as a field. InfluxDB
  reserves that name and answered every write with a 422.
- A websocket timeout reconnects once. monitor_connection used to close and
  reconnect while the receive loop reconnected too, opening a second socket
  (and a second set of subscriptions) on every timeout.

No network access is required.
"""

import asyncio
import os
import sys
from unittest import mock

import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# config.py requires the API token at import time; no network calls are made
os.environ.setdefault("WEATHERFLOW_COLLECTOR_API_TOKEN", "test-token")

from collector.websocket import WebsocketCollector  # noqa: E402
from handlers.rest_forecasts import RESTForecastsHandler  # noqa: E402


def _hourly_payload():
    return {
        "metadata": {"station_id": 12345},
        "station_info": {"elevation": 100.0, "time_zone": "UTC"},
        "data": {
            "forecast": {
                "hourly": [
                    {
                        "time": 1893456000,
                        "air_temperature": 10.0,
                        "relative_humidity": 50,
                        "sea_level_pressure": 1013.0,
                        "wind_avg": 2.0,
                    },
                    {
                        "time": 1893459600,
                        "air_temperature": 11.0,
                        "relative_humidity": 48,
                        "sea_level_pressure": 1012.0,
                        "wind_avg": 3.0,
                    },
                ]
            }
        },
    }


def test_hourly_forecast_points_have_no_time_field():
    event_manager = mock.AsyncMock()
    handler = RESTForecastsHandler(event_manager)

    asyncio.run(handler.handle_forecast_hourly(_hourly_payload()))

    event_manager.publish.assert_awaited_once()
    event_type, batch = event_manager.publish.await_args.args
    assert event_type == "influxdb_storage_event"
    points = batch["batch_data"]
    assert points
    for measurement, _tags, fields, timestamp in points:
        assert "time" not in fields, measurement
        assert timestamp in (1893456000, 1893459600)


def _collector():
    return WebsocketCollector(mock.AsyncMock())


def test_monitor_timeout_closes_without_reconnecting():
    collector = _collector()
    collector.timeout_duration = 1
    collector.last_message_time = 0  # long past the timeout
    collector.close_connection = mock.AsyncMock()
    collector.establish_connection = mock.AsyncMock()

    async def run_one_check():
        with mock.patch(
            "collector.websocket.asyncio.sleep", side_effect=asyncio.CancelledError
        ):
            try:
                await collector.monitor_connection()
            except asyncio.CancelledError:
                pass

    asyncio.run(run_one_check())

    collector.close_connection.assert_awaited_once()
    collector.establish_connection.assert_not_awaited()


def test_closed_socket_reconnects_once_and_first_failure_does_not_crash():
    collector = _collector()
    collector.websocket = mock.AsyncMock()
    # First recv fails before any message (the finally block used to raise
    # UnboundLocalError here), then the loop is stopped.
    collector.websocket.recv.side_effect = [
        websockets.ConnectionClosedOK(None, None),
        asyncio.CancelledError(),
    ]
    collector.establish_connection = mock.AsyncMock()

    async def run():
        with mock.patch(
            "collector.websocket.utils.async_publish_metrics", new=mock.AsyncMock()
        ):
            try:
                await collector.receive_and_forward_messages()
            except asyncio.CancelledError:
                pass

    asyncio.run(run())

    collector.establish_connection.assert_awaited_once()
