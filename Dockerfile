# syntax=docker/dockerfile:1

# Base images are Docker Hardened Images (DHI) from dhi.io, which needs a
# `docker login dhi.io` with a Docker Hub account before building.

# Build stage: the DHI Python -dev variant (root, with a shell and pip).
FROM dhi.io/python:3.15-debian13-dev@sha256:e341dc2363f0bd99a058683a9bcbf79b994c4670ded4924a099ed9c1a6cf7f54 AS build
RUN python3 -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
    && /venv/bin/pip uninstall --yes pip
# Writable directory for the station metadata cache, owned by the nonroot user.
RUN mkdir -p /app/conf && chown 65532:65532 /app/conf

# Runtime stage: the matching DHI Python runtime, with no shell or package
# manager, running as the nonroot user (UID 65532). The venv links to its
# interpreter, so build and runtime must stay on the same Python tag.
FROM dhi.io/python:3.15-debian13@sha256:ac18385fab4463424ca98f364a8a5b904271dbf681f1c06b14b191108b4080ae
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
