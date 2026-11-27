"""Tests for the opt-in websocket server provider."""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("WEATHERFLOW_COLLECTOR_API_TOKEN", "test-token")

import config  # noqa: E402
from provider.websocket_server import WebSocketServerProvider  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import InvalidStatus  # noqa: E402

DEVICE_ID = 1001
SERIAL = "ST-00000001"
METADATA = {
    "1": {
        "name": "Test Station",
        "enabled": True,
        "devices": [
            {
                "device_id": DEVICE_ID,
                "device_type": "ST",
                "serial_number": SERIAL,
                "enabled": True,
            },
            {"device_id": 2002, "device_type": "ST", "enabled": False},
        ],
    }
}


class FakeEventManager:
    is_metric_event_processing = False

    def __init__(self):
        self.published = []

    async def publish(self, event_type, data, publisher=None):
        self.published.append((event_type, data))


def rapid_wind(collector_type="collector_udp"):
    data = {"type": "rapid_wind", "ob": [1700000000, 3.2, 180]}
    if collector_type == "collector_udp":
        data["serial_number"] = SERIAL
    else:
        data["device_id"] = DEVICE_ID
    return {
        "metadata": {"collector_type": collector_type},
        "data": data,
        "station_info": {"station_name": "Test Station"},
    }


def test_off_by_default_and_bound_to_localhost():
    if "WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ENABLED" not in os.environ:
        assert config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ENABLED is False
    if "WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_HOST" not in os.environ:
        assert config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_HOST == "127.0.0.1"
    assert config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_PORT == 6789


def test_parse_path():
    provider = WebSocketServerProvider(FakeEventManager(), station_metadata=METADATA)
    assert provider.parse_path(f"/collector_udp/{DEVICE_ID}") == (
        "collector_udp",
        DEVICE_ID,
    )
    assert provider.parse_path(f"/collector_websocket/{DEVICE_ID}/") == (
        "collector_websocket",
        DEVICE_ID,
    )
    assert provider.parse_path("/") is None
    assert provider.parse_path("/collector_udp/not-a-number") is None
    assert provider.parse_path("/collector_udp/2002") is None  # disabled device
    assert provider.parse_path(f"/something_else/{DEVICE_ID}") is None


def test_message_keeps_upstream_shape():
    provider = WebSocketServerProvider(FakeEventManager(), station_metadata=METADATA)
    message = json.loads(provider.format_rapid_wind_data(rapid_wind()))
    reading = message["rapid_wind"]["collector_udp"]["Test Station"]
    assert reading["wind_speed"] == 3.2
    assert reading["wind_direction"] == 180
    assert reading["timestamp"] == 1700000000


def test_broadcasts_to_subscribed_client_only():
    async def scenario():
        events = FakeEventManager()
        provider = WebSocketServerProvider(events, station_metadata=METADATA)
        provider.host, provider.port = "127.0.0.1", 0
        server_task = asyncio.create_task(provider.start_server())
        for _ in range(100):
            if provider.server is not None:
                break
            await asyncio.sleep(0.01)
        port = provider.server.sockets[0].getsockname()[1]
        base = f"ws://127.0.0.1:{port}"
        try:
            async with connect(f"{base}/collector_udp/{DEVICE_ID}") as subscribed, \
                    connect(f"{base}/") as unsubscribed:
                for _ in range(100):
                    if provider.connection_count() == 2:
                        break
                    await asyncio.sleep(0.01)
                provider.update(rapid_wind())
                message = json.loads(await asyncio.wait_for(subscribed.recv(), 2))
                assert "collector_udp" in message["rapid_wind"]
                try:
                    await asyncio.wait_for(unsubscribed.recv(), 0.2)
                    raise AssertionError("unsubscribed client got a message")
                except asyncio.TimeoutError:
                    pass
                for _ in range(100):
                    if events.published:
                        break
                    await asyncio.sleep(0.01)
        finally:
            provider.server.close()
            server_task.cancel()
        assert any(event == "system_metrics_event" for event, _ in events.published)

    asyncio.run(scenario())


