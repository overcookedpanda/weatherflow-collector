"""The overview dashboards' live wind panels match the websocket server."""

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARDS = [
    ROOT / "grafana" / "dashboards" / kind / "lux4rd0_weatherflow-overview.json"
    for kind in ("internal", "external")
]
# Kept in step with provider.websocket_server.COLLECTOR_TYPES, which is not
# imported here so this test runs without the websockets package.
COLLECTOR_TYPES = {"collector_udp", "collector_websocket"}
FIELDS = {"wind_speed", "wind_direction", "latitude", "longitude", "timestamp", "station_name"}


def websocket_panels(dashboard):
    return [
        panel
        for panel in dashboard["panels"]
        if panel.get("datasource", {}).get("type") == "golioth-websocket-datasource"
    ]


def test_live_wind_panels_are_back_and_match_the_server():
    for path in DASHBOARDS:
        dashboard = json.loads(path.read_text())
        panels = websocket_panels(dashboard)
        assert len(panels) == 3, path
        for panel in panels:
            assert "WEBSOCKET_SERVER_ENABLED" in panel["description"]
            for target in panel["targets"]:
                assert target["path"] == "$source/$device_id"
                for field in target["fields"]:
                    match = re.search(r"\.(\w+)$", field["jsonPath"])
                    assert match and match.group(1) in FIELDS, field["jsonPath"]
        variables = {v["name"]: v for v in dashboard["templating"]["list"]}
        assert {"data_source_websocket", "source", "device_id"} <= set(variables)
        sources = {option["value"] for option in variables["source"]["options"]}
        assert sources <= COLLECTOR_TYPES


def test_no_original_author_hosts():
    for path in DASHBOARDS:
        text = path.read_text().lower()
        assert "app20" not in text
