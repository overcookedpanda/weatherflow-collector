#!/usr/bin/env python3
"""Build the Prometheus dashboards in grafana/dashboards/prometheus.

Run from the repository root after changing this file:

    python3 scripts/build_prometheus_dashboards.py

The dashboards query the series made by the collector's Prometheus output
(see storage/prometheus_metrics.py). Values are in the units the Tempest
reports (Celsius, m/s, mb, mm); change a panel's unit in Grafana to display
others.
"""

import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards" / "prometheus"
DS = {"type": "prometheus", "uid": "${datasource}"}
SEL = 'station_name=~"$station_name",collector_type=~"$collector_type"'
STATION = 'station_name=~"$station_name"'


def variables(include_collector_type=True):
    items = [
        {
            "name": "datasource",
            "label": "Data source",
            "type": "datasource",
            "query": "prometheus",
            "current": {},
            "hide": 0,
        },
        {
            "name": "station_name",
            "label": "Station",
            "type": "query",
            "datasource": DS,
            "query": {
                "query": "label_values(weatherflow_obs_air_temperature, station_name)",
                "refId": "StationName",
            },
            "definition": "label_values(weatherflow_obs_air_temperature, station_name)",
            "refresh": 2,
            "includeAll": True,
            "multi": True,
            "current": {},
            "sort": 1,
            "hide": 0,
        },
    ]
    if include_collector_type:
        items.append(
            {
                "name": "collector_type",
                "label": "Source",
                "description": "primary is the source set by WEATHERFLOW_COLLECTOR_PRIMARY_SOURCE.",
                "type": "query",
                "datasource": DS,
                "query": {
                    "query": "label_values(weatherflow_obs_air_temperature, collector_type)",
                    "refId": "CollectorType",
                },
                "definition": "label_values(weatherflow_obs_air_temperature, collector_type)",
                "refresh": 2,
                "current": {"text": "primary", "value": "primary"},
                "sort": 1,
                "includeAll": False,
                "multi": False,
                "hide": 0,
            }
        )
    return items


class Layout:
    """Places panels on Grafana's 24-column grid, left to right."""

    def __init__(self):
        self.x = self.y = self.row_height = 0
        self.next_id = 1

    def place(self, panel, w, h):
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_height
            self.row_height = 0
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        panel["id"] = self.next_id
        self.next_id += 1
        self.x += w
        self.row_height = max(self.row_height, h)
        return panel

    def row(self, title):
        if self.x:
            self.x, self.y = 0, self.y + self.row_height
            self.row_height = 0
        panel = {"type": "row", "title": title, "collapsed": False, "panels": []}
        return self.place(panel, 24, 1)


def target(expr, legend="", ref="A", instant=False, fmt="time_series"):
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
        "instant": instant,
        "range": not instant,
        "format": fmt,
    }


def stat(title, expr, unit, decimals=1, description=""):
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": [target(expr, "{{station_name}}", instant=True)],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "auto",
            "justifyMode": "auto",
            "orientation": "auto",
        },
    }


def timeseries(title, targets, unit, description="", draw="line", stack=False):
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": [
            target(expr, legend, ref=chr(ord("A") + i)) for i, (expr, legend) in enumerate(targets)
        ],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": draw,
                    "lineWidth": 1,
                    "fillOpacity": 10 if draw == "line" else 80,
                    "showPoints": "never",
                    "spanNulls": 300000,
                    "stacking": {"mode": "normal" if stack else "none"},
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
    }


def forecast_table(title, columns, key, description=""):
    """An instant table merging one query per column, joined on key."""
    targets = [
        target(expr, ref=chr(ord("A") + i), instant=True, fmt="table")
        for i, (expr, _name, _unit) in enumerate(columns)
    ]
    renames = {f"Value #{chr(ord('A') + i)}": name for i, (_e, name, _u) in enumerate(columns)}
    overrides = [
        {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "unit", "value": unit}]}
        for _e, name, unit in columns
    ]
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": targets,
        "transformations": [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "__name__": True, "collector_type": True, "station_id": True},
                    "renameByName": {**renames, key: "Ahead"},
                    "indexByName": {key: 0},
                },
            },
            {
                "id": "convertFieldType",
                "options": {"conversions": [{"targetField": "Ahead", "destinationType": "number"}]},
            },
            {"id": "sortBy", "options": {"sort": [{"field": "Ahead", "desc": False}]}},
        ],
        "fieldConfig": {"defaults": {"decimals": 1}, "overrides": overrides},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def dashboard(uid, title, description, panels, tags, include_collector_type=True, time_from="now-24h"):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["weatherflow", "prometheus", *tags],
        "editable": True,
        "graphTooltip": 1,
        "refresh": "1m",
        "schemaVersion": 39,
        "time": {"from": time_from, "to": "now"},
        "timezone": "browser",
        "templating": {"list": variables(include_collector_type)},
        "annotations": {"list": []},
        "links": [
            {
                "type": "dashboards",
                "tags": ["weatherflow", "prometheus"],
                "asDropdown": True,
                "title": "WeatherFlow",
                "includeVars": True,
                "keepTime": True,
            }
        ],
        "panels": panels,
    }


