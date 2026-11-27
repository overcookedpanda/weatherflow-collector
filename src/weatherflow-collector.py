# main.py

"""
WeatherFlow Collector Main Module

This module serves as the entry point for the WeatherFlow Collector system, an advanced framework designed 
to gather, process, and distribute meteorological data from various sources. It integrates several components 
like WebSocket clients, REST API clients, and UDP listeners to collect data, which is then processed and 
optionally forwarded to different endpoints such as InfluxDB, file systems, or WebSocket servers.

Key Features:
- Modular architecture allowing flexible data collection from multiple sources.
- Integration with WeatherFlow's REST API and WebSocket services.
- Support for local UDP data reception.
- Event-driven design for efficient data processing and handling.
- Configurable data forwarding to InfluxDB, file systems, and WebSocket servers.

Usage:
The main module initializes various components based on the system configuration and orchestrates their 
interaction. It sets up the event manager, data processors, and various clients (UDP, REST, WebSocket, etc.).
It runs asynchronously, continuously managing the flow of data across the system.

Dependencies:
- asyncio: For asynchronous programming.
- config: Configuration module with system settings.
- threading: For managing threads in UDP collector.
- logger: Custom logging module.
- EventManager, collectorDataProcessor, and various collector modules: Core components of the system.

Components:
- StationMetadataManager: Manages metadata for WeatherFlow stations.
- EventManager: Central event handling mechanism.
- collectorDataProcessor: Processes data from various collectors.
- InfluxDBHandler, Filecollector, WSServercollector: Handlers for forwarding processed data.
- UDPcollector, WScollector, RestObservationscollector, RestForecastscollector: collectors for data collection.

Author: [Your Name or Team's Name]
Last Update: [Last Update Date]

Note:
WeatherFlow Collector is specifically designed to work with WeatherFlow's meteorological data systems.
Modifications may be required for use with other systems or data sources.
"""


import asyncio
import sys
import config
import logger

from collector.rest_forecasts import RestForcecastsCollector
from collector.rest_import import RestImportCollector
from collector.rest_observations_device import RESTObservationsDeviceCollector
from collector.rest_observations_station import RESTObservationsStationCollector
from collector.rest_stats import RestStatsCollector
from collector.udp import UDPCollector
from collector.websocket import WebsocketCollector
from event_manager import EventManager
from processor.collector_data import CollectorDataProcessor
from station_metadata_manager import StationMetadataManager
from handlers.handler import Handler
from storage.influxdb import InfluxDBStorage
from storage.prometheus import PrometheusStorage
from handlers.system_metrics import SystemMetricsHandler
from provider.websocket_server import WebSocketServerProvider
from config_validator import validate_all


# Initialize logger
logger_main = logger.get_module_logger()


async def setup_app():

    if not validate_all():
        logger_main.error(
            "Configuration validation failed. Check the logs for details."
        )
        raise Exception("Configuration validation failed")

    station_metadata_manager = StationMetadataManager()
    station_metadata_manager.run()  # Not an async function

    event_manager = EventManager()

    collector_data_processor = CollectorDataProcessor(event_manager)
    event_manager.subscribe("collector_data_event", collector_data_processor)

    if config.WEATHERFLOW_COLLECTOR_HANDLER_ENABLED:
        logger_main.info("handler enabled.")
        handler = Handler(event_manager)
        event_manager.subscribe("processed_data_event", handler)
    else:
        logger_main.info("handler disabled.")

    if config.WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED:
        logger_main.info("handler_influxdb enabled.")
        handler_influxdb = InfluxDBStorage(
            event_manager,
            config.WEATHERFLOW_COLLECTOR_INFLUXDB_URL,
            config.WEATHERFLOW_COLLECTOR_INFLUXDB_TOKEN,
            config.WEATHERFLOW_COLLECTOR_INFLUXDB_ORG,
            config.WEATHERFLOW_COLLECTOR_INFLUXDB_BUCKET,
        )
        event_manager.subscribe("influxdb_storage_event", handler_influxdb)
    else:
        logger_main.info("handler_influxdb disabled.")

    if config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_ENABLED:
        logger_main.info("handler_prometheus enabled.")
        handler_prometheus = PrometheusStorage(event_manager)
        # Same events as InfluxDB, so both outputs can run at once.
        event_manager.subscribe("influxdb_storage_event", handler_prometheus)
        await handler_prometheus.start()
    else:
        logger_main.info("handler_prometheus disabled.")

    if config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ENABLED:
        logger_main.info("websocket_server_provider enabled.")
        websocket_server_provider = WebSocketServerProvider(event_manager)
        event_manager.subscribe("processed_data_event", websocket_server_provider)
        asyncio.create_task(websocket_server_provider.start_server())
    else:
        logger_main.info("websocket_server_provider disabled.")

    if config.WEATHERFLOW_COLLECTOR_SYSTEM_METRICS_ENABLED:
        logger_main.info("system_metrics_event enabled.")
        system_metrics_handler = SystemMetricsHandler(event_manager)
        event_manager.subscribe("system_metrics_event", system_metrics_handler)
    else:
        logger_main.info("system_metrics_event disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_UDP_ENABLED:
        logger_main.info("collector_udp enabled.")
        collector_udp = UDPCollector(event_manager)
        await collector_udp.start_listening()
    else:
        logger_main.info("udp_collector disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_WEBSOCKET_ENABLED:
        logger_main.info("collector_websocket enabled.")
        collector_websocket = WebsocketCollector(event_manager)
        asyncio.create_task(collector_websocket.start())
    else:
        logger_main.info("collector_websocket disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_DEVICE_ENABLED:
        logger_main.info("collector_rest_observations_device enabled.")
        collector_rest_observations_device = RESTObservationsDeviceCollector(
            event_manager
        )
        asyncio.create_task(collector_rest_observations_device.run_forever())
    else:
        logger_main.info("collector_rest_observations_device disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_STATION_ENABLED:
        logger_main.info("collector_rest_observations_station enabled.")
        collector_rest_observations_station = RESTObservationsStationCollector(
            event_manager
        )
        asyncio.create_task(collector_rest_observations_station.run_forever())
    else:
        logger_main.info("collector_rest_observations_station disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_FORECASTS_ENABLED:
        logger_main.info("collector_rest_forecasts enabled.")
        collector_rest_forecasts = RestForcecastsCollector(event_manager)
        asyncio.create_task(collector_rest_forecasts.run_forever())
    else:
        logger_main.info("collector_rest_forecasts disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_STATS_ENABLED:
        logger_main.info("collector_rest_stats enabled.")
        collector_rest_stats = RestStatsCollector(event_manager)
        asyncio.create_task(collector_rest_stats.run_forever())
    else:
        logger_main.info("collector_rest_stats disabled.")

    if config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_IMPORT_ENABLED:
        logger_main.info("collector_rest_import enabled.")
        collector_rest_import = RestImportCollector(event_manager)
        asyncio.create_task(collector_rest_import.run_once())
    else:
        logger_main.info("collector_rest_import disabled.")

async def main_async():
    try:
        logger_main.info("Starting WeatherFlow Collector")
        await setup_app()
        # Keep the asyncio loop running indefinitely
        while True:
            await asyncio.sleep(3600)  # Sleep for an hour; you can adjust this duration
    except Exception as e:
        logger_main.error(f"Application stopped due to error: {e}")
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger_main.info("Stopping WeatherFlow Collector")
    except Exception as e:
        logger_main.error(f"Application terminated due to an error: {e}")
        sys.exit(1)  # a non-zero exit lets the container restart policy act
