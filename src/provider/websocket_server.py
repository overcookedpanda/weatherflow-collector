"""Optional websocket server that rebroadcasts live rapid wind readings.

Off by default. When enabled it listens on 127.0.0.1 unless
WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_HOST says otherwise. Anyone who
can reach the port can read the live feed, including the station name and
coordinates, and there is no authentication, so only bind it to a LAN address
on a network you trust.

Clients connect to ws://HOST:PORT/<collector_type>/<device_id>, for example
/collector_udp/123456, and receive each rapid_wind reading for that device as
JSON. Connections without a valid path are accepted and receive nothing.
"""

import asyncio
import json
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

import config
import logger
import utils.utils as utils

logger_WebSocketServerProvider = logger.get_module_logger(
    __name__ + ".WebSocketServerProvider"
)

# Clients only listen; anything they send is read and dropped. Keep frames small
# so a client can't make the server buffer large messages.
MAX_CLIENT_MESSAGE_BYTES = 4096

COLLECTOR_TYPES = ("collector_udp", "collector_websocket")


class WebSocketServerProvider:
    def __init__(self, event_manager, station_metadata=None):
        self.event_manager = event_manager
        self.host = config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_HOST
        self.port = config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_PORT
        if station_metadata is None:
            station_metadata = utils.StationMetadataSingleton().get_metadata()
        self.station_metadata = station_metadata or {}
        self.clients = set()  # (connection, collector_type, device_id)
        self.default_clients = set()
        self.module_name = "websocket_server"
        self.collector_type = "websocket_server"
        self.metrics_by_client_type = {}
        self.server = None

    @staticmethod
    def allowed_origins():
        """Clients with no Origin header plus any configured browser origins."""
        return [None, *config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ORIGINS]

    def enabled_device_ids(self):
        ids = set()
        for station_info in self.station_metadata.values():
            if not station_info.get("enabled", False):
                continue
            for device in station_info.get("devices", []):
                if device.get("enabled", False) and device.get("device_id") is not None:
                    ids.add(device["device_id"])
        return ids

    def get_device_id_from_serial(self, serial_number):
        for station_info in self.station_metadata.values():
            for device in station_info.get("devices", []):
                if device.get("serial_number") == serial_number:
                    return device.get("device_id")
        return None

    def parse_path(self, path):
        """Return (collector_type, device_id) for a valid path, else None."""
        parts = (path or "").split("?", 1)[0].strip("/").split("/")
        if len(parts) < 2 or parts[0] not in COLLECTOR_TYPES:
            return None
        try:
            device_id = int(parts[1])
        except ValueError:
            return None
        if device_id not in self.enabled_device_ids():
            return None
        return parts[0], device_id

    def connection_count(self):
        return len(self.clients) + len(self.default_clients)

    async def register(self, connection, path):
        """Register a client. Returns False if it was refused."""
        max_connections = (
            config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_MAX_CONNECTIONS
        )
        if self.connection_count() >= max_connections:
            logger_WebSocketServerProvider.warning(
                "Maximum connection limit reached. Closing new connection."
            )
            await connection.close(code=1013, reason="Connection limit reached")
            return False

        target = self.parse_path(path)
        if target is None:
            self.default_clients.add(connection)
            logger_WebSocketServerProvider.info(
                "Client connected without a valid collector type and device ID; "
                "it will receive nothing."
            )
            return True

        collector_type, device_id = target
        self.clients.add((connection, collector_type, device_id))
        logger_WebSocketServerProvider.info(
            f"Client registered for device ID '{device_id}' with collector type "
            f"'{collector_type}'."
        )
        return True

    def unregister(self, connection):
        for client in [c for c in self.clients if c[0] is connection]:
            self.clients.discard(client)
        self.default_clients.discard(connection)

    async def websocket_handler(self, connection):
        try:
            if not await self.register(connection, connection.request.path):
                return
            async for _message in connection:
                logger_WebSocketServerProvider.debug(
                    "Ignoring a message sent by a websocket client."
                )
        except ConnectionClosed:
            pass
        except Exception as e:
            logger_WebSocketServerProvider.error(f"Error in websocket_handler: {e}")
        finally:
            self.unregister(connection)

    def update(self, full_data):
        collector_type = full_data.get("metadata", {}).get("collector_type")
        data = full_data.get("data", {})
        if collector_type not in COLLECTOR_TYPES or data.get("type") != "rapid_wind":
            return

        if collector_type == "collector_udp":
            serial_number = data.get("serial_number")
            device_id = (
                self.get_device_id_from_serial(serial_number) if serial_number else None
            )
        else:
            device_id = data.get("device_id")

        if device_id is None:
            logger_WebSocketServerProvider.debug(
                f"Device ID is unknown for {collector_type} rapid_wind data"
            )
            return

        message = self.format_rapid_wind_data(full_data)
        if message is None:
            return
        asyncio.create_task(self.broadcast(message, collector_type, device_id))

    def format_rapid_wind_data(self, full_data):
        observation = full_data.get("data", {}).get("ob", [])
        if not observation or len(observation) != 3:
            logger_WebSocketServerProvider.error(
                "Invalid or incomplete rapid_wind observation received"
            )
            return None
        timestamp, wind_speed, wind_direction = observation

        source = full_data.get("metadata", {}).get("collector_type", "unknown-source")
        station_info = full_data.get("station_info", {})
        station_name = station_info.get("station_name") or "unknown"
        # Same message shape as the upstream collector, so existing panels work.
        structured_data = {
            "rapid_wind": {
                source: {
                    station_name: {
                        "station_name": station_info.get("station_name"),
                        "timestamp": timestamp,
                        "wind_speed": wind_speed,
                        "wind_direction": wind_direction,
                        "latitude": station_info.get("station_latitude"),
                        "longitude": station_info.get("station_longitude"),
                    }
                }
            }
        }
        return json.dumps(structured_data)

    async def broadcast(self, message, collector_type, device_id):
        start = time.time()
        metrics = self.metrics_by_client_type.setdefault(
            collector_type, {"message_count": 0, "error_count": 0, "client_count": 0}
        )
        targets = [
            client[0]
            for client in self.clients
            if client[1] == collector_type and client[2] == device_id
        ]
        metrics["client_count"] = len(targets)
        message_bytes = len(message.encode("utf-8"))

        if targets:
            results = await asyncio.gather(
                *(connection.send(message) for connection in targets),
                return_exceptions=True,
            )
            failures = [r for r in results if isinstance(r, Exception)]
            if failures:
                metrics["error_count"] += len(failures)
                logger_WebSocketServerProvider.debug(
                    f"{len(failures)} websocket client(s) could not be sent to"
                )
            metrics["message_count"] += 1

        await utils.async_publish_metrics(
            self.event_manager,
            metric_name=f"broadcast_{collector_type}",
            module_name=self.module_name,
            rate=metrics["message_count"],
            errors=metrics["error_count"],
            duration=time.time() - start,
            client_count=metrics["client_count"],
            bytes=message_bytes,
        )

    def cleanup_closed(self):
        for client in list(self.clients):
            if client[0].state is State.CLOSED:
                self.clients.discard(client)
        for connection in list(self.default_clients):
            if connection.state is State.CLOSED:
                self.default_clients.discard(connection)

    async def cleanup_connections(self):
        interval = (
            config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_CLEANUP_CONNECTIONS_INTERVAL
        )
        while True:
            await asyncio.sleep(interval)
            try:
                self.cleanup_closed()
            except Exception as e:
                logger_WebSocketServerProvider.error(
                    f"Error during connection cleanup: {e}"
                )

    async def start_server(self):
        try:
            self.server = await serve(
                self.websocket_handler,
                self.host,
                self.port,
                ping_interval=config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_PING_INTERVAL,
                ping_timeout=config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_PING_TIMEOUT,
                max_size=MAX_CLIENT_MESSAGE_BYTES,
                origins=self.allowed_origins(),
            )
        except OSError as e:
            logger_WebSocketServerProvider.error(
                f"Could not start the websocket server on {self.host}:{self.port}: {e}"
            )
            return
        logger_WebSocketServerProvider.info(
            f"WebSocket server listening on ws://{self.host}:{self.port}/"
        )
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            logger_WebSocketServerProvider.warning(
                "The websocket server is reachable beyond this machine and has no "
                "authentication; anyone who can reach it can read the live feed."
            )
        await asyncio.gather(self.server.wait_closed(), self.cleanup_connections())