def current_conditions():
    layout = Layout()
    obs = lambda field: f"weatherflow_obs_{field}{{{SEL}}}"  # noqa: E731
    rain_today = (
        f"max by (station_name) (weatherflow_obs_local_daily_rain_accumulation{{{SEL}}} "
        f"or weatherflow_obs_local_day_rain_accumulation{{{SEL}}})"
    )
    panels = [
        layout.row("Now"),
        layout.place(stat("Temperature", obs("air_temperature"), "celsius"), 4, 4),
        layout.place(stat("Dew point", obs("calculated_dew_point"), "celsius"), 4, 4),
        layout.place(stat("Humidity", obs("relative_humidity"), "percent", 0), 4, 4),
        layout.place(stat("Sea level pressure", obs("calculated_sea_level_pressure"), "pressurembar"), 4, 4),
        layout.place(stat("Wind", f"weatherflow_rapid_wind_wind_speed{{{SEL}}}", "velocityms"), 4, 4),
        layout.place(stat("Wind direction", f"weatherflow_rapid_wind_wind_direction{{{SEL}}}", "degree", 0), 4, 4),
        layout.place(stat("Wind gust", obs("wind_gust"), "velocityms"), 4, 4),
        layout.place(stat("UV index", obs("uv"), "none"), 4, 4),
        layout.place(stat("Solar radiation", obs("solar_radiation"), "Wm2", 0), 4, 4),
        layout.place(stat("Rain today", rain_today, "lengthmm"), 4, 4),
        layout.place(
            stat(
                "Lightning strikes, last hour",
                f"sum by (station_name) (sum_over_time(weatherflow_obs_lightning_strike_count{{{SEL}}}[1h]))",
                "none",
                0,
            ),
            4,
            4,
        ),
        layout.place(stat("Battery", obs("battery"), "volt", 2), 4, 4),
        layout.row("Trends"),
        layout.place(
            timeseries(
                "Temperature",
                [(obs("air_temperature"), "Temperature"), (obs("calculated_dew_point"), "Dew point")],
                "celsius",
            ),
            12,
            8,
        ),
        layout.place(timeseries("Humidity", [(obs("relative_humidity"), "Humidity")], "percent"), 12, 8),
        layout.place(
            timeseries(
                "Wind",
                [
                    (obs("wind_lull"), "Lull"),
                    (obs("wind_avg"), "Average"),
                    (obs("wind_gust"), "Gust"),
                    (f"weatherflow_rapid_wind_wind_speed{{{SEL}}}", "Rapid"),
                ],
                "velocityms",
            ),
            12,
            8,
        ),
        layout.place(
            timeseries(
                "Pressure",
                [(obs("calculated_sea_level_pressure"), "Sea level"), (obs("station_pressure"), "Station")],
                "pressurembar",
            ),
            12,
            8,
        ),
        layout.place(timeseries("Rain today", [(rain_today, "Accumulated")], "lengthmm"), 12, 8),
        layout.place(
            timeseries(
                "Lightning",
                [
                    (obs("lightning_strike_count"), "Strikes"),
                    (obs("lightning_strike_avg_distance"), "Average distance (km)"),
                ],
                "none",
                draw="bars",
            ),
            12,
            8,
        ),
        layout.place(
            timeseries(
                "Sun",
                [(obs("solar_radiation"), "Solar radiation (W/m²)"), (obs("uv"), "UV index")],
                "none",
            ),
            12,
            8,
        ),
        layout.place(timeseries("Brightness", [(obs("illuminance"), "Illuminance")], "lux"), 12, 8),
    ]
    return dashboard(
        "weatherflow-prom-current",
        "WeatherFlow - Current Conditions (Prometheus)",
        "Live readings from the collector's Prometheus output.",
        panels,
        ["current"],
    )


