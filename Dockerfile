# syntax=docker/dockerfile:1.7
# Multi-stage: one image serves the API and the worker; the UI runs the same image
# with a different command, so there is exactly one artifact to promote per release.

FROM python:3.11-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HGC_PHREEQC_DATABASE_DIR=/opt/phreeqc/database
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install ".[ui]"

FROM base AS runtime
# PHREEQC databases are pinned in the image and checksum-verified at startup:
# a silently changed llnl.dat would invalidate every historical saturation index.
COPY ops/phreeqc-databases /opt/phreeqc/database
COPY --from=builder /install /usr/local
COPY src /app/src
WORKDIR /app
ENV PYTHONPATH=/app/src

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin hgc \
    && chown -R hgc:hgc /app
USER 10001

EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "hgc.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
