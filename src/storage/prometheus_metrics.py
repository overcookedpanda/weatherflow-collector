"""Turn the collector's storage events into Prometheus series.

The collector publishes each reading as an InfluxDB-shaped point: a
measurement, tags, fields and a timestamp. This module maps every numeric
field to a gauge named <measurement>_<field>, with the tags as labels, for
example weatherflow_obs_air_temperature{station_name="...", ...}.

Prometheus stores one value per series per instant, so a few measurements are
reshaped:

- Hourly and daily forecasts are stamped with the current time and labelled
  with how far ahead they are (hours_ahead, days_ahead), instead of being
  written at future timestamps.
- Current forecasts and REST stats describe the present, so they are stamped
  with the current time.
- Observations keep their own timestamps. Ones older than the configured
  maximum age, such as a history import, are skipped.
- Each observation measurement also gets <measurement>_observed_timestamp_seconds,
  the time of its latest reading, so dashboards can show how old the data is
  even when Prometheus scrapes the endpoint, which carries no timestamps.
- Per-reading counts, such as lightning strikes, also get a running total,
  <measurement>_<field>_total, that grows once per new reading. Summing the
  gauge over time would count a held value once per scrape.

It is standard library only, so it can be tested without the web stack.
"""

import math
import re
import threading
import time

SNAPSHOT_MEASUREMENTS = {
    "weatherflow_forecast_current",
    "weatherflow_stats",
}
FORECAST_HOURLY = "weatherflow_forecast_hourly"
FORECAST_DAILY = "weatherflow_forecast_daily"
FORECAST_HOURLY_DROPPED_LABELS = {"number_of_days_out", "local_day"}

SKIPPED_FIELDS = {"timestamp", "time"}

# Fields that count events in each reading; each also gets a running total.
COUNTED_FIELDS = {
    "weatherflow_obs": {"lightning_strike_count"},
}
OBSERVED_TIMESTAMP_SUFFIX = "observed_timestamp_seconds"

# Readings stamped this far in the future are treated as clock skew.
FUTURE_TOLERANCE_SECONDS = 300

_INVALID_NAME = re.compile(r"[^a-zA-Z0-9_:]")
_INVALID_LABEL = re.compile(r"[^a-zA-Z0-9_]")


def metric_name(measurement, field):
    name = _INVALID_NAME.sub("_", f"{measurement}_{field}")
    if name[0].isdigit():
        name = "_" + name
    return name


def label_name(key):
    name = _INVALID_LABEL.sub("_", str(key))
    if name[0].isdigit():
        name = "_" + name
    if name.startswith("__"):
        name = "tag" + name  # names starting with __ are reserved
    return name


