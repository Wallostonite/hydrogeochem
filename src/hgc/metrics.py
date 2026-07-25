"""Prometheus instrumentation. Names are stable API; changing one breaks dashboards."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

PHREEQC_RUN_SECONDS = Histogram(
    "hgc_phreeqc_run_seconds",
    "Wall time of a PHREEQC execution",
    labelnames=("database", "outcome"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60),
)

PHREEQC_POOL_RECYCLES = Counter(
    "hgc_phreeqc_pool_recycles_total",
    "Times the worker pool was torn down (timeout or crash)",
    labelnames=("reason",),
)

UPSTREAM_REQUEST_SECONDS = Histogram(
    "hgc_upstream_request_seconds",
    "Latency of calls to USGS services",
    labelnames=("service", "outcome"),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

CACHE_EVENTS = Counter(
    "hgc_cache_events_total", "Cache hits and misses", labelnames=("namespace", "event")
)

RUNS_TOTAL = Counter(
    "hgc_runs_total", "Model runs by terminal status", labelnames=("status", "mode")
)

QUEUE_DEPTH = Gauge("hgc_queue_depth", "Pending run tasks", labelnames=("queue",))

HTTP_REQUEST_SECONDS = Histogram(
    "hgc_http_request_seconds",
    "API request latency",
    labelnames=("method", "route", "status"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
