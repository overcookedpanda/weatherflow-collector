"""Tests for the Prometheus scrape endpoint and remote write push."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("WEATHERFLOW_COLLECTOR_API_TOKEN", "test-token")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import config  # noqa: E402
import config_validator  # noqa: E402
from remote_write_decode import decode_write_request, snappy_decompress  # noqa: E402
from storage.prometheus import PrometheusStorage  # noqa: E402
from storage.prometheus_metrics import MetricStore  # noqa: E402


class FakeEventManager:
    is_metric_event_processing = False

    async def publish(self, event_type, data, publisher=None):
        pass


def reading(now):
    return {
        "data_type": "single",
        "measurement": "weatherflow_obs",
        "tags": {"collector_type": "collector_udp", "station_name": "Test Station"},
        "fields": {"air_temperature": 21.5},
        "timestamp": now,
    }


def make_storage(queue_samples=False):
    store = MetricStore(queue_samples=queue_samples)
    return PrometheusStorage(FakeEventManager(), store=store)


def test_off_by_default():
    for name in (
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_ENABLED",
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED",
    ):
        if name not in os.environ:
            assert getattr(config, name) is False
    if "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_HOST" not in os.environ:
        assert config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_HOST == "127.0.0.1"
    # InfluxDB stays the default output.
    if "WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED" not in os.environ:
        assert config.WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED is True


def test_scrape_endpoint_serves_metrics():
    async def scenario():
        import time

        storage = make_storage()
        storage.update(reading(time.time()))
        site = await storage.start_scrape_endpoint(host="127.0.0.1", port=0)
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/metrics") as response:
                    assert response.status == 200
                    assert response.headers["Content-Type"].startswith("text/plain")
                    body = await response.text()
        finally:
            await storage.close()
        assert "# TYPE weatherflow_obs_air_temperature gauge" in body
        assert 'station_name="Test Station"' in body

    asyncio.run(scenario())


async def _receiver(statuses):
    """Start a fake remote write receiver; returns (runner, url, received)."""
    received = []

    async def handle(request):
        received.append((dict(request.headers), await request.read()))
        return web.Response(status=statuses.pop(0) if statuses else 204)

    app = web.Application()
    app.router.add_post("/api/v1/write", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/api/v1/write", received


def test_remote_write_sends_a_valid_payload(monkeypatch):
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "bearer"
    )
    monkeypatch.setattr(
        config,
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_BEARER_TOKEN",
        "example-token",
    )

    async def scenario():
        import time

        now = time.time()
        storage = make_storage(queue_samples=True)
        storage.update(reading(now))
        runner, url, received = await _receiver([])
        try:
            async with aiohttp.ClientSession() as session:
                assert await storage.push_once(session, url=url) == 1
        finally:
            await runner.cleanup()
        headers, body = received[0]
        assert headers["Content-Encoding"] == "snappy"
        assert headers["Content-Type"] == "application/x-protobuf"
        assert headers["X-Prometheus-Remote-Write-Version"] == "0.1.0"
        assert headers["Authorization"] == "Bearer example-token"
        (labels, samples), = decode_write_request(snappy_decompress(body))
        assert ("__name__", "weatherflow_obs_air_temperature") in labels
        assert samples == [(21.5, int(now * 1000))]

    asyncio.run(scenario())


def test_remote_write_retries_server_errors_and_drops_rejections(monkeypatch):
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "none"
    )

    async def scenario():
        import time

        storage = make_storage(queue_samples=True)
        storage.update(reading(time.time()))
        runner, url, received = await _receiver([503, 204])
        try:
            async with aiohttp.ClientSession() as session:
                assert await storage.push_once(session, url=url) == 0  # 503, kept
                assert await storage.push_once(session, url=url) == 1  # retried
                assert await storage.push_once(session, url=url) == 0  # nothing left
        finally:
            await runner.cleanup()
        assert len(received) == 2
        assert received[0][1] == received[1][1]

        storage = make_storage(queue_samples=True)
        storage.update(reading(time.time()))
        runner, url, received = await _receiver([400])
        try:
            async with aiohttp.ClientSession() as session:
                assert await storage.push_once(session, url=url) == 0
                assert await storage.push_once(session, url=url) == 0  # dropped
        finally:
            await runner.cleanup()
        assert len(received) == 1

    asyncio.run(scenario())


def test_sigv4_auth_headers(monkeypatch):
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "sigv4"
    )
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_SIGV4_REGION", "us-east-1"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    storage = make_storage()
    headers = storage.auth_headers(
        "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-example/api/v1/remote_write",
        {"Content-Type": "application/x-protobuf"},
        b"body",
    )
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/aps/aws4_request" in headers["Authorization"]
    assert "X-Amz-Date" in headers


def test_validator(monkeypatch):
    set_ = lambda name, value: monkeypatch.setattr(config, name, value)  # noqa: E731
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED", False)
    assert config_validator.validate_prometheus_config() is True

    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED", True)
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL", "")
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "none")
    assert config_validator.validate_prometheus_config() is False

    set_(
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL",
        "http://example.com/api/v1/write",
    )
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "bearer")
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_BEARER_TOKEN", "x")
    assert config_validator.validate_prometheus_config() is False  # http with creds

    set_(
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL",
        "https://example.com/api/v1/write",
    )
    assert config_validator.validate_prometheus_config() is True

    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "sigv4")
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_SIGV4_REGION", "")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    assert config_validator.validate_prometheus_config() is False


def test_influxdb_settings_not_required_when_influxdb_is_off(monkeypatch):
    monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED", False)
    monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_INFLUXDB_URL", None)
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED", False
    )
    assert config_validator.validate_all() is True
