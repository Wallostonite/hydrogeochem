"""Celery application.

PHREEQC work is CPU-bound, so workers use the prefork pool with a low concurrency and
scale horizontally on queue depth rather than vertically on threads.
"""

from __future__ import annotations

from celery import Celery

from ..config import get_settings
from ..logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level, settings.log_format, f"{settings.service_name}-worker")

celery_app = Celery(
    "hgc",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["hgc.worker.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,              # a killed worker re-delivers rather than loses the run
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,     # CPU-bound tasks must not be hoarded by one worker
    task_time_limit=900,
    task_soft_time_limit=840,
    result_expires=86_400,
    broker_transport_options={"visibility_timeout": 1800},
    task_default_queue="runs",
    task_routes={"hgc.batch.*": {"queue": "batches"}},
)
