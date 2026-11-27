"""Opt-in Prometheus output: a scrape endpoint, remote write, or both.

Subscribes to the same storage events as the InfluxDB writer, so it can run
beside it or on its own. See storage/prometheus_metrics.py for how readings
become series.

- Scrape: serves /metrics on 127.0.0.1:9110 unless configured otherwise.
- Remote write: pushes new samples every interval to a Prometheus remote write
  URL, such as Amazon Managed Service for Prometheus (SigV4), Grafana Cloud or
  Mimir (basic auth), or any receiver that takes a bearer token.
"""

import asyncio
import os
import time

import aiohttp
from aiohttp import web

import config
import logger
import utils.utils as utils
from storage.prometheus_metrics import MetricStore
from utils import remote_write, sigv4

logger_PrometheusStorage = logger.get_module_logger(__name__ + ".PrometheusStorage")

EXPOSITION_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
USER_AGENT = "weatherflow-collector"
MAX_SERIES_PER_REQUEST = 2000
RETRYABLE_STATUSES = {429}


class PrometheusStorage:
    def __init__(self, event_manager, store=None):
        self.event_manager = event_manager
        self.scrape_enabled = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_ENABLED
        self.remote_write_enabled = (
            config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED
        )
        self.store = store or MetricStore(
            drop_labels=config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_DROP_LABELS,
            max_series=config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_MAX_SERIES,
            stale_after=config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_STALE_AFTER,
            max_sample_age=config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_MAX_SAMPLE_AGE,
            primary_source=config.WEATHERFLOW_COLLECTOR_PRIMARY_SOURCE,
            queue_samples=self.remote_write_enabled,
            logger=logger_PrometheusStorage,
        )
        self.module_name = "prometheus_handler"
        self.runner = None
        self.push_count = 0
        self.error_count = 0

    # Events

    def update(self, data):
        try:
            self.store.add_event(data)
        except Exception as e:
            logger_PrometheusStorage.error(f"Could not map a reading for Prometheus: {e}")

    # Scrape endpoint

    async def handle_metrics(self, request):
        return web.Response(
            body=self.store.exposition().encode("utf-8"),
            headers={"Content-Type": EXPOSITION_CONTENT_TYPE},
        )

    def make_app(self):
        app = web.Application()
        app.router.add_get("/metrics", self.handle_metrics)
        return app

    async def start_scrape_endpoint(self, host=None, port=None):
        host = host or config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_HOST
        port = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_PORT if port is None else port
        self.runner = web.AppRunner(self.make_app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        logger_PrometheusStorage.info(
            f"Prometheus metrics at http://{host}:{port}/metrics"
        )
        if host not in ("127.0.0.1", "localhost", "::1"):
            logger_PrometheusStorage.warning(
                "The Prometheus endpoint is reachable beyond this machine and has no "
                "authentication."
            )
        return site

    # Remote write

    def auth_headers(self, url, headers, body):
        mode = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH
        if mode == "sigv4":
            return sigv4.sign(
                "POST",
                url,
                headers,
                body,
                region=config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_SIGV4_REGION,
                service="aps",
                access_key=os.environ.get("AWS_ACCESS_KEY_ID", ""),
                secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
                session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
            )
        if mode == "basic":
            return {
                "Authorization": aiohttp.BasicAuth(
                    config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_USERNAME,
                    config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_PASSWORD,
                ).encode()
            }
        if mode == "bearer":
            token = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_BEARER_TOKEN
            return {"Authorization": f"Bearer {token}"}
        return {}

    async def push_once(self, session, url=None):
        """Send queued samples. Returns the number of series sent."""
        url = url or config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL
        series = self.store.drain()
        sent = 0
        for start in range(0, len(series), MAX_SERIES_PER_REQUEST):
            chunk = series[start : start + MAX_SERIES_PER_REQUEST]
            body = remote_write.snappy_compress(remote_write.encode_write_request(chunk))
            headers = {
                "Content-Encoding": remote_write.CONTENT_ENCODING,
                "Content-Type": remote_write.CONTENT_TYPE,
                "User-Agent": USER_AGENT,
                "X-Prometheus-Remote-Write-Version": remote_write.PROTOCOL_VERSION,
            }
            headers.update(self.auth_headers(url, headers, body))
            try:
                async with session.post(url, data=body, headers=headers) as response:
                    if 200 <= response.status < 300:
                        sent += len(chunk)
                        continue
                    detail = (await response.text())[:200]
                    retry = response.status in RETRYABLE_STATUSES or response.status >= 500
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                detail, retry = str(e), True
                response = None

            self.error_count += 1
            status = response.status if response is not None else "no response"
            if retry:
                logger_PrometheusStorage.warning(
                    f"Remote write failed ({status}); will retry: {detail}"
                )
                self.store.requeue(series[start:])
            else:
                logger_PrometheusStorage.error(
                    f"Remote write rejected ({status}); dropping {len(chunk)} series: "
                    f"{detail}"
                )
                self.store.requeue(series[start + MAX_SERIES_PER_REQUEST :])
            break
        return sent

    async def remote_write_loop(self):
        interval = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_INTERVAL
        timeout = aiohttp.ClientTimeout(
            total=config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_TIMEOUT
        )
        logger_PrometheusStorage.info(
            f"Prometheus remote write every {interval}s "
            f"(auth: {config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH})"
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await asyncio.sleep(interval)
                start = time.time()
                try:
                    sent = await self.push_once(session)
                    self.store.prune()
                except Exception as e:
                    self.error_count += 1
                    logger_PrometheusStorage.error(f"Remote write error: {e}")
                    continue
                if sent:
                    self.push_count += 1
                await utils.async_publish_metrics(
                    self.event_manager,
                    metric_name="remote_write",
                    module_name=self.module_name,
                    rate=self.push_count,
                    errors=self.error_count,
                    duration=time.time() - start,
                )

    async def start(self):
        if self.scrape_enabled:
            try:
                await self.start_scrape_endpoint()
            except OSError as e:
                logger_PrometheusStorage.error(
                    f"Could not start the Prometheus endpoint: {e}"
                )
        if self.remote_write_enabled:
            asyncio.create_task(self.remote_write_loop())

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
