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
WEBSOCKET_DATASOURCE = {
    "type": "golioth-websocket-datasource",
    "uid": "${data_source_websocket}",
}
# A made-up reading in the shape format_rapid_wind_data() sends. The station
# name has a comma and an apostrophe, which a quoted JSONPath key would break on.
FAKE_STATION = "Test, Ridge's Station"
FAKE_READING = {
    "station_name": FAKE_STATION,
    "timestamp": 1700000000,
    "wind_speed": 4.2,
    "wind_direction": 135,
    "latitude": 1.5,
    "longitude": -2.5,
}
STEP = r"\.\*|\['[^']*'\]|\.\w+"


def fake_message(source):
    return {"rapid_wind": {source: {FAKE_STATION: dict(FAKE_READING)}}}


def evaluate(json_path, message):
    """Evaluate the JSONPath subset the panels use: .key, ['key'] and .*"""
    body = json_path.removeprefix("$")
    assert re.fullmatch(f"(?:{STEP})+", body), json_path
    nodes = [message]
    for step in re.findall(STEP, body):
        if step == ".*":
            nodes = [child for node in nodes for child in node.values()]
        else:
            key = step[2:-2] if step.startswith("[") else step[1:]
            nodes = [node[key] for node in nodes if key in node]
    return nodes


def websocket_panels(dashboard):
    return [
        panel
        for panel in dashboard["panels"]
        if panel.get("datasource", {}).get("type") == WEBSOCKET_DATASOURCE["type"]
    ]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_live_wind_panels_are_back_and_match_the_server():
    for path in DASHBOARDS:
        dashboard = load(path)
        panels = websocket_panels(dashboard)
        assert len(panels) == 3, path
        for panel in panels:
            assert "WEBSOCKET_SERVER_ENABLED" in panel["description"]
            assert panel["datasource"] == WEBSOCKET_DATASOURCE, path
            for target in panel["targets"]:
                assert target["datasource"] == WEBSOCKET_DATASOURCE, path
                assert target["path"] == "$source/$device_id"
                for field in target["fields"]:
                    key = field["jsonPath"].rsplit(".", 1)[1]
                    for source in COLLECTOR_TYPES:
                        json_path = field["jsonPath"].replace("$source", source)
                        values = evaluate(json_path, fake_message(source))
                        assert values == [FAKE_READING[key]], field["jsonPath"]
        variables = {v["name"]: v for v in dashboard["templating"]["list"]}
        assert {"data_source_websocket", "source", "device_id"} <= set(variables)
        sources = {option["value"] for option in variables["source"]["options"]}
        assert sources <= COLLECTOR_TYPES
        assert 'FROM "weatherflow_rapid_wind"' in variables["device_id"]["query"]


def test_external_overview_imports_without_a_websocket_datasource():
    inputs = load(DASHBOARDS[1]).get("__inputs", [])
    assert WEBSOCKET_DATASOURCE["type"] not in {item.get("pluginId") for item in inputs}


def test_live_wind_speed_is_labelled_in_metres_per_second():
    for path in DASHBOARDS:
        for panel in websocket_panels(load(path)):
            assert "velocitymph" not in json.dumps(panel), panel["title"]


def test_no_original_author_hosts():
    for path in DASHBOARDS:
        assert "app20" not in path.read_text(encoding="utf-8").lower()
