"""Tests for the Prometheus scrape endpoint and remote write push."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("WEATHERFLOW_COLLECTOR_API_TOKEN", "test-token")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import config  # noqa: E402
import config_validator  # noqa: E402
from remote_write_decode import decode_write_request, snappy_decompress  # noqa: E402
import storage.prometheus as prometheus_module  # noqa: E402
from storage.prometheus import PrometheusStorage  # noqa: E402
from storage.prometheus_metrics import MetricStore  # noqa: E402

TEMPERATURE = "weatherflow_obs_air_temperature"


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


async def _receiver(statuses, body=b"", headers=None):
    """Start a fake remote write receiver on loopback; returns (runner, url, received)."""
    received = []

    async def handle(request):
        received.append((dict(request.headers), await request.read()))
        status = statuses.pop(0) if statuses else 204
        return web.Response(status=status, body=body if status >= 300 else b"", headers=headers)

    async def elsewhere(request):
        received.append(("redirect target", await request.read()))
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/api/v1/write", handle)
    app.router.add_post("/elsewhere", elsewhere)
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
                # The reading and its observed-timestamp series.
                assert await storage.push_once(session, url=url) == 2
        finally:
            await runner.cleanup()
        headers, body = received[0]
        assert headers["Content-Encoding"] == "snappy"
        assert headers["Content-Type"] == "application/x-protobuf"
        assert headers["X-Prometheus-Remote-Write-Version"] == "0.1.0"
        assert headers["Authorization"] == "Bearer example-token"
        decoded = {
            dict(labels)["__name__"]: samples
            for labels, samples in decode_write_request(snappy_decompress(body))
        }
        assert decoded[TEMPERATURE] == [(21.5, int(now * 1000))]
        assert decoded["weatherflow_obs_observed_timestamp_seconds"] == [
            (now, int(now * 1000))
        ]

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
                assert await storage.push_once(session, url=url) == 2  # retried
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


def test_remote_write_does_not_follow_redirects(monkeypatch):
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "none"
    )

    async def scenario():
        import time

        storage = make_storage(queue_samples=True)
        storage.update(reading(time.time()))
        runner, url, received = await _receiver([307], headers={"Location": "/elsewhere"})
        try:
            async with aiohttp.ClientSession() as session:
                assert await storage.push_once(session, url=url) == 0
        finally:
            await runner.cleanup()
        assert [entry[0] for entry in received if entry[0] == "redirect target"] == []
        assert len(received) == 1

    asyncio.run(scenario())


def test_undecodable_error_body_still_retries(monkeypatch):
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "none"
    )

    async def scenario():
        import time

        storage = make_storage(queue_samples=True)
        storage.update(reading(time.time()))
        runner, url, received = await _receiver(
            [503, 204], body=b"\xff\xfe not utf-8", headers={"Content-Type": "text/plain; charset=utf-8"}
        )
        try:
            async with aiohttp.ClientSession() as session:
                assert await storage.push_once(session, url=url) == 0
                assert await storage.push_once(session, url=url) == 2
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_close_stops_the_remote_write_loop(monkeypatch):
    monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_INTERVAL", 3600)

    async def scenario():
        storage = make_storage(queue_samples=True)
        storage.scrape_enabled = False
        storage.remote_write_enabled = True
        await storage.start()
        task = storage.remote_write_task
        assert task is not None and not task.done()
        await storage.close()
        assert task.cancelled()

    asyncio.run(scenario())


def test_scrape_bind_failure_stops_startup_when_it_is_the_only_output(monkeypatch):
    monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED", False)

    async def scenario():
        blocker = make_storage()
        site = await blocker.start_scrape_endpoint(host="127.0.0.1", port=0)
        port = site._server.sockets[0].getsockname()[1]
        storage = make_storage()
        storage.scrape_enabled = True
        storage.remote_write_enabled = False
        monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_HOST", "127.0.0.1")
        monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_PORT", port)
        try:
            raised = False
            try:
                await storage.start()
            except OSError:
                raised = True
            assert raised
            monkeypatch.setattr(config, "WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED", True)
            await storage.start()  # InfluxDB still runs, so carry on
        finally:
            await storage.close()
            await blocker.close()

    asyncio.run(scenario())


def test_validator_rejects_credentials_in_the_url(monkeypatch):
    set_ = lambda name, value: monkeypatch.setattr(config, name, value)  # noqa: E731
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED", True)
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "none")
    set_(
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL",
        "http://user:example-secret@example.com/api/v1/write",
    )
    assert config_validator.validate_prometheus_config() is False
    set_(
        "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL",
        "http://receiver.example:9009/api/v1/push",
    )
    assert config_validator.validate_prometheus_config() is True


def test_age_limits_are_checked_against_poll_intervals(monkeypatch):
    set_ = lambda name, value: monkeypatch.setattr(config, name, value)  # noqa: E731
    set_("WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_INTERVAL", 60)
    set_("WEATHERFLOW_COLLECTOR_COLLECTOR_REST_FORECASTS_FETCH_INTERVAL", 60)
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_MAX_SAMPLE_AGE", 600)
    set_("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_STALE_AFTER", 900)
    assert config_validator.warn_prometheus_ages() == []
    set_("WEATHERFLOW_COLLECTOR_COLLECTOR_REST_FORECASTS_FETCH_INTERVAL", 1800)
    set_("WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_INTERVAL", 900)
    assert len(config_validator.warn_prometheus_ages()) == 2


@pytest.mark.parametrize(
    "failure, resend_failed", [(429, True), (400, False), (TimeoutError(), True)],
    ids=["rate-limited", "rejected", "transport-timeout"],
)
def test_partial_push_preserves_unsent_chunks(monkeypatch, failure, resend_failed):
    monkeypatch.setattr(prometheus_module, "MAX_SERIES_PER_REQUEST", 1)
    monkeypatch.setattr(
        config, "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH", "none"
    )
    storage = PrometheusStorage(
        FakeEventManager(), store=MetricStore(queue_samples=True, clock=lambda: 1000)
    )
    expected = [
        ([("__name__", f"weatherflow_stats_{field}")], [(value, 1_000_000)])
        for field, value in [("minimum", 10.0), ("mean", 20.0), ("maximum", 30.0)]
    ]
    storage.update({
        "measurement": "weatherflow_stats",
        "fields": {"minimum": 10, "mean": 20, "maximum": 30},
    })

    def response(status):
        context = MagicMock()
        context.__aenter__.return_value = Mock(
            status=status, content=Mock(read=AsyncMock(return_value=b""))
        )
        return context

    failed_post = response(failure) if isinstance(failure, int) else failure
    pending = expected[1:] if resend_failed else expected[2:]
    session = Mock()
    session.post.side_effect = [response(204), failed_post] + [response(204) for _ in pending]

    async def scenario():
        url = "http://receiver.example/api/v1/write"
        assert await storage.push_once(session, url=url) == 1
        assert session.post.call_count == 2
        assert await storage.push_once(session, url=url) == len(pending)
        assert await storage.push_once(session, url=url) == 0

    asyncio.run(scenario())

    requests = [
        decode_write_request(snappy_decompress(post.kwargs["data"]))
        for post in session.post.call_args_list
    ]
    assert requests == [[item] for item in expected[:2] + pending]
    assert storage.error_count == 1


@pytest.mark.parametrize(
    "username, password, interval, valid",
    [
        ("reader", "example", 1, True),
        ("", "example", 1, False),
        ("reader", "", 1, False),
        ("reader", "example", 0, False),
    ],
    ids=["valid", "missing-username", "missing-password", "zero-interval"],
)
def test_basic_remote_write_config_requires_both_credentials_and_positive_interval(
    monkeypatch, username, password, interval, valid
):
    prefix = "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_"
    for key, value in {
        "ENABLED": True,
        "URL": "https://receiver.example/api/v1/write",
        "AUTH": "basic",
        "USERNAME": username,
        "PASSWORD": password,
        "INTERVAL": interval,
    }.items():
        monkeypatch.setattr(config, prefix + key, value)

    assert config_validator.validate_prometheus_config() is valid
    if valid:
        authorization = make_storage().auth_headers("", {}, b"")["Authorization"]
        assert aiohttp.BasicAuth.decode(authorization) == (username, password, "latin1")
