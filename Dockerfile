# syntax=docker/dockerfile:1

# Base images are Docker Hardened Images (DHI) from dhi.io, which needs a
# `docker login dhi.io` with a Docker Hub account before building.

# Build stage: the DHI Python -dev variant (root, with a shell and pip).
FROM dhi.io/python:3.13-debian13-dev@sha256:ffb298fd19247236736952037a0e24dc1f8c7c382da8525158f655f89b1ba4aa AS build
RUN python3 -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
    && /venv/bin/pip uninstall --yes pip
# Writable directory for the station metadata cache, owned by the nonroot user.
RUN mkdir -p /app/conf && chown 65532:65532 /app/conf

# Runtime stage: the matching DHI Python runtime, with no shell or package
# manager, running as the nonroot user (UID 65532). The venv links to its
# interpreter, so build and runtime must stay on the same Python tag.
FROM dhi.io/python:3.13-debian13@sha256:34feca93f54e15dce5d4ee311ed0b01c6ea8a96869e8d2627e20f85696e2e527
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
