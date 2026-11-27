"""The Prometheus dashboards match their generator and are well formed."""

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_prometheus_dashboards.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_prometheus_dashboards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboards_are_up_to_date():
    builder = load_builder()
    for name, build in (
        ("weatherflow-current_conditions", builder.current_conditions),
        ("weatherflow-forecast", builder.forecast),
        ("weatherflow-collector_health", builder.collector_health),
    ):
        on_disk = json.loads((builder.OUT / f"{name}.json").read_text(encoding="utf-8"))
        assert on_disk == build(), (
            f"{name}.json is stale; run python3 scripts/build_prometheus_dashboards.py"
        )


def test_dashboards_are_well_formed():
    uids = set()
    for path in sorted((ROOT / "grafana" / "dashboards" / "prometheus").glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        assert dashboard["uid"] not in uids
        uids.add(dashboard["uid"])
        ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(ids) == len(set(ids))
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                expr = target["expr"]
                assert expr.count("(") == expr.count(")"), expr
                assert expr.count("{") == expr.count("}"), expr
                assert target["datasource"]["uid"] == "${datasource}"
    assert len(uids) == 3
