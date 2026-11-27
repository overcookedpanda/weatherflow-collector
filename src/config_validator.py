# config_validator.py

import os
from urllib.parse import urlsplit

import config
import logger

logger_ConfigValidator = logger.get_module_logger()


def obfuscate_token(token):
    if token and len(token) > 10:
        return token[:5] + "..." + token[-5:]
    return token


def validate_api_config():
    if not config.WEATHERFLOW_COLLECTOR_API_TOKEN:
        logger_ConfigValidator.error("WeatherFlow API token is not set.")
        return False
    else:
        obscured_token = obfuscate_token(config.WEATHERFLOW_COLLECTOR_API_TOKEN)
        logger_ConfigValidator.info(
            f"WeatherFlow API token is set (partially shown): {obscured_token}"
        )
    return True


def validate_influxdb_config():
    missing_config = []
    if not config.WEATHERFLOW_COLLECTOR_INFLUXDB_URL:
        missing_config.append("WEATHERFLOW_COLLECTOR_INFLUXDB_URL")
    else:
        logger_ConfigValidator.info(
            f"InfluxDB URL is set to: {config.WEATHERFLOW_COLLECTOR_INFLUXDB_URL}"
        )

    if not config.WEATHERFLOW_COLLECTOR_INFLUXDB_TOKEN:
        missing_config.append("WEATHERFLOW_COLLECTOR_INFLUXDB_TOKEN")
    else:
        obscured_token = obfuscate_token(config.WEATHERFLOW_COLLECTOR_INFLUXDB_TOKEN)
        logger_ConfigValidator.info(
            f"InfluxDB Token is set (partially shown): {obscured_token}"
        )

    if not config.WEATHERFLOW_COLLECTOR_INFLUXDB_ORG:
        missing_config.append("WEATHERFLOW_COLLECTOR_INFLUXDB_ORG")
    else:
        logger_ConfigValidator.info(
            f"InfluxDB Organization is set to: {config.WEATHERFLOW_COLLECTOR_INFLUXDB_ORG}"
        )

    if not config.WEATHERFLOW_COLLECTOR_INFLUXDB_BUCKET:
        missing_config.append("WEATHERFLOW_COLLECTOR_INFLUXDB_BUCKET")
    else:
        logger_ConfigValidator.info(
            f"InfluxDB Bucket is set to: {config.WEATHERFLOW_COLLECTOR_INFLUXDB_BUCKET}"
        )

    if missing_config:
        logger_ConfigValidator.error(
            f"Missing InfluxDB configuration(s): {', '.join(missing_config)}."
        )
        return False

    return True


def validate_module_enablement():
    logger_ConfigValidator.info("Module Enablement Status:")

    # Function to log the status of each module
    def log_module_status(setting_name, is_enabled):
        status = "Enabled" if is_enabled else "Disabled"
        logger_ConfigValidator.info(f"{setting_name} module is {status}")

    # Log the status for each module
    log_module_status(
        "Storage InfluxDB", config.WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED
    )
    log_module_status(
        "Collector REST Forecasts",
        config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_FORECASTS_ENABLED,
    )
    log_module_status(
        "Collector REST Import",
        config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_IMPORT_ENABLED,
    )
    log_module_status(
        "Collector REST Observations Device",
        config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_DEVICE_ENABLED,
    )
    log_module_status(
        "Collector REST Observations Station",
        config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_STATION_ENABLED,
    )
    log_module_status(
        "Collector REST Stats",
        config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_STATS_ENABLED,
    )
    log_module_status(
        "System Metrics", config.WEATHERFLOW_COLLECTOR_SYSTEM_METRICS_ENABLED
    )
    log_module_status(
        "Collector UDP", config.WEATHERFLOW_COLLECTOR_COLLECTOR_UDP_ENABLED
    )
    log_module_status(
        "Collector WebSocket", config.WEATHERFLOW_COLLECTOR_COLLECTOR_WEBSOCKET_ENABLED
    )
    log_module_status(
        "Provider WebSocket Server",
        config.WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ENABLED,
    )
    log_module_status(
        "Storage Prometheus scrape endpoint",
        config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_ENABLED,
    )
    log_module_status(
        "Storage Prometheus remote write",
        config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED,
    )
    log_module_status("Handler", config.WEATHERFLOW_COLLECTOR_HANDLER_ENABLED)

    return True


REMOTE_WRITE_AUTH_MODES = ("none", "sigv4", "basic", "bearer")


def warn_prometheus_ages():
    """Warn when an age limit is shorter than the poll interval it must cover."""
    max_age = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_MAX_SAMPLE_AGE
    stale = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_STALE_AFTER
    observations = config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_OBSERVATIONS_INTERVAL
    forecasts = config.WEATHERFLOW_COLLECTOR_COLLECTOR_REST_FORECASTS_FETCH_INTERVAL
    warnings = []
    if max_age <= observations:
        warnings.append(
            f"MAX_SAMPLE_AGE ({max_age}s) is not above the REST observations interval "
            f"({observations}s), so REST observations may be skipped"
        )
    if stale <= max(observations, forecasts):
        warnings.append(
            f"STALE_AFTER ({stale}s) is not above the REST poll intervals, so series "
            "may expire between polls"
        )
    for warning in warnings:
        logger_ConfigValidator.warning(f"Prometheus output: {warning}.")
    return warnings


def validate_prometheus_config():
    """Check the Prometheus remote write settings. Secrets are never logged."""
    if config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_ENABLED:
        warn_prometheus_ages()
    if not config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED:
        return True

    problems = []
    url = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL
    auth = config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH
    if not url:
        problems.append("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL is not set")
    else:
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            problems.append(
                "the remote write URL must not contain a user name or password; use "
                "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH instead"
            )
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            problems.append("the remote write URL must be an http or https URL")
        elif parsed.scheme != "https" and auth != "none":
            problems.append("the remote write URL must use https when credentials are sent")

    if auth not in REMOTE_WRITE_AUTH_MODES:
        problems.append(
            "WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH must be one of "
            + ", ".join(REMOTE_WRITE_AUTH_MODES)
        )
    elif auth == "sigv4":
        if not config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_SIGV4_REGION:
            problems.append(
                "set WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_SIGV4_REGION "
                "or AWS_REGION for sigv4"
            )
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if not os.environ.get(name):
                problems.append(f"{name} is not set, and sigv4 needs it")
    elif auth == "basic":
        if not config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_USERNAME:
            problems.append("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_USERNAME is not set")
        if not config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_PASSWORD:
            problems.append("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_PASSWORD is not set")
    elif auth == "bearer":
        if not config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_BEARER_TOKEN:
            problems.append("WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_BEARER_TOKEN is not set")

    if config.WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_INTERVAL < 1:
        problems.append("the remote write interval must be at least 1 second")

    for problem in problems:
        logger_ConfigValidator.error(f"Prometheus remote write: {problem}.")
    if not problems:
        logger_ConfigValidator.info(f"Prometheus remote write auth: {auth}")
    return not problems


def validate_all():
    results = [validate_api_config(), validate_module_enablement()]
    # Only check the outputs that are turned on, so a Prometheus-only setup
    # doesn't need InfluxDB settings.
    if config.WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED:
        results.append(validate_influxdb_config())
    results.append(validate_prometheus_config())
    return all(results)
