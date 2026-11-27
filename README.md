# weatherflow-collector

![CodeRabbit Pull Request Reviews](https://img.shields.io/coderabbit/prs/github/overcookedpanda/weatherflow-collector?utm_source=oss&utm_medium=github&utm_campaign=overcookedpanda%2Fweatherflow-collector&labelColor=171717&color=FF570A&link=https%3A%2F%2Fcoderabbit.ai&label=CodeRabbit+Reviews)

Collects data from a [WeatherFlow Tempest](https://tempest.earth) weather
station (local UDP broadcast, the Tempest websocket and REST APIs, including
forecasts) and stores it for Grafana dashboards: current conditions, rain and
lightning (with a strike-distance heat map), forecast vs observed, and more.

This is a maintained, hardened fork of
[lux4rd0/weatherflow-collector](https://github.com/lux4rd0/weatherflow-collector)
by Dave Schmid, which has had no commits since July 2024. All credit for the
original design, collectors and dashboards goes to him. It also carries the
station metadata retry fix and a field fix from
[jay23606/weatherflow-collector](https://github.com/jay23606/weatherflow-collector).
See [NOTICE](NOTICE) for what changed, and
[docs/UPSTREAM_README.md](docs/UPSTREAM_README.md) for the original
documentation, which still describes most settings.

## What's different here

- **Hardened image:** multi-stage build on
  [Docker Hardened Images](https://docs.docker.com/dhi/) Python 3.13, with no
  shell or package manager at runtime, nonroot, and run with a read-only root
  filesystem and no capabilities.
- **Signed releases:** images on GHCR with SBOM and provenance attestations,
  signed keyless with cosign.
- **Smaller surface:** the export processor, file storage and a private
  plugin import (which stopped source builds from starting) are gone. The
  websocket server is now off by default and listens only on localhost.
- **Prometheus output (opt in):** a scrape endpoint, remote write (Amazon
  Managed Service for Prometheus with SigV4, Grafana Cloud, Mimir), or both,
  beside InfluxDB or instead of it.
- **Upkeep:** CI with lint, tests, a secret scan and an image vulnerability
  scan; Dependabot for Python, base images and Actions.

## Quick start

1. Create a personal access token at tempestwx.com (Settings, Data
   Authorizations).
2. Copy the examples and fill in your values:

   ```sh
   cp .env.example .env
   cp compose.example.yaml compose.yaml
   ```

3. Start it and check the logs:

   ```sh
   docker compose up -d
   docker compose logs -f weatherflow-collector
   ```

The collector needs host networking to hear the hub's UDP broadcast on port
50222. By default it writes to InfluxDB v2, and the dashboards in
`grafana/dashboards` use InfluxQL against that bucket. Nothing changes for
InfluxDB users unless they turn on the outputs below.

## Prometheus output

Off by default. It runs beside InfluxDB, or on its own if you set
`WEATHERFLOW_COLLECTOR_STORAGE_INFLUXDB_ENABLED=False` (InfluxDB settings are
then not required). Turn on either output, or both.

**Scrape endpoint:** serves `/metrics` for a Prometheus or Grafana Alloy
scraper.

```sh
WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_ENABLED=True
WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_HOST=127.0.0.1   # default
WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_SCRAPE_PORT=9110        # default
```

It has no authentication. With host networking, setting the host to `0.0.0.0`
or a LAN address lets anyone on that network read your weather data.

**Remote write:** pushes new samples every 30 seconds.

```sh
WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_ENABLED=True
WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_URL=<REMOTE_WRITE_URL>
WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_REMOTE_WRITE_AUTH=sigv4  # none, sigv4, basic or bearer
```

- **Amazon Managed Service for Prometheus:** `AUTH=sigv4`, the workspace's
  remote write URL, `..._REMOTE_WRITE_SIGV4_REGION` (or `AWS_REGION`), and
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and, for temporary credentials,
  `AWS_SESSION_TOKEN`. Give that IAM identity only `aps:RemoteWrite` on the
  workspace.
- **Grafana Cloud or Mimir:** `AUTH=basic`, with
  `..._REMOTE_WRITE_USERNAME` (the instance ID) and `..._REMOTE_WRITE_PASSWORD`
  (an access policy token with metrics write).
- **Bearer token:** `AUTH=bearer` with `..._REMOTE_WRITE_BEARER_TOKEN`.

Credentials are read from the environment and never logged, and the URL must
use https when credentials are sent. Put them in `.env`, not in compose files.

**What you get:** each numeric field becomes a gauge named
`<measurement>_<field>`, such as `weatherflow_obs_air_temperature`, labelled
with the station, device and collector type. Hourly and daily forecasts get an
`hours_ahead` or `days_ahead` label instead of future timestamps. Station
coordinates, elevation and time zone are left out of the labels; change that
with `WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_DROP_LABELS`. History imported
with the REST import collector is not sent, since Prometheus stores rarely
accept old samples. Other settings, with defaults:

| Setting (prefix `WEATHERFLOW_COLLECTOR_STORAGE_PROMETHEUS_`) | Default |
| --- | --- |
| `REMOTE_WRITE_INTERVAL` (seconds) | 30 |
| `REMOTE_WRITE_TIMEOUT` (seconds) | 30 |
| `STALE_AFTER` (seconds without a reading before a series is dropped) | 900 |
| `MAX_SAMPLE_AGE` (seconds; older observations are skipped) | 600 |
| `MAX_SERIES` | 10000 |

Prometheus versions of the dashboards are in `grafana/dashboards/prometheus`.

## Websocket server

Off by default. It rebroadcasts live rapid wind readings to websocket
clients, such as Grafana's websocket datasource, at
`ws://HOST:6789/<collector_type>/<device_id>`.

```sh
WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_ENABLED=True
WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_HOST=127.0.0.1   # default
WEATHERFLOW_COLLECTOR_PROVIDER_WEBSOCKET_SERVER_PORT=6789        # default
```

It has no authentication, and each message includes the station's name and
coordinates. Upstream it listened on all interfaces; here it listens only on
localhost. With host networking, setting the host to `0.0.0.0` or a LAN address
lets anyone on that network read the live feed, so do that only on a network
you trust. A sample datasource is in `grafana/datasources`; replace its
placeholder host.

## Verify the image

A release builds the image once, pushes it by digest, scans that digest
for HIGH and CRITICAL issues, signs it and verifies the signature. Only then
do `latest` (from `main`) and the version tags (from `v*` tags) move to it,
so a failed or interrupted release never leaves a tag on an unchecked image.

Check a release yourself with cosign 3. Resolve the tag to a digest once,
verify that digest, and deploy that same digest, so a tag that moves in
between can't swap the image. The verify accepts only signatures made by this
repo's release workflow on `main` or a version tag:

```sh
set -eu
IMAGE=ghcr.io/overcookedpanda/weatherflow-collector
DIGEST=$(docker buildx imagetools inspect "$IMAGE:latest" --format '{{ .Manifest.Digest }}')
cosign verify "$IMAGE@$DIGEST" \
  --certificate-identity-regexp '^https://github.com/overcookedpanda/weatherflow-collector/\.github/workflows/release\.yml@refs/(heads/main|tags/v.+)$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null
echo "Verified: $IMAGE@$DIGEST"
```

`set -eu` stops at the first failed command, so the reference is printed only
after the verify succeeds. Use the printed `image@sha256:...` reference as the image
in your compose file. If nothing is printed, don't deploy.

## Development

Building the image yourself needs a free Docker Hub account and
`docker login dhi.io`, because the base images come from Docker Hardened
Images. The published image on GHCR needs no login.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Never commit tokens, station or
device IDs, coordinates or hostnames; CI and the pre-commit hooks scan for
secrets.

## License

[AGPL-3.0](LICENSE), as the original.