def sample_value(value):
    """Return a finite float for numeric and boolean values, else None."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    return None


def epoch_seconds(timestamp):
    if timestamp is None:
        return None
    if isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    if hasattr(timestamp, "timestamp"):  # datetime
        return float(timestamp.timestamp())
    try:
        return float(timestamp)
    except (TypeError, ValueError):
        return None


def escape_label_value(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_value(value):
    return repr(float(value))


class MetricStore:
    """Latest value per series, plus a queue of samples for remote write."""

    def __init__(
        self,
        *,
        drop_labels=(),
        max_series=10000,
        stale_after=900,
        max_sample_age=600,
        primary_source=None,
        queue_samples=False,
        max_queued_samples=100000,
        clock=time.time,
        logger=None,
    ):
        self.drop_labels = set(drop_labels)
        self.max_series = max_series
        self.stale_after = stale_after
        self.max_sample_age = max_sample_age
        self.primary_source = primary_source
        self.queue_samples = queue_samples
        self.max_queued_samples = max_queued_samples
        self.clock = clock
        self.logger = logger
        self._lock = threading.Lock()
        # key -> [value, order_ts, updated_at]; key = (name, ((label, value), ...))
        self._latest = {}
        self._queue = []  # (key, value, timestamp_ms)
        self._last_sent = {}  # key -> timestamp_ms
        self._warned_full = False
        self._warned_queue = False
        self._counter_names = set()
        # total key -> latest reading time counted into it
        self._counted_reading = {}

    # Input

    def add_event(self, data):
        if data.get("data_type", "single") == "batch":
            self.add_batch(data.get("batch_data") or [])
        else:
            self.add_point(
                data.get("measurement"),
                data.get("tags") or {},
                data.get("fields") or {},
                data.get("timestamp"),
            )

    def add_batch(self, batch):
        points = [p for p in batch if isinstance(p, (list, tuple)) and len(p) == 4]

        # Apply oldest first, so a batch of history settles on its latest point.
        def order(point):
            ts = epoch_seconds(point[3])
            return math.inf if ts is None else ts

        for measurement, tags, fields, timestamp in sorted(points, key=order):
            self.add_point(measurement, tags, fields, timestamp)

    def add_point(self, measurement, tags, fields, timestamp=None):
        if not measurement or not isinstance(tags, dict) or not isinstance(fields, dict):
            return
        now = self.clock()
        ts = epoch_seconds(timestamp)
        # A reading's own time, kept even when the sample time is corrected,
        # so a repeat of the same reading is never counted twice.
        reading_ts = ts
        labels = {
            label_name(k): str(v)
            for k, v in tags.items()
            if v is not None and k not in self.drop_labels
        }

        if measurement == FORECAST_HOURLY:
            if ts is None:
                return
            hours_ahead = round((ts - now) / 3600)
            if hours_ahead < 0:
                return
            for key in FORECAST_HOURLY_DROPPED_LABELS:
                labels.pop(key, None)
            labels["hours_ahead"] = str(hours_ahead)
            sample_ts, order_ts = now, now
        elif measurement == FORECAST_DAILY:
            if ts is None or ts < now:
                return
            labels["days_ahead"] = str(int((ts - now) // 86400))
            sample_ts, order_ts = now, now
        elif measurement in SNAPSHOT_MEASUREMENTS:
            sample_ts, order_ts = now, now
        else:
            if ts is None or ts > now + FUTURE_TOLERANCE_SECONDS:
                ts = now
            if now - ts > self.max_sample_age:
                return
            sample_ts, order_ts = ts, ts
        if reading_ts is None:
            reading_ts = sample_ts

        label_sets = [labels]
        if self.primary_source and labels.get("collector_type") == self.primary_source:
            label_sets.append({**labels, "collector_type": "primary"})
        is_observation = measurement not in SNAPSHOT_MEASUREMENTS and measurement not in (
            FORECAST_HOURLY,
            FORECAST_DAILY,
        )
        counted = COUNTED_FIELDS.get(measurement, set())

        with self._lock:
            for label_set in label_sets:
                label_items = tuple(sorted(label_set.items()))
                if is_observation:
                    name = metric_name(measurement, OBSERVED_TIMESTAMP_SUFFIX)
                    self._record((name, label_items), sample_ts, sample_ts, order_ts, now)
                for field, raw in fields.items():
                    if field in SKIPPED_FIELDS:
                        continue
                    value = sample_value(raw)
                    if value is None:
                        continue
                    name = metric_name(measurement, field)
                    is_new = self._record((name, label_items), value, sample_ts, order_ts, now)
                    if field in counted and is_new:
                        total_key = (name + "_total", label_items)
                        # Count a reading only when its own time is later than
                        # the last one counted. _record compares sample times,
                        # which the clock replaces for a future-dated reading,
                        # so a replay could otherwise look new, including one
                        # that falls back inside the tolerance later on. One
                        # number per total keeps memory bounded.
                        last = self._counted_reading.get(total_key)
                        if last is not None and reading_ts <= last:
                            continue
                        if self._add_to_total(total_key, value, sample_ts, order_ts, now):
                            self._counted_reading[total_key] = reading_ts

    def _has_room(self):
        if len(self._latest) < self.max_series:
            return True
        if not self._warned_full and self.logger:
            self.logger.warning(
                f"Prometheus output holds {self.max_series} series; "
                "new series are being dropped."
            )
            self._warned_full = True
        return False

    def _record(self, key, value, sample_ts, order_ts, now):
        """Store a value; returns True when it is a newer reading than the last."""
        current = self._latest.get(key)
        if current is None:
            if not self._has_room():
                return False
        elif order_ts < current[1]:
            return False  # older than what we already have
        is_new = current is None or order_ts > current[1]
        self._latest[key] = [value, order_ts, now]
        self._enqueue(key, value, sample_ts)
        return is_new

    def _add_to_total(self, key, increment, sample_ts, order_ts, now):
        """Add to a running total; returns False when there was no room for it."""
        current = self._latest.get(key)
        if current is None and not self._has_room():
            return False
        total = (current[0] if current else 0.0) + increment
        self._latest[key] = [total, order_ts, now]
        self._counter_names.add(key[0])
        self._enqueue(key, total, sample_ts)
        return True

    def _enqueue(self, key, value, sample_ts):
        if self.queue_samples:
            if len(self._queue) >= self.max_queued_samples:
                del self._queue[: len(self._queue) // 10 or 1]
                if not self._warned_queue and self.logger:
                    self.logger.warning(
                        "Prometheus remote write queue is full; dropping the "
                        "oldest samples."
                    )
                    self._warned_queue = True
            self._queue.append((key, value, int(sample_ts * 1000)))

    # Output

    def prune(self):
        cutoff = self.clock() - self.stale_after
        with self._lock:
            for key in [k for k, v in self._latest.items() if v[2] < cutoff]:
                del self._latest[key]
                self._last_sent.pop(key, None)
                self._counted_reading.pop(key, None)

    def series_count(self):
        with self._lock:
            return len(self._latest)

    def exposition(self):
        """Render current values in the Prometheus text format."""
        self.prune()
        with self._lock:
            items = sorted(self._latest.items())
        lines = []
        current_name = None
        for (name, labels), (value, _order, _updated) in items:
            if name != current_name:
                kind = "counter" if name in self._counter_names else "gauge"
                lines.append(f"# TYPE {name} {kind}")
                current_name = name
            if labels:
                rendered = ",".join(
                    f'{k}="{escape_label_value(v)}"' for k, v in labels
                )
                lines.append(f"{name}{{{rendered}}} {format_value(value)}")
            else:
                lines.append(f"{name} {format_value(value)}")
        return "\n".join(lines) + "\n" if lines else ""

    def drain(self):
        """Take queued samples as [(labels, [(value, ts_ms), ...]), ...].

        Samples at or before the last one sent for a series are dropped, since
        receivers reject out-of-order samples.
        """
        with self._lock:
            queued, self._queue = self._queue, []
            self._warned_queue = False
            by_key = {}
            for key, value, ts_ms in queued:
                by_key.setdefault(key, {})[ts_ms] = value
            series = []
            for key, samples in by_key.items():
                last = self._last_sent.get(key, -1)
                kept = sorted((ts, v) for ts, v in samples.items() if ts > last)
                if not kept:
                    continue
                self._last_sent[key] = kept[-1][0]
                name, labels = key
                series.append(
                    ({"__name__": name, **dict(labels)}, [(v, ts) for ts, v in kept])
                )
        return series

    def requeue(self, series):
        """Put samples back after a failed push, so the next push retries them."""
        with self._lock:
            restored = []
            for labels, samples in series:
                name = labels["__name__"]
                key = (name, tuple(sorted((k, v) for k, v in labels.items() if k != "__name__")))
                first = min(ts for _v, ts in samples)
                if self._last_sent.get(key, -1) >= first:
                    self._last_sent[key] = first - 1
                restored.extend((key, v, ts) for v, ts in samples)
            self._queue = (restored + self._queue)[-self.max_queued_samples :]
