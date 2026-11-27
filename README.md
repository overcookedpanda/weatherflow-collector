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
- **Smaller surface:** the export processor, file storage, public websocket
  server and a private plugin import (which stopped source builds from
  starting) are gone.
- **Upkeep:** CI with lint, tests, a secret scan and an image vulnerability
  scan; Dependabot for Python, base images and Actions.

Planned next: a Prometheus output, so data can go to Prometheus-compatible
stores (Grafana Cloud, Amazon Managed Service for Prometheus, Mimir,
VictoriaMetrics) instead of InfluxDB, with matching dashboards.

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
50222. Today it writes to InfluxDB v2; the dashboards in `grafana/dashboards`
use InfluxQL against that bucket.

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
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null \
  && echo "Verified: $IMAGE@$DIGEST"
```

`set -eu` stops at the first failure, and the reference is printed only after
the verify succeeds. Use the printed `image@sha256:...` reference as the image
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