def forecast():
    layout = Layout()
    daily = lambda field: f"weatherflow_forecast_daily_{field}{{{STATION}}}"  # noqa: E731
    hourly = lambda field, extra="": f"weatherflow_forecast_hourly_{field}{{{STATION}{extra}}}"  # noqa: E731
    panels = [
        layout.row("Today"),
        layout.place(stat("High", f'weatherflow_forecast_daily_air_temp_high{{{STATION},days_ahead="0"}}', "celsius"), 6, 4),
        layout.place(stat("Low", f'weatherflow_forecast_daily_air_temp_low{{{STATION},days_ahead="0"}}', "celsius"), 6, 4),
        layout.place(
            stat("Chance of rain", f'weatherflow_forecast_daily_precip_probability{{{STATION},days_ahead="0"}}', "percent", 0),
            6,
            4,
        ),
        layout.place(
            stat("Next hour", hourly("air_temperature", ',hours_ahead="1"'), "celsius"),
            6,
            4,
        ),
        layout.row("Days ahead"),
        layout.place(
            forecast_table(
                "Daily forecast",
                [
                    (daily("air_temp_high"), "High", "celsius"),
                    (daily("air_temp_low"), "Low", "celsius"),
                    (daily("precip_probability"), "Chance of rain", "percent"),
                ],
                "days_ahead",
                "Ahead is days from today; 0 is today.",
            ),
            24,
            10,
        ),
        layout.row("Hours ahead"),
        layout.place(
            forecast_table(
                "Hourly forecast",
                [
                    (hourly("air_temperature"), "Temperature", "celsius"),
                    (hourly("relative_humidity"), "Humidity", "percent"),
                    (hourly("precip_probability"), "Chance of rain", "percent"),
                    (hourly("wind_avg"), "Wind", "velocityms"),
                    (hourly("sea_level_pressure"), "Pressure", "pressurembar"),
                ],
                "hours_ahead",
                "Ahead is hours from now.",
            ),
            24,
            12,
        ),
        layout.row("Forecast vs observed"),
        layout.place(
            timeseries(
                "Temperature: forecast an hour earlier vs observed",
                [
                    (f'weatherflow_obs_air_temperature{{{STATION},collector_type="primary"}}', "Observed"),
                    (
                        hourly("air_temperature", ',hours_ahead="1"') + " offset 1h",
                        "Forecast 1h ahead",
                    ),
                ],
                "celsius",
                "Compares each hour's observation with the forecast made an hour before.",
            ),
            24,
            9,
        ),
    ]
    return dashboard(
        "weatherflow-prom-forecast",
        "WeatherFlow - Forecast (Prometheus)",
        "Tempest forecasts from the collector's Prometheus output.",
        panels,
        ["forecast"],
        include_collector_type=False,
    )


def collector_health():
    layout = Layout()
    panels = [
        layout.row("Freshness"),
        layout.place(
            stat(
                "Seconds since last observation",
                f"time() - max by (station_name) (timestamp(weatherflow_obs_air_temperature{{{SEL}}}))",
                "s",
                0,
                "From the latest sample Prometheus holds for the selected source.",
            ),
            8,
            4,
        ),
        layout.place(
            stat(
                "Seconds since last rapid wind",
                f"time() - max by (station_name) (timestamp(weatherflow_rapid_wind_wind_speed{{{SEL}}}))",
                "s",
                0,
            ),
            8,
            4,
        ),
        layout.place(
            stat("Station battery", f"weatherflow_obs_battery{{{SEL}}}", "volt", 2),
            8,
            4,
        ),
        layout.row("Collector"),
        layout.place(
            timeseries(
                "Events per minute by module",
                [("sum by (module_name) (increase(weatherflow_system_metrics_rate[5m])) / 5", "{{module_name}}")],
                "none",
            ),
            12,
            8,
        ),
        layout.place(
            timeseries(
                "Errors per minute by module",
                [("sum by (module_name) (increase(weatherflow_system_metrics_errors[5m])) / 5", "{{module_name}}")],
                "none",
            ),
            12,
            8,
        ),
        layout.place(
            timeseries(
                "Processing time by metric",
                [("max by (metric_name) (weatherflow_system_metrics_duration)", "{{metric_name}}")],
                "s",
            ),
            24,
            8,
        ),
    ]
    return dashboard(
        "weatherflow-prom-health",
        "WeatherFlow - Collector Health (Prometheus)",
        "Is data arriving, and how the collector is doing.",
        panels,
        ["health"],
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (
        ("weatherflow-current_conditions", current_conditions),
        ("weatherflow-forecast", forecast),
        ("weatherflow-collector_health", collector_health),
    ):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
