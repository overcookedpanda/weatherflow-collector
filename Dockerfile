# syntax=docker/dockerfile:1

# Build stage: Debian 13 with its Python 3.13, matching the distroless runtime.
FROM debian:13-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0 AS build
RUN apt-get update \
    && apt-get install --no-install-recommends --yes python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
    && /venv/bin/pip uninstall --yes pip
# Writable directory for the station metadata cache, owned by the nonroot user.
RUN mkdir -p /app/conf && chown 65532:65532 /app/conf

# Runtime stage: distroless Python, no shell or package manager, nonroot user.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:72de1dda9d4c52c370e2ef2d93c28e3e1a8193c92058b7dfa42bace38a83d114
LABEL org.opencontainers.image.source="https://github.com/overcookedpanda/weatherflow-collector" \
      org.opencontainers.image.description="WeatherFlow Tempest collector (hardened fork of lux4rd0/weatherflow-collector)" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /venv /venv
COPY --from=build --chown=65532:65532 /app/conf /app/conf
COPY src/ /app/
USER 65532:65532
ENTRYPOINT ["/venv/bin/python3", "/app/weatherflow-collector.py"]