def test_refuses_connections_over_the_limit(monkeypatch):
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_MAX_CONNECTIONS", 0
    )

    class FakeConnection:
        closed_with = None

        async def close(self, code, reason):
            self.closed_with = code

    async def scenario():
        provider = WebSocketServerProvider(FakeEventManager(), station_metadata=METADATA)
        connection = FakeConnection()
        assert await provider.register(connection, "/") is False
        assert connection.closed_with == 1013
        assert provider.connection_count() == 0

    asyncio.run(scenario())


def test_browser_origins_are_refused_unless_listed(monkeypatch):
    monkeypatch.setattr(
        config,
        "WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ORIGINS",
        ["https://grafana.example"],
    )
    assert WebSocketServerProvider.allowed_origins() == [None, "https://grafana.example"]

    async def scenario():
        provider = WebSocketServerProvider(FakeEventManager(), station_metadata=METADATA)
        provider.host, provider.port = "127.0.0.1", 0
        server_task = asyncio.create_task(provider.start_server())
        for _ in range(100):
            if provider.server is not None:
                break
            await asyncio.sleep(0.01)
        port = provider.server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/collector_udp/{DEVICE_ID}"
        try:
            async with connect(url):
                pass  # no Origin header, like a server-side client
            async with connect(url, origin="https://grafana.example"):
                pass
            refused = False
            try:
                async with connect(url, origin="https://other.example"):
                    pass
            except InvalidStatus as e:
                refused = e.response.status_code == 403
            assert refused
        finally:
            provider.server.close()
            server_task.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "collector, changes",
    [
        ("collector_udp", {"serial_number": "unknown"}),
        ("collector_websocket", {"device_id": None}),
        ("collector_websocket", {"ob": [1700000000, 3.2]}),
        ("collector_websocket", {"type": "obs_st"}),
        ("collector_rest", {}),
    ],
    ids=["unknown-serial", "missing-device", "incomplete", "other-message", "other-source"],
)
def test_update_ignores_data_that_cannot_be_broadcast(monkeypatch, collector, changes):
    provider = WebSocketServerProvider(FakeEventManager(), station_metadata=METADATA)
    broadcast = AsyncMock()
    monkeypatch.setattr(provider, "broadcast", broadcast)
    event = rapid_wind(collector)
    event["data"].update(changes)

    async def scenario():
        provider.update(event)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    broadcast.assert_not_called()


def test_broadcast_continues_after_one_subscriber_send_fails():
    class RecordingEventManager(FakeEventManager):
        async def publish(self, event_type, data, publisher=None):
            await super().publish(event_type, data, publisher)
            published.set()

    published = asyncio.Event()
    events = RecordingEventManager()
    provider = WebSocketServerProvider(events, station_metadata=METADATA)
    collector = "collector_websocket"
    healthy = AsyncMock()
    failing = AsyncMock()
    failing.send.side_effect = OSError("connection lost")
    udp_client = AsyncMock()

    async def scenario():
        connections = (
            (healthy, collector), (failing, collector), (udp_client, "collector_udp")
        )
        for connection, source in connections:
            assert await provider.register(connection, f"/{source}/{DEVICE_ID}")
        provider.update(rapid_wind(collector))
        await asyncio.wait_for(published.wait(), timeout=1)

    asyncio.run(scenario())

    healthy.send.assert_awaited_once()
    message = json.loads(healthy.send.await_args.args[0])
    assert message["rapid_wind"][collector]["Test Station"]["wind_speed"] == 3.2
    failing.send.assert_awaited_once()
    udp_client.send.assert_not_called()
    (event, metrics), = events.published
    assert event == "system_metrics_event"
    assert metrics["metric_name"] == "broadcast_collector_websocket"
    assert (metrics["rate"], metrics["errors"], metrics["client_count"]) == (1, 1, 2)
